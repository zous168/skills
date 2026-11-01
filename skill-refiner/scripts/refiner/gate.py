"""Machine-checkable finalization gate for completed Skill Refiner runs."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .common import RefinerError, digest, manifest, read_json
from .artifact_validation import validate_skill_schema_package
from .stages import verify_stage_manifest
from .obligations import validate_obligation_plans, validate_obligation_results


def _verify_phase_history(state):
    phases = state.get("phase_results")
    expected_phase_statuses = ["not_started", "pending", "running", "passed", "failed", "error", "skipped", "reused"]
    expected_gate_statuses = ["pending", "running", "passed", "not_passed"]
    if (not isinstance(phases, dict)
            or phases.get("schema_version") != "skill-refiner.phase-results/v1"
            or phases.get("status_enums") != {"phase": expected_phase_statuses,
                                               "plan_gate": expected_gate_statuses}):
        raise RefinerError("Phase result state is missing or does not match the versioned status contract")
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        if entry.get("status") not in {"passed", "reused"}:
            raise RefinerError(f"Plan phase {name} is not complete: {entry.get('status')!r}")
        suites = entry.get("suites", {})
        if any(suites.get(suite) not in {"passed", "reused"}
               for suite in ("development", "acceptance")):
            raise RefinerError(f"Plan phase {name} lacks passed development and acceptance evidence")
    if phases.get("plan_gate", {}).get("status") != "passed":
        raise RefinerError(f"Plan gate is not passed: {phases.get('plan_gate', {}).get('status')!r}")
    evaluation = phases.get("skill_evaluation", {})
    if evaluation.get("status") != "passed" or any(
            evaluation.get("suites", {}).get(suite) != "passed"
            for suite in ("baseline", "regression", "acceptance")):
        raise RefinerError("Target Skill evaluation lacks passed baseline, regression and acceptance evidence")
    if phases.get("final_verification", {}).get("status") not in {"running", "passed"}:
        raise RefinerError("Final Refiner verification has not reached its machine gate")

    history = state.get("phase_history")
    if not isinstance(history, list):
        raise RefinerError("Phase history is missing")
    gate_passes = [row.get("sequence", index) for index, row in enumerate(history)
                   if isinstance(row, dict) and row.get("phase") == "plan_gate"
                   and row.get("status") == "passed"]
    evaluations = [row.get("sequence", index) for index, row in enumerate(history)
                   if isinstance(row, dict) and row.get("phase") == "skill_evaluation"
                   and row.get("status") == "running"]
    if not gate_passes or not evaluations or min(gate_passes) > min(evaluations):
        raise RefinerError("Target Skill evaluation started without an earlier passed plan gate")


def _verify_plan_control_artifacts(root, plans):
    from .checks import validate_plan
    from .common import PlanValidationError
    from .requirement_revision import unapproved_recovery, requirement_revision
    from .pipeline import (verified_response_sources, load_response_applicability, response_applicability_lock,
                           validate_plan_review, _response_source_contents, _review_target_cases, _validation_error_case_ids,
                           _validation_error_diagnostic, _validation_error_feedback, _authorized_execution_revision,
                           _recovery_feedback_material, _compact_historical_revision_failures)

    if any(plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} for plan in plans.values()):
        bindings, inputs = verified_response_sources(root, include_inputs=True)
        actual = response_applicability_lock(root, inputs, plans, bindings)
        lock = read_json(_required_file(root, "oracle.lock.json"))
        if lock.get("response_applicability_sha256", {}) != actual:
            raise RefinerError("Frozen response applicability decision hash mismatch")
    for suite, plan in plans.items():
        sources, source_inputs = (verified_response_sources(root, include_inputs=True)
                                  if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else (None, None))
        summary_path = _required_file(root, f"plan-controls/{suite}.json")
        summary = read_json(summary_path)
        if (summary.get("schema_version") != "skill-refiner.plan-controls/v1"
                or summary.get("suite") != suite or summary.get("status") != "passed"
                or summary.get("plan_sha256") != digest(plan)
                or summary.get("plan_snapshot") != plan
                or summary.get("revision_failures", [])):
            raise RefinerError(f"{suite} plan control summary does not bind to the frozen plan")

        attempts_dir = root / "plan-controls" / "attempts"
        attempts = []
        preserved_requirements = None
        preserved_cases = None
        preserved_plan = None
        review_targets = set()
        expected_feedback = []
        expected_sources = []
        provisional_case_ids = set()
        provisional_sources = {}
        latest_draft = None
        latest_draft_attempt = None
        provisional_context_seen = False
        historic_execution_revision = []
        recovered = None
        resume_path = root / "resume-contract.json"
        resume = read_json(resume_path) if resume_path.is_file() else {}
        if suite == "development" and sources is not None and resume.get("refine_plan_recovery_sha256"):
            from .refine_plan_recovery import verify_recovery
            recovered = verify_recovery(root, resume)
            if recovered.get("control_records"):
                material = _recovery_feedback_material(recovered, root, source_inputs, sources)
                preserved_plan = material["reference"]["previous_plan"]
                preserved_requirements = {row["id"] for row in preserved_plan.get("requirements", [])}
                preserved_cases = {row["id"] for row in preserved_plan.get("cases", [])}
                review_targets.update(material["case_ids"])
                expected_feedback.extend(material["feedback"])
                expected_sources.extend(material["sources"])
                historic_execution_revision = material["execution_revision"]
        if attempts_dir.is_dir():
            for path in sorted(attempts_dir.glob(f"{suite}-*.json")):
                attempt = read_json(path)
                if (attempt.get("schema_version") != "skill-refiner.plan-controls/v1"
                        or attempt.get("suite") != suite
                        or not isinstance(attempt.get("attempt"), int)
                        or attempt.get("plan_sha256") != digest(attempt.get("plan_snapshot"))):
                    raise RefinerError(f"Malformed {suite} plan-control attempt evidence: {path.name}")
                if attempt["attempt"] != len(attempts) + 1:
                    raise RefinerError(f"{suite} plan-revision attempt history has a gap")
                control_exception = None
                structure_exception = None
                applicability = (load_response_applicability(root, source_inputs, attempt["plan_snapshot"], sources)
                                 if sources is not None else None)
                try:
                    recomputed = validate_plan(attempt["plan_snapshot"], root, max_cases=30,
                                               verified_response_sources=sources,
                                               required_response_contract_cases=(applicability["required_case_ids"]
                                                                                 if applicability is not None else None))
                except PlanValidationError as exc:
                    control_exception = exc
                    expected_status = exc.evidence.get("status")
                    expected_cases = exc.evidence.get("case_control_results")
                    expected_failures = exc.evidence.get("control_failures")
                    expected_coverage = exc.evidence.get("coverage_failures")
                except RefinerError as exc:
                    structure_exception = exc
                    expected_status = "validation_error"
                    expected_cases, expected_failures, expected_coverage = [], [], []
                    diagnostic = _validation_error_diagnostic(attempt["plan_snapshot"], exc)
                    recorded = attempt.get("validation_error")
                    # Earlier records predate the structured fields; verify their
                    # original three fields without inventing retroactive scope.
                    if isinstance(recorded, dict) and set(recorded) == {"type", "message", "case_ids"}:
                        diagnostic = {key: diagnostic[key] for key in recorded}
                    if recorded != diagnostic:
                        raise RefinerError(f"{suite} plan validation diagnostic differs from recomputation: {path.name}") from exc
                else:
                    expected_status = "passed"
                    expected_cases = recomputed.get("case_control_results")
                    expected_failures = []
                    expected_coverage = []
                revision_failures = attempt.get("revision_failures", [])
                if not isinstance(revision_failures, list) or any(not isinstance(row, dict) for row in revision_failures):
                    raise RefinerError(f"Malformed {suite} plan-revision failure evidence: {path.name}")
                # Independent positive/negative controls can pass while the
                # overall revision fails to preserve its frozen matrix.
                if expected_status == "passed" and revision_failures:
                    expected_status = "failed"
                snapshot = attempt["plan_snapshot"]
                current_provisional = {row["id"] for row in snapshot.get("cases", [])
                                       if isinstance(row, dict) and isinstance(row.get("id"), str)}
                provisional = attempt.get("provisional_context")
                # Older v7 attempts predate this field. They are compatible only
                # while no draft case is removed and no newer context has appeared.
                # A missing field can never authorize a provisional deletion.
                if provisional is None and snapshot.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
                    if provisional_context_seen or provisional_case_ids - current_provisional:
                        raise RefinerError(f"{suite} provisional draft handoff missing from control history: {path.name}")
                if provisional is not None:
                    provisional_context_seen = True
                    expected_provisional = {"schema_version": "skill-refiner.provisional-plan/v1",
                        "source_attempt": latest_draft_attempt,
                        "latest_plan_sha256": digest(latest_draft) if latest_draft is not None else None,
                        "case_ids": sorted(provisional_case_ids),
                        "removed_case_ids": sorted(provisional_case_ids - current_provisional),
                        "added_case_ids": sorted(current_provisional - provisional_case_ids),
                        "removed_case_sources": [provisional_sources[x] for x in sorted(provisional_case_ids - current_provisional)] if snapshot.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else [],
                        "removal_schema_sha256": (digest(read_json(Path(__file__).resolve().parents[2] / "schemas/v7/provisional-deletion-review.schema.json"))
                            if snapshot.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} and provisional_case_ids - current_provisional else None)}
                    if provisional != expected_provisional:
                        raise RefinerError(f"{suite} provisional draft handoff differs from control history: {path.name}")
                revision_context = attempt.get("revision_context")
                if snapshot.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
                    if not isinstance(revision_context, dict):
                        raise RefinerError(f"{suite} v7 plan-revision context is missing")
                    import jsonschema
                    schema = read_json(Path(__file__).resolve().parents[2] / "schemas/v7/plan-revision-context.schema.json")
                    try:
                        jsonschema.validate(revision_context, schema)
                    except jsonschema.ValidationError as exc:
                        raise RefinerError(f"{suite} invalid v7 plan-revision context") from exc
                    old = {row["id"]: row for row in (preserved_plan or {}).get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    new = {row["id"]: row for row in snapshot.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    changes = [{"case_id": key, "before": old.get(key), "after": new.get(key)}
                               for key in sorted(old.keys() | new.keys()) if preserved_plan and old.get(key) != new.get(key)]
                    expected_context = {"schema_version": "skill-refiner.plan-revision-context/v1",
                        "base_plan_sha256": digest(preserved_plan) if preserved_plan else None,
                        "feedback": expected_feedback, "feedback_sources": expected_sources,
                        "targeted_case_ids": sorted(set(old).intersection(review_targets)) if preserved_plan else [],
                        "case_changes": changes,
                        "requirements_changed": bool(preserved_plan and preserved_plan.get("requirements") != snapshot.get("requirements")),
                        "execution_plan_changed": bool(preserved_plan and preserved_plan.get("execution_plan") != snapshot.get("execution_plan"))}
                    if revision_context != expected_context:
                        raise RefinerError(f"{suite} plan-revision feedback or complete differences do not match history: {path.name}")
                requirement_ids = {row["id"] for row in snapshot.get("requirements", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                case_ids = {row["id"] for row in snapshot.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                if preserved_requirements is None:
                    preserved_requirements, preserved_cases = requirement_ids, case_ids
                else:
                    missing_requirements = preserved_requirements - requirement_ids
                    missing_cases = preserved_cases - case_ids
                    if missing_requirements or missing_cases:
                        failure_kinds = {row.get("kind") for row in revision_failures}
                        if attempt.get("status") not in {"failed", "invalid_coverage", "validation_error"} or "dropped_frozen_matrix_entries" not in failure_kinds:
                            raise RefinerError(f"{suite} plan-revision history lost a preserved requirement or case: {path.name}")
                    elif attempt.get("status") == "passed":
                        preserved_requirements, preserved_cases = requirement_ids, case_ids
                if preserved_plan is not None and snapshot is not preserved_plan:
                    changed_requirements = snapshot.get("requirements") != preserved_plan.get("requirements")
                    from .pipeline import _verified_planning_capacity
                    planning_capacity = (_verified_planning_capacity(root, recovered, resume.get("options", {}).get("max_cases"))
                                         if recovered else None)
                    current_execution_revision = _authorized_execution_revision(preserved_plan, snapshot, expected_sources,
                                                                                verified_capacity=planning_capacity,
                                                                                unapproved_history=unapproved_recovery(recovered))
                    execution_revision = (historic_execution_revision + current_execution_revision
                                          if current_execution_revision is not None else None)
                    changed_execution = execution_revision is None
                    if attempt.get("execution_plan_revision", execution_revision) != execution_revision:
                        raise RefinerError(f"{suite} execution plan revision differs from verified feedback: {path.name}")
                    old_cases = {row["id"]: row for row in preserved_plan.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    new_cases = {row["id"]: row for row in snapshot.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    unrelated = {key for key, value in old_cases.items() if key in new_cases and new_cases[key] != value and key not in review_targets}
                    kinds = {row.get("kind") for row in revision_failures}
                    requirement_change_reviewable = False
                    if changed_requirements and unapproved_recovery(recovered):
                        try:
                            expected_requirement_revision = requirement_revision(recovered["control_records"][0]["plan_snapshot"], snapshot)
                            requirement_revision(preserved_plan, snapshot)
                        except RefinerError:
                            if "invalid_requirement_revision" not in kinds and "changed_frozen_requirement_contract" not in kinds:
                                raise RefinerError(f"{suite} invalid requirement revision lacks failure evidence: {path.name}")
                        else:
                            if attempt.get("requirement_revision") == expected_requirement_revision:
                                requirement_change_reviewable = True
                            elif "changed_frozen_requirement_contract" not in kinds:
                                raise RefinerError(f"{suite} requirement revision evidence differs from history: {path.name}")
                    if (changed_requirements and not requirement_change_reviewable and "changed_frozen_requirement_contract" not in kinds and "invalid_requirement_revision" not in kinds or
                            changed_execution and "changed_frozen_execution_plan" not in kinds or
                            unrelated and "modified_cases_outside_feedback_scope" not in kinds):
                        raise RefinerError(f"{suite} plan-revision semantic evidence differs from preserved plan: {path.name}")
                if preserved_plan is None:
                    preserved_plan = snapshot
                review_path = root / "plan-reviews" / "attempts" / f"{suite}-{attempt['attempt']:02}.json"
                if review_path.is_file():
                    if attempt.get("status") != "passed":
                        raise RefinerError(f"{suite} review cannot follow a failed plan self-check")
                    prior_review = read_json(review_path)
                    if (prior_review.get("plan_sha256") != attempt.get("plan_sha256") or
                            prior_review.get("plan_snapshot") != snapshot or
                            prior_review.get("suite") != suite or prior_review.get("attempt") != attempt['attempt']):
                        raise RefinerError(f"{suite} review does not bind its plan revision")
                    review = prior_review.get("review")
                    validate_plan_review(review, snapshot, prior_review.get("recovery_revision"),
                                         prior_review.get("execution_plan_revision"), provisional,
                                         _response_source_contents(source_inputs) if sources is not None else None,
                                         attempt.get("requirement_revision"), source_inputs)
                    if prior_review.get("status") != ("passed" if review.get("approved") is True else "rejected"):
                        raise RefinerError(f"{suite} review status disagrees with its assessment")
                    if prior_review.get("status") == "rejected" and review.get("approved") is False:
                        expected_feedback.extend(review.get("issues", []))
                        if snapshot.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
                            affected = sorted(_review_target_cases(review, snapshot))
                            review_targets.update(affected)
                            expected_sources.append({"kind": "independent_review", "attempt": attempt["attempt"],
                                                     "review_sha256": digest(review), "case_ids": affected})
                        else:
                            # Historical review format had only free-text issues.
                            issue_text = "\n".join(review.get("issues", []))
                            review_targets.update(case_id for case_id in case_ids
                                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(case_id)}(?![A-Za-z0-9_-])", issue_text, re.I))
                        preserved_plan = snapshot
                if (attempt.get("status") != expected_status
                        or digest(attempt.get("case_control_results")) != digest(expected_cases)
                        or digest(attempt.get("control_failures")) != digest(expected_failures)
                        or digest(attempt.get("coverage_failures")) != digest(expected_coverage)):
                    raise RefinerError(f"{suite} plan-control evidence differs from the complete recomputed controls: {path.name}")
                invalid_path = root / "plan-reviews" / "invalid" / f"{suite}-{attempt['attempt']:02}.json"
                if invalid_path.is_file():
                    if attempt.get("status") != "passed" or review_path.is_file():
                        raise RefinerError(f"{suite} invalid review has inconsistent plan self-check")
                    invalid = read_json(invalid_path)
                    if (invalid.get("schema_version") != "skill-refiner.invalid-plan-review/v1"
                            or invalid.get("suite") != suite or invalid.get("attempt") != attempt["attempt"]
                            or invalid.get("plan_sha256") != digest(snapshot) or invalid.get("plan_snapshot") != snapshot):
                        raise RefinerError(f"{suite} invalid review evidence does not bind its plan")
                    try:
                        validate_plan_review(invalid.get("review"), snapshot, invalid.get("recovery_revision"), invalid.get("execution_plan_revision"),
                                             provisional, _response_source_contents(source_inputs) if sources is not None else None,
                                             attempt.get("requirement_revision"), source_inputs)
                    except RefinerError as exc:
                        if str(exc) != invalid.get("error"):
                            raise RefinerError(f"{suite} invalid review diagnostic changed") from exc
                        expected_feedback.append(str(exc))
                    else:
                        raise RefinerError(f"{suite} invalid review unexpectedly passes validation")
                if control_exception is not None:
                    expected_feedback.append(str(control_exception))
                    ids = sorted({row.get("case_id") for row in expected_failures
                                  if isinstance(row, dict) and isinstance(row.get("case_id"), str)})
                    # The recomputed failure identifies affected cases in every
                    # plan version; do not infer scope from surrounding prose.
                    review_targets.update(ids)
                    expected_sources.append({"kind": "control_failure", "attempt": attempt["attempt"], "case_ids": ids})
                elif structure_exception is not None:
                    diagnostic = attempt["validation_error"]
                    if "execution_paths" in diagnostic:
                        expected_feedback.append(_validation_error_feedback(diagnostic))
                        ids = diagnostic["case_ids"]
                        source = {"kind": "validation_error", "attempt": attempt["attempt"], "case_ids": ids,
                                  "execution_paths": diagnostic["execution_paths"]}
                    else:
                        expected_feedback.append(str(structure_exception))
                        ids = diagnostic["case_ids"]
                        source = {"kind": "validation_error", "attempt": attempt["attempt"], "case_ids": ids}
                    review_targets.update(ids)
                    expected_sources.append(source)
                elif revision_failures:
                    expected_feedback.append("Plan revision self-check failed after full controls: " +
                                             ("see complete plan revision failures below" if recovered and recovered.get("subsequent_controls")
                                              else json.dumps(revision_failures, ensure_ascii=False)))
                if revision_failures:
                    feedback_failures = (_compact_historical_revision_failures(attempt, preserved_plan)
                                         if recovered and recovered.get("subsequent_controls") else revision_failures)
                    expected_feedback.append("Complete plan revision failures: " + json.dumps(feedback_failures, ensure_ascii=False))
                if snapshot.get("coverage_gaps"):
                    if attempt.get("status") == "passed":
                        raise RefinerError(f"{suite} unresolved coverage gaps cannot pass the plan self-check")
                    gaps = snapshot["coverage_gaps"]
                    expected_feedback.append("Unresolved declared coverage gaps: " + json.dumps(gaps, ensure_ascii=False))
                    expected_sources.append({"kind": "coverage_gaps", "attempt": attempt["attempt"], "gaps": gaps})
                provisional_sources.update({row["id"]: {"case_id": row["id"], "source_plan_sha256": digest(snapshot), "case": row}
                                            for row in snapshot.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)})
                provisional_case_ids.update(current_provisional)
                latest_draft = snapshot
                latest_draft_attempt = attempt["attempt"]
                attempts.append(attempt)
        latest_matching = [row for row in attempts
                           if row.get("plan_sha256") == digest(plan) and row.get("status") == "passed"
                           and not row.get("revision_failures", [])]
        if not latest_matching:
            raise RefinerError(f"{suite} plan has no passed full-control attempt evidence")
        final_requirements = {row["id"] for row in plan.get("requirements", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
        final_cases = {row["id"] for row in plan.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
        if not (preserved_requirements <= final_requirements and preserved_cases <= final_cases):
            raise RefinerError(f"{suite} final plan lost a preserved requirement or case")
        recomputed = validate_plan(plan, root, max_cases=30, verified_response_sources=sources,
                                   required_response_contract_cases=(load_response_applicability(
                                       root, source_inputs, plan, sources)["required_case_ids"] if sources is not None else None))
        if digest(summary.get("case_control_results")) != digest(recomputed.get("case_control_results")):
            raise RefinerError(f"{suite} current control summary differs from the full recomputed controls")


def _verify_plan_review_artifacts(root, plans):
    from .pipeline import validate_plan_review, recovery_plan_revision, _response_source_contents
    from .requirement_revision import unapproved_recovery, requirement_revision

    resume = read_json(root / "resume-contract.json") if (root / "resume-contract.json").is_file() else {}
    recovery = None
    if (root / "refine-plan-recovery.json").exists() and not resume.get("refine_plan_recovery_sha256"):
        raise RefinerError("Recovered-plan provenance is not bound to the resume contract")
    if resume.get("refine_plan_recovery_sha256"):
        from .refine_plan_recovery import verify_recovery
        recovery = verify_recovery(root, resume)
    for suite, plan in plans.items():
        summary = read_json(_required_file(root, f"plan-reviews/{suite}.json"))
        if (summary.get("schema_version") != "skill-refiner.plan-review/v1"
                or summary.get("suite") != suite or summary.get("status") != "passed"
                or summary.get("plan_sha256") != digest(plan)
                or summary.get("plan_snapshot") != plan
                or not isinstance(summary.get("review"), dict)
                or summary["review"].get("approved") is not True):
            raise RefinerError(f"{suite} independent review does not bind to the frozen plan")
        reference = ({**recovery, "previous_plan": recovery["control_records"][0]["plan_snapshot"]}
                     if recovery and recovery.get("control_records") else recovery)
        revision = recovery_plan_revision(reference, plan) if reference and suite == "development" else None
        if summary.get("recovery_revision") != revision:
            raise RefinerError(f"{suite} independent review does not bind every recovery revision")
        control = read_json(_required_file(root, f"plan-controls/attempts/{suite}-{summary['attempt']:02}.json"))
        execution_revision = control.get("execution_plan_revision") or None
        if summary.get("execution_plan_revision") != execution_revision:
            raise RefinerError(f"{suite} independent review does not bind the step revision")
        requirements_revision = (requirement_revision(reference["previous_plan"], plan)
                                 if reference and suite == "development" and unapproved_recovery(recovery)
                                 and reference["previous_plan"].get("requirements") != plan.get("requirements") else None)
        if summary.get("requirement_revision") != requirements_revision or control.get("requirement_revision") != requirements_revision:
            raise RefinerError(f"{suite} independent review does not bind the complete requirement revision")
        frozen_inputs = read_json(root / "inputs.json") if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else None
        validate_plan_review(summary["review"], plan, revision, execution_revision, control.get("provisional_context"),
                             _response_source_contents(frozen_inputs) if frozen_inputs is not None else None,
                             requirements_revision, frozen_inputs)

        attempts_dir = root / "plan-reviews" / "attempts"
        attempts = []
        if attempts_dir.is_dir():
            for path in sorted(attempts_dir.glob(f"{suite}-*.json")):
                row = read_json(path)
                if (row.get("schema_version") != "skill-refiner.plan-review/v1"
                        or row.get("suite") != suite
                        or not isinstance(row.get("attempt"), int)
                        or row.get("plan_sha256") != digest(row.get("plan_snapshot"))
                        or not isinstance(row.get("review"), dict)):
                    raise RefinerError(f"Malformed {suite} independent-review attempt evidence: {path.name}")
                revision = recovery_plan_revision(reference, row["plan_snapshot"]) if reference and suite == "development" else None
                if row.get("recovery_revision") != revision:
                    raise RefinerError(f"{suite} review attempt does not bind the recovered plan revision")
                control = read_json(_required_file(root, f"plan-controls/attempts/{suite}-{row['attempt']:02}.json"))
                execution_revision = control.get("execution_plan_revision") or None
                if row.get("execution_plan_revision") != execution_revision:
                    raise RefinerError(f"{suite} review attempt does not bind the step revision")
                requirements_revision = (requirement_revision(reference["previous_plan"], row["plan_snapshot"])
                                         if reference and suite == "development" and unapproved_recovery(recovery)
                                         and reference["previous_plan"].get("requirements") != row["plan_snapshot"].get("requirements") else None)
                if row.get("requirement_revision") != requirements_revision or control.get("requirement_revision") != requirements_revision:
                    raise RefinerError(f"{suite} review attempt does not bind its requirement revision")
                frozen_inputs = read_json(root / "inputs.json") if row["plan_snapshot"].get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else None
                validate_plan_review(row["review"], row["plan_snapshot"], revision, execution_revision, control.get("provisional_context"),
                                     _response_source_contents(frozen_inputs) if frozen_inputs is not None else None,
                                     requirements_revision, frozen_inputs)
                expected_status = "passed" if row["review"].get("approved") else "rejected"
                if row.get("status") != expected_status:
                    raise RefinerError(f"{suite} independent-review attempt status differs from its response: {path.name}")
                attempts.append(row)
        if not any(row.get("plan_sha256") == digest(plan)
                   and row.get("status") == "passed" for row in attempts):
            raise RefinerError(f"{suite} plan has no persisted approved independent review")


def _required_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise RefinerError(f"Completion evidence missing: {path}")
    return path


def _phase(result, *, phase: str, required_cases: set[str], must_pass: bool,
           suite_sha256: str | None = None) -> list[str]:
    if not isinstance(result, dict) or not isinstance(result.get("cases"), list):
        raise RefinerError(f"{phase} evidence has no executed case records")
    if must_pass and result.get("passed") is not True:
        raise RefinerError(f"{phase} did not pass")

    engines = result.get("engines")
    if engines is not None:
        if not isinstance(engines, list) or not engines:
            raise RefinerError(f"{phase} matrix evidence has no executors")
        executor_ids = []
        for engine in engines:
            if not isinstance(engine, dict) or not isinstance(engine.get("executor"), str):
                raise RefinerError(f"{phase} has malformed executor evidence")
            executor_ids.append(engine["executor"])
            if suite_sha256 is not None and engine.get("suite_sha256") != suite_sha256:
                raise RefinerError(f"{phase} executor {engine['executor']} used a different frozen suite")
            if must_pass and engine.get("passed") is not True:
                raise RefinerError(f"{phase} failed on executor {engine['executor']}")
            engine_cases = engine.get("cases")
            if not isinstance(engine_cases, list):
                raise RefinerError(f"{phase} has no cases for executor {engine['executor']}")
            rows = {}
            for row in engine_cases:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise RefinerError(f"{phase} has malformed case evidence on {engine['executor']}")
                if row["id"] in rows:
                    raise RefinerError(f"{phase} duplicates case {row['id']} on {engine['executor']}")
                rows[row["id"]] = row
            if not required_cases <= rows.keys():
                raise RefinerError(f"{phase} omitted cases on executor {engine['executor']}")
            for case_id, row in rows.items():
                if row.get("status") not in {"PASS", "FAIL", "ERROR"}:
                    raise RefinerError(f"{phase} contains unexecuted case {case_id} on {engine['executor']}")
                if phase == "baseline" and row.get("status") == "ERROR":
                    raise RefinerError(f"{phase} contains an execution error on {engine['executor']}")
                if must_pass and (row.get("status") != "PASS" or row.get("passed") is not True):
                    raise RefinerError(f"{phase} has a non-passing case {case_id} on {engine['executor']}")
        if len(set(executor_ids)) != len(executor_ids):
            raise RefinerError(f"{phase} contains duplicate executor records")
        return executor_ids

    rows = {row.get("id"): row for row in result["cases"] if isinstance(row, dict)}
    if not required_cases <= rows.keys():
        raise RefinerError(f"{phase} omitted required case records")
    for case_id, row in rows.items():
        if row.get("status") not in {"PASS", "FAIL", "ERROR"}:
            raise RefinerError(f"{phase} contains unexecuted case {case_id}")
        if phase == "baseline" and row.get("status") == "ERROR":
            raise RefinerError(f"{phase} contains an execution error")
        if must_pass and (row.get("status") != "PASS" or row.get("passed") is not True):
            raise RefinerError(f"{phase} has a non-passing case {case_id}")
    return []


def _verify_response_contract_results(plans, evidence, executor_ids):
    """Recheck every actual v7 response contract against frozen judge evidence."""
    from .checks import _case_response_contract
    for phase, suite in (("regression", "development"), ("acceptance", "acceptance")):
        plan = plans[suite]
        if plan.get("schema_version") not in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
            continue
        required = {case["id"]: _case_response_contract(case)["contract_sha256"]
                    for case in plan["cases"] if case.get("response_contract") is not None}
        result = evidence.get(phase) or {}
        engines = result.get("engines")
        if engines is None:
            if executor_ids:
                raise RefinerError(f"{phase} response contracts lack the executor matrix")
            engines = [{"executor": "default", "cases": result.get("cases", [])}]
        for engine in engines:
            rows = engine.get("cases", [])
            for case_id, contract_hash in required.items():
                matches = [row for row in rows if isinstance(row, dict) and row.get("id") == case_id]
                if len(matches) != 1 or matches[0].get("status") != "PASS" or matches[0].get("passed") is not True:
                    raise RefinerError(f"{phase} response contract case was not passed: {case_id}")
                upstream = matches[0].get("evidence") or {}
                if upstream.get("case_id") != case_id or str(upstream.get("status", "")).upper() != "PASS":
                    raise RefinerError(f"{phase} response contract upstream evidence is not bound: {case_id}")
                diagnostics = []
                for item in (upstream.get("grading") or {}).get("assertion_results", []):
                    try:
                        row = json.loads(item.get("evidence", ""))
                    except (TypeError, ValueError):
                        continue
                    if isinstance(row, dict) and row.get("case_id") == case_id:
                        diagnostics.append(row)
                if len(diagnostics) != 1 or diagnostics[0].get("passed") is not True:
                    raise RefinerError(f"{phase} response contract judge evidence is missing: {case_id}")
                found = [a for a in diagnostics[0].get("assertions", []) if isinstance(a, dict)
                         and a.get("assertion_id") == "response_contract"]
                if (len(found) != 1 or found[0].get("passed") is not True
                        or not isinstance(found[0].get("expected_condition"), dict)
                        or found[0]["expected_condition"].get("contract_sha256") != contract_hash):
                    raise RefinerError(f"{phase} response contract judge evidence is invalid: {case_id}")


def _verify_delivered_planned_dependencies(final, plans):
    """Require delivered v8 target files in addition to actual case assertions."""
    from .planned_dependencies import inspect_target_dependencies
    for suite in plans:
        if suite.get("schema_version") != "skill-refiner.plan/v8":
            continue
        for case in suite["cases"]:
            missing = [row for row in inspect_target_dependencies(case, final) if row["status"] != "ready"]
            if missing:
                raise RefinerError("Delivered target dependency is not ready for case " + case["id"])



def _verify_planned_dependency_results(final, plans, evidence, executor_ids, *, probe_budget=None, deadline=None):
    """Re-execute each frozen validator and compare case-bound execution evidence."""
    from .docker_target_probe import validate_case_dependencies, comparable_probe_records
    for phase, name in (("regression", "development"), ("acceptance", "acceptance")):
        plan = plans[name]
        if plan.get('schema_version') != 'skill-refiner.plan/v8':
            continue
        required = [case for case in plan['cases'] if case.get('planned_target_dependencies')]
        if not required: continue
        result = evidence.get(phase) or {}
        engines = result.get('engines')
        if engines is None:
            if executor_ids: raise RefinerError('Validator evidence lacks executor matrix')
            engines = [{'executor': 'default', 'cases': result.get('cases', [])}]
        for engine in engines:
            for case in required:
                rows = [row for row in engine.get('cases', []) if row.get('id') == case['id']]
                if len(rows) != 1 or rows[0].get('status') != 'PASS' or rows[0].get('passed') is not True:
                    raise RefinerError('Validator case did not pass: ' + case['id'])
                row = rows[0]
                response = (row.get('evidence') or {}).get('response')
                recorded = row.get('planned_dependency_probes')
                if not isinstance(response, str) or not isinstance(recorded, list):
                    raise RefinerError('Validator response or execution evidence is missing: ' + case['id'])
                try:
                    replayed = validate_case_dependencies(case, final, response, probe_budget=probe_budget,
                                                          phase="final", deadline=deadline)
                    same = comparable_probe_records(recorded) == comparable_probe_records(replayed)
                except Exception as exc:
                    raise RefinerError('Validator evidence cannot be independently replayed: ' + case['id']) from exc
                if not same or not all(item['probe']['passed'] is True for item in replayed):
                    raise RefinerError('Validator execution differs from accepted case evidence: ' + case['id'])

def _verify_pipeline(root: Path, *, expected_status: set[str], probe_budget=None, deadline=None) -> dict:
    state = read_json(_required_file(root, "state.json"))
    if state.get("status") not in expected_status:
        raise RefinerError(f"Run status is {state.get('status')!r}; a fully accepted status is required")
    try:
        verify_stage_manifest(root, mode="refine", run_status=state["status"],
                              allow_active_verification=True)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise RefinerError(f"Stage contract verification failed: {exc}") from exc
    _required_file(root, "report.md")
    plan = read_json(_required_file(root, "test-plan.json"))
    acceptance = read_json(_required_file(root, "acceptance-plan.json"))
    inputs = read_json(_required_file(root, "inputs.json"))
    lock = read_json(_required_file(root, "oracle.lock.json"))
    if lock.get("sha256") != digest({"plan": plan, "acceptance": acceptance, "inputs": inputs}):
        raise RefinerError("Frozen requirements or tests do not match their lock")
    if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        if inputs.get("_response_contract_v7") is not True:
            from .pipeline import response_contract_migration
            from .refine_plan_recovery import verify_recovery
            resume_contract = read_json(_required_file(root, "resume-contract.json"))
            recovery = verify_recovery(root, resume_contract)
            migration = response_contract_migration(root, inputs, recovery, resume_contract)
            if lock.get("response_contract_migration_sha256") != digest(migration):
                raise RefinerError("Frozen response contract migration hash differs")
        elif lock.get("response_contract_migration_sha256") is not None:
            raise RefinerError("Unexpected response contract migration for a new v7 input")
        validator = Path(__file__).with_name("response_contract.py")
        if lock.get("response_contract_sha256") != hashlib.sha256(validator.read_bytes()).hexdigest():
            raise RefinerError("Frozen response contract validator changed")
    if plan.get("schema_version") == "skill-refiner.plan/v8":
        validator = Path(__file__).with_name("planned_dependencies.py")
        if lock.get("planned_dependency_validator_sha256") != hashlib.sha256(validator.read_bytes()).hexdigest():
            raise RefinerError("Frozen planned-dependency validator changed")
        if inputs.get("_planned_target_dependencies_v8") is True:
            if lock.get("planned_dependency_migration_sha256") is not None:
                raise RefinerError("Unexpected planned-dependency migration for new v8 input")
        else:
            from .refine_plan_recovery import verify_recovery
            resume_contract = read_json(_required_file(root, "resume-contract.json"))
            recovered = verify_recovery(root, resume_contract)
            marker = recovered.get("planned_dependency_migration")
            if not marker or lock.get("planned_dependency_migration_sha256") != digest(marker):
                raise RefinerError("v8 planned dependency migration is missing or differs from lock")
    validate_obligation_plans(inputs, {"development": plan, "acceptance": acceptance})
    if not isinstance(plan.get("cases"), list) or not plan["cases"]:
        raise RefinerError("Development plan contains no executable cases")
    if not isinstance(acceptance.get("cases"), list) or not acceptance["cases"]:
        raise RefinerError("Acceptance plan contains no executable cases")
    if plan.get("schema_version") in {"skill-refiner.plan/v4", "skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        if (acceptance.get("schema_version") != plan.get("schema_version")
                or acceptance.get("stage_contracts") != plan.get("stage_contracts")):
            raise RefinerError("Independent acceptance changed the frozen Skill stage/schema contract")
    if (plan.get("schema_version") in {"skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}
            and acceptance.get("execution_plan") != plan.get("execution_plan")):
        raise RefinerError("Independent acceptance changed the frozen execution plan")
    if acceptance.get("coverage_gaps") or plan.get("coverage_gaps"):
        raise RefinerError("A completed run cannot contain unresolved coverage gaps")
    _verify_phase_history(state)
    _verify_plan_control_artifacts(root, {"development": plan, "acceptance": acceptance})
    _verify_plan_review_artifacts(root, {"development": plan, "acceptance": acceptance})

    source = Path(state.get("source", "")).resolve()
    original = root / "original" / source.name
    if not source.is_dir() or not original.is_dir():
        raise RefinerError("Original Skill or immutable snapshot is missing")
    source_manifest = manifest(source)
    if source_manifest != manifest(original) or source_manifest != lock.get("source_manifest"):
        raise RefinerError("Original Skill changed or does not match the frozen baseline")

    final = Path(state.get("final_skill", "")).resolve()
    if not (final / "SKILL.md").is_file() or not final.is_dir():
        raise RefinerError("Accepted candidate Skill is missing")
    if plan.get("schema_version") in {"skill-refiner.plan/v4", "skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        try:
            validate_skill_schema_package(final, plan["stage_contracts"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RefinerError(f"Delivered Skill schema/validator package failed verification: {exc}") from exc
    _verify_delivered_planned_dependencies(final, (plan, acceptance))
    evidence = read_json(_required_file(root, "final/evidence.json"))
    candidate_manifest = manifest(final)
    if evidence.get("candidate_manifest") != candidate_manifest:
        raise RefinerError("Candidate Skill no longer matches accepted evidence")
    if evidence.get("oracle_hash") != lock.get("sha256"):
        raise RefinerError("Accepted evidence refers to a different frozen test suite")
    acceptance_target = read_json(_required_file(root, "acceptance/target.json"))
    acceptance_candidate_ref = Path(acceptance_target.get("candidate", ""))
    acceptance_candidate = (root / acceptance_candidate_ref).resolve()
    if (acceptance_target.get("candidate_manifest") != candidate_manifest
            or acceptance_target.get("candidate_sha256") != digest(candidate_manifest)
            or acceptance_target.get("suite_sha256") != digest(acceptance["cases"])
            or acceptance_candidate_ref.is_absolute()
            or not acceptance_candidate.is_relative_to(root)
            or not acceptance_candidate.is_dir()
            or manifest(acceptance_candidate) != candidate_manifest):
        raise RefinerError("Independent acceptance was not run against the delivered candidate and frozen suite")

    baseline_cases = {case["id"] for case in plan["cases"] if isinstance(case, dict) and isinstance(case.get("id"), str)}
    regression_cases = baseline_cases
    acceptance_cases = {case["id"] for case in acceptance["cases"] if isinstance(case, dict) and isinstance(case.get("id"), str)}
    if not baseline_cases or not acceptance_cases:
        raise RefinerError("Frozen plans contain no valid case identifiers")
    baseline_executors = _phase(evidence.get("baseline"), phase="baseline", required_cases=baseline_cases,
                                must_pass=False, suite_sha256=digest(plan["cases"]))
    executors = _phase(evidence.get("regression"), phase="regression", required_cases=regression_cases,
                       must_pass=True, suite_sha256=digest(plan["cases"]))
    acceptance_executors = _phase(evidence.get("acceptance"), phase="acceptance", required_cases=acceptance_cases,
                                  must_pass=True, suite_sha256=digest(acceptance["cases"]))
    if executors != acceptance_executors or baseline_executors != executors:
        raise RefinerError("Baseline, regression and independent acceptance used different executors")
    validate_obligation_results(inputs, {"development": plan, "acceptance": acceptance},
                                evidence, executor_ids=executors)
    _verify_response_contract_results({"development": plan, "acceptance": acceptance}, evidence, executors)
    needs_probes = any(case.get('planned_target_dependencies') for suite in (plan,acceptance)
                       if suite.get('schema_version') == 'skill-refiner.plan/v8' for case in suite['cases'])
    if needs_probes:
        from .probe_budget import ProbeBudget
        config = ProbeBudget.configuration((plan,acceptance), len(executors) or 1, state['limits']['max_rounds'])
        if probe_budget is None:
            if not (root / 'validator-probe-budget' / 'config.json').is_file():
                raise RefinerError('Persistent validator probe budget is missing')
            probe_budget = ProbeBudget(root, config, deadline)
        elif probe_budget.config != config:
            raise RefinerError('Validator budget does not match frozen plan')
        with probe_budget.final_session(state['limits']['max_seconds'], state.get('elapsed_seconds', 0)) as final_deadline:
            _verify_planned_dependency_results(final, {"development": plan, "acceptance": acceptance}, evidence,
                                               executors, probe_budget=probe_budget, deadline=final_deadline)
    _required_file(root, "final/changes.patch")

    backend_config = root / "backend-config.json"
    if backend_config.is_file():
        configured = read_json(backend_config).get("executors", [])
        expected_executors = [entry.get("id") for entry in configured if isinstance(entry, dict)]
        if expected_executors and executors != expected_executors:
            raise RefinerError("Not every configured executor passed regression and acceptance")
    else:
        backends = state.get("backends")
        configured = backends.get("executors", []) if isinstance(backends, dict) else []
        expected_executors = [entry.get("id") for entry in configured if isinstance(entry, dict)]
        if expected_executors and executors != expected_executors:
            raise RefinerError("Not every configured executor passed regression and acceptance")

    expected_final = (root / "final" / source.name).resolve()
    if final != expected_final:
        raise RefinerError("Accepted candidate is outside this run's final directory")

    return {"status": "verified", "run_status": state["status"], "run_dir": str(root),
            "final_skill": str(final), "executors": executors,
            "target_obligation_tracking": ("verified_from_frozen_judge_evidence"
                if inputs.get("business_analysis", {}).get("schema_version") == "skill-refiner.analysis/v4"
                else "not_part_of_legacy_contract"),
            "development_cases": sorted(baseline_cases), "acceptance_cases": sorted(acceptance_cases),
            "evidence": str((root / "final/evidence.json").resolve())}


def verify_run(path, *, probe_budget=None, deadline=None) -> dict:
    """Verify `run` or `create` evidence; reject partial, edited or failed runs."""
    root = Path(path).resolve()
    state = read_json(_required_file(root, "state.json"))
    if state.get("mode") == "create":
        if state.get("status") != "created" or state.get("refinement_status") not in {"passed", "unchanged"}:
            raise RefinerError("Creation did not complete every refinement gate")
        try:
            verify_stage_manifest(root, mode="create", run_status=state["status"],
                                  allow_active_verification=True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise RefinerError(f"Creation stage contract verification failed: {exc}") from exc
        refinement_root = root / "refinement"
        verified = _verify_pipeline(refinement_root, expected_status={"passed", "unchanged"}, probe_budget=probe_budget, deadline=deadline)
        # Creation analysis and its independent source review remain authoritative;
        # a child cannot silently omit them and pass only a smaller frozen suite.
        analysis = read_json(root / "analysis.json") if (root / "analysis.json").is_file() else {}
        if analysis.get("schema_version") == "skill-refiner.analysis/v4":
            from .creation import validate_analysis, validate_analysis_review
            creation_inputs = read_json(_required_file(root, "inputs.json"))
            contract = read_json(_required_file(root, "creation-resume-contract.json"))
            if contract.get("clarification_recovery_sha256"):
                from .analysis_recovery import verify_recovery
                from .clarification_recovery import verify_clarification_recovery
                verify_clarification_recovery(root, contract, creation_inputs,
                                             verify_recovery(root, contract, creation_inputs))
            business_lock = read_json(_required_file(root, "business.lock.json"))
            if (business_lock.get("input_sha256") != digest(creation_inputs)
                    or business_lock.get("sha256") != digest({"inputs": creation_inputs, "analysis": analysis})):
                raise RefinerError("Creation business/standard obligation contract no longer matches its lock")
            validate_analysis(analysis, creation_inputs.get("requested_name"),
                              {f"spec-{i+1}" for i, _ in enumerate(creation_inputs.get("requirements", []))}
                              | ({"business_description"} if creation_inputs.get("business_description") else set()),
                              standards=creation_inputs.get("standards"))
            review = read_json(_required_file(root, "analysis-review.json"))
            validate_analysis_review(review, analysis)
            if not review["approved"]:
                raise RefinerError("Target standard obligation analysis was not independently approved")
            child_inputs = read_json(_required_file(refinement_root, "inputs.json"))
            if child_inputs.get("business_analysis") != analysis:
                raise RefinerError("Child tests did not use the approved target standard obligation analysis")
            if child_inputs.get("creation_standard_sources") != creation_inputs.get("standards"):
                raise RefinerError("Child tests did not preserve the approved creation standard authority")
        create_final = Path(state.get("final_skill", "")).resolve()
        expected_create_final = (root / "final" / Path(verified["final_skill"]).name).resolve()
        if create_final != expected_create_final:
            raise RefinerError("Created Skill is outside this creation run's final directory")
        if not create_final.is_dir() or manifest(create_final) != manifest(Path(verified["final_skill"])):
            raise RefinerError("Created Skill differs from the fully accepted refinement candidate")
        create_evidence_path = _required_file(root, "final/evidence.json")
        create_evidence = read_json(create_evidence_path)
        refinement_evidence = _required_file(refinement_root, "final/evidence.json")
        if create_evidence.get("refinement_evidence_sha256") != digest(read_json(refinement_evidence)):
            raise RefinerError("Creation evidence does not bind the accepted refinement record")
        _required_file(root, "final/creation.patch")
        _required_file(root, "final/changes.patch")
        _required_file(root, "report.md")
        verified.update(run_status="created", run_dir=str(root), final_skill=str(create_final),
                        evidence=str(create_evidence_path.resolve()))
        return verified
    return _verify_pipeline(root, expected_status={"passed", "unchanged"}, probe_budget=probe_budget, deadline=deadline)

"""Target standards have their own authority; they are never invented spec requirements.

These checks validate traceability, not the truth of a model's claims. Runtime proof
still comes from the locked cases on every configured executor and the final gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema

from .common import RefinerError, read_json

VERSION = "skill-refiner.analysis/v4"
SCHEMA = read_json(Path(__file__).resolve().parents[2] / "schemas/v1/target-obligations.schema.json")


def standard_sources(standards):
    """Build content identities from the *frozen* inputs, not live standard files."""
    rows = []
    for index, value in enumerate(standards or []):
        content = value if isinstance(value, str) else value.get("content") if isinstance(value, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RefinerError("Frozen standard source must contain nonempty text")
        rows.append({"id": f"standard-{index + 1}", "content": content,
                     "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    return rows


def standards_for_context(context):
    """A creation child keeps its parent's source IDs despite path rediscovery.

    Source metadata paths can differ and discovery can encounter the same text
    twice. Extra or missing *content* is not allowed to change the reviewed scope.
    """
    actual = context.get("standards")
    frozen = context.get("creation_standard_sources")
    if frozen is None:
        return actual
    if not isinstance(frozen, list) or not frozen:
        raise RefinerError("Creation standard authority must be a nonempty frozen source list")
    expected_hashes = {r["content_sha256"] for r in standard_sources(frozen)}
    actual_hashes = {r["content_sha256"] for r in standard_sources(actual)}
    if actual_hashes != expected_hashes:
        raise RefinerError("Child standard contents differ from the reviewed creation standard authority")
    return frozen


def effective_requirements(analysis):
    """Project applicable target obligations into the existing plan contract."""
    result = copy.deepcopy(analysis["requirements"])
    if analysis.get("schema_version") != VERSION:
        return result
    for row in analysis["target_obligations"]["obligations"]:
        if row["subject"] != "target" or not row["applicable"]:
            continue
        source = row["source"]
        result.append({"id": row["id"], "behavior": row["behavior"], "required": True,
                       "source": f'{source["source_id"]}: sha256={source["content_sha256"]}; {source["quote"]}',
                       "acceptance_criteria": copy.deepcopy(row["acceptance_criteria"])})
    return result


def revision_requirements(analysis):
    """Include excluded/controller obligations in semantic revision accounting too."""
    result = {row["id"]: row for row in copy.deepcopy(analysis["requirements"])}
    if analysis.get("schema_version") == VERSION:
        for row in analysis["target_obligations"]["obligations"]:
            result[row["id"]] = {"id": row["id"], "required": row["subject"] == "target" and row["applicable"],
                                 "standard_obligation": copy.deepcopy(row),
                                 "coverage_gaps": [copy.deepcopy(gap)
                                     for gap in analysis["target_obligations"]["coverage_gaps"]
                                     if row["id"] in gap["obligation_ids"]]}
    return result


def _normalize_source_quote_whitespace(value):
    """Treat line wrapping as layout while preserving all source words and order."""
    return " ".join(value.split())


def validate_obligations(analysis, standards):
    if analysis.get("schema_version") != VERSION:
        if "target_obligations" in analysis:
            raise RefinerError("Legacy analysis cannot silently opt into target obligations")
        return
    if standards is None:
        raise RefinerError("Analysis/v4 requires its frozen standard source texts")
    report = analysis["target_obligations"]
    jsonschema.validate(report, SCHEMA)
    sources = {row["id"]: row for row in standard_sources(standards)}
    declared = report["sources"]
    if (len(declared) != len(sources) or len({row["source_id"] for row in declared}) != len(declared)
            or {row["source_id"] for row in declared} != sources.keys()):
        raise RefinerError("Obligation inventory must account for every frozen standard source exactly once")
    for row in declared:
        if row["content_sha256"] != sources[row["source_id"]]["content_sha256"]:
            raise RefinerError("Obligation source content hash does not match frozen standards")
    obligations = report["obligations"]
    ids = [row["id"] for row in obligations]
    business_ids = {row["id"] for row in analysis["requirements"]}
    if len(ids) != len(set(ids)) or set(ids) & business_ids:
        raise RefinerError("Obligation IDs must be unique and distinct from business requirements")
    for source in declared:
        expected = {row["id"] for row in obligations if row["source"]["source_id"] == source["source_id"]}
        if not expected or set(source["obligation_ids"]) != expected:
            raise RefinerError("Every standard source must explicitly map all of its assessed obligations")
    criteria = [c["id"] for row in obligations for c in row["acceptance_criteria"]]
    business_criteria = {c["id"] for row in analysis["requirements"] for c in row["acceptance_criteria"]}
    if len(criteria) != len(set(criteria)) or set(criteria) & business_criteria:
        raise RefinerError("Obligation acceptance criterion IDs must be globally unique")
    gaps = {row["id"]: row for row in report["coverage_gaps"]}
    if len(gaps) != len(report["coverage_gaps"]):
        raise RefinerError("Obligation coverage gap IDs must be unique")
    for row in obligations:
        source = row["source"]
        authority = sources.get(source["source_id"])
        if (authority is None or source["content_sha256"] != authority["content_sha256"]
                or not source["quote"].strip()
                or _normalize_source_quote_whitespace(source["quote"])
                not in _normalize_source_quote_whitespace(authority["content"])):
            raise RefinerError(f'Obligation {row["id"]} is not grounded in its frozen standard text/hash')
        target = row["subject"] == "target" and row["applicable"]
        linked_scenarios = {s["id"] for s in analysis["scenarios"] if row["id"] in s["requirement_ids"]}
        linked_workflow = {w["id"] for w in analysis["workflow"] if row["id"] in w["requirement_ids"]}
        if set(row["scenario_ids"]) != linked_scenarios or set(row["workflow_ids"]) != linked_workflow:
            raise RefinerError(f'Obligation {row["id"]} has inconsistent scenario/workflow links')
        if row["subject"] == "controller":
            if linked_scenarios or linked_workflow or row["mechanism"]["gap_ids"]:
                raise RefinerError("Controller-only or excluded obligations cannot become target behavior/tests")
            if row["applicable"] and row["mechanism"]["state"] == "not_applicable":
                raise RefinerError("Applicable controller obligation cannot be marked not_applicable")
            if not row["applicable"] and row["mechanism"]["state"] != "not_applicable":
                raise RefinerError("Excluded controller obligation needs an explicit not_applicable mechanism")
            continue
        if not target:
            if linked_scenarios or linked_workflow or row["mechanism"]["gap_ids"]:
                raise RefinerError("Controller-only or excluded obligations cannot become target behavior/tests")
            if row["mechanism"]["state"] != "not_applicable":
                raise RefinerError("Excluded target obligation needs an explicit not_applicable mechanism")
            continue
        if not row["acceptance_criteria"] or not linked_scenarios or not linked_workflow:
            raise RefinerError(f'Applicable target obligation {row["id"]} lacks atomic acceptance/workflow/scenarios')
        applicable = [s for s in analysis["scenarios"] if s["id"] in linked_scenarios and s["applicable"]]
        covered = {cid for s in applicable for cid in s["acceptance_criteria_ids"]}
        if {c["id"] for c in row["acceptance_criteria"]} - covered:
            raise RefinerError(f'Target obligation {row["id"]} criteria lack applicable scenarios')
        if row["mechanism"]["state"] == "not_applicable":
            raise RefinerError("Applicable target obligation cannot have a not_applicable mechanism")
        gap_ids = row["mechanism"]["gap_ids"]
        if row["mechanism"]["state"] == "blocked" and not gap_ids:
            raise RefinerError("Blocked target mechanism requires a concrete coverage gap")
        for gap_id in gap_ids:
            if gap_id not in gaps or row["id"] not in gaps[gap_id]["obligation_ids"]:
                raise RefinerError("Target mechanism references an unknown or unrelated coverage gap")
    for gap in gaps.values():
        if not gap["obligation_ids"] or not set(gap["obligation_ids"]) <= set(ids):
            raise RefinerError("Coverage gap must reference known obligations")
        for oid in gap["obligation_ids"]:
            row = next(row for row in obligations if row["id"] == oid)
            if (row["subject"] != "target" or not row["applicable"]
                    or gap["id"] not in row["mechanism"]["gap_ids"]
                    or not set(gap["scenario_ids"]) <= set(row["scenario_ids"])):
                raise RefinerError("Coverage gap cannot be hidden on an excluded/controller obligation")


def validate_obligation_review(review, analysis):
    """Independent source completeness and subject/coverage assessments are mandatory."""
    if analysis.get("schema_version") != VERSION:
        return
    inventory = analysis["target_obligations"]
    for field, key, expected, flags in (
        ("standard_source_assessments", "source_id", {r["source_id"]: r["content_sha256"] for r in inventory["sources"]},
         ("all_obligations_captured", "subject_boundary_correct")),
        ("obligation_assessments", "obligation_id", {r["id"]: r["source"]["content_sha256"] for r in inventory["obligations"]},
         ("source_verified", "subject_applicability_justified", "criteria_atomic", "mechanism_and_gaps_explicit",
          "scenario_coverage_complete")),
    ):
        rows = review.get(field, [])
        if len(rows) != len(expected) or {r.get(key) for r in rows} != expected.keys():
            raise RefinerError("Independent analysis review must assess every standard source and obligation exactly once")
        for row in rows:
            if row["content_sha256"] != expected[row[key]]:
                raise RefinerError("Independent standard review is bound to a different source hash")
            if review["approved"] and not all(row[name] for name in flags):
                raise RefinerError("Analysis approval contradicts standard obligation/source assessments")


def validate_obligation_plans(context, plans, *, require_closed=True):
    """Run before any target evaluation and again at final verification."""
    analysis = context.get("business_analysis") or {}
    if analysis.get("schema_version") != VERSION:
        if context.get("target_obligations") is not None:
            raise RefinerError("Obligations require their versioned analysis, not an unbound side channel")
        return
    validate_obligations(analysis, standards_for_context(context))
    expected = effective_requirements(analysis)
    if context.get("contract") != expected:
        raise RefinerError("Effective target contract differs from frozen business/standard obligations")
    if require_closed and analysis["target_obligations"]["coverage_gaps"]:
        raise RefinerError("Target standard obligations contain unresolved technical coverage gaps")
    obligations = [r for r in analysis["target_obligations"]["obligations"]
                   if r["subject"] == "target" and r["applicable"]]
    for suite, plan in plans.items():
        if plan.get("schema_version") not in {"skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
            raise RefinerError("Analysis/v4 target obligations require the frozen plan/v6 or v7 contract")
        if plan.get("requirements") != expected:
            raise RefinerError(f"{suite} plan changed or omitted the frozen standard obligation contract")
        stages = {rid for stage in plan.get("stage_contracts", {}).get("stages", [])
                  for rid in stage["requirement_ids"]}
        steps = {rid for step in plan.get("execution_plan", {}).get("steps", [])
                 for rid in step["requirement_ids"]}
        for row in obligations:
            if row["id"] not in stages or row["id"] not in steps:
                raise RefinerError(f"{suite} omits obligation {row['id']} from its stage/work plan")
            for criterion in row["acceptance_criteria"]:
                for sid in row["scenario_ids"]:
                    scenario = next(s for s in analysis["scenarios"] if s["id"] == sid)
                    if not scenario["applicable"] or criterion["id"] not in scenario["acceptance_criteria_ids"]:
                        continue
                    cases = [c for c in plan["cases"] if c.get("scenario_id") == sid
                             and row["id"] in c["requirement_ids"]
                             and criterion["id"] in c["acceptance_criteria_ids"]]
                    if not cases:
                        raise RefinerError(f"{suite} omits obligation {row['id']}/{criterion['id']}/{sid}")


def validate_obligation_results(context, plans, evidence, *, executor_ids):
    """Require upstream judge diagnostics bound to each frozen O case/check.

    A PASS summary or a standards-review statement alone is insufficient. This
    validates collected execution evidence, not OS-wide absence of side effects.
    """
    analysis = context.get("business_analysis") or {}
    if analysis.get("schema_version") != VERSION:
        return
    validate_obligation_plans(context, plans)
    target_ids = {r["id"] for r in analysis["target_obligations"]["obligations"]
                  if r["subject"] == "target" and r["applicable"]}
    for phase, suite in (("regression", "development"), ("acceptance", "acceptance")):
        cases = {c["id"]: c for c in plans[suite]["cases"] if target_ids.intersection(c["requirement_ids"])}
        result = evidence.get(phase) or {}
        engines = result.get("engines")
        if engines is None:
            if executor_ids:
                raise RefinerError("Obligation results omitted the configured executor matrix")
            engines = [{"executor": "default", "cases": result.get("cases", [])}]
        elif (len(engines) != len(executor_ids)
              or {r.get("executor") for r in engines} != set(executor_ids)):
            raise RefinerError("Obligation results omitted or duplicated a configured executor")
        for engine in engines:
            rows = engine.get("cases", [])
            for case_id, case in cases.items():
                matches = [row for row in rows if row.get("id") == case_id]
                if len(matches) != 1 or matches[0].get("status") != "PASS" or matches[0].get("passed") is not True:
                    raise RefinerError(f"{phase} has no executed passing obligation case {case_id}")
                upstream = matches[0].get("evidence") or {}
                if upstream.get("case_id") != case_id or str(upstream.get("status", "")).upper() != "PASS":
                    raise RefinerError(f"{phase} obligation case is not bound to its upstream result: {case_id}")
                diagnostics = []
                for assertion in (upstream.get("grading") or {}).get("assertion_results", []):
                    try:
                        value = json.loads(assertion.get("evidence", ""))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict) and value.get("case_id") == case_id:
                        diagnostics.append(value)
                if len(diagnostics) != 1:
                    raise RefinerError(f"{phase} obligation case lacks unique controller judge diagnostics: {case_id}")
                diagnostic = diagnostics[0]
                assertions = diagnostic.get("assertions", [])
                expected = {check["id"]: check for check in case["checks"]}
                if plans[suite].get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} and case.get("response_contract") is not None:
                    from .checks import _case_response_contract
                    expected["response_contract"] = {"id": "response_contract", "type": "json_schema"}
                    frozen_hash = _case_response_contract(case)["contract_sha256"]
                    matching = [a for a in assertions if a.get("assertion_id") == "response_contract"]
                    if (len(matching) != 1 or not isinstance(matching[0].get("expected_condition"), dict)
                            or matching[0]["expected_condition"].get("contract_sha256") != frozen_hash):
                        raise RefinerError(f"{phase} obligation response contract evidence differs from the frozen case: {case_id}")
                if (diagnostic.get("passed") is not True or len(assertions) != len(expected)
                        or {a.get("assertion_id") for a in assertions} != expected.keys()
                        or any(a.get("check") != expected.get(a.get("assertion_id"))
                               or a.get("passed") is not True
                               or not {"field_path", "expected_condition", "actual_value", "evidence"} <= a.keys()
                               for a in assertions)):
                    raise RefinerError(f"{phase} obligation judge evidence differs from frozen checks: {case_id}")

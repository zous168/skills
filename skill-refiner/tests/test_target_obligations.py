"""Offline controls for standard authority and the O→criterion→case→judge chain.

Synthetic judge records exercise validation only; they are not target Skill proof.
"""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_analysis_contract_v3 as analysis_fixture
import test_plan_contract as fixture
from refiner.artifact_validation import content_schema_id, schema_ref, validate_value
from refiner.checks import grade, validate_plan
from refiner.common import RefinerError, digest, read_json, write_json
from refiner.cli import main
from refiner.creation import (ANALYSIS_V3, ANALYSIS_V4, ANALYSIS_REVIEW_V4,
                              analysis_revision_delta, validate_analysis, validate_analysis_review,
                              validate_analysis_revision_review)
from refiner.obligations import (effective_requirements, standard_sources, validate_obligations,
                                 validate_obligation_plans, validate_obligation_results)
from refiner.runtime import codex_output_schema, decode_codex_response


class TargetObligationTests(unittest.TestCase):
    def setUp(self):
        self.standards = ["The target must expose the requested result token. Controller logs stay outside the target."]
        self.analysis = analysis_fixture.AnalysisV3Tests("runTest").analysis()
        self.analysis["schema_version"] = "skill-refiner.analysis/v4"
        # Two distinct applicable situations, with all risk links preserved.
        second = self.analysis["scenarios"][1]
        second["kind"] = "boundary"
        second["detail"]["risk_dimensions"] = ["workflow_state"]
        for risk in self.analysis["risk_assessments"]:
            risk["scenario_ids"] = [s["id"] for s in self.analysis["scenarios"] if s["applicable"]
                                    and risk["dimension"] in s["detail"]["risk_dimensions"]]
            risk["applicable"] = bool(risk["scenario_ids"])
        sha = standard_sources(self.standards)[0]["content_sha256"]
        sid = self.analysis["scenarios"][0]["id"]
        self.obligation = {
            "id": "O-RESULT", "subject": "target", "applicable": True,
            "applicability_reason": "The source expressly specifies target output behavior.",
            "behavior": "Expose the required result token in the target response.",
            "source": {"source_id": "standard-1", "content_sha256": sha,
                       "quote": "The target must expose the requested result token."},
            "acceptance_criteria": [{"id": "O-RESULT.A1", "statement": "Return the required result token.",
                                     "evidence": "The independently collected response contains the required token."}],
            "scenario_ids": [sid], "workflow_ids": ["W1"],
            "mechanism": {"state": "planned", "interface": "Read the response from a fresh target invocation.",
                          "evidence_strategy": "Frozen response assertion and complete positive/negative controls.",
                          "gap_ids": []},
        }
        self.analysis["target_obligations"] = {
            "schema_version": "skill-refiner.target-obligations/v1",
            "sources": [{"source_id": "standard-1", "content_sha256": sha, "obligation_ids": ["O-RESULT"]}],
            "obligations": [self.obligation], "coverage_gaps": []}
        self.analysis["workflow"][0]["requirement_ids"].append("O-RESULT")
        self.analysis["scenarios"][0]["requirement_ids"].append("O-RESULT")
        self.analysis["scenarios"][0]["acceptance_criteria_ids"].append("O-RESULT.A1")

    def context(self):
        return {"business_analysis": self.analysis, "standards": self.standards,
                "contract": effective_requirements(self.analysis)}

    def review(self):
        review = analysis_fixture.AnalysisV3Tests.review(self.analysis)
        review.update(schema_version="skill-refiner.analysis-review/v4", revision_assessments=[])
        review["requirement_assessments"].append({
            "requirement_id": "O-RESULT", "grounded": True, "acceptance_criteria_atomic": True,
            "criteria_observable": True, "basis": "The actual standard separately specifies this target outcome."})
        sha = self.obligation["source"]["content_sha256"]
        review["standard_source_assessments"] = [{
            "source_id": "standard-1", "content_sha256": sha, "all_obligations_captured": True,
            "subject_boundary_correct": True, "basis": "Target outcome and controller policy are distinguished."}]
        review["obligation_assessments"] = [{
            "obligation_id": "O-RESULT", "content_sha256": sha, "source_verified": True,
            "subject_applicability_justified": True, "criteria_atomic": True,
            "mechanism_and_gaps_explicit": True, "scenario_coverage_complete": True,
            "basis": "A concrete target interface and independent case controls are planned."}]
        return review

    def plans(self):
        plan = fixture._minimal_plan()
        plan["schema_version"] = "skill-refiner.plan/v6"
        plan["requirements"] = effective_requirements(self.analysis)
        scenarios = [s for s in self.analysis["scenarios"] if s["applicable"]]
        for case, scenario in zip(plan["cases"], scenarios):
            case.update(kind=scenario["kind"], scenario_id=scenario["id"], scenario=scenario["situation"],
                        scenario_detail=copy.deepcopy(scenario["detail"]),
                        requirement_ids=copy.deepcopy(scenario["requirement_ids"]),
                        acceptance_criteria_ids=copy.deepcopy(scenario["acceptance_criteria_ids"]))
            case["asset_audit"]["source_refs"] = [
                row["source"] for row in plan["requirements"] if row["id"] in case["requirement_ids"]]
        for step in plan["execution_plan"]["steps"]:
            step["requirement_ids"].append("O-RESULT")
        plan["stage_contracts"]["stages"][0]["requirement_ids"].append("O-RESULT")
        return {"development": plan, "acceptance": copy.deepcopy(plan)}

    def execution(self, plans):
        # Use the actual local grader, never convert a model's stated PASS to evidence.
        def rows(plan):
            result = []
            for case in plan["cases"]:
                diagnostic = grade(case, case["good_response"], ROOT)
                self.assertTrue(diagnostic["passed"])
                result.append({"id": case["id"], "status": "PASS", "passed": True,
                               "evidence": {"case_id": case["id"], "status": "PASS",
                                   "grading": {"assertion_results": [{"evidence": json.dumps(diagnostic)}]}}})
            return result
        return {phase: {"passed": True, "cases": [],
                        "engines": [{"executor": executor, "passed": True, "cases": rows(plans[suite])}
                                    for executor in ("first", "second")]}
                for phase, suite in (("regression", "development"), ("acceptance", "acceptance"))}

    def test_v4_schema_wire_roundtrip_and_legacy_schemas_remain_distinct(self):
        validate_analysis(self.analysis, "demo-skill", {"spec-1"}, standards=self.standards)
        validate_value(self.analysis, schema_ref("skill-refiner.analysis-v4"))
        validate_value(self.analysis["target_obligations"], schema_ref("skill-refiner.target-obligations"))
        jsonschema.validate(self.analysis, codex_output_schema(ANALYSIS_V4))
        self.assertEqual(decode_codex_response(self.analysis, ANALYSIS_V4), self.analysis)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.analysis, ANALYSIS_V3)
        with self.assertRaisesRegex(RefinerError, "frozen standard source"):
            validate_analysis(self.analysis)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            write_json(path, self.analysis)
            self.assertEqual(content_schema_id(path), "skill-refiner.analysis-v4")
            path = Path(directory) / "analysis-review.json"
            write_json(path, self.review())
            self.assertEqual(content_schema_id(path), "skill-refiner.analysis-review-v4")

    def test_source_text_hash_quote_and_inventory_are_bound_to_real_frozen_source(self):
        for variant in ("text", "hash", "source_hash", "quote", "missing_source", "missing_obligation_map"):
            analysis = copy.deepcopy(self.analysis)
            standards = self.standards[:]
            if variant == "text":
                standards[0] += " A changed standard."
            elif variant == "hash":
                analysis["target_obligations"]["obligations"][0]["source"]["content_sha256"] = "a" * 64
            elif variant == "source_hash":
                analysis["target_obligations"]["sources"][0]["content_sha256"] = "a" * 64
            elif variant == "quote":
                analysis["target_obligations"]["obligations"][0]["source"]["quote"] = "This source text does not exist."
            elif variant == "missing_source":
                analysis["target_obligations"]["sources"] = []
            else:
                analysis["target_obligations"]["sources"][0]["obligation_ids"] = ["O-UNKNOWN"]
            with self.subTest(variant=variant), self.assertRaises(RefinerError):
                validate_obligations(analysis, standards)

    def test_source_quote_allows_layout_linebreaks_but_keeps_text_and_hash_bound(self):
        analysis = copy.deepcopy(self.analysis)
        standards = ["The target must expose the\nrequested result token."]
        authority = standard_sources(standards)[0]
        analysis["target_obligations"]["sources"][0]["content_sha256"] = authority["content_sha256"]
        row = analysis["target_obligations"]["obligations"][0]
        row["source"]["content_sha256"] = authority["content_sha256"]
        row["source"]["quote"] = "The target must expose the requested result token."
        validate_obligations(analysis, standards)
        row["source"]["quote"] = "The target must expose a different result token."
        with self.assertRaisesRegex(RefinerError, "not grounded in its frozen standard"):
            validate_obligations(analysis, standards)
        row["source"]["quote"] = "The target must expose the requested result token."
        row["source"]["content_sha256"] = "a" * 64
        with self.assertRaisesRegex(RefinerError, "not grounded in its frozen standard"):
            validate_obligations(analysis, standards)

    def test_creation_source_identity_survives_duplicate_discovered_paths_not_content_changes(self):
        context = self.context()
        context["creation_standard_sources"] = copy.deepcopy(self.standards)
        context["standards"] += [{"source": "frozen-copy.md", "content": self.standards[0]}]
        validate_obligation_plans(context, self.plans())
        context["standards"].append({"source": "new-standard.md", "content": "A newly unreviewed target rule."})
        with self.assertRaisesRegex(RefinerError, "reviewed creation standard authority"):
            validate_obligation_plans(context, self.plans())

    def test_standard_cannot_be_forged_as_spec_requirement(self):
        self.analysis["requirements"].append(effective_requirements(self.analysis)[-1])
        self.analysis["requirements"][-1]["id"] = "FORGED"
        # Strip O first to isolate the business-source rejection.
        self.analysis["requirements"][-1]["acceptance_criteria"][0]["id"] = "FORGED.A1"
        with self.assertRaisesRegex(RefinerError, "original business source"):
            validate_analysis(self.analysis, source_ids={"spec-1"}, standards=self.standards)
        projection = effective_requirements(self.analysis)
        self.assertTrue(projection[-1]["source"].startswith("standard-1: sha256="))

    def test_controller_and_excluded_obligations_cannot_enter_target_cases(self):
        for subject, applicable in (("controller", True), ("target", False)):
            analysis = copy.deepcopy(self.analysis)
            row = analysis["target_obligations"]["obligations"][0]
            row.update(subject=subject, applicable=applicable)
            row["mechanism"]["state"] = "planned" if subject == "controller" else "not_applicable"
            self.assertEqual(len(effective_requirements(analysis)), 1)
            with self.subTest(subject=subject), self.assertRaisesRegex(RefinerError, "cannot become target"):
                validate_obligations(analysis, self.standards)
            row["scenario_ids"], row["workflow_ids"] = [], []
            analysis["workflow"][0]["requirement_ids"].remove("O-RESULT")
            analysis["scenarios"][0]["requirement_ids"].remove("O-RESULT")
            analysis["scenarios"][0]["acceptance_criteria_ids"].remove("O-RESULT.A1")
            validate_analysis(analysis, standards=self.standards)

    def test_excluded_target_must_be_not_applicable_but_controller_can_be_planned(self):
        controller = copy.deepcopy(self.analysis)
        row = controller["target_obligations"]["obligations"][0]
        row.update(subject="controller", applicable=True, scenario_ids=[], workflow_ids=[])
        row["mechanism"]["state"] = "planned"
        controller["workflow"][0]["requirement_ids"].remove("O-RESULT")
        controller["scenarios"][0]["requirement_ids"].remove("O-RESULT")
        controller["scenarios"][0]["acceptance_criteria_ids"].remove("O-RESULT.A1")
        validate_analysis(controller, standards=self.standards)

        controller["target_obligations"]["obligations"][0]["mechanism"]["state"] = "not_applicable"
        with self.assertRaisesRegex(RefinerError, "Applicable controller obligation"):
            validate_obligations(controller, self.standards)

        excluded = copy.deepcopy(self.analysis)
        row = excluded["target_obligations"]["obligations"][0]
        row.update(applicable=False, scenario_ids=[], workflow_ids=[])
        row["mechanism"]["state"] = "planned"
        excluded["workflow"][0]["requirement_ids"].remove("O-RESULT")
        excluded["scenarios"][0]["requirement_ids"].remove("O-RESULT")
        excluded["scenarios"][0]["acceptance_criteria_ids"].remove("O-RESULT.A1")
        with self.assertRaisesRegex(RefinerError, "Excluded target obligation"):
            validate_obligations(excluded, self.standards)

    def test_required_obligation_needs_atomic_criteria_and_bidirectional_links(self):
        for variant in ("criterion", "scenario", "workflow", "criterion_coverage", "duplicate_id"):
            analysis = copy.deepcopy(self.analysis)
            row = analysis["target_obligations"]["obligations"][0]
            if variant == "criterion":
                row["acceptance_criteria"] = []
            elif variant == "scenario":
                row["scenario_ids"] = []
            elif variant == "workflow":
                row["workflow_ids"] = []
            elif variant == "criterion_coverage":
                analysis["scenarios"][0]["acceptance_criteria_ids"].remove("O-RESULT.A1")
            else:
                row["acceptance_criteria"][0]["id"] = "R1.A1"
            with self.subTest(variant=variant), self.assertRaises(RefinerError):
                validate_analysis(analysis, standards=self.standards)

    def test_independent_review_covers_all_sources_and_obligations_not_only_business(self):
        validate_analysis_review(self.review(), self.analysis)
        jsonschema.validate(self.review(), codex_output_schema(ANALYSIS_REVIEW_V4))
        for variant in ("source", "obligation", "required", "false_assessment", "wrong_hash", "legacy"):
            review = self.review()
            if variant == "source":
                review["standard_source_assessments"] = []
            elif variant == "obligation":
                review["obligation_assessments"] = []
            elif variant == "required":
                review["requirement_assessments"].pop()
            elif variant == "false_assessment":
                review["obligation_assessments"][0]["mechanism_and_gaps_explicit"] = False
            elif variant == "wrong_hash":
                review["standard_source_assessments"][0]["content_sha256"] = "a" * 64
            else:
                review["schema_version"] = "skill-refiner.analysis-review/v3"
            with self.subTest(variant=variant), self.assertRaises((RefinerError, jsonschema.ValidationError)):
                validate_analysis_review(review, self.analysis)

    def test_both_frozen_plans_keep_each_obligation_criterion_and_scenario(self):
        context, plans = self.context(), self.plans()
        validate_obligation_plans(context, plans)
        with tempfile.TemporaryDirectory() as directory:
            for plan in plans.values():
                validate_plan(plan, directory, expected_requirements=context["contract"],
                              expected_scenarios=self.analysis["scenarios"],
                              required_scenario_ids=[s["id"] for s in self.analysis["scenarios"] if s["applicable"]],
                              required_case_kinds=["normal", "boundary"])
        for suite in plans:
            for variant in ("requirement", "criterion", "scenario", "stage", "work_step"):
                changed = copy.deepcopy(plans)
                if variant == "requirement":
                    changed[suite]["requirements"].pop()
                elif variant == "criterion":
                    changed[suite]["cases"][0]["acceptance_criteria_ids"].remove("O-RESULT.A1")
                elif variant == "scenario":
                    changed[suite]["cases"][0]["scenario_id"] = "SC-UNKNOWN"
                elif variant == "stage":
                    changed[suite]["stage_contracts"]["stages"][0]["requirement_ids"].remove("O-RESULT")
                else:
                    for step in changed[suite]["execution_plan"]["steps"]:
                        step["requirement_ids"].remove("O-RESULT")
                with self.subTest(suite=suite, variant=variant), self.assertRaises(RefinerError):
                    validate_obligation_plans(context, changed)

    def test_concrete_technical_gap_is_preserved_and_blocks_evaluation_gate(self):
        row = self.obligation
        row["mechanism"].update(state="blocked", gap_ids=["G-NATIVE-OBSERVATION"])
        self.analysis["target_obligations"]["coverage_gaps"] = [{
            "id": "G-NATIVE-OBSERVATION", "obligation_ids": [row["id"]],
            "scenario_ids": row["scenario_ids"], "reason": "No reliable full-scope observation carrier is available.",
            "required_evidence": "Actual complete action observation in the target execution scope."}]
        validate_analysis(self.analysis, standards=self.standards)
        validate_obligation_plans(self.context(), self.plans(), require_closed=False)
        with self.assertRaisesRegex(RefinerError, "unresolved technical coverage"):
            validate_obligation_plans(self.context(), self.plans())
        row["mechanism"]["gap_ids"] = []
        with self.assertRaisesRegex(RefinerError, "concrete coverage gap"):
            validate_obligations(self.analysis, self.standards)

    def test_same_id_mechanism_subject_and_source_changes_enter_semantic_revision(self):
        for field, value in (("subject", "controller"), ("behavior", "Silently drop the output behavior.")):
            revised = copy.deepcopy(self.analysis)
            revised["target_obligations"]["obligations"][0][field] = value
            delta = analysis_revision_delta(self.analysis, revised)
            self.assertIn("O-RESULT", delta["requirements_changed"])
            self.assertIn("/standard_obligation/" + field, delta["changes"][0]["changed_paths"])
            validate_value(delta, schema_ref("skill-refiner.analysis-revision-delta-v2"))
        revised = copy.deepcopy(self.analysis)
        revised["target_obligations"]["obligations"][0]["mechanism"]["interface"] = "A newly changed public interface."
        delta = analysis_revision_delta(self.analysis, revised)
        self.assertIn("O-RESULT", delta["requirements_changed"])
        with self.assertRaisesRegex(RefinerError, "every changed ID"):
            validate_analysis_revision_review(self.review(), delta, standard_sources(self.standards), revised)

    def test_real_judge_diagnostics_required_for_every_executor_and_frozen_check(self):
        plans = self.plans()
        evidence = self.execution(plans)
        validate_obligation_results(self.context(), plans, evidence, executor_ids=["first", "second"])
        for variant in ("missing_executor", "missing_case", "failed_case", "summary_only", "wrong_case",
                        "missing_assertion", "changed_check", "failed_assertion"):
            changed = copy.deepcopy(evidence)
            engine = changed["acceptance"]["engines"][1]
            row = engine["cases"][0]
            if variant == "missing_executor":
                changed["acceptance"]["engines"].pop()
            elif variant == "missing_case":
                engine["cases"].pop(0)
            elif variant == "failed_case":
                row.update(status="FAIL", passed=False)
            elif variant == "summary_only":
                row.pop("evidence")
            elif variant == "wrong_case":
                row["evidence"]["case_id"] = "forged"
            else:
                record = row["evidence"]["grading"]["assertion_results"][0]
                diagnostic = json.loads(record["evidence"])
                if variant == "missing_assertion":
                    diagnostic["assertions"] = []
                elif variant == "changed_check":
                    diagnostic["assertions"][0]["check"]["value"] = "forged"
                else:
                    diagnostic["assertions"][0]["passed"] = False
                record["evidence"] = json.dumps(diagnostic)
            with self.subTest(variant=variant), self.assertRaises(RefinerError):
                validate_obligation_results(self.context(), plans, changed, executor_ids=["first", "second"])

    def test_legacy_contracts_are_not_silently_reinterpreted_as_v4(self):
        old = analysis_fixture.AnalysisV3Tests("runTest").analysis()
        validate_analysis(old)
        self.assertEqual(effective_requirements(old), old["requirements"])
        validate_obligation_plans({"business_analysis": old}, {})
        old["target_obligations"] = self.analysis["target_obligations"]
        with self.assertRaises(RefinerError):
            validate_obligations(old, self.standards)

    def test_create_does_not_infer_case_capacity_from_analysis_scenarios(self):
        self.analysis["requirements"][0]["source"] = "business_description: Return the requested result."
        scenario = copy.deepcopy(self.analysis["scenarios"][0])
        scenario.update(id="SC-O-ERROR", kind="error", situation="An independently injected validator failure occurs.")
        scenario["detail"]["stimulus"] = "Inject a local validator failure before the handoff."
        scenario["detail"]["risk_dimensions"] = ["dependency_failure"]
        self.analysis["scenarios"].append(scenario)
        self.obligation["scenario_ids"].append(scenario["id"])
        risk = next(r for r in self.analysis["risk_assessments"] if r["dimension"] == "dependency_failure")
        risk.update(applicable=True, scenario_ids=[scenario["id"]])
        roles = []
        def ask(agent, role, instruction, payload, schema):
            roles.append(role)
            if role == "analyze":
                self.assertEqual(schema["properties"]["schema_version"]["const"], "skill-refiner.analysis/v4")
                self.assertEqual(payload["frozen_standard_sources"], standard_sources(self.standards))
                return copy.deepcopy(self.analysis)
            if role == "analysis-review":
                self.assertEqual(payload["effective_contract"], effective_requirements(self.analysis))
                return self.review()
            if role == "draft":
                raise RefinerError("Test stops after confirming analysis did not over-infer case capacity")
            self.fail("Capacity preflight must stop before draft or any target evaluation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, output = root / "config.json", root / "run"
            write_json(config, {"schema_version": 1,
                "agents": {"default": {"backend": "command", "command": ["offline-never-executed"]}},
                "executors": [{"id": "offline", "backend": "command", "command": ["offline-never-executed"]}]})
            with patch("refiner.creation.AUTHORING", self.standards[0]), patch("refiner.agent.Agent.ask", ask):
                code = main(["create", "Return the requested result.", "--name", "demo-skill",
                             "--config", str(config), "--output", str(output), "--max-cases", "2",
                             "--acceptance-cases", "2", "--max-agent-calls", "32", "--max-seconds", "3600"])
            self.assertEqual(code, 2)
            self.assertEqual(roles, ["analyze", "analysis-review", "draft"])
            report = read_json(output / "runtime-preflight-after-analysis.json")
            self.assertEqual(report["status"], "passed")
            self.assertFalse(any(r["code"] == "INSUFFICIENT_CASE_CAPACITY" for r in report["issues"]))
            self.assertFalse((output / "drafts").exists())

    def test_pipeline_gap_gate_prevents_any_target_execution(self):
        from refiner import pipeline
        original_run = pipeline.run
        self.obligation["mechanism"].update(state="blocked", gap_ids=["G-OBS"])
        self.analysis["target_obligations"]["coverage_gaps"] = [{
            "id": "G-OBS", "obligation_ids": ["O-RESULT"], "scenario_ids": self.obligation["scenario_ids"],
            "reason": "Independent target action observation is not available in this scope.",
            "required_evidence": "A complete real observation carrier, not synthetic controls or file hash snapshots."}]
        plans = self.plans()
        # The acceptance suite uses different fixture inputs; frozen expected semantics stay the same.
        for case in plans["acceptance"]["cases"]:
            case["files"] = {"fresh-input.txt": "Acceptance fixture, not development."}
        def run(args):
            args.creation_context = {"business_analysis": self.analysis,
                                     "contract": effective_requirements(self.analysis)}
            return original_run(args)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, output, config = root / "demo-skill", root / "run", root / "config.json"
            target.mkdir()
            (target / "SKILL.md").write_text("---\nname: demo-skill\ndescription: 本地控制测试。\n---\n返回结果。",
                                           encoding="utf-8")
            write_json(config, {"schema_version": 1,
                "agents": {"default": {"backend": "command", "command": ["offline-never-executed"]}},
                "executors": [{"id": "offline", "backend": "command", "command": ["offline-never-executed"]}]})
            with (patch("refiner.pipeline.run", side_effect=run),
                  patch("refiner.pipeline.AUTHORING", self.standards[0]),
                  patch("refiner.pipeline.make_plan_suites", return_value=(plans["development"], plans["acceptance"])),
                  patch("refiner.agent.Agent.ask") as ask,
                  patch("refiner.matrix.Matrix.evaluate") as evaluate):
                code = main(["run", str(target), "--config", str(config), "--output", str(output),
                             "--skill-up", sys.executable, "--max-agent-calls", "32", "--max-seconds", "3600"])
            self.assertEqual(code, 2)
            state = read_json(output / "state.json")
            self.assertEqual(state["status"], "plan_not_passed")
            self.assertIn("G-OBS", str(state["gaps"]))
            ask.assert_not_called()
            evaluate.assert_not_called()
            self.assertFalse((output / "baseline").exists())

    def test_new_execution_cannot_reuse_legacy_approved_analysis_or_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, output, config = root / "legacy", root / "new-execution", root / "config.json"
            old.mkdir()
            analysis = analysis_fixture.AnalysisV3Tests("runTest").analysis()
            write_json(old / "analysis.json", analysis)
            write_json(old / "analysis-review.json", analysis_fixture.AnalysisV3Tests.review(analysis))
            (old / "drafts/demo-skill").mkdir(parents=True)
            (old / "drafts/demo-skill/SKILL.md").write_text("原先已保存的草稿证据。", encoding="utf-8")
            write_json(old / "state.json", {"mode": "create", "status": "planned",
                                           "draft_root": str(old / "drafts/demo-skill")})
            before = {p.relative_to(old).as_posix(): p.read_bytes() for p in old.rglob("*") if p.is_file()}
            write_json(config, {"schema_version": 1,
                "agents": {"default": {"backend": "command", "command": ["offline-never-executed"]}},
                "executors": [{"id": "offline", "backend": "command", "command": ["offline-never-executed"]}]})
            error = io.StringIO()
            with (patch("refiner.agent.Agent.ask") as ask, patch("refiner.creation.refine") as refine,
                  contextlib.redirect_stderr(error)):
                code = main(["create", "--resume-from", str(old), "--output", str(output), "--config", str(config)])
            self.assertEqual(code, 2)
            self.assertIn("no verified incremental standard-obligation migration", error.getvalue())
            self.assertIn("old evidence is preserved", error.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(before, {p.relative_to(old).as_posix(): p.read_bytes() for p in old.rglob("*") if p.is_file()})
            ask.assert_not_called()
            refine.assert_not_called()


if __name__ == "__main__":
    unittest.main()

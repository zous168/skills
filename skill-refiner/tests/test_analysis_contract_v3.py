"""Offline validation of analysis feedback, versioning, and scenario semantics."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_plan_contract as fixtures
from refiner.agent import Agent, _analysis_schema_errors
from refiner.artifact_validation import content_schema_id
from refiner.common import RefinerError, read_json, write_json
from refiner.cli import main
from refiner.checks import validate_plan
from refiner.creation import (ANALYSIS_V1, ANALYSIS_V2, ANALYSIS_V3, analysis_markdown,
                              preflight_creation_case_capacity, validate_analysis,
                              validate_analysis_review)
from refiner.pipeline import make_plan, validate_plan_review
from refiner.runtime import codex_output_schema, decode_codex_response
from refiner.schemas import PLAN_V5, PLAN_V6


class AnalysisV3Tests(unittest.TestCase):
    def analysis(self):
        value = fixtures.CreationAnalysisConclusionTests("runTest")._analysis()
        value["schema_version"] = "skill-refiner.analysis/v3"
        for scenario in value["scenarios"]:
            if not scenario["applicable"]:
                scenario.update(requirement_ids=[], acceptance_criteria_ids=[], detail=None)
        return value

    def test_exclusions_are_explainable_without_fabricated_test_or_links(self):
        analysis = self.analysis()
        validate_analysis(analysis)
        self.assertIn("Excluded because", analysis_markdown(analysis))
        self.assertEqual(decode_codex_response(analysis, ANALYSIS_V3), analysis)
        wire = codex_output_schema(ANALYSIS_V3)
        jsonschema.validate(analysis, wire)
        legacy = copy.deepcopy(analysis)
        legacy["schema_version"] = "skill-refiner.analysis/v2"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(legacy, ANALYSIS_V2)

    def test_applicable_scenarios_and_required_coverage_still_enforced(self):
        for field, value in (("requirement_ids", []), ("acceptance_criteria_ids", []), ("detail", None)):
            analysis = self.analysis()
            analysis["scenarios"][0][field] = value
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                validate_analysis(analysis)
        analysis = self.analysis()
        analysis["requirements"][0]["acceptance_criteria"].append(
            {"id": "R1.A2", "statement": "Honor a second distinct outcome.", "evidence": "Observe a second outcome."})
        with self.assertRaisesRegex(RefinerError, "R1.A2"):
            validate_analysis(analysis)

    def test_concise_chinese_title_and_forbidden_outcome_without_english_regression(self):
        analysis = self.analysis()
        detail = analysis["scenarios"][0]["detail"]
        detail["title"] = "一般咨询"
        detail["forbidden_outcomes"] = ["遗漏字段"]
        validate_analysis(analysis)
        detail["title"] = "bad"
        with self.assertRaises(jsonschema.ValidationError):
            validate_analysis(analysis)

    def test_prior_workflow_step_and_real_external_dependencies(self):
        analysis = self.analysis()
        first = analysis["workflow"][0]
        second = {**copy.deepcopy(first), "id": "W2", "order": 2, "dependency_ids": ["W1"]}
        analysis["workflow"].append(second)
        validate_analysis(analysis)
        for dependencies in (["W2"], ["UNKNOWN"]):
            second["dependency_ids"] = dependencies
            with self.subTest(dependencies=dependencies), self.assertRaises(RefinerError):
                validate_analysis(analysis)
        second["dependency_ids"] = []
        first["dependency_ids"] = ["W2"]
        with self.assertRaisesRegex(RefinerError, "earlier workflow steps"):
            validate_analysis(analysis)
        first["dependency_ids"] = []
        analysis["external_dependencies"] = [{"id": "EXT1", "name": "Declared service",
            "purpose": "Obtain the requested result", "required": True, "availability": "Available when configured",
            "failure_behavior": "Report unavailable service", "requirement_ids": ["R1"]}]
        second["dependency_ids"] = ["W1", "EXT1"]
        validate_analysis(analysis)

    def test_v3_review_verifies_exclusions_and_retains_applicable_risk_checks(self):
        analysis = self.analysis()
        review = self.review(analysis)
        validate_analysis_review(review, analysis)
        excluded = next(row for row in review["scenario_assessments"] if not row["detail_executable"])
        excluded["risk_coverage_complete"] = False
        with self.assertRaisesRegex(RefinerError, "contradicts"):
            validate_analysis_review(review, analysis)
        excluded["risk_coverage_complete"] = True
        review["scenario_assessments"][0]["traceability_complete"] = False
        with self.assertRaisesRegex(RefinerError, "contradicts"):
            validate_analysis_review(review, analysis)

    @staticmethod
    def review(analysis):
        return {"schema_version": "skill-refiner.analysis-review/v2", "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                "acceptance_criteria_atomic": True, "criteria_observable": True,
                "basis": "The source and criterion are traceable and observable."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": row["applicable"],
                "risk_coverage_complete": True, "traceability_complete": row["applicable"],
                "basis": "The applicable behavior or the exclusion is supported by scope."} for row in analysis["scenarios"]],
            "context_scope_assessment": {"intent_grounded": True, "actors_complete": True,
                "scope_clear": True, "constraints_complete": True,
                "basis": "User, scope and boundaries are grounded in the request."},
            "workflow_assessments": [{"workflow_id": row["id"], "order_and_actors_clear": True,
                "inputs_outputs_complete": True, "completion_and_failure_testable": True,
                "requirement_traceable": True, "basis": "Step and completion can be observed."}
                for row in analysis["workflow"]],
            "risk_assessments": [{"dimension": row["dimension"], "applicability_justified": True,
                "scenario_links_complete": True, "basis": "The dimension and links were checked."}
                for row in analysis["risk_assessments"]],
            "dependencies_assessed": True, "blocking_decisions_actionable": True}

    def test_25_applicable_scenarios_defer_case_count_to_frozen_plan(self):
        analysis = {"scenarios": [{"id": f"S-{index:02}", "applicable": True}
                                   for index in range(1, 26)]}

        result = preflight_creation_case_capacity(analysis, 12, 12)

        self.assertEqual(result["status"], "deferred_to_frozen_plan")
        self.assertEqual(result["applicable_scenario_count"], 25)
        self.assertEqual(result["case_capacities"], {"development": 12, "acceptance": 12})
        self.assertIn("frozen plan", result["decision_source"])

    def test_13_distinct_development_cases_are_rejected_by_plan_gate_before_evaluation(self):
        plan = fixtures._minimal_plan()
        template = plan["cases"][0]
        cases = []
        for index in range(1, 14):
            row = copy.deepcopy(template)
            row["id"] = f"dev-case-{index:02}"
            row["prompt"] = f"{template['prompt']} Execute distinct input variation {index}."
            row["scenario"] = f"Distinct scenario {index} is submitted through the documented workflow."
            row["expected_outcome"] = f"The response for distinct scenario {index} follows its mapped criterion."
            cases.append(row)
        plan["cases"] = cases
        case_ids = [row["id"] for row in cases]
        for step in plan["execution_plan"]["steps"]:
            step["case_ids"] = case_ids
        target_evaluations = []

        with tempfile.TemporaryDirectory() as directory:
            # validate_plan is the planning gate; target evaluation is only
            # reachable after it returns successfully.
            with self.assertRaisesRegex(RefinerError, "Plan exceeds 12 cases"):
                validate_plan(plan, directory, max_cases=12)

        self.assertEqual(target_evaluations, [])
    def test_analysis_schema_selection_and_v2_legacy_validation(self):
        legacy = fixtures.CreationAnalysisConclusionTests("runTest")._analysis()
        validate_analysis(legacy)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            write_json(path, legacy)
            self.assertEqual(content_schema_id(path), "skill-refiner.analysis-v2")
            write_json(path, self.analysis())
            self.assertEqual(content_schema_id(path), "skill-refiner.analysis-v3")
            legacy.pop("schema_version")
            write_json(path, legacy)
            self.assertEqual(content_schema_id(path), "skill-refiner.analysis")

    def test_full_schema_feedback_and_full_prior_response_are_sent_on_retry(self):
        schema = {"type": "object", "required": ["a", "b", "keep"], "properties": {
            "a": {"type": "string", "minLength": 5}, "b": {"type": "integer"}, "keep": {"type": "string"}}}
        invalid = {"a": "x", "b": "bad", "keep": "Z" * 13000}
        valid = {"a": "valid", "b": 3, "keep": invalid["keep"]}
        calls = []
        def fake_process(*args, **kwargs):
            request = json.loads(kwargs["input_text"])
            calls.append(request)
            value = invalid if len(calls) == 1 else valid
            return 0, json.dumps(value, ensure_ascii=False), "", 0.01
        with tempfile.TemporaryDirectory() as directory:
            config = {"agents": {"default": {"backend": "command", "command": ["fixture"]}}}
            agent = Agent(directory, config=config)
            with patch("refiner.agent.run_process", side_effect=fake_process):
                result = agent.ask("analyze", "analyze", {"business_sources": ["original"]}, schema)
            self.assertEqual(result, valid)
            repair = calls[1]["input"]
            self.assertEqual(repair["business_sources"], ["original"])
            self.assertEqual(json.loads(repair["previous_invalid_response"]), invalid)
            self.assertEqual({e["path"] for e in repair["contract_errors"]}, {"/a", "/b"})
            self.assertEqual({e["constraint"] for e in repair["contract_errors"]}, {"minLength", "type"})
            self.assertIn("Preserve all business requirements", repair["revision_instruction"])
            saved = read_json(Path(directory) / "agent-calls/001-analyze/contract-errors.json")
            self.assertEqual(saved["errors"], repair["contract_errors"])

    def test_schema_feedback_lists_each_missing_field_once(self):
        schema = {"type": "object", "required": ["x", "y"], "properties": {"x": {}, "y": {}}}
        errors = _analysis_schema_errors(schema, {})
        self.assertEqual([item["path"] for item in errors], ["/x", "/y"])
        self.assertTrue(all(item["actual"] == {"missing": True} for item in errors))

    def test_codex_decode_error_still_reports_all_analysis_schema_errors(self):
        schema = {"type": "object", "required": ["arr", "n"], "properties": {
            "arr": {"type": "array", "items": {"type": "string"}}, "n": {"type": "integer"}}}
        invalid = {"arr": "wrong shape", "n": "wrong type"}
        valid = {"arr": ["ok"], "n": 1}
        outputs = [{"final_message": json.dumps(invalid)}, {"final_message": json.dumps(valid)}]
        with tempfile.TemporaryDirectory() as directory:
            config = {"agents": {"default": {"backend": "codex", "model": "offline"}}}
            agent = Agent(directory, config=config)
            with patch("refiner.runtime.execute", side_effect=outputs):
                self.assertEqual(agent.ask("analyze", "analyze", {}, schema), valid)
            errors = read_json(Path(directory) / "agent-calls/001-analyze/contract-errors.json")["errors"]
            self.assertEqual({e["path"] for e in errors}, {"/arr", "/n"})

    def test_chinese_analysis_detail_stays_exact_and_valid_in_plan_v6(self):
        plan = fixtures._minimal_plan()
        detail = plan["cases"][0]["scenario_detail"]
        detail["title"] = "一般咨询"
        detail["forbidden_outcomes"] = ["遗漏字段"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(plan, PLAN_V5)
        plan["schema_version"] = "skill-refiner.plan/v6"
        jsonschema.validate(plan, PLAN_V6)
        wire = codex_output_schema(PLAN_V6)
        wire_title = wire["properties"]["cases"]["items"]["properties"]["scenario_detail"]["properties"]["title"]
        self.assertEqual(wire_title, {"type": "string"})
        scenario_schema = PLAN_V6["properties"]["cases"]["items"]["properties"]["scenario_detail"]
        self.assertEqual(decode_codex_response(detail, scenario_schema), detail)
        scenarios = [{"id": c["scenario_id"], "kind": c["kind"], "applicable": True,
                      "requirement_ids": c["requirement_ids"], "acceptance_criteria_ids": c["acceptance_criteria_ids"],
                      "situation": c["scenario"], "detail": c["scenario_detail"]} for c in plan["cases"]]
        with tempfile.TemporaryDirectory() as directory:
            validate_plan(plan, directory, expected_scenarios=scenarios,
                          required_scenario_ids=[s["id"] for s in scenarios])
            file = Path(directory) / "test-plan.json"
            write_json(file, plan)
            self.assertEqual(content_schema_id(file), "skill-refiner.plan-v6")
            altered = copy.deepcopy(scenarios)
            altered[0]["detail"]["title"] = "其他咨询"
            with self.assertRaises(RefinerError):
                validate_plan(plan, directory, expected_scenarios=altered,
                              required_scenario_ids=[s["id"] for s in altered])
        review = fixtures._approved_review(plan)
        validate_plan_review(review, plan)
        review["execution_plan_complete"] = False
        with self.assertRaisesRegex(RefinerError, "execution plan"):
            validate_plan_review(review, plan)

    def test_both_planning_roles_choose_v6_only_for_v3_analysis(self):
        seen = []
        class FakeAgent:
            def ask(self, role, instruction, payload, schema):
                seen.append((role, schema["properties"]["schema_version"]["const"]))
                raise RefinerError("controlled offline stop")
        with tempfile.TemporaryDirectory() as directory:
            for role in ("plan", "acceptance-plan"):
                with self.assertRaisesRegex(RefinerError, "controlled offline stop"):
                    make_plan(FakeAgent(), {"business_analysis": self.analysis()}, directory, 12, role=role)
            with self.assertRaisesRegex(RefinerError, "controlled offline stop"):
                make_plan(FakeAgent(), {}, directory, 12)
        self.assertEqual(seen, [("plan", "skill-refiner.plan/v6"),
                                ("acceptance-plan", "skill-refiner.plan/v6"),
                                ("plan", "skill-refiner.plan/v5")])


if __name__ == "__main__":
    unittest.main()

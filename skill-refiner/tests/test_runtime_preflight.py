"""Offline feasibility checks for saved budgets and explicit checkpoint facts."""
from __future__ import annotations

import copy
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from refiner.common import RefinerError  # noqa: E402
from refiner.preflight import (  # noqa: E402
    CREATE_PLANNING_STEPS, REFINE_PLANNING_STEPS, SCHEMA_PATH,
    build_runtime_preflight, emit_runtime_preflight, validate_runtime_preflight,
)


class RuntimePreflightTests(unittest.TestCase):
    def setUp(self):
        self.limits = {"max_seconds": 1200, "max_agent_calls": 20,
                       "max_evaluations": 40, "max_cases": 5, "acceptance_cases": 5}
        self.config = {"schema_version": 1, "agents": {
            "planning": {"backend": "codex", "model": "gpt-6-astra", "timeout": 120,
                         "api_key_env": "PRIVATE_KEY_ENV"}},
            "executors": [{"id": "child", "backend": "codex_subagent", "model": "gpt-6-astra"}]}

    def build(self, **changes):
        args = {"mode": "create", "phase": "before_analysis", "limits": self.limits,
                "config": self.config, "applicable_scenario_count": 5}
        args.update(changes)
        return build_runtime_preflight(**args)

    def codes(self, report):
        return {issue["code"] for issue in report["issues"]}

    def test_saved_five_call_remainder_cannot_complete_seven_call_create_plan(self):
        limits = {**self.limits, "max_agent_calls": 7}
        report = self.build(limits=limits, usage={"agent_calls": 2}, plan_only=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["minimum_required"]["pending_steps"], list(CREATE_PLANNING_STEPS))
        issue = next(row for row in report["issues"] if row["code"] == "INSUFFICIENT_AGENT_CALLS")
        self.assertEqual((issue["field"], issue["expected"], issue["actual"]),
                         ("remaining_budget.agent_calls", 7, 5))

    def test_scenario_inventory_does_not_impose_one_case_per_scenario(self):
        report = self.build(limits={**self.limits, "max_cases": 12, "acceptance_cases": 12},
                            applicable_scenario_count=25)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["coverage"]["applicable_scenario_count"], 25)
        self.assertEqual(report["coverage"]["minimum_cases_per_suite"], 2)
        self.assertEqual(report["coverage"]["verification"], "known_minimum_only")
        self.assertNotIn("INSUFFICIENT_CASE_CAPACITY", self.codes(report))

    def test_frozen_plan_over_capacity_is_blocked_with_actual_case_count(self):
        report = self.build(limits={**self.limits, "max_cases": 12, "acceptance_cases": 12},
                            applicable_scenario_count=25,
                            planned_case_counts={"development": 13, "acceptance": 12})
        self.assertEqual(report["status"], "failed")
        issue = next(row for row in report["issues"] if row["code"] == "PLANNED_CASES_EXCEED_LIMIT")
        self.assertEqual((issue["field"], issue["expected"], issue["actual"]),
                         ("planned_case_counts.development", 12, 13))

    def test_only_explicit_validated_step_ids_reduce_call_floor(self):
        limits = {**self.limits, "max_agent_calls": 7}
        report = self.build(limits=limits, usage={"agent_calls": 3}, plan_only=True,
                            validated_reused_steps=["analyze", "analysis-review", "draft"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["minimum_required"]["pending_steps"], list(REFINE_PLANNING_STEPS))
        self.assertEqual(report["minimum_required"]["agent_calls"], 4)
        for invalid in (["analysis.json"], ["analyze", "analyze"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.build(validated_reused_steps=invalid)

    def test_zero_or_negative_remaining_time_is_terminal_not_clamped(self):
        for elapsed in (1200, 1250):
            with self.subTest(elapsed=elapsed):
                report = self.build(usage={"elapsed_seconds": elapsed})
                self.assertIn("RUN_TIME_EXHAUSTED", self.codes(report))
                self.assertEqual(report["remaining_budget"]["seconds"], 1200 - elapsed)

    def test_unpinned_effort_is_explicitly_unresolved_and_never_live_verified(self):
        report = self.build()
        profile = next(p for p in report["execution_profiles"] if p["profile"] == "agent:planning")
        self.assertIn("reasoning_effort", profile["unresolved_inherited_fields"])
        self.assertEqual(profile["declared_settings"]["timeout"], 120)
        self.assertEqual(profile["inference_verification"], "not_live_tested")
        self.assertEqual(report["verification"]["effective_configuration"], "not_resolved")
        self.assertEqual(report["verification"]["time_feasibility"], "positive_remaining_time_only")
        self.assertFalse(report["verification"]["repair_rounds_guaranteed"])
        self.assertNotIn("api_key_env", profile["declared_settings"])
        self.assertNotIn("PRIVATE_KEY_ENV", json.dumps(report))

    def test_full_run_reserves_standards_review_without_guaranteeing_repairs(self):
        full = self.build()
        plan = self.build(plan_only=True)
        self.assertEqual(full["minimum_required"]["agent_calls"], 8)
        self.assertEqual(plan["minimum_required"]["agent_calls"], 7)
        self.assertEqual(plan["minimum_required"]["case_evaluations"], 0)
        self.assertEqual(plan["minimum_required"]["full_run_case_evaluation_floor"], 4)
        self.assertEqual(self.build(mode="refine")["minimum_required"]["agent_calls"], 5)

    def test_evaluation_floor_counts_engines_and_recorded_consumption(self):
        report = self.build(executor_count=2, usage={"target_evaluations": 2},
                            limits={**self.limits, "max_evaluations": 7})
        self.assertEqual(report["minimum_required"]["case_evaluations"], 8)
        self.assertIn("INSUFFICIENT_EVALUATIONS", self.codes(report))
        reused = self.build(executor_count=2, usage={"target_evaluations": 2},
                            validated_completed_evaluations=2,
                            limits={**self.limits, "max_evaluations": 8})
        self.assertEqual(reused["minimum_required"]["case_evaluations"], 6)
        self.assertNotIn("INSUFFICIENT_EVALUATIONS", self.codes(reused))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            self.build(usage={"target_evaluations": 1}, validated_completed_evaluations=2)

    def test_scenario_count_is_context_only_before_frozen_plans(self):
        limits = {**self.limits, "max_cases": 2, "acceptance_cases": 2}
        unknown = self.build(limits=limits, applicable_scenario_count=None)
        self.assertEqual(unknown["status"], "passed")
        self.assertEqual(unknown["coverage"]["verification"], "known_minimum_only")
        self.assertIn("SCENARIO_CASE_MAPPING_DEFERRED", {row["code"] for row in unknown["warnings"]})
        known = self.build(limits=limits, applicable_scenario_count=5)
        self.assertEqual(known["status"], "passed")
        self.assertEqual(known["coverage"]["verification"], "known_minimum_only")

    def test_frozen_four_plus_two_case_plan_needs_six_evaluations_not_the_scenario_floor(self):
        report = self.build(applicable_scenario_count=2,
                            planned_case_counts={"development": 4, "acceptance": 2},
                            limits={**self.limits, "max_evaluations": 5})
        self.assertEqual(report["planned_case_counts"], {"development": 4, "acceptance": 2})
        self.assertEqual(report["minimum_required"]["development_cases"], 4)
        self.assertEqual(report["minimum_required"]["acceptance_cases"], 2)
        self.assertEqual(report["minimum_required"]["case_evaluations"], 6)
        self.assertEqual(report["coverage"]["minimum_cases_per_suite"], 2)
        self.assertIn("INSUFFICIENT_EVALUATIONS", self.codes(report))
        plan_only = self.build(applicable_scenario_count=2, plan_only=True,
                               planned_case_counts={"development": 4, "acceptance": 2},
                               limits={**self.limits, "max_evaluations": 0})
        self.assertEqual(plan_only["status"], "passed")
        self.assertEqual(plan_only["minimum_required"]["case_evaluations"], 0)
        self.assertEqual(plan_only["minimum_required"]["full_run_case_evaluation_floor"], 6)

    def test_frozen_case_evaluation_cost_multiplies_by_executor_count(self):
        report = self.build(applicable_scenario_count=2, executor_count=2,
                            planned_case_counts={"development": 4, "acceptance": 2},
                            limits={**self.limits, "max_evaluations": 11})
        self.assertEqual(report["minimum_required"]["case_evaluations"], 12)
        self.assertIn("INSUFFICIENT_EVALUATIONS", self.codes(report))

    def test_frozen_case_counts_are_checked_for_structural_minimum_and_suite_cap(self):
        under = self.build(planned_case_counts={"development": 4, "acceptance": 2})
        coverage = {row["field"] for row in under["issues"]
                    if row["code"] == "INSUFFICIENT_PLANNED_COVERAGE"}
        self.assertEqual(coverage, set())
        self.assertEqual(under["status"], "passed")
        over = self.build(applicable_scenario_count=2,
                          planned_case_counts={"development": 6, "acceptance": 2})
        self.assertIn("PLANNED_CASES_EXCEED_LIMIT", self.codes(over))
        zero = self.build(applicable_scenario_count=2,
                          planned_case_counts={"development": 0, "acceptance": 2})
        self.assertEqual(zero["status"], "failed")
        validate_runtime_preflight(zero)

    def test_frozen_case_counts_reject_missing_extra_or_invalid_values(self):
        invalid = [{}, {"development": 4}, {"development": 4, "acceptance": 2, "extra": 1},
                   {"development": -1, "acceptance": 2}, {"development": True, "acceptance": 2},
                   {"development": 4, "acceptance": 2.5}, [4, 2]]
        for counts in invalid:
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                self.build(planned_case_counts=counts)

    def test_declared_launcher_defaults_have_provenance_and_inputs_are_not_mutated(self):
        origin = {"launch_manifest_path": "C:/skills/refiner/references/local-runtime.json",
                  "configured_defaults": {"max_agent_calls": 8, "max_seconds": 300},
                  "applied_defaults": {"max_seconds": 300, "timeout": 120, "api_key": "DO_NOT_PERSIST"},
                  "explicit_flags": ["--max-cases", "--plan-only"]}
        original = copy.deepcopy((self.limits, self.config, origin))
        report = self.build(launch_context=origin)
        self.assertEqual(report["configuration_origin"]["applied_defaults"]["timeout"], 120)
        self.assertEqual(report["configuration_origin"]["applied_defaults"]["api_key"], "[REDACTED]")
        self.assertNotIn("DO_NOT_PERSIST", json.dumps(report))
        self.assertEqual((self.limits, self.config, origin), original)

    def test_configured_defaults_are_not_misreported_as_applied_by_a_readonly_command(self):
        report = self.build(launch_context={"configured_defaults": {"max_agent_calls": 8,
                                                                    "max_seconds": 300, "timeout": 120}})
        self.assertEqual(report["configuration_origin"]["configured_defaults"]["max_agent_calls"], 8)
        self.assertEqual(report["configuration_origin"]["applied_defaults"], {})
        self.assertEqual(report["limits"]["max_agent_calls"], 20)

    def test_failed_report_is_persisted_before_rejecting_backend_start(self):
        report = self.build(limits={**self.limits, "max_agent_calls": 1})
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(RefinerError, "INSUFFICIENT_AGENT_CALLS"):
                emit_runtime_preflight(report, directory)
            saved = json.loads((Path(directory) / "runtime-preflight.json").read_text(encoding="utf-8"))
            validate_runtime_preflight(saved)
            self.assertEqual(saved, report)
            self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_report_schema_and_semantics_reject_invented_verification(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        report = self.build()
        validate_runtime_preflight(report)
        report["verification"]["inference"] = "passed"
        with self.assertRaises(jsonschema.ValidationError):
            validate_runtime_preflight(report)
        report = self.build()
        report["status"] = "failed"
        with self.assertRaises(jsonschema.ValidationError):
            validate_runtime_preflight(report)

    def test_invalid_numeric_facts_and_unsafe_report_path_are_rejected(self):
        for changes in ({"usage": {"agent_calls": True}}, {"executor_count": 0},
                        {"usage": {"elapsed_seconds": float("nan")}},
                        {"usage": []}, {"config": []}, {"config": {"executors": [None]}},
                        {"launch_context": []},
                        {"launch_context": {"applied_defaults": {"profile": {"secret": "x"}}}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(**changes)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                emit_runtime_preflight(self.build(), directory, "../outside.json")


if __name__ == "__main__":
    unittest.main()

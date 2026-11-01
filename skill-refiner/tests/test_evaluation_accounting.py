"""Offline budget and cache-proof checks; no model, network or workflow runs."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from refiner.common import RefinerError, digest, manifest, write_json  # noqa: E402
from refiner.pipeline import (  # noqa: E402
    _begin_evaluation_cost, _evaluation_accounting, _evaluation_identity,
    _finish_evaluation_cost, _pending_evaluation_cost, _persisted_evaluation_cost,
    _validated_evaluation_cache,
)
from refiner.preflight import build_runtime_preflight  # noqa: E402


class EvaluationAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.original = self.work / "original" / "demo"
        self.original.mkdir(parents=True)
        (self.original / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
        self.plan = {"cases": [{"id": f"D{i}"} for i in range(4)]}
        self.acceptance = {"cases": [{"id": f"A{i}"} for i in range(2)]}
        self.execution = {"model": "fixture-only"}
        self.checker, self.helper = "a" * 64, "b" * 64

    def identity(self, cases, target=None):
        return _evaluation_identity(target or self.original, cases, None, True,
                                    self.execution, self.checker, self.helper)

    def save_cache(self, suite, cases, *, cost=None, executors=("default",), statuses=None,
                   covered=(), target=None):
        suite = self.work / suite
        suite.mkdir(parents=True, exist_ok=True)
        rows = []
        for executor in executors:
            for case in cases:
                status = (statuses or {}).get(case["id"], "PASS")
                row = {"id": case["id"], "status": status, "passed": status == "PASS"}
                if executor != "default":
                    row.update(id=executor + "::" + case["id"], case_id=case["id"], executor=executor)
                rows.append(row)
        result = {"cases": rows, "passed": all(row["passed"] for row in rows)}
        write_json(suite / "summary.json", result)
        cache = {"schema_version": "skill-refiner.evaluation-cache/v1",
                 "cache_sha256": digest(self.identity(cases, target)), "result_sha256": digest(result),
                 "evaluations_used": len(rows) if cost is None else cost}
        if covered:
            cache["accounted_ledger_paths"] = list(covered)
        write_json(suite / "resume-cache.json", cache)
        return result, cache

    def pending(self, *, executors=None, resume=True, acceptance_target=None):
        return _pending_evaluation_cost(self.work, self.original, self.plan, self.acceptance,
                                        self.execution, self.checker, self.helper,
                                        executors or ["default"], resume=resume,
                                        validated_acceptance_target=acceptance_target)

    def test_failed_state_cost_is_preserved_without_a_success_cache(self):
        write_json(self.work / "state.json", {"target_evaluations": 4})
        self.assertEqual(_persisted_evaluation_cost(self.work), 4)
        self.assertEqual(_evaluation_accounting(self.work)["unresolved_attempts"], [])

    def test_old_cache_eight_plus_new_failure_four_is_twelve_on_every_resume(self):
        self.save_cache("baseline", self.plan["cases"], cost=8)
        attempt = self.work / "baseline/resume-attempts/attempt-001"
        path = _begin_evaluation_cost(self.work, self.work / "baseline", attempt, 4, 8)
        _finish_evaluation_cost(path, 4, status="error", error="isolated fixture failure")
        for _ in range(3):
            self.assertEqual(_evaluation_accounting(self.work)["total_cost"], 12)
        included = [path.relative_to(self.work).as_posix()]
        self.save_cache("baseline", self.plan["cases"], cost=12, covered=included)
        for _ in range(3):
            self.assertEqual(_evaluation_accounting(self.work)["total_cost"], 12)

    def test_running_reservation_is_conservative_and_not_terminal_evidence(self):
        write_json(self.work / "state.json", {"target_evaluations": 8})
        path = _begin_evaluation_cost(self.work, self.work / "baseline", self.work / "baseline", 4, 8)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["charged_count"], 4)
        self.assertIsNone(record["finished_at"])
        result = _evaluation_accounting(self.work)
        self.assertEqual(result["total_cost"], 12)
        self.assertEqual(result["unresolved_attempts"], ["baseline/evaluation-cost.json"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)

    def test_known_terminal_attempt_releases_only_unexecuted_reservation(self):
        write_json(self.work / "state.json", {"target_evaluations": 8})
        path = _begin_evaluation_cost(self.work, self.work / "baseline", self.work / "baseline", 4, 8)
        _finish_evaluation_cost(path, 2, status="error", error="two fixtures executed before failure")
        accounting = _evaluation_accounting(self.work)
        self.assertEqual(accounting["total_cost"], 10)
        self.assertEqual(accounting["unresolved_attempts"], [])
        with self.assertRaises(RefinerError):
            _finish_evaluation_cost(path, 0, status="completed")

    def test_target_and_original_named_budget_files_do_not_inflate_controller_cost(self):
        self.save_cache("baseline", self.plan["cases"], cost=8)
        for relative in ("original/demo/resume-cache.json", "original/demo/evaluation-cost.json",
                         "candidates/1/demo/baseline/resume-cache.json", "random/evaluation-cost.json"):
            path = self.work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not valid json", encoding="utf-8")
        self.assertEqual(_persisted_evaluation_cost(self.work), 8)

    def test_damaged_known_budget_evidence_fails_closed(self):
        path = self.work / "baseline/evaluation-cost.json"
        path.parent.mkdir()
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RefinerError):
            _evaluation_accounting(self.work)
        path.unlink()
        (path.parent / "resume-cache.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(RefinerError):
            _evaluation_accounting(self.work)
        (path.parent / "resume-cache.json").unlink()
        write_json(self.work / "state.json", {"target_evaluations": "4"})
        with self.assertRaises(RefinerError):
            _evaluation_accounting(self.work)

    def test_running_attempt_cannot_claim_a_released_reservation(self):
        path = _begin_evaluation_cost(self.work, self.work / "baseline", self.work / "baseline", 4, 0)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["charged_count"] = 0
        write_json(path, record)
        with self.assertRaises(RefinerError):
            _evaluation_accounting(self.work)

    def test_cache_cannot_deduplicate_missing_or_foreign_ledgers(self):
        self.save_cache("baseline", self.plan["cases"], cost=8,
                        covered=["original/demo/evaluation-cost.json"])
        with self.assertRaises(RefinerError):
            _evaluation_accounting(self.work)

    def test_no_cache_four_plus_two_plan_with_budget_five_is_rejected(self):
        pending = self.pending()
        self.assertEqual(pending, {"full_cost": 6, "reusable_cost": 0,
                                  "minimum_new_cost": 6, "validated_cache_paths": []})
        report = build_runtime_preflight(
            mode="refine", phase="before-evaluation", plan_only=False,
            validated_reused_steps=("plan", "development-plan-review", "acceptance-plan", "acceptance-plan-review"),
            limits={"max_seconds": 300, "max_agent_calls": 8, "max_evaluations": 5,
                    "max_cases": 4, "acceptance_cases": 2},
            planned_case_counts={"development": 4, "acceptance": 2}, applicable_scenario_count=2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["minimum_required"]["case_evaluations"], 6)

    def test_valid_baseline_cache_four_with_two_remaining_needs_only_acceptance(self):
        self.save_cache("baseline", self.plan["cases"])
        write_json(self.work / "state.json", {"target_evaluations": 4})
        pending = self.pending()
        self.assertEqual(pending["reusable_cost"], 4)
        self.assertEqual(pending["minimum_new_cost"], 2)
        report = build_runtime_preflight(
            mode="refine", phase="before-evaluation", plan_only=False,
            validated_reused_steps=("plan", "development-plan-review", "acceptance-plan", "acceptance-plan-review"),
            limits={"max_seconds": 300, "max_agent_calls": 8, "max_evaluations": 6,
                    "max_cases": 4, "acceptance_cases": 2},
            usage={"target_evaluations": _persisted_evaluation_cost(self.work)},
            validated_completed_evaluations=pending["reusable_cost"],
            planned_case_counts={"development": 4, "acceptance": 2}, applicable_scenario_count=2)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["minimum_required"]["case_evaluations"], 2)

    def test_real_consumption_alone_does_not_grant_cache_reuse(self):
        write_json(self.work / "state.json", {"target_evaluations": 4})
        self.assertEqual(_persisted_evaluation_cost(self.work), 4)
        self.assertEqual(self.pending()["reusable_cost"], 0)
        self.assertEqual(self.pending()["minimum_new_cost"], 6)

    def test_observed_business_failure_is_reused_without_repeating_its_evaluation(self):
        result, _ = self.save_cache("baseline", self.plan["cases"], statuses={"D1": "FAIL"})
        cached = _validated_evaluation_cache(self.work / "baseline", self.identity(self.plan["cases"]),
                                             [c["id"] for c in self.plan["cases"]], ["default"])
        self.assertEqual(cached, result)
        self.assertFalse(cached["passed"])
        self.assertEqual(self.pending()["reusable_cost"], 4)

    def test_cache_requires_complete_unique_case_executor_coverage(self):
        self.save_cache("baseline", self.plan["cases"], executors=("one", "two"))
        self.assertIsNotNone(_validated_evaluation_cache(
            self.work / "baseline", self.identity(self.plan["cases"]), [c["id"] for c in self.plan["cases"]], ["one", "two"]))
        result_path = self.work / "baseline/summary.json"
        cache_path = self.work / "baseline/resume-cache.json"
        original = json.loads(result_path.read_text(encoding="utf-8"))
        for mutation in (lambda rows: rows[:-1], lambda rows: rows + [rows[0]],
                         lambda rows: [{**rows[0], "status": "ERROR", "passed": False}, *rows[1:]],
                         lambda rows: [{**rows[0], "executor": "unknown"}, *rows[1:]]):
            result = {"cases": mutation(original["cases"]), "passed": False}
            write_json(result_path, result)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache["result_sha256"] = digest(result)
            write_json(cache_path, cache)
            self.assertIsNone(_validated_evaluation_cache(
                self.work / "baseline", self.identity(self.plan["cases"]),
                [c["id"] for c in self.plan["cases"]], ["one", "two"]))

    def test_cache_identity_and_result_hash_are_both_required(self):
        self.save_cache("baseline", self.plan["cases"])
        wrong = {**self.identity(self.plan["cases"]), "checker_sha256": "c" * 64}
        self.assertIsNone(_validated_evaluation_cache(
            self.work / "baseline", wrong, [c["id"] for c in self.plan["cases"]], ["default"]))
        result_path = self.work / "baseline/summary.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["seconds"] = 9
        write_json(result_path, result)
        with self.assertRaises(RefinerError):
            _validated_evaluation_cache(self.work / "baseline", self.identity(self.plan["cases"]),
                                        [c["id"] for c in self.plan["cases"]], ["default"])

    def test_valid_acceptance_target_and_cache_can_be_reused_without_using_state_as_proof(self):
        suite = "acceptance/attempts/initial/evaluation"
        self.save_cache(suite, self.acceptance["cases"])
        target_manifest = manifest(self.original)
        target = {"candidate": "original/demo", "candidate_manifest": target_manifest,
                  "candidate_sha256": digest(target_manifest), "suite_sha256": digest(self.acceptance["cases"])}
        write_json(self.work / "acceptance/attempts/initial/target.json", target)
        self.assertEqual(self.pending()["reusable_cost"], 0)
        pending = self.pending(acceptance_target=target)
        self.assertEqual(pending["reusable_cost"], 2)
        self.assertEqual(pending["minimum_new_cost"], 4)
        (self.original / "SKILL.md").write_text("changed", encoding="utf-8")
        self.assertEqual(self.pending(acceptance_target=target)["reusable_cost"], 0)

    def test_acceptance_cache_for_another_candidate_is_not_credited(self):
        suite = "acceptance/attempts/initial/evaluation"
        self.save_cache(suite, self.acceptance["cases"])
        target_manifest = manifest(self.original)
        target = {"candidate": "original/demo", "candidate_manifest": target_manifest,
                  "candidate_sha256": digest(target_manifest), "suite_sha256": digest(self.acceptance["cases"])}
        write_json(self.work / "acceptance/attempts/initial/target.json", target)
        intended = {**target, "candidate": "candidates/round-2/demo"}
        self.assertEqual(self.pending(acceptance_target=intended)["reusable_cost"], 0)

    def test_multi_executor_cost_and_ledger_schema(self):
        self.assertEqual(self.pending(executors=["one", "two"])["minimum_new_cost"], 12)
        schema_path = Path(__file__).resolve().parents[1] / "schemas/v1/evaluation-cost.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        path = _begin_evaluation_cost(self.work, self.work / "baseline", self.work / "baseline", 4, 0)
        record = _finish_evaluation_cost(path, 4, status="completed")
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(record)


if __name__ == "__main__":
    unittest.main()

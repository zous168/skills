"""Offline interruption recovery checks for a create parent and its child."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from refiner.cli import main  # noqa: E402
from refiner.common import RefinerError, read_json, write_json  # noqa: E402
from refiner.creation import (  # noqa: E402
    _apply_child_budget, _read_child_budget, _record_child_budget_entry,
    _reconcile_child_budget,
)
from refiner.pipeline import _begin_evaluation_cost, _finish_evaluation_cost  # noqa: E402


class ChildBudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "create"
        self.work.mkdir()
        self.parent = {"mode": "create", "status": "refining", "elapsed_seconds": 396.671,
                       "creation_agent_calls": 7, "agent_calls": 7, "target_evaluations": 0}
        self.empty = {"elapsed_seconds": 0.0, "agent_calls": 0, "target_evaluations": 0,
                      "settled": True, "unresolved": []}
        _record_child_budget_entry(self.work, self.parent, parent_elapsed=396.671,
                                   parent_calls=7, child_usage=self.empty)

    def calls(self, root, count, *, pending=False, future_finished=False, missing_finish=False):
        now = datetime.now(timezone.utc)
        for number in range(1, count + 1):
            directory = root / "agent-calls" / f"{number:03}-fixture"
            write_json(directory / "request-manifest.json", {"status": "submitted" if pending else "completed"})
            write_json(directory / "execution.json", {
                "status": "running" if pending and number == count else "completed", "seconds": 1,
                "finished_at": (None if missing_finish else
                                (now + timedelta(seconds=10) if future_finished else now).isoformat())})

    def child(self, *, elapsed=100, calls=4, evaluations=6, status="error", active=None,
              pending=False, future_finished=False, missing_finish=False):
        child = self.work / "refinement"
        self.calls(child, calls, pending=pending, future_finished=future_finished, missing_finish=missing_finish)
        write_json(child / "stages.json", {"status": status, "active_stage": active})
        write_json(child / "state.json", {"status": status, "elapsed_seconds": elapsed,
                                          "agent_calls": calls, "target_evaluations": evaluations})
        return child

    def test_inherited_346_seconds_and_four_calls_are_not_reset_or_double_counted(self):
        self.child()
        corrected, usage = _reconcile_child_budget(self.work, self.parent)
        self.assertAlmostEqual(corrected["elapsed_seconds"], 496.671)
        self.assertEqual(corrected["creation_agent_calls"], 7)  # inherited 4 plus parent work 3
        self.assertEqual(corrected["agent_calls"], 11)
        self.assertEqual(corrected["target_evaluations"], 6)
        for _ in range(3):
            corrected, repeated = _reconcile_child_budget(self.work, corrected)
            self.assertEqual(repeated, usage)
            self.assertAlmostEqual(corrected["elapsed_seconds"], 496.671)
            self.assertEqual(corrected["agent_calls"], 11)

    def test_parent_elapsed_already_including_child_time_is_not_charged_twice(self):
        self.child()
        self.parent["elapsed_seconds"] = 510.125
        corrected, _ = _reconcile_child_budget(self.work, self.parent)
        self.assertEqual(corrected["elapsed_seconds"], 510.125)

    def test_second_entry_only_adds_child_time_after_its_new_overlap_boundary(self):
        self.child()
        corrected, usage = _reconcile_child_budget(self.work, self.parent)
        _record_child_budget_entry(self.work, corrected, parent_elapsed=501.671,
                                   parent_calls=7, child_usage=usage)
        self.child(elapsed=130, calls=5, evaluations=8)
        corrected, _ = _reconcile_child_budget(self.work, corrected)
        self.assertAlmostEqual(corrected["elapsed_seconds"], 531.671)
        self.assertEqual(corrected["agent_calls"], 12)
        self.assertEqual(corrected["target_evaluations"], 8)

    def test_running_child_heartbeat_cannot_be_used_as_actual_total_time(self):
        self.child(status="planning", active="planning", pending=True)
        with self.assertRaisesRegex(RefinerError, "last heartbeat"):
            _reconcile_child_budget(self.work, self.parent)
        usage = _read_child_budget(self.work, require_settled=False)
        _apply_child_budget(self.work, self.parent, usage, parent_elapsed=450.0, parent_calls=7)
        self.assertFalse(usage["settled"])
        self.assertTrue(self.parent["child_budget"]["reconciliation_required"])
        self.assertEqual(self.parent["elapsed_seconds"], 450.0)
        self.assertEqual(self.parent["agent_calls"], 11)
        self.assertEqual(self.parent["target_evaluations"], 6)

    def test_terminal_pid_without_verified_end_timing_is_still_unresolved(self):
        self.child(missing_finish=True)
        with self.assertRaisesRegex(RefinerError, "last heartbeat"):
            _reconcile_child_budget(self.work, self.parent)

    def test_elapsed_state_predating_last_work_requires_external_time_reconciliation(self):
        self.child(future_finished=True)
        with self.assertRaisesRegex(RefinerError, "predates"):
            _reconcile_child_budget(self.work, self.parent)

    def test_running_evaluation_reservation_retains_cost_and_blocks_restart(self):
        child = self.child(evaluations=2)
        ledger = _begin_evaluation_cost(child, child / "baseline", child / "baseline", 4, 2)
        usage = _read_child_budget(self.work, require_settled=False)
        self.assertEqual(usage["target_evaluations"], 6)
        self.assertFalse(usage["settled"])
        with self.assertRaisesRegex(RefinerError, "reservation"):
            _reconcile_child_budget(self.work, self.parent)
        self.assertEqual(read_json(ledger)["status"], "running")

    def test_failed_child_ledger_cost_exceeds_state_but_is_retained(self):
        child = self.child(evaluations=2)
        ledger = _begin_evaluation_cost(child, child / "baseline", child / "baseline", 4, 2)
        _finish_evaluation_cost(ledger, 4, status="error", error="fixture")
        state = read_json(child / "state.json")
        write_json(child / "state.json", state)  # trusted terminal write after ledger finalization
        corrected, _ = _reconcile_child_budget(self.work, self.parent)
        self.assertEqual(corrected["target_evaluations"], 6)

    def test_missing_state_with_evaluation_evidence_cannot_be_zero_consumption(self):
        child = self.work / "refinement"
        _begin_evaluation_cost(child, child / "baseline", child / "baseline", 4, 0)
        with self.assertRaisesRegex(RefinerError, "without a budget state"):
            _read_child_budget(self.work)

    def test_child_consumption_without_parent_overlap_anchor_is_rejected(self):
        self.child()
        self.parent.pop("child_budget")
        with self.assertRaisesRegex(RefinerError, "no parent/child overlap"):
            _reconcile_child_budget(self.work, self.parent)

    def test_decreased_child_time_calls_or_evaluations_do_not_refund_budget(self):
        self.child()
        corrected, _ = _reconcile_child_budget(self.work, self.parent)
        for field, value in (("elapsed_seconds", 99), ("target_evaluations", 5)):
            with self.subTest(field=field):
                changed = read_json(self.work / "refinement/state.json")
                changed[field] = value
                write_json(self.work / "refinement/state.json", changed)
                with self.assertRaises(RefinerError):
                    _reconcile_child_budget(self.work, corrected)
                self.child()

    def test_retained_call_records_prevent_refund_when_state_counter_lags(self):
        child = self.child()
        corrected, _ = _reconcile_child_budget(self.work, self.parent)
        saved = read_json(child / "state.json")
        saved["agent_calls"] = 3
        write_json(child / "state.json", saved)
        repeated, usage = _reconcile_child_budget(self.work, corrected)
        self.assertEqual(usage["agent_calls"], 4)
        self.assertEqual(repeated["agent_calls"], 11)

    def _run_child_exception_fixture(self, *, return_unsettled=False):
        import test_plan_contract as fixtures
        analysis = fixtures.CreationAnalysisConclusionTests()._analysis()
        review = {
            "schema_version": "skill-refiner.analysis-review/v2", "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                "acceptance_criteria_atomic": True, "criteria_observable": True, "basis": "Matches source."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": True,
                "risk_coverage_complete": True, "traceability_complete": True, "basis": "Observable fixture."}
                for row in analysis["scenarios"]],
            "context_scope_assessment": {"intent_grounded": True, "actors_complete": True,
                "scope_clear": True, "constraints_complete": True, "basis": "Explicit source."},
            "workflow_assessments": [{"workflow_id": "W1", "order_and_actors_clear": True,
                "inputs_outputs_complete": True, "completion_and_failure_testable": True,
                "requirement_traceable": True, "basis": "Observable step."}],
            "risk_assessments": [{"dimension": row["dimension"], "applicability_justified": True,
                "scenario_links_complete": True, "basis": "Matches scope."} for row in analysis["risk_assessments"]],
            "dependencies_assessed": True, "blocking_decisions_actionable": True,
        }
        draft = {"files": [{"path": "SKILL.md", "content": "---\nname: " + analysis["name"]
                            + "\ndescription: Offline test skill.\n---\n# Fixture\nFollow the request.\n",
                            "reason": "Fixture implementation.", "requirement_ids": ["R1"]}]}
        root = self.work.parent
        config = root / "backend.json"
        profile = {"backend": "command", "command": [sys.executable, "-c", "raise SystemExit(99)"]}
        write_json(config, {"schema_version": 1, "agents": {"default": profile},
                            "executors": [{"id": "offline", **profile}]})
        spec = root / "request.md"
        spec.write_text("Return a useful result with the requested fields.", encoding="utf-8")
        output = root / "fresh-create"

        def fail_child(args):
            child = Path(args.output)
            self.calls(child, 2, pending=return_unsettled)
            ledger = _begin_evaluation_cost(child, child / "baseline", child / "baseline", 4, 0)
            _finish_evaluation_cost(ledger, 4, status="error", error="offline fixture interruption")
            status = "passed" if return_unsettled else "error"
            write_json(child / "stages.json", {"status": status, "active_stage": None})
            write_json(child / "state.json", {"status": status, "elapsed_seconds": 10,
                                              "agent_calls": 2, "target_evaluations": 4})
            if return_unsettled:
                return {"status": "passed", "agent_calls": 2, "target_evaluations": 4}
            raise RefinerError("offline child exception")

        with patch("refiner.creation.Agent.ask", side_effect=[analysis, review, draft]), \
                patch("refiner.creation.lint", return_value=[]), \
                patch("refiner.creation.refine", side_effect=fail_child) as nested, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["create", "--spec", str(spec), "--output", str(output), "--config", str(config)])
        self.assertEqual(code, 2)
        saved = read_json(output / "state.json")
        self.assertEqual(nested.call_count, 1, saved.get("error"))
        self.assertEqual(saved["agent_calls"], 2)
        self.assertEqual(saved["target_evaluations"], 4)
        return saved, output

    def test_child_writes_consumption_then_raises_without_returning_a_result(self):
        saved, _ = self._run_child_exception_fixture()
        self.assertGreaterEqual(saved["elapsed_seconds"], 10)

    def test_child_passed_response_with_unsettled_process_cannot_advance_to_delivery(self):
        saved, output = self._run_child_exception_fixture(return_unsettled=True)
        self.assertIn("verified termination", saved["error"])
        self.assertTrue(saved["child_budget"]["reconciliation_required"])
        self.assertFalse((output / "final").exists())

    def test_parent_recovery_guard_runs_before_any_parent_or_nested_backend(self):
        config = self.work.parent / "backend.json"
        profile = {"backend": "command", "command": [sys.executable, "-c", "raise SystemExit(99)"]}
        write_json(config, {"schema_version": 1, "agents": {"default": profile},
                            "executors": [{"id": "offline", **profile}]})
        write_json(self.work / "inputs.json", {"business_description": "", "requested_name": None,
                                               "requirements": [], "standards": []})
        write_json(self.work / "creation-resume-contract.json", {"destination": None})
        write_json(self.work / "state.json", {"mode": "create", "status": "error", "elapsed_seconds": 346.671,
                                              "agent_calls": 5, "creation_agent_calls": 5, "target_evaluations": 0})
        self.calls(self.work, 5, pending=True)
        errors = StringIO()
        with patch("refiner.creation.Agent.ask") as ask, patch("refiner.creation.refine") as child, \
                redirect_stdout(StringIO()), redirect_stderr(errors):
            code = main(["create", "--resume-run", str(self.work), "--config", str(config)])
        self.assertEqual(code, 2)
        self.assertIn("no reconciled terminal evidence", errors.getvalue())
        ask.assert_not_called()
        child.assert_not_called()


if __name__ == "__main__":
    unittest.main()

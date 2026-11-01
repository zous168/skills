"""Offline refinement plan recovery; no real model or target evaluation."""
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from refiner.common import RefinerError, digest, manifest, read_json, snapshot, write_json
from refiner.refine_plan_recovery import migrate, verify_recovery, _controller_hash
from refiner.stages import StageManifest
from refiner.pipeline import make_plan


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RefinePlanRecoveryTests(unittest.TestCase):
    def setUp(self):
        diagnostic = [{"path": "/fixture", "schema_path": "/required", "constraint": "required",
                       "expected": "fixture", "actual": None, "message": "fixture error"}]
        diagnostics = patch("refiner.agent._plan_schema_errors", return_value=diagnostic)
        diagnostics.start()
        self.addCleanup(diagnostics.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("# 示例技能\n", encoding="utf-8")
        (self.target / "docs").mkdir()
        (self.target / "docs" / "evaluation-cost.json").write_text("{}", encoding="utf-8")
        self.archive = self.root / "archive"
        # The archived controller fingerprint and verifier use Python modules;
        # bundled executables are unrelated to this migration fixture. Copying
        # them can leave a Windows executable transiently locked during cleanup.
        def ignore_non_python(directory, names):
            return {name for name in names if name == "__pycache__" or
                    (not (Path(directory) / name).is_dir() and Path(name).suffix.lower() != ".py")}
        shutil.copytree(ROOT / "scripts", self.archive / "scripts", ignore=ignore_non_python)
        shutil.copytree(ROOT / "schemas", self.archive / "schemas")
        marker = self.archive / "scripts" / "refiner" / "runtime.py"
        marker.write_bytes(marker.read_bytes() + b"\n# archived controller fixture\n")
        self.context = {"files": [{"path": "SKILL.md", "content": "# 示例技能\n"}],
                        "requirements": [{"source": "fixture", "content": "按工单分类"}],
                        "standards": ["fixture"]}
        self.old = self.root / "old"
        self.ancestor = self.root / "ancestor"
        self._make_run(self.ancestor, count=1, status="planning_error", elapsed=10.62, ancestor=True)
        self._make_run(self.old, count=2, status="plan_not_passed", elapsed=320.8)
        self.out = self.root / "new"

    def _make_run(self, root, count, status, elapsed, ancestor=False):
        root.mkdir()
        snapshot(self.target, root / "original" / self.target.name)
        write_json(root / "inputs.json", self.context)
        stage = StageManifest(root, "refine")
        stage.complete("inputs")
        stage.activate("planning")
        previous = None
        for number in range(1, count + 1):
            path = root / "agent-calls" / f"{number:03}-plan"
            path.mkdir(parents=True)
            payload = {**self.context, "validation_feedback": []}
            if previous is not None:
                payload["previous_invalid_response"] = previous[:12000]
            request = {"schema_version": "skill-refiner.agent-request/v1", "role": "plan",
                       "instruction": "fixture", "input": payload, "response_schema": {}}
            request_bytes = json.dumps(request, ensure_ascii=False).encode("utf-8")
            transmission = b"synthetic fixture"
            write_json(path / "request.json", request)
            (path / "transmission.txt").write_bytes(transmission)
            raw = json.dumps({"requirements": [{"id": "R1"}], "cases": [{"id": "c1"}]}, ensure_ascii=False)
            meta = {"role": "plan", "status": "execution_error" if ancestor else "invalid_response",
                    "request_sha256": sha(request_bytes), "request_bytes": len(request_bytes),
                    "transmitted_sha256": sha(transmission), "transmitted_bytes": len(transmission),
                    "response_sha256": sha(raw.encode("utf-8"))}
            write_json(path / "request-manifest.json", meta)
            if not ancestor:
                (path / "raw-response.txt").write_text(raw, encoding="utf-8")
                write_json(path / "execution.json", {"role": "plan", "status": "invalid_response", "finished_at": "fixture"})
                write_json(path / "process.execution.json", {"status": "completed", "exit_code": 0, "finished_at": "fixture"})
                for file in ("process.log", "process.stdout.log", "process.stderr.log"):
                    (path / file).write_text("fixture", encoding="utf-8")
            previous = raw
        stage.finish(status)
        contract = {"schema_version": "skill-refiner.resume/v1", "source": str(self.target),
                    "source_manifest": manifest(self.target), "inputs_sha256": digest(self.context),
                    "controller_sha256": _controller_hash(self.archive), "execution_sha256": "a" * 64,
                    "options": {"max_rounds": 2, "planning_attempts": 2, "max_cases": 12, "acceptance_cases": 12},
                    "limits": {"max_seconds": 3589, "max_agent_calls": 31, "max_evaluations": 80,
                               "max_rounds": 2, "planning_attempts": 2, "max_cases": 12, "acceptance_cases": 12}}
        write_json(root / "resume-contract.json", contract)
        write_json(root / "state.json", {"status": status, "source": str(self.target), "workspace": str(root),
                                         "agent_calls": count, "elapsed_seconds": elapsed, "target_evaluations": 0,
                                         "active_agent_calls": {}, "rounds": []})

    def migrate(self):
        return migrate(self.old, self.archive, self.ancestor, self.out)

    def test_preserves_old_records_usage_and_complete_feedback(self):
        before = {p.relative_to(self.old).as_posix(): sha(p.read_bytes()) for p in self.old.rglob("*") if p.is_file()}
        result = self.migrate()
        self.assertEqual(before, {p.relative_to(self.old).as_posix(): sha(p.read_bytes()) for p in self.old.rglob("*") if p.is_file()})
        self.assertEqual(result["inherited"], {"agent_calls": 2, "elapsed_seconds": 320.8, "target_evaluations": 0})
        self.assertEqual(result["ancestor"]["agent_calls"], 1)
        contract = read_json(self.out / "resume-contract.json")
        recovery = verify_recovery(self.out, contract)
        (self.out / "report.md").write_text("new report for resumed run", encoding="utf-8")
        (self.out / "runtime-preflight-before-planning.json").write_text("{}", encoding="utf-8")
        verify_recovery(self.out, contract)
        self.assertEqual(recovery["previous_invalid_response"], (self.old / "agent-calls/002-plan/raw-response.txt").read_text(encoding="utf-8"))
        self.assertEqual(recovery["previous_plan"]["cases"][0]["id"], "c1")
        self.assertGreater(len(recovery["contract_errors"]), 0)
        self.assertEqual(read_json(self.out / "stages.json")["stages"][0]["status"], "completed")
        self.assertNotEqual(read_json(self.out / "stages.json")["stages"][1]["status"], "completed")
        seen = []
        class FakeAgent:
            def ask(self, role, instruction, payload, schema):
                seen.append((role, payload))
                raise RefinerError("offline stop")
        with self.assertRaisesRegex(RefinerError, "offline stop"):
            make_plan(FakeAgent(), self.context, self.out, 12, recovery=recovery)
        self.assertEqual(seen[0][1]["previous_invalid_response"], recovery["previous_invalid_response"])
        self.assertEqual(seen[0][1]["contract_errors"], recovery["contract_errors"])
        self.assertEqual(seen[0][1]["previous_plan"]["cases"][0]["id"], "c1")

    def test_rejects_edited_response_and_unexpected_attachment(self):
        raw = self.old / "agent-calls/002-plan/raw-response.txt"
        prior = raw.read_bytes()
        raw.write_bytes(prior + b" ")
        with self.assertRaises(RefinerError):
            self.migrate()
        self.assertFalse(self.out.exists())
        raw.write_bytes(prior)
        (raw.parent / "unrecognized.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(RefinerError, "Unknown unauthenticated"):
            self.migrate()
        self.assertFalse(self.out.exists())

    def test_rejects_contract_and_source_evidence_tampering(self):
        self.migrate()
        contract = read_json(self.out / "resume-contract.json")
        changed = copy.deepcopy(contract)
        changed["limits"]["max_agent_calls"] += 1
        with self.assertRaises(RefinerError):
            verify_recovery(self.out, changed)
        raw = self.out / "agent-calls/002-plan/raw-response.txt"
        raw.write_text("{}", encoding="utf-8")
        with self.assertRaises(RefinerError):
            verify_recovery(self.out, contract)

    def test_rejects_changed_inputs_and_old_controller(self):
        inputs = self.old / "inputs.json"
        contents = inputs.read_bytes()
        inputs.write_text("{}", encoding="utf-8")
        with self.assertRaises(RefinerError):
            self.migrate()
        inputs.write_bytes(contents)
        (self.archive / "scripts/refiner/runtime.py").write_text("changed", encoding="utf-8")
        with self.assertRaises(RefinerError):
            self.migrate()

    def test_unexpected_hidden_evaluation_directory_is_rejected(self):
        hidden = self.old / "baseline" / "unrecorded.txt"
        hidden.parent.mkdir()
        hidden.write_text("unreported work", encoding="utf-8")
        with self.assertRaisesRegex(RefinerError, "Historical target evaluation directory"):
            self.migrate()
        self.assertFalse(self.out.exists())

    def test_recovered_invalid_draft_can_be_fixed_but_cases_cannot_disappear(self):
        self.migrate()
        recovery = verify_recovery(self.out, read_json(self.out / "resume-contract.json"))
        result = {"requirements": [{"id": "R1", "behavior": "fixed"}],
                  "cases": [{"id": "c1", "kind": "normal", "title": "fixed"}],
                  "execution_plan": {"steps": ["fixed"]}}
        class FakeAgent:
            def ask(self, role, *args):
                if role == "plan":
                    return copy.deepcopy(result)
                raise RefinerError("independent review reached")
        with patch("refiner.pipeline.validate_plan", return_value={"case_control_results": []}):
            with self.assertRaisesRegex(Exception, "independent review reached"):
                make_plan(FakeAgent(), self.context, self.out, 12, max_attempts=1, recovery=recovery)
        # A replacement case cannot silently erase the recovered case ID.
        result["cases"][0]["id"] = "other"
        with patch("refiner.pipeline.validate_plan", return_value={"case_control_results": []}):
            with self.assertRaisesRegex(Exception, "dropped_recovered_matrix_entries"):
                make_plan(FakeAgent(), self.context, self.out, 12, max_attempts=1, recovery=recovery)
        self.assertEqual(read_json(self.out / "plan-controls/development.json")["status"], "failed")

    def test_rejects_archive_linked_python_and_directory(self):
        outside = self.root / "outside.py"
        outside.write_text("print('outside')", encoding="utf-8")
        linked = self.archive / "scripts" / "linked.py"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest("Symlink creation unavailable: " + str(exc))
        try:
            with self.assertRaisesRegex(RefinerError, "linked file"):
                self.migrate()
        finally:
            linked.unlink()
        directory = self.root / "outside-dir"
        directory.mkdir()
        linked_dir = self.archive / "scripts" / "linked-dir"
        try:
            linked_dir.symlink_to(directory, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Directory symlink creation unavailable: " + str(exc))
        try:
            with self.assertRaisesRegex(RefinerError, "linked directory"):
                self.migrate()
        finally:
            linked_dir.unlink()
        self.assertFalse(self.out.exists())

    def test_rejects_linked_archive_root(self):
        linked = self.root / "archive-link"
        try:
            linked.symlink_to(self.archive, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Directory symlink creation unavailable: " + str(exc))
        try:
            with self.assertRaisesRegex(RefinerError, "linked or reparse component"):
                migrate(self.old, linked, self.ancestor, self.out)
        finally:
            linked.unlink()
        self.assertFalse(self.out.exists())

    def test_nonempty_old_or_ancestor_evaluation_history_is_rejected(self):
        for directory in (self.old, self.ancestor):
            path = directory / "state.json"
            original = path.read_bytes()
            try:
                state = read_json(path)
                state["target_evaluations"] = 0
                state["evaluation_history"] = [{"suite": "baseline", "result": "failed"}]
                write_json(path, state)
                with self.assertRaisesRegex(RefinerError, "evaluation evidence"):
                    self.migrate()
                self.assertFalse(self.out.exists())
            finally:
                path.write_bytes(original)

    def test_added_schema_registry_version_is_explicitly_migrated(self):
        archive_registry = self.archive / "scripts/refiner/artifact_validation.py"
        text = archive_registry.read_text(encoding="utf-8")
        line = '    "skill-refiner.plan-v7": ("v7", "schemas/v7/plan.schema.json", "json-schema"),\n'
        if line not in text:
            self.skipTest("v7 schema is not registered in this source tree")
        archive_registry.write_text(text.replace(line, ""), encoding="utf-8")
        stage_path = self.old / "stages.json"
        stage = read_json(stage_path)
        stage["schema_refs"] = [r for r in stage["schema_refs"] if r["id"] != "skill-refiner.plan-v7"]
        write_json(stage_path, stage)
        contract_path = self.old / "resume-contract.json"
        contract = read_json(contract_path)
        contract["controller_sha256"] = _controller_hash(self.archive)
        write_json(contract_path, contract)
        self.migrate()
        old_refs = read_json(self.out / "recovery-source-stages.json")["schema_refs"]
        new_refs = read_json(self.out / "stages.json")["schema_refs"]
        self.assertNotIn("skill-refiner.plan-v7", [r["id"] for r in old_refs])
        self.assertIn("skill-refiner.plan-v7", [r["id"] for r in new_refs])
        verify_recovery(self.out, read_json(self.out / "resume-contract.json"))

    def test_suite_or_phase_history_started_cannot_be_reset(self):
        for directory in (self.old, self.ancestor):
            path = directory / "state.json"
            original = path.read_bytes()
            try:
                state = read_json(path)
                state["phase_results"] = {"skill_evaluation": {"status": "not_started", "suites": {"baseline": "passed"}}}
                write_json(path, state)
                with self.assertRaisesRegex(RefinerError, "phase or suite has started"):
                    self.migrate()
                state["phase_results"]["skill_evaluation"]["suites"]["baseline"] = "not_started"
                state["phase_history"] = [{"phase": "skill_evaluation", "status": "running"}]
                write_json(path, state)
                with self.assertRaisesRegex(RefinerError, "phase has started"):
                    self.migrate()
                state["phase_history"][0]["status"] = "skipped"
                write_json(path, state)
                migrate(self.old, self.archive, self.ancestor, self.root / ("skipped-" + directory.name))
            finally:
                path.write_bytes(original)

    def test_repeated_migration_is_rejected_without_reset(self):
        self.migrate()
        with self.assertRaisesRegex(RefinerError, "Repeated refinement migration"):
            migrate(self.out, ROOT, self.ancestor, self.root / "twice")
        self.assertFalse((self.root / "twice").exists())
        self.assertEqual(read_json(self.out / "state.json")["agent_calls"], 2)


if __name__ == "__main__":
    unittest.main()

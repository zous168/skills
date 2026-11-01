"""Offline process and CLI boundary checks; no model/backend requests are made."""
import argparse
import hashlib
import copy
import importlib.util
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refiner.agent import Agent
from refiner.artifact_validation import schema_ref, validate_artifact
from refiner.cli import main, workflow_options
from refiner.common import RefinerError, kill_process_tree, read_json, run_process, write_json
from refiner.runtime import run_cli
from refiner.diagnostics import inspect_config
from refiner.preflight import build_runtime_preflight, emit_runtime_preflight
from refiner.stages import StageManifest


def wait_until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("The controlled process did not produce the expected observable evidence")


class ProcessEvidenceTests(unittest.TestCase):
    def test_text_api_normalizes_crlf_while_raw_sidecar_preserves_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []
            code, stdout, stderr, _ = run_process(
                [sys.executable, "-c", "import os; os.write(1, b'first\\r\\nsecond\\r\\n')"],
                timeout=3, log=root / "process.log",
                on_output=lambda stream, chunk: events.append((stream, chunk)))
            self.assertEqual((code, stdout, stderr), (0, "first\nsecond\n", ""))
            self.assertEqual("".join(x[1] for x in events if x[0] == "stdout"), stdout)
            self.assertEqual((root / "process.stdout.log").read_bytes(), b"first\r\nsecond\r\n")

    def test_partial_stdout_and_stderr_are_readable_before_exit_and_schema_valid_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate = root / "finish"
            log = root / "process.log"
            result, errors = [], []
            script = ("import sys,time,pathlib; sys.stdout.write('中文输出'); sys.stdout.flush(); "
                      "sys.stderr.write('错误流诊断'); sys.stderr.flush(); "
                      f"gate=pathlib.Path({str(gate)!r}); "
                      "\nwhile not gate.exists(): time.sleep(.01)")

            def invoke():
                try:
                    result.append(run_process([sys.executable, "-X", "utf8", "-c", script],
                                              timeout=6, log=log))
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            try:
                wait_until(lambda: (root / "process.stdout.log").exists()
                           and (root / "process.stdout.log").read_text(encoding="utf-8") == "中文输出"
                           and (root / "process.stderr.log").read_text(encoding="utf-8") == "错误流诊断")
                running = read_json(root / "process.execution.json")
                self.assertEqual(running["status"], "running")
                self.assertGreater(running["pid"], 0)
                self.assertIsNone(running["exit_code"])
                self.assertIsNone(running["finished_at"])
                self.assertTrue(thread.is_alive())
            finally:
                gate.write_text("finish")
                thread.join(8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result[0][:3], (0, "中文输出", "错误流诊断"))
            record_path = root / "process.execution.json"
            record = read_json(record_path)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["stdout_bytes"], len("中文输出".encode()))
            self.assertEqual(record["stderr_bytes"], len("错误流诊断".encode()))
            data = record_path.read_bytes()
            ref = schema_ref("skill-refiner.process-execution")
            artifact = {"path": record_path.name, "media_type": "application/json",
                        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                        "schema_ref": ref, "validation": {"validator_id": ref["validator_id"]}}
            validate_artifact(record_path, root, artifact)
            self.assertEqual(artifact["validation"]["status"], "passed")

    def test_external_harness_termination_preserves_already_emitted_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "harness.py"
            child = "import sys,time; print('已落盘', flush=True); time.sleep(30)"
            script.write_text("import sys\nfrom pathlib import Path\n"
                              f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                              "from refiner.common import run_process\n"
                              f"run_process([sys.executable, '-X', 'utf8', '-c', {child!r}], "
                              f"timeout=25, log=Path({str(root / 'process.log')!r}))\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-X", "utf8", str(script)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_until(lambda: (root / "process.stdout.log").exists()
                           and "已落盘" in (root / "process.stdout.log").read_text(encoding="utf-8"))
            finally:
                kill_process_tree(proc.pid)
                proc.wait(timeout=6)
            self.assertIn("已落盘", (root / "process.stdout.log").read_text(encoding="utf-8"))
            self.assertIn("已落盘", (root / "process.log").read_text(encoding="utf-8"))
            # An abruptly interrupted writer cannot claim a terminal result.
            record = read_json(root / "process.execution.json")
            self.assertEqual(record["status"], "running")
            self.assertIsNone(record["finished_at"])

    def test_timeout_keeps_outputs_and_records_a_terminal_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RefinerError, "timed out"):
                run_process([sys.executable, "-X", "utf8", "-c",
                             "import sys,time; print('started',flush=True); "
                             "print('backend diagnostic',file=sys.stderr,flush=True); time.sleep(5)"],
                            timeout=0.5, log=root / "process.log")
            record = read_json(root / "process.execution.json")
            self.assertEqual(record["status"], "timed_out")
            self.assertIsNotNone(record["finished_at"])
            self.assertIn("started", (root / "process.stdout.log").read_text())
            self.assertIn("backend diagnostic", (root / "process.stderr.log").read_text())

    def test_failed_launch_is_recorded_and_credential_arguments_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(OSError):
                run_process([str(root / "missing-program.exe"), "--api-key", "test-credential"],
                            timeout=1, log=root / "process.log")
            record = read_json(root / "process.execution.json")
            self.assertEqual(record["status"], "launch_error")
            self.assertEqual(record["argv"][-1], "[REDACTED]")
            self.assertNotIn("test-credential", json.dumps(record))


class AgentExecutionEvidenceTests(unittest.TestCase):
    def test_call_error_does_not_override_an_unconfirmed_child_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "agent-calls/001-analyze"
            old.mkdir(parents=True)
            write_json(old / "request-manifest.json", {"status": "execution_error"})
            write_json(old / "execution.json", {"status": "execution_error"})
            write_json(old / "process.execution.json", {"status": "termination_unconfirmed", "pid": 12345})
            agent = Agent(root, config={"agents": {"planning": {"backend": "codex"}}})
            with patch("refiner.runtime.execute") as backend, redirect_stdout(StringIO()), \
                    self.assertRaisesRegex(RefinerError, "no reconciled terminal evidence"):
                agent.ask("plan", "Return JSON.", {}, {"type": "object"})
            backend.assert_not_called()
            self.assertEqual(agent.calls, 1)

    def test_unsettled_prior_process_is_not_silently_restarted_from_its_running_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "agent-calls/001-analyze"
            old.mkdir(parents=True)
            write_json(old / "request-manifest.json", {"status": "submitted"})
            write_json(old / "execution.json", {"status": "running", "timeout_seconds": 120})
            before = (old / "execution.json").read_bytes()
            agent = Agent(root, config={"agents": {"planning": {"backend": "codex"}}})
            with patch("refiner.runtime.execute") as backend, redirect_stdout(StringIO()), \
                    self.assertRaisesRegex(RefinerError, "no reconciled terminal evidence"):
                agent.ask("plan", "Return JSON.", {}, {"type": "object"})
            backend.assert_not_called()
            self.assertEqual(agent.calls, 1)
            self.assertEqual((old / "execution.json").read_bytes(), before)
            self.assertEqual(len(list((root / "agent-calls").iterdir())), 1)

    def test_call_count_and_running_evidence_are_persisted_before_backend_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = Agent(root, config={"agents": {"planning": {"backend": "codex", "timeout": 2}}})
            state = {"mode": "create", "status": "analyzing"}
            agent.bind_state(state, elapsed=lambda: 1.25)

            def backend(*args, **kwargs):
                observed = read_json(root / "state.json")
                self.assertEqual(observed["agent_calls"], 1)
                self.assertEqual(observed["creation_agent_calls"], 1)
                self.assertEqual(observed["active_agent_calls"]["1"]["role"], "plan")
                self.assertEqual(read_json(root / "agent-calls/001-plan/execution.json")["status"], "running")
                return {"final_message": '{"ok":true}'}

            schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
            with patch("refiner.runtime.execute", side_effect=backend), redirect_stdout(StringIO()):
                self.assertEqual(agent.ask("plan", "Return the requested record.", {}, schema), {"ok": True})
            final = read_json(root / "state.json")
            self.assertEqual(final["active_agent_calls"], {})
            self.assertEqual(final["last_agent_call"]["status"], "completed")
            self.assertEqual(read_json(root / "agent-calls/001-plan/execution.json")["status"], "completed")

    def test_stderr_diagnostics_are_forwarded_as_live_events(self):
        def process(*args, **kwargs):
            callback = kwargs["on_output"]
            callback("stderr", "model refresh failed\n")
            final = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
            return 0, json.dumps(final) + "\n" + json.dumps({"type": "turn.completed"}) + "\n", "model refresh failed\n", 0.1

        with tempfile.TemporaryDirectory() as tmp, patch("refiner.runtime.run_process", side_effect=process):
            output = StringIO()
            with redirect_stdout(output):
                result = run_cli({"backend": "codex", "executable": sys.executable},
                                 "fixture", tmp, 2, planning=True, progress_label="analyze")
            event = json.loads(output.getvalue().strip())
            self.assertEqual(event["stream"], "stderr")
            self.assertEqual(event["event_type"], "diagnostic")
            self.assertIn("model refresh failed", event["text"])
            self.assertEqual(result["final_message"], "done")


class RuntimeCheckpointDurabilityTests(unittest.TestCase):
    def test_new_preflight_attempt_does_not_overwrite_prior_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = {"max_seconds": 30, "max_agent_calls": 8, "max_evaluations": 12,
                      "max_cases": 4, "acceptance_cases": 2}
            first = build_runtime_preflight(mode="create", phase="before-analysis", limits=limits, plan_only=True)
            second = build_runtime_preflight(mode="create", phase="before-analysis", limits=limits,
                                             usage={"elapsed_seconds": 1}, plan_only=True)
            with redirect_stdout(StringIO()):
                p1 = emit_runtime_preflight(first, tmp)
                old_bytes, old_mtime = p1.read_bytes(), p1.stat().st_mtime_ns
                p2 = emit_runtime_preflight(second, tmp)
            self.assertNotEqual(p1, p2)
            self.assertEqual(p1.read_bytes(), old_bytes)
            self.assertEqual(p1.stat().st_mtime_ns, old_mtime)
            self.assertEqual(read_json(p2)["usage"]["elapsed_seconds"], 1)

    def test_completed_stage_is_reused_without_rebinding_changed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "inputs.json", {"request": "fixture"})
            (root / "inputs").mkdir()
            (root / "inputs/request.md").write_text("输入", encoding="utf-8")
            stages = StageManifest(root, "create")
            stages.complete("inputs")
            original = (root / "stages.json").read_bytes()
            stages.complete("inputs")
            self.assertEqual((root / "stages.json").read_bytes(), original)
            (root / "inputs/request.md").write_text("被修改的输入", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed"):
                stages.complete("inputs")
            self.assertEqual((root / "stages.json").read_bytes(), original)


class WorkflowPreflightIntegrationTests(unittest.TestCase):
    def test_doctor_reports_installed_limits_without_claiming_effective_or_live_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "backend.json"
            profile = {"backend": "codex", "executable": sys.executable}
            write_json(cfg, {"schema_version": 1, "agents": {"default": profile},
                             "executors": [{"id": "offline", **profile}]})
            context = {"launch_manifest_path": str(Path(tmp) / "runtime.json"),
                       "configured_defaults": {"max_seconds": 300, "max_agent_calls": 8},
                       "applied_defaults": {}, "explicit_flags": []}
            with patch("refiner.diagnostics.run_process", return_value=(0, "fixture version/login", "", 0.01)):
                report = inspect_config(cfg, launch_context=context)
            self.assertTrue(report["ready"])
            self.assertEqual(report["configuration_origin"]["configured_defaults"]["max_seconds"], 300)
            self.assertEqual(report["configuration_origin"]["applied_defaults"], {})
            for row in report["profiles"]:
                self.assertEqual(row["verification"], "not_live_tested")
                self.assertEqual(row["effective_configuration"], "not_resolved")
                self.assertIn("reasoning_effort", row["unresolved_inherited_fields"])

    def test_create_rejects_insufficient_call_budget_without_starting_an_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "backend-called"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            profile = {"backend": "command", "command": command}
            cfg = root / "backends.json"
            write_json(cfg, {"schema_version": 1, "agents": {"default": profile},
                             "executors": [{"id": "offline", **profile}]})
            spec = root / "request.md"
            spec.write_text("创建一个输出中文结果的示例 Skill。", encoding="utf-8")
            run = root / "run"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["create", "--spec", str(spec), "--output", str(run),
                             "--config", str(cfg), "--plan-only", "--max-agent-calls", "6"])
            self.assertEqual(code, 2)
            self.assertFalse(marker.exists())
            self.assertFalse((run / "agent-calls").exists())
            report = read_json(run / "runtime-preflight-before-analysis.json")
            issue = next(x for x in report["issues"] if x["code"] == "INSUFFICIENT_AGENT_CALLS")
            self.assertEqual((issue["expected"], issue["actual"]), (7, 6))
            self.assertEqual(read_json(run / "state.json")["target_evaluations"], 0)

    def test_refine_rejects_insufficient_calls_before_a_model_or_evaluation_is_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            skill = target / "SKILL.md"
            skill.write_text("---\nname: offline-demo\ndescription: Return a requested result.\n---\n\n处理请求并返回中文结果。\n", encoding="utf-8")
            before = skill.read_bytes()
            marker = root / "backend-called"
            profile = {"backend": "command", "command": [sys.executable, "-c",
                       f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
            cfg = root / "backend.json"
            write_json(cfg, {"schema_version": 1, "agents": {"default": profile},
                             "executors": [{"id": "offline", **profile}]})
            run = root / "run"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["run", str(target), "--output", str(run), "--config", str(cfg),
                             "--skill-up", sys.executable, "--plan-only", "--max-agent-calls", "3"])
            self.assertEqual(code, 2)
            self.assertFalse(marker.exists())
            self.assertFalse((run / "agent-calls").exists())
            self.assertEqual(skill.read_bytes(), before)
            report = read_json(run / "runtime-preflight-before-planning.json")
            self.assertIn("INSUFFICIENT_AGENT_CALLS", [x["code"] for x in report["issues"]])

    def test_case_capacity_is_not_inferred_from_analysis_scenario_count(self):
        import test_plan_contract as fixtures
        fixture = fixtures.CreationAnalysisConclusionTests()
        analysis = fixture._analysis()
        extra = copy.deepcopy(analysis["scenarios"][1])
        extra.update(id="SC-NORMAL-03", situation="Another supported request shape.")
        extra["detail"].update(title="Another supported request shape.", stimulus="Submit the third request shape.")
        analysis["scenarios"].append(extra)
        next(row for row in analysis["risk_assessments"]
             if row["dimension"] == "input_variation")["scenario_ids"].append(extra["id"])
        # Build an independent-review fixture that satisfies the real review schema.
        review = {
            "schema_version": "skill-refiner.analysis-review/v2", "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                "acceptance_criteria_atomic": True, "criteria_observable": True, "basis": "Matches spec-1."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": True,
                "risk_coverage_complete": True, "traceability_complete": True, "basis": "Observable fixture."}
                for row in analysis["scenarios"]],
            "context_scope_assessment": {"intent_grounded": True, "actors_complete": True,
                "scope_clear": True, "constraints_complete": True, "basis": "Explicit source contract."},
            "workflow_assessments": [{"workflow_id": "W1", "order_and_actors_clear": True,
                "inputs_outputs_complete": True, "completion_and_failure_testable": True,
                "requirement_traceable": True, "basis": "Observable step."}],
            "risk_assessments": [{"dimension": row["dimension"], "applicability_justified": True,
                "scenario_links_complete": True, "basis": "Matches scope."} for row in analysis["risk_assessments"]],
            "dependencies_assessed": True, "blocking_decisions_actionable": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "backends.json"
            profile = {"backend": "command", "command": [sys.executable, "-c", "raise SystemExit(99)"]}
            write_json(cfg, {"schema_version": 1, "agents": {"default": profile},
                             "executors": [{"id": "offline", **profile}]})
            spec = root / "request.md"
            spec.write_text("Return a useful result with the requested fields.", encoding="utf-8")
            run = root / "run"
            with patch("refiner.creation.Agent.ask", side_effect=[
                    analysis, review, RefinerError("Stop after confirming case-count deferral")]) as ask, \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["create", "--spec", str(spec), "--output", str(run), "--config", str(cfg),
                             "--plan-only", "--max-cases", "4", "--acceptance-cases", "2"])
            self.assertEqual(code, 2)
            self.assertEqual([call.args[0] for call in ask.call_args_list],
                             ["analyze", "analysis-review", "draft"])
            self.assertFalse((run / "drafts").exists())
            report = read_json(run / "runtime-preflight-after-analysis.json")
            self.assertEqual(report["status"], "passed")
            self.assertNotIn("INSUFFICIENT_CASE_CAPACITY", [x["code"] for x in report["issues"]])
            stage = next(x for x in read_json(run / "stages.json")["stages"] if x["id"] == "analysis")
            self.assertEqual(stage["status"], "completed")

    def test_cli_does_not_apply_an_implicit_per_call_timeout(self):
        for creating in (False, True):
            parser = argparse.ArgumentParser()
            workflow_options(parser, creating=creating)
            self.assertIsNone(parser.parse_args([]).timeout)

    def test_launcher_records_applied_defaults_and_preserves_explicit_overrides(self):
        spec = importlib.util.spec_from_file_location("refiner_launcher_fixture", ROOT / "scripts/launch.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "references").mkdir()
            cfg = root / "backends.json"
            cfg.write_text("{}")
            write_json(root / "references/local-runtime.json", {
                "schema_version": 1, "python": sys.executable, "skill_up": sys.executable,
                "config": str(cfg), "limits": {"max_seconds": 1800, "max_agent_calls": 24}})
            with patch.object(launcher, "__file__", str(root / "scripts/launch.py")), \
                    patch.object(sys, "argv", ["launch.py", "create", "--max-seconds", "999"]), \
                    patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(launcher.main(), 0)
            args = run.call_args.args[0]
            self.assertEqual(args.count("--max-seconds"), 1)
            self.assertEqual(args[args.index("--max-seconds") + 1], "999")
            context = json.loads(run.call_args.kwargs["env"]["SKILL_REFINER_LAUNCH_CONTEXT"])
            self.assertEqual(context["configured_defaults"]["max_seconds"], 1800)
            self.assertNotIn("max_seconds", context["applied_defaults"])
            self.assertEqual(context["applied_defaults"]["max_agent_calls"], 24)
            self.assertNotIn("timeout", context["applied_defaults"])


if __name__ == "__main__":
    unittest.main()

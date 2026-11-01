"""Long-running calls inherit the workflow deadline instead of local magic caps."""
import inspect
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refiner.common import run_process
from refiner.matrix import Matrix
from refiner.runtime import run_api
from refiner.skillup import SkillUp


class StopBeforeCompletion(Exception):
    pass


class ExecutionTimeoutInheritanceTests(unittest.TestCase):
    def test_long_running_execution_defaults_have_no_local_timeout(self):
        self.assertIsNone(inspect.signature(run_process).parameters["timeout"].default)
        self.assertIsNone(inspect.signature(SkillUp).parameters["timeout"].default)
        self.assertIsNone(inspect.signature(Matrix).parameters["timeout"].default)

    def test_model_shell_tool_uses_only_the_remaining_workflow_deadline(self):
        message_with_tool = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "shell", "arguments": json.dumps({"command": "long-task"})},
            }],
        }
        responses = [
            (message_with_tool, {}),
            ({"role": "assistant", "content": "finished"}, {}),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                patch("refiner.runtime.api_request", side_effect=responses), \
                patch("refiner.runtime.run_process", return_value=(0, "ok", "", 1)) as process:
            result = run_api({"model": "fixture", "max_turns": 2}, "task", tmp, None)
        self.assertEqual(result["final_message"], "finished")
        self.assertIsNone(process.call_args.kwargs["timeout"])

    def test_skillup_evaluation_uses_remaining_aggregate_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "skill-up"
            executable.write_text("fixture", encoding="utf-8")
            backend = SkillUp(str(executable), engine_config={"name": "fixture"}, timeout=None)
            backend.deadline = time.monotonic() + 30

            def stop(argv, **kwargs):
                raise StopBeforeCompletion

            with patch("refiner.skillup.run_process", side_effect=stop) as process:
                with self.assertRaises(StopBeforeCompletion):
                    backend._execute(Path(tmp) / "config.json", Path(tmp) / "run", 2, lock_held=True)
            timeout = process.call_args.kwargs["timeout"]
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)

    def test_skillup_without_an_aggregate_deadline_does_not_invent_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "skill-up"
            executable.write_text("fixture", encoding="utf-8")
            backend = SkillUp(str(executable), engine_config={"name": "fixture"}, timeout=None)

            def stop(argv, **kwargs):
                raise StopBeforeCompletion

            with patch("refiner.skillup.run_process", side_effect=stop) as process:
                with self.assertRaises(StopBeforeCompletion):
                    backend._execute(Path(tmp) / "config.json", Path(tmp) / "run", 2, lock_held=True)
            self.assertIsNone(process.call_args.kwargs["timeout"])

    def test_generated_case_judge_does_not_add_a_30_second_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            (target / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
            executable = root / "skill-up"
            executable.write_text("fixture", encoding="utf-8")
            backend = SkillUp(str(executable), engine_config={"name": "fixture"}, timeout=None)
            observed = []
            validation_timeouts = []

            def stop_after_inspection(argv, **kwargs):
                if "validate" in argv:
                    validation_timeouts.append(kwargs["timeout"])
                    return 0, "", "", 0
                config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
                case_path = target / config["cases"]["files"][0]
                observed.append(json.loads(case_path.read_text(encoding="utf-8")))
                raise StopBeforeCompletion

            with patch("refiner.skillup.run_process", side_effect=stop_after_inspection):
                with self.assertRaises(StopBeforeCompletion):
                    backend.evaluate(target, [{"id": "case-one", "prompt": "fixture", "files": {},
                                               "checks": [{"id": "a1", "type": "text_equal", "value": "ok"}]}],
                                     root / "run")
            self.assertEqual(len(observed), 1)
            self.assertIsNone(observed[0]["judge"]["timeout_seconds"])
            self.assertEqual(validation_timeouts, [None])

    def test_managed_executor_transport_does_not_add_240_second_fallback(self):
        from refiner.execute import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "case"
            skill = cwd / "skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Fixture\n", encoding="utf-8")
            profile = root / "profile.json"
            request = root / "request.json"
            output = root / "output.json"
            profile.write_text(json.dumps({"backend": "command", "command": ["fixture", "{output_file}"]}),
                                encoding="utf-8")
            request.write_text(json.dumps({"messages": ["fixture"]}), encoding="utf-8")

            def process(argv, **kwargs):
                Path(argv[-1]).write_text("{}", encoding="utf-8")
                return 0, "", "", 0

            with patch("refiner.execute.Path.cwd", return_value=cwd), \
                    patch("refiner.execute.sys.argv", ["execute.py", str(profile), str(request), str(output)]), \
                    patch("refiner.execute.run_process", side_effect=process) as run_process:
                main()
            self.assertIsNone(run_process.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()

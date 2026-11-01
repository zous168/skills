"""Managed Codex evaluation networking policy; all CLI launches are mocked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refiner.common import RefinerError
from refiner.profiles import validate_profile
from refiner.runtime import run_cli


_EVENTS = "\n".join(json.dumps(row) for row in (
    {"type": "thread.started", "thread_id": "fixture-thread"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "local result"}},
    {"type": "turn.completed"},
)) + "\n"


class ManagedCodexEvaluationNetworkPolicyTests(unittest.TestCase):
    def invoke(self, backend, *, sandbox="workspace-write", planning=False):
        captured = {}

        def process(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            return 0, _EVENTS, "", 0.01

        profile = {
            "backend": backend,
            "model": "fixture-model",
            "reasoning_effort": "medium",
            "codex_sandbox": sandbox,
        }
        if backend == "codex_subagent":
            profile["child_model"] = "fixture-child-model"
        with tempfile.TemporaryDirectory() as tmp, \
                patch("refiner.runtime.executable", return_value="codex-fixture"), \
                patch("refiner.runtime.run_process", side_effect=process) as run_process, \
                patch("refiner.runtime.native_rollouts", return_value=([], [], [])):
            result = run_cli(profile, "fixture task", tmp, 5,
                             planning=planning, target_evaluation=True)
        return result, captured, run_process

    def assert_strict_policy(self, argv):
        self.assertNotIn("--ignore-user-config", argv)
        self.assertNotIn("--ignore-rules", argv)
        self.assertIn("--strict-config", argv)
        self.assertIn("--sandbox", argv)
        sandbox = argv[argv.index("--sandbox") + 1]
        self.assertEqual(sandbox, "workspace-write")
        self.assertNotEqual(sandbox, "danger-full-access")
        self.assertEqual(argv[-1], "-")

        configs = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"]
        expected = {
            "sandbox_workspace_write.network_access=true",
            "features.network_proxy.enabled=true",
            "features.network_proxy.domains={}",
            "features.network_proxy.allow_local_binding=false",
            "features.network_proxy.allow_upstream_proxy=false",
            'web_search="disabled"',
            "mcp_servers={}",
        }
        self.assertTrue(expected.issubset(set(configs)), configs)
        self.assertEqual(sum(value == "features.network_proxy.domains={}" for value in configs), 1)
        for feature in ("apps", "browser_use", "computer_use"):
            index = argv.index(feature)
            self.assertEqual(argv[index - 1], "--disable")

        # The safety overrides are last among config values, after profile model
        # settings, so those settings cannot weaken the frozen network policy.
        policy_start = configs.index("sandbox_workspace_write.network_access=true")
        self.assertGreater(policy_start, configs.index('model_reasoning_effort="medium"'))
        self.assertEqual(configs[-7:], [
            "sandbox_workspace_write.network_access=true",
            "features.network_proxy.enabled=true",
            "features.network_proxy.domains={}",
            "features.network_proxy.allow_local_binding=false",
            "features.network_proxy.allow_upstream_proxy=false",
            'web_search="disabled"',
            "mcp_servers={}",
        ])

    def test_codex_target_evaluation_uses_one_shot_deny_all_policy(self):
        result, captured, _ = self.invoke("codex")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["metadata"]["command_network_policy"]["mode"], "deny_all")
        self.assertEqual(result["metadata"]["command_network_policy"]["client_model_auth_network"], "outside_scope")
        argv = captured["argv"]
        self.assertIn("--ephemeral", argv)
        self.assertNotIn("--enable", argv)
        self.assert_strict_policy(argv)

    def test_codex_subagent_target_evaluation_uses_same_policy(self):
        result, captured, _ = self.invoke("codex_subagent")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["metadata"]["command_network_policy"]["mode"], "deny_all")
        argv = captured["argv"]
        self.assertIn(["--enable", "multi_agent"], [argv[i:i + 2] for i in range(len(argv) - 1)])
        self.assert_strict_policy(argv)

    def test_danger_full_access_is_rejected_before_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("refiner.runtime.executable", return_value="codex-fixture"), \
                patch("refiner.runtime.run_process") as run_process:
            with self.assertRaisesRegex(RefinerError, "rejects danger-full-access"):
                run_cli({"backend": "codex", "codex_sandbox": "danger-full-access"},
                        "fixture task", tmp, 5, target_evaluation=True)
        run_process.assert_not_called()

    def test_read_only_is_rejected_before_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("refiner.runtime.executable", return_value="codex-fixture"), \
                patch("refiner.runtime.run_process") as run_process:
            with self.assertRaisesRegex(RefinerError, "read-only networking behavior is not verified"):
                run_cli({"backend": "codex", "codex_sandbox": "read-only"},
                        "fixture task", tmp, 5, target_evaluation=True)
        run_process.assert_not_called()

    def test_planning_mode_cannot_be_misused_as_a_target_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("refiner.runtime.executable", return_value="codex-fixture"), \
                patch("refiner.runtime.run_process") as run_process:
            with self.assertRaisesRegex(RefinerError, "planning-only"):
                run_cli({"backend": "codex", "codex_sandbox": "read-only"},
                        "fixture task", tmp, 5, planning=True, target_evaluation=True)
        run_process.assert_not_called()

    def test_unknown_profile_network_policy_is_not_silently_ignored(self):
        with self.assertRaisesRegex(RefinerError, "Unknown backend profile fields"):
            validate_profile({"backend": "codex", "network_policy": "allow_all"})

    def test_managed_transport_marks_only_codex_targets_for_strict_policy(self):
        from refiner.execute import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "case"
            skill = cwd / "skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Local fixture\n", encoding="utf-8")
            profile = root / "profile.json"
            request = root / "request.json"
            output = root / "output.json"
            profile.write_text(json.dumps({"backend": "codex"}), encoding="utf-8")
            request.write_text(json.dumps({"messages": ["fixture"], "timeout_seconds": 5}), encoding="utf-8")
            result = {"exit_code": 0, "transcript": [], "artifacts": {"files": []}, "metadata": {}}
            with patch("refiner.execute.Path.cwd", return_value=cwd), \
                    patch("refiner.execute.sys.argv", ["execute.py", str(profile), str(request), str(output)]), \
                    patch("refiner.execute.execute", return_value=result) as execute:
                main()
            execute.assert_called_once()
            self.assertIs(execute.call_args.kwargs["target_evaluation"], True)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()

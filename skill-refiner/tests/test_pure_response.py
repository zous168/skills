import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from refiner.common import RefinerError
from refiner import pure_response as pure


class PureResponsePreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill = self.root / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text("# 测试", encoding="utf-8")
        (self.skill / "references").mkdir()
        (self.skill / "references" / "rule.md").write_text("只回答", encoding="utf-8")
        self.cli = self.root / "codex.exe"
        self.cli.write_bytes(b"fixture executable")
        digest = hashlib.sha256(self.cli.read_bytes()).hexdigest()
        self.hash_patch = patch.object(pure, "_PROBED_CLI_SHA256", digest)
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def prepare(self):
        return pure.prepare(self.cli, self.skill, "工单", model="fixture-model", reasoning_effort="medium")

    def test_bundle_is_hash_bound_and_disabled_by_default(self):
        result = self.prepare()
        self.assertEqual(result["status"], "coverage_gap")
        self.assertEqual([x["path"] for x in result["bundle_manifest"]], ["SKILL.md", "references/rule.md"])
        payload = json.loads(result["payload"])
        self.assertEqual(payload["package"][1]["text"], "只回答")
        argv = result["argv"]
        for feature in pure._DISABLED_FEATURES:
            index = argv.index(feature)
            self.assertEqual(argv[index - 1], "--disable")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("mcp_servers={}", argv)
        first = result["bundle_manifest_sha256"]
        (self.skill / "references" / "rule.md").write_text("改动", encoding="utf-8")
        self.assertNotEqual(first, self.prepare()["bundle_manifest_sha256"])

    def test_new_or_changed_binary_is_rejected(self):
        self.cli.write_bytes(b"different executable")
        with self.assertRaises(RefinerError):
            self.prepare()

    def test_runtime_files_and_schema_are_not_simulated(self):
        (self.skill / "validator.py").write_text("print(1)", encoding="utf-8")
        with self.assertRaisesRegex(RefinerError, "non-Markdown"):
            self.prepare()

    def test_clean_stream_never_certifies_absence(self):
        stream = [
            {"type": "thread.started"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "好"}},
            {"type": "turn.completed"},
        ]
        rendered = "\n".join(map(json.dumps, stream))
        result = pure.classify_stdout(rendered, "", 0)
        self.assertEqual(result["status"], "coverage_gap")
        self.assertIs(result["absence_of_side_effects_proven"], False)
        self.assertIs(result["absence_of_external_calls_proven"], False)
        for stderr in ("ERROR unsupported call: exec_command", "unsupported custom tool call: apply_patch"):
            with self.assertRaises(RefinerError):
                pure.classify_stdout(rendered, stderr, 0)
        stream.insert(2, {"type": "item.started", "item": {"type": "command_execution"}})
        with self.assertRaises(RefinerError):
            pure.classify_stdout("\n".join(map(json.dumps, stream)), "", 0)

    def test_incomplete_and_non_message_events_rejected(self):
        with self.assertRaises(RefinerError):
            pure.classify_stdout('{"type":"turn.completed"}', "", 0)
        with self.assertRaises(RefinerError):
            pure.classify_stdout('not json', "", 0)
        with self.assertRaises(RefinerError):
            pure.classify_stdout(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call"}}), "", 0)


if __name__ == "__main__":
    unittest.main()

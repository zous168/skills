"""Native rollout observation tests; all model and CLI calls are simulated."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refiner.checks import check_one
from refiner.native_tools import native_tool_events
from refiner.runtime import native_rollouts, run_cli


def response(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def item(kind, **payload):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": kind, **payload}}}


def completed():
    return {"type": "event_msg", "payload": {"type": "task_complete"}}


class ToolNormalizationTests(unittest.TestCase):
    def test_noncommand_call_and_actual_output_are_preserved_without_fake_success(self):
        rows = [response("function_call", name="submit", namespace="external", call_id="net",
                         arguments=json.dumps({"url": "https://example.invalid/api"})),
                response("function_call_output", call_id="net", output="delivered response")]
        trace, raw, report = native_tool_events(rows, "child")
        self.assertEqual(trace[0]["tool_call"]["name"], "external.submit")
        self.assertEqual(trace[0]["tool_call"]["arguments"]["url"], "https://example.invalid/api")
        self.assertEqual(trace[1]["tool_result"]["content"], "delivered response")
        self.assertEqual(trace[1]["tool_result"]["status"], "unknown")
        self.assertEqual(len(raw), 2)
        self.assertIn("external.submit", report["unclassified_tools"])
        self.assertFalse(check_one({"type": "tool_call_absent", "value": "external"}, "", ".", trace)[0])
        self.assertFalse(report["absence_of_external_calls_proven"])

    def test_freeform_patch_and_file_change_are_one_observed_call_and_do_not_copy_reasoning(self):
        rows = [response("reasoning", raw_content="private reasoning must never be copied"),
                item("Reasoning", id="r", raw_content="private reasoning must never be copied"),
                response("custom_tool_call", name="apply_patch", call_id="patch", input="patch text",
                         encrypted_content="opaque private transport"),
                response("custom_tool_call_output", call_id="patch", output="done"),
                item("FileChange", id="patch", changes=[{"path": "temporary.txt", "kind": "add"}],
                     status="completed", stdout="applied", stderr="")]
        trace, raw, report = native_tool_events(rows, "child")
        self.assertEqual(len([r for r in trace if r["role"] == "tool_call"]), 1)
        self.assertEqual(trace[0]["tool_call"]["arguments"], {"input": "patch text"})
        self.assertIn("filesystem_change_tool", report["observed_effect_categories"])
        self.assertNotIn("private reasoning", json.dumps(raw))
        self.assertNotIn("encrypted_content", json.dumps(raw))
        self.assertFalse(check_one({"type": "tool_call_absent", "value": "apply_patch"}, "", ".", trace)[0])

    def test_legacy_command_keeps_real_exit_code_and_deduplicates_response_views(self):
        rows = [response("function_call", name="exec_command", call_id="cmd", arguments='{"cmd":"fixture"}'),
                response("function_call_output", call_id="cmd", output="protocol text"),
                item("CommandExecution", id="cmd", command="fixture", exit_code=7,
                     aggregated_output="actual stdout")]
        trace, _, report = native_tool_events(rows, "child")
        self.assertEqual([r["tool_call"]["name"] for r in trace if r["role"] == "tool_call"],
                         ["command_execution"])
        result = next(row["tool_result"] for row in trace if row["role"] == "tool_result")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["content"], "actual stdout")
        self.assertEqual(report["status"], "complete")

    def test_mcp_result_keeps_exact_name_and_does_not_trust_read_only_hint(self):
        rows = [item("McpToolCall", id="m", server="service", tool="request", arguments={"x": 1},
                     readOnlyHint=True, status="completed", result={"content": [{"text": "ok"}]})]
        trace, raw, report = native_tool_events(rows, "child")
        self.assertEqual(trace[0]["tool_call"]["name"], "mcp__service__request")
        self.assertIn("mcp__service__request", report["unclassified_tools"])
        self.assertTrue(raw[0]["payload"]["item"]["readOnlyHint"])
        self.assertEqual(report["os_network_operations"], "not_observed")
        self.assertFalse(report["absence_of_side_effects_proven"])

    def test_unknown_tool_format_and_missing_output_cannot_establish_absence(self):
        rows = [response("future_tool_call", name="future.network", call_id="new", action={"url": "target"})]
        trace, raw, report = native_tool_events(rows, "child")
        self.assertEqual(len(raw), 1)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["unknown_event_types"], ["future_tool_call"])
        self.assertEqual(report["unmatched_call_ids"], ["new"])
        self.assertFalse(check_one({"type": "tool_call_absent", "value": ".+"}, "", ".", trace)[0])

    def test_orphan_output_and_malformed_rollout_are_not_silently_ignored(self):
        _, _, report = native_tool_events([
            response("function_call_output", call_id="unknown", output="observed"),
            {"_parse_error": 9}], "child")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["orphan_output_ids"], ["unknown"])
        self.assertTrue(any("Invalid JSON rollout line 9" in e for e in report["errors"]))

    def test_unknown_call_event_is_retained_even_without_tool_in_its_type(self):
        trace, raw, report = native_tool_events([
            {"type": "event_msg", "payload": {"type": "network_call_started", "call_id": "network"}}], "child")
        self.assertEqual(len(raw), 1)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(trace[0]["tool_call"]["name"], "unparsed_event.network_call_started")

    def test_write_then_delete_stays_visible_even_when_file_hash_inventory_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = list(root.iterdir())
            target = root / "temporary.txt"
            target.write_text("transient", encoding="utf-8")
            target.unlink()
            self.assertEqual(before, list(root.iterdir()))
        trace, _, report = native_tool_events([
            item("FileChange", id="write", changes=[{"path": "temporary.txt", "kind": "add"}], status="completed"),
            item("FileChange", id="delete", changes=[{"path": "temporary.txt", "kind": "delete"}], status="completed"),
        ], "child")
        self.assertEqual(report["tool_call_count"], 2)
        self.assertFalse(check_one({"type": "tool_call_absent", "value": "apply_patch"}, "", ".", trace)[0])
        # This demonstrates the snapshot blind spot, not an OS monitor.
        self.assertEqual(report["os_file_operations"], "not_observed")

    def test_credentials_and_nested_private_reasoning_are_removed_from_both_channels(self):
        value = {"headers": {"Authorization": "Bearer auth-sentinel"}, "nested": {
            "password": "password-sentinel", "raw_content": "reasoning-sentinel",
            "array": [{"api_key": "key-sentinel", "encrypted_content": "encrypted-sentinel"}]}}
        trace, raw, _ = native_tool_events([
            response("function_call", call_id="private", name="opaque.request", arguments=json.dumps(value)),
            response("function_call_output", call_id="private", output=json.dumps(value)),
            item("McpToolCall", id="m", server="local", tool="request", arguments=value,
                 status="completed", result=value),
        ], "child")
        rendered = json.dumps([trace, raw])
        for secret in ("auth-sentinel", "password-sentinel", "reasoning-sentinel", "key-sentinel", "encrypted-sentinel"):
            self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_parent_delegation_task_is_hash_length_metadata_not_copied_text(self):
        task = "private-task-sentinel" * 5000
        trace, raw, report = native_tool_events([
            response("function_call", name="spawn_agent", call_id="s", arguments=json.dumps({"message": task})),
            response("function_call_output", call_id="s", output='{"agent_id":"child"}'),
        ], "parent", parent_delegation=True)
        rendered = json.dumps([trace, raw, report])
        self.assertNotIn("private-task-sentinel", rendered)
        self.assertLess(len(rendered), 8000)
        self.assertIn("delegation_content", rendered)
        self.assertIn("sha256", rendered)

    def test_basic_digest_headers_and_url_userinfo_are_not_copied(self):
        values = ["Authorization: Basic credential-sentinel",
                  "curl -H 'Authorization: Digest response=digest-sentinel' target",
                  "https://user:password-sentinel@example.invalid/path"]
        trace, raw, _ = native_tool_events([
            response("function_call", name="opaque.request", call_id="headers", arguments=json.dumps({"values": values})),
            response("function_call_output", call_id="headers", output="\n".join(values)),
        ], "child")
        rendered = json.dumps([trace, raw])
        for secret in ("credential-sentinel", "digest-sentinel", "password-sentinel"):
            self.assertNotIn(secret, rendered)

    def test_conflicting_call_ids_retain_second_external_action_and_fail_closed(self):
        trace, raw, report = native_tool_events([
            response("function_call", name="reader.read", call_id="same", arguments="{}"),
            response("function_call", name="external.send", call_id="same", arguments="{}"),
            response("function_call_output", call_id="same", output="observed"),
        ], "child")
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("same", report["conflicting_call_ids"])
        self.assertIn("external.send", [r["tool_call"]["name"] for r in trace if r["role"] == "tool_call"])
        self.assertFalse(check_one({"type": "tool_call_absent", "value": "external.send"}, "", ".", trace)[0])
        self.assertEqual(len(raw), 3)

    def test_conflicting_outputs_are_retained_and_not_silently_overwritten(self):
        trace, _, report = native_tool_events([
            response("function_call", name="reader.read", call_id="same", arguments="{}"),
            response("function_call_output", call_id="same", output="first"),
            response("function_call_output", call_id="same", output="second"),
        ], "child")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual([r["tool_result"]["content"] for r in trace if r["role"] == "tool_result"], ["first", "second"])

    def test_mcp_cross_view_argument_conflicts_are_not_treated_as_equivalent(self):
        base = [response("function_call", name="mcp__service__request", call_id="m",
                         arguments='{"url":"read"}')]
        conflicting = item("McpToolCall", id="m", server="service", tool="request",
                           arguments={"url": "write"}, status="completed", result="done")
        trace, _, report = native_tool_events(base + [conflicting], "child")
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("m", report["conflicting_call_ids"])
        self.assertEqual([r["tool_call"]["arguments"]["url"] for r in trace if r["role"] == "tool_call"],
                         ["read", "write"])
        matching = item("McpToolCall", id="m", server="service", tool="request",
                        arguments='{"url":"read"}', status="completed", result="done")
        trace, _, report = native_tool_events(base + [matching], "child")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len([r for r in trace if r["role"] == "tool_call"]), 1)

    def test_unclassified_tool_cannot_pass_an_unmatched_absence_regex(self):
        trace, _, _ = native_tool_events([
            response("function_call", name="opaque.request", call_id="u", arguments="{}"),
            response("function_call_output", call_id="u", output="done"),
        ], "child")
        for kind in ("tool_call_absent", "tool_command_absent"):
            passed, diagnostic = check_one({"type": kind, "value": "web|http|network"}, "", ".", trace)
            self.assertFalse(passed)
            self.assertIn("coverage gap", diagnostic)
        self.assertFalse(check_one({"type": "tool_call_absent", "value": ".+"}, "", ".",
                                   native_tool_events([], "empty")[0])[0])
        # Synthesized checker controls remain independent of runtime observation.
        self.assertTrue(check_one({"type": "tool_call_absent", "value": "external"}, "", ".", [],
                                  synthetic_control=True)[0])

    def test_capture_budget_omits_large_data_with_fingerprints_and_cannot_claim_complete(self):
        trace, raw, report = native_tool_events([
            response("function_call", name="reader.read", call_id="large", arguments="{}"),
            response("function_call_output", call_id="large", output="large-value-sentinel" * 2000),
        ], "child", max_event_bytes=300, max_total_bytes=900)
        self.assertEqual(report["status"], "incomplete")
        self.assertGreater(report["capture"]["raw"]["omitted_count"], 0)
        self.assertIn("sha256", json.dumps(report["capture"]))
        self.assertNotIn("large-value-sentinel", json.dumps([trace, raw]))
        self.assertLess(len(json.dumps([trace, raw])), 10000)


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions/2026/10/31"
        self.sessions.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.parent_rows = [
            response("function_call", name="spawn_agent", call_id="spawn", arguments='{"message":"task"}'),
            response("function_call_output", call_id="spawn", output='{"agent_id":"child"}'),
        ]

    def write_rollout(self, identifier, rows, parent=None):
        meta = {"id": identifier, "parent_thread_id": parent, "cli_version": "fixture-version"}
        path = self.sessions / (identifier + ".jsonl")
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in
                                  [{"type": "session_meta", "payload": meta}] + rows), encoding="utf-8")
        os.utime(path, (1000, 1000))
        return path

    def prepare(self, child_rows):
        self.write_rollout("parent", self.parent_rows)
        self.write_rollout("child", child_rows, "parent")
        self.write_rollout("unrelated", [response("custom_tool_call", call_id="private", name="secret",
                                                 input="unrelated private data"), completed()], "other-parent")

    def invoke(self):
        stdout = "\n".join(json.dumps(row) for row in [
            {"type": "thread.started", "thread_id": "parent"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
            {"type": "turn.completed"}])
        with patch("refiner.runtime.executable", return_value="fixture-codex"), \
             patch("refiner.runtime.run_process", return_value=(0, stdout, "", 0.1)), \
             patch("refiner.runtime.time.time", return_value=1000):
            return run_cli({"backend": "codex_subagent"}, "task", self.root, 10)

    def test_all_available_related_tools_are_saved_and_unrelated_history_is_not_copied(self):
        self.prepare([response("custom_tool_call", call_id="patch", name="apply_patch", input="patch text"),
                      response("custom_tool_call_output", call_id="patch", output="done"), completed()])
        result = self.invoke()
        self.assertEqual(result["exit_code"], 0)
        files = {f["name"]: f["content"] for f in result["artifacts"]["files"]}
        self.assertIn("native-child-child-tool-events.jsonl", files)
        self.assertIn("patch text", files["native-child-child-tool-events.jsonl"])
        self.assertEqual(json.loads(files["native-child-child.json"]), [])
        self.assertNotIn("unrelated private data", json.dumps(files))
        self.assertFalse(result["metadata"]["native_observation"]["absence_of_side_effects_proven"])

    def test_unknown_tool_events_return_error_with_full_available_evidence(self):
        self.prepare([response("future_tool_call", call_id="new", name="unknown.network"), completed()])
        result = self.invoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["metadata"]["native_observation"]["status"], "incomplete")
        self.assertIn("Unknown tool event formats", result["stderr"])
        self.assertTrue(any("unknown.network" in f["content"] for f in result["artifacts"]["files"]))
        self.assertTrue(any(f["name"] == "adapter-stderr.txt" and "Unknown tool event formats" in f["content"]
                            for f in result["artifacts"]["files"]))

    def test_prior_completed_turn_does_not_hide_a_current_unfinished_child(self):
        self.prepare([completed(), {"type": "event_msg", "payload": {"type": "task_started"}}])
        result = self.invoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("latest task", result["stderr"])

    def test_parent_tool_actions_and_nested_delegation_are_not_hidden(self):
        self.parent_rows += [item("CommandExecution", id="parent-command", command="external-call",
                                  exit_code=0, aggregated_output="observed")]
        self.prepare([completed()])
        self.write_rollout("grandchild", [
            item("CommandExecution", id="grand-command", command="write-and-delete", exit_code=0,
                 aggregated_output="observed"), completed()], "child")
        result = self.invoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("nondelegation tools", result["stderr"])
        self.assertIn("Nested native delegation", result["stderr"])
        self.assertTrue(any(row.get("tool_call", {}).get("arguments", {}).get("command") == "write-and-delete"
                            for row in result["transcript"]))

    def test_truncated_json_is_an_error_while_prior_tool_evidence_survives(self):
        self.prepare([item("CommandExecution", id="c", command="fixture", exit_code=0, aggregated_output="ok"),
                      completed()])
        path = self.sessions / "child.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write('\n{"type":')
        os.utime(path, (1000, 1000))
        result = self.invoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Invalid JSON rollout line", result["stderr"])
        self.assertTrue(any(row.get("tool_result", {}).get("content") == "ok" for row in result["transcript"]))

    def test_observed_child_spawn_is_rejected_even_if_grandchild_rollout_is_missing(self):
        self.prepare([response("function_call", name="spawn_agent", call_id="nested", arguments="{}"),
                      response("function_call_output", call_id="nested", output='{"agent_id":"missing"}'),
                      completed()])
        result = self.invoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("Child attempted nested native delegation", result["stderr"])


if __name__ == "__main__":
    unittest.main()

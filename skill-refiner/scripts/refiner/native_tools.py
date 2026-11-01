"""Normalize observable native tool events without claiming OS-wide effect isolation."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex


_NON_TOOL_ITEMS = {"Reasoning", "AgentMessage", "UserMessage", "ContextCompaction", "Plan",
                   "reasoning", "agent_message", "user_message", "context_compaction", "plan"}
_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call",
               "computer_call", "image_generation_call"}
_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}
_ITEM_TYPES = {"CommandExecution", "command_execution", "FileChange", "file_change",
               "McpToolCall", "mcp_tool_call", "DynamicToolCall", "dynamic_tool_call",
               "WebSearch", "WebSearchCall", "web_search", "ImageGeneration", "image_generation",
               "CollabAgentToolCall", "SubAgentActivity"}
_PRIVATE_FIELDS = {"encrypted_content", "internal_chat_message_metadata_passthrough", "raw_content", "summary_text"}
_CREDENTIAL_KEYS = re.compile(r"^(?:authorization|proxy_authorization|password|passwd|secret|client_secret|"
                              r"api_key|apikey|access_token|refresh_token|token|cookie|set_cookie)$", re.I)


def _fingerprint(value, reason):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"omitted": reason, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def sanitize_tool_value(value, *, _depth=0):
    """Remove credentials/private reasoning from both raw and normalized channels."""
    if _depth > 24:
        return _fingerprint(value, "nesting_limit")
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if _CREDENTIAL_KEYS.fullmatch(str(key).replace("-", "_")) else
                          sanitize_tool_value(child, _depth=_depth + 1))
                for key, child in value.items() if key not in _PRIVATE_FIELDS}
    if isinstance(value, list):
        return [sanitize_tool_value(child, _depth=_depth + 1) for child in value]
    if isinstance(value, str):
        # Serialized JSON is common in function arguments and tool stdout.
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(sanitize_tool_value(parsed, _depth=_depth + 1), ensure_ascii=False)
        text = re.sub(r"(?:sk-|crsr_)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        text = re.sub(r"(?i)(\b(?:basic|digest|bearer|negotiate|ntlm)\s+)[A-Za-z0-9._~+/\-=]+",
                      r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(\b(?:proxy-authorization|authorization)\s*:\s*)[^\r\n]+",
                      r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/]*@", r"\1[REDACTED]@", text)
        return re.sub(r"""(?ix)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|
                       passwd|secret|authorization|cookie|raw_content|encrypted_content)["']?\s*[:=]\s*)
                       (?:"[^"]*"|'[^']*'|[^\s,;}\]]+)""", r"\1[REDACTED]", text)
    return value


def _parent_metadata(row):
    """Delegation inputs/results are routing evidence, not another copy of the task."""
    result = copy.deepcopy({key: row[key] for key in ("timestamp", "ordinal", "type") if key in row})
    payload = row.get("payload", {})
    allowed = {"type", "id", "call_id", "name", "namespace", "status", "tool", "server", "kind",
               "thread_id", "agent_thread_id", "receiver_thread_ids", "exit_code"}
    minimal = {key: payload[key] for key in allowed if key in payload}
    for key in ("arguments", "input", "output", "result"):
        if key in payload:
            minimal[key] = _fingerprint(payload[key], "delegation_content")
    if isinstance(payload.get("item"), dict):
        child = payload["item"]
        minimal["item"] = {key: child[key] for key in allowed if key in child}
        for key in ("arguments", "input", "output", "result", "command", "aggregated_output", "changes"):
            if key in child:
                minimal["item"][key] = _fingerprint(child[key], "delegation_content")
    result["payload"] = minimal
    return result


def _bounded_channel(values, *, byte_budget, event_budget):
    stored, samples, used, omitted = [], [], 0, 0
    for value in values:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > event_budget or used + len(encoded) > byte_budget:
            omitted += 1
            marker = _fingerprint(value, "capture_byte_budget")
            if len(samples) < 20:
                samples.append(marker)
            continue
        stored.append(value)
        used += len(encoded)
    return stored, {"bytes": used, "omitted_count": omitted, "omitted_samples": samples}


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _safe_event(row):
    """Keep tool inputs/results; omit reasoning and internal transport metadata."""
    result = {key: copy.deepcopy(row[key]) for key in ("timestamp", "ordinal", "type") if key in row}
    payload = sanitize_tool_value(row["payload"])
    for key in _PRIVATE_FIELDS:
        payload.pop(key, None)
    if isinstance(payload.get("item"), dict):
        for key in _PRIVATE_FIELDS:
            payload["item"].pop(key, None)
    result["payload"] = payload
    return result


def native_tool_events(rows, thread_id, *, parent_delegation=False, max_event_bytes=256_000,
                       max_total_bytes=4_000_000, max_events=5000):
    """Return transcript, original tool-only events and explicit observation limits.

    A delivered function output is observed evidence, not proof that a command
    succeeded. Only an actual exit code or explicit tool status supplies success.
    """
    calls, outputs, raw, errors, unknown = {}, {}, [], [], set()
    extra_outputs, conflicting_ids = [], set()
    effect_classes, unclassified = set(), set()

    def call(identifier, name, arguments, index, *, command=False, view=False):
        if not isinstance(identifier, str) or not identifier:
            identifier = "unidentified-" + str(index)
            errors.append("Tool event has no stable call ID at row " + str(index))
        if not isinstance(name, str) or not name:
            name = "unidentified_tool"
            errors.append("Tool event has no tool name at row " + str(index))
        if identifier in calls:
            # CommandExecution carries the real command and exit status and
            # preserves the existing command_execution trace name.
            previous = calls[identifier]
            previous_name = previous["name"].split(".")[-1]
            old_command = previous["arguments"].get("command", previous["arguments"].get("cmd")) if isinstance(previous["arguments"], dict) else None
            if isinstance(old_command, list):
                old_command = shlex.join(old_command)
            if (command and previous_name in {"exec_command", "shell_command", "local_shell_call", "command_execution"}
                    and (old_command is None or old_command == arguments.get("command"))):
                calls[identifier].update(name="command_execution", arguments=arguments)
                return identifier
            mapped_views = {"apply_patch", "spawn_agent", "spawn", "send_input", "send_message",
                            "wait", "wait_agent", "resume_agent", "close_agent"}
            if view and ((previous_name in mapped_views and previous_name == name.split(".")[-1])
                         or (previous_name in mapped_views and name == "collaboration.interacted")):
                return identifier
            if previous["name"] == name and previous["arguments"] == arguments:
                return identifier
            conflicting_ids.add(identifier)
            errors.append("Conflicting tool calls reuse call ID: " + identifier)
            conflict_key = identifier + "#conflict-" + str(index)
            calls[conflict_key] = {"id": identifier, "name": name, "arguments": arguments, "index": index}
            return identifier
        calls[identifier] = {"id": identifier, "name": name, "arguments": arguments, "index": index}
        return identifier

    def output(identifier, value, index, *, status="unknown", exit_code=None, priority=0):
        if not isinstance(identifier, str) or not identifier:
            identifier = "unidentified-output-" + str(index)
            errors.append("Tool output has no stable call ID at row " + str(index))
        result = {"call_id": thread_id + ":" + identifier, "status": status, "content": _text(value)}
        if exit_code is not None:
            result["exit_code"] = exit_code
        prior = outputs.get(identifier)
        if prior is not None and priority == prior["priority"] and result != prior["result"]:
            conflicting_ids.add(identifier)
            errors.append("Conflicting tool outputs reuse call ID: " + identifier)
            extra_outputs.append({"result": result, "index": index})
            return
        if prior is None or priority > prior["priority"]:
            outputs[identifier] = {"result": result, "index": index, "priority": priority}

    def arguments(value, index):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except ValueError:
            errors.append("Function arguments are not JSON at row " + str(index))
            return {"unparsed_arguments": value}

    def tool_status(value, is_error=False):
        if is_error or str(value).lower() in {"error", "failed", "failure", "cancelled", "declined"}:
            return "error"
        if str(value).lower() in {"completed", "complete", "success", "succeeded"}:
            return "success"
        return "unknown"

    for index, row in enumerate(rows):
        if index >= max_events:
            errors.append("Rollout event count exceeds capture budget")
            break
        if not isinstance(row, dict):
            errors.append("Non-object rollout row " + str(index))
            continue
        if "_parse_error" in row:
            errors.append("Invalid JSON rollout line " + str(row["_parse_error"]))
            continue
        if parent_delegation:
            row = _parent_metadata(row)
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            errors.append("Non-object rollout payload at row " + str(index))
            continue
        kind = payload.get("type", "")
        if row.get("type") == "response_item":
            if kind in _OUTPUT_TYPES:
                raw.append(_safe_event(row))
                output(payload.get("call_id", payload.get("id")), payload.get("output", ""), index,
                       status=tool_status(payload.get("status"), payload.get("is_error", False)))
            elif kind in _CALL_TYPES:
                raw.append(_safe_event(row))
                identifier = payload.get("call_id", payload.get("id"))
                name = payload.get("name") or kind
                namespace = payload.get("namespace")
                if namespace and "." not in name:
                    name = str(namespace) + "." + name
                if kind == "custom_tool_call":
                    args = {"input": payload.get("input", "")}
                elif kind == "local_shell_call":
                    args = copy.deepcopy(payload.get("action", {}))
                    if isinstance(args, dict) and isinstance(args.get("command"), list):
                        args["command"] = shlex.join(args["command"])
                else:
                    args = arguments(payload.get("arguments", payload.get("action", {})), index)
                call(identifier, name, args, index)
                last_name = name.split(".")[-1]
                if last_name in {"exec_command", "shell_command", "local_shell_call"}:
                    effect_classes.add("subprocess_effects_not_observed")
                elif last_name == "apply_patch":
                    effect_classes.add("filesystem_change_tool")
                elif (last_name not in {"spawn_agent", "wait", "wait_agent", "send_input", "send_message",
                                       "close_agent", "resume_agent"}
                      and kind not in {"web_search_call", "computer_call", "image_generation_call"}):
                    unclassified.add(name)
                if kind in {"web_search_call", "computer_call", "image_generation_call"}:
                    effect_classes.add(kind)
                    if "output" in payload or "result" in payload:
                        output(identifier, payload.get("output", payload.get("result")), index,
                               status=tool_status(payload.get("status")))
            elif isinstance(kind, str) and ("tool" in kind or kind.endswith(("_call", "_call_output"))):
                raw.append(_safe_event(row))
                unknown.add(kind)
                call(payload.get("call_id", payload.get("id")), payload.get("name") or kind,
                     {"unparsed_event": _safe_event(row)["payload"]}, index)
        elif row.get("type") == "event_msg" and kind == "item_completed":
            item = payload.get("item", {})
            if not isinstance(item, dict):
                errors.append("Completed item is not an object at row " + str(index))
                continue
            item_kind = item.get("type", "")
            if item_kind in _NON_TOOL_ITEMS:
                continue
            raw.append(_safe_event(row))
            identifier = item.get("call_id", item.get("id"))
            if item_kind in {"CommandExecution", "command_execution"}:
                command = item.get("command", "")
                if isinstance(command, list):
                    command = shlex.join(command)
                identifier = call(identifier, "command_execution", {"command": command}, index, command=True)
                effect_classes.add("subprocess_effects_not_observed")
                if type(item.get("exit_code")) is int:
                    code = item["exit_code"]
                    output(identifier, item.get("aggregated_output", ""), index,
                           status="success" if code == 0 else "error", exit_code=code, priority=3)
            elif item_kind in {"FileChange", "file_change"}:
                identifier = call(identifier, "apply_patch", {"changes": item.get("changes", [])}, index, view=True)
                effect_classes.add("filesystem_change_tool")
                output(identifier, {k: item[k] for k in ("changes", "stdout", "stderr") if k in item}, index,
                       status=tool_status(item.get("status")), priority=2)
            elif item_kind in {"McpToolCall", "mcp_tool_call", "DynamicToolCall", "dynamic_tool_call"}:
                name = item.get("tool") or item.get("name")
                if item.get("server"):
                    name = "mcp__" + str(item["server"]) + "__" + str(name or "unknown")
                identifier = call(identifier, name, arguments(item.get("arguments", {}), index), index, view=True)
                unclassified.add(str(name))
                if "result" in item or "output" in item:
                    result = item.get("result", item.get("output"))
                    is_error = isinstance(result, dict) and bool(result.get("isError") or result.get("is_error"))
                    output(identifier, result, index, status=tool_status(item.get("status"), is_error), priority=2)
            elif item_kind in {"CollabAgentToolCall", "SubAgentActivity"}:
                name = item.get("tool") or item.get("kind")
                identifier = call(identifier, "collaboration." + str(name or "unknown"), {}, index, view=True)
                if item.get("status") is not None:
                    output(identifier, {k: item[k] for k in ("agents_states", "receiver_thread_ids",
                                                          "agent_thread_id") if k in item}, index,
                           status=tool_status(item["status"]), priority=1)
            elif item_kind in _ITEM_TYPES:
                identifier = call(identifier, str(item_kind), {k: v for k, v in item.items()
                                  if k not in {"type", "id", "status", "result", "output"}}, index)
                effect_classes.add(str(item_kind))
                if "result" in item or "output" in item:
                    output(identifier, item.get("result", item.get("output")), index,
                           status=tool_status(item.get("status")), priority=1)
            else:
                unknown.add(str(item_kind))
                call(identifier, "unparsed_item." + str(item_kind),
                     {"unparsed_event": _safe_event(row)["payload"]["item"]}, index)
        elif row.get("type") == "event_msg" and isinstance(kind, str) and re.search(
                r"(?:tool|exec_command|apply_patch|web_search|file_change|(?:^|_)call(?:_|$))", kind):
            # An unrecognized older/newer tool-event protocol is retained and
            # rejects successful observation rather than disappearing.
            raw.append(_safe_event(row))
            unknown.add(kind)
            call(payload.get("call_id", payload.get("id")), "unparsed_event." + kind,
                 {"unparsed_event": _safe_event(row)["payload"]}, index)

    ordered = []
    for identifier, entry in calls.items():
        ordered.append((entry["index"], 0, {"role": "tool_call", "tool_call": {
            "id": thread_id + ":" + entry["id"], "name": entry["name"], "arguments": entry["arguments"]}}))
    for identifier, entry in outputs.items():
        ordered.append((entry["index"], 1, {"role": "tool_result", "tool_result": entry["result"]}))
    for entry in extra_outputs:
        ordered.append((entry["index"], 1, {"role": "tool_result", "tool_result": entry["result"]}))
    unmatched = sorted(set(calls) - set(outputs))
    orphaned = sorted(set(outputs) - set(calls))
    if unknown:
        errors.append("Unknown tool event formats: " + ", ".join(sorted(unknown)))
    if unmatched:
        errors.append("Tool calls lack observed outputs: " + ", ".join(unmatched))
    if orphaned:
        errors.append("Tool outputs lack observed calls: " + ", ".join(orphaned))
    trace = sanitize_tool_value([entry[2] for entry in sorted(ordered, key=lambda x: (x[0], x[1]))])
    raw = sanitize_tool_value(raw)
    # Reserve a third channel for the backward-compatible command artifact.
    channel_budget = max(1, max_total_bytes // 3)
    trace, trace_capture = _bounded_channel(trace, byte_budget=channel_budget, event_budget=max_event_bytes)
    raw, raw_capture = _bounded_channel(raw, byte_budget=channel_budget, event_budget=max_event_bytes)
    if trace_capture["omitted_count"] or raw_capture["omitted_count"]:
        errors.append("Tool evidence exceeded capture byte budget; omitted values are hash/length recorded")
    report = {
        "status": "complete" if not errors else "incomplete",
        "scope": "available parent-linked rollout tool events only",
        "tool_event_count": len(raw), "tool_call_count": len(calls), "tool_result_count": len(outputs),
        "unknown_event_types": sorted(unknown), "unmatched_call_ids": unmatched, "orphan_output_ids": orphaned,
        "unclassified_tools": sorted(unclassified), "observed_effect_categories": sorted(effect_classes),
        "errors": errors,
        "conflicting_call_ids": sorted(conflicting_ids),
        "capture": {"event_byte_limit": max_event_bytes, "total_byte_limit": max_total_bytes,
                    "event_count_limit": max_events, "raw": raw_capture, "transcript": trace_capture},
        "os_file_operations": "not_observed", "os_network_operations": "not_observed",
        "absence_of_side_effects_proven": False, "absence_of_external_calls_proven": False,
        "limitations": [
            "A before/after file hash cannot detect a write followed by deletion or restoration.",
            "Tool events do not observe file or network operations performed inside a process or MCP service.",
            "Tool names, readOnlyHint and declared sandbox settings do not prove read-only behavior or network isolation.",
        ],
    }
    report = sanitize_tool_value(report)
    if len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > 65536:
        report = {"status": "incomplete", "scope": "available rollout tool events only",
                  "errors": ["Native observation metadata exceeded capture budget"],
                  "omitted_report": _fingerprint(report, "metadata_byte_budget"),
                  "unclassified_tools": ["omitted observation metadata"],
                  "absence_of_side_effects_proven": False, "absence_of_external_calls_proven": False,
                  "os_file_operations": "not_observed", "os_network_operations": "not_observed"}
    # Standard system/content fields survive SessionResult serializers without
    # inventing a tool call or relying on unknown transcript fields.
    trace.append({"role": "system", "content": json.dumps(
        {"skill_refiner_native_observation": report}, ensure_ascii=False)})
    return trace, raw, report

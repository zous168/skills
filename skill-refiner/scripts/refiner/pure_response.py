"""Experimental pure-response Codex CLI preparation; never certifies absence.

The native CLI does not expose a complete, authenticated live tool registry to this
controller.  Therefore this module only builds a hash-bound candidate request and
reports a coverage gap. It is deliberately not registered as an executor and cannot
clear the plan gate. Do not treat synthetic probes as live observation evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from .common import RefinerError

# The negative probes apply to this binary only, not to a version string, alias,
# platform, later build, dynamic tool registry, or a model-hosted tool.
_PROBED_CLI_SHA256 = "444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b"
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "code_mode_host", "apps", "browser_use",
    "computer_use", "view_image", "goals", "multi_agent", "multi_agent_v2",
    "plugins", "hooks",
)
_MAX_BUNDLE_BYTES = 2_000_000
_MAX_FILES = 100


def _bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def _unsafe_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def collect_text_only_skill(target):
    """Freeze every package file, rejecting executable/non-markdown assets.

    This is conservative. Skills requiring schemas, validators or runtime files
    need a real executor and cannot be evaluated by this experimental pathway.
    """
    root = Path(target).absolute()
    if not root.is_dir() or _unsafe_link(root):
        raise RefinerError("Pure-response target must be a safe regular directory")
    rows, total = [], 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        directory = Path(directory)
        for name in sorted(dirs):
            if _unsafe_link(directory / name):
                raise RefinerError("Pure-response target contains a linked directory")
        for name in sorted(files):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            if _unsafe_link(path) or not path.is_file():
                raise RefinerError("Pure-response target contains an unsafe file: " + relative)
            if path.suffix.lower() != ".md":
                raise RefinerError("Pure-response cannot execute or interpret non-Markdown assets: " + relative)
            data = path.read_bytes()
            total += len(data)
            if len(rows) >= _MAX_FILES or total > _MAX_BUNDLE_BYTES:
                raise RefinerError("Pure-response target exceeds the bundle size limit")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RefinerError("Pure-response target is not UTF-8: " + relative) from exc
            rows.append({"path": relative, "sha256": _bytes_hash(data), "bytes": len(data), "text": text})
    rows.sort(key=lambda row: row["path"])
    if not any(row["path"] == "SKILL.md" for row in rows):
        raise RefinerError("Pure-response target has no SKILL.md")
    return rows


def prepare(executable, target, request, *, model, reasoning_effort=None):
    """Return bounded request material; do not execute or certify isolation."""
    binary = Path(executable).absolute()
    if not binary.is_file() or _unsafe_link(binary):
        raise RefinerError("Pure-response CLI must be a safe regular executable file")
    binary_hash = _bytes_hash(binary.read_bytes())
    if binary_hash != _PROBED_CLI_SHA256:
        raise RefinerError("Pure-response CLI hash differs from the tested binary")
    if not isinstance(request, str) or not request or len(request.encode("utf-8")) > _MAX_BUNDLE_BYTES:
        raise RefinerError("Pure-response request must be a bounded non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise RefinerError("Pure-response model must be explicit")
    bundle = collect_text_only_skill(target)
    manifest = [{key: row[key] for key in ("path", "sha256", "bytes")} for row in bundle]
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = json.dumps({"package": bundle, "task": request}, ensure_ascii=False, separators=(",", ":"))
    argv = [str(binary), "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--ephemeral", "--sandbox", "read-only", "--color", "never", "--json"]
    for feature in _DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    argv.extend(["-c", 'web_search="disabled"', "-c", "mcp_servers={}", "--model", model])
    if reasoning_effort is not None:
        if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise RefinerError("Unsupported pure-response reasoning effort")
        argv.extend(["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
    argv.append("-")
    return {"status": "coverage_gap", "cli_sha256": binary_hash, "argv": argv,
            "bundle_manifest": manifest, "bundle_manifest_sha256": _bytes_hash(manifest_bytes),
            "payload": payload, "payload_sha256": _bytes_hash(payload.encode("utf-8")),
            "limitations": ["No authenticated live tool registry is exposed to this controller.",
                            "Offline malicious-tool probes do not prove all live tool routes are disabled.",
                            "This mode does not execute target validators or target scripts."]}


def classify_stdout(stdout, stderr, exit_code):
    """Reject observed tool activity; absence of observed activity is not proof."""
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise RefinerError("Pure-response event stream is not JSONL") from exc
        if not isinstance(item, dict):
            raise RefinerError("Pure-response event is not an object")
        events.append(item)
    allowed = {"thread.started", "turn.started", "turn.completed", "item.completed"}
    for event in events:
        if event.get("type") not in allowed:
            raise RefinerError("Pure-response has an unexpected or tool event")
        if event["type"] == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message" or not isinstance(item.get("text"), str):
                raise RefinerError("Pure-response has a non-message output item")
    types = [event["type"] for event in events]
    if (exit_code != 0 or types.count("thread.started") != 1 or types.count("turn.started") != 1
            or types.count("turn.completed") != 1 or types.count("item.completed") != 1
            or not types.index("thread.started") < types.index("turn.started")
            < types.index("item.completed") < types.index("turn.completed")):
        raise RefinerError("Pure-response terminal stream is incomplete")
    # A rejected function call can be absent from stdout but present in CLI router diagnostics.
    if any(token in stderr.lower() for token in ("error", "unsupported call", "tool call", "tool_call")):
        raise RefinerError("Pure-response CLI reported an error or attempted tool call")
    return {"status": "coverage_gap", "absence_of_side_effects_proven": False,
            "absence_of_external_calls_proven": False,
            "reason": "A clean CLI event stream does not prove that all tool routes were disabled.",
            "final_message": next(event["item"]["text"] for event in events if event["type"] == "item.completed")}

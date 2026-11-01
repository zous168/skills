"""Manually run deterministic local Codex CLI tool-routing controls.

Usage: python tests/probe_pure_response_cli.py /path/to/codex.exe [model]
Uses a loopback fake provider, no real model or credentials. A denying proxy
rejects network forwarding; this is not a live isolation proof.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from refiner.pure_response import _DISABLED_FEATURES, _PROBED_CLI_SHA256


def events(item, model):
    rid = "resp_fixture"
    if item["type"] == "function_call":
        added = {**item, "arguments": ""}
        middle = [{"type": "response.function_call_arguments.delta", "item_id": item["id"],
                   "output_index": 0, "delta": item["arguments"]},
                  {"type": "response.function_call_arguments.done", "item_id": item["id"],
                   "output_index": 0, "arguments": item["arguments"]}]
    elif item["type"] == "custom_tool_call":
        added, middle = item, []
    else:
        added = {**item, "content": []}
        text = item["content"][0]["text"]
        middle = [{"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
                   "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                  {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                   "content_index": 0, "delta": text},
                  {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
                   "content_index": 0, "text": text}]
    response = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                "model": model, "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    return [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": added}, *middle,
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response}]


def run_one(executable, root, name, model, control=False):
    case = root / ("control" if control else name)
    case.mkdir()
    home, work = case / "home", case / "work"
    home.mkdir(); work.mkdir()
    marker = (case / "marker.txt").as_posix()
    ledger = []
    command = 'cmd.exe /c echo harmless > "' + marker + '"'
    args = {"cmd": command, "command": command}
    if name == "spawn_agent":
        args = {"message": "Synthetic local test. Do not run tools."}
    elif name == "request_user_input":
        args = {"questions": [{"id": "q", "header": "fixture", "question": "offline?",
                               "options": [{"label": "yes", "description": "fixture"},
                                           {"label": "no", "description": "fixture"}]}]}
    if name == "custom_patch":
        attack = {"type": "custom_tool_call", "id": "fc", "call_id": "call", "status": "completed",
                  "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: marker.txt\n+x\n*** End Patch"}
    else:
        attack = {"type": "function_call", "id": "fc", "call_id": "call", "status": "completed",
                  "name": name, "arguments": json.dumps(args)}
    final = {"type": "message", "id": "msg", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_CONNECT(self):
            self.send_error(502, "proxy refuses external forwarding")
        def do_GET(self):
            self.send_error(502, "unexpected request")
        def do_POST(self):
            if self.path != "/v1/responses":
                self.send_error(502, "unexpected request")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(body)
            tools = request.get("tools", [])
            additional = []
            def find_additional(value):
                if isinstance(value, dict):
                    if value.get("type") == "additional_tools":
                        additional.append(value)
                    for entry in value.values():
                        find_additional(entry)
                elif isinstance(value, list):
                    for entry in value:
                        find_additional(entry)
            def names(values, prefix=""):
                found = []
                for row in values:
                    if not isinstance(row, dict):
                        continue
                    name = row.get("name", "")
                    if row.get("type") == "namespace":
                        found.extend(names(row.get("tools", []), prefix + name + "."))
                    elif name:
                        found.append(prefix + name)
                return found
            find_additional(request.get("input", []))
            ledger.append({"tool_count": len(tools), "tool_names": names(tools),
                           "additional_tool_names": [name for item in additional
                                                     for name in names(item.get("tools", []))]})
            item = attack if len(ledger) == 1 else final
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for row in events(item, model):
                self.wfile.write(("event: " + row["type"] + "\ndata: " + json.dumps(row) + "\n\n").encode())
                self.wfile.flush()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = "http://127.0.0.1:%d/v1" % server.server_port
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(CODEX_HOME=str(home), HTTP_PROXY=url, HTTPS_PROXY=url, ALL_PROXY=url,
               NO_PROXY="127.0.0.1,localhost", http_proxy=url, https_proxy=url,
               all_proxy=url, no_proxy="127.0.0.1,localhost")
    argv = [str(executable), "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--ephemeral", "--sandbox", ("danger-full-access" if control else "read-only"), "--json"]
    if not control:
        for feature in _DISABLED_FEATURES:
            argv += ["--disable", feature]
    config = {"model_provider": "fixture", "model_providers.fixture.name": "Local fixture",
              "model_providers.fixture.base_url": url, "model_providers.fixture.wire_api": "responses",
              "model_providers.fixture.requires_openai_auth": False,
              "model_providers.fixture.request_max_retries": 0,
              "model_providers.fixture.stream_max_retries": 0,
              "web_search": "disabled", "apps._default.enabled": False, "mcp_servers": {},
              "analytics.enabled": False}
    for key, value in config.items():
        argv += ["-c", key + "=" + json.dumps(value)]
    argv += ["--model", model, "-"]
    try:
        result = subprocess.run(argv, cwd=work, env=env, input=b"Synthetic local fixture",
                                capture_output=True, timeout=25)
    finally:
        server.shutdown(); server.server_close(); thread.join()
    stderr = result.stderr.decode("utf-8", "replace")
    wrote = (case / "marker.txt").exists() or (work / "marker.txt").exists()
    rejected = ("unsupported call:" in stderr or "unsupported custom tool call:" in stderr
                or "request_user_input is unavailable" in stderr)
    ok = result.returncode == 0 and bool(ledger) and (wrote if control else (not wrote and rejected))
    return {"control": control, "tool": name, "ok": ok, "marker_created": wrote,
            "sandbox": "danger-full-access" if control else "read-only",
            "request_tool_names": ledger[0]["tool_names"] if ledger else [],
            "additional_tool_names": ledger[0]["additional_tool_names"] if ledger else [],
            "router_diagnostics": [line for line in stderr.splitlines()
                                   if "codex_core::tools::router" in line],
            "router_rejected": rejected, "exit_code": result.returncode}


def main():
    if len(sys.argv) not in (2, 3) or os.name != "nt":
        raise SystemExit("Supply the tested Windows Codex executable path")
    exe = Path(sys.argv[1]).resolve(strict=True)
    model = sys.argv[2] if len(sys.argv) == 3 else "fixture-model"
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    if digest != _PROBED_CLI_SHA256:
        raise SystemExit("Executable hash differs from the pinned offline probe")
    with tempfile.TemporaryDirectory(prefix="refiner-tool-control-") as tmp:
        root = Path(tmp)
        names = ["exec_command", "shell_command", "apply_patch", "spawn_agent",
                 "mcp__local__x", "custom_patch", "request_user_input"]
        results = [run_one(exe, root, "exec_command", model, control=True)]
        results += [run_one(exe, root, name, model) for name in names]
        output = {"scope": "loopback fake provider; no real model or credentials; not live isolation proof",
                  "executable_sha256": digest, "model": model, "cases": results,
                  "passed": all(row["ok"] for row in results)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

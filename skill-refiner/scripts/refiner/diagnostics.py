"""Read-only availability checks, deliberately distinct from live verification."""
import os
import shutil

from .common import run_process
from .profiles import load_config
from .runtime import executable


def inspect_config(path, *, launch_context=None):
    config = load_config(path)
    rows = []
    profiles = [("agent:" + role, p) for role, p in config["agents"].items()]
    profiles += [("executor:" + p["id"], p) for p in config["executors"]]
    for role, profile in profiles:
        backend = profile["backend"]
        row = {"profile": role, "backend": backend, "model": profile.get("model", "local-default"),
               "ready": True, "verification": "not_live_tested"}
        row["declared_settings"] = {key: profile[key] for key in
                                    ("model", "reasoning_effort", "timeout", "codex_sandbox") if key in profile}
        row["effective_configuration"] = "not_resolved"
        row["unresolved_inherited_fields"] = [key for key in ("model", "reasoning_effort")
                                               if backend in {"codex", "codex_subagent"} and not profile.get(key)]
        try:
            if backend == "openai_compatible":
                key = profile.get("api_key_env")
                if key and not os.environ.get(key):
                    raise RuntimeError("Missing credential environment variable: " + key)
                row["note"] = "Credentials configured; endpoint/model availability requires a real run"
            elif backend == "command":
                command = profile["command"][0]
                if not command or shutil.which(command) is None:
                    raise RuntimeError("Command executable is unavailable or not executable: " + repr(command))
                row["note"] = "Command executable available; use compare/run to verify its protocol"
            elif backend == "skillup":
                row["note"] = "User-provided adapter; use compare/run to verify its protocol"
            else:
                name = {"cursor": "cursor-agent", "claude_code": "claude"}.get(backend, "codex")
                binary = executable(profile, name)
                code, stdout, stderr, _ = run_process([binary, "--version"], timeout=15)
                if code:
                    raise RuntimeError("Executable version check failed")
                row["version"] = stdout.strip()
                env = os.environ.copy()
                env["NO_OPEN_BROWSER"] = "1"
                auth = {"cursor": [binary, "status"], "claude_code": [binary, "auth", "status"]}.get(backend, [binary, "login", "status"])
                code, stdout, stderr, _ = run_process(auth, timeout=20, env=env)
                text = ((stdout or "") + (stderr or "")).lower()
                if code or "not logged in" in text or '\"loggedin\": false' in text:
                    raise RuntimeError("Backend is not authenticated")
                row["authenticated"] = True
        except Exception as exc:
            row.update(ready=False, error=str(exc))
        rows.append(row)
    from .preflight import _configuration_origin
    return {"ready": all(r["ready"] for r in rows), "profiles": rows,
            "configuration_origin": _configuration_origin(launch_context),
            "note": "Availability checks do not prove model execution or Skill compatibility"}

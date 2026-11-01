#!/usr/bin/env python3
"""Run an installed Skill with its provisioned interpreter and local defaults."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    skill = Path(__file__).resolve().parent.parent
    settings = skill / "references" / "local-runtime.json"
    if not settings.is_file():
        raise ValueError("Installed runtime is missing: " + str(settings)
                         + ". In a source checkout, use its .venv and scripts/refine.py.")
    runtime = json.loads(settings.read_text(encoding="utf-8"))
    if runtime.get("schema_version") != 1:
        raise ValueError("Unsupported installed runtime schema")
    python = Path(runtime["python"])
    skill_up = Path(runtime["skill_up"])
    config = Path(runtime["config"])
    for path in (python, skill_up, config):
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Installed dependency missing: " + str(path))
    if not os.access(python, os.X_OK) or not os.access(skill_up, os.X_OK):
        raise ValueError("Installed interpreter or skill-up is not executable")
    args = sys.argv[1:]
    command = args[0] if args else None
    supplied = {arg.split("=", 1)[0] for arg in args[1:] if arg.startswith("--")}
    launch_context = {"launch_manifest_path": str(settings),
                      "configured_defaults": dict(runtime.get("limits", {})),
                      "applied_defaults": {}, "explicit_flags": sorted(supplied)}
    legacy = {"--engine", "--engine-config", "--agent-command"}
    if command in {"run", "create", "compare", "doctor"} and not supplied.intersection(legacy | {"--config"}):
        args += ["--config", str(config)]
    if command in {"run", "create", "compare"} and "--skill-up" not in supplied:
        args += ["--skill-up", str(skill_up)]
    if command in {"run", "create", "compare"}:
        for name, value in runtime.get("limits", {}).items():
            if command == "compare" and name not in {"timeout", "max_seconds", "max_evaluations"}:
                continue
            flag = "--" + name.replace("_", "-")
            if flag not in supplied:
                args += [flag, str(value)]
                launch_context["applied_defaults"][name] = value
    if command == "bootstrap" and "--directory" not in supplied:
        args += ["--directory", str(skill / ".runtime" / "bin")]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Informational provenance only: the CLI still validates all actual flags.
    env["SKILL_REFINER_LAUNCH_CONTEXT"] = json.dumps(launch_context, ensure_ascii=False)
    # User profiles contain explicit CLI paths. Also support the legacy doctor's
    # PATH lookup without requiring shell initialization or changing global PATH.
    env["PATH"] = os.pathsep.join([*runtime.get("path_entries", []), str(skill_up.parent),
                                  env.get("PATH", "")])
    command = [str(python), str(skill / "scripts" / "refine.py"), *args]
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("skill-refiner launcher: " + str(exc), file=sys.stderr)
        raise SystemExit(2)

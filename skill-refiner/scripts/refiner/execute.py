"""skill-up local transport entrypoint; receives no grader or expected answer."""
from pathlib import Path
import json
import sys
import hashlib

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refiner.common import RefinerError, read_json, write_json, relative_path, run_process
from refiner.runtime import execute
from refiner.evidence import archive_diagnostics, workspace_paths


def verify_skill(root, expected):
    for name, sha in expected.items():
        path = relative_path(root, name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise RefinerError("Installed Skill source changed or does not match candidate: " + name)


def main():
    profile, request = read_json(sys.argv[1]), read_json(sys.argv[2])
    output = Path(sys.argv[3])
    cwd = Path.cwd()
    skill_root = None
    result = None
    before_paths = None
    helpers = {}
    stage = "installed_source_check"
    try:
        skills = sorted(cwd.rglob("SKILL.md"))
        if len(skills) != 1:
            raise RuntimeError(f"Expected exactly one installed Skill; found {len(skills)}")
        expected = profile.get("_skill_manifest", {})
        skill_root = skills[0].parent
        verify_skill(skill_root, expected)
        helpers = {name: sha for name, sha in profile.get('_helper_manifest', {}).items() if (cwd / name).exists()}
        verify_skill(cwd, helpers)
        if profile["backend"] == "command":
            # External adapters implement the existing skill-up file protocol.
            argv = [part.replace("{input_file}", str(Path(sys.argv[2]).resolve())).replace(
                    "{output_file}", str(output.resolve())) for part in profile["command"]]
            code, stdout, stderr, _ = run_process(argv, cwd=cwd, timeout=request.get("timeout_seconds"))
            if code or not output.exists():
                raise RuntimeError(f"Command adapter failed (exit {code}) or did not write SessionResult")
            verify_skill(skill_root, expected)
            verify_skill(cwd, helpers)
            return 0
        prompt = ("Use the installed Skill at this authoritative absolute root: " + str(skill_root.resolve()) +
                  ". Resolve its script and reference paths under this root and invoke this copy. "
                  "Do not substitute a project checkout, global installation, or another discovered copy. "
                  "The task workspace is " + str(cwd.resolve()) + ". Task input/output paths are relative "
                  "to that workspace unless the task specifies otherwise.\nInstalled Skill files:\n")
        prompt += json.dumps([{"path": str(s.resolve()), "content": s.read_text(encoding="utf-8")} for s in skills])
        prompt += "\nTask messages:\n" + json.dumps(request["messages"], ensure_ascii=False)
        if helpers:
            prompt += (
                '\nMeasurement protocol for the supplied frozen Python observer: Invoke refiner-python-probe.py '
                'directly through the native shell/command tool with concrete arguments visible in that command. '
                'Do not hide observer calls inside Python subprocess.run, Python heredocs, generated wrappers, '
                'or shell loops with variable argument names. For batches, issue explicit concrete command lines '
                '(one tool call may contain several lines). The grader observes actual native command strings. '
                'The native tool already captures process status/stdout/stderr; the observer records business '
                'exit status in its evidence file. Do not print repr(), debug dictionaries, labels, status lines '
                'or encoded stdout strings. Forward complete business stdout as received. Preserve Unicode values; '
                'do not double-escape JSON backslashes. Read/install instructions separately from execution when '
                'their output would otherwise contaminate business stdout. Do not modify inputs, the frozen '
                'observer or installed implementation. These are measurement directions, not business repairs.')
        stage = "execution"
        before_paths = workspace_paths(cwd)
        result = execute(profile, prompt, cwd, request.get("timeout_seconds"),
                         target_evaluation=profile["backend"] in {"codex", "codex_subagent"})
        stage = "post_execution_source_check"
        verify_skill(skill_root, expected)
        verify_skill(cwd, helpers)
    except Exception as exc:
        # Completed backend events may be the only copy of the failure evidence.
        # Keep them while the failed source check still makes the session ERROR.
        if not isinstance(result, dict):
            result = {"transcript": []}
        result.update(exit_code=1, final_message="")
        result["stderr"] = "\n".join(s for s in (result.get("stderr", ""), str(exc)) if s)
        result.setdefault("metadata", {}).update(backend=profile["backend"], error=str(exc), failure_stage=stage)
    if skill_root is not None:
        result.setdefault("metadata", {})["installed_skill_root"] = str(skill_root.resolve())
    if before_paths is not None:
        try:
            result.setdefault("artifacts", {}).setdefault("files", []).append(archive_diagnostics(cwd, before_paths))
        except (OSError, ValueError) as exc:
            result.setdefault("metadata", {})["diagnostic_archive_error"] = str(exc)
    # Upstream retains metadata only as an explicit structured artifact.
    result.setdefault("artifacts", {}).setdefault("files", []).append(
        {"name": "adapter-metadata.json", "content": json.dumps(result.pop("metadata", {}))})
    write_json(output, result)
    return 0  # SessionResult.exit_code is authoritative; upstream reports ERROR.


if __name__ == "__main__":
    raise SystemExit(main())

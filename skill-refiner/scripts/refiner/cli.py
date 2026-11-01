from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .bootstrap import PROJECT_ROOT, install
from .common import RefinerError, run_process
from .standards import lint

MAX_REPAIR_ROUNDS = 3
MAX_PLANNING_ATTEMPTS = 4
MAX_WALL_CLOCK_SECONDS = 7200
MAX_COMPARE_SECONDS = 3600
MAX_AGENT_CALLS = 128
MAX_EVALUATIONS = 200


def workflow_options(parser, *, creating=False):
    parser.add_argument("--spec", action="append", default=[], help="Independent business requirements (repeatable)")
    parser.add_argument("--standard", action="append", default=[],
                        help="Additional standards; project standards are auto-discovered (repeatable)")
    parser.add_argument("--output", help="Evidence workspace; must be new for fresh runs and match the resume path otherwise")
    parser.add_argument("--config", help="Independent analysis/creation/planning/repair/review and regression backends")
    parser.add_argument("--skill-up", help="Explicit skill-up executable")
    parser.add_argument("--engine", choices=["codex", "claude_code"],
                        help="Legacy skill-up engine; select explicitly when not using --config")
    parser.add_argument("--engine-config", help="Legacy skill-up engine JSON")
    parser.add_argument("--agent-command", help="Legacy controller JSON stdin/stdout command")
    parser.add_argument("--max-rounds", type=int, default=2,
                        help="Repair rounds; default 2, hard cap 3")
    parser.add_argument("--planning-attempts", type=int, default=4 if creating else 2,
                        help="Bounded plan/review revisions per development or acceptance suite")
    parser.add_argument("--agent-parallelism", type=int, choices=(1, 2), default=2,
                        help="Concurrent controller Agents for the verified planning wave (1 disables concurrency)")
    parser.add_argument("--max-cases", type=int, default=8 if creating else 4)
    parser.add_argument("--acceptance-cases", type=int, default=6 if creating else 2)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=None,
                        help="Optional per-call fallback timeout; the run deadline is the default bound")
    parser.add_argument("--max-seconds", type=int, default=1800 if creating else 1200,
                        help="Total wall-clock budget; hard cap 7200 seconds")
    parser.add_argument("--max-agent-calls", type=int, default=24 if creating else 16)
    parser.add_argument("--max-evaluations", type=int, default=80 if creating else 40,
                        help="Case-executor evaluations; hard cap 200")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autonomous Skill planning, repair and real skill-up regression")
    subs = parser.add_subparsers(dest="command", required=True)
    boot = subs.add_parser("bootstrap", help="Install verified skill-up v0.13.0 locally")
    boot.add_argument("--directory")
    doctor = subs.add_parser("doctor", help="Check local agent and evaluation dependencies")
    doctor.add_argument("--config", help="Inspect every configured backend without making model calls")
    check = subs.add_parser("check", help="Run deterministic Skill format/resource/script checks")
    check.add_argument("target")
    verify = subs.add_parser("verify", help="Verify that a run passed every required pipeline gate")
    verify.add_argument("run", help="Completed run workspace created by `run` or `create`")
    execute = subs.add_parser("run", help="Plan, evaluate, repair, regress and independently accept a Skill")
    execute.add_argument("target")
    workflow_options(execute)
    execute.add_argument("--reuse-plan", help="Reuse frozen autonomous plans from an earlier run; rerun baseline and all verification")
    execute.add_argument("--resume-from", help="Resume this run workspace, reusing validated completed calls and evaluations")
    execute.add_argument("--plan-only", action="store_true",
                         help="Freeze and display the reviewed plan, then stop before running target tests")
    create = subs.add_parser("create", help="Analyze a business request, implement a Skill and run the full refinement pipeline")
    create.add_argument("description", nargs="?", help="Natural-language business request; --spec files may also supply it")
    create.add_argument("--name", help="Optional kebab-case Skill name; otherwise chosen during analysis")
    create.add_argument("--resume-from", help="Start a new creation from an approved contract and validated draft")
    create.add_argument("--resume-run", help="Continue an interrupted creation workspace in place")
    create.add_argument("--plan-only", action="store_true",
                        help="Create the reviewed design and frozen test checklist, then stop before target tests")
    create.add_argument("--destination", help="New destination populated only after successful multi-engine acceptance")
    workflow_options(create, creating=True)
    migrate = subs.add_parser("import-create-inputs", help="Validate and import only a completed create inputs checkpoint into a new workspace")
    migrate.add_argument("--source-run", required=True)
    migrate.add_argument("--controller-archive", help="Exact source controller archive; required unless revalidating the completed checkpoint with the current controller")
    migrate.add_argument("--revalidate-source-checkpoint", action="store_true",
                         help="When the exact source controller archive is unavailable, revalidate the completed inputs checkpoint and carried evidence with the current controller; uncompleted stages are not trusted")
    migrate.add_argument("--process-reconciliation", help="External evidence of old call termination and a process check")
    migrate.add_argument("--supplement", help="An explicit new UTF-8 requirements file to append after validating old inputs")
    migrate.add_argument("--supplement-source", help="Origin of the user's explicit supplementary business decision")
    migrate.add_argument("--carry-analysis-feedback", action="store_true", help="Carry verified, rejected analysis and independent-review evidence to the continuation")
    migrate.add_argument("--carry-technical-clarification", action="store_true", help="Also carry a verified unfinished clarification chain for technical triage")
    migrate.add_argument("--prior-seconds", type=float, required=True)
    migrate.add_argument("--prior-agent-calls", type=int, required=True)
    migrate.add_argument("--prior-evaluations", type=int, required=True)
    workflow_options(migrate, creating=True)
    compare = subs.add_parser("compare", help="Run one frozen suite against all configured engines; no planning or repair")
    compare.add_argument("target")
    compare.add_argument("--suite", required=True, help="Earlier run directory containing locked plans")
    compare.add_argument("--config", required=True)
    compare.add_argument("--output")
    compare.add_argument("--skill-up")
    compare.add_argument("--parallelism", type=int, default=2)
    compare.add_argument("--timeout", type=int, default=None,
                         help="Optional per-call fallback timeout; the comparison deadline is the default bound")
    compare.add_argument("--max-seconds", type=int, default=1200)
    compare.add_argument("--max-evaluations", type=int, default=40)
    args = parser.parse_args(argv)
    try:
        args.launch_context = json.loads(os.environ.get("SKILL_REFINER_LAUNCH_CONTEXT", "{}"))
        if not isinstance(args.launch_context, dict):
            raise RefinerError("Invalid launcher configuration provenance")
        if args.command == "bootstrap":
            print(install(args.directory))
            return 0
        if args.command == "doctor":
            if args.config:
                from .diagnostics import inspect_config
                result = inspect_config(args.config, launch_context=args.launch_context)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["ready"] else 1
            from .skillup import locate
            try:
                up = locate()
            except RefinerError:
                up = None
            result = {"python": sys.executable, "skill_up": up, "project": str(PROJECT_ROOT),
                      "backend_config_required": True,
                      "note": "No provider is selected by default. Pass --config to check configured backends; "
                              "workflows require --config or an explicit legacy adapter."}
            if up:
                code, output, _, _ = run_process([up, "--version"], timeout=15)
                result["skill_up_version"] = output.strip()
                result["skill_up_works"] = code == 0
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("skill_up_works") else 1
        if args.command == "check":
            findings = lint(Path(args.target).resolve())
            print(json.dumps(findings, ensure_ascii=False, indent=2))
            return int(any(f["severity"] == "error" for f in findings))
        if args.command == "verify":
            from .gate import verify_run
            result = verify_run(args.run)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "compare":
            for key in ("parallelism", "timeout", "max_seconds", "max_evaluations"):
                if getattr(args, key) < 1:
                    raise RefinerError(f"--{key.replace('_', '-')} must be positive")
            if args.max_seconds > MAX_COMPARE_SECONDS or args.max_evaluations > MAX_EVALUATIONS:
                raise RefinerError(f"compare is bounded to {MAX_COMPARE_SECONDS} seconds and 200 case-executor evaluations")
            from .compare import run as compare_run
            return compare_run(args)
        for key in ("max_rounds", "planning_attempts", "parallelism", "timeout", "max_seconds", "max_agent_calls", "max_evaluations"):
            value = getattr(args, key)
            if value is None and key == "timeout":
                continue
            if value < 1:
                raise RefinerError(f"--{key.replace('_', '-')} must be positive")
        if args.max_rounds > MAX_REPAIR_ROUNDS:
            raise RefinerError(f"--max-rounds cannot exceed {MAX_REPAIR_ROUNDS}")
        if args.planning_attempts > MAX_PLANNING_ATTEMPTS:
            raise RefinerError(f"--planning-attempts cannot exceed {MAX_PLANNING_ATTEMPTS}")
        if args.max_seconds > MAX_WALL_CLOCK_SECONDS:
            raise RefinerError(f"--max-seconds cannot exceed {MAX_WALL_CLOCK_SECONDS}")
        if args.max_agent_calls > MAX_AGENT_CALLS:
            raise RefinerError(f"--max-agent-calls cannot exceed {MAX_AGENT_CALLS}")
        if args.max_evaluations > MAX_EVALUATIONS:
            raise RefinerError(f"--max-evaluations cannot exceed {MAX_EVALUATIONS}")
        if not 2 <= args.max_cases <= 30 or not 2 <= args.acceptance_cases <= 30:
            raise RefinerError("Case limits must be between 2 and 30")
        if args.command == "import-create-inputs":
            from .checkpoint_migration import import_inputs
            result = import_inputs(args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "create":
            from .creation import run
        else:
            from .pipeline import run
        state = run(args)
        print("Status: " + str(state["status"]))
        print("Work plan: " + str(Path(state["workspace"]) / "work-plan.md"))
        print("Run state: " + str(Path(state["workspace"]) / "state.json"))
        print("Stage manifest: " + str(Path(state["workspace"]) / "stages.json"))
        print("Report: " + str(Path(state["workspace"]) / "report.md"))
        return 0 if state["status"] in {"passed", "unchanged", "created", "planned"} else 2
    except (RefinerError, OSError, ValueError) as exc:
        print(f"skill-refiner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


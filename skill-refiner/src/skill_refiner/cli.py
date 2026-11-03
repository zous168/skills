from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .errors import RefinerError
from .service import adopt_run, create_run, doctor, refine_run, resume_run, status_run, verify_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-refiner")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "refine"):
        item = sub.add_parser(name)
        if name == "refine":
            item.add_argument("--source", type=Path, required=True)
        item.add_argument("--requirements", type=Path, required=True)
        item.add_argument("--config", type=Path, required=True)
        item.add_argument("--output-root", type=Path, required=True)
        item.add_argument("--plan-only", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("run_dir", type=Path)
    for name in ("status", "verify"):
        item = sub.add_parser(name)
        item.add_argument("run_dir", type=Path)
    adopt = sub.add_parser("adopt")
    adopt.add_argument("run_dir", type=Path)
    adopt.add_argument("destination", type=Path)
    check = sub.add_parser("doctor")
    check.add_argument("--config", type=Path)
    return parser


def _dump(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            result = asyncio.run(create_run(args.requirements, args.config, args.output_root, plan_only=args.plan_only))
        elif args.command == "refine":
            result = asyncio.run(refine_run(args.source, args.requirements, args.config, args.output_root, plan_only=args.plan_only))
        elif args.command == "resume":
            result = asyncio.run(resume_run(args.run_dir))
        elif args.command == "status":
            result = status_run(args.run_dir)
        elif args.command == "verify":
            result = verify_run(args.run_dir)
        elif args.command == "adopt":
            result = adopt_run(args.run_dir, args.destination)
        else:
            result = doctor(args.config)
        _dump(result)
        return 0
    except RefinerError as exc:
        _dump({"error": exc.code, "message": str(exc), "details": exc.details})
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())

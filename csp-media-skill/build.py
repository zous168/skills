#!/usr/bin/env python3
"""Build and verify the installable CSP Media Agent Skill in dist/."""
from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path
import shutil
import tempfile


PROJECT = Path(__file__).resolve().parent
SOURCE = PROJECT / "skill-src"
DIST_ROOT = PROJECT / "dist"
TARGET = DIST_ROOT / "csp-media"
REQUIRED = (
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("references/client-contract.md"),
    Path("references/verification.md"),
    Path("scripts/csp_media.py"),
)


def validate_source() -> list[Path]:
    if not SOURCE.is_dir():
        raise SystemExit(f"Skill source directory is missing: {SOURCE}")
    missing = [str(path) for path in REQUIRED if not (SOURCE / path).is_file()]
    if missing:
        raise SystemExit("Skill source is incomplete: " + ", ".join(missing))
    skill_text = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
    if not skill_text.startswith("---\n") or "\nname: csp-media\n" not in skill_text:
        raise SystemExit("skill-src/SKILL.md must declare the csp-media Agent Skill name")
    files = sorted(path.relative_to(SOURCE) for path in SOURCE.rglob("*") if path.is_file())
    if not files:
        raise SystemExit("Skill source directory contains no files")
    return files


def differs(files: list[Path]) -> list[str]:
    if not TARGET.is_dir():
        return ["dist/csp-media is missing"]
    source_files = set(files)
    target_files = {path.relative_to(TARGET) for path in TARGET.rglob("*") if path.is_file()}
    issues = [f"missing from dist: {path}" for path in sorted(source_files - target_files)]
    issues.extend(f"unexpected in dist: {path}" for path in sorted(target_files - source_files))
    issues.extend(f"content differs: {path}" for path in sorted(source_files & target_files)
                  if not filecmp.cmp(SOURCE / path, TARGET / path, shallow=False))
    return issues


def build(files: list[Path]) -> None:
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".csp-media-build-", dir=DIST_ROOT) as temporary:
        staged = Path(temporary) / "csp-media"
        shutil.copytree(SOURCE, staged)
        backup = DIST_ROOT / ".csp-media-previous"
        if backup.exists():
            shutil.rmtree(backup)
        had_target = TARGET.exists()
        if had_target:
            os.replace(TARGET, backup)
        try:
            os.replace(staged, TARGET)
        except Exception:
            if had_target and backup.exists():
                os.replace(backup, TARGET)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    print(f"Built {TARGET.relative_to(PROJECT)} ({len(files)} files)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify dist matches skill-src without modifying it")
    args = parser.parse_args()
    files = validate_source()
    if args.check:
        issues = differs(files)
        if issues:
            print("dist/csp-media is out of date:")
            for issue in issues:
                print(f"- {issue}")
            return 1
        print(f"dist/csp-media matches skill-src ({len(files)} files)")
        return 0
    build(files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

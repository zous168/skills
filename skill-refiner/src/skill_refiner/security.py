from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

from .errors import InputError
from .hashing import digest_file


TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".sh", ".ps1", ".cmd", ".bat", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".svg", ".csv", ".xml",
}
PROTECTED_PREFIXES = (".refiner/", ".git/", ".runs/", "run/")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(https?://[^:/\s]+:)([^@/\s]+)@"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
]


def redact(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda match: match.group(1) + "[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\x00" in value or "\\" in value:
        raise InputError(f"unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise InputError(f"unsafe relative path: {value!r}")
    normalized = path.as_posix()
    if normalized.casefold().startswith(PROTECTED_PREFIXES):
        raise InputError(f"protected path cannot be modified: {normalized}")
    if any(part.rstrip(" .").split(".", 1)[0].upper() in WINDOWS_RESERVED or part != part.rstrip(" .") for part in path.parts):
        raise InputError(f"unsafe Windows path: {value!r}")
    return path


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def ensure_safe_tree(root: Path) -> None:
    if not root.is_dir() or _is_reparse_point(root):
        raise InputError(f"unsafe or missing directory: {root}")
    root = root.resolve()
    for path in root.rglob("*"):
        if _is_reparse_point(path):
            raise InputError(f"links and reparse points are forbidden: {path}")
        if path.is_file():
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise InputError(f"binary or unsupported file type: {path}")
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise InputError(f"non UTF-8 file is forbidden: {path}") from exc


def tree_manifest(root: Path) -> dict[str, str]:
    ensure_safe_tree(root)
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest_file(path)
    return result


def safe_copy_tree(source: Path, destination: Path) -> None:
    ensure_safe_tree(source)
    if destination.exists():
        raise InputError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    for relative in tree_manifest(source):
        source_file = source / Path(relative)
        target_file = destination / Path(relative)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)


def sanitized_environment(allowed: tuple[str, ...], credential_names: tuple[str, ...]) -> dict[str, str]:
    names = set(allowed) | set(credential_names)
    return {name: os.environ[name] for name in names if name in os.environ}


from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, get_args

import jsonschema
import yaml

from .models import AdapterProfile
from .stage_contracts import STAGES, controller_contract_hash, registry


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
IDENTITY = "skill-refiner"
CONTRACT_VERSION = 1
EXPECTED_STAGE_IDS = (
    "freeze_inputs",
    "analyze_requirements",
    "build_plan",
    "review_plan",
    "plan_gate",
    "capture_baseline",
    "generate_candidate",
    "static_validation",
    "development_evaluation",
    "repair_candidate",
    "regression_evaluation",
    "acceptance_evaluation",
    "final_verify",
    "prepare_delivery",
)
EXPECTED_PACKAGED_SCHEMA_PATHS = (
    "schemas/candidate-proposal.schema.json",
    "schemas/config.schema.json",
    "schemas/v1/stage-artifacts.schema.json",
    "schemas/v1/stage-attempt.schema.json",
    "schemas/v1/stage-contracts.schema.json",
    "schemas/v1/stages.schema.json",
)
REQUIRED_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "README.md",
    "pyproject.toml",
    "scripts/refiner.py",
    "src/skill_refiner/cli.py",
    "src/skill_refiner/kernel.py",
    "src/skill_refiner/self_architecture.py",
    "src/skill_refiner/stage_contracts.py",
    "src/skill_refiner/store.py",
    "src/skill_refiner/workflow.py",
    "references/architecture.md",
    "references/persistence.md",
)
_TEXT_SUFFIXES = {
    ".cfg", ".ini", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml",
}
_TRANSIENT_DIRS = {".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "__pycache__"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(relative: str) -> str:
    return _sha256_bytes((PACKAGE_ROOT / relative).read_bytes())


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def packaged_schema_relative_paths() -> tuple[str, ...]:
    return tuple(path.relative_to(PACKAGE_ROOT).as_posix() for path in sorted((PACKAGE_ROOT / "schemas").rglob("*.json")))


def _skill_identity() -> str:
    text = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise RuntimeError("SKILL.md has no valid YAML frontmatter")
    frontmatter = text.split("---\n", 2)[1]
    value = yaml.safe_load(frontmatter)
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise RuntimeError("SKILL.md frontmatter has no name")
    return value["name"]


def _pyproject_identity() -> tuple[str, str | None]:
    value = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = value.get("project", {})
    scripts = project.get("scripts", {})
    return str(project.get("name", "")), scripts.get(IDENTITY)


def _adapter_kind_contract(config_schema: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    schema_kinds = tuple(sorted(config_schema["$defs"]["adapterProfile"]["properties"]["kind"]["enum"]))
    model_kinds = tuple(sorted(get_args(AdapterProfile.model_fields["kind"].annotation)))
    return schema_kinds, model_kinds


def _retired_identifier_hits() -> tuple[str, ...]:
    retired = (
        "skill-refiner-" + "next",
        "skill_refiner_" + "next",
        "refiner_" + "next.py",
        "Skill Refiner " + "Next",
    )
    hits: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        if any(part in _TRANSIENT_DIRS for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8")
        if any(value in text or value in relative.as_posix() for value in retired):
            hits.append(relative.as_posix())
    return tuple(hits)


def self_architecture_report() -> dict[str, Any]:
    missing = tuple(relative for relative in REQUIRED_FILES if not (PACKAGE_ROOT / relative).is_file())
    if missing:
        raise RuntimeError(f"required architecture files are missing: {list(missing)}")

    skill_name = _skill_identity()
    project_name, console_script = _pyproject_identity()
    if (skill_name, project_name, console_script) != (IDENTITY, IDENTITY, "skill_refiner.cli:main"):
        raise RuntimeError(
            "public identity mismatch: "
            f"skill={skill_name!r}, project={project_name!r}, console_script={console_script!r}"
        )

    stage_ids = tuple(STAGES)
    registry_ids = tuple(item.stage_id for item in registry().stages)
    if stage_ids != EXPECTED_STAGE_IDS or registry_ids != EXPECTED_STAGE_IDS:
        raise RuntimeError("the fixed fourteen-stage contract changed")

    actual_schema_paths = packaged_schema_relative_paths()
    if actual_schema_paths != EXPECTED_PACKAGED_SCHEMA_PATHS:
        raise RuntimeError(
            f"packaged schema set mismatch: expected={EXPECTED_PACKAGED_SCHEMA_PATHS}, actual={actual_schema_paths}"
        )
    schema_hashes: dict[str, str] = {}
    config_schema: dict[str, Any] | None = None
    for relative in actual_schema_paths:
        value = json.loads((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(value)
        schema_hashes[relative] = _sha256_file(relative)
        if relative == "schemas/config.schema.json":
            config_schema = value
    assert config_schema is not None
    schema_kinds, model_kinds = _adapter_kind_contract(config_schema)
    if schema_kinds != model_kinds:
        raise RuntimeError(f"adapter kind mismatch: schema={schema_kinds}, model={model_kinds}")

    retired_hits = _retired_identifier_hits()
    if retired_hits:
        raise RuntimeError(f"retired identifiers remain in: {list(retired_hits)}")

    required_hashes = {relative: _sha256_file(relative) for relative in REQUIRED_FILES}
    controller_sha256 = controller_contract_hash()
    contract = {
        "contract_version": CONTRACT_VERSION,
        "identity": IDENTITY,
        "console_script": console_script,
        "controller_contract_sha256": controller_sha256,
        "stage_ids": stage_ids,
        "schema_hashes": schema_hashes,
        "required_file_hashes": required_hashes,
        "adapter_kinds": model_kinds,
        "retired_identifier_policy": "forbidden",
    }
    return {
        "status": "ok",
        "identity": IDENTITY,
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": _canonical_sha256(contract),
        "controller_contract_sha256": controller_sha256,
        "stages": {"count": len(stage_ids), "ids": stage_ids},
        "packaged_schemas": {"paths": actual_schema_paths, "hashes": schema_hashes},
        "required_files": required_hashes,
        "adapter_kinds": model_kinds,
        "retired_identifier_policy": "forbidden",
    }


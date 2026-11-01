"""Package-owned schemas and fail-closed validation for stage handoffs."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path

import jsonschema


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_CATALOG = {
    "skill-refiner.clarification-recovery": ("v1", "schemas/v1/clarification-recovery.schema.json", "json-schema"),
    "skill-refiner.target-obligations": ("v1", "schemas/v1/target-obligations.schema.json", "json-schema"),
    "skill-refiner.analysis-v4": ("v4", "schemas/v4/analysis.schema.json", "json-schema"),
    "skill-refiner.analysis-review-v4": ("v4", "schemas/v4/analysis-review.schema.json", "json-schema"),
    "skill-refiner.technical-recovery": ("v1", "schemas/v1/technical-recovery.schema.json", "json-schema"),
    "skill-refiner.technical-triage": ("v1", "schemas/v1/technical-triage.schema.json", "json-schema"),
    "skill-refiner.technical-coverage": ("v1", "schemas/v1/technical-coverage.schema.json", "json-schema"),
    "skill-refiner.analysis-recovery": ("v1", "schemas/v1/analysis-recovery.schema.json", "json-schema"),
    "skill-refiner.analysis-revision-delta": ("v1", "schemas/v1/analysis-revision-delta.schema.json", "json-schema"),
    "skill-refiner.analysis-revision-delta-v2": ("v2", "schemas/v2/analysis-revision-delta.schema.json", "json-schema"),
    "skill-refiner.clarification-review": ("v1", "schemas/v1/clarification-review.schema.json", "json-schema"),
    "skill-refiner.checkpoint-migration": ("v1", "schemas/v1/checkpoint-migration.schema.json", "json-schema"),
    "skill-refiner.checkpoint-migration-v2": ("v2", "schemas/v2/checkpoint-migration.schema.json", "json-schema"),
    "skill-refiner.checkpoint-migration-v3": ("v3", "schemas/v3/checkpoint-migration.schema.json", "json-schema"),
    "skill-refiner.evaluation-cost": ("v1", "schemas/v1/evaluation-cost.schema.json", "json-schema"),
    "skill-refiner.process-execution": ("v1", "schemas/v1/process-execution.schema.json", "json-schema"),
    "skill-refiner.runtime-preflight": ("v1", "schemas/v1/runtime-preflight.schema.json", "json-schema"),
    "skill-refiner.stages": ("v2", "schemas/v2/stages.schema.json", "json-schema"),
    "skill-refiner.artifact-record": ("v1", "schemas/v1/artifact-record.schema.json", "json-schema"),
    "skill-refiner.stage-artifact-contract": ("v1", "schemas/v1/stage-artifact-contract.schema.json", "json-schema"),
    "skill-refiner.json-object": ("v1", "schemas/v1/json-object.schema.json", "json-schema"),
    "skill-refiner.static-findings": ("v1", "schemas/v1/static-findings.schema.json", "json-schema"),
    "skill-refiner.markdown": ("v1", "schemas/v1/markdown.schema.json", "json-schema-text"),
    "skill-refiner.utf8-text": ("v1", "schemas/v1/utf8-text.schema.json", "json-schema-text"),
    "skill-refiner.unified-diff": ("v1", "schemas/v1/unified-diff.schema.json", "json-schema-text"),
    "skill-refiner.binary-content": ("v1", "schemas/v1/binary-content.schema.json", "json-schema-binary-metadata"),
    "skill-refiner.plan-v2": ("v2", "schemas/v2/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v3": ("v3", "schemas/v3/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v4": ("v4", "schemas/v4/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v5": ("v5", "schemas/v5/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v6": ("v6", "schemas/v6/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v7": ("v7", "schemas/v7/plan.schema.json", "json-schema"),
    "skill-refiner.plan-v8": ("v8", "schemas/v8/plan.schema.json", "json-schema"),
    "skill-refiner.plan-review-v8": ("v8", "schemas/v8/plan-review.schema.json", "json-schema"),
    "skill-refiner.plan-review-recovery-v8": ("v8", "schemas/v8/plan-review-recovery.schema.json", "json-schema"),
    "skill-refiner.response-applicability-analysis-v1": ("v7", "schemas/v7/response-applicability-analysis.schema.json", "json-schema"),
    "skill-refiner.response-applicability-decision-v1": ("v7", "schemas/v7/response-applicability-decision.schema.json", "json-schema"),
    "skill-refiner.response-contract-migration-v1": ("v7", "schemas/v7/response-contract-migration.schema.json", "json-schema"),
    "skill-refiner.execution-plan": ("v1", "schemas/v1/execution-plan.schema.json", "json-schema"),
    "skill-refiner.plan-review-v2": ("v2", "schemas/v2/plan-review.schema.json", "json-schema"),
    "skill-refiner.plan-review-v3": ("v3", "schemas/v3/plan-review.schema.json", "json-schema"),
    "skill-refiner.plan-review-v4": ("v4", "schemas/v4/plan-review.schema.json", "json-schema"),
    "skill-refiner.plan-review-v5": ("v5", "schemas/v5/plan-review.schema.json", "json-schema"),
    "skill-refiner.plan-review-recovery-v6": ("v6", "schemas/v6/plan-review.schema.json", "json-schema"),
    "skill-refiner.repair": ("v1", "schemas/v1/repair.schema.json", "json-schema"),
    "skill-refiner.analysis": ("v1", "schemas/v1/analysis.schema.json", "json-schema"),
    "skill-refiner.analysis-review": ("v1", "schemas/v1/analysis-review.schema.json", "json-schema"),
    "skill-refiner.analysis-v2": ("v2", "schemas/v2/analysis.schema.json", "json-schema"),
    "skill-refiner.analysis-v3": ("v3", "schemas/v3/analysis.schema.json", "json-schema"),
    "skill-refiner.analysis-review-v2": ("v2", "schemas/v2/analysis-review.schema.json", "json-schema"),
    "skill-refiner.analysis-review-v3": ("v3", "schemas/v3/analysis-review.schema.json", "json-schema"),
    "skill-refiner.skill-stage-contracts": ("v1", "schemas/v1/skill-stage-contracts.schema.json", "json-schema"),
}

LEGACY_SCHEMA_PATHS = {
    schema_id: f"schemas/{relative.rsplit('/', 1)[-1][:-len('.schema.json')]}-{version}.schema.json"
    for schema_id, (version, relative, _) in SCHEMA_CATALOG.items()
}


def schema_ref(schema_id: str) -> dict:
    """Return the package-relative, hash-pinned reference for a shipped schema."""
    try:
        version, relative, validator_id = SCHEMA_CATALOG[schema_id]
    except KeyError as exc:
        raise ValueError(f"Unknown schema id: {schema_id}") from exc
    path = (PACKAGE_ROOT / relative).resolve()
    if not path.is_relative_to(PACKAGE_ROOT) or not path.is_file():
        raise ValueError(f"Required packaged schema is missing: {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"id": schema_id, "version": version, "path": relative,
            "sha256": digest, "validator_id": validator_id}


def all_schema_refs() -> list[dict]:
    return [schema_ref(schema_id) for schema_id in sorted(SCHEMA_CATALOG)]


def schema_ref_compatible(stored: dict, current: dict) -> bool:
    """Accept the old flat path only when its identity and frozen bytes still match."""
    if not isinstance(stored, dict) or set(stored) != set(current):
        return False
    if any(stored[key] != current[key] for key in current if key != "path"):
        return False
    return stored["path"] in {current["path"], LEGACY_SCHEMA_PATHS.get(current["id"])}


# Registries frozen before these explicitly additive contracts remain valid.
_ADDITIVE_SCHEMA_IDS = frozenset({
    "skill-refiner.plan-v7",
    "skill-refiner.plan-v8",
    "skill-refiner.plan-review-v8",
    "skill-refiner.plan-review-recovery-v8",
    "skill-refiner.plan-review-recovery-v6",
    "skill-refiner.response-applicability-analysis-v1",
    "skill-refiner.response-applicability-decision-v1",
    "skill-refiner.response-contract-migration-v1",
})


def schema_refs_compatible(stored_refs: list[dict], current_refs: list[dict]) -> bool:
    """Permit only known additive schemas; all previously frozen refs stay exact."""
    if not isinstance(stored_refs, list):
        return False
    stored_by_id = {row.get("id"): row for row in stored_refs if isinstance(row, dict)}
    current_by_id = {row.get("id"): row for row in current_refs if isinstance(row, dict)}
    if len(stored_by_id) != len(stored_refs) or len(current_by_id) != len(current_refs):
        return False
    missing = current_by_id.keys() - stored_by_id.keys()
    if stored_by_id.keys() - current_by_id.keys() or missing - _ADDITIVE_SCHEMA_IDS:
        return False
    return all(schema_ref_compatible(stored_by_id[key], current_by_id[key]) for key in stored_by_id)


def _schema_for(reference: dict) -> tuple[dict, dict]:
    if not isinstance(reference, dict) or reference.get("id") not in SCHEMA_CATALOG:
        raise ValueError("Unknown or missing artifact schema reference")
    actual = schema_ref(reference["id"])
    if not schema_ref_compatible(reference, actual):
        raise ValueError(f"Packaged schema reference mismatch for {reference.get('id')}: "
                         "version, path, validator, or SHA-256 changed")
    path = PACKAGE_ROOT / actual["path"]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except (OSError, ValueError, jsonschema.SchemaError) as exc:
        raise ValueError(f"Packaged schema is invalid: {actual['path']}: {exc}") from exc
    if schema.get("$id") != actual["id"] or schema.get("x-schema-version") != actual["version"]:
        raise ValueError(f"Packaged schema identity/version mismatch: {actual['path']}")
    return actual, schema


def validate_value(value, reference: dict, *, subject="artifact") -> None:
    actual, schema = _schema_for(reference)
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"{subject} fails {actual['id']} at {location}: {exc.message}") from exc


def validate_self_review_record(record: dict) -> None:
    """Validate the shared, bounded self-review record and its cross-field invariants."""
    relative = "schemas/v1/self-review-record.schema.json"
    try:
        schema = json.loads((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise ValueError(f"Packaged self-review schema is invalid: {relative}: {exc}") from exc
    if schema.get("$id") != "agent-skill.self-review-record" or schema.get("x-schema-version") != "v1":
        raise ValueError(f"Packaged self-review schema identity/version mismatch: {relative}")
    try:
        jsonschema.validate(record, schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"Self-review record fails agent-skill.self-review-record at {location}: "
                         f"{exc.message}") from exc

    checks = record["checks"]
    check_ids = [row["check_id"] for row in checks]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("Self-review check IDs must be unique")
    unresolved = {row["check_id"] for row in checks if row["result"] in {"fail", "blocked"}}
    gap_ids = [row["check_id"] for row in record["open_gaps"]]
    if len(gap_ids) != len(set(gap_ids)) or set(gap_ids) != unresolved:
        raise ValueError("Self-review open_gaps must identify every and only failed or blocked check")
    expected = "pass"
    if any(row["result"] == "blocked" for row in checks):
        expected = "blocked"
    elif any(row["result"] == "fail" for row in checks):
        expected = "revise_once" if record["review_round"] == 0 else "blocked"
    if record["overall_result"] != expected:
        raise ValueError("Self-review overall_result conflicts with check results and the one-correction limit")


def validate_stage_manifest(data: dict, reference: dict | None = None) -> None:
    reference = reference or schema_ref("skill-refiner.stages")
    validate_value(data, reference, subject="stages.json")


def validate_skill_schema_package(skill_root: Path, stage_contracts: dict) -> None:
    """Check the runtime schemas and validator files declared for a delivered Skill."""
    skill_root = Path(skill_root).resolve()
    schema_ids = [definition["id"] for definition in stage_contracts["schemas"]]
    if len(schema_ids) != len(set(schema_ids)):
        raise ValueError(
            "Delivered Skill contract must declare exactly one active version per logical schema ID"
        )
    runtime_validator_paths = {
        definition["validator_path"]
        for definition in stage_contracts["schemas"]
        if definition["runtime_needed"]
    }
    for definition in stage_contracts["schemas"]:
        relative_schema = Path(definition["path"])
        if relative_schema.is_absolute() or ".." in relative_schema.parts:
            raise ValueError(f"Delivered Skill schema path is unsafe: {definition['path']}")
        if (len(relative_schema.parts) < 3 or relative_schema.parts[0] != "schemas"
                or relative_schema.parts[1] != definition["version"]):
            raise ValueError(
                "Delivered Skill schema path version directory must match its declared version: "
                f"{definition['path']} vs {definition['version']}"
            )
        if not definition["runtime_needed"]:
            schema_file = skill_root / definition["path"]
            validator_file = skill_root / definition["validator_path"]
            if schema_file.exists():
                raise ValueError(
                    f"Delivered Skill packages a schema marked as not runtime-needed: {definition['path']}"
                )
            if (definition["validator_path"] not in runtime_validator_paths
                    and validator_file.exists()):
                raise ValueError(
                    "Delivered Skill packages a validator used only by a non-runtime schema: "
                    f"{definition['validator_path']}"
                )
            continue
        for key in ("path", "validator_path"):
            relative = Path(definition[key])
            unresolved = skill_root / relative
            if unresolved.is_symlink():
                raise ValueError(f"Delivered Skill runtime {key} must not be a symlink: {relative}")
            target = unresolved.resolve()
            if relative.is_absolute() or not target.is_relative_to(skill_root) or not target.is_file():
                raise ValueError(f"Delivered Skill is missing or has an unsafe runtime {key}: {relative}")
        try:
            schema = json.loads((skill_root / definition["path"]).read_text(encoding="utf-8"))
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except (OSError, UnicodeError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
            raise ValueError(f"Delivered Skill schema is invalid at {definition['path']}: {exc}") from exc
        if schema.get("$id") != definition["id"] or schema.get("x-schema-version") != definition["version"]:
            raise ValueError(f"Delivered Skill schema identity/version mismatch: {definition['path']}")

    # The controller itself reads schemas from earlier runs. Keep that private compatibility
    # registry intact, while requiring every downstream Skill schema file to be declared as
    # a current runtime contract so stale versions cannot sneak into the delivered package.
    try:
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        skill_text = ""
    frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", skill_text, re.S)
    is_controller = bool(frontmatter and re.search(r"(?m)^name:\s*skill-refiner\s*$", frontmatter[1]))
    if is_controller:
        return

    schemas_dir = skill_root / "schemas"
    if not schemas_dir.exists():
        return
    runtime_paths = {
        Path(definition["path"]).as_posix()
        for definition in stage_contracts["schemas"]
        if definition["runtime_needed"]
    }
    for candidate in schemas_dir.rglob("*.schema.json"):
        relative = candidate.relative_to(skill_root).as_posix()
        if relative not in runtime_paths:
            raise ValueError(
                "Delivered Skill contains an undeclared or historical schema file; "
                f"only current runtime schemas in stage_contracts may be packaged: {relative}"
            )


def validate_contract_declaration(declaration: dict, *, subject="stage contract") -> None:
    validate_value(declaration, schema_ref("skill-refiner.stage-artifact-contract"), subject=subject)


def validate_artifact(path: Path, root: Path, artifact: dict, *, expected_contract_id=None) -> None:
    """Validate one recorded artifact's bytes against its pinned packaged contract."""
    path, root = Path(path).resolve(), Path(root).resolve()
    relative = Path(artifact.get("path", ""))
    if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Artifact path is missing or unsafe: {relative}")
    data = path.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if len(data) != artifact.get("bytes") or actual_hash != artifact.get("sha256"):
        raise ValueError(f"Artifact size or SHA-256 changed: {relative}")

    schema_id = content_schema_id(path, artifact.get("media_type"))
    if expected_contract_id and expected_contract_id != schema_id:
        raise ValueError(f"Artifact {relative} uses {schema_id}; stage contract requires {expected_contract_id}")
    reference = artifact.get("schema_ref")
    if not isinstance(reference, dict) or reference.get("id") != schema_id:
        raise ValueError(f"Artifact {relative} has a missing or incorrect schema id; expected {schema_id}")
    actual_reference = schema_ref(schema_id)
    if (reference != actual_reference
            or artifact.get("validation", {}).get("validator_id") != actual_reference["validator_id"]):
        raise ValueError(f"Artifact {relative} schema reference or validator id changed")

    if actual_reference["validator_id"] == "json-schema":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Artifact {relative} is not valid UTF-8 JSON: {exc}") from exc
    elif schema_id == "skill-refiner.binary-content":
        value = {"bytes": len(data), "sha256": actual_hash}
    else:
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Artifact {relative} is not valid UTF-8 text: {exc}") from exc
    validate_value(value, reference, subject=f"artifact {relative}")
    if schema_id == "skill-refiner.unified-diff" and value:
        lines = value.splitlines()
        first_header = next((index for index, line in enumerate(lines) if line.startswith("--- ")), None)
        if first_header is None or first_header + 1 >= len(lines) or not lines[first_header + 1].startswith("+++ "):
            raise ValueError(f"Artifact {relative} is not a complete unified diff header")
    artifact.setdefault("validation", {})["status"] = "passed"

    record = {key: artifact[key] for key in ("path", "media_type", "bytes", "sha256", "schema_ref", "validation")}
    validate_value(record, schema_ref("skill-refiner.artifact-record"),
                   subject=f"artifact inventory record {relative}")


def content_schema_id(path: Path, media_type: str | None = None) -> str:
    suffix = Path(path).suffix.lower()
    path_text = str(path).replace("\\", "/")
    basename = Path(path).name.lower()
    parts = set(part.lower() for part in path_text.split("/"))
    in_original_skill = "original" in parts
    in_skill_tree = bool(parts.intersection({"original", "final", "candidates", "drafts"}))
    in_skill_tree |= bool(re.search(r"/engines/[^/]+/skill/", path_text))
    if basename == "response-contract-migration.json" and not in_skill_tree:
        return "skill-refiner.response-contract-migration-v1"
    if suffix == ".json" and not in_skill_tree and path.is_file():
        try:
            kind = json.loads(path.read_text(encoding="utf-8")).get("schema_version")
        except (OSError, UnicodeError, ValueError, AttributeError):
            kind = None
        typed = {"skill-refiner.response-applicability-analysis/v1": "skill-refiner.response-applicability-analysis-v1",
                 "skill-refiner.response-applicability-decision/v1": "skill-refiner.response-applicability-decision-v1"}
        if kind in typed:
            return typed[kind]
    if re.search(r"(?:^|/)drafts/[0-9]+/static-findings\.json$", path_text):
        return "skill-refiner.static-findings"
    if basename == "evaluation-cost.json" and not in_skill_tree:
        return "skill-refiner.evaluation-cost"
    if basename == "process.execution.json" and not in_skill_tree:
        return "skill-refiner.process-execution"
    if basename.startswith("runtime-preflight") and suffix == ".json" and not in_skill_tree:
        return "skill-refiner.runtime-preflight"
    if basename.startswith("clarification-review-attempt-") and suffix == ".json" and not in_skill_tree:
        return "skill-refiner.clarification-review"
    if basename.startswith("analysis-revision-delta-") and suffix == ".json" and not in_skill_tree:
        if path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("schema_version") == "skill-refiner.analysis-revision-delta/v2":
                    return "skill-refiner.analysis-revision-delta-v2"
            except (OSError, UnicodeError, ValueError, AttributeError):
                pass
        return "skill-refiner.analysis-revision-delta"
    if basename.startswith("technical-triage-") and suffix == ".json" and not in_skill_tree:
        return "skill-refiner.technical-triage"
    if basename == "technical-coverage.json" and not in_skill_tree:
        return "skill-refiner.technical-coverage"
    if ((basename in {"test-plan.json", "acceptance-plan.json"} and not in_skill_tree)
            or "/plan/response.json" in path_text or "/acceptance-plan/response.json" in path_text):
        if path.is_file():
            try:
                version = json.loads(path.read_text(encoding="utf-8")).get("schema_version")
            except (OSError, UnicodeError, ValueError, AttributeError):
                version = None
            if version in {f"skill-refiner.plan/v{n}" for n in range(2, 9)}:
                return "skill-refiner.plan-" + version.rsplit("/", 1)[1]
        return "skill-refiner.plan-v5"
    if (basename == "analysis.json" and not in_skill_tree) or "/analyze/response.json" in path_text:
        if path.is_file():
            try:
                version = json.loads(path.read_text(encoding="utf-8")).get("schema_version")
            except (OSError, UnicodeError, ValueError, AttributeError):
                version = None
            if version in (None, "skill-refiner.analysis/v1"):
                return "skill-refiner.analysis"
            if version == "skill-refiner.analysis/v2":
                return "skill-refiner.analysis-v2"
            if version == "skill-refiner.analysis/v4":
                return "skill-refiner.analysis-v4"
        return "skill-refiner.analysis-v3"
    if basename.startswith("analysis-review-attempt-") and suffix == ".json" and not in_skill_tree and path.is_file():
        try:
            version = json.loads(path.read_text(encoding="utf-8")).get("schema_version")
            if version in ("skill-refiner.analysis-review/v3", "skill-refiner.analysis-review/v4"):
                return "skill-refiner.analysis-review-" + version.rsplit("/", 1)[1]
        except (OSError, UnicodeError, ValueError, AttributeError):
            pass
    if (basename == "analysis-review.json" and not in_skill_tree) or "/analysis-review/response.json" in path_text:
        if path.is_file():
            try:
                version = json.loads(path.read_text(encoding="utf-8")).get("schema_version")
            except (OSError, UnicodeError, ValueError, AttributeError):
                version = None
            if version in (None, "skill-refiner.analysis-review/v1"):
                return "skill-refiner.analysis-review"
            if version == "skill-refiner.analysis-review/v3":
                return "skill-refiner.analysis-review-v3"
            if version == "skill-refiner.analysis-review/v4":
                return "skill-refiner.analysis-review-v4"
        return "skill-refiner.analysis-review-v2"
    if basename == "changes.json" and "/iterations/round-" in path_text:
        return "skill-refiner.repair"
    if "/plan-review/response.json" in path_text:
        return "skill-refiner.plan-review-v5"
    media_type = media_type or mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream"
    if suffix == ".json" or media_type == "application/json" or media_type.endswith("+json"):
        return "skill-refiner.json-object"
    if suffix in {".patch", ".diff"} or media_type == "text/x-diff":
        return "skill-refiner.unified-diff"
    if suffix in {".md", ".markdown"} or media_type == "text/markdown":
        return "skill-refiner.markdown"
    if media_type.startswith("text/") or suffix in {".py", ".sh", ".txt", ".yaml", ".yml", ".toml", ".ini"}:
        return "skill-refiner.utf8-text"
    return "skill-refiner.binary-content"

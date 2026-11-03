from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import jsonschema

from skill_refiner.adapters import AgentAdapter
from skill_refiner.kernel import RefinerControlKernel
from skill_refiner.models import AdapterProfile, CandidateProposer
from skill_refiner.security import ensure_safe_tree, safe_copy_tree, tree_manifest
from skill_refiner.self_architecture import (
    EXPECTED_PACKAGED_SCHEMA_PATHS,
    EXPECTED_STAGE_IDS,
    packaged_schema_relative_paths,
    self_architecture_report,
)
from skill_refiner.service import doctor


ROOT = Path(__file__).parents[1]


def test_doctor_reports_stable_self_architecture_contract() -> None:
    first = self_architecture_report()
    second = doctor()["self_architecture"]
    assert first == second
    assert first["status"] == "ok"
    assert first["identity"] == "skill-refiner"
    assert len(first["contract_sha256"]) == 64
    assert len(first["controller_contract_sha256"]) == 64
    assert first["stages"] == {"count": 14, "ids": EXPECTED_STAGE_IDS}


def test_packaged_schema_set_is_complete_valid_and_hashed() -> None:
    assert packaged_schema_relative_paths() == EXPECTED_PACKAGED_SCHEMA_PATHS
    report = self_architecture_report()
    assert tuple(report["packaged_schemas"]["paths"]) == EXPECTED_PACKAGED_SCHEMA_PATHS
    for relative in EXPECTED_PACKAGED_SCHEMA_PATHS:
        schema = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert len(report["packaged_schemas"]["hashes"][relative]) == 64


def test_config_schema_and_runtime_accept_the_same_adapter_kinds() -> None:
    schema = json.loads((ROOT / "schemas" / "config.schema.json").read_text(encoding="utf-8"))
    schema_kinds = set(schema["$defs"]["adapterProfile"]["properties"]["kind"]["enum"])
    runtime_kinds = set(get_args(AdapterProfile.model_fields["kind"].annotation))
    assert schema_kinds == runtime_kinds == {"command", "fake", "deepseek", "cursor_sdk"}
    for relative in (
        "examples/config.adk.json",
        "examples/config.builtin-test.json",
        "examples/config.deepseek.json",
        "examples/config.cursor-sdk.json",
    ):
        jsonschema.validate(json.loads((ROOT / relative).read_text(encoding="utf-8")), schema)


def test_transient_caches_are_excluded_from_checks_manifests_and_copies(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: test\ndescription: test\n---\n", encoding="utf-8")
    (source / "notes.md").write_text("keep\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"\x00\x01binary")
    pytest_cache = source / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "CACHEDIR.TAG").write_text("cache", encoding="utf-8")
    (source / "module.pyc").write_bytes(b"\x00\x01binary")

    ensure_safe_tree(source)
    assert set(tree_manifest(source)) == {"SKILL.md", "notes.md"}
    destination = tmp_path / "copy"
    safe_copy_tree(source, destination)
    assert set(tree_manifest(destination)) == {"SKILL.md", "notes.md"}


def test_proposal_interfaces_cannot_claim_kernel_authority() -> None:
    for protocol in (AgentAdapter, CandidateProposer):
        for forbidden in ("apply_proposal", "evaluate", "final_verify", "set_run_status", "verify_evidence"):
            assert not hasattr(protocol, forbidden)
    authoritative = {
        "apply_proposal", "evaluate", "final_verify", "prepare_delivery", "set_run_status", "verify_evidence",
    }
    assert authoritative <= set(dir(RefinerControlKernel))


def test_retired_identifier_is_absent_without_embedding_it_in_source() -> None:
    retired = "skill-refiner-" + "next"
    for relative in ("SKILL.md", "README.md", "references/architecture.md"):
        assert retired not in (ROOT / relative).read_text(encoding="utf-8")

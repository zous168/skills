from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from skill_refiner.errors import InputError, TamperError
from skill_refiner.hashing import digest_file, digest_json, read_json
from skill_refiner.kernel import RefinerControlKernel
from skill_refiner.models import RunMode, RunStatus, StageAttemptEnvelope, StageContractRegistry, StageIndex, StageInputRecord
from skill_refiner.service import create_run, resume_run
from skill_refiner.stage_contracts import (
    CONTRACT_SCHEMA_PATH,
    SCHEMA_PATH,
    STAGE_OUTPUT_MODELS,
    STAGES,
    registry,
    schema_file,
    validate_stage_output,
)


ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("stage-persistence")
    result = asyncio.run(create_run(
        EXAMPLES / "requirements.md", EXAMPLES / "config.builtin-test.json", root / "runs",
    ))
    assert result.status == RunStatus.VERIFIED
    return result.run_dir


def _rows(run_dir: Path) -> list[dict]:
    connection = sqlite3.connect(run_dir / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM stages ORDER BY ordinal")]
    finally:
        connection.close()


def test_registry_defines_every_stage_once() -> None:
    contract = registry()
    assert isinstance(contract, StageContractRegistry)
    assert tuple(item.stage_id for item in contract.stages) == STAGES
    assert len({item.input_schema_id for item in contract.stages}) == len(STAGES)
    assert len({item.output_schema_id for item in contract.stages}) == len(STAGES)
    assert set(STAGE_OUTPUT_MODELS) == set(STAGES)
    assert all(item.validation_points == ("generation", "handoff", "resume", "final_verify") for item in contract.stages)


def test_packaged_contract_schema_validates_registry() -> None:
    schema = json.loads(schema_file(CONTRACT_SCHEMA_PATH).read_text(encoding="utf-8"))
    jsonschema.validate(registry().model_dump(mode="json"), schema)
    assert json.loads(schema_file(SCHEMA_PATH).read_text(encoding="utf-8"))["$id"] == "skill-refiner.stage-artifacts/v1"


def test_every_completed_stage_has_valid_input_output_and_envelope(completed_run: Path) -> None:
    kernel = RefinerControlKernel.open(completed_run)
    rows = _rows(completed_run)
    assert len(rows) == len(STAGES)
    for row in rows:
        stage_id = row["stage_id"]
        attempt = row["attempt"]
        base = completed_run / "stages" / stage_id / str(attempt)
        input_path = base / "input.json"
        output_path = base / "output.json"
        envelope_path = base / "envelope.json"
        assert input_path.is_file() and output_path.is_file() and envelope_path.is_file()

        stage_input = StageInputRecord.model_validate(read_json(input_path))
        output = validate_stage_output(stage_id, read_json(output_path))
        envelope = StageAttemptEnvelope.model_validate(read_json(envelope_path))
        assert stage_input.stage_id == stage_id
        assert envelope.status == "passed"
        assert envelope.input.content_sha256 == digest_json(stage_input.model_dump(mode="json")) == row["input_sha256"]
        assert envelope.output is not None
        assert envelope.output.content_sha256 == digest_json(output) == row["output_sha256"]
        assert envelope.budget_after.elapsed_seconds >= envelope.budget_before.elapsed_seconds
        assert envelope.budget_after.agent_calls >= envelope.budget_before.agent_calls
        assert envelope.budget_after.evaluations >= envelope.budget_before.evaluations
    assert kernel.validate_completed_stages()["checked_stages"] == list(STAGES)
    assert len(kernel.validate_stage_attempts()["checked_attempts"]) == len(STAGES)


def test_database_schema_version_is_persisted(completed_run: Path) -> None:
    connection = sqlite3.connect(completed_run / "state.sqlite3")
    try:
        assert connection.execute("SELECT value FROM metadata WHERE key='database_schema_version'").fetchone()[0] == "2"
    finally:
        connection.close()


def test_live_stage_index_is_schema_bound_and_matches_sqlite(completed_run: Path) -> None:
    index = StageIndex.model_validate(read_json(completed_run / "stages.json"))
    assert tuple(item.stage_id for item in index.stages) == STAGES
    assert all(item.status.value == "passed" for item in index.stages)
    assert RefinerControlKernel.open(completed_run).validate_stage_index()["count"] == len(STAGES)


@pytest.mark.parametrize("stage_id", STAGES)
def test_tampering_any_stage_output_blocks_resume(completed_run: Path, tmp_path: Path, stage_id: str) -> None:
    clone = tmp_path / "run"
    shutil.copytree(completed_run, clone)
    row = next(item for item in _rows(clone) if item["stage_id"] == stage_id)
    output = clone / "stages" / stage_id / str(row["attempt"]) / "output.json"
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(TamperError):
        RefinerControlKernel.open(clone)


def test_tampered_frozen_schema_blocks_resume(completed_run: Path, tmp_path: Path) -> None:
    clone = tmp_path / "run"
    shutil.copytree(completed_run, clone)
    schema = clone / SCHEMA_PATH
    schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(TamperError):
        RefinerControlKernel.open(clone)


def test_tampered_sqlite_stage_hash_blocks_resume(completed_run: Path, tmp_path: Path) -> None:
    clone = tmp_path / "run"
    shutil.copytree(completed_run, clone)
    connection = sqlite3.connect(clone / "state.sqlite3")
    try:
        connection.execute("UPDATE stages SET input_sha256=? WHERE stage_id='review_plan'", ("0" * 64,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(TamperError):
        RefinerControlKernel.open(clone)


def test_tampered_content_addressed_object_blocks_resume(completed_run: Path, tmp_path: Path) -> None:
    clone = tmp_path / "run"
    shutil.copytree(completed_run, clone)
    row = next(item for item in _rows(clone) if item["stage_id"] == "development_evaluation")
    digest = row["output_sha256"]
    object_path = clone / "artifacts" / "sha256" / digest[:2] / digest
    object_path.write_text("{}", encoding="utf-8")

    with pytest.raises(TamperError):
        RefinerControlKernel.open(clone)


def test_invalid_stage_output_persists_failed_attempt(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-invalid-output"
    kernel = RefinerControlKernel.initialize(
        run_dir, RunMode.CREATE, EXAMPLES / "requirements.md", EXAMPLES / "config.builtin-test.json",
    )

    with pytest.raises(ValidationError):
        asyncio.run(kernel.stage("freeze_inputs", lambda: {"invalid": True}))

    envelope = StageAttemptEnvelope.model_validate(read_json(run_dir / "stages" / "freeze_inputs" / "1" / "envelope.json"))
    assert envelope.status == "failed"
    assert envelope.output is None
    assert envelope.error is not None
    assert envelope.error.code == "ValidationError"
    assert _rows(run_dir)[0]["status"] == "failed"


def test_resume_reuses_passed_stages_and_retries_only_failed_stage(tmp_path: Path) -> None:
    fixtures = tmp_path / "examples"
    shutil.copytree(EXAMPLES, fixtures)
    author_path = fixtures / "fixtures" / "author.json"
    good_author = author_path.read_text(encoding="utf-8")
    author_path.write_text(json.dumps({
        "summary": "unsafe proposal",
        "changes": [{"path": "../escape.md", "operation": "write", "content": "no"}],
    }), encoding="utf-8")
    output_root = tmp_path / "runs"

    with pytest.raises(InputError):
        asyncio.run(create_run(fixtures / "requirements.md", fixtures / "config.builtin-test.json", output_root))
    run_dir = next(output_root.iterdir())
    before = {row["stage_id"]: row for row in _rows(run_dir)}
    assert before["generate_candidate"]["status"] == "failed"
    assert before["static_validation"]["status"] == "pending"

    author_path.write_text(good_author, encoding="utf-8")
    result = asyncio.run(resume_run(run_dir))
    assert result.status == RunStatus.VERIFIED
    after = {row["stage_id"]: row for row in _rows(run_dir)}
    assert all(after[stage]["attempt"] == 1 for stage in STAGES[:6])
    assert after["generate_candidate"]["attempt"] == 2
    assert all(after[stage]["attempt"] == 1 for stage in STAGES[7:])
    assert len(RefinerControlKernel.open(run_dir).validate_stage_attempts()["checked_attempts"]) == len(STAGES) + 1


def test_resume_terminal_run_is_idempotent(completed_run: Path) -> None:
    before = {row["stage_id"]: row["attempt"] for row in _rows(completed_run)}
    result = asyncio.run(resume_run(completed_run))
    after = {row["stage_id"]: row["attempt"] for row in _rows(completed_run)}
    assert result.status == RunStatus.VERIFIED
    assert before == after


def test_crash_interrupted_attempt_is_closed_then_retried(completed_run: Path, tmp_path: Path) -> None:
    clone = tmp_path / "run"
    shutil.copytree(completed_run, clone)
    kernel = RefinerControlKernel.open(clone)
    stage_id = "static_validation"
    interrupted_attempt = 2
    input_value = kernel._stage_input(stage_id).model_dump(mode="json")
    input_sha = kernel.store.put_json(f"stages/{stage_id}/{interrupted_attempt}/input.json", input_value)
    connection = sqlite3.connect(clone / "state.sqlite3")
    try:
        connection.execute(
            "UPDATE stages SET status='running',attempt=?,input_sha256=?,output_sha256=NULL,started_at='2026-11-03T00:00:00+00:00',finished_at=NULL WHERE stage_id=?",
            (interrupted_attempt, input_sha, stage_id),
        )
        connection.execute("UPDATE stages SET status='pending' WHERE ordinal>8")
        connection.execute("UPDATE runs SET status='error'")
        connection.commit()
    finally:
        connection.close()
    kernel.store.export_state()

    result = asyncio.run(resume_run(clone))
    assert result.status == RunStatus.VERIFIED
    after = {row["stage_id"]: row for row in _rows(clone)}
    assert after[stage_id]["attempt"] == 3
    assert all(after[stage]["attempt"] == 2 for stage in STAGES[8:])
    interrupted = StageAttemptEnvelope.model_validate(
        read_json(clone / "stages" / stage_id / str(interrupted_attempt) / "envelope.json")
    )
    assert interrupted.status == "failed"
    assert interrupted.error is not None and interrupted.error.code == "INTERRUPTED"

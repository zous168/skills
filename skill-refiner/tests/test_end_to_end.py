from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skill_refiner.errors import InputError, TamperError
from skill_refiner.kernel import STAGES
from skill_refiner.models import RunStatus
from skill_refiner.security import safe_relative_path
from skill_refiner.service import adopt_run, create_run, refine_run, resume_run, status_run, verify_run
from skill_refiner.workflow import build_adk_workflow


ROOT = Path(__file__).parents[1]
REQUIREMENTS = ROOT / "examples" / "requirements.md"
CONFIG = ROOT / "examples" / "config.builtin-test.json"
ADK_CONFIG = ROOT / "examples" / "config.adk.json"


def test_create_runs_real_cases_and_verifies(tmp_path: Path) -> None:
    result = asyncio.run(create_run(REQUIREMENTS, CONFIG, tmp_path / "runs"))

    assert result.status == RunStatus.VERIFIED
    assert result.verified
    assert (result.run_dir / "candidate" / "SKILL.md").is_file()
    assert (result.run_dir / "state.sqlite3").is_file()
    assert (result.run_dir / "events.jsonl").is_file()
    verification = verify_run(result.run_dir)
    assert verification.verified
    state = status_run(result.run_dir)
    assert [stage["stage_id"] for stage in state["stages"]] == list(STAGES)
    assert all(stage["status"] == "passed" for stage in state["stages"])


def test_plan_only_does_not_generate_candidate(tmp_path: Path) -> None:
    result = asyncio.run(create_run(REQUIREMENTS, CONFIG, tmp_path / "runs", plan_only=True))

    assert result.status == RunStatus.PLANNED
    assert not (result.run_dir / "candidate" / "SKILL.md").exists()
    assert (result.run_dir / "plan.json").is_file()


def test_planned_run_resumes_remaining_stages(tmp_path: Path) -> None:
    planned = asyncio.run(create_run(REQUIREMENTS, CONFIG, tmp_path / "runs", plan_only=True))

    resumed = asyncio.run(resume_run(planned.run_dir))

    assert resumed.status == RunStatus.VERIFIED
    assert resumed.verified
    state = status_run(planned.run_dir)
    assert all(stage["status"] == "passed" for stage in state["stages"])


def test_tamper_is_detected(tmp_path: Path) -> None:
    result = asyncio.run(create_run(REQUIREMENTS, CONFIG, tmp_path / "runs"))
    skill = result.run_dir / "candidate" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")

    with pytest.raises(TamperError):
        verify_run(result.run_dir)


def test_refine_preserves_unmodified_file_and_adopts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: old\ndescription: old skill\n---\n", encoding="utf-8")
    (source / "notes.md").write_text("keep me", encoding="utf-8")

    result = asyncio.run(refine_run(source, REQUIREMENTS, CONFIG, tmp_path / "runs"))
    assert (result.run_dir / "candidate" / "notes.md").read_text(encoding="utf-8") == "keep me"
    adopted = tmp_path / "adopted"
    adoption = adopt_run(result.run_dir, adopted)
    assert adoption.adopted
    assert (adopted / "notes.md").is_file()


@pytest.mark.parametrize("value", ["../escape.md", "/absolute.md", "a\\b.md", ".refiner/state.json"])
def test_unsafe_candidate_paths_are_rejected(value: str) -> None:
    with pytest.raises(InputError):
        safe_relative_path(value)


def test_real_adk_graph_contains_fixed_nodes() -> None:
    pytest.importorskip("google.adk.workflow")

    async def noop(_node_input=None):
        return {"ok": True}

    workflow = build_adk_workflow({stage: noop for stage in STAGES})
    names = {node.name for node in workflow.graph.nodes}
    assert set(STAGES) <= names
    assert workflow.max_concurrency == 1


def test_real_adk_runner_executes_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("google.adk.runners")
    result = asyncio.run(create_run(REQUIREMENTS, ADK_CONFIG, tmp_path / "adk-runs"))

    assert result.status == RunStatus.VERIFIED
    assert result.details["runtime"]["driver"] == "google-adk"

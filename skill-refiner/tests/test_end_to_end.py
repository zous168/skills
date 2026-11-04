from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from skill_refiner.errors import InputError, NeedsUserDecision, TamperError, VerificationError
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
    assert (result.run_dir / "progress.json").is_file()
    assert state["progress"]["counts"] == {
        "pending": 0, "in_progress": 0, "verified": 2, "blocked": 0,
    }
    assert all(item["status"] == "verified" for item in state["progress"]["work_items"])


def test_plan_only_does_not_generate_candidate(tmp_path: Path) -> None:
    result = asyncio.run(create_run(REQUIREMENTS, CONFIG, tmp_path / "runs", plan_only=True))

    assert result.status == RunStatus.PLANNED
    assert not (result.run_dir / "candidate" / "SKILL.md").exists()
    assert (result.run_dir / "plan.json").is_file()
    state = status_run(result.run_dir)
    assert state["progress"]["current_work_item_id"] == "W-greet"
    assert state["progress"]["counts"]["pending"] == 2
    assert state["progress"]["counts"]["verified"] == 0


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


def test_reviewer_check_failure_blocks_plan_gate(tmp_path: Path) -> None:
    fixtures = tmp_path / "examples"
    shutil.copytree(ROOT / "examples", fixtures)
    review_path = fixtures / "fixtures" / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["checks"]["atomic_work_items"] = False
    review_path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(NeedsUserDecision):
        asyncio.run(create_run(
            fixtures / "requirements.md", fixtures / "config.builtin-test.json", tmp_path / "runs",
        ))


def test_failed_mapped_case_blocks_work_item_progress(tmp_path: Path) -> None:
    fixtures = tmp_path / "examples"
    shutil.copytree(ROOT / "examples", fixtures)
    author_path = fixtures / "fixtures" / "author.json"
    author = json.loads(author_path.read_text(encoding="utf-8"))
    for change in author["changes"]:
        if change["path"] == "scripts/hello.py":
            change["content"] = change["content"].replace("Hello,", "Hi,")
    author_path.write_text(json.dumps(author), encoding="utf-8")
    output_root = tmp_path / "runs"

    with pytest.raises(VerificationError):
        asyncio.run(create_run(
            fixtures / "requirements.md", fixtures / "config.builtin-test.json", output_root,
        ))
    run_dir = next(output_root.iterdir())
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    progress = state["progress"]
    assert progress["counts"]["blocked"] >= 1
    assert any(item["status"] == "blocked" and item["evidence_refs"] for item in progress["work_items"])

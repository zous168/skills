from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from skill_refiner.contracts import PLAN_SCHEMA
from skill_refiner.errors import VerificationError
from skill_refiner.kernel import RefinerControlKernel
from skill_refiner.models import CandidateProposal, RunMode
from skill_refiner.security import ensure_safe_tree, redact


ROOT = Path(__file__).parents[1]


def test_candidate_proposal_is_strict() -> None:
    with pytest.raises(Exception):
        CandidateProposal.model_validate({
            "schema_version": "skill-refiner.candidate-proposal/v1",
            "plan_sha256": "0" * 64,
            "changes": [],
            "summary": "x",
            "verdict": "PASS",
        })


def test_redaction_removes_common_credentials() -> None:
    assert "secret-value" not in redact("api_key=secret-value")
    assert "hunter2" not in redact("https://user:hunter2@example.com/path")


def test_binary_input_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "payload.bin").write_bytes(b"\x00\x01")
    with pytest.raises(Exception):
        ensure_safe_tree(tmp_path)


def test_plan_schema_rejects_free_form_cases() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "requirements": [{"id": "REQ-1", "description": "greet a name"}],
                "development_cases": [
                    {
                        "id": "DEV-1",
                        "title": "free-form prose is not executable",
                        "steps": ["run it"],
                        "expected": "works",
                    }
                ],
                "acceptance_cases": [
                    {
                        "id": "C-accept",
                        "kind": "acceptance",
                        "requirement_ids": ["REQ-1"],
                        "argv": ["{python}", "scripts/hello.py", "Bob"],
                        "assertions": [{"id": "A-exit", "kind": "exit_code", "expected": 0}],
                    }
                ],
            },
            PLAN_SCHEMA,
        )


def test_plan_schema_accepts_executable_cases() -> None:
    case = {
        "id": "C-greet",
        "kind": "normal",
        "requirement_ids": ["REQ-1"],
        "argv": ["{python}", "scripts/hello.py", "Alice"],
        "assertions": [
            {"id": "A-exit", "kind": "exit_code", "expected": 0},
            {"id": "A-text", "kind": "stdout_contains", "expected": "Hello, Alice!"},
        ],
    }
    jsonschema.validate(
        {
            "requirements": [{"id": "REQ-1", "description": "greet a name"}],
            "development_cases": [case],
            "acceptance_cases": [{**case, "id": "C-accept", "kind": "acceptance"}],
        },
        PLAN_SCHEMA,
    )


def test_kernel_rejects_unconfigured_plan_executable(tmp_path: Path) -> None:
    kernel = RefinerControlKernel.initialize(
        tmp_path / "run-executor-contract",
        RunMode.CREATE,
        ROOT / "examples" / "requirements.md",
        ROOT / "examples" / "config.builtin-test.json",
    )
    payload = {
        "requirements": [{"id": "REQ-1", "description": "greet a name"}],
        "development_cases": [
            {
                "id": "C-dev",
                "kind": "normal",
                "requirement_ids": ["REQ-1"],
                "argv": ["python3", "greet.py", "Alice"],
                "assertions": [{"id": "A-dev-exit", "kind": "exit_code", "expected": 0}],
            }
        ],
        "acceptance_cases": [
            {
                "id": "C-accept",
                "kind": "acceptance",
                "requirement_ids": ["REQ-1"],
                "argv": ["{python}", "greet.py", "Bob"],
                "assertions": [{"id": "A-accept-exit", "kind": "exit_code", "expected": 0}],
            }
        ],
    }
    with pytest.raises(VerificationError, match="unsupported executable"):
        kernel.make_plan(payload)


@pytest.mark.parametrize(
    ("assertion", "message"),
    [
        ({"id": "A-lines", "kind": "stdout_contains", "expected": "Welcome, Alice!\n"}, "portable stdout substring"),
        ({"id": "A-manifest", "kind": "manifest_unchanged", "expected": {}}, "controller-only manifest_unchanged"),
    ],
)
def test_kernel_rejects_nonportable_model_assertions(
    tmp_path: Path, assertion: dict[str, object], message: str,
) -> None:
    kernel = RefinerControlKernel.initialize(
        tmp_path / f"run-{assertion['id']}",
        RunMode.CREATE,
        ROOT / "examples" / "requirements.md",
        ROOT / "examples" / "config.builtin-test.json",
    )
    case = {
        "id": "C-dev",
        "kind": "normal",
        "requirement_ids": ["REQ-1"],
        "argv": ["{python}", "greet.py", "Alice"],
        "assertions": [assertion],
    }
    payload = {
        "requirements": [{"id": "REQ-1", "description": "greet a name"}],
        "development_cases": [case],
        "acceptance_cases": [{**case, "id": "C-accept", "kind": "acceptance"}],
    }
    with pytest.raises(VerificationError, match=message):
        kernel.make_plan(payload)

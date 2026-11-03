from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest


cursor_sdk = pytest.importorskip("cursor_sdk")


def _run_worker(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> tuple[int, dict[str, object]]:
    from skill_refiner import cursor_sdk_worker

    def fake_prompt(message, options):
        assert "JSON Schema" in message
        assert options.model == "auto"
        return result

    monkeypatch.setattr(cursor_sdk.Agent, "prompt", fake_prompt)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-test-secret")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "credential_env": "CURSOR_API_KEY",
        "model": "auto",
        "prompt": "Return JSON matching the JSON Schema",
        "cwd": os.getcwd(),
    })))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    code = cursor_sdk_worker.main()
    value = json.loads(output.getvalue())
    assert "cursor-test-secret" not in output.getvalue()
    return code, value


def test_cursor_sdk_worker_emits_finished_result(monkeypatch: pytest.MonkeyPatch) -> None:
    code, value = _run_worker(monkeypatch, SimpleNamespace(
        status="finished", result='{"status":"ok"}', id="run-1", agent_id="agent-1",
        model="auto", duration_ms=12, usage=None, error_code=None, error_message=None,
    ))
    assert code == 0
    assert value["status"] == "finished"
    assert value["result"] == '{"status":"ok"}'


def test_cursor_sdk_worker_fails_closed_on_run_error(monkeypatch: pytest.MonkeyPatch) -> None:
    code, value = _run_worker(monkeypatch, SimpleNamespace(
        status="error", result="", id="run-2", agent_id="agent-2",
        model="auto", duration_ms=8, usage=None, error_code="backend", error_message="failed",
    ))
    assert code == 3
    assert value["status"] == "error"

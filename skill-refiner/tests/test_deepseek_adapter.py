from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import jsonschema
import pytest
from pydantic import ValidationError

from skill_refiner.adapters import DeepSeekAdapter
from skill_refiner.errors import AdapterError
from skill_refiner.models import AdapterProfile
from skill_refiner.store import EvidenceStore


SCHEMA = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"type": "boolean"}},
    "additionalProperties": False,
}


def _profile(**updates) -> AdapterProfile:
    value = {
        "kind": "deepseek",
        "model": "deepseek-v4-pro",
        "credential_env": ["DEEPSEEK_API_KEY"],
        "base_url": "https://api.deepseek.com",
        "max_retries": 0,
        "timeout": 5,
    }
    value.update(updates)
    return AdapterProfile.model_validate(value)


def _store(tmp_path: Path) -> EvidenceStore:
    store = EvidenceStore(tmp_path / "run")
    store.initialize()
    return store


def _completed(payload: dict | None = None) -> dict:
    text = json.dumps(payload if payload is not None else {"approved": True})
    return {
        "id": "resp_test_123", "status": "completed", "model": "deepseek-v4-pro",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


def _invoke(adapter: DeepSeekAdapter):
    return asyncio.run(adapter.invoke("reviewer", {"task": "review"}, SCHEMA, stage_id="review_plan"))


def test_deepseek_profile_requires_model_credential_and_https() -> None:
    with pytest.raises(ValidationError):
        AdapterProfile.model_validate({"kind": "deepseek", "credential_env": ["DEEPSEEK_API_KEY"]})
    with pytest.raises(ValidationError):
        AdapterProfile.model_validate({"kind": "deepseek", "model": "deepseek-v4-pro"})
    with pytest.raises(ValidationError):
        AdapterProfile.model_validate({
            "kind": "deepseek", "model": "deepseek-v4-pro",
            "credential_env": ["DEEPSEEK_API_KEY"], "base_url": "http://api.deepseek.com",
        })


def test_deepseek_example_matches_packaged_config_schema() -> None:
    root = Path(__file__).parents[1]
    schema = json.loads((root / "schemas" / "config.schema.json").read_text(encoding="utf-8"))
    config = json.loads((root / "examples" / "config.deepseek.json").read_text(encoding="utf-8"))
    jsonschema.validate(config, schema)


def test_success_uses_json_schema_and_persists_redacted_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "deepseek-test-secret-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completed())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekAdapter(_profile(), _store(tmp_path), client).invoke(
                "reviewer", {"task": "review"}, SCHEMA, stage_id="review_plan",
            )

    result = asyncio.run(execute())
    assert result.payload == {"approved": True}
    assert result.metadata["provider"] == "deepseek"
    assert result.metadata["response_id"] == "resp_test_123"
    assert captured["authorization"] == f"Bearer {secret}"
    assert captured["body"]["text"]["format"] == {
        "type": "json_schema", "name": "review_plan_reviewer", "schema": SCHEMA,
    }
    assert captured["body"]["store"] is False
    text_evidence = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "run").rglob("*") if path.is_file() and path.suffix in {".json", ".txt", ".jsonl"}
    )
    assert secret not in text_evidence
    assert secret.encode() not in (tmp_path / "run" / "state.sqlite3").read_bytes()
    assert (tmp_path / "run" / "calls" / "0001-reviewer" / "provider-response.json").is_file()


def test_missing_credential_fails_and_records_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = _store(tmp_path)
    with pytest.raises(AdapterError, match="credential is unavailable"):
        _invoke(DeepSeekAdapter(_profile(), store))
    with store.connect() as db:
        row = db.execute("SELECT * FROM calls").fetchone()
    assert row["role"] == "reviewer"
    assert "DEEPSEEK_API_KEY" not in row["diagnostics"]


def test_retryable_http_status_is_bounded_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=_completed())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = DeepSeekAdapter(_profile(max_retries=1), _store(tmp_path), client)
            return await adapter.invoke("reviewer", {}, SCHEMA, stage_id="review_plan")

    result = asyncio.run(execute())
    assert result.payload["approved"] is True
    assert result.metadata["http_attempts"] == 2
    assert calls == 2


def test_timeout_is_bounded_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=_completed())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekAdapter(_profile(max_retries=1), _store(tmp_path), client).invoke(
                "reviewer", {}, SCHEMA, stage_id="review_plan",
            )

    assert asyncio.run(execute()).payload["approved"] is True
    assert calls == 2


@pytest.mark.parametrize(
    "body,error",
    [
        ({"id": "x", "status": "failed", "output": []}, "did not complete"),
        (_completed({"approved": "yes"}), "failed validation"),
        ({"id": "x", "status": "completed", "output": []}, "content was empty"),
    ],
)
def test_invalid_terminal_responses_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict, error: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))) as client:
            return await DeepSeekAdapter(_profile(), _store(tmp_path), client).invoke(
                "reviewer", {}, SCHEMA, stage_id="review_plan",
            )

    with pytest.raises(AdapterError, match=error):
        asyncio.run(execute())


def test_non_retryable_http_error_does_not_retry_and_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "deepseek-secret-in-error"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": f"api_key={secret}"}})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DeepSeekAdapter(_profile(max_retries=3), _store(tmp_path), client).invoke(
                "reviewer", {}, SCHEMA, stage_id="review_plan",
            )

    with pytest.raises(AdapterError):
        asyncio.run(execute())
    assert calls == 1
    error_text = (tmp_path / "run" / "calls" / "0001-reviewer" / "error.json").read_text(encoding="utf-8")
    assert secret not in error_text

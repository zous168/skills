from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Protocol

import httpx

from .errors import AdapterError
from .hashing import canonical_json, digest_bytes, digest_json, write_json
from .models import AdapterProfile, AdapterResponse
from .security import redact, sanitized_environment
from .store import EvidenceStore, utc_now


class AgentAdapter(Protocol):
    async def invoke(self, role: str, request: dict[str, Any], response_schema: dict[str, Any], *, stage_id: str) -> AdapterResponse: ...


class CommandAdapter:
    def __init__(self, profile: AdapterProfile, store: EvidenceStore, allowed_environment: tuple[str, ...]):
        self.profile = profile
        self.store = store
        self.allowed_environment = allowed_environment

    async def invoke(self, role: str, request: dict[str, Any], response_schema: dict[str, Any], *, stage_id: str) -> AdapterResponse:
        with self.store.connect() as db:
            call_no = int(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]) + 1
        call_dir = self.store.run_dir / "calls" / f"{call_no:04}-{role}"
        call_dir.mkdir(parents=True)
        envelope = {"role": role, "request": request, "response_schema": response_schema}
        request_sha = digest_json(envelope)
        write_json(call_dir / "request.json", envelope)
        started = utc_now()
        argv = [*self.profile.argv, *self.profile.args]
        environment = sanitized_environment(self.allowed_environment, self.profile.credential_env)
        timeout = self.profile.timeout or 300
        try:
            creation = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE,
                        "env": environment, "cwd": str(call_dir)}
            if os.name == "nt":
                creation["creationflags"] = 0x00000200
            else:
                creation["start_new_session"] = True
            process = await asyncio.create_subprocess_exec(*argv, **creation)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(canonical_json(envelope)), timeout=timeout)
                timed_out = False
            except TimeoutError:
                timed_out = True
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = await process.communicate()
        except OSError as exc:
            raise AdapterError(f"failed to start adapter {argv[0]}: {exc}") from exc
        if len(stdout) > self.profile.max_output_bytes or len(stderr) > self.profile.max_output_bytes:
            raise AdapterError("adapter output exceeded configured byte limit")
        stdout_text = redact(stdout.decode("utf-8", errors="replace"))
        stderr_text = redact(stderr.decode("utf-8", errors="replace"))
        (call_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
        (call_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
        payload: dict[str, Any] = {}
        if not timed_out and process.returncode == 0:
            try:
                decoded = json.loads(stdout.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("response must be an object")
                import jsonschema
                jsonschema.validate(decoded, response_schema)
                payload = decoded
            except Exception as exc:
                raise AdapterError(f"adapter returned malformed JSON: {exc}") from exc
        finished = utc_now()
        response_sha = digest_bytes(stdout)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_no, stage_id, role, request_sha, response_sha, process.returncode, int(timed_out), started, finished, stderr_text[:4096]),
            )
        response = AdapterResponse(payload=payload, exit_code=process.returncode or 0, timed_out=timed_out,
                                   stdout=stdout_text, stderr=stderr_text)
        write_json(call_dir / "response.json", response.model_dump(mode="json"))
        if timed_out:
            raise AdapterError(f"adapter timed out after {timeout}s")
        if process.returncode != 0:
            raise AdapterError(f"adapter exited with {process.returncode}")
        return response


class DeepSeekAdapter:
    """Schema-constrained DeepSeek Responses API adapter.

    The credential is read only at invocation time and is never included in
    persisted request, response, diagnostic, or SQLite evidence.
    """

    RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

    def __init__(self, profile: AdapterProfile, store: EvidenceStore, client: httpx.AsyncClient | None = None):
        self.profile = profile
        self.store = store
        self._client = client

    async def invoke(self, role: str, request: dict[str, Any], response_schema: dict[str, Any], *, stage_id: str) -> AdapterResponse:
        with self.store.connect() as db:
            call_no = int(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]) + 1
        call_dir = self.store.run_dir / "calls" / f"{call_no:04}-{role}"
        call_dir.mkdir(parents=True)
        envelope = {"role": role, "request": request, "response_schema": response_schema}
        request_sha = digest_json(envelope)
        write_json(call_dir / "request.json", envelope)
        started = utc_now()
        response_bytes = b""
        response_sha: str | None = None
        http_status: int | None = None
        timed_out = False
        attempts: list[dict[str, Any]] = []
        credential_name = next((name for name in self.profile.credential_env if os.environ.get(name)), None)
        if credential_name is None:
            error = AdapterError(
                "deepseek credential is unavailable; set one configured credential environment variable",
                details={"credential_env": list(self.profile.credential_env)},
            )
            self._record(call_no, stage_id, role, request_sha, None, None, False, started, utc_now(), str(error))
            write_json(call_dir / "error.json", {"code": error.code, "message": str(error), "details": error.details})
            raise error
        api_key = os.environ[credential_name]
        api_request = {
            "model": self.profile.model,
            "instructions": (
                "Return exactly one JSON object that conforms to the supplied JSON Schema. "
                "Do not add Markdown fences or explanatory text."
            ),
            "input": json.dumps({"role": role, "request": request}, ensure_ascii=False, sort_keys=True),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": _schema_name(stage_id, role),
                    "schema": response_schema,
                }
            },
            "store": False,
        }
        endpoint = self.profile.base_url.rstrip("/") + "/responses"
        timeout = self.profile.timeout or 300
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        try:
            body: dict[str, Any] | None = None
            for attempt_no in range(1, self.profile.max_retries + 2):
                try:
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=api_request,
                        timeout=timeout,
                    )
                    http_status = response.status_code
                    response_bytes = response.content
                    if len(response_bytes) > self.profile.max_output_bytes:
                        raise AdapterError("deepseek response exceeded configured byte limit")
                    attempts.append({"attempt": attempt_no, "status": http_status, "outcome": "response"})
                    if http_status in self.RETRYABLE_STATUS and attempt_no <= self.profile.max_retries:
                        await asyncio.sleep(0.05 * (2 ** (attempt_no - 1)))
                        continue
                    if http_status < 200 or http_status >= 300:
                        diagnostic = _safe_http_diagnostic(response_bytes)
                        raise AdapterError(f"deepseek API returned HTTP {http_status}: {diagnostic}")
                    try:
                        parsed = response.json()
                    except ValueError as exc:
                        raise AdapterError("deepseek API returned malformed JSON") from exc
                    if not isinstance(parsed, dict):
                        raise AdapterError("deepseek API response must be a JSON object")
                    body = parsed
                    break
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    timed_out = timed_out or isinstance(exc, httpx.TimeoutException)
                    attempts.append({"attempt": attempt_no, "status": None, "outcome": type(exc).__name__})
                    if attempt_no <= self.profile.max_retries:
                        await asyncio.sleep(0.05 * (2 ** (attempt_no - 1)))
                        continue
                    raise AdapterError(f"deepseek API transport failed after {attempt_no} attempts: {type(exc).__name__}") from exc
            if body is None:
                raise AdapterError("deepseek API produced no terminal response")
            response_sha = digest_bytes(response_bytes)
            write_json(call_dir / "http-attempts.json", attempts)
            self.store.put_json(f"calls/{call_no:04}-{role}/provider-response.json", body)
            status = body.get("status")
            if status not in {None, "completed"}:
                raise AdapterError(f"deepseek response did not complete: {status}")
            output_text = _extract_response_text(body)
            if not output_text.strip():
                raise AdapterError("deepseek response content was empty")
            try:
                payload = json.loads(output_text)
                if not isinstance(payload, dict):
                    raise ValueError("structured response is not an object")
                import jsonschema
                jsonschema.validate(payload, response_schema)
            except Exception as exc:
                raise AdapterError(f"deepseek structured response failed validation: {exc}") from exc
            metadata = {
                "provider": "deepseek", "model": body.get("model", self.profile.model),
                "response_id": body.get("id"), "usage": body.get("usage", {}),
                "http_status": http_status, "http_attempts": len(attempts),
            }
            result = AdapterResponse(
                payload=payload, exit_code=0, stdout=output_text, metadata=metadata,
            )
            write_json(call_dir / "response.json", result.model_dump(mode="json"))
            self._record(
                call_no, stage_id, role, request_sha, response_sha, 0, False,
                started, utc_now(), json.dumps(metadata, ensure_ascii=False, sort_keys=True)[:4096],
            )
            return result
        except AdapterError as exc:
            write_json(call_dir / "http-attempts.json", attempts)
            write_json(call_dir / "error.json", {"code": exc.code, "message": redact(str(exc)), "http_status": http_status})
            if response_bytes and response_sha is None:
                response_sha = digest_bytes(response_bytes)
            self._record(
                call_no, stage_id, role, request_sha, response_sha, http_status, timed_out,
                started, utc_now(), redact(str(exc))[:4096],
            )
            raise
        finally:
            if own_client:
                await client.aclose()

    def _record(
        self, call_no: int, stage_id: str, role: str, request_sha: str,
        response_sha: str | None, exit_code: int | None, timed_out: bool,
        started: str, finished: str, diagnostics: str,
    ) -> None:
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_no, stage_id, role, request_sha, response_sha, exit_code, int(timed_out),
                 started, finished, diagnostics),
            )


class CursorSdkAdapter:
    """Schema-validating adapter for the local Python cursor-sdk runtime."""

    def __init__(self, profile: AdapterProfile, store: EvidenceStore, allowed_environment: tuple[str, ...]):
        self.profile = profile
        self.store = store
        self.allowed_environment = allowed_environment

    async def invoke(self, role: str, request: dict[str, Any], response_schema: dict[str, Any], *, stage_id: str) -> AdapterResponse:
        with self.store.connect() as db:
            call_no = int(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]) + 1
        call_dir = self.store.run_dir / "calls" / f"{call_no:04}-{role}"
        call_dir.mkdir(parents=True)
        workspace = call_dir / "sdk-workspace"
        workspace.mkdir()
        envelope = {"role": role, "request": request, "response_schema": response_schema}
        request_sha = digest_json(envelope)
        write_json(call_dir / "request.json", envelope)
        credential_name = next((name for name in self.profile.credential_env if os.environ.get(name)), None)
        started = utc_now()
        if credential_name is None:
            error = AdapterError(
                "cursor_sdk credential is unavailable; set one configured credential environment variable",
                details={"credential_env": list(self.profile.credential_env)},
            )
            self._record(call_no, stage_id, role, request_sha, None, None, False, started, utc_now(), str(error))
            write_json(call_dir / "error.json", {"code": error.code, "message": str(error), "details": error.details})
            raise error
        prompt = (
            "Return exactly one JSON object matching the supplied JSON Schema. "
            "Do not use Markdown fences, tools, files, or explanatory prose.\n"
            + json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        )
        worker_input = {
            "model": self.profile.model,
            "credential_env": credential_name,
            "prompt": prompt,
            "cwd": str(workspace),
        }
        environment = sanitized_environment(self.allowed_environment, self.profile.credential_env)
        timeout = self.profile.timeout or 300
        creation: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": environment,
            "cwd": str(workspace),
        }
        if os.name == "nt":
            creation["creationflags"] = 0x00000200
        else:
            creation["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "skill_refiner.cursor_sdk_worker", **creation,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(canonical_json(worker_input)), timeout=timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        if len(stdout) > self.profile.max_output_bytes or len(stderr) > self.profile.max_output_bytes:
            raise AdapterError("cursor_sdk worker output exceeded configured byte limit")
        stdout_text = redact(stdout.decode("utf-8", errors="replace"))
        stderr_text = redact(stderr.decode("utf-8", errors="replace"))
        (call_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
        (call_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
        response_sha = digest_bytes(stdout)
        try:
            if timed_out:
                raise AdapterError(f"cursor_sdk worker timed out after {timeout}s")
            if process.returncode != 0:
                raise AdapterError(f"cursor_sdk worker exited with {process.returncode}: {stderr_text[:1024]}")
            worker_response = json.loads(stdout.decode("utf-8"))
            if not isinstance(worker_response, dict) or worker_response.get("status") != "finished":
                raise AdapterError(
                    f"cursor_sdk run did not finish: {worker_response.get('status') if isinstance(worker_response, dict) else None}"
                )
            output_text = worker_response.get("result")
            if not isinstance(output_text, str) or not output_text.strip():
                raise AdapterError("cursor_sdk result was empty")
            payload = json.loads(output_text)
            if not isinstance(payload, dict):
                raise ValueError("structured response is not an object")
            import jsonschema
            jsonschema.validate(payload, response_schema)
            metadata = {
                "provider": "cursor_sdk", "model": worker_response.get("model", self.profile.model),
                "run_id": worker_response.get("run_id"), "agent_id": worker_response.get("agent_id"),
                "usage": worker_response.get("usage"), "duration_ms": worker_response.get("duration_ms"),
            }
            result = AdapterResponse(payload=payload, exit_code=0, stdout=output_text, metadata=metadata)
            write_json(call_dir / "provider-response.json", worker_response)
            write_json(call_dir / "response.json", result.model_dump(mode="json"))
            self._record(call_no, stage_id, role, request_sha, response_sha, 0, False, started, utc_now(), json.dumps(metadata, ensure_ascii=False)[:4096])
            return result
        except Exception as exc:
            error = exc if isinstance(exc, AdapterError) else AdapterError(f"cursor_sdk structured response failed validation: {exc}")
            write_json(call_dir / "error.json", {"code": error.code, "message": redact(str(error))})
            self._record(call_no, stage_id, role, request_sha, response_sha, process.returncode, timed_out, started, utc_now(), redact(str(error))[:4096])
            raise error

    def _record(self, call_no: int, stage_id: str, role: str, request_sha: str, response_sha: str | None,
                exit_code: int | None, timed_out: bool, started: str, finished: str, diagnostics: str) -> None:
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_no, stage_id, role, request_sha, response_sha, exit_code, int(timed_out), started, finished, diagnostics),
            )


def _schema_name(stage_id: str, role: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]", "_", f"{stage_id}_{role}")
    return value[:128] or "stage_response"


def _safe_http_diagnostic(content: bytes) -> str:
    text = redact(content.decode("utf-8", errors="replace"))
    return re.sub(r"\s+", " ", text).strip()[:1024] or "empty response"


def _extract_response_text(body: dict[str, Any]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str):
        return direct
    pieces: list[str] = []
    for item in body.get("output", []) if isinstance(body.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
        elif isinstance(item.get("text"), str):
            pieces.append(item["text"])
    return "".join(pieces)


class FakeAdapter:
    def __init__(self, profile: AdapterProfile, fixture_root: Path, store: EvidenceStore):
        self.profile = profile
        self.fixture_root = fixture_root
        self.store = store

    async def invoke(self, role: str, request: dict[str, Any], response_schema: dict[str, Any], *, stage_id: str) -> AdapterResponse:
        with self.store.connect() as db:
            call_no = int(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]) + 1
        path = (self.fixture_root / str(self.profile.fixture)).resolve()
        if not path.is_file() or not path.is_relative_to(self.fixture_root.resolve()):
            raise AdapterError(f"missing fake fixture: {self.profile.fixture}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "responses" in payload:
            selected = payload["responses"].get(stage_id)
            if isinstance(selected, str):
                selected_path = (path.parent / selected).resolve()
                if not selected_path.is_file() or not selected_path.is_relative_to(self.fixture_root.resolve()):
                    raise AdapterError(f"missing stage fixture for {stage_id}: {selected}")
                payload = json.loads(selected_path.read_text(encoding="utf-8"))
            else:
                payload = selected
        if not isinstance(payload, dict):
            raise AdapterError("fake fixture must contain a JSON object")
        try:
            import jsonschema
            jsonschema.validate(payload, response_schema)
        except Exception as exc:
            raise AdapterError(f"fake fixture does not match response schema: {exc}") from exc
        started = utc_now()
        envelope = {"role": role, "request": request, "response_schema": response_schema}
        response_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        call_dir = self.store.run_dir / "calls" / f"{call_no:04}-{role}"
        call_dir.mkdir(parents=True)
        write_json(call_dir / "request.json", envelope)
        write_json(call_dir / "response.json", payload)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_no, stage_id, role, digest_json(envelope), digest_bytes(response_text.encode()), 0, 0,
                 started, utc_now(), "fake fixture"),
            )
        return AdapterResponse(payload=payload, exit_code=0, stdout=response_text)


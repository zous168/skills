from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import TamperError
from .hashing import digest_bytes, digest_file, write_json
from .models import (
    EvaluationReport,
    FrozenPlan,
    ProgressProjection,
    RunStatus,
    StageIndex,
    StageIndexRecord,
    WorkItemProgressRecord,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  source_path TEXT,
  requirements_sha256 TEXT NOT NULL,
  config_sha256 TEXT NOT NULL,
  controller_sha256 TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  elapsed_seconds REAL NOT NULL DEFAULT 0,
  agent_calls INTEGER NOT NULL DEFAULT 0,
  evaluations INTEGER NOT NULL DEFAULT 0,
  repair_rounds INTEGER NOT NULL DEFAULT 0,
  last_failure_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS stages (
  stage_id TEXT PRIMARY KEY,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  input_sha256 TEXT,
  output_sha256 TEXT,
  started_at TEXT,
  finished_at TEXT,
  error_code TEXT,
  error_message TEXT
);
CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  stage_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  input_sha256 TEXT,
  output_sha256 TEXT,
  started_at TEXT,
  finished_at TEXT,
  error_code TEXT,
  recoverable INTEGER NOT NULL,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  logical_path TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
  call_no INTEGER PRIMARY KEY,
  stage_id TEXT NOT NULL,
  role TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_sha256 TEXT,
  exit_code INTEGER,
  timed_out INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  diagnostics TEXT NOT NULL DEFAULT ''
);
"""


class EvidenceStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.db_path = self.run_dir / "state.sqlite3"
        self.object_dir = self.run_dir / "artifacts" / "sha256"
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.object_dir.mkdir(parents=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('database_schema_version','2')")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def put_bytes(self, logical_path: str, content: bytes, media_type: str) -> str:
        if not logical_path or "\\" in logical_path or logical_path.startswith("/") or ".." in Path(logical_path).parts:
            raise TamperError(f"unsafe artifact logical path: {logical_path}")
        digest = digest_bytes(content)
        target = self.object_dir / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if target.exists() and digest_file(target) != digest:
                raise TamperError(f"artifact object changed: {digest}")
            if not target.exists():
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(content)
                temporary.replace(target)
            materialized = self.run_dir / Path(logical_path)
            materialized.parent.mkdir(parents=True, exist_ok=True)
            materialized_temporary = materialized.with_name(materialized.name + ".tmp")
            materialized_temporary.write_bytes(content)
            materialized_temporary.replace(materialized)
            with self.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?)",
                    (logical_path, digest, len(content), media_type, utc_now()),
                )
        return digest

    def put_json(self, logical_path: str, value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(logical_path, payload, "application/json")

    def get_bytes(self, logical_path: str) -> bytes:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE logical_path=?", (logical_path,)).fetchone()
        if row is None:
            raise TamperError(f"missing artifact record: {logical_path}")
        target = self.object_dir / row["content_sha256"][:2] / row["content_sha256"]
        if not target.is_file() or digest_file(target) != row["content_sha256"]:
            raise TamperError(f"missing or changed artifact: {logical_path}")
        content = target.read_bytes()
        if len(content) != row["byte_count"]:
            raise TamperError(f"artifact size changed: {logical_path}")
        with self.connect() as db:
            schema_row = db.execute("SELECT value FROM metadata WHERE key='database_schema_version'").fetchone()
        if schema_row is not None and int(schema_row[0]) >= 2:
            materialized = self.run_dir / Path(logical_path)
            if not materialized.is_file() or digest_file(materialized) != row["content_sha256"]:
                raise TamperError(f"materialized artifact missing or changed: {logical_path}")
        return content

    def get_json(self, logical_path: str) -> Any:
        return json.loads(self.get_bytes(logical_path).decode("utf-8"))

    def event(self, run_id: str, stage_id: str, attempt: int, status: str, **details: Any) -> None:
        now = utc_now()
        row = (
            run_id, stage_id, attempt, status, details.pop("input_sha256", None),
            details.pop("output_sha256", None), details.pop("started_at", now),
            details.pop("finished_at", now), details.pop("error_code", None),
            int(details.pop("recoverable", False)), json.dumps(details, ensure_ascii=False, sort_keys=True),
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,stage_id,attempt,status,input_sha256,output_sha256,started_at,finished_at,error_code,recoverable,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        self.export_events()

    def export_events(self) -> None:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY sequence")]
        target = self.run_dir / "events.jsonl"
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        temporary.replace(target)

    def export_state(self) -> dict[str, Any]:
        with self.connect() as db:
            run = db.execute("SELECT * FROM runs").fetchone()
            stages = [dict(row) for row in db.execute("SELECT * FROM stages ORDER BY ordinal")]
        value = {"run": dict(run) if run else None, "stages": stages}
        if run is not None:
            progress = self._progress_projection(dict(run), stages)
            if progress is not None:
                progress_value = progress.model_dump(mode="json")
                value["progress"] = progress_value
                write_json(self.run_dir / "progress.json", progress_value)
        write_json(self.run_dir / "state.json", value)
        with self.connect() as db:
            contract = db.execute("SELECT content_sha256 FROM artifacts WHERE logical_path='contracts/stages.json'").fetchone()
        if contract is not None:
            records = []
            for row in stages:
                attempt = int(row["attempt"])
                prefix = f"stages/{row['stage_id']}/{attempt}" if attempt else None
                records.append(StageIndexRecord(
                    stage_id=row["stage_id"], ordinal=row["ordinal"], status=row["status"], attempt=attempt,
                    input_sha256=row["input_sha256"], output_sha256=row["output_sha256"],
                    input_path=f"{prefix}/input.json" if prefix else None,
                    output_path=f"{prefix}/output.json" if prefix and row["output_sha256"] else None,
                    envelope_path=f"{prefix}/envelope.json" if prefix and row["status"] in {"passed", "failed"} else None,
                    started_at=row["started_at"], finished_at=row["finished_at"],
                    error_code=row["error_code"], error_message=row["error_message"],
                ))
            index = StageIndex(
                schema_version="skill-refiner.stages/v1", contract_sha256=contract["content_sha256"],
                stages=tuple(records),
            )
            write_json(self.run_dir / "stages.json", index.model_dump(mode="json"))
        return value

    def _progress_projection(
        self, run: dict[str, Any], stages: list[dict[str, Any]],
    ) -> ProgressProjection | None:
        with self.connect() as db:
            plan_row = db.execute(
                "SELECT content_sha256 FROM artifacts WHERE logical_path='plan/frozen.json'"
            ).fetchone()
        if plan_row is None:
            return None

        plan = FrozenPlan.model_validate(self.get_json("plan/frozen.json"))
        stage_rows = {row["stage_id"]: row for row in stages}
        reports: dict[str, tuple[EvaluationReport, str]] = {}
        for stage_id in ("development_evaluation", "regression_evaluation", "acceptance_evaluation"):
            row = stage_rows[stage_id]
            if row["status"] != "passed" or not row["attempt"] or not row["output_sha256"]:
                continue
            logical_path = f"stages/{stage_id}/{row['attempt']}/output.json"
            reports[stage_id] = (EvaluationReport.model_validate(self.get_json(logical_path)), logical_path)

        current_stage = next(
            (row["stage_id"] for row in stages if row["status"] == "running"),
            next((row["stage_id"] for row in stages if row["status"] not in {"passed", "skipped"}), None),
        )
        candidate_started = any(
            row["status"] in {"running", "passed", "failed"}
            for row in stages
            if row["ordinal"] >= stage_rows["generate_candidate"]["ordinal"]
        )

        records: list[WorkItemProgressRecord] = []
        for item in plan.work_items:
            evidence_refs: list[str] = []
            mapped_results: dict[str, dict[str, bool]] = {}
            for stage_id, (report, logical_path) in reports.items():
                result_map = {result.case_id: result.passed for result in report.results}
                wanted = item.acceptance_case_ids if stage_id == "acceptance_evaluation" else item.development_case_ids
                selected = {case_id: result_map[case_id] for case_id in wanted if case_id in result_map}
                if selected:
                    mapped_results[stage_id] = selected
                    evidence_refs.append(logical_path)

            blocked = any(not passed for results in mapped_results.values() for passed in results.values())
            development_verified = all(
                stage_id in mapped_results
                and set(mapped_results[stage_id]) == set(item.development_case_ids)
                and all(mapped_results[stage_id].values())
                for stage_id in ("development_evaluation", "regression_evaluation")
            )
            acceptance_verified = (
                "acceptance_evaluation" in mapped_results
                and set(mapped_results["acceptance_evaluation"]) == set(item.acceptance_case_ids)
                and all(mapped_results["acceptance_evaluation"].values())
            )
            if blocked:
                status = "blocked"
            elif development_verified and acceptance_verified:
                status = "verified"
            elif candidate_started:
                status = "in_progress"
            else:
                status = "pending"
            records.append(WorkItemProgressRecord(
                id=item.id,
                title=item.title,
                status=status,
                requirement_ids=item.requirement_ids,
                scope=item.scope,
                depends_on=item.depends_on,
                done_when=item.done_when,
                development_case_ids=item.development_case_ids,
                acceptance_case_ids=item.acceptance_case_ids,
                evidence_refs=tuple(evidence_refs),
            ))

        status_by_id = {record.id: record.status for record in records}
        current_work_item_id = next((
            record.id
            for record in records
            if record.status != "verified"
            and all(status_by_id.get(dependency) == "verified" for dependency in record.depends_on)
        ), next((record.id for record in records if record.status != "verified"), None))
        counts = {
            status: sum(record.status == status for record in records)
            for status in ("pending", "in_progress", "verified", "blocked")
        }
        return ProgressProjection(
            schema_version="skill-refiner.progress/v1",
            run_id=run["run_id"],
            run_status=RunStatus(run["status"]),
            updated_at=run["updated_at"],
            current_stage=current_stage,
            current_work_item_id=current_work_item_id,
            counts=counts,
            work_items=tuple(records),
        )


from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunMode(StrEnum):
    CREATE = "create"
    REFINE = "refine"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PLANNED = "planned"
    BLOCKED = "blocked"
    ERROR = "error"
    VERIFIED = "verified"
    ADOPTED = "adopted"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Budgets(StrictModel):
    max_seconds: Annotated[int, Field(ge=1, le=7200)] = 3600
    max_agent_calls: Annotated[int, Field(ge=1, le=256)] = 64
    max_evaluations: Annotated[int, Field(ge=1, le=200)] = 80
    max_repair_rounds: Annotated[int, Field(ge=0, le=5)] = 2
    per_call_timeout: Annotated[int, Field(ge=1, le=1800)] = 300
    concurrency: Annotated[int, Field(ge=1, le=8)] = 2


class AdapterProfile(StrictModel):
    kind: Literal["command", "fake", "deepseek", "cursor_sdk"]
    argv: tuple[str, ...] = ()
    model: str | None = None
    args: tuple[str, ...] = ()
    credential_env: tuple[str, ...] = ()
    timeout: int | None = None
    max_output_bytes: Annotated[int, Field(ge=1024, le=10_000_000)] = 1_000_000
    fixture: str | None = None
    base_url: str = "https://api.deepseek.com"
    max_retries: Annotated[int, Field(ge=0, le=3)] = 2

    @model_validator(mode="after")
    def validate_kind(self) -> "AdapterProfile":
        if self.kind == "command" and not self.argv:
            raise ValueError("command adapter requires argv")
        if self.kind == "fake" and not self.fixture:
            raise ValueError("fake adapter requires fixture")
        if self.kind == "deepseek":
            if not self.model:
                raise ValueError("deepseek adapter requires model")
            if not self.credential_env:
                raise ValueError("deepseek adapter requires at least one credential environment variable")
            parsed = urlsplit(self.base_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("deepseek base_url must be a credential-free HTTPS origin")
        if self.kind == "cursor_sdk":
            if not self.model:
                raise ValueError("cursor_sdk adapter requires model")
            if not self.credential_env:
                raise ValueError("cursor_sdk adapter requires at least one credential environment variable")
        return self


class RefinerConfig(StrictModel):
    schema_version: Literal["skill-refiner.config/v1"]
    runtime_driver: Literal["adk", "builtin_test"] = "adk"
    budgets: Budgets = Budgets()
    roles: dict[str, AdapterProfile]
    executors: dict[str, AdapterProfile]
    allowed_environment: tuple[str, ...] = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")

    @field_validator("roles")
    @classmethod
    def require_roles(cls, value: dict[str, AdapterProfile]) -> dict[str, AdapterProfile]:
        required = {"planner", "author", "repair", "reviewer", "evaluator"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"missing role profiles: {sorted(missing)}")
        return value


class FileChange(StrictModel):
    path: str
    operation: Literal["write", "delete"]
    content: str | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "FileChange":
        if self.operation == "write" and self.content is None:
            raise ValueError("write change requires content")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("delete change must not include content")
        return self


class CandidateProposal(StrictModel):
    schema_version: Literal["skill-refiner.candidate-proposal/v1"]
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    changes: tuple[FileChange, ...]
    summary: str = Field(min_length=1)


class CaseAssertion(StrictModel):
    id: str = Field(pattern=r"^A-[A-Za-z0-9._-]+$")
    kind: Literal["exit_code", "stdout_contains", "json_schema", "file_exists", "manifest_unchanged"]
    expected: Any


class TestCase(StrictModel):
    id: str = Field(pattern=r"^C-[A-Za-z0-9._-]+$")
    kind: Literal["normal", "boundary", "error", "negative", "regression", "acceptance"]
    requirement_ids: tuple[str, ...]
    argv: tuple[str, ...]
    stdin: str = ""
    assertions: tuple[CaseAssertion, ...]
    timeout: int = Field(default=60, ge=1, le=600)


class FrozenPlan(StrictModel):
    schema_version: Literal["skill-refiner.plan/v1"]
    requirements_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements: tuple[dict[str, Any], ...]
    development_cases: tuple[TestCase, ...]
    acceptance_cases: tuple[TestCase, ...]
    stage_contracts: tuple[dict[str, Any], ...]


class AdapterResponse(StrictModel):
    payload: dict[str, Any]
    exit_code: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResult(StrictModel):
    status: RunStatus
    run_id: str
    run_dir: Path
    stage: str | None = None
    verified: bool = False
    adopted: bool = False
    error_code: str | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class StageDependency(StrictModel):
    artifact: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StageInputRecord(StrictModel):
    schema_version: Literal["skill-refiner.stage-input/v1"]
    stage_id: str
    dependencies: tuple[StageDependency, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)


class StageArtifactRef(StrictModel):
    logical_path: str
    schema_id: str
    schema_version: Literal[1]
    schema_path: str
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_count: int = Field(ge=0)
    media_type: Literal["application/json"] = "application/json"
    validation_result: Literal["passed"] = "passed"


class BudgetSnapshot(StrictModel):
    elapsed_seconds: float = Field(ge=0)
    agent_calls: int = Field(ge=0)
    evaluations: int = Field(ge=0)
    repair_rounds: int = Field(ge=0)


class StageErrorRecord(StrictModel):
    code: str
    message: str
    recoverable: bool


class StageAttemptEnvelope(StrictModel):
    schema_version: Literal["skill-refiner.stage-attempt/v1"]
    run_id: str
    stage_id: str
    attempt: int = Field(ge=1)
    status: Literal["passed", "failed"]
    input: StageArtifactRef
    output: StageArtifactRef | None
    budget_before: BudgetSnapshot
    budget_after: BudgetSnapshot
    started_at: str
    finished_at: str
    error: StageErrorRecord | None


class FreezeInputsOutput(StrictModel):
    frozen: dict[str, str]
    stages: tuple[str, ...]


class AnalysisOutput(StrictModel):
    requirements: tuple[dict[str, Any], ...]


class ReviewOutput(StrictModel):
    approved: bool
    issues: tuple[str, ...]


class PlanGateOutput(StrictModel):
    approved: bool
    plan_only: bool


class BaselineOutput(StrictModel):
    applicable: bool
    manifest: dict[str, str] = Field(default_factory=dict)


class CandidateOutput(StrictModel):
    summary: str
    applied: tuple[str, ...]
    manifest: dict[str, str]


class StaticValidationOutput(StrictModel):
    frontmatter: dict[str, Any]
    manifest: dict[str, str]


class CaseExecutionResult(StrictModel):
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)


class EvaluationReport(StrictModel):
    phase: Literal["development", "regression", "acceptance"]
    passed: bool
    results: tuple[CaseExecutionResult, ...]


class RepairOutput(StrictModel):
    needed: bool
    summary: str | None = None
    applied: tuple[str, ...] = ()
    manifest: dict[str, str] = Field(default_factory=dict)


class FinalVerificationOutput(StrictModel):
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_manifest: dict[str, str]
    candidate_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified_at: str


class DeliveryOutput(StrictModel):
    candidate: str
    report: str


class StageContractRecord(StrictModel):
    stage_id: str
    ordinal: int = Field(ge=1)
    input_schema_id: str
    output_schema_id: str
    schema_version: Literal[1]
    schema_path: str
    validator_id: str
    validation_points: tuple[Literal["generation", "handoff", "resume", "final_verify"], ...]
    failure_states: tuple[str, ...]
    resume_policy: Literal["reuse_if_valid_else_block"]


class StageContractRegistry(StrictModel):
    schema_version: Literal["skill-refiner.stage-contracts/v1"]
    stages: tuple[StageContractRecord, ...]


class StageIndexRecord(StrictModel):
    stage_id: str
    ordinal: int = Field(ge=1)
    status: StageStatus
    attempt: int = Field(ge=0)
    input_sha256: str | None
    output_sha256: str | None
    input_path: str | None
    output_path: str | None
    envelope_path: str | None
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    error_message: str | None


class StageIndex(StrictModel):
    schema_version: Literal["skill-refiner.stages/v1"]
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stages: tuple[StageIndexRecord, ...]


class CandidateProposer(Protocol):
    """Protocol surface for optional Harness² integration.

    Implementations may only return ``CandidateProposal``. They receive no
    mutable Kernel handle and cannot write frozen inputs, tests or evidence.
    """

    async def propose(self, request: dict[str, Any]) -> CandidateProposal: ...


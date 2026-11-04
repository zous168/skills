from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
import jsonschema

from .errors import BudgetExhausted, InputError, TamperError, VerificationError
from .hashing import canonical_json, digest_file, digest_json, read_json, write_json
from .models import (
    AdapterProfile,
    CandidateProposal,
    FileChange,
    FrozenPlan,
    RefinerConfig,
    RunMode,
    RunStatus,
    BudgetSnapshot,
    StageArtifactRef,
    StageAttemptEnvelope,
    StageContractRegistry,
    StageDependency,
    StageErrorRecord,
    StageInputRecord,
    StageIndex,
    TestCase,
)
from .security import ensure_safe_tree, safe_copy_tree, safe_relative_path, sanitized_environment, tree_manifest
from .store import EvidenceStore, utc_now
from .stage_contracts import (
    ATTEMPT_SCHEMA_PATH,
    CONTRACT_SCHEMA_PATH,
    SCHEMA_PATH,
    STAGE_DEPENDENCIES,
    STAGES,
    STAGES_SCHEMA_PATH,
    controller_contract_hash,
    registry,
    schema_file,
    schema_hash,
    validate_stage_output,
)


class RefinerControlKernel:
    """Deterministic authority for state, budgets, paths, evidence and verdicts.

    Workflow and model adapters can propose data. Only this class can mutate a
    run, apply candidate changes, execute tests, or mark a run verified.
    """

    def __init__(self, run_dir: Path, config: RefinerConfig):
        self.run_dir = run_dir.resolve()
        self.store = EvidenceStore(self.run_dir)
        self.config = config
        self.candidate_dir = self.run_dir / "candidate"
        self.baseline_dir = self.run_dir / "baseline"
        self._clock_started = time.monotonic()

    @classmethod
    def initialize(
        cls,
        run_dir: Path,
        mode: RunMode,
        requirements_path: Path,
        config_path: Path,
        source_path: Path | None = None,
    ) -> "RefinerControlKernel":
        requirements_path = requirements_path.resolve()
        config_path = config_path.resolve()
        if not requirements_path.is_file() or not config_path.is_file():
            raise InputError("requirements and config must be existing files")
        config = RefinerConfig.model_validate(_load_structured(config_path))
        if mode == RunMode.REFINE and source_path is None:
            raise InputError("refine mode requires --source")
        store = EvidenceStore(run_dir)
        store.initialize()
        kernel = cls(run_dir, config)
        requirements_bytes = requirements_path.read_bytes()
        config_bytes = config_path.read_bytes()
        requirements_sha = store.put_bytes("inputs/requirements.md", requirements_bytes, "text/markdown")
        config_sha = store.put_bytes("inputs/config.json", config_bytes, "application/json")
        controller_sha = controller_contract_hash()
        run_id = run_dir.name
        now = utc_now()
        with store.connect() as db:
            db.execute(
                "INSERT INTO runs(run_id,mode,status,source_path,requirements_sha256,config_sha256,controller_sha256,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, mode.value, RunStatus.CREATED.value, str(source_path.resolve()) if source_path else None,
                 requirements_sha, config_sha, controller_sha, now, now),
            )
            db.executemany(
                "INSERT INTO stages(stage_id,ordinal,status) VALUES(?,?,?)",
                [(stage, ordinal, "pending") for ordinal, stage in enumerate(STAGES, 1)],
            )
        contract_value = registry().model_dump(mode="json")
        jsonschema.validate(contract_value, json.loads(schema_file(CONTRACT_SCHEMA_PATH).read_text(encoding="utf-8")))
        store.put_json("contracts/stages.json", contract_value)
        for relative in (SCHEMA_PATH, ATTEMPT_SCHEMA_PATH, CONTRACT_SCHEMA_PATH, STAGES_SCHEMA_PATH):
            store.put_bytes(relative, schema_file(relative).read_bytes(), "application/schema+json")
        kernel.candidate_dir.mkdir()
        if source_path is not None:
            ensure_safe_tree(source_path.resolve())
            safe_copy_tree(source_path.resolve(), kernel.baseline_dir)
            for relative in tree_manifest(kernel.baseline_dir):
                target = kernel.candidate_dir / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(kernel.baseline_dir / Path(relative), target)
        store.export_state()
        return kernel

    @classmethod
    def open(cls, run_dir: Path) -> "RefinerControlKernel":
        store = EvidenceStore(run_dir)
        if not store.db_path.is_file():
            raise InputError(f"not a refiner run: {run_dir}")
        raw = store.get_bytes("inputs/config.json")
        try:
            config_value = json.loads(raw)
        except json.JSONDecodeError:
            config_value = yaml.safe_load(raw)
        kernel = cls(run_dir, RefinerConfig.model_validate(config_value))
        if kernel.run["controller_sha256"] != controller_contract_hash():
            raise TamperError("controller stage contract or packaged schema changed; start a new run")
        kernel.validate_contract_snapshot()
        kernel.validate_stage_index()
        kernel.validate_stage_attempts()
        kernel.validate_completed_stages()
        return kernel

    @property
    def run(self) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM runs").fetchone()
        if row is None:
            raise TamperError("run record is missing")
        return dict(row)

    def set_run_status(self, status: RunStatus) -> None:
        self._update_elapsed()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET status=?,updated_at=?", (status.value, utc_now()))
        self.store.export_state()

    def _update_elapsed(self) -> None:
        increment = max(0.0, time.monotonic() - self._clock_started)
        self._clock_started = time.monotonic()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET elapsed_seconds=elapsed_seconds+?,updated_at=?", (increment, utc_now()))

    def check_budget(self, *, calls: int = 0, evaluations: int = 0, repairs: int = 0) -> None:
        self._update_elapsed()
        run = self.run
        budget = self.config.budgets
        proposed = {
            "elapsed_seconds": float(run["elapsed_seconds"]),
            "agent_calls": int(run["agent_calls"]) + calls,
            "evaluations": int(run["evaluations"]) + evaluations,
            "repair_rounds": int(run["repair_rounds"]) + repairs,
        }
        limits = {
            "elapsed_seconds": budget.max_seconds,
            "agent_calls": budget.max_agent_calls,
            "evaluations": budget.max_evaluations,
            "repair_rounds": budget.max_repair_rounds,
        }
        exceeded = [name for name, value in proposed.items() if value > limits[name]]
        if exceeded:
            raise BudgetExhausted(f"budget exhausted: {', '.join(exceeded)}", details=proposed)
        with self.store.connect() as db:
            db.execute(
                "UPDATE runs SET agent_calls=?,evaluations=?,repair_rounds=?,updated_at=?",
                (proposed["agent_calls"], proposed["evaluations"], proposed["repair_rounds"], utc_now()),
            )

    def _budget_snapshot(self) -> BudgetSnapshot:
        run = self.run
        return BudgetSnapshot(
            elapsed_seconds=float(run["elapsed_seconds"]), agent_calls=int(run["agent_calls"]),
            evaluations=int(run["evaluations"]), repair_rounds=int(run["repair_rounds"]),
        )

    def _artifact_ref(self, logical_path: str, schema_id: str, schema_path_value: str) -> StageArtifactRef:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE logical_path=?", (logical_path,)).fetchone()
        if row is None:
            raise TamperError(f"missing artifact metadata: {logical_path}")
        return StageArtifactRef(
            logical_path=logical_path, schema_id=schema_id, schema_version=1,
            schema_path=schema_path_value, schema_sha256=schema_hash(schema_path_value),
            content_sha256=row["content_sha256"], byte_count=row["byte_count"],
            validation_result="passed",
        )

    def _stage_input(self, stage_id: str) -> StageInputRecord:
        dependencies: list[StageDependency] = []
        with self.store.connect() as db:
            for dependency in STAGE_DEPENDENCIES[stage_id]:
                if dependency in STAGES:
                    row = db.execute("SELECT attempt,output_sha256,status FROM stages WHERE stage_id=?", (dependency,)).fetchone()
                    if row is None or row["status"] != "passed" or not row["output_sha256"]:
                        raise VerificationError(f"stage {stage_id} dependency is not complete: {dependency}")
                    logical_path = f"stages/{dependency}/{row['attempt']}/output.json"
                    dependencies.append(StageDependency(artifact=logical_path, sha256=row["output_sha256"]))
                else:
                    artifact = db.execute("SELECT content_sha256 FROM artifacts WHERE logical_path=?", (dependency,)).fetchone()
                    if artifact is None:
                        raise VerificationError(f"stage {stage_id} artifact dependency is missing: {dependency}")
                    dependencies.append(StageDependency(artifact=dependency, sha256=artifact["content_sha256"]))
        return StageInputRecord(
            schema_version="skill-refiner.stage-input/v1", stage_id=stage_id,
            dependencies=tuple(dependencies), parameters={"mode": self.run["mode"]},
        )

    async def stage(self, stage_id: str, action: Callable[[], Awaitable[Any] | Any]) -> Any:
        if stage_id not in STAGES:
            raise InputError(f"unknown stage: {stage_id}")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM stages WHERE stage_id=?", (stage_id,)).fetchone()
        attempt = int(row["attempt"]) + 1
        stage_input = self._stage_input(stage_id)
        input_value = stage_input.model_dump(mode="json")
        input_logical = f"stages/{stage_id}/{attempt}/input.json"
        input_sha = self.store.put_json(input_logical, input_value)
        input_ref = self._artifact_ref(
            input_logical, f"skill-refiner.stage.{stage_id}.input/v1", SCHEMA_PATH,
        )
        budget_before = self._budget_snapshot()
        started_at = utc_now()
        with self.store.connect() as db:
            db.execute(
                "UPDATE stages SET status='running',attempt=?,started_at=?,finished_at=NULL,error_code=NULL,error_message=NULL WHERE stage_id=?",
                (attempt, started_at, stage_id),
            )
            db.execute("UPDATE stages SET input_sha256=? WHERE stage_id=?", (input_sha, stage_id))
        self.store.event(self.run["run_id"], stage_id, attempt, "running")
        self.store.export_state()
        try:
            result = action()
            if hasattr(result, "__await__"):
                result = await result
            validated_output = validate_stage_output(stage_id, _jsonable(result))
            output_logical = f"stages/{stage_id}/{attempt}/output.json"
            output_sha = self.store.put_json(output_logical, validated_output)
            output_ref = self._artifact_ref(
                output_logical, f"skill-refiner.stage.{stage_id}.output/v1", SCHEMA_PATH,
            )
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            self._update_elapsed()
            finished_at = utc_now()
            envelope = StageAttemptEnvelope(
                schema_version="skill-refiner.stage-attempt/v1", run_id=self.run["run_id"],
                stage_id=stage_id, attempt=attempt, status="failed", input=input_ref, output=None,
                budget_before=budget_before, budget_after=self._budget_snapshot(), started_at=started_at,
                finished_at=finished_at,
                error=StageErrorRecord(code=code, message=str(exc)[:4096], recoverable=getattr(exc, "recoverable", False)),
            )
            envelope_logical = f"stages/{stage_id}/{attempt}/envelope.json"
            envelope_sha = self.store.put_json(envelope_logical, envelope.model_dump(mode="json"))
            with self.store.connect() as db:
                db.execute(
                    "UPDATE stages SET status='failed',finished_at=?,error_code=?,error_message=? WHERE stage_id=?",
                    (finished_at, code, str(exc)[:4096], stage_id),
                )
            self.store.event(self.run["run_id"], stage_id, attempt, "failed", error_code=code,
                             recoverable=getattr(exc, "recoverable", False), message=str(exc), envelope_sha256=envelope_sha)
            self.set_run_status(RunStatus.BLOCKED if getattr(exc, "recoverable", False) else RunStatus.ERROR)
            raise
        self._update_elapsed()
        finished_at = utc_now()
        envelope = StageAttemptEnvelope(
            schema_version="skill-refiner.stage-attempt/v1", run_id=self.run["run_id"],
            stage_id=stage_id, attempt=attempt, status="passed", input=input_ref, output=output_ref,
            budget_before=budget_before, budget_after=self._budget_snapshot(), started_at=started_at,
            finished_at=finished_at, error=None,
        )
        envelope_logical = f"stages/{stage_id}/{attempt}/envelope.json"
        envelope_sha = self.store.put_json(envelope_logical, envelope.model_dump(mode="json"))
        with self.store.connect() as db:
            db.execute(
                "UPDATE stages SET status='passed',output_sha256=?,finished_at=? WHERE stage_id=?",
                (output_sha, finished_at, stage_id),
            )
        self.store.event(self.run["run_id"], stage_id, attempt, "passed", output_sha256=output_sha,
                         input_sha256=input_sha, envelope_sha256=envelope_sha)
        self.store.export_state()
        return result

    def skip_stage(self, stage_id: str, reason: str) -> None:
        with self.store.connect() as db:
            db.execute("UPDATE stages SET status='skipped',finished_at=?,error_message=? WHERE stage_id=?",
                       (utc_now(), reason, stage_id))
        self.store.event(self.run["run_id"], stage_id, 0, "skipped", reason=reason)
        self.store.export_state()

    def reconcile_interrupted_stage(self) -> dict[str, Any] | None:
        """Close one crash-interrupted attempt without overwriting its evidence."""
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM stages WHERE status='running' ORDER BY ordinal"
            )]
        if not rows:
            return None
        if len(rows) != 1:
            raise TamperError("more than one stage is marked running in a serial workflow")
        row = rows[0]
        stage_id = row["stage_id"]
        attempt = int(row["attempt"])
        input_logical = f"stages/{stage_id}/{attempt}/input.json"
        input_value = self.store.get_json(input_logical)
        StageInputRecord.model_validate(input_value)
        input_ref = self._artifact_ref(
            input_logical, f"skill-refiner.stage.{stage_id}.input/v1", SCHEMA_PATH,
        )
        now = utc_now()
        budget = self._budget_snapshot()
        envelope = StageAttemptEnvelope(
            schema_version="skill-refiner.stage-attempt/v1", run_id=self.run["run_id"],
            stage_id=stage_id, attempt=attempt, status="failed", input=input_ref, output=None,
            budget_before=budget, budget_after=budget, started_at=row["started_at"] or now,
            finished_at=now,
            error=StageErrorRecord(code="INTERRUPTED", message="previous process ended without a terminal stage envelope", recoverable=True),
        )
        envelope_logical = f"stages/{stage_id}/{attempt}/envelope.json"
        envelope_sha = self.store.put_json(envelope_logical, envelope.model_dump(mode="json"))
        with self.store.connect() as db:
            db.execute(
                "UPDATE stages SET status='failed',finished_at=?,error_code='INTERRUPTED',error_message=? WHERE stage_id=?",
                (now, envelope.error.message, stage_id),
            )
        self.store.event(
            self.run["run_id"], stage_id, attempt, "failed", error_code="INTERRUPTED",
            recoverable=True, message=envelope.error.message, envelope_sha256=envelope_sha,
        )
        self.store.export_state()
        return {"stage_id": stage_id, "attempt": attempt, "envelope_sha256": envelope_sha}

    def requirements_text(self) -> str:
        return self.store.get_bytes("inputs/requirements.md").decode("utf-8")

    def freeze_inputs(self) -> dict[str, Any]:
        run = self.run
        frozen = {key: run[key] for key in ("requirements_sha256", "config_sha256", "controller_sha256", "mode")}
        return {"frozen": frozen, "stages": list(STAGES)}

    def make_plan(self, payload: dict[str, Any]) -> FrozenPlan:
        run = self.run
        value = {
            "schema_version": "skill-refiner.plan/v2",
            "requirements_sha256": run["requirements_sha256"],
            "config_sha256": run["config_sha256"],
            "requirements": payload.get("requirements", []),
            "work_items": payload.get("work_items", []),
            "development_cases": payload.get("development_cases", []),
            "acceptance_cases": payload.get("acceptance_cases", []),
            "stage_contracts": [record.model_dump(mode="json") for record in registry().stages],
        }
        plan = FrozenPlan.model_validate(value)
        if not plan.requirements or not plan.work_items or not plan.development_cases or not plan.acceptance_cases:
            raise VerificationError(
                "plan must include requirements, granular work items, development cases and held-out acceptance cases"
            )
        allowed_executors = {"{python}", *(f"{{executor:{name}}}" for name in self.config.executors)}
        for case in (*plan.development_cases, *plan.acceptance_cases):
            if not case.argv or case.argv[0] not in allowed_executors:
                raise VerificationError(
                    f"case {case.id} uses unsupported executable {case.argv[0] if case.argv else None!r}; "
                    f"allowed first argv tokens are {sorted(allowed_executors)}"
                )
            for assertion in case.assertions:
                if assertion.kind == "stdout_contains" and (
                    not isinstance(assertion.expected, str)
                    or "\r" in assertion.expected
                    or "\n" in assertion.expected
                ):
                    raise VerificationError(
                        f"assertion {assertion.id} must use a portable stdout substring without CR/LF"
                    )
                if assertion.kind == "manifest_unchanged":
                    raise VerificationError(
                        f"assertion {assertion.id} uses controller-only manifest_unchanged; "
                        "model plans must use file_exists or an executable content check"
                    )
        development_ids = {case.id for case in plan.development_cases}
        acceptance_ids = {case.id for case in plan.acceptance_cases}
        if len(development_ids) != len(plan.development_cases):
            raise VerificationError("development case ids must be unique")
        if len(acceptance_ids) != len(plan.acceptance_cases):
            raise VerificationError("acceptance case ids must be unique")
        if development_ids & acceptance_ids:
            raise VerificationError("acceptance case ids must be held out from development")
        self._validate_work_items(plan)
        write_json(self.run_dir / "plan.json", plan.model_dump(mode="json"))
        self.store.put_json("plan/frozen.json", plan.model_dump(mode="json"))
        return plan

    @staticmethod
    def _validate_work_items(plan: FrozenPlan) -> None:
        requirement_ids = [item.get("id") for item in plan.requirements]
        if any(not isinstance(item, str) or not item for item in requirement_ids):
            raise VerificationError("every requirement must have a non-empty string id")
        if len(set(requirement_ids)) != len(requirement_ids):
            raise VerificationError("requirement ids must be unique")

        work_ids = [item.id for item in plan.work_items]
        if len(set(work_ids)) != len(work_ids):
            raise VerificationError("work item ids must be unique")
        work_id_set = set(work_ids)
        requirement_id_set = set(requirement_ids)
        development = {case.id: case for case in plan.development_cases}
        acceptance = {case.id: case for case in plan.acceptance_cases}

        assigned_requirements: set[str] = set()
        assigned_development: set[str] = set()
        assigned_acceptance: set[str] = set()
        graph: dict[str, tuple[str, ...]] = {}
        for item in plan.work_items:
            for field_name, values in (
                ("scope", item.scope),
                ("requirement_ids", item.requirement_ids),
                ("depends_on", item.depends_on),
                ("done_when", item.done_when),
                ("development_case_ids", item.development_case_ids),
                ("acceptance_case_ids", item.acceptance_case_ids),
            ):
                if len(set(values)) != len(values):
                    raise VerificationError(f"work item {item.id} has duplicate {field_name}")
            unknown_requirements = set(item.requirement_ids) - requirement_id_set
            if unknown_requirements:
                raise VerificationError(
                    f"work item {item.id} references unknown requirements: {sorted(unknown_requirements)}"
                )
            unknown_dependencies = set(item.depends_on) - work_id_set
            if unknown_dependencies:
                raise VerificationError(
                    f"work item {item.id} references unknown dependencies: {sorted(unknown_dependencies)}"
                )
            if item.id in item.depends_on:
                raise VerificationError(f"work item {item.id} cannot depend on itself")
            unknown_development = set(item.development_case_ids) - set(development)
            unknown_acceptance = set(item.acceptance_case_ids) - set(acceptance)
            if unknown_development or unknown_acceptance:
                raise VerificationError(
                    f"work item {item.id} references unknown case ids: "
                    f"development={sorted(unknown_development)}, acceptance={sorted(unknown_acceptance)}"
                )
            item_requirements = set(item.requirement_ids)
            for case_id in item.development_case_ids:
                if not item_requirements.intersection(development[case_id].requirement_ids):
                    raise VerificationError(
                        f"development case {case_id} does not cover a requirement of work item {item.id}"
                    )
            for case_id in item.acceptance_case_ids:
                if not item_requirements.intersection(acceptance[case_id].requirement_ids):
                    raise VerificationError(
                        f"acceptance case {case_id} does not cover a requirement of work item {item.id}"
                    )
            assigned_requirements.update(item.requirement_ids)
            assigned_development.update(item.development_case_ids)
            assigned_acceptance.update(item.acceptance_case_ids)
            graph[item.id] = item.depends_on

        uncovered_requirements = requirement_id_set - assigned_requirements
        if uncovered_requirements:
            raise VerificationError(f"requirements are not covered by work items: {sorted(uncovered_requirements)}")
        unassigned_development = set(development) - assigned_development
        unassigned_acceptance = set(acceptance) - assigned_acceptance
        if unassigned_development or unassigned_acceptance:
            raise VerificationError(
                "test cases are not assigned to work items: "
                f"development={sorted(unassigned_development)}, acceptance={sorted(unassigned_acceptance)}"
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(work_id: str) -> None:
            if work_id in visiting:
                raise VerificationError(f"work item dependency cycle includes {work_id}")
            if work_id in visited:
                return
            visiting.add(work_id)
            for dependency in graph[work_id]:
                visit(dependency)
            visiting.remove(work_id)
            visited.add(work_id)

        for work_id in work_ids:
            visit(work_id)

    def load_plan(self) -> FrozenPlan:
        plan = FrozenPlan.model_validate(self.store.get_json("plan/frozen.json"))
        run = self.run
        if plan.requirements_sha256 != run["requirements_sha256"] or plan.config_sha256 != run["config_sha256"]:
            raise TamperError("frozen plan no longer matches frozen inputs")
        return plan

    def apply_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan_sha = digest_json(self.load_plan().model_dump(mode="json"))
        proposal = CandidateProposal.model_validate({
            "schema_version": "skill-refiner.candidate-proposal/v1",
            "plan_sha256": plan_sha,
            "changes": payload.get("changes", []),
            "summary": payload.get("summary", "candidate update"),
        })
        if proposal.plan_sha256 != plan_sha:
            raise TamperError("candidate proposal is not bound to the frozen plan")
        applied: list[str] = []
        for change in proposal.changes:
            relative = safe_relative_path(change.path)
            target = self.candidate_dir.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if change.operation == "write":
                target.write_text(change.content or "", encoding="utf-8", newline="\n")
            elif target.exists():
                if not target.is_file():
                    raise InputError(f"only files may be deleted: {change.path}")
                target.unlink()
            applied.append(f"{change.operation}:{relative.as_posix()}")
        ensure_safe_tree(self.candidate_dir)
        return {"summary": proposal.summary, "applied": applied, "manifest": tree_manifest(self.candidate_dir)}

    def static_validate(self) -> dict[str, Any]:
        ensure_safe_tree(self.candidate_dir)
        skill_file = self.candidate_dir / "SKILL.md"
        if not skill_file.is_file():
            raise VerificationError("candidate is missing SKILL.md")
        text = skill_file.read_text(encoding="utf-8")
        match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not match:
            raise VerificationError("SKILL.md requires YAML frontmatter")
        frontmatter = yaml.safe_load(match.group(1))
        if not isinstance(frontmatter, dict) or not frontmatter.get("name") or not frontmatter.get("description"):
            raise VerificationError("SKILL.md frontmatter requires name and description")
        return {"frontmatter": frontmatter, "manifest": tree_manifest(self.candidate_dir)}

    async def evaluate(self, cases: tuple[TestCase, ...], phase: str, *, raise_on_failure: bool = True) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for case in cases:
            self.check_budget(evaluations=1)
            result = await self._execute_case(case)
            self.store.put_json(f"evaluations/{phase}/{case.id}.json", result)
            results.append(result)
        passed = all(result["passed"] for result in results)
        report = {"phase": phase, "passed": passed, "results": results}
        if not passed:
            fingerprint = digest_json([{"id": item["case_id"], "failures": item["failures"]} for item in results if not item["passed"]])
            with self.store.connect() as db:
                db.execute("UPDATE runs SET last_failure_fingerprint=?", (fingerprint,))
            if raise_on_failure:
                raise VerificationError(f"{phase} evaluation failed", details=report)
        return report

    async def _execute_case(self, case: TestCase) -> dict[str, Any]:
        if not case.argv:
            raise VerificationError(f"case {case.id} has no argv")
        first, *remaining = case.argv
        if first == "{python}":
            executable = sys.executable
        elif first.startswith("{executor:") and first.endswith("}"):
            name = first[len("{executor:"):-1]
            profile = self.config.executors.get(name)
            if profile is None or profile.kind != "command" or not profile.argv:
                raise VerificationError(f"case {case.id} requests unknown command executor: {name}")
            executable, *prefix = profile.argv
            remaining = [*prefix, *profile.args, *remaining]
        else:
            raise VerificationError(f"case {case.id} executable must be {{python}} or a configured {{executor:name}}")
        argv = [executable, *(token.replace("{candidate}", str(self.candidate_dir)) for token in remaining)]
        environment = sanitized_environment(self.config.allowed_environment, ())
        creation: dict[str, Any] = {
            "cwd": str(self.candidate_dir), "env": environment,
            "stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            creation["creationflags"] = 0x00000200
        else:
            creation["start_new_session"] = True
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(*argv, **creation)
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(case.stdin.encode()), timeout=case.timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        failures: list[str] = []
        for assertion in case.assertions:
            expected = assertion.expected
            if assertion.kind == "exit_code" and process.returncode != expected:
                failures.append(f"{assertion.id}: exit code {process.returncode!r} != {expected!r}")
            elif assertion.kind == "stdout_contains" and str(expected) not in stdout_text:
                failures.append(f"{assertion.id}: stdout missing {expected!r}")
            elif assertion.kind == "file_exists" and not self.candidate_dir.joinpath(*safe_relative_path(str(expected)).parts).is_file():
                failures.append(f"{assertion.id}: file does not exist: {expected}")
            elif assertion.kind == "json_schema":
                try:
                    import jsonschema
                    jsonschema.validate(json.loads(stdout_text), expected)
                except Exception as exc:
                    failures.append(f"{assertion.id}: JSON schema assertion failed: {exc}")
            elif assertion.kind == "manifest_unchanged":
                current = tree_manifest(self.candidate_dir)
                if current != expected:
                    failures.append(f"{assertion.id}: manifest changed")
        if timed_out:
            failures.append(f"timeout after {case.timeout}s")
        return {
            "case_id": case.id, "passed": not failures, "failures": failures,
            "argv": argv, "exit_code": process.returncode, "timed_out": timed_out,
            "stdout": stdout_text, "stderr": stderr_text,
            "duration_seconds": round(time.monotonic() - started, 6),
        }

    def final_verify(self) -> dict[str, Any]:
        self.validate_contract_snapshot()
        self.validate_stage_index()
        self.validate_stage_attempts()
        self.validate_completed_stages()
        plan = self.load_plan()
        manifest = tree_manifest(self.candidate_dir)
        if not manifest:
            raise VerificationError("candidate is empty")
        with self.store.connect() as db:
            statuses = {row["stage_id"]: row["status"] for row in db.execute("SELECT stage_id,status FROM stages")}
        required = ("static_validation", "development_evaluation", "regression_evaluation", "acceptance_evaluation")
        incomplete = [stage for stage in required if statuses.get(stage) != "passed"]
        if incomplete:
            raise VerificationError(f"required verification stages have not passed: {incomplete}")
        value = {
            "plan_sha256": digest_json(plan.model_dump(mode="json")),
            "candidate_manifest": manifest,
            "candidate_manifest_sha256": digest_json(manifest),
            "verified_at": utc_now(),
        }
        self.store.put_json("verification/final.json", value)
        write_json(self.run_dir / "verification.json", value)
        self.set_run_status(RunStatus.VERIFIED)
        return value

    def prepare_delivery(self) -> dict[str, Any]:
        verification = self.store.get_json("verification/final.json")
        report = (
            f"# Skill Refiner report\n\n"
            f"Run: `{self.run['run_id']}`\n\n"
            f"Status: **verified**\n\n"
            f"Candidate manifest: `{verification['candidate_manifest_sha256']}`\n"
        )
        (self.run_dir / "report.md").write_text(report, encoding="utf-8", newline="\n")
        return {"candidate": str(self.candidate_dir), "report": str(self.run_dir / "report.md")}

    def verify_evidence(self) -> dict[str, Any]:
        if self.run["status"] not in {RunStatus.VERIFIED.value, RunStatus.ADOPTED.value}:
            raise VerificationError("run has not reached verified status")
        verification = self.store.get_json("verification/final.json")
        manifest = tree_manifest(self.candidate_dir)
        if digest_json(manifest) != verification["candidate_manifest_sha256"]:
            raise TamperError("candidate differs from the verified manifest")
        self.load_plan()
        self.validate_contract_snapshot()
        self.validate_stage_index()
        self.validate_stage_attempts()
        self.validate_completed_stages()
        with self.store.connect() as db:
            logical_paths = [row[0] for row in db.execute("SELECT logical_path FROM artifacts")]
        for logical_path in logical_paths:
            self.store.get_bytes(logical_path)
        return {"verified": True, "manifest_sha256": verification["candidate_manifest_sha256"],
                "artifacts_checked": len(logical_paths)}

    def validate_contract_snapshot(self) -> dict[str, Any]:
        frozen = StageContractRegistry.model_validate(self.store.get_json("contracts/stages.json"))
        expected = registry()
        if frozen != expected:
            raise TamperError("frozen stage contract differs from the packaged controller contract")
        contract_schema = json.loads(schema_file(CONTRACT_SCHEMA_PATH).read_text(encoding="utf-8"))
        jsonschema.validate(frozen.model_dump(mode="json"), contract_schema)
        checked = []
        for relative in (SCHEMA_PATH, ATTEMPT_SCHEMA_PATH, CONTRACT_SCHEMA_PATH, STAGES_SCHEMA_PATH):
            if self.store.get_bytes(relative) != schema_file(relative).read_bytes():
                raise TamperError(f"frozen schema differs from packaged schema: {relative}")
            checked.append(relative)
        return {"contract": "passed", "schemas": checked}

    def validate_stage_index(self) -> dict[str, Any]:
        index = StageIndex.model_validate(read_json(self.run_dir / "stages.json"))
        schema = json.loads(schema_file(STAGES_SCHEMA_PATH).read_text(encoding="utf-8"))
        jsonschema.validate(index.model_dump(mode="json"), schema)
        with self.store.connect() as db:
            contract = db.execute("SELECT content_sha256 FROM artifacts WHERE logical_path='contracts/stages.json'").fetchone()
            rows = [dict(row) for row in db.execute("SELECT * FROM stages ORDER BY ordinal")]
        if contract is None or index.contract_sha256 != contract["content_sha256"]:
            raise TamperError("stages index is not bound to the frozen stage contract")
        if tuple(item.stage_id for item in index.stages) != tuple(row["stage_id"] for row in rows):
            raise TamperError("stages index order differs from SQLite")
        for item, row in zip(index.stages, rows, strict=True):
            if (item.status.value != row["status"] or item.attempt != row["attempt"]
                    or item.input_sha256 != row["input_sha256"] or item.output_sha256 != row["output_sha256"]):
                raise TamperError(f"stages index differs from SQLite: {item.stage_id}")
        return {"stage_index": "passed", "count": len(index.stages)}

    def validate_stage_attempts(self) -> dict[str, Any]:
        with self.store.connect() as db:
            paths = [row[0] for row in db.execute(
                "SELECT logical_path FROM artifacts WHERE logical_path LIKE 'stages/%/envelope.json' ORDER BY logical_path"
            )]
        checked: list[str] = []
        for logical_path in paths:
            envelope = StageAttemptEnvelope.model_validate(self.store.get_json(logical_path))
            if envelope.stage_id not in STAGES:
                raise TamperError(f"unknown stage in attempt envelope: {envelope.stage_id}")
            expected_path = f"stages/{envelope.stage_id}/{envelope.attempt}/envelope.json"
            if logical_path != expected_path:
                raise TamperError(f"attempt envelope path mismatch: {logical_path}")
            input_value = self.store.get_json(envelope.input.logical_path)
            stage_input = StageInputRecord.model_validate(input_value)
            if stage_input.stage_id != envelope.stage_id:
                raise TamperError(f"attempt input stage mismatch: {logical_path}")
            if digest_json(input_value) != envelope.input.content_sha256:
                raise TamperError(f"attempt input hash mismatch: {logical_path}")
            if envelope.output is not None:
                output_value = self.store.get_json(envelope.output.logical_path)
                validate_stage_output(envelope.stage_id, output_value)
                if digest_json(output_value) != envelope.output.content_sha256:
                    raise TamperError(f"attempt output hash mismatch: {logical_path}")
            if envelope.status == "passed" and (envelope.output is None or envelope.error is not None):
                raise TamperError(f"passed attempt envelope is incomplete: {logical_path}")
            if envelope.status == "failed" and envelope.error is None:
                raise TamperError(f"failed attempt envelope has no error: {logical_path}")
            before = envelope.budget_before
            after = envelope.budget_after
            if (after.elapsed_seconds < before.elapsed_seconds or after.agent_calls < before.agent_calls
                    or after.evaluations < before.evaluations or after.repair_rounds < before.repair_rounds):
                raise TamperError(f"attempt budget counters decreased: {logical_path}")
            checked.append(logical_path)
        return {"checked_attempts": checked}

    def validate_completed_stages(self) -> dict[str, Any]:
        checked: list[str] = []
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM stages ORDER BY ordinal")]
        for row in rows:
            if row["status"] != "passed":
                continue
            stage_id = row["stage_id"]
            attempt = int(row["attempt"])
            input_logical = f"stages/{stage_id}/{attempt}/input.json"
            output_logical = f"stages/{stage_id}/{attempt}/output.json"
            envelope_logical = f"stages/{stage_id}/{attempt}/envelope.json"
            input_value = self.store.get_json(input_logical)
            output_value = self.store.get_json(output_logical)
            envelope_value = self.store.get_json(envelope_logical)
            stage_input = StageInputRecord.model_validate(input_value)
            envelope = StageAttemptEnvelope.model_validate(envelope_value)
            validate_stage_output(stage_id, output_value)
            if stage_input.stage_id != stage_id or envelope.stage_id != stage_id or envelope.attempt != attempt:
                raise TamperError(f"stage identity mismatch: {stage_id}")
            if envelope.status != "passed" or envelope.error is not None or envelope.output is None:
                raise TamperError(f"completed stage has invalid envelope: {stage_id}")
            if envelope.input.content_sha256 != row["input_sha256"] or envelope.output.content_sha256 != row["output_sha256"]:
                raise TamperError(f"stage hashes do not match SQLite state: {stage_id}")
            if envelope.input.schema_sha256 != schema_hash(SCHEMA_PATH) or envelope.output.schema_sha256 != schema_hash(SCHEMA_PATH):
                raise TamperError(f"stage schema hash changed: {stage_id}")
            checked.append(stage_id)
        return {"checked_stages": checked}


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    return value

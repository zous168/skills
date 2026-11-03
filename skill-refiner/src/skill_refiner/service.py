from __future__ import annotations

import importlib.metadata
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .errors import EnvironmentError, InputError, TamperError
from .hashing import read_json, write_json
from .kernel import RefinerControlKernel
from .models import RunMode, RunResult, RunStatus
from .security import safe_copy_tree, tree_manifest
from .self_architecture import self_architecture_report
from .workflow import PipelineRun, build_adk_workflow, run_adk_workflow, run_builtin_workflow


def _new_run_dir(output_root: Path) -> Path:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root / f"run-{uuid.uuid4().hex[:12]}"


async def _execute(kernel: RefinerControlKernel, fixture_root: Path, plan_only: bool) -> RunResult:
    kernel.set_run_status(RunStatus.RUNNING)
    pipeline = PipelineRun(kernel=kernel, fixture_root=fixture_root.resolve(), plan_only=plan_only)
    if kernel.config.runtime_driver == "adk":
        runtime = await run_adk_workflow(pipeline)
    else:
        runtime = await run_builtin_workflow(pipeline)
    if plan_only:
        kernel.set_run_status(RunStatus.PLANNED)
    state = kernel.run
    return RunResult(
        status=RunStatus(state["status"]), run_id=state["run_id"], run_dir=kernel.run_dir,
        stage="plan_gate" if plan_only else "prepare_delivery",
        verified=state["status"] == RunStatus.VERIFIED.value,
        message="plan frozen" if plan_only else "candidate verified",
        details={"runtime": runtime},
    )


async def create_run(
    requirements: Path,
    config: Path,
    output_root: Path,
    *,
    plan_only: bool = False,
) -> RunResult:
    run_dir = _new_run_dir(output_root)
    kernel = RefinerControlKernel.initialize(run_dir, RunMode.CREATE, requirements, config)
    write_json(run_dir / "runtime.json", {"fixture_root": str(config.resolve().parent), "plan_only": plan_only})
    return await _execute(kernel, config.resolve().parent, plan_only)


async def refine_run(
    source: Path,
    requirements: Path,
    config: Path,
    output_root: Path,
    *,
    plan_only: bool = False,
) -> RunResult:
    run_dir = _new_run_dir(output_root)
    kernel = RefinerControlKernel.initialize(run_dir, RunMode.REFINE, requirements, config, source)
    write_json(run_dir / "runtime.json", {"fixture_root": str(config.resolve().parent), "plan_only": plan_only})
    return await _execute(kernel, config.resolve().parent, plan_only)


async def resume_run(run_dir: Path) -> RunResult:
    kernel = RefinerControlKernel.open(run_dir)
    current = RunStatus(kernel.run["status"])
    if current in {RunStatus.VERIFIED, RunStatus.ADOPTED}:
        return RunResult(
            status=current, run_id=kernel.run["run_id"], run_dir=kernel.run_dir,
            stage=None, verified=current in {RunStatus.VERIFIED, RunStatus.ADOPTED},
            adopted=current == RunStatus.ADOPTED, message="run is already terminal; nothing to resume",
        )
    kernel.reconcile_interrupted_stage()
    metadata = read_json(kernel.run_dir / "runtime.json")
    plan_only = bool(metadata.get("plan_only"))
    if current == RunStatus.PLANNED:
        # A reviewed plan is a checkpoint, not a terminal outcome. Continuing
        # the same run must open the remaining stages without regenerating the
        # frozen plan or resetting any consumed budgets.
        plan_only = False
        metadata["plan_only"] = False
        write_json(kernel.run_dir / "runtime.json", metadata)
    return await _execute(kernel, Path(metadata["fixture_root"]), plan_only)


def verify_run(run_dir: Path) -> RunResult:
    kernel = RefinerControlKernel.open(run_dir)
    details = kernel.verify_evidence()
    state = kernel.run
    return RunResult(
        status=RunStatus(state["status"]), run_id=state["run_id"], run_dir=kernel.run_dir,
        stage="final_verify", verified=True, message="evidence and candidate verified", details=details,
    )


def adopt_run(run_dir: Path, destination: Path) -> RunResult:
    kernel = RefinerControlKernel.open(run_dir)
    kernel.verify_evidence()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise InputError("adopt destination must not exist or must be empty")
    if destination.exists():
        destination.rmdir()
    safe_copy_tree(kernel.candidate_dir, destination)
    if tree_manifest(destination) != tree_manifest(kernel.candidate_dir):
        raise TamperError("adopted destination does not match verified candidate")
    kernel.set_run_status(RunStatus.ADOPTED)
    state = kernel.run
    return RunResult(
        status=RunStatus.ADOPTED, run_id=state["run_id"], run_dir=kernel.run_dir,
        stage="adopt", verified=True, adopted=True, message=f"adopted to {destination}",
        details={"destination": str(destination)},
    )


def status_run(run_dir: Path) -> dict[str, Any]:
    kernel = RefinerControlKernel.open(run_dir)
    return kernel.store.export_state()


def doctor(config: Path | None = None) -> dict[str, Any]:
    checks: dict[str, Any] = {"python": "ok"}
    try:
        version = importlib.metadata.version("google-adk")
        major = int(version.split(".", 1)[0])
        if major != 2:
            raise EnvironmentError(f"google-adk 2.x required, found {version}")
        checks["google_adk"] = version
        # Construction itself invokes ADK graph validation.
        async def noop(_value: Any = None) -> dict[str, bool]:
            return {"ok": True}
        build_adk_workflow({stage: noop for stage in __import__("skill_refiner.kernel", fromlist=["STAGES"]).STAGES})
        checks["adk_graph"] = "ok"
    except Exception as exc:
        checks["adk_graph"] = f"error: {exc}"
        raise EnvironmentError("ADK runtime check failed", details=checks) from exc
    try:
        checks["self_architecture"] = self_architecture_report()
    except Exception as exc:
        checks["self_architecture"] = {"status": "error", "message": str(exc)}
        raise EnvironmentError("self-architecture check failed", details=checks) from exc
    if config is not None:
        from .models import RefinerConfig
        import json
        import yaml
        text = config.read_text(encoding="utf-8")
        value = json.loads(text) if config.suffix.lower() == ".json" else yaml.safe_load(text)
        parsed_config = RefinerConfig.model_validate(value)
        checks["config"] = "ok"
        deepseek_roles: dict[str, Any] = {}
        missing_credentials: list[str] = []
        for role, profile in parsed_config.roles.items():
            if profile.kind != "deepseek":
                continue
            available = any(os.environ.get(name) for name in profile.credential_env)
            deepseek_roles[role] = {
                "model": profile.model, "base_url": profile.base_url,
                "credential": "available" if available else "missing",
            }
            if not available:
                missing_credentials.append(role)
        if deepseek_roles:
            checks["deepseek"] = deepseek_roles
        cursor_sdk_roles: dict[str, Any] = {}
        for role, profile in parsed_config.roles.items():
            if profile.kind != "cursor_sdk":
                continue
            available = any(os.environ.get(name) for name in profile.credential_env)
            cursor_sdk_roles[role] = {"model": profile.model, "credential": "available" if available else "missing"}
            if not available:
                missing_credentials.append(role)
        if cursor_sdk_roles:
            try:
                checks["cursor_sdk_version"] = importlib.metadata.version("cursor-sdk")
            except importlib.metadata.PackageNotFoundError as exc:
                raise EnvironmentError("cursor-sdk is not installed", details=checks) from exc
            checks["cursor_sdk"] = cursor_sdk_roles
        if missing_credentials:
            raise EnvironmentError(
                f"DeepSeek credentials are missing for roles: {', '.join(sorted(missing_credentials))}",
                details=checks,
            )
    return checks

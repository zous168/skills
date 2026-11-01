"""Migrate a failed refinement plan with authenticated feedback and usage.

The original run is read-only. Only pre-plan, terminal, unevaluated runs are
supported; this is not a general migration for frozen plans or evaluations.
"""
from __future__ import annotations

from contextlib import contextmanager

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import jsonschema

from .common import RefinerError, digest, manifest, read_json, write_json
from .schemas import PLAN

_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v1/refine-plan-recovery.schema.json"
_BUDGET_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v1/refine-budget-amendment.schema.json"
_CAPACITY_ASSESSMENT_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v1/refine-capacity-assessment.schema.json"
_PLANNED_DEPENDENCY_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v8/planned-dependency-migration.schema.json"
_PLAN_V8_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v8/plan.schema.json"
_HASH = re.compile(r"^[a-f0-9]{64}$")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



def _io_path(path, *, force=False):
    """Use Windows extended-length syntax for long internal evidence paths."""
    path = Path(path).absolute()
    value = str(path)
    if os.name == "nt" and (force or len(value) >= 220) and not value.startswith("\\\\?\\"):
        if value.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + value[2:])
        return Path("\\\\?\\" + value)
    return path

def _safe_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RefinerError("Unsafe recovery path")
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(x in {"..", "."} for x in parts):
        raise RefinerError("Unsafe recovery path")
    root = _io_path(root, force=len(str(Path(root) / relative)) >= 220)
    current = root
    for part in parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RefinerError("Recovery evidence contains a link")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise RefinerError("Recovery evidence is missing or outside its run")
    return current


def _safe_root(path):
    path = Path(path).absolute()
    for component in reversed((path, *path.parents)):
        info = _io_path(component).lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RefinerError("Recovery root contains a linked or reparse component: " + str(component))
    return path.resolve(strict=True)


def _controller_hash(package):
    package = _safe_root(package)
    scripts = _safe_root(package / "scripts")
    if not scripts.is_dir() or not scripts.is_relative_to(package):
        raise RefinerError("Controller archive lacks a safe scripts directory")
    files = []
    def onerror(exc):
        raise RefinerError("Cannot enumerate controller archive safely") from exc
    for directory, subdirs, names in os.walk(scripts, followlinks=False, onerror=onerror):
        base = Path(directory)
        for name in subdirs:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Controller archive contains a linked directory: " + str(path))
            if not path.resolve().is_relative_to(scripts):
                raise RefinerError("Controller archive directory escapes scripts")
        for name in names:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Controller archive contains a linked file: " + str(path))
            if "__pycache__" in path.parts or path.suffix.lower() != ".py":
                continue
            if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(scripts):
                raise RefinerError("Controller archive contains an unsafe Python file")
            files.append(path)
    if not files:
        raise RefinerError("Controller archive is empty")
    return digest({p.relative_to(scripts).as_posix(): _sha(p) for p in sorted(files)})


def _schema():
    schema = read_json(_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _inventory(stage, root):
    found = {}
    for field in ("artifacts", "input_artifacts"):
        for row in stage.get(field, []):
            path = _safe_file(root, row.get("path"))
            checksum = _sha(path)
            if checksum != row.get("sha256") or path.stat().st_size != row.get("bytes"):
                raise RefinerError("Stage inventory artifact changed: " + str(row.get("path")))
            old = found.setdefault(row["path"], checksum)
            if old != checksum:
                raise RefinerError("Conflicting stage inventory")
    return found


def _validated_calls(root, inventory, expected_count, *, allow_later=False):
    all_directories = sorted(p for p in (root / "agent-calls").iterdir() if p.is_dir())
    directories = [root / "agent-calls" / f"{i:03}-plan" for i in range(1, expected_count + 1)]
    if any(not p.is_dir() for p in directories) or (not allow_later and all_directories != directories):
        raise RefinerError("Plan call ledger is incomplete or contains another role")
    raw_values, extra = [], {}
    for directory in directories:
        base = directory.relative_to(root).as_posix()
        for name in ("request.json", "request-manifest.json", "transmission.txt", "execution.json",
                     "process.execution.json", "process.log", "process.stdout.log", "process.stderr.log"):
            if f"{base}/{name}" not in inventory:
                raise RefinerError("Plan call lacks stage-authenticated evidence: " + name)
        req = read_json(_safe_file(root, f"{base}/request.json"))
        meta = read_json(_safe_file(root, f"{base}/request-manifest.json"))
        execution = read_json(_safe_file(root, f"{base}/execution.json"))
        process = read_json(_safe_file(root, f"{base}/process.execution.json"))
        serialized = json.dumps(req, ensure_ascii=False).encode("utf-8")
        transmitted = _safe_file(root, f"{base}/transmission.txt").read_bytes()
        if (req.get("role") != "plan" or meta.get("role") != "plan" or meta.get("status") != "invalid_response"
                or meta.get("request_sha256") != hashlib.sha256(serialized).hexdigest()
                or meta.get("transmitted_sha256") != hashlib.sha256(transmitted).hexdigest()
                or meta.get("request_bytes") != len(serialized) or meta.get("transmitted_bytes") != len(transmitted)
                or execution.get("status") != "invalid_response" or execution.get("role") != "plan"
                or not execution.get("finished_at") or process.get("status") != "completed"
                or process.get("exit_code") != 0 or not process.get("finished_at")):
            raise RefinerError("Plan call request or terminal evidence is inconsistent")
        raw_rel = f"{base}/raw-response.txt"
        raw_file = _safe_file(root, raw_rel)
        raw = raw_file.read_text(encoding="utf-8")
        if _text_sha(raw) != meta.get("response_sha256"):
            raise RefinerError("Invalid plan response does not match its authenticated manifest")
        if raw_values and req.get("input", {}).get("previous_invalid_response") != raw_values[-1][:12000]:
            raise RefinerError("Prior plan retry chain is not bound to the previous raw response")
        extra[raw_rel] = _sha(raw_file)
        raw_values.append(raw)
    return directories, raw_values, extra


def verify_recovery(work, contract):
    """Verify migration contract, immutable old evidence and repair payload on resume."""
    root = Path(work).resolve()
    preliminary = read_json(_safe_file(root, "refine-plan-recovery.json"))
    if preliminary.get("schema_version") == "skill-refiner.refine-plan-recovery/v2":
        return _verify_recovery_chain(root, contract, preliminary)
    record_path = _safe_file(root, "refine-plan-recovery.json")
    data = read_json(record_path)
    jsonschema.Draft202012Validator(_schema()).validate(data)
    if (contract.get("refine_plan_recovery_sha256") != digest(data)
            or contract.get("refine_plan_recovery_schema_sha256") != _sha(_SCHEMA)):
        raise RefinerError("Refine recovery contract or packaged schema changed")
    saved_contract = read_json(_safe_file(root, "recovery-source-contract.json"))
    comparable = {k: v for k, v in contract.items() if k not in {"refine_plan_recovery_sha256", "refine_plan_recovery_schema_sha256"}}
    if comparable != {**saved_contract, "controller_sha256": contract.get("controller_sha256")}:
        raise RefinerError("Migrated execution contract or limits differ from the frozen original")
    old_stages = read_json(_safe_file(root, "recovery-source-stages.json"))
    old_state = read_json(_safe_file(root, "recovery-source-state.json"))
    if old_state.get("agent_calls") != data["usage"]["agent_calls"] or old_state.get("elapsed_seconds") != data["usage"]["elapsed_seconds"]:
        raise RefinerError("Migrated historical usage differs from the source record")
    for name, field in (("recovery-ancestor-state.json", "state_sha256"),
                        ("recovery-ancestor-contract.json", "contract_sha256"),
                        ("recovery-ancestor-stages.json", "stages_sha256")):
        if _sha(_safe_file(root, name)) != data["ancestor"][field]:
            raise RefinerError("Ancestor provenance changed: " + name)
    for rel, sha in data["ancestor"]["evidence"].items():
        if _sha(_safe_file(root, "recovery-ancestor-evidence/" + rel)) != sha:
            raise RefinerError("Ancestor call evidence changed: " + rel)
    provenance = root / "recovery-source-evidence"
    old_inventory = {}
    for row in old_stages.get("stages", []):
        old_inventory.update(_inventory(row, provenance))
    for rel, sha in old_inventory.items():
        if data["source_files"].get(rel) != sha:
            raise RefinerError("Recovered stage inventory does not match provenance")
    _validated_calls(provenance, old_inventory, data["usage"]["agent_calls"])
    for rel, sha in data["source_files"].items():
        if not _HASH.fullmatch(sha) or _sha(_safe_file(provenance, rel)) != sha:
            raise RefinerError("Migrated source evidence changed: " + rel)
        # Frozen inputs and old calls also remain at their execution paths.
        # Old reports and preflight outputs live only under provenance so new
        # execution can generate its own outputs without invalidating history.
        if (rel == "inputs.json" or rel.startswith(("original/", "agent-calls/", "profiles/"))
                or rel == "backend-config.json") and _sha(_safe_file(root, rel)) != sha:
            raise RefinerError("Migrated execution input changed: " + rel)
    for name, field in (("recovery-source-contract.json", "source_contract_sha256"),
                        ("recovery-source-state.json", "source_state_sha256"),
                        ("recovery-source-stages.json", "source_stages_sha256")):
        if _sha(_safe_file(root, name)) != data[field]:
            raise RefinerError("Refine recovery provenance changed: " + name)
    raw = _safe_file(root, "agent-calls/" + data["latest_call"] + "/raw-response.txt").read_text(encoding="utf-8")
    if _text_sha(raw) != data["latest_response_text_sha256"]:
        raise RefinerError("Migrated latest plan response changed")
    from .runtime import decode_codex_response
    from .agent import _plan_schema_errors, _plan_response_has_secrets
    try:
        value = decode_codex_response(json.loads(raw), PLAN)
    except (ValueError, TypeError) as exc:
        raise RefinerError("Migrated plan is not decodable") from exc
    if _plan_response_has_secrets(raw, value) or _plan_schema_errors(PLAN, value) != data["schema_errors"]:
        raise RefinerError("Recovered diagnostics changed or response contains credentials")
    req = [row.get("id") for row in value.get("requirements", []) if isinstance(row, dict)]
    cases = [row.get("id") for row in value.get("cases", []) if isinstance(row, dict)]
    if req != data["requirement_ids"] or cases != data["case_ids"]:
        raise RefinerError("Recovered plan matrix changed")
    return {"previous_plan": value, "previous_invalid_response": raw,
            "contract_errors": data["schema_errors"], "usage": data["usage"]}


def _reject_evaluation_evidence(root, state, stages):
    if (state.get("target_evaluations", 0) != 0 or state.get("evaluation_history", []) != []
            or state.get("active_evaluation") or state.get("rounds", []) != []
            or state.get("skill_evaluation") not in (None, "not_started")):
        raise RefinerError("Historical target evaluation evidence is nonempty or inconsistent")
    phase = state.get("phase_results", {}).get("skill_evaluation", {})
    allowed_unstarted = {"not_started", "pending", "skipped"}
    if phase and (phase.get("status") not in allowed_unstarted
                  or not isinstance(phase.get("suites", {}), dict)
                  or any(value not in allowed_unstarted for value in phase.get("suites", {}).values())):
        raise RefinerError("Historical target evaluation phase or suite has started")
    history = state.get("phase_history", [])
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise RefinerError("Historical phase ledger is malformed")
    if any(row.get("phase") == "skill_evaluation" and row.get("status") not in allowed_unstarted
           for row in history):
        raise RefinerError("Historical target evaluation phase has started")
    for stage in stages.get("stages", []):
        if stage.get("id") in {"baseline", "repair", "regression", "acceptance", "delivery"} and stage.get("status") not in {"pending", "skipped"}:
            raise RefinerError("Historical execution stage has started")
    # Controller-owned locations only. The immutable original Skill and call
    # working directories may legitimately contain identically named assets.
    for name in ("baseline", "iterations", "acceptance", "repair", "regression", "candidate", "candidates"):
        if (root / name).exists() or (root / name).is_symlink():
            raise RefinerError("Historical target evaluation directory exists: " + name)
    if (root / "oracle.lock.json").exists() or (root / "oracle.lock.json").is_symlink():
        raise RefinerError("Historical frozen oracle evidence exists")


def migrate(source_run, archive, ancestor_run, output, *, source_snapshot=None,
            amend_max_cases=None, amend_max_evaluations=None, capacity_assessment=None,
            planned_dependency_migration=False):
    source = _safe_root(source_run)
    archive = _safe_root(archive)
    ancestor = _safe_root(ancestor_run)
    output_path = Path(output).absolute()
    target = _safe_root(output_path.parent) / output_path.name
    if target.exists() or any(target.is_relative_to(root) or root.is_relative_to(target)
                              for root in (source, archive, ancestor)):
        raise RefinerError("Recovery output must be a new independent directory")
    if not target.parent.is_dir():
        raise RefinerError("Recovery parent directory must exist")
    old_contract = read_json(_safe_file(source, "resume-contract.json"))
    state = read_json(_safe_file(source, "state.json"))
    stages = read_json(_safe_file(source, "stages.json"))
    old_hash = _controller_hash(archive)
    if old_hash != old_contract.get("controller_sha256"):
        raise RefinerError("Archived controller does not match original run")
    if old_contract.get("refine_plan_recovery_sha256"):
        if source_snapshot is None:
            raise RefinerError("Repeated refinement migration requires an independent terminal source snapshot")
        return _migrate_recovery_chain(source, archive, ancestor, target, source_snapshot,
                                       amend_max_cases=amend_max_cases,
                                       amend_max_evaluations=amend_max_evaluations,
                                       capacity_assessment=capacity_assessment,
                                       planned_dependency_migration=planned_dependency_migration)
    if planned_dependency_migration:
        raise RefinerError("Planned dependency migration requires an authenticated recovery chain")
    if amend_max_cases is not None or amend_max_evaluations is not None or capacity_assessment is not None:
        raise RefinerError("Capacity amendment requires an authenticated repeated recovery")
    if (old_contract.get("schema_version") != "skill-refiner.resume/v1"
            or stages.get("mode") != "refine" or stages.get("run_id") != source.name
            or state.get("status") != "plan_not_passed" or stages.get("status") != "plan_not_passed"
            or stages.get("active_stage") is not None or state.get("active_agent_calls")
            or state.get("target_evaluations", 0) != 0 or state.get("rounds")):
        raise RefinerError("Only a terminal, unevaluated, plan-not-passed run can be migrated")
    _reject_evaluation_evidence(source, state, stages)
    rows = {row.get("id"): row for row in stages.get("stages", [])}
    if rows.get("inputs", {}).get("status") != "completed" or rows.get("planning", {}).get("status") != "blocked":
        raise RefinerError("Expected completed inputs and blocked planning")
    if any(row.get("status") == "completed" for key, row in rows.items() if key != "inputs"):
        raise RefinerError("Cannot migrate a run with other completed stages")
    for reference in stages.get("schema_refs", []):
        p = _safe_file(archive, reference.get("path"))
        if _sha(p) != reference.get("sha256"):
            raise RefinerError("Archived stage schema does not match the original registry")
    files = _inventory(rows["inputs"], source)
    files.update(_inventory(rows["planning"], source))
    for row in rows.values():
        files.update(_inventory(row, source))
    previous_input = read_json(_safe_file(source, "inputs.json"))
    if digest(previous_input) != old_contract.get("inputs_sha256"):
        raise RefinerError("Original inputs contract changed")
    skill = Path(old_contract.get("source", ""))
    original = source / "original" / skill.name
    if (state.get("source") != str(skill) or not original.is_dir()
            or manifest(skill) != old_contract.get("source_manifest")
            or manifest(original) != old_contract.get("source_manifest")):
        raise RefinerError("Source Skill or immutable original snapshot changed")
    calls = state.get("agent_calls")
    if not isinstance(calls, int) or calls < 1:
        raise RefinerError("Missing inherited call usage")
    dirs, raw, extra = _validated_calls(source, files, calls)
    for directory in dirs:
        request = read_json(directory / "request.json")
        for key in ("files", "requirements", "standards"):
            if request.get("input", {}).get(key) != previous_input.get(key):
                raise RefinerError("Original plan request differs from frozen inputs: " + key)
    files.update(extra)
    from .agent import _plan_schema_errors, _plan_response_has_secrets
    from .runtime import decode_codex_response
    try:
        previous = decode_codex_response(json.loads(raw[-1]), PLAN)
    except (TypeError, ValueError) as exc:
        raise RefinerError("Latest invalid plan cannot be decoded") from exc
    if _plan_response_has_secrets(raw[-1], previous):
        raise RefinerError("Latest invalid plan contains credentials")
    errors = _plan_schema_errors(PLAN, previous)
    if not errors:
        raise RefinerError("Latest source plan unexpectedly validates under current schema")
    requirements = [row.get("id") for row in previous.get("requirements", []) if isinstance(row, dict)]
    cases = [row.get("id") for row in previous.get("cases", []) if isinstance(row, dict)]
    if (not requirements or not cases or any(not isinstance(x, str) or not x for x in requirements + cases)
            or len(set(requirements)) != len(requirements) or len(set(cases)) != len(cases)):
        raise RefinerError("Latest plan has no preservable unique requirement/case IDs")
    # Authenticate previous controller's canonical plan schema before using current diagnostics.
    code = ("import sys;sys.path.insert(0,sys.argv[1]);from refiner.schemas import PLAN;"
            "from refiner.common import digest,read_json;from refiner.stages import StageManifest;"
            "s=read_json(sys.argv[2]);StageManifest._validate_manifest_schema(s);"
            "print(digest(PLAN))")
    proc = subprocess.run([sys.executable, "-B", "-c", code, str(archive / "scripts"), str(source / "stages.json")],
                          cwd=str(archive), capture_output=True, text=True)
    if proc.returncode or proc.stdout.strip() != digest(PLAN):
        raise RefinerError("Original and current canonical plan schemas differ")
    anc_state = read_json(_safe_file(ancestor, "state.json"))
    anc_contract = read_json(_safe_file(ancestor, "resume-contract.json"))
    if (anc_state.get("source") != str(skill) or anc_contract.get("source") != str(skill)
            or anc_contract.get("source_manifest") != old_contract.get("source_manifest")
            or anc_state.get("active_agent_calls") or anc_state.get("target_evaluations", 0) != 0
            or anc_state.get("status") not in {"planning_error", "plan_not_passed", "error"}
            or not isinstance(anc_state.get("agent_calls"), int)
            or not isinstance(anc_state.get("elapsed_seconds"), (int, float))):
        raise RefinerError("Ancestor budget evidence is incomplete or refers to a different Skill")
    anc_stages = read_json(_safe_file(ancestor, "stages.json"))
    if anc_stages.get("mode") != "refine" or anc_stages.get("run_id") != ancestor.name:
        raise RefinerError("Ancestor stage manifest is invalid")
    _reject_evaluation_evidence(ancestor, anc_state, anc_stages)
    anc_evidence = {}
    for row in anc_stages.get("stages", []):
        anc_evidence.update(_inventory(row, ancestor))
    ancestor_calls = sorted(p.name for p in (ancestor / "agent-calls").iterdir() if p.is_dir())
    if len(ancestor_calls) != anc_state["agent_calls"] or not all(
            f"agent-calls/{name}/request-manifest.json" in anc_evidence for name in ancestor_calls):
        raise RefinerError("Ancestor call accounting does not match its stage evidence")
    elapsed = state.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or elapsed < 0 or calls > old_contract["limits"]["max_agent_calls"]:
        raise RefinerError("Original run usage is invalid")
    source_hashes = {name: _sha(_safe_file(source, name)) for name in ("state.json", "stages.json", "resume-contract.json")}
    # Extra non-stage backend artifacts remain exclusively in the unchanged source run.
    omitted = sorted(path.relative_to(source).as_posix() for d in dirs for path in d.rglob("*")
                     if path.is_file() and path.relative_to(source).as_posix() not in files)
    config_file = source / "backend-config.json"
    if config_file.exists():
        if read_json(_safe_file(source, "backend-config.json")) != state.get("backends"):
            raise RefinerError("Backend configuration differs from original state")
        files["backend-config.json"] = _sha(config_file)
        for profile in state["backends"].get("executors", []):
            identifier = profile.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
                raise RefinerError("Unsafe executor profile ID")
            relative = "profiles/" + identifier + ".json"
            profile_file = _safe_file(source, relative)
            if read_json(profile_file) != profile:
                raise RefinerError("Executor profile differs from original state")
            files[relative] = _sha(profile_file)
    elif state.get("backends"):
        raise RefinerError("Original backend configuration is missing")
    allowed = set(files) | set(omitted) | {"state.json", "stages.json", "resume-contract.json"}
    allowed_dirs = {parent.as_posix() for relative in allowed for parent in Path(relative).parents
                    if parent.as_posix() != "."}
    def source_walk_error(exc):
        raise RefinerError("Cannot enumerate source evidence safely") from exc
    for directory, subdirs, names in os.walk(source, followlinks=False, onerror=source_walk_error):
        base = Path(directory)
        for name in subdirs:
            path = base / name
            relative = path.relative_to(source).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or relative not in allowed_dirs:
                raise RefinerError("Unexpected or linked source directory: " + relative)
        for name in names:
            path = base / name
            relative = path.relative_to(source).as_posix()
            _safe_file(source, relative)
            if relative not in allowed:
                raise RefinerError("Unexpected source artifact: " + relative)
    for relative in omitted:
        _safe_file(source, relative)
        if not any(relative == f"agent-calls/{d.name}/{suffix}" for d in dirs
                   for suffix in ("backend-result.json", "response.txt", "cwd/response-schema.json")):
            raise RefinerError("Unknown unauthenticated plan call evidence: " + relative)
    data = {"schema_version": "skill-refiner.refine-plan-recovery/v1", "source_run": str(source),
            "source_controller_sha256": old_hash, "source_contract_sha256": source_hashes["resume-contract.json"],
            "source_state_sha256": source_hashes["state.json"], "source_stages_sha256": source_hashes["stages.json"],
            "source_files": files, "source_omissions": omitted, "latest_call": dirs[-1].name,
            "latest_response_text_sha256": _text_sha(raw[-1]), "schema_errors": errors,
            "requirement_ids": requirements, "case_ids": cases,
            "usage": {"agent_calls": calls, "elapsed_seconds": elapsed, "target_evaluations": 0},
            "ancestor": {"run": str(ancestor), "state_sha256": _sha(ancestor / "state.json"),
                         "contract_sha256": _sha(ancestor / "resume-contract.json"),
                         "stages_sha256": _sha(ancestor / "stages.json"), "evidence": anc_evidence,
                         "agent_calls": anc_state["agent_calls"], "elapsed_seconds": anc_state["elapsed_seconds"],
                         "target_evaluations": 0}}
    jsonschema.Draft202012Validator(_schema()).validate(data)
    current = _controller_hash(Path(__file__).resolve().parents[2])
    if current == old_hash:
        raise RefinerError("Recovery migration requires a different controller version")
    with tempfile.TemporaryDirectory(prefix=".refine-migrate-", dir=target.parent) as tmp:
        temp = Path(tmp) / target.name
        temp.mkdir()
        for relative in files:
            source_file = _safe_file(source, relative)
            destination = temp / "recovery-source-evidence" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination)
            if (relative == "inputs.json" or relative.startswith(("original/", "agent-calls/", "profiles/"))
                    or relative == "backend-config.json"):
                destination = temp / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, destination)
        for old_name, new_name in (("state.json", "recovery-source-state.json"),
                                   ("stages.json", "recovery-source-stages.json"),
                                   ("resume-contract.json", "recovery-source-contract.json")):
            shutil.copyfile(source / old_name, temp / new_name)
        for name in ("state.json", "resume-contract.json", "stages.json"):
            shutil.copyfile(ancestor / name, temp / ("recovery-ancestor-" + ("contract.json" if name == "resume-contract.json" else name)))
        for relative in anc_evidence:
            destination = temp / "recovery-ancestor-evidence" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_safe_file(ancestor, relative), destination)
        state_copy = copy.deepcopy(state); state_copy["workspace"] = str(target.resolve())
        stage_copy = copy.deepcopy(stages); stage_copy["run_id"] = target.name
        # A new controller may add schema versions, but it may not remove or
        # modify any schema registered by the old run. The original registry
        # remains preserved in recovery-source-stages.json.
        from .artifact_validation import all_schema_refs, schema_ref_compatible
        from .stages import StageManifest
        new_refs = all_schema_refs()
        new_by_id = {row["id"]: row for row in new_refs}
        for prior in stages["schema_refs"]:
            if prior.get("id") not in new_by_id or not schema_ref_compatible(prior, new_by_id[prior["id"]]):
                raise RefinerError("Current controller changed a historical stage schema")
        stage_copy["schema_refs"] = new_refs
        StageManifest._validate_manifest_schema(stage_copy)
        contract_copy = copy.deepcopy(old_contract); contract_copy["controller_sha256"] = current
        contract_copy["refine_plan_recovery_sha256"] = digest(data)
        contract_copy["refine_plan_recovery_schema_sha256"] = _sha(_SCHEMA)
        write_json(temp / "refine-plan-recovery.json", data)
        write_json(temp / "state.json", state_copy)
        write_json(temp / "stages.json", stage_copy)
        write_json(temp / "resume-contract.json", contract_copy)
        verify_recovery(temp, contract_copy)
        # Source bytes and stage-recorded artifacts must still match at commit.
        for relative, sha in {**files, **source_hashes}.items():
            if _sha(_safe_file(source, relative)) != sha:
                raise RefinerError("Source changed while migrating: " + relative)
        if _controller_hash(archive) != old_hash:
            raise RefinerError("Controller archive changed while migrating")
        for reference in stages.get("schema_refs", []):
            if _sha(_safe_file(archive, reference["path"])) != reference["sha256"]:
                raise RefinerError("Archived schema changed while migrating")
        for relative, sha in anc_evidence.items():
            if _sha(_safe_file(ancestor, relative)) != sha:
                raise RefinerError("Ancestor evidence changed while migrating: " + relative)
        temp.rename(target)
    return {"status": "migrated", "workspace": str(target), "reused_stages": ["inputs"],
            "plan_status": "not_passed", "inherited": data["usage"],
            "ancestor": data["ancestor"], "schema_errors": len(errors)}



_CHAIN_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v2/refine-plan-recovery.schema.json"


def _chain_tree(root, *, archive=False):
    root = _io_path(_safe_root(root), force=True)
    result = {}
    def fail(exc):
        raise RefinerError("Cannot enumerate recovery chain safely") from exc
    for directory, subdirs, names in os.walk(root, followlinks=False, onerror=fail):
        base = Path(directory)
        for name in list(subdirs):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Recovery chain contains a linked directory")
            if archive and base == root and name not in {"scripts", "schemas"}:
                subdirs.remove(name)
            elif archive and name == "__pycache__":
                subdirs.remove(name)
        for name in names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if archive and not (relative.startswith("schemas/") or (relative.startswith("scripts/") and path.suffix.lower() == ".py")):
                continue
            _safe_file(root, relative)
            result[relative] = _sha(path)
    return result


def _chain_snapshot(path, source, expected):
    path = _safe_root(path)
    if not path.is_file():
        raise RefinerError("Independent source snapshot is not a file")
    record = read_json(path)
    rows = record.get("files") if isinstance(record, dict) else None
    if not isinstance(rows, list) or not isinstance(record.get("source"), str):
        raise RefinerError("Independent source snapshot is malformed")
    if Path(record["source"]).resolve() != Path(source).resolve():
        raise RefinerError("Independent source snapshot refers to a different run")
    files = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or row["path"] in files:
            raise RefinerError("Independent source snapshot has invalid or duplicate entries")
        file = _safe_file(source, row["path"])
        if type(row.get("bytes")) is not int or file.stat().st_size != row["bytes"] or _sha(file) != row.get("sha256"):
            raise RefinerError("Independent source snapshot differs from source")
        files[row["path"]] = row["sha256"]
    if files != expected:
        raise RefinerError("Independent source snapshot has missing or extra files")
    return _sha(path)


def _archived_verify_at(archive, source):
    # The exact archived code is authenticated against the old resume contract
    # before execution. -B prevents any mutation of the source or archive.
    code = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
            "from refiner.refine_plan_recovery import verify_recovery;"
            "from refiner.common import read_json;from pathlib import Path;"
            "root=Path(sys.argv[2]);contract=read_json(root/'resume-contract.json');"
            "result=verify_recovery(root,contract);"
            "print(json.dumps(result,ensure_ascii=False))")
    proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code,
                           str(_io_path(archive / "scripts", force=True)), str(_io_path(source, force=True))], cwd=str(_io_path(archive, force=True)),
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        # Preserve an actionable exception class / WinError without forwarding
        # arbitrary archived stderr, paths, input text, or credentials.
        rows = proc.stderr.splitlines()
        last = rows[-1] if rows else ""
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::\s*\[WinError ([0-9]+)\])?", last)
        detail = (": " + match.group(1) + (" (WinError " + match.group(2) + ")" if match.group(2) else "")) if match else ""
        raise RefinerError("Prior recovery contract does not verify using its archived controller" + detail)
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise RefinerError("Archived recovery verifier returned invalid evidence") from exc
    if not isinstance(result, dict) or not isinstance(result.get("usage"), dict):
        raise RefinerError("Archived recovery verifier returned incomplete evidence")
    return result


def _archived_verify(archive, source):
    """Run nested historical verifiers from a short, hash-checked Windows copy.

    Windows CreateProcess may reject a long current directory even when Python
    file operations accept the extended-length path. Historical verifiers are
    immutable and can recursively spawn another verifier with its archive as
    cwd, so simply adding a path prefix to the outer process is insufficient.
    """
    if os.name != "nt":
        return _archived_verify_at(archive, source)
    archive, source = _safe_root(archive), _safe_root(source)
    source_files = _chain_tree(source)
    archive_files = _chain_tree(archive, archive=True)
    scratch = Path(tempfile.mkdtemp(prefix="ra-"))
    try:
        copy_source, copy_archive = scratch / "s" / source.name, scratch / "a"
        for src, inventory, dest in ((source, source_files, copy_source),
                                     (archive, archive_files, copy_archive)):
            for relative in inventory:
                path = _io_path(dest / relative, force=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_safe_file(src, relative), path)
        if _chain_tree(copy_source) != source_files or _chain_tree(copy_archive, archive=True) != archive_files:
            raise RefinerError("Short-path archived verifier copy differs from authenticated evidence")
        result = _archived_verify_at(copy_archive, copy_source)
        if (_chain_tree(copy_source) != source_files or _chain_tree(copy_archive, archive=True) != archive_files
                or _chain_tree(source) != source_files or _chain_tree(archive, archive=True) != archive_files):
            raise RefinerError("Archived verifier or source changed during short-path verification")
        path_holders = [result, *result.get("subsequent_controls", [])]
        for holder in path_holders:
            for field in ("source_evidence_root", "pending_source_evidence_root"):
                if field in holder:
                    copied_root = _io_path(copy_source, force=True)
                    evidence = _io_path(holder[field], force=True)
                    if not evidence.is_relative_to(copied_root):
                        raise RefinerError("Archived evidence path is outside its short-path source copy")
                    holder[field] = str(_io_path(source / evidence.relative_to(copied_root), force=True))
    except BaseException as primary:
        try:
            shutil.rmtree(_io_path(scratch, force=True))
        except OSError as cleanup:
            primary.add_note("Archived verifier temporary cleanup failed: " + type(cleanup).__name__)
        raise
    else:
        shutil.rmtree(_io_path(scratch, force=True))
        return result



def _chain_stage_inventory(stage, source):
    # The earlier migration retained skipped verification references while the
    # live root later rewrote those names. Accept only the original authenticated
    # v1 provenance, never treat the skipped stage as completed.
    for field in ("artifacts", "input_artifacts"):
        for row in stage.get(field, []):
            path = _safe_file(source, row.get("path"))
            if _sha(path) == row.get("sha256") and path.stat().st_size == row.get("bytes"):
                continue
            prior = read_json(_safe_file(source, "refine-plan-recovery.json"))
            name = row.get("path")
            if (stage.get("id") != "verification" or stage.get("status") != "skipped"
                    or prior.get("schema_version") != "skill-refiner.refine-plan-recovery/v1"
                    or name not in {"report.md", "runtime-preflight-before-planning.json"}
                    or prior.get("source_files", {}).get(name) != row.get("sha256")):
                raise RefinerError("Recovery stage artifact changed: " + str(name))
            old = _safe_file(source, "recovery-source-evidence/" + name)
            if _sha(old) != row.get("sha256") or old.stat().st_size != row.get("bytes"):
                raise RefinerError("Recovery skipped-stage archived evidence changed")

def _chain_control_records(source, paths, count, *, pending_prelaunch=False,
                           authenticated_prelaunch=None, allow_pending=False):
    calls = source / "agent-calls"
    dirs = sorted(p for p in calls.iterdir() if p.is_dir())
    if len(dirs) != count or any(not re.fullmatch(r"[0-9]{3}-.+", p.name)
                                 or int(p.name.split("-", 1)[0]) != i
                                 for i, p in enumerate(dirs, 1)):
        raise RefinerError("Recovery call ledger is incomplete or duplicated")
    terminal_dirs = dirs[:-1] if pending_prelaunch else dirs
    plans = []
    for directory in terminal_dirs:
        if directory.name == authenticated_prelaunch:
            continue
        base = directory.relative_to(source).as_posix()
        request = read_json(_safe_file(source, base + "/request.json"))
        meta = read_json(_safe_file(source, base + "/request-manifest.json"))
        execution = read_json(_safe_file(source, base + "/execution.json"))
        process = read_json(_safe_file(source, base + "/process.execution.json"))
        role = directory.name.split("-", 1)[1]
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        transmitted = _safe_file(source, base + "/transmission.txt").read_bytes()
        if (request.get("role") != role or meta.get("role") != role or execution.get("role") != role
                or meta.get("request_sha256") != hashlib.sha256(encoded).hexdigest()
                or meta.get("request_bytes") != len(encoded)
                or meta.get("transmitted_sha256") != hashlib.sha256(transmitted).hexdigest()
                or meta.get("transmitted_bytes") != len(transmitted)
                or not process.get("finished_at") or not execution.get("finished_at")):
            raise RefinerError("Recovery call evidence is inconsistent: " + directory.name)
        status = meta.get("status")
        if (status == "completed" and execution.get("status") == "completed"
                and process.get("status") == "completed" and process.get("exit_code") == 0):
            response = read_json(_safe_file(source, base + "/response.json"))
            checksum = hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if meta.get("response_sha256") != checksum:
                raise RefinerError("Recovery call response hash differs")
            if role == "plan": plans.append((directory.name, response))
        elif (status == "invalid_response" and execution.get("status") == "invalid_response"
              and process.get("status") == "completed" and process.get("exit_code") == 0):
            raw = _safe_file(source, base + "/raw-response.txt").read_text(encoding="utf-8")
            if meta.get("response_sha256") != _text_sha(raw):
                raise RefinerError("Recovery invalid response hash differs")
        elif (status in {"execution_error", "cancelled"} and execution.get("status") == status
              and process.get("status") in {"completed", "execution_error", "cancelled", "timed_out"}
              and type(process.get("exit_code")) is int):
            # A failed, but confirmed exited, later call is still charged. It is
            # historical evidence, never a plan response or a completed review.
            continue
        else:
            raise RefinerError("Recovery includes a nonterminal or unsupported controller call")
    records, matched, offset = [], [], 0
    for index, relative in enumerate(paths, 1):
        item = read_json(_safe_file(source, relative))
        plan = item.get("plan_snapshot")
        if (item.get("schema_version") != "skill-refiner.plan-controls/v1" or item.get("suite") != "development"
                or item.get("attempt") != index or item.get("status") not in {"validation_error", "failed", "invalid_coverage"}
                or not isinstance(plan, dict) or item.get("plan_sha256") != digest(plan)):
            raise RefinerError("Recovery plan control record is inconsistent")
        matches = [(j, name) for j, (name, value) in enumerate(plans) if j >= offset and value == plan]
        if len(matches) != 1:
            raise RefinerError("Recovery control is not uniquely bound to its plan response")
        j, name = matches[0]; offset = j + 1
        matched.append(name); records.append(item)
    if not records or (not pending_prelaunch and not allow_pending and plans[-1][0] != matched[-1]):
        raise RefinerError("Recovery latest plan has no corresponding failed control")
    final = read_json(_safe_file(source, "plan-controls/development.json"))
    if final != records[-1]:
        raise RefinerError("Recovery latest control summary differs from its attempt")
    if pending_prelaunch:
        if len(plans) < 2 or plans[-2][0] != matched[-1] or plans[-1][0] == matched[-1]:
            raise RefinerError("Interrupted draft is not the single subsequent complete plan")
        return records, matched[-1], plans[-1]
    if allow_pending:
        latest_matched_index = next(i for i, (name, _) in enumerate(plans) if name == matched[-1])
        if len(plans) - latest_matched_index > 2:
            raise RefinerError("Recovery has multiple unverified later plan drafts")
        return records, matched[-1], plans[-1] if plans[-1][0] != matched[-1] else None
    return records, matched[-1]


def _interrupted_draft_evidence(source, state, stages, previous, prior_data):
    """Recover a completed plan interrupted before its applicability call starts.

    The incomplete call is charged and retained, but is never represented as a
    completed review or a passed plan. Every file is also bound by the full
    independently captured source inventory in the surrounding chain.
    """
    count = state.get("agent_calls")
    if (prior_data.get("schema_version") != "skill-refiner.refine-plan-recovery/v2"
            or state.get("status") != "planning_error" or stages.get("status") != "planning_error"
            or not isinstance(count, int) or count <= previous["usage"]["agent_calls"]):
        raise RefinerError("Interrupted draft is not an eligible terminal recovery")
    history = state.get("phase_history", [])
    if (not history or history[-1].get("phase") != "plan_gate"
            or history[-1].get("status") != "not_passed" or history[-1].get("reason") != "upstream_error"
            or history[-1].get("error") != state.get("error")):
        raise RefinerError("Interrupted draft lacks a matching terminal phase")
    dirs = sorted(p for p in (source / "agent-calls").iterdir() if p.is_dir())
    if len(dirs) != count or dirs[-1].name != f"{count:03}-analysis-review":
        raise RefinerError("Interrupted applicability call ledger is inconsistent")
    base = "agent-calls/" + dirs[-1].name + "/"
    manifest = read_json(_safe_file(source, base + "request-manifest.json"))
    submitted = read_json(_safe_file(source, base + "request-manifest.json.tmp"))
    request = read_json(_safe_file(source, base + "request.json"))
    transmitted = _safe_file(source, base + "transmission.txt").read_bytes()
    serialized = json.dumps(request, ensure_ascii=False).encode("utf-8")
    expected_error = state.get("error", "")
    if ("WinError 5" not in expected_error or "request-manifest.json.tmp" not in expected_error
            or request.get("role") != "analysis-review" or manifest.get("role") != "analysis-review"
            or manifest.get("status") != "prepared" or submitted != {**manifest, "status": "submitted"}
            or manifest.get("request_sha256") != hashlib.sha256(serialized).hexdigest()
            or manifest.get("request_bytes") != len(serialized)
            or manifest.get("transmitted_sha256") != hashlib.sha256(transmitted).hexdigest()
            or manifest.get("transmitted_bytes") != len(transmitted)
            or any((dirs[-1] / name).exists() for name in
                   ("execution.json", "process.execution.json", "response.json", "response.txt"))):
        raise RefinerError("Interrupted applicability prelaunch evidence is inconsistent")
    paths = sorted(k for k in _chain_tree(source) if re.fullmatch(r"plan-controls/attempts/development-[0-9]{2}\.json", k))
    records, last_control_call, (draft_call, draft) = _chain_control_records(source, paths, count, pending_prelaunch=True)
    if request.get("input", {}).get("plan") != draft:
        raise RefinerError("Interrupted applicability request is not bound to the completed draft")
    planning_paths = {row.get("path") for row in stages["stages"][1].get("artifacts", [])}
    required = set(paths) | {"plan-controls/development.json", "agent-calls/" + draft_call + "/response.json"}
    if not required <= planning_paths:
        raise RefinerError("Interrupted draft or control is not authenticated by the planning stage")
    return records, last_control_call, draft_call, draft, paths



def _carry_forward_evidence(source, state, stages, previous, prior_data):
    """Reuse a proven plan only for a pre-call decision-read infrastructure error."""
    if (prior_data.get("schema_version") != "skill-refiner.refine-plan-recovery/v2"
            or state.get("status") != "planning_error" or stages.get("status") != "planning_error"
            or state.get("error") != "Independent response applicability decision is missing"
            or state.get("agent_calls") != previous.get("usage", {}).get("agent_calls")):
        raise RefinerError("Only an authenticated decision-read failure with no new calls can reuse prior controls")
    history = state.get("phase_history", [])
    if (not isinstance(history, list) or not history or history[-1].get("phase") != "plan_gate"
            or history[-1].get("status") != "not_passed" or history[-1].get("reason") != "upstream_error"
            or history[-1].get("error") != state["error"]):
        raise RefinerError("Prior planning error lacks a matching terminal phase record")
    if (source / "plan-controls").exists() or (source / "plan-controls").is_symlink():
        raise RefinerError("A decision-read failure with new control artifacts needs explicit reconciliation")
    calls = source / "agent-calls"
    dirs = sorted(p for p in calls.iterdir() if p.is_dir())
    if (len(dirs) != state["agent_calls"] or any(not re.fullmatch(r"[0-9]{3}-.+", p.name)
            or int(p.name.split("-", 1)[0]) != i for i, p in enumerate(dirs, 1))):
        raise RefinerError("Decision-read recovery call ledger changed")
    root = _io_path(_safe_root(source), force=True)
    evidence = _io_path(_safe_root(previous.get("source_evidence_root", "")), force=True)
    if not evidence.is_relative_to(root):
        raise RefinerError("Inherited applicability evidence is outside the preserved source")
    records = previous.get("control_records")
    hashes = previous.get("control_record_hashes")
    paths = prior_data.get("control_paths")
    if (not isinstance(records, list) or not isinstance(hashes, list) or not isinstance(paths, list)
            or not records or len(records) != len(hashes) or len(paths) != len(hashes)):
        raise RefinerError("Inherited controls are incomplete")
    for path, expected, record in zip(paths, hashes, records):
        preserved = _safe_file(evidence, path)
        if _sha(preserved) != expected or read_json(preserved) != record:
            raise RefinerError("Inherited plan control changed")
    return records, prior_data["latest_plan_call"], evidence, hashes


def _validate_inherited_prelaunch(source, previous):
    name = previous.get("pending_prelaunch_call") or f"{previous['usage']['agent_calls']:03}-analysis-review"
    if not re.fullmatch(r"[0-9]{3}-analysis-review", name):
        raise RefinerError("Invalid authenticated prelaunch call identity")
    live = _safe_root(source / "agent-calls" / name)
    expected_names = {"request.json", "request-manifest.json", "request-manifest.json.tmp", "transmission.txt"}
    if {p.name for p in live.iterdir()} != expected_names:
        raise RefinerError("Prelaunch call contains execution or unexpected evidence")
    source_files = previous.get("pending_source_files", {})
    for filename in expected_names:
        relative = f"agent-calls/{name}/{filename}"
        if not _HASH.fullmatch(source_files.get(relative, "")) or _sha(_safe_file(source, relative)) != source_files[relative]:
            raise RefinerError("Prelaunch live evidence differs from its authenticated source")
    return name


def _carry_interrupted_prelaunch(source, state, stages, previous, prior_data):
    """Carry an authenticated draft after an unchanged prelaunch-ledger stop."""
    if (prior_data.get("schema_version") != "skill-refiner.refine-plan-recovery/v2"
            or state.get("status") != "planning_error" or stages.get("status") != "planning_error"
            or state.get("agent_calls") != previous.get("usage", {}).get("agent_calls")
            or not isinstance(previous.get("pending_plan"), dict)):
        raise RefinerError("Prelaunch carry-forward is not an authenticated unchanged call ledger")
    name = _validate_inherited_prelaunch(source, previous)
    if name != f"{state['agent_calls']:03}-analysis-review":
        raise RefinerError("Prelaunch call does not match the charged call number")
    expected_error = ("Controller call has no reconciled terminal evidence: " +
                      str(Path(state.get("workspace", "")) / "agent-calls" / name) + ".")
    if not isinstance(state.get("error"), str) or not state["error"].startswith(expected_error):
        raise RefinerError("Prelaunch recovery has a different terminal error")
    history = state.get("phase_history", [])
    if (not history or history[-1].get("phase") != "plan_gate" or history[-1].get("status") != "not_passed"
            or history[-1].get("reason") != "upstream_error" or history[-1].get("error") != state["error"]):
        raise RefinerError("Prelaunch recovery lacks its matching terminal phase")
    root = _io_path(_safe_root(source), force=True)
    for key in ("source_evidence_root", "pending_source_evidence_root"):
        evidence = _io_path(_safe_root(previous[key]), force=True)
        if not evidence.is_relative_to(root):
            raise RefinerError("Prelaunch inherited evidence is outside the preserved source")
    paths = prior_data.get("control_paths")
    if not isinstance(paths, list) or len(paths) != len(previous.get("control_records", [])):
        raise RefinerError("Prelaunch inherited control references are incomplete")
    return name


def _later_plan_history(source, state, stages, previous):
    """Authenticate later completed calls and controls after the prelaunch call."""
    prelaunch = _validate_inherited_prelaunch(source, previous)
    paths = sorted(k for k in _chain_tree(source)
                   if re.fullmatch(r"plan-controls/attempts/development-[0-9]{2}\.json", k))
    records, latest_checked, pending = _chain_control_records(
        source, paths, state["agent_calls"], authenticated_prelaunch=prelaunch, allow_pending=True)
    planning_paths = {row.get("path") for row in stages["stages"][1].get("artifacts", [])}
    latest = pending[0] if pending else latest_checked
    required = set(paths) | {"plan-controls/development.json", "agent-calls/" + latest + "/response.json"}
    if not required <= planning_paths:
        raise RefinerError("Later plan history is not authenticated by the planning stage")
    if pending and (state.get("status") != "planning_error" or not isinstance(state.get("error"), str)):
        raise RefinerError("An unchecked later draft requires a terminal planning error")
    return records, paths, latest, pending, prelaunch


def _merge_replayed_controls(previous, later):
    """Keep authenticated history once when a resumed run replayed its controls."""
    if len(later) < len(previous):
        raise RefinerError("Recovery replay omitted authenticated prior controls")
    for old, replay in zip(previous, later):
        old_record, replay_record = old.get("record", {}), replay.get("record", {})
        if (old_record.get("plan_sha256") != replay_record.get("plan_sha256")
                or old_record.get("plan_snapshot") != replay_record.get("plan_snapshot")):
            raise RefinerError("Recovery replay changed or reordered authenticated prior plans")
    return list(previous) + list(later[len(previous):])


def _planned_dependency_record(data, source_contract, source_plan, source_control_sha):
    """Bind a v8 selection to an authenticated, failed, unapproved v7 plan."""
    if (not isinstance(source_plan, dict) or source_plan.get("schema_version") != "skill-refiner.plan/v7"
            or not _HASH.fullmatch(source_control_sha or "")
            or not _HASH.fullmatch(source_contract.get("refine_plan_recovery_sha256", ""))
            or data.get("usage", {}).get("target_evaluations") != 0):
        raise RefinerError("Planned dependency migration needs an authenticated unevaluated v7 plan")
    record = {
        "schema_version": "skill-refiner.planned-dependency-migration/v1",
        "migration_schema_sha256": _sha(_PLANNED_DEPENDENCY_SCHEMA),
        "source_recovery_sha256": source_contract["refine_plan_recovery_sha256"],
        "inputs_sha256": source_contract["inputs_sha256"],
        "source_plan_schema_version": "skill-refiner.plan/v7",
        "selected_plan_schema_version": "skill-refiner.plan/v8",
        "source_plan_sha256": digest(source_plan),
        "source_control_sha256": source_control_sha,
        "selected_plan_schema_sha256": _sha(_PLAN_V8_SCHEMA),
        "usage": copy.deepcopy(data["usage"]),
        "options": copy.deepcopy(source_contract["options"]),
        "limits": copy.deepcopy(source_contract["limits"]),
    }
    jsonschema.Draft202012Validator(read_json(_PLANNED_DEPENDENCY_SCHEMA)).validate(record)
    return record


def _forward_unevaluated_migration(source, state, stages, previous, prior_data):
    """Carry a just-migrated, unexecuted plan without inventing new controls."""
    if (state.get("status") != "plan_not_passed" or stages.get("status") != "plan_not_passed"
            or state.get("agent_calls") != previous.get("usage", {}).get("agent_calls")
            or state.get("elapsed_seconds") != previous.get("usage", {}).get("elapsed_seconds")
            or state.get("target_evaluations") != 0 or not previous.get("subsequent_controls")
            or not isinstance(previous.get("latest_control_plan"), dict)
            or (source / "plan-controls").exists()):
        raise RefinerError("Source is not an unexecuted, authenticated plan migration")
    _validate_inherited_prelaunch(source, previous)
    source_name = Path(prior_data["source_run"]).name
    parent_state = read_json(_safe_file(source, f"recovery-chain/source/{source_name}/state.json"))
    contract = read_json(_safe_file(source, "resume-contract.json"))
    if contract.get("refine_budget_amendment_sha256"):
        amendment = read_json(_safe_file(source, "refine-budget-amendment.json"))
        parent_state = copy.deepcopy(parent_state)
        parent_state["limits"]["max_cases"] = amendment["new_max_cases"]
        parent_state["limits"]["max_evaluations"] = amendment["new_max_evaluations"]
    if {k: v for k, v in state.items() if k != "workspace"} != {
            k: v for k, v in parent_state.items() if k != "workspace"}:
        raise RefinerError("Unexecuted migration state differs from its authenticated parent")
    return prior_data["latest_plan_call"]


def _validate_capacity_assessment(assessment, plan, control_sha256, new_cases, acceptance, new_evaluations):
    """Verify an explicit controller decision against the authenticated plan."""
    if not isinstance(assessment, dict):
        raise RefinerError("Capacity amendment requires an explicit structured assessment")
    try:
        jsonschema.Draft202012Validator(read_json(_CAPACITY_ASSESSMENT_SCHEMA)).validate(assessment)
    except jsonschema.ValidationError as exc:
        raise RefinerError("Capacity assessment does not match its schema") from exc
    gaps = plan.get("coverage_gaps", [])
    index = assessment["gap_index"]
    if (not isinstance(gaps, list) or index >= len(gaps)
            or assessment["source_plan_sha256"] != digest(plan)
            or assessment["source_control_sha256"] != control_sha256
            or assessment["gap_quote"] != gaps[index]
            or assessment["authorized_limits"] != {"max_cases": new_cases,
                                                    "acceptance_cases": acceptance,
                                                    "max_evaluations": new_evaluations}):
        raise RefinerError("Capacity assessment differs from the authenticated plan, control, gap or limits")
    return assessment["gap_quote"]


def _explicit_zero_evaluation_lineage(source):
    """Require explicit integer-zero accounting from every preserved ancestor."""
    root = _safe_root(source)
    prefix = Path()
    evidence = []

    def read_state(relative):
        path = _safe_file(root, relative.as_posix())
        state = read_json(path)
        value = state.get("target_evaluations") if isinstance(state, dict) else None
        if type(value) is not int or value != 0:
            raise RefinerError("Capacity amendment requires explicit integer-zero evaluations in every ancestor state")
        evidence.append({"path": relative.as_posix(), "sha256": _sha(path), "target_evaluations": 0})

    for _ in range(64):
        read_state(prefix / "state.json")
        data = read_json(_safe_file(root, (prefix / "refine-plan-recovery.json").as_posix()))
        for value in (data.get("usage", {}).get("target_evaluations"),
                      data.get("ancestor", {}).get("target_evaluations")):
            if type(value) is not int or value != 0:
                raise RefinerError("Capacity amendment requires explicit integer-zero ancestor accounting")
        if data.get("schema_version") == "skill-refiner.refine-plan-recovery/v1":
            read_state(prefix / "recovery-source-state.json")
            read_state(prefix / "recovery-ancestor-state.json")
            return evidence
        if data.get("schema_version") != "skill-refiner.refine-plan-recovery/v2":
            raise RefinerError("Capacity amendment has an unsupported ancestor chain")
        prefix = prefix / "recovery-chain" / "source" / Path(data["source_run"]).name
    raise RefinerError("Capacity amendment ancestor chain exceeds the supported depth")


def _budget_amendment(source, source_run, old_contract, state, previous, new_cases, new_evaluations,
                      capacity_assessment=None):
    """Build a bounded, source-bound capacity amendment for the unfrozen plan."""
    if (type(new_cases) is not int or type(new_evaluations) is not int
            or not 2 <= new_cases <= 24 or not 0 <= new_evaluations <= 200):
        raise RefinerError("Amended case/evaluation limits are outside the authorized range")
    options, limits = old_contract.get("options", {}), old_contract.get("limits", {})
    old_cases, old_evaluations = options.get("max_cases"), limits.get("max_evaluations")
    if (type(old_cases) is not int or type(old_evaluations) is not int
            or new_cases <= old_cases or new_evaluations < old_evaluations
            or limits.get("max_cases") != old_cases or options.get("acceptance_cases") != limits.get("acceptance_cases")
            or options.get("max_rounds") != 2 or limits.get("max_rounds") != 2
            or state.get("target_evaluations") != 0):
        raise RefinerError("Capacity amendment does not preserve the existing execution contract")
    config = read_json(_safe_file(source, "backend-config.json"))
    executors = config.get("executors") if isinstance(config, dict) else None
    if not isinstance(executors, list) or len(executors) != 1:
        raise RefinerError("Capacity amendment requires exactly one authenticated executor")
    zero_lineage = _explicit_zero_evaluation_lineage(source)
    originals = _safe_root(source / "original")
    for directory, _, names in os.walk(_io_path(originals, force=True), followlinks=False):
        if Path(directory).name == "evals" and any(name in {"eval.yaml", "evals.json"} for name in names):
            raise RefinerError("Existing native evaluations require separate budget reconciliation")
    entries = previous.get("subsequent_controls", [])
    plan = previous.get("latest_control_plan")
    if not entries or not isinstance(plan, dict) or entries[-1].get("record", {}).get("plan_snapshot") != plan:
        raise RefinerError("Capacity amendment lacks the latest authenticated failed plan")
    if entries[-1]["record"].get("status") not in {"failed", "invalid_coverage", "validation_error"}:
        raise RefinerError("Capacity amendment requires a failed plan control")
    acceptance = options.get("acceptance_cases")
    if type(acceptance) is not int:
        raise RefinerError("Acceptance case capacity is invalid")
    capacity_gap = _validate_capacity_assessment(capacity_assessment, plan, entries[-1]["sha256"],
                                                 new_cases, acceptance, new_evaluations)
    worst = (2 + 1) * (new_cases + acceptance)
    if new_evaluations - state["target_evaluations"] < worst:
        raise RefinerError("Amended evaluation capacity cannot cover the known two-repair worst case")
    record = {"schema_version": "skill-refiner.refine-budget-amendment/v1", "source_run": source_run,
              "source_contract_sha256": _sha(_safe_file(source, "resume-contract.json")),
              "source_plan_sha256": digest(plan), "source_control_sha256": entries[-1]["sha256"],
              "capacity_gap": capacity_gap, "capacity_assessment": copy.deepcopy(capacity_assessment),
              "old_max_cases": old_cases, "new_max_cases": new_cases,
              "old_max_evaluations": old_evaluations, "new_max_evaluations": new_evaluations,
              "acceptance_cases": acceptance, "executor_count": 1, "max_rounds": 2,
              "known_native_cases": 0, "known_worst_case_evaluations": worst,
              "zero_evaluation_lineage": zero_lineage,
              "usage": {"agent_calls": state["agent_calls"], "elapsed_seconds": state["elapsed_seconds"],
                        "target_evaluations": state["target_evaluations"]}}
    jsonschema.Draft202012Validator(read_json(_BUDGET_SCHEMA)).validate(record)
    return record


def _verify_recovery_chain(work, contract, data):
    root = _safe_root(work)
    schema = read_json(_CHAIN_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(data)
    if contract.get("refine_plan_recovery_sha256") != digest(data) or contract.get("refine_plan_recovery_schema_sha256") != _sha(_CHAIN_SCHEMA):
        raise RefinerError("Recovery chain or schema digest changed")
    source = _safe_root(root / "recovery-chain" / "source" / Path(data["source_run"]).name)
    archive = _safe_root(root / "recovery-chain" / "archive")
    if _chain_tree(source) != data["source_files"] or _chain_tree(archive, archive=True) != data["archive_files"]:
        raise RefinerError("Recovery source or archived controller inventory changed")
    if _controller_hash(archive) != data["source_controller_sha256"]:
        raise RefinerError("Recovery archived controller changed")
    snapshot = _safe_file(root, "recovery-chain/source-observed.json")
    if _sha(snapshot) != data["source_snapshot_sha256"]:
        raise RefinerError("Independent source snapshot changed")
    snapshot_data = read_json(snapshot)
    snapshot_rows = snapshot_data.get("files", [])
    if (snapshot_data.get("source") is None or Path(snapshot_data["source"]).resolve() != Path(data["source_run"]).resolve()
            or len(snapshot_rows) != len(data["source_files"])
            or {row.get("path"): row.get("sha256") for row in snapshot_rows} != data["source_files"]):
        raise RefinerError("Independent source snapshot does not match preserved source")
    old_contract = read_json(_safe_file(source, "resume-contract.json"))
    if old_contract.get("controller_sha256") != data["source_controller_sha256"]:
        raise RefinerError("Source controller identity differs")
    budget_fields = {"refine_budget_amendment_sha256", "refine_budget_amendment_schema_sha256",
                     "refine_capacity_assessment_schema_sha256"}
    new_amendment = bool(contract.get("refine_budget_amendment_sha256") and
                         not old_contract.get("refine_budget_amendment_sha256"))
    migration_fields = {"planned_dependency_migration_sha256", "planned_dependency_migration_schema_sha256"}
    new_plan_migration = bool(contract.get("planned_dependency_migration_sha256") and
                              not old_contract.get("planned_dependency_migration_sha256"))
    ignored = {"refine_plan_recovery_sha256", "refine_plan_recovery_schema_sha256", "controller_sha256"}
    if new_amendment:
        ignored |= budget_fields | {"options", "limits"}
    if new_plan_migration:
        ignored |= migration_fields
    comparable = {k: v for k, v in contract.items() if k not in ignored}
    prior = {k: v for k, v in old_contract.items() if k not in ignored}
    if comparable != prior:
        raise RefinerError("Recovery chain altered frozen execution contract")
    previous = _archived_verify(archive, source)
    prior_data = read_json(_safe_file(source, "refine-plan-recovery.json"))
    if prior_data.get("schema_version") == "skill-refiner.refine-plan-recovery/v1":
        expected_lineage = [prior_data["ancestor"]["run"], prior_data["source_run"], data["source_run"]]
    elif prior_data.get("schema_version") == "skill-refiner.refine-plan-recovery/v2":
        expected_lineage = [*prior_data["lineage"], data["source_run"]]
    else:
        raise RefinerError("Unsupported previous recovery chain")
    if (data["lineage"] != expected_lineage or data["ancestor"] != prior_data["ancestor"]
            or len({os.path.normcase(os.path.abspath(p)) for p in expected_lineage}) != len(expected_lineage)):
        raise RefinerError("Recovery provenance chain contains a changed or repeated ancestor")
    state = read_json(_safe_file(source, "state.json")); stages = read_json(_safe_file(source, "stages.json"))
    if (state.get("status") not in {"plan_not_passed", "planning_error"} or stages.get("status") != state.get("status")
            or stages.get("active_stage") is not None or state.get("active_agent_calls")
            or stages.get("mode") != "refine" or stages.get("run_id") != source.name):
        raise RefinerError("Recovery chain source is not terminal")
    _reject_evaluation_evidence(source, state, stages)
    if contract.get("refine_budget_amendment_sha256"):
        record_path = _safe_file(root, "refine-budget-amendment.json")
        if (_sha(record_path) != contract["refine_budget_amendment_sha256"]
                or _sha(_BUDGET_SCHEMA) != contract.get("refine_budget_amendment_schema_sha256")
                or _sha(_CAPACITY_ASSESSMENT_SCHEMA) != contract.get("refine_capacity_assessment_schema_sha256")):
            raise RefinerError("Capacity amendment or packaged schema changed")
        record = read_json(record_path)
        jsonschema.Draft202012Validator(read_json(_BUDGET_SCHEMA)).validate(record)
        if new_amendment:
            expected = _budget_amendment(source, data["source_run"], old_contract, state, previous,
                                         record["new_max_cases"], record["new_max_evaluations"],
                                         record.get("capacity_assessment"))
            if record != expected:
                raise RefinerError("Capacity amendment differs from authenticated source evidence")
            expected_options, expected_limits = copy.deepcopy(old_contract["options"]), copy.deepcopy(old_contract["limits"])
            expected_options["max_cases"] = expected_limits["max_cases"] = record["new_max_cases"]
            expected_limits["max_evaluations"] = record["new_max_evaluations"]
            if contract.get("options") != expected_options or contract.get("limits") != expected_limits:
                raise RefinerError("Capacity amendment changed unrelated frozen limits")
        elif (contract.get("refine_budget_amendment_sha256") != old_contract.get("refine_budget_amendment_sha256")
              or _sha(_safe_file(source, "refine-budget-amendment.json")) != contract["refine_budget_amendment_sha256"]):
            raise RefinerError("Inherited capacity amendment changed")
    rows = stages.get("stages", [])
    if (not isinstance(rows, list) or [r.get("id") for r in rows] != ["inputs", "planning", "baseline", "repair", "regression", "acceptance", "delivery", "verification"]
            or rows[0].get("status") != "completed" or rows[1].get("status") != "blocked"
            or any(r.get("status") == "completed" for r in rows[1:])):
        raise RefinerError("Recovery source stage ledger differs from unevaluated planning")
    if data["usage"] != {"agent_calls": state.get("agent_calls"), "elapsed_seconds": state.get("elapsed_seconds"), "target_evaluations": 0}:
        raise RefinerError("Recovery chain usage differs from source")
    prior_usage = previous["usage"]
    if (data["usage"]["agent_calls"] < prior_usage.get("agent_calls", 0)
            or data["usage"]["elapsed_seconds"] < prior_usage.get("elapsed_seconds", 0)):
        raise RefinerError("Recovery chain usage went backwards")
    source_inputs = read_json(_safe_file(source, "inputs.json"))
    if digest(source_inputs) != old_contract.get("inputs_sha256"):
        raise RefinerError("Recovery chain inputs differ from contract")
    for reference in stages.get("schema_refs", []):
        if _sha(_safe_file(archive, reference.get("path"))) != reference.get("sha256"):
            raise RefinerError("Recovery archived stage schema differs")
    for stage in stages.get("stages", []):
        _chain_stage_inventory(stage, source)
    pending = None
    subsequent = []
    latest_observed_plan = None
    if (state.get("agent_calls") > previous["usage"]["agent_calls"]
            and (previous.get("pending_prelaunch_call") or isinstance(previous.get("pending_plan"), dict))):
        later_records, later_paths, latest, draft, prelaunch = _later_plan_history(source, state, stages, previous)
        if data["control_paths"] != later_paths:
            raise RefinerError("Later control references differ from authenticated history")
        records = previous["control_records"]
        record_hashes = previous["control_record_hashes"]
        evidence_root = _io_path(_safe_root(previous["source_evidence_root"]), force=True)
        replayed = [
            {"record": row, "sha256": _sha(_safe_file(source, path)),
             "source_evidence_root": str(_io_path(source, force=True)),
             "source_files": copy.deepcopy(data["source_files"])}
            for path, row in zip(later_paths, later_records)]
        subsequent = _merge_replayed_controls(previous.get("subsequent_controls", []), replayed)
        latest_observed_plan = draft[1] if draft else later_records[-1]["plan_snapshot"]
        pending = {"pending_prelaunch_call": prelaunch,
                   "pending_source_evidence_root": str(_io_path(source, force=True)),
                   "pending_source_files": copy.deepcopy(data["source_files"])}
        if draft:
            pending.update(pending_plan=draft[1], pending_plan_call=draft[0],
                           pending_plan_request=read_json(_safe_file(source, "agent-calls/" + draft[0] + "/request.json")))
    elif state.get("status") == "planning_error" and state.get("agent_calls") > previous["usage"]["agent_calls"]:
        new_records, _, latest, draft, new_paths = _interrupted_draft_evidence(source, state, stages, previous, prior_data)
        if data["control_paths"] != prior_data.get("control_paths"):
            raise RefinerError("Inherited control references differ from previous recovery")
        records = previous["control_records"]
        record_hashes = previous["control_record_hashes"]
        evidence_root = _io_path(_safe_root(previous["source_evidence_root"]), force=True)
        pending = {"pending_plan": draft, "pending_plan_call": latest,
                   "pending_plan_request": read_json(_safe_file(source, "agent-calls/" + latest + "/request.json")),
                   "pending_source_evidence_root": str(_io_path(source, force=True)),
                   "pending_source_files": copy.deepcopy(data["source_files"]),
                   "interrupted_control_record": new_records[-1],
                   "interrupted_control_sha256": _sha(_safe_file(source, new_paths[-1])),
                   "pending_prelaunch_call": f"{state['agent_calls']:03}-analysis-review"}
    elif state.get("status") == "planning_error" and isinstance(previous.get("pending_plan"), dict):
        call = _carry_interrupted_prelaunch(source, state, stages, previous, prior_data)
        if data["control_paths"] != prior_data.get("control_paths"):
            raise RefinerError("Inherited control references differ from previous recovery")
        records, latest = previous["control_records"], previous["pending_plan_call"]
        record_hashes = previous["control_record_hashes"]
        evidence_root = _io_path(_safe_root(previous["source_evidence_root"]), force=True)
        pending = {key: previous[key] for key in ("pending_plan", "pending_plan_call", "pending_plan_request",
                   "pending_source_evidence_root", "pending_source_files", "interrupted_control_record",
                   "interrupted_control_sha256")}
        pending["pending_prelaunch_call"] = call
    elif (state.get("status") == "plan_not_passed" and state.get("agent_calls") == previous["usage"]["agent_calls"]
          and previous.get("subsequent_controls")):
        latest = _forward_unevaluated_migration(source, state, stages, previous, prior_data)
        if data["control_paths"] != prior_data.get("control_paths"):
            raise RefinerError("Forwarded control references differ from previous recovery")
        records, record_hashes = previous["control_records"], previous["control_record_hashes"]
        evidence_root = _io_path(_safe_root(previous["source_evidence_root"]), force=True)
        subsequent = copy.deepcopy(previous["subsequent_controls"])
        latest_observed_plan = previous["latest_control_plan"]
        pending = {key: previous[key] for key in ("pending_prelaunch_call", "pending_source_evidence_root", "pending_source_files")}
    elif state.get("status") == "planning_error":
        records, latest, evidence_root, record_hashes = _carry_forward_evidence(source, state, stages, previous, prior_data)
        if data["control_paths"] != prior_data.get("control_paths"):
            raise RefinerError("Inherited control references differ from previous recovery")
    else:
        records, latest = _chain_control_records(source, data["control_paths"], data["usage"]["agent_calls"])
        evidence_root = _io_path(source, force=True)
        record_hashes = [data["source_files"][p] for p in data["control_paths"]]
    if latest != data["latest_plan_call"]:
        raise RefinerError("Recovery latest plan call differs")
    planning_paths = {row.get("path") for row in rows[1].get("artifacts", [])}
    required_paths = set(data["control_paths"]) | {"plan-controls/development.json", "agent-calls/" + latest + "/response.json"}
    if state.get("status") != "planning_error" and not (source / "plan-controls").exists():
        pass  # a verified unexecuted migration inherited its prior controls
    elif state.get("status") != "planning_error" and not required_paths.issubset(planning_paths):
        raise RefinerError("Recovery planning stage does not authenticate latest plan and controls")
    for name, sha in data["source_files"].items():
        if (name == "inputs.json" or name.startswith(("original/", "agent-calls/", "profiles/")) or name == "backend-config.json"):
            if _sha(_safe_file(root, name)) != sha:
                raise RefinerError("Recovery live input or prior call changed")
    latest_plan = records[-1]["plan_snapshot"]
    for kind in ("requirements", "cases"):
        def ids(plan):
            rows = plan.get(kind, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] for row in rows):
                raise RefinerError("Recovery matrix has invalid identifiers")
            result = [row["id"].casefold() for row in rows]
            if len(set(result)) != len(result):
                raise RefinerError("Recovery matrix identifiers are ambiguous")
            return set(result)
        prior_ids = ids(previous["previous_plan"])
        for item in records:
            new_ids = ids(item["plan_snapshot"])
            if not prior_ids.issubset(new_ids):
                raise RefinerError("Recovery matrix dropped historical " + kind)
            prior_ids = new_ids
    gaps = (latest_observed_plan or latest_plan).get("coverage_gaps", [])
    if not isinstance(gaps, list):
        raise RefinerError("Recovery plan coverage gaps are malformed")
    diagnostics = []
    sources = []
    for item in records:
        error = item.get("validation_error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise RefinerError("Recovery control is missing detailed validation feedback")
        diagnostics.append(error["message"])
        sources.append({"kind": "validation_error", "attempt": item["attempt"], "case_ids": error.get("case_ids", [])})
    if gaps:
        diagnostics.append("Unresolved coverage gaps from the authenticated plan: " + json.dumps(gaps, ensure_ascii=False))
    plan_migration = None
    if contract.get("planned_dependency_migration_sha256"):
        marker_path = _safe_file(root, "planned-dependency-migration.json")
        if (_sha(marker_path) != contract["planned_dependency_migration_sha256"]
                or _sha(_PLANNED_DEPENDENCY_SCHEMA) != contract.get("planned_dependency_migration_schema_sha256")):
            raise RefinerError("Planned dependency migration evidence or schema changed")
        plan_migration = read_json(marker_path)
        jsonschema.Draft202012Validator(read_json(_PLANNED_DEPENDENCY_SCHEMA)).validate(plan_migration)
        if plan_migration["selected_plan_schema_sha256"] != _sha(_PLAN_V8_SCHEMA):
            raise RefinerError("Selected v8 plan schema changed")
        if new_plan_migration:
            if (state.get("status") != "plan_not_passed" or not subsequent
                    or (pending or {}).get("pending_plan") or latest_observed_plan is None):
                raise RefinerError("Planned dependency migration requires a completed failed control")
            expected = _planned_dependency_record(data, old_contract, latest_observed_plan, subsequent[-1]["sha256"])
            if plan_migration != expected:
                raise RefinerError("Planned dependency migration differs from authenticated evidence")
        elif (previous.get("planned_dependency_migration") != plan_migration
              or _sha(_safe_file(source, "planned-dependency-migration.json")) != contract["planned_dependency_migration_sha256"]):
            raise RefinerError("Inherited planned dependency migration changed")
    return {"previous_plan": latest_plan,
            "previous_invalid_response": json.dumps(latest_plan, ensure_ascii=False),
            "contract_errors": [], "validation_feedback": diagnostics, "feedback_sources": sources,
            "control_records": records, "control_record_hashes": record_hashes,
            "coverage_gaps": gaps, "source_evidence_root": str(evidence_root),
            "source_files": copy.deepcopy(previous.get("source_files", {}) if state.get("status") == "planning_error" else data["source_files"]),
            "usage": data["usage"], **({"subsequent_controls": subsequent} if subsequent else {}),
            **({"latest_control_plan": latest_observed_plan} if latest_observed_plan is not None and not (pending or {}).get("pending_plan") else {}),
            **(pending or {}),
            **({"planned_dependency_migration": plan_migration} if plan_migration else {})}



@contextmanager
def _chain_temporary_directory(parent):
    """Clean long Windows paths without replacing the original migration error."""
    path = Path(tempfile.mkdtemp(prefix=".refine-chain-", dir=parent))
    try:
        yield path
    except BaseException as primary:
        try:
            shutil.rmtree(_io_path(path, force=True))
        except OSError as cleanup:
            primary.add_note("Recovery temporary cleanup failed: " + type(cleanup).__name__)
        raise
    else:
        shutil.rmtree(_io_path(path, force=True))

def _migrate_recovery_chain(source, archive, ancestor, target, snapshot_path, *,
                            amend_max_cases=None, amend_max_evaluations=None, capacity_assessment=None,
                            planned_dependency_migration=False):
    old_contract = read_json(_safe_file(source, "resume-contract.json"))
    state = read_json(_safe_file(source, "state.json")); stages = read_json(_safe_file(source, "stages.json"))
    if old_contract.get("controller_sha256") != _controller_hash(archive):
        raise RefinerError("Archived controller does not match source run")
    files = _chain_tree(source)
    snapshot_sha = _chain_snapshot(snapshot_path, source, files)
    archive_files = _chain_tree(archive, archive=True)
    prior = _archived_verify(archive, source)
    prior_data = read_json(_safe_file(source, "refine-plan-recovery.json"))
    amendment = None
    if amend_max_cases is not None or amend_max_evaluations is not None or capacity_assessment is not None:
        if (amend_max_cases is None or amend_max_evaluations is None or capacity_assessment is None
                or old_contract.get("refine_budget_amendment_sha256")):
            raise RefinerError("Capacity amendment needs both limits and an explicit assessment; it cannot overwrite an earlier amendment")
        _forward_unevaluated_migration(source, state, stages, prior, prior_data)
        _reject_evaluation_evidence(source, state, stages)
        assessment = read_json(_safe_root(capacity_assessment))
        amendment = _budget_amendment(source, str(source), old_contract, state, prior,
                                      amend_max_cases, amend_max_evaluations, assessment)
    if prior_data.get("schema_version") == "skill-refiner.refine-plan-recovery/v1":
        if Path(prior_data["ancestor"]["run"]).resolve() != ancestor:
            raise RefinerError("Supplied ancestor differs from authenticated prior migration")
        lineage = [prior_data["ancestor"]["run"], prior_data["source_run"]]
        ancestor_data = prior_data["ancestor"]
    elif prior_data.get("schema_version") == "skill-refiner.refine-plan-recovery/v2":
        lineage = list(prior_data["lineage"])
        ancestor_data = prior_data["ancestor"]
        if Path(ancestor_data["run"]).resolve() != ancestor:
            raise RefinerError("Supplied ancestor differs from authenticated recovery chain")
    else:
        raise RefinerError("Unsupported previous recovery chain")
    lineage.append(str(source))
    if len({os.path.normcase(os.path.abspath(p)) for p in lineage}) != len(lineage):
        raise RefinerError("Recovery chain contains a repeated source")
    if not isinstance(state.get("agent_calls"), int) or state["agent_calls"] < prior["usage"]["agent_calls"]:
        raise RefinerError("Recovery source call usage regressed")
    if not isinstance(state.get("elapsed_seconds"), (int, float)) or state["elapsed_seconds"] < prior["usage"]["elapsed_seconds"]:
        raise RefinerError("Recovery source elapsed usage regressed")
    if (state["agent_calls"] > old_contract["limits"]["max_agent_calls"]
            or state["elapsed_seconds"] > old_contract["limits"]["max_seconds"]):
        raise RefinerError("Recovery source exceeds frozen budget")
    if (state.get("agent_calls") > prior["usage"]["agent_calls"]
            and (prior.get("pending_prelaunch_call") or isinstance(prior.get("pending_plan"), dict))):
        records, control_paths, latest, _, _ = _later_plan_history(source, state, stages, prior)
    elif state.get("status") == "planning_error" and state.get("agent_calls") > prior["usage"]["agent_calls"]:
        records, _, latest, _, _ = _interrupted_draft_evidence(source, state, stages, prior, prior_data)
        control_paths = prior_data["control_paths"]
    elif state.get("status") == "planning_error" and isinstance(prior.get("pending_plan"), dict):
        _carry_interrupted_prelaunch(source, state, stages, prior, prior_data)
        records, latest = prior["control_records"], prior["pending_plan_call"]
        control_paths = prior_data["control_paths"]
    elif (state.get("status") == "plan_not_passed" and state.get("agent_calls") == prior["usage"]["agent_calls"]
          and prior.get("subsequent_controls")):
        latest = _forward_unevaluated_migration(source, state, stages, prior, prior_data)
        records, control_paths = prior["control_records"], prior_data["control_paths"]
    elif state.get("status") == "planning_error":
        records, latest, _, _ = _carry_forward_evidence(source, state, stages, prior, prior_data)
        control_paths = prior_data["control_paths"]
    else:
        control_paths = sorted(k for k in files if re.fullmatch(r"plan-controls/attempts/development-[0-9]{2}\.json", k))
        records, latest = _chain_control_records(source, control_paths, state["agent_calls"])
    if planned_dependency_migration:
        if (old_contract.get("planned_dependency_migration_sha256") or state.get("status") != "plan_not_passed"
                or not control_paths or not records):
            raise RefinerError("Planned dependency migration requires an unapproved completed failed plan")
    data = {"schema_version":"skill-refiner.refine-plan-recovery/v2", "source_run":str(source),
            "source_controller_sha256":old_contract["controller_sha256"], "source_snapshot_sha256":snapshot_sha,
            "source_files":files, "archive_files":archive_files, "lineage":lineage,
            "usage":{"agent_calls":state["agent_calls"],"elapsed_seconds":state["elapsed_seconds"],"target_evaluations":0},
            "ancestor":ancestor_data, "control_paths":control_paths, "latest_plan_call":latest}
    jsonschema.Draft202012Validator(read_json(_CHAIN_SCHEMA)).validate(data)
    current = _controller_hash(Path(__file__).resolve().parents[2])
    if current == old_contract["controller_sha256"]:
        raise RefinerError("Recovery migration requires a different controller version")
    with _chain_temporary_directory(target.parent) as tmp:
        temp = _io_path(tmp / target.name, force=True); temp.mkdir()
        preserved = temp / "recovery-chain/source" / source.name
        preserved_archive = temp / "recovery-chain/archive"
        for base, inventory, dest in ((source, files, preserved), (archive, archive_files, preserved_archive)):
            for relative in inventory:
                dst=_io_path(dest / relative, force=True); dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_safe_file(base, relative), dst)
        shutil.copyfile(_safe_root(snapshot_path), temp / "recovery-chain/source-observed.json")
        for relative in files:
            if relative in {"inputs.json", "backend-config.json", "refine-budget-amendment.json", "planned-dependency-migration.json"} or relative.startswith(("original/", "agent-calls/", "profiles/")):
                dest=temp / relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(_safe_file(source,relative),dest)
        state_copy=copy.deepcopy(state);state_copy["workspace"]=str(target)
        stage_copy=copy.deepcopy(stages);stage_copy["run_id"]=target.name
        for row in stage_copy.get("stages", []):
            if row.get("id") == "verification" and row.get("status") == "skipped":
                row["artifacts"] = []; row["input_artifacts"] = []
            if row.get("id") == "planning" and row.get("status") == "blocked":
                row["artifacts"] = []
        from .artifact_validation import all_schema_refs, schema_ref_compatible
        from .stages import StageManifest
        new_refs=all_schema_refs();by_id={r["id"]:r for r in new_refs}
        for ref in stages["schema_refs"]:
            if ref.get("id") not in by_id or not schema_ref_compatible(ref,by_id[ref["id"]]):
                raise RefinerError("Current controller changed a historical stage schema")
        stage_copy["schema_refs"]=new_refs;StageManifest._validate_manifest_schema(stage_copy)
        contract=copy.deepcopy(old_contract);contract["controller_sha256"]=current
        contract["refine_plan_recovery_sha256"]=digest(data);contract["refine_plan_recovery_schema_sha256"]=_sha(_CHAIN_SCHEMA)
        if amendment:
            write_json(temp / "refine-budget-amendment.json", amendment)
            contract["refine_budget_amendment_sha256"] = _sha(temp / "refine-budget-amendment.json")
            contract["refine_budget_amendment_schema_sha256"] = _sha(_BUDGET_SCHEMA)
            contract["refine_capacity_assessment_schema_sha256"] = _sha(_CAPACITY_ASSESSMENT_SCHEMA)
            contract["options"]["max_cases"] = contract["limits"]["max_cases"] = amendment["new_max_cases"]
            contract["limits"]["max_evaluations"] = amendment["new_max_evaluations"]
            state_copy["limits"]["max_cases"] = amendment["new_max_cases"]
            state_copy["limits"]["max_evaluations"] = amendment["new_max_evaluations"]
        if planned_dependency_migration:
            marker = _planned_dependency_record(data, old_contract, records[-1]["plan_snapshot"],
                                                 _sha(_safe_file(source, control_paths[-1])))
            write_json(temp / "planned-dependency-migration.json", marker)
            contract["planned_dependency_migration_sha256"] = _sha(temp / "planned-dependency-migration.json")
            contract["planned_dependency_migration_schema_sha256"] = _sha(_PLANNED_DEPENDENCY_SCHEMA)
        write_json(temp/"state.json",state_copy);write_json(temp/"stages.json",stage_copy)
        write_json(temp/"resume-contract.json",contract);write_json(temp/"refine-plan-recovery.json",data)
        _verify_recovery_chain(temp,contract,data)
        if _chain_tree(source)!=files or _chain_tree(archive,archive=True)!=archive_files or _sha(_safe_root(snapshot_path))!=snapshot_sha:
            raise RefinerError("Recovery chain source changed during migration")
        temp.rename(target)
    return {"status":"migrated","workspace":str(target),"reused_stages":["inputs"],"plan_status":"not_passed",
            "inherited":data["usage"],"ancestor":ancestor_data,"control_attempts":len(records)}

def main():
    parser = argparse.ArgumentParser()
    for name in ("source-run", "controller-archive", "ancestor-run", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--source-snapshot")
    parser.add_argument("--amend-max-cases", type=int)
    parser.add_argument("--amend-max-evaluations", type=int)
    parser.add_argument("--capacity-assessment")
    parser.add_argument("--planned-dependency-migration", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source_run, args.controller_archive, args.ancestor_run, args.output,
                         source_snapshot=args.source_snapshot, amend_max_cases=args.amend_max_cases,
                         amend_max_evaluations=args.amend_max_evaluations,
                         capacity_assessment=args.capacity_assessment,
                         planned_dependency_migration=args.planned_dependency_migration)
    except (OSError, ValueError, RefinerError, jsonschema.ValidationError) as exc:
        print("skill-refiner: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

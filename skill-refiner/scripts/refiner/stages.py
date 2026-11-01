"""Versioned, machine-readable stage contracts and artifact manifests."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import fnmatch
import json
import mimetypes
from pathlib import Path

from .common import read_json, write_json
from .artifact_validation import (all_schema_refs, content_schema_id, schema_ref,
                                 validate_artifact, validate_contract_declaration,
                                 validate_stage_manifest, schema_ref_compatible,
                                 schema_refs_compatible)


def _a(path, media_type=None, glob=False, required=True):
    # Analysis handoffs can be legitimately reused from a historical version;
    # the inventory binds and validates the exact schema selected from content.
    contract_id = ("skill-refiner.artifact-record" if glob or "*" in path or path in {
                       "analysis.json", "analysis-review.json", "test-plan.json", "acceptance-plan.json"}
                   else content_schema_id(Path(path), media_type))
    return {"path": path, "media_type": media_type, "glob": glob, "required": required,
            "contract_id": contract_id,
            "content_policy": "by_media_type" if contract_id == "skill-refiner.artifact-record" else "fixed"}


def _calls(role):
    # Role-call evidence is indexed and hash-checked when present. It is optional
    # in a new run when the plan or draft is explicitly reused from an earlier run.
    return [_a(f"agent-calls/*-{role}/{name}", media_type, True, required=False)
            for name, media_type in (("request.json", "application/json"),
                                     ("request-manifest.json", "application/json"),
                                     ("contract-errors.json", "application/json"),
                                     ("transmission.txt", "text/plain"),
                                     ("execution.json", "application/json"),
                                     ("process.execution.json", "application/json"),
                                     ("process.log", "text/plain"),
                                     ("process.stdout.log", "text/plain"),
                                     ("process.stderr.log", "text/plain"),
                                     ("response.json", "application/json"))]


def _artifact_declarations_compatible(stored, current):
    if not isinstance(stored, list) or len(stored) != len(current):
        return False
    for old, new in zip(stored, current):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if ({key: value for key, value in old.items() if key != "schema_ref"}
                != {key: value for key, value in new.items() if key != "schema_ref"}):
            return False
        if not schema_ref_compatible(old.get("schema_ref"), new.get("schema_ref")):
            return False
    return True


CONTRACTS = {
    "refine": [
        {"id": "inputs", "inputs": [], "outputs": [_a("inputs.json"), _a("original/**", glob=True)],
         "validation_gate": {"id": "immutable-input-snapshot", "implementation": "snapshot; manifest"},
         "failure_states": ["error"]},
        {"id": "planning", "inputs": [_a("inputs.json"), _a("original/**", glob=True)],
         "outputs": [_a("test-plan.json"), _a("acceptance-plan.json"), _a("oracle.lock.json"),
                      _a("plan-controls/development.json"), _a("plan-controls/acceptance.json"),
                      _a("plan-controls/attempts/*.json", glob=True),
                      _a("plan-reviews/development.json"), _a("plan-reviews/acceptance.json"),
                      _a("plan-reviews/attempts/*.json", glob=True),
                      *_calls("plan"), *_calls("acceptance-plan"), *_calls("plan-review"),
                    ],
          "validation_gate": {"id": "reviewed-frozen-plans", "implementation": "schema; all response controls; asset audit; independent review; oracle lock"},
         "failure_states": ["coverage_gap", "error"]},
        {"id": "baseline", "inputs": [_a("test-plan.json"), _a("original/**", glob=True)],
         "outputs": [_a("baseline/summary.json"), _a("baseline/standards.json"),
                     _a("baseline/evaluation-cost.json", required=False),
                     _a("baseline/resume-attempts/attempt-*/evaluation-cost.json", glob=True, required=False),
                     _a("baseline/resume-cache.json", required=False), *_calls("standards-review")],
         "validation_gate": {"id": "baseline-observed", "implementation": "evaluate; standards_review; reject execution ERROR"},
         "failure_states": ["execution_error", "error"]},
        {"id": "repair", "inputs": [_a("baseline/summary.json"), _a("test-plan.json"),
                                       _a("acceptance/attempts/*/evaluation/summary.json",
                                          glob=True, required=False)],
         "outputs": [_a("iterations/round-*/changes.json", glob=True), _a("candidates/**", glob=True),
                     *_calls("repair")],
         "validation_gate": {"id": "evidence-bound-patch", "implementation": "REPAIR schema; apply_changes"},
         "failure_states": ["no_progress", "error"]},
        {"id": "regression", "inputs": [_a("iterations/round-*/changes.json", glob=True), _a("test-plan.json")],
         "outputs": [_a("iterations/round-*/regression/summary.json", glob=True),
                     _a("iterations/round-*/regression/evaluation-cost.json", glob=True, required=False),
                     _a("iterations/round-*/regression/resume-attempts/attempt-*/evaluation-cost.json", glob=True, required=False),
                     _a("iterations/round-*/regression/resume-cache.json", glob=True, required=False),
                     _a("iterations/round-*/standards.json", glob=True), *_calls("standards-review")],
         "validation_gate": {"id": "same-suite-no-regressions", "implementation": "compare_candidate; qualifies"},
         "failure_states": ["no_progress", "not_converged", "error"]},
        {"id": "acceptance", "inputs": [_a("acceptance-plan.json"), _a("acceptance/target.json")],
         "outputs": [_a("acceptance/summary.json"), _a("acceptance/attempts/**", glob=True, required=False),
                     _a("acceptance/attempts/**/evaluation/resume-cache.json", glob=True, required=False)],
         "validation_gate": {"id": "fresh-independent-acceptance", "implementation": "evaluate; failures return to evidence-bound repair and regression"},
         "failure_states": ["failed_acceptance", "execution_error", "error"]},
        {"id": "delivery", "inputs": [_a("final/evidence.json"), _a("oracle.lock.json")],
         "outputs": [_a("final/changes.patch", "text/x-diff"), _a("final/**", glob=True)],
         "validation_gate": {"id": "evidence-bound-candidate", "implementation": "snapshot; digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("final/evidence.json")],
         "outputs": [_a("verification.json"), _a("report.md", "text/markdown"),
                     _a("runtime-preflight*.json", glob=True, required=False)],
         "validation_gate": {"id": "machine-completion-gate", "implementation": "verify_run"},
         "failure_states": ["verification_failed", "error"]},
    ],
    "create": [
        {"id": "inputs", "inputs": [], "outputs": [_a("inputs.json"), _a("inputs/**", "text/plain", True)],
         "validation_gate": {"id": "frozen-business-inputs", "implementation": "input hash; integrity"},
         "failure_states": ["error"]},
        {"id": "analysis", "inputs": [_a("inputs.json"), _a("inputs/**", "text/plain", True)],
         "outputs": [_a("analysis.json"), _a("analysis-review.json"), _a("business-analysis.md", "text/markdown"),
                     _a("business.lock.json"), _a("analysis-attempt-*.json", glob=True, required=False),
                     _a("analysis-review-attempt-*.json", glob=True, required=False),
                     _a("analysis-revision-delta-*.json", glob=True, required=False),
                     _a("technical-triage-*.json", glob=True, required=False),
                     _a("analysis-derived-technical-*.json", glob=True, required=False),
                     _a("technical-coverage.json", required=False),
                     _a("clarification-review-attempt-*.json", glob=True, required=False),
                     *_calls("analyze"), *_calls("analysis-review")],
         "validation_gate": {"id": "grounded-reviewed-business-contract", "implementation": "validate_analysis; validate_analysis_review"},
         "failure_states": ["needs_clarification", "error"]},
        {"id": "draft", "inputs": [_a("analysis.json"), _a("business.lock.json")],
         "outputs": [_a("draft-manifest.json"), _a("drafts/**", glob=True), *_calls("draft")],
         "validation_gate": {"id": "static-valid-first-draft", "implementation": "lint; manifest"},
         "failure_states": ["error"]},
        {"id": "refinement", "inputs": [_a("draft-manifest.json"), _a("drafts/**", glob=True)],
         "outputs": [_a("refinement/report.md", "text/markdown"), _a("refinement/final/evidence.json")],
         "validation_gate": {"id": "child-refinement-verified", "implementation": "refine; verify_run"},
         "failure_states": ["coverage_gap", "not_converged", "failed_acceptance", "error"]},
        {"id": "delivery", "inputs": [_a("refinement/final/evidence.json"), _a("business.lock.json")],
         "outputs": [_a("final/evidence.json"), _a("final/creation.patch", "text/x-diff"),
                     _a("final/changes.patch", "text/x-diff"),
                     _a("test-scenarios.md", "text/markdown")],
         "validation_gate": {"id": "created-candidate-binds-refinement", "implementation": "snapshot; refinement evidence digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("refinement/verification.json"), _a("final/evidence.json")],
         "outputs": [_a("verification.json"), _a("report.md", "text/markdown"),
                     _a("runtime-preflight*.json", glob=True, required=False)],
         "validation_gate": {"id": "machine-creation-completion-gate", "implementation": "verify_run"},
         "failure_states": ["verification_failed", "error"]},
    ],
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def collect_artifacts(root, declarations):
    root = Path(root).resolve()
    results, missing = [], []
    files = sorted((path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
                   key=lambda path: path.relative_to(root).as_posix())

    def matches(path_parts, pattern_parts):
        if not pattern_parts:
            return not path_parts
        if pattern_parts[0] == "**":
            return (matches(path_parts, pattern_parts[1:])
                    or bool(path_parts) and matches(path_parts[1:], pattern_parts))
        return (bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], pattern_parts[0])
                and matches(path_parts[1:], pattern_parts[1:]))

    for declaration in declarations:
        pattern = declaration["path"].replace("\\", "/")
        if declaration.get("glob"):
            pattern_parts = tuple(part for part in pattern.split("/") if part not in {"", "."})
            existing = [path for path in files
                        if matches(tuple(path.relative_to(root).as_posix().split("/")), pattern_parts)]
        else:
            path = root / pattern
            existing = [path] if path.is_file() and not path.is_symlink() else []
        if not existing and declaration.get("required", True):
            missing.append(pattern)
        for path in existing:
            data = path.read_bytes()
            media_type = declaration.get("media_type")
            if media_type is None:
                media_type = {".md": "text/markdown", ".py": "text/x-python",
                              ".sh": "text/x-shellscript", ".txt": "text/plain"}.get(path.suffix)
            if media_type is None:
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            schema_id = content_schema_id(path, media_type)
            reference = schema_ref(schema_id)
            results.append({"path": path.relative_to(root).as_posix(),
                            "media_type": media_type, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "schema_ref": reference,
                            "validation": {"status": "pending", "validator_id": reference["validator_id"]}})
    return results, missing


class StageManifest:
    """Track stage state and hash-addressed file handoffs for one run."""

    def __init__(self, root, mode, *, resume=False):
        self.root = Path(root).resolve()
        self.path = self.root / "stages.json"
        if resume:
            if not self.path.is_file():
                raise ValueError("Cannot resume without stages.json")
            self.data = read_json(self.path)
            expected = self._bound_contract(mode)
            expected_ids = [item["id"] for item in expected]
            expected_refs = self._validate_manifest_schema(self.data)
            if (self.data.get("schema_version") != "skill-refiner.stages/v2"
                    or self.data.get("mode") != mode
                    or self.data.get("run_id") != self.root.name
                    or [row.get("id") for row in self.data.get("stages", [])] != expected_ids):
                raise ValueError("Cannot resume: stage manifest does not match this run contract")
            for row, contract in zip(self.data["stages"], expected):
                for field in ("inputs", "outputs", "validation_gate", "failure_states"):
                    compatible = (
                        _artifact_declarations_compatible(row.get(field), contract[field])
                        if field in {"inputs", "outputs"} else row.get(field) == contract[field]
                    )
                    if not compatible:
                        raise ValueError(f"Cannot resume: stage {row.get('id')} {field} contract changed")
                    if field in {"inputs", "outputs"}:
                        # Canonicalize old flat schema paths so the next save writes vN paths.
                        row[field] = contract[field]
            self.data["schema_refs"] = expected_refs
            if self.data.get("active_stage") not in {None, *expected_ids}:
                raise ValueError("Cannot resume: stage manifest has an unknown active stage")
            for stage in self.data["stages"]:
                if stage.get("status") != "completed":
                    continue
                for field in ("artifacts", "input_artifacts"):
                    inventory = stage.get(field)
                    if not isinstance(inventory, list):
                        raise ValueError(f"Cannot resume: completed stage {stage['id']} has no {field} inventory")
                    for artifact in inventory:
                        relative = Path(artifact.get("path", ""))
                        path = (self.root / relative).resolve()
                        if (relative.is_absolute() or not path.is_relative_to(self.root)
                                or not path.is_file()):
                            raise ValueError(f"Cannot resume: completed stage {stage['id']} artifact is missing or unsafe")
                        declarations = stage["outputs" if field == "artifacts" else "inputs"]
                        self._validate_artifact(stage, field, path, artifact, declarations,
                                                require_recorded_pass=True)
            active = self.data.get("active_stage")
            if active:
                stage = next(row for row in self.data["stages"] if row["id"] == active)
                if stage.get("status") == "running":
                    self._capture_outputs(stage)
                    stage["status"] = "pending"
                    stage["completed_at"] = None
                    stage["interrupted"] = True
                    self.data["interrupted_stage"] = active
            for stage in self.data["stages"]:
                if stage.get("status") == "blocked":
                    stage["status"] = "pending"
                    stage["completed_at"] = None
            self.data["status"] = "running"
            self.data["active_stage"] = None
            self.data.setdefault("resume_count", 0)
            self.data["resume_count"] += 1
            self.data["last_resumed_at"] = _now()
            self._save()
            return
        self.data = {"schema_version": "skill-refiner.stages/v2", "run_id": self.root.name,
                     "mode": mode, "status": "running", "active_stage": None,
                     "schema_refs": all_schema_refs(),
                     "created_at": _now(), "updated_at": _now(),
                     "stages": [{**item, "status": "pending", "started_at": None,
                                 "completed_at": None, "artifacts": [], "input_artifacts": [],
                                 "missing_outputs": [], "missing_inputs": []}
                                for item in self._bound_contract(mode)]}
        self._save()

    @staticmethod
    def _bound_contract(mode=None):
        mode = mode or getattr(StageManifest, "_mode", None)
        if mode not in CONTRACTS:
            raise ValueError("Unknown stage contract mode")
        stages = json.loads(json.dumps(CONTRACTS[mode]))
        for stage in stages:
            for field in ("inputs", "outputs"):
                for declaration in stage[field]:
                    declaration["schema_ref"] = schema_ref(declaration["contract_id"])
                    validate_contract_declaration(declaration, subject=f"{stage['id']} {field} {declaration['path']}")
        return stages

    @staticmethod
    def _validate_manifest_schema(data):
        expected_refs = all_schema_refs()
        if not schema_refs_compatible(data.get("schema_refs"), expected_refs):
            raise ValueError("Stage schema registry or schema hash changed; this run cannot be resumed")
        manifest_ref = next((item for item in expected_refs if item["id"] == "skill-refiner.stages"), None)
        validate_stage_manifest(data, manifest_ref)
        return expected_refs

    def _contract(self, stage_id):
        return next(row for row in self.data["stages"] if row["id"] == stage_id)

    @staticmethod
    def _declaration_for(path, declarations):
        relative = Path(path).as_posix()
        for declaration in declarations:
            pattern = declaration["path"].replace("\\", "/")
            if declaration.get("glob"):
                pattern_parts = tuple(part for part in pattern.split("/") if part not in {"", "."})
                path_parts = tuple(relative.split("/"))

                def matches(path_parts, pattern_parts):
                    if not pattern_parts:
                        return not path_parts
                    if pattern_parts[0] == "**":
                        return (matches(path_parts, pattern_parts[1:])
                                or bool(path_parts) and matches(path_parts[1:], pattern_parts))
                    return (bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], pattern_parts[0])
                            and matches(path_parts[1:], pattern_parts[1:]))

                matched = matches(path_parts, pattern_parts)
            else:
                matched = relative == pattern
            if matched:
                return declaration
        raise ValueError(f"Artifact {relative} is not declared by this stage contract")

    def _validate_artifact(self, stage, field, path, artifact, declarations, *, require_recorded_pass=False):
        relative = artifact.get("path", "")
        declaration = self._declaration_for(relative, declarations)
        expected_id = declaration["contract_id"]
        if expected_id == "skill-refiner.artifact-record":
            expected_id = None
        if require_recorded_pass and artifact.get("validation", {}).get("status") != "passed":
            raise ValueError(f"Stage {stage['id']} {field} artifact {relative} has no passing validation record")
        validate_artifact(path, self.root, artifact, expected_contract_id=expected_id)

    def _validate_inventory(self, stage, field, *, require_recorded_pass=False):
        declarations = stage["outputs" if field == "artifacts" else "inputs"]
        for artifact in stage.get(field, []):
            relative = Path(artifact.get("path", ""))
            path = (self.root / relative).resolve()
            self._validate_artifact(stage, field, path, artifact, declarations,
                                    require_recorded_pass=require_recorded_pass)

    def _bind_inventory(self, stage, field):
        self._validate_inventory(stage, field)

    def _save(self):
        self.data["updated_at"] = _now()
        self._validate_manifest_schema(self.data)
        write_json(self.path, self.data)

    def is_completed(self, stage_id):
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        return stage.get("status") == "completed"

    def _artifacts(self, declarations):
        return collect_artifacts(self.root, declarations)

    def _capture_outputs(self, stage):
        stage["artifacts"], stage["missing_outputs"] = self._artifacts(stage["outputs"])

    def _capture_inputs(self, stage):
        stage["input_artifacts"], stage["missing_inputs"] = self._artifacts(stage["inputs"])

    @staticmethod
    def _assert_inputs(stage):
        if stage["missing_inputs"]:
            raise ValueError(f"Stage {stage['id']} is missing required inputs: "
                             + ", ".join(stage["missing_inputs"]))

    @staticmethod
    def _assert_outputs(stage):
        if stage["missing_outputs"]:
            raise ValueError(f"Stage {stage['id']} is missing required outputs: "
                             + ", ".join(stage["missing_outputs"]))

    def complete(self, stage_id):
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        if stage["status"] == "completed":
            # Reuse an immutable handoff without refreshing timestamps/hashes.
            # Explicit activate() is required for a genuine later execution pass.
            self._validate_inventory(stage, "artifacts", require_recorded_pass=True)
            self._validate_inventory(stage, "input_artifacts", require_recorded_pass=True)
            return
        self._capture_outputs(stage)
        self._assert_outputs(stage)
        self._bind_inventory(stage, "artifacts")
        self._capture_inputs(stage)
        self._assert_inputs(stage)
        self._bind_inventory(stage, "input_artifacts")
        stage["status"] = "completed"
        stage["completed_at"] = _now()
        if self.data.get("active_stage") == stage_id:
            self.data["active_stage"] = None
        self._save()

    def activate(self, stage_id):
        if self.data.get("active_stage") == stage_id:
            stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
            if stage["status"] == "running":
                self._capture_outputs(stage)
                self._bind_inventory(stage, "artifacts")
                self._save()
                return
        for row in self.data["stages"]:
            if row["id"] == self.data.get("active_stage") and row["status"] == "running":
                self._capture_outputs(row)
                self._assert_outputs(row)
                self._bind_inventory(row, "artifacts")
                self._assert_inputs(row)
                self._bind_inventory(row, "input_artifacts")
                row["status"] = "completed"
                row["completed_at"] = _now()
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        if stage["status"] == "completed":
            # Refinement can return from held-out acceptance to repair and regression.
            # Reopen the named stage so each later pass refreshes its artifact handoff.
            stage["status"] = "running"
            stage["completed_at"] = None
            self.data["active_stage"] = stage_id
            self._capture_inputs(stage)
            self._assert_inputs(stage)
            self._bind_inventory(stage, "input_artifacts")
        else:
            self._capture_inputs(stage)
            self._assert_inputs(stage)
            self._bind_inventory(stage, "input_artifacts")
            stage["status"] = "running"
            stage["started_at"] = stage["started_at"] or _now()
            self.data["active_stage"] = stage_id
        self._capture_outputs(stage)
        self._save()

    def finish(self, status):
        self.data["status"] = status
        active = self.data.get("active_stage")
        for stage in self.data["stages"]:
            if stage["id"] == active and stage["status"] == "running":
                successful = status in {"passed", "unchanged", "created"}
                self._capture_outputs(stage)
                if successful:
                    self._assert_outputs(stage)
                    self._bind_inventory(stage, "artifacts")
                    self._assert_inputs(stage)
                    self._bind_inventory(stage, "input_artifacts")
                stage["status"] = "completed" if successful else "blocked"
                stage["completed_at"] = _now()
            elif stage["status"] == "pending":
                stage["status"] = "skipped"
                self._capture_inputs(stage)
                self._capture_outputs(stage)
        self.data["active_stage"] = None
        self._save()


def verify_stage_manifest(root, *, mode, run_status, allow_active_verification=False):
    """Verify stage contract states and that recorded handoff artifacts are intact."""
    root = Path(root).resolve()
    manifest_path = root / "stages.json"
    if not manifest_path.is_file():
        raise ValueError("Completion evidence missing: stages.json")
    from .common import read_json
    data = read_json(manifest_path)
    StageManifest._validate_manifest_schema(data)
    if data.get("schema_version") != "skill-refiner.stages/v2" or data.get("mode") != mode:
        raise ValueError("Stage manifest schema or mode mismatch")
    rows = data.get("stages", [])
    by_id = {stage.get("id"): stage for stage in rows}
    expected_contract = StageManifest._bound_contract(mode)
    expected_ids = [stage["id"] for stage in expected_contract]
    if [stage.get("id") for stage in rows] != expected_ids:
        raise ValueError("Stage manifest does not match the versioned stage contract")
    for stage, contract in zip(rows, expected_contract):
        for field in ("inputs", "outputs", "validation_gate", "failure_states"):
            if stage.get(field) != contract[field]:
                raise ValueError(f"Stage {stage['id']} {field} contract changed")

    active = data.get("active_stage")
    pre_finish = allow_active_verification and active == "verification" and by_id["verification"].get("status") == "running"
    if pre_finish:
        if data.get("status") != "running":
            raise ValueError("Stage manifest has an invalid in-progress state")
    elif data.get("status") != run_status or active is not None:
        raise ValueError("Stage manifest run status is incomplete or inconsistent")

    required = ["inputs", "planning", "baseline", "acceptance", "delivery"] if mode == "refine" else [
        "inputs", "analysis", "draft", "refinement", "delivery"]
    if mode == "refine" and run_status != "unchanged":
        required.extend(["repair", "regression"])
    for stage_id in required:
        if by_id[stage_id].get("status") != "completed":
            raise ValueError(f"Required stage {stage_id} is not completed")
    verification_status = by_id["verification"].get("status")
    if verification_status != ("running" if pre_finish else "completed"):
        raise ValueError("Verification stage is incomplete")
    if mode == "refine" and run_status == "unchanged":
        allowed = {"completed", "skipped", "pending"} if pre_finish else {"completed", "skipped"}
        if any(by_id[s].get("status") not in allowed for s in ("repair", "regression")):
            raise ValueError("Unchanged run has an invalid repair or regression stage state")

    for stage_id in expected_ids:
        stage = by_id[stage_id]
        if stage.get("status") not in {"completed", "skipped", "blocked", "running"}:
            raise ValueError(f"Stage {stage_id} has an invalid status")
        if stage.get("status") == "completed":
            if stage.get("missing_outputs") or stage.get("missing_inputs"):
                raise ValueError(f"Completed stage {stage_id} recorded missing required artifacts")
            for field in ("artifacts", "input_artifacts"):
                recorded = stage.get(field)
                if not isinstance(recorded, list):
                    raise ValueError(f"Completed stage {stage_id} has no {field} inventory")
                for artifact in recorded:
                    relative = Path(artifact.get("path", ""))
                    path = (root / relative).resolve()
                    if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
                        raise ValueError(f"Stage {stage_id} artifact is missing or outside its run directory")
                    declarations = stage["outputs" if field == "artifacts" else "inputs"]
                    declaration = StageManifest._declaration_for(relative, declarations)
                    expected_id = declaration["contract_id"]
                    if expected_id == "skill-refiner.artifact-record":
                        expected_id = None
                    if artifact.get("validation", {}).get("status") != "passed":
                        raise ValueError(f"Stage {stage_id} {field} artifact has no passing schema validation: {relative}")
                    validate_artifact(path, root, artifact, expected_contract_id=expected_id)
            for field, declarations in (("artifacts", stage["outputs"]),
                                        ("input_artifacts", stage["inputs"])):
                paths = {item["path"] for item in stage[field]}
                for declaration in declarations:
                    if not declaration.get("required", True):
                        continue
                    pattern = declaration["path"]
                    if not any(fnmatch.fnmatch(path, pattern) for path in paths):
                        kind = "output" if field == "artifacts" else "input"
                        raise ValueError(f"Completed stage {stage_id} omitted required {kind} {pattern}")
        elif stage.get("status") == "running" and not (pre_finish and stage_id == "verification"):
            raise ValueError(f"Stage {stage_id} was left running")
    return data


REFINE_EVENTS = {"planning": "planning", "coverage_gap": "planning", "baseline": "baseline",
                 "repairing": "repair", "regression": "regression",
                 "acceptance": "acceptance", "passed": "delivery", "unchanged": "delivery"}
CREATE_EVENTS = {"analyzing": "analysis", "resuming": "analysis", "needs_clarification": "analysis",
                 "reusing_draft": "draft", "creating": "draft", "refining": "refinement",
                 "created": "delivery"}

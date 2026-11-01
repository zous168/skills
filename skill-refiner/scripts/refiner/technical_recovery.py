"""Preserve and validate an unfinished analysis/clarification chain."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .analysis_recovery import _call, _file_hash
from .common import RefinerError, digest, read_json


def clarification_history(root, inputs, *, after_call, source_manifest=None, before_call=None):
    from .creation import validate_analysis, validate_clarification_review, validate_clarification_revision
    root = Path(root).resolve()
    sources = ([{"id": "business_description", "content": inputs["business_description"]}]
               if inputs.get("business_description") else []) + [
        {"id": f"spec-{index+1}", "source": row["source"], "content": row["content"]}
        for index, row in enumerate(inputs.get("requirements", []))]
    recorded = None
    if source_manifest is not None:
        row = next((s for s in source_manifest.get("stages", []) if s.get("id") == "analysis"), None)
        if not row or row.get("status") not in {"blocked", "error", "pending"}:
            raise RefinerError("Source clarification is not an incomplete analysis stage")
        recorded = {item["path"]: item["sha256"] for item in row.get("artifacts", [])}
    calls = root / "agent-calls"
    directories = sorted((p for p in calls.iterdir() if p.is_dir() and p.name.split("-", 1)[0].isdigit()
                          and int(p.name.split("-", 1)[0]) > after_call
                          and (before_call is None or int(p.name.split("-", 1)[0]) <= before_call)),
                         key=lambda p: int(p.name.split("-", 1)[0]))
    latest = None
    pairs, retained, invalid = [], [], None
    for directory in directories:
        role = "analyze" if directory.name.endswith("-analyze") else "analysis-review" if directory.name.endswith("-analysis-review") else None
        if role is None:
            continue
        manifest_path = directory / "request-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RefinerError("Clarification call manifest is missing or unsafe")
        relative = manifest_path.relative_to(root).as_posix()
        if recorded is not None and recorded.get(relative) != _file_hash(manifest_path):
            raise RefinerError(f"Clarification call manifest changed: {relative}")
        manifest = read_json(manifest_path)
        if manifest.get("status") == "invalid_response" and role == "analysis-review":
            request_path, raw_path, execution_path = (directory / "request.json", directory / "raw-response.txt",
                                                      directory / "execution.json")
            if any(p.is_symlink() or not p.is_file() for p in (request_path, raw_path, execution_path)):
                raise RefinerError("Invalid clarification response evidence is missing")
            # Historical manifests bind raw-response by response_sha256 but do
            # not necessarily list that file in the stage artifact inventory.
            for path in (request_path, execution_path):
                rel = path.relative_to(root).as_posix()
                if recorded is not None and recorded.get(rel) != _file_hash(path):
                    raise RefinerError(f"Invalid clarification call evidence differs from the source stage inventory: {rel}")
            request = read_json(request_path)
            execution = read_json(execution_path)
            raw = raw_path.read_text(encoding="utf-8")
            if (request.get("role") != role or manifest.get("role") != role
                    or execution.get("role") != role or execution.get("status") != "invalid_response"
                    or execution.get("exit_code") != 0 or not execution.get("finished_at")
                    or manifest.get("request_sha256") != hashlib.sha256(json.dumps(request, ensure_ascii=False).encode("utf-8")).hexdigest()
                    or manifest.get("response_sha256") != hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    or not latest or request.get("input", {}).get("analysis") != latest[1]):
                raise RefinerError("Invalid clarification response is not bound to its analysis")
            process_path = directory / "process.execution.json"
            rel = process_path.relative_to(root).as_posix()
            if recorded is not None and rel in recorded and not process_path.is_file():
                raise RefinerError("Recorded clarification process evidence is missing")
            if process_path.exists() or process_path.is_symlink():
                if process_path.is_symlink() or not process_path.is_file():
                    raise RefinerError("Unsafe clarification process evidence")
                if recorded is not None and recorded.get(rel) != _file_hash(process_path):
                    raise RefinerError("Clarification process evidence differs from the source stage inventory")
                process = read_json(process_path)
                if process.get("status") != "completed" or process.get("exit_code") != 0 or not process.get("finished_at"):
                    raise RefinerError("Invalid clarification response process is not settled")
            invalid = raw
            retained.append(directory.name)
            continue
        if manifest.get("status") != "completed":
            raise RefinerError(f"Clarification call is not settled: {directory.name}")
        request, response = _call(root, directory, role, recorded)
        payload = request.get("input", {})
        retained.append(directory.name)
        if role == "analyze":
            if any(payload.get(k) != v for k, v in inputs.items()):
                raise RefinerError("Clarification analysis changed frozen inputs")
            validate_analysis(response, inputs.get("requested_name"), {row["id"] for row in sources},
                              standards=inputs.get("standards"))
            if pairs:
                validate_clarification_revision(pairs[-1]["analysis"], response, pairs[-1]["review"])
            latest = (directory.name, response)
        else:
            if not latest or payload.get("analysis") != latest[1] or payload.get("business_sources") != sources:
                raise RefinerError("Clarification review is not bound to its analysis and business sources")
            if invalid is not None and payload.get("previous_invalid_response") != invalid[:12000]:
                raise RefinerError("Clarification retry is not bound to the retained invalid response")
            validate_clarification_review(response, latest[1], sources)
            pairs.append({"analysis_call": latest[0], "review_call": directory.name,
                          "analysis": latest[1], "review": response})
            invalid = None
    if not pairs or not latest or pairs[-1]["analysis_call"] != latest[0] or invalid is not None:
        raise RefinerError("Latest clarification analysis has no validated review")
    return pairs, retained


def verify_technical_recovery(work, contract, inputs):
    from .artifact_validation import schema_ref, validate_value
    root = Path(work).resolve()
    record = read_json(root / "technical-recovery.json")
    validate_value(record, schema_ref("skill-refiner.technical-recovery"), subject="technical recovery")
    if digest(record) != contract.get("technical_recovery_sha256") or record["inputs_sha256"] != digest(inputs):
        raise RefinerError("Technical recovery provenance or inputs changed")
    history_root = root / "analysis-previous" / "technical-history"
    if history_root.is_symlink() or not history_root.resolve().is_relative_to(root):
        raise RefinerError("Unsafe technical history directory")
    actual = set()
    for path in history_root.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise RefinerError("Unsafe link in technical history")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(record["files"]):
        raise RefinerError("Technical recovery evidence inventory changed")
    for relative, expected in record["files"].items():
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root) or _file_hash(path) != expected:
            raise RefinerError(f"Technical recovery evidence changed: {relative}")
    pairs, calls = clarification_history(history_root, inputs, after_call=0)
    if (calls != record["calls"] or digest(pairs[-1]["analysis"]) != record["analysis_sha256"]
            or digest(pairs[-1]["review"]) != record["clarification_sha256"]):
        raise RefinerError("Technical recovery chain no longer matches its provenance")
    return pairs[-1]

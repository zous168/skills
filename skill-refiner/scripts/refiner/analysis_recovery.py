"""Validate rejected analysis/review call pairs without treating them as passed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import RefinerError, digest, read_json


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _call(root, directory, role, recorded=None):
    if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
        raise RefinerError("Unsafe analysis recovery call directory")
    paths = {name: directory / name for name in ("request.json", "request-manifest.json", "response.json", "execution.json")}
    for path in paths.values():
        if path.is_symlink() or not path.is_file():
            raise RefinerError(f"Missing analysis recovery evidence: {path}")
        relative = path.relative_to(root).as_posix()
        if recorded is not None and recorded.get(relative) != _file_hash(path):
            raise RefinerError(f"Analysis call evidence differs from the source stage inventory: {relative}")
    request, manifest, response, execution = (read_json(paths[name]) for name in
                                               ("request.json", "request-manifest.json", "response.json", "execution.json"))
    if (not all(isinstance(v, dict) for v in (request, manifest, response, execution))
            or request.get("role") != role or manifest.get("role") != role
            or manifest.get("status") != "completed" or execution.get("status") != "completed"
            or execution.get("role") != role or type(execution.get("exit_code")) is not int
            or execution["exit_code"] != 0 or not isinstance(execution.get("finished_at"), str)
            or not execution["finished_at"].strip()
            or manifest.get("request_sha256") != hashlib.sha256(json.dumps(request, ensure_ascii=False).encode("utf-8")).hexdigest()
            or manifest.get("response_sha256") != hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()):
        raise RefinerError(f"Analysis recovery request/response integrity failed: {directory.name}")
    process = directory / "process.execution.json"
    process_relative = process.relative_to(root).as_posix()
    if recorded is not None and process_relative in recorded and not process.is_file():
        raise RefinerError("Recorded analysis process evidence is missing")
    if process.exists() or process.is_symlink():
        if process.is_symlink() or not process.is_file():
            raise RefinerError("Unsafe analysis process evidence")
        relative = process.relative_to(root).as_posix()
        if recorded is not None and recorded.get(relative) != _file_hash(process):
            raise RefinerError(f"Analysis process evidence differs from the source stage inventory: {relative}")
        data = read_json(process)
        if (not isinstance(data, dict) or data.get("status") != "completed"
                or type(data.get("exit_code")) is not int or data["exit_code"] != 0
                or not isinstance(data.get("finished_at"), str) or not data["finished_at"].strip()):
            raise RefinerError(f"Analysis process is not known to have completed: {directory.name}")
    return request, response


def rejected_history(root, inputs, *, source_manifest=None, allow_pending_clarification=False,
                    technical_seed=None, unvalidated_calls=None, pending_analysis=None,
                    invalid_analysis=None):
    """Return only fully validated, independently rejected analysis pairs."""
    from .creation import (analysis_revision_delta, validate_analysis, validate_analysis_review,
                           validate_analysis_revision_review)
    root = Path(root).resolve()
    calls = root / "agent-calls"
    if not calls.is_dir():
        return []
    recorded = None
    if source_manifest is not None:
        rows = [row for row in source_manifest.get("stages", []) if row.get("id") == "analysis"]
        if len(rows) != 1 or rows[0].get("status") not in {"blocked", "error", "pending"}:
            raise RefinerError("Source analysis is not an incomplete or rejected stage")
        recorded = {item["path"]: item["sha256"] for item in rows[0].get("artifacts", [])}
    directories = sorted((p for p in calls.iterdir() if p.is_dir() and p.name.split("-", 1)[0].isdigit()),
                         key=lambda p: int(p.name.split("-", 1)[0]))
    latest_analysis, pairs = None, []
    unvalidated_analysis = None
    source_ids = ({"business_description"} if inputs.get("business_description") else set()) | {
        f"spec-{index + 1}" for index in range(len(inputs.get("requirements", [])))}
    for directory in directories:
        if not directory.name.endswith(("-analyze", "-analysis-review")):
            continue
        manifest_path = directory / "request-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RefinerError(f"Analysis call has no retained manifest: {directory.name}")
        manifest_relative = manifest_path.relative_to(root).as_posix()
        if recorded is not None and recorded.get(manifest_relative) != _file_hash(manifest_path):
            raise RefinerError(f"Analysis call manifest differs from the source stage inventory: {manifest_relative}")
        manifest = read_json(manifest_path)
        status = manifest.get("status") if isinstance(manifest, dict) else None
        if status in {"invalid_response", "execution_error", "cancelled", "rejected_context_budget"}:
            # Terminal failures are charged but are not complete analysis/review pairs.
            if directory.name.endswith("-analyze"):
                latest_analysis = None
            continue
        if status != "completed":
            raise RefinerError(f"Analysis call is not settled: {directory.name}")
        if directory.name.endswith("-analyze"):
            request, response = _call(root, directory, "analyze", recorded)
            request_input = request.get("input")
            if not isinstance(request_input, dict) or any(request_input.get(k) != v for k, v in inputs.items()):
                raise RefinerError("Analysis recovery request does not match the frozen business inputs")
            try:
                validate_analysis(response, inputs.get("requested_name"), source_ids,
                                  standards=inputs.get("standards"))
            except Exception as exc:
                # A completed model call is not a completed analysis checkpoint.
                # Keep its authenticated request/response in the migration file
                # inventory, but never present an invalid result as reusable
                # analysis or as an independently rejected review pair.
                latest_analysis = None
                unvalidated_analysis = (directory.name, response)
                if invalid_analysis is not None:
                    invalid_analysis[:] = [{"analysis_call": directory.name, "analysis": response,
                                            "validation_error": str(exc)}]
                if unvalidated_calls is not None and directory.name not in unvalidated_calls:
                    unvalidated_calls.append(directory.name)
                continue
            latest_analysis = (directory, response)
            unvalidated_analysis = None
            if invalid_analysis is not None:
                invalid_analysis.clear()
        elif directory.name.endswith("-analysis-review"):
            request, response = _call(root, directory, "analysis-review", recorded)
            payload = request.get("input")
            # Clarification reviews use a different payload/contract and cannot
            # serve as the independent review of a full business analysis.
            if not isinstance(payload, dict) or "inputs" not in payload:
                continue
            if (latest_analysis is None and unvalidated_analysis is not None
                    and payload.get("inputs") == inputs
                    and payload.get("analysis") == unvalidated_analysis[1]):
                # A review that merely follows an invalid response cannot
                # promote that response into reusable analysis history.
                unvalidated_analysis = None
                if invalid_analysis is not None:
                    invalid_analysis.clear()
                if unvalidated_calls is not None and directory.name not in unvalidated_calls:
                    unvalidated_calls.append(directory.name)
                continue
            if latest_analysis is None and technical_seed and payload.get("analysis") == technical_seed["analysis"]:
                if payload.get("technical_coverage") != technical_seed["coverage"]:
                    raise RefinerError("Technical analysis review lost its coverage gap")
                latest_analysis = (Path(technical_seed["analysis_call"]), technical_seed["analysis"])
            if latest_analysis is None:
                raise RefinerError("Independent review lacks a validated preceding analysis")
            if payload.get("inputs") != inputs or payload.get("analysis") != latest_analysis[1]:
                raise RefinerError("Independent review is not bound to its preceding analysis and frozen inputs")
            try:
                validate_analysis_review(response, latest_analysis[1])
                review_version = response.get('schema_version')
                if review_version in ('skill-refiner.analysis-review/v3', 'skill-refiner.analysis-review/v4'):
                    if review_version == 'skill-refiner.analysis-review/v4':
                        from .obligations import effective_requirements, standard_sources
                        sources = ([{'id': 'business_description', 'content': inputs['business_description']}]
                                   if inputs.get('business_description') else []) + [
                            {'id': f'spec-{index+1}', 'source': row['source'], 'content': row['content']}
                            for index, row in enumerate(inputs.get('requirements', []))] + standard_sources(inputs.get('standards', []))
                        if payload.get('review_sources') != sources:
                            raise RefinerError('Standard obligation review source inventory changed')
                        if payload.get('effective_contract') != effective_requirements(latest_analysis[1]):
                            raise RefinerError('Standard obligation review lost its effective target contract')
                        if payload.get('revision_delta') is None:
                            if pairs:
                                raise RefinerError('Revised obligation analysis omitted its semantic revision review')
                            if response.get('revision_assessments') != []:
                                raise RefinerError('Initial obligation review cannot claim unbound revision assessments')
                            if response.get('approved') is False:
                                pairs.append({"analysis_call": latest_analysis[0].name, "review_call": directory.name,
                                              "analysis": latest_analysis[1], "review": response})
                            latest_analysis = None
                            continue
                    previous = payload.get('previous_analysis')
                    if not pairs or previous != pairs[-1]['analysis']:
                        raise RefinerError('Revision review is not bound to the latest rejected analysis')
                    delta = analysis_revision_delta(previous, latest_analysis[1])
                    if payload.get('revision_delta') != delta:
                        raise RefinerError('Revision review delta differs from the actual before/after analysis')
                    sources = ([{'id': 'business_description', 'content': inputs['business_description']}]
                               if inputs.get('business_description') else []) + [
                        {'id': f'spec-{index+1}', 'source': row['source'], 'content': row['content']}
                        for index, row in enumerate(inputs.get('requirements', []))] + [
                        {'id': f'standard-{index+1}', 'content': row if isinstance(row, str) else row.get('content', '')}
                        for index, row in enumerate(inputs.get('standards', []))]
                    if review_version == 'skill-refiner.analysis-review/v4':
                        sources = sources[:len(sources) - len(inputs.get('standards', []))] + standard_sources(inputs.get('standards', []))
                    if payload.get('review_sources') != sources:
                        raise RefinerError('Revision review source inventory changed')
                    validate_analysis_revision_review(response, delta, sources, latest_analysis[1])
            except Exception as exc:
                raise RefinerError(f"Historical independent review failed validation: {directory.name}: {exc}") from exc
            if response.get("approved") is not False:
                latest_analysis = None
                continue
            pairs.append({"analysis_call": latest_analysis[0].name, "review_call": directory.name,
                          "analysis": latest_analysis[1], "review": response})
            latest_analysis = None
    if latest_analysis and pending_analysis is not None:
        pending_analysis.append({"analysis_call": latest_analysis[0].name,
                                 "analysis": latest_analysis[1]})
    elif latest_analysis and not allow_pending_clarification:
        raise RefinerError("Completed analysis lacks its independent review; reconcile that incomplete phase before continuing")
    return pairs


def verify_recovery(work, contract, inputs, *, unvalidated_calls=None, pending_analysis=None,
                    invalid_analysis=None, source_manifest=None):
    from .artifact_validation import schema_ref, validate_value
    root = Path(work).resolve()
    record = read_json(root / "analysis-recovery.json")
    validate_value(record, schema_ref("skill-refiner.analysis-recovery"), subject="analysis recovery")
    if digest(record) != contract.get("analysis_recovery_sha256") or record["inputs_sha256"] != digest(inputs):
        raise RefinerError("Analysis recovery provenance or frozen inputs changed")
    expected = record["files"]
    for relative, sha in expected.items():
        rel = Path(relative)
        path = root / rel
        if rel.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or _file_hash(path) != sha:
            raise RefinerError(f"Analysis recovery evidence changed: {relative}")
    for row in record["pairs"]:
        for key in ("analysis_call", "review_call"):
            name = row[key]
            if Path(name).name != name:
                raise RefinerError("Unsafe analysis recovery call path")
            directory = root / "agent-calls" / name
            actual = {p.relative_to(root).as_posix() for p in directory.rglob("*") if p.is_file()}
            declared = {p for p in expected if p.startswith(f"agent-calls/{name}/")}
            if actual != declared:
                raise RefinerError(f"Analysis recovery call inventory changed: {name}")
    technical_seed = None
    if contract.get("technical_recovery_sha256") and (root / "technical-coverage.json").is_file():
        from .technical_recovery import verify_technical_recovery
        from .technical_triage import route_technical_questions
        pair = verify_technical_recovery(root, contract, inputs)
        state = read_json(root / "state.json")
        coverage = read_json(root / "technical-coverage.json")
        if state.get("technical_coverage_sha256") != digest(coverage):
            raise RefinerError("Technical coverage state and evidence disagree")
        matches = []
        for path in sorted(root.glob("technical-triage-*.json")):
            derived, gap = route_technical_questions(pair["analysis"], read_json(path), pair["review"])
            if gap == coverage:
                matches.append(derived)
        if len(matches) != 1:
            raise RefinerError("Technical coverage lacks exactly one validated triage")
        technical_seed = {"analysis_call": pair["analysis_call"], "analysis": matches[0], "coverage": coverage}
    history = rejected_history(root, inputs,
                               allow_pending_clarification=bool(contract.get("technical_recovery_sha256")),
                               technical_seed=technical_seed, unvalidated_calls=unvalidated_calls,
                               pending_analysis=pending_analysis, invalid_analysis=invalid_analysis)
    if source_manifest is not None and (pending_analysis or invalid_analysis):
        rows = [row for row in source_manifest.get("stages", []) if row.get("id") == "analysis"]
        if len(rows) != 1 or rows[0].get("status") not in {"blocked", "error", "pending"}:
            raise RefinerError("Pending analysis is not attached to an incomplete source stage")
        recorded = {item["path"]: item["sha256"] for item in rows[0].get("artifacts", [])}
        call_names = {row["analysis_call"] for row in (pending_analysis or [])}
        call_names.update(row["analysis_call"] for row in (invalid_analysis or []))
        for call_name in call_names:
            directory = root / "agent-calls" / call_name
            if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
                raise RefinerError("Unsafe incomplete analysis call directory")
            declared = {relative for relative in recorded if relative.startswith(f"agent-calls/{call_name}/")}
            required = {f"agent-calls/{call_name}/{name}" for name in
                        ("request.json", "request-manifest.json", "response.json", "execution.json")}
            if not required <= declared:
                raise RefinerError(f"Incomplete analysis call lacks required stage artifacts: {call_name}")
            for relative in declared:
                path = root / relative
                if path.is_symlink() or not path.is_file() or recorded.get(relative) != _file_hash(path):
                    raise RefinerError(f"Incomplete analysis call differs from the stage inventory: {relative}")
    if len(history) < len(record["pairs"]):
        raise RefinerError("Analysis recovery call pairs are missing")
    for expected_pair, actual in zip(record["pairs"], history):
        if (expected_pair["analysis_call"] != actual["analysis_call"] or expected_pair["review_call"] != actual["review_call"]
                or expected_pair["analysis_sha256"] != digest(actual["analysis"])
                or expected_pair["review_sha256"] != digest(actual["review"])):
            raise RefinerError("Analysis recovery pair no longer matches its provenance")
    return history

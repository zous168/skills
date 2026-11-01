"""Preserve a newer pending clarification without replacing an older technical gap."""
from __future__ import annotations

from pathlib import Path

from .analysis_recovery import _call, _file_hash
from .common import RefinerError, digest, read_json
from .technical_recovery import clarification_history


def latest_clarification(root, inputs, history, *, source_manifest=None,
                         validated_clarification_boundary=0):
    """Return a validated pending chain after the latest independently rejected analysis."""
    from .creation import validate_analysis
    if not history:
        return None
    root = Path(root).resolve()
    last = history[-1]
    after = max(int(last["review_call"].split("-", 1)[0]), validated_clarification_boundary)
    recorded = None
    if source_manifest is not None:
        stage = next((row for row in source_manifest.get("stages", []) if row["id"] == "analysis"), None)
        if not stage or stage["status"] not in {"blocked", "error", "pending"}:
            raise RefinerError("Latest clarification needs an incomplete source analysis")
        recorded = {r["path"]: r["sha256"] for r in stage.get("artifacts", [])}
    analyses, reviews = [], []
    calls = root / "agent-calls"
    for directory in sorted(calls.iterdir() if calls.is_dir() else []):
        prefix = directory.name.split("-", 1)[0]
        role = ("analyze" if directory.name.endswith("-analyze") else
                "analysis-review" if directory.name.endswith("-analysis-review") else None)
        if not directory.is_dir() or not prefix.isdigit() or int(prefix) <= after or not role:
            continue
        meta = directory / "request-manifest.json"
        if meta.is_symlink() or not meta.is_file():
            raise RefinerError("Latest clarification call has no safe manifest")
        if recorded is not None and recorded.get(meta.relative_to(root).as_posix()) != _file_hash(meta):
            raise RefinerError("Latest clarification call manifest differs from source stage evidence")
        manifest = read_json(meta)
        if manifest.get("role") != role:
            raise RefinerError("Latest clarification call role differs from its directory")
        if manifest.get("status") in {"starting", "running", "response_received"}:
            raise RefinerError("Latest clarification contains an unsettled call")
        if manifest.get("status") != "completed":
            # Failed calls remain charged and retained by the migration ledger.
            # They are not completed clarification evidence.
            continue
        request, response = _call(root, directory, role, recorded)
        if role == "analyze":
            request_input = request.get("input")
            if not isinstance(request_input, dict) or any(request_input.get(key) != value
                                                          for key, value in inputs.items()):
                raise RefinerError("Latest clarification analysis does not match the frozen business inputs")
            source_ids = ({"business_description"} if inputs.get("business_description") else set()) | {
                f"spec-{index + 1}" for index in range(len(inputs.get("requirements", [])))}
            try:
                validate_analysis(response, inputs.get("requested_name"), source_ids,
                                  standards=inputs.get("standards"))
            except Exception:
                # Preserve the completed call as charged evidence, but a
                # semantically invalid response cannot start a recoverable
                # clarification chain or block reuse of an earlier valid one.
                continue
            analyses.append((int(prefix), request))
        elif response.get("schema_version") == "skill-refiner.clarification-review/v1":
            reviews.append(int(prefix))
        elif "inputs" in request.get("input", {}):
            raise RefinerError("Later completed full review needs checkpoint reconciliation before pending clarification migration")
    if not analyses:
        return None
    if not reviews or max(number for number, _ in analyses) > max(reviews):
        raise RefinerError("Latest completed analysis has no validated clarification review")
    # A failed call before the latest completed analysis is separately charged.
    # Only the completed pending chain is an input to clarification validation.
    first = min(number for number, _ in analyses)
    pairs, retained = clarification_history(root, inputs, after_call=first - 1,
                                            before_call=max(reviews), source_manifest=source_manifest)
    first_request = next(request for number, request in analyses if number == first)["input"]
    if (first_request.get("previous_analysis") != last["analysis"]
            or first_request.get("previous_analysis_reviews") != [row["review"] for row in history]):
        raise RefinerError("Latest clarification lost the complete preceding rejected analysis/review history")
    return {"pair": pairs[-1], "calls": retained, "after_review_call": last["review_call"],
            "after_review_sha256": digest(last["review"])}


def verify_clarification_recovery(work, contract, inputs, history):
    from .artifact_validation import schema_ref, validate_value
    root = Path(work).resolve()
    path = root / "clarification-recovery.json"
    marker = contract.get("clarification_recovery_sha256")
    if path.is_symlink():
        raise RefinerError("Unsafe latest clarification recovery artifact")
    if path.is_file() != bool(marker):
        raise RefinerError("Latest clarification recovery contract and artifact disagree")
    if not marker:
        return None
    record = read_json(path)
    validate_value(record, schema_ref("skill-refiner.clarification-recovery"), subject="latest clarification recovery")
    if digest(record) != marker or record["inputs_sha256"] != digest(inputs):
        raise RefinerError("Latest clarification recovery provenance or inputs changed")
    base = root / "analysis-previous" / "latest-clarification"
    if base.is_symlink() or not base.resolve().is_relative_to(root):
        raise RefinerError("Unsafe latest clarification evidence root")
    actual = set()
    for evidence in base.rglob("*"):
        if evidence.is_symlink() or (hasattr(evidence, "is_junction") and evidence.is_junction()):
            raise RefinerError("Unsafe link in latest clarification evidence")
        if evidence.is_file():
            actual.add(evidence.relative_to(root).as_posix())
    if actual != set(record["files"]):
        raise RefinerError("Latest clarification evidence inventory changed")
    for relative, sha in record["files"].items():
        evidence = root / relative
        if not evidence.resolve().is_relative_to(base) or not evidence.is_file() or _file_hash(evidence) != sha:
            raise RefinerError("Latest clarification evidence hash changed")
    matched = [i for i, row in enumerate(history) if row["review_call"] == record["after_review_call"]
               and digest(row["review"]) == record["after_review_sha256"]]
    if len(matched) != 1:
        raise RefinerError("Latest clarification does not follow its bound rejected review")
    prefix = history[:matched[0] + 1]
    pairs, calls = clarification_history(base, inputs, after_call=0)
    pair = pairs[-1]
    first_request, _ = _call(base, base / "agent-calls" / calls[0], "analyze")
    if (calls != record["calls"] or digest(pair["analysis"]) != record["analysis_sha256"]
            or digest(pair["review"]) != record["clarification_sha256"]
            or first_request["input"].get("previous_analysis") != prefix[-1]["analysis"]
            or first_request["input"].get("previous_analysis_reviews") != [row["review"] for row in prefix]):
        raise RefinerError("Latest clarification request/response chain or preceding feedback changed")
    return {"pair": pair, "calls": calls, "after_review_call": record["after_review_call"],
            "after_review_sha256": record["after_review_sha256"]}

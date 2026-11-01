"""Bounded, source-grounded revision of an authenticated unapproved requirement draft."""
import copy
import hashlib
import json
from pathlib import Path

from .common import RefinerError, digest, read_json


def unapproved_recovery(recovery):
    rows = recovery.get("control_records", []) if isinstance(recovery, dict) else []
    return bool(rows) and all(isinstance(row, dict) and row.get("status") in {"failed", "invalid_coverage", "validation_error"} for row in rows)


def requirement_revision(previous, current):
    def indexed(rows, label):
        if not isinstance(rows, list):
            raise RefinerError(label + " must be a list")
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in result:
                raise RefinerError(label + " requires unique IDs")
            result[row["id"]] = row
        return result
    old, new = indexed(previous.get("requirements"), "requirements"), indexed(current.get("requirements"), "requirements")
    if old.keys() - new.keys():
        raise RefinerError("Requirement revision removed a prior requirement")
    changes = []
    def add(kind, req_id, criterion_id, field, before, after):
        if before != after:
            changes.append({"change_id": f"RQ{len(changes)+1:04}", "kind": kind, "requirement_id": req_id,
                            "criterion_id": criterion_id, "field": field, "before": before, "after": after})
    for rid in sorted(old):
        a, b = old[rid], new[rid]
        if a.get("required") is True and b.get("required") is not True:
            raise RefinerError("Requirement revision weakened a required requirement: " + rid)
        ac, bc = indexed(a.get("acceptance_criteria", []), "acceptance criteria"), indexed(b.get("acceptance_criteria", []), "acceptance criteria")
        if ac.keys() - bc.keys():
            raise RefinerError("Requirement revision removed an existing acceptance criterion: " + rid)
        for field in sorted((a.keys() | b.keys()) - {"id", "acceptance_criteria"}):
            add("requirement_field", rid, None, field, {"present":field in a,"value":a.get(field)}, {"present":field in b,"value":b.get(field)})
        for cid in sorted(ac):
            for field in sorted((ac[cid].keys() | bc[cid].keys()) - {"id"}):
                add("criterion_field", rid, cid, field, {"present":field in ac[cid],"value":ac[cid].get(field)}, {"present":field in bc[cid],"value":bc[cid].get(field)})
        for cid in sorted(bc.keys() - ac.keys()):
            add("criterion_added", rid, cid, "record", None, copy.deepcopy(bc[cid]))
    for rid in sorted(new.keys() - old.keys()):
        add("requirement_added", rid, None, "record", None, copy.deepcopy(new[rid]))
    if len(changes) > 256 or len(json.dumps(changes, ensure_ascii=False).encode("utf-8")) > 200000:
        raise RefinerError("Requirement revision exceeds the bounded review limit")
    return {"schema_version":"skill-refiner.requirement-revision/v1", "previous_plan_sha256":digest(previous),
            "plan_sha256":digest(current), "review_schema_sha256":digest(read_json(Path(__file__).resolve().parents[2] / "schemas/v7/requirement-revision-review.schema.json")), "changes":changes}


def review_schema(schema, revision):
    if not revision or not revision["changes"]:
        return schema
    schema = copy.deepcopy(schema)
    schema["required"].append("requirement_revision_assessments")
    schema["properties"]["requirement_revision_assessments"] = read_json(Path(__file__).resolve().parents[2] / "schemas/v7/requirement-revision-review.schema.json")
    return schema


def normative_sources(inputs):
    result = {}
    for row in inputs.get("requirements", []):
        if isinstance(row, dict) and isinstance(row.get("source"), str) and isinstance(row.get("content"), str):
            if row["source"] in result and result[row["source"]] != row["content"]:
                raise RefinerError("Requirement revision source identifier is ambiguous")
            result[row["source"]] = row["content"]
    if isinstance(inputs.get("business_description"), str) and inputs["business_description"]:
        result["business_description"] = inputs["business_description"]
    for i, text in enumerate(inputs.get("standards", [])):
        if isinstance(text, str): result[f"standard:{i}"] = text
    return result


def validate_assessments(review, revision, inputs):
    if not revision or not revision["changes"]: return
    refs = normative_sources(inputs)
    assessments = review["requirement_revision_assessments"]
    expected = {x["change_id"] for x in revision["changes"]}
    ids = [x["change_id"] for x in assessments]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise RefinerError("Independent review must assess every requirement change exactly once")
    for row in assessments:
        if row["previous_plan_sha256"] != revision["previous_plan_sha256"] or row["plan_sha256"] != revision["plan_sha256"]:
            raise RefinerError("Requirement change assessment is not bound to both plans")
        if any(not e["quote"].strip() or e["quote"] not in refs.get(e["source"], "") or
               e["source_sha256"] != hashlib.sha256(refs.get(e["source"], "").encode("utf-8")).hexdigest()
               for e in row["evidence"]):
            raise RefinerError("Requirement change assessment cites unverified normative evidence")
        if len(row["reason"].strip()) < 12:
            raise RefinerError("Requirement revision assessment has no substantive reason")
        if review["approved"] and (not row["preserves_prior"] or not row["supported_by_source"]):
            raise RefinerError("Requirement revision lacks independent support or weakens prior requirements")

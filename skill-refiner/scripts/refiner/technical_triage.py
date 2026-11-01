"""Route observation questions to bounded technical coverage, not user decisions."""
from __future__ import annotations

import copy

from .artifact_validation import schema_ref, validate_value
from .common import RefinerError, digest
from .obligations import effective_requirements


def validate_triage(triage, analysis):
    try:
        validate_value(triage, schema_ref("skill-refiner.technical-triage"), subject="technical triage")
    except ValueError as exc:
        raise RefinerError(str(exc)) from exc
    expected = {row["id"] for row in analysis["blocking_questions"]}
    rows = triage["assessments"]
    ids = [row["question_id"] for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise RefinerError("Technical triage must assess every blocking question exactly once")
    requirements = {row["id"] for row in effective_requirements(analysis) if row["required"]}
    scenarios = {row["id"] for row in analysis["scenarios"] if row["applicable"]}
    for row in rows:
        if not set(row["requirement_ids"]) <= requirements or not set(row["scenario_ids"]) <= scenarios:
            raise RefinerError("Technical triage references unknown or inapplicable requirements/scenarios")
        if row["domain"] == "technical_evidence":
            linked = [scenario for scenario in analysis["scenarios"] if scenario["id"] in row["scenario_ids"]]
            required = set(row["requirement_ids"])
            if any(not required.intersection(scenario["requirement_ids"]) for scenario in linked):
                raise RefinerError("Technical triage scenario must test a referenced required requirement")
            if not required <= {req for scenario in linked for req in scenario["requirement_ids"]}:
                raise RefinerError("Technical triage requirement must be mapped to a referenced scenario")
        if not row["reason"].strip():
            raise RefinerError("Technical triage requires an explicit rationale")


def local_observation_probe():
    """A finite parser control is partial evidence, never a complete observation proof."""
    from .runtime import parse_codex
    events = [{"type": "item.completed", "item": {"type": "command_execution", "id": "command-probe",
                                                    "command": "fixture-command", "exit_code": 0,
                                                    "aggregated_output": "fixture"}},
              {"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "noncommand-probe",
                                                    "name": "fixture-noncommand"}},
              {"type": "turn.completed"}]
    _, trace = parse_codex(events)
    rendered = repr(trace)
    return {"probe": "offline synthetic parser control; not a live provider or target execution",
            "command_event_visible": "fixture-command" in rendered,
            "noncommand_fixture_visible": "fixture-noncommand" in rendered,
            "limitations": ["A finite parser fixture cannot prove a complete tool transcript.",
                            "Observed command text cannot establish absence of nested network calls or temporary writes.",
                            "The Python audit helper covers its observed Python execution, not arbitrary native processes or OS isolation."]}


def route_technical_questions(analysis, triage, clarification_review=None):
    validate_triage(triage, analysis)
    classifications = ({row["question_id"]: row["classification"]
                        for row in clarification_review["question_assessments"]}
                       if clarification_review is not None else None)
    rows = [row for row in triage["assessments"] if row["domain"] == "technical_evidence"
            and (classifications is None or classifications.get(row["question_id"]) == "required_in_scope")]
    if not rows:
        return copy.deepcopy(analysis), None
    by_id = {row["id"]: row for row in analysis["blocking_questions"]}
    questions = [{"question_id": row["question_id"], "question": by_id[row["question_id"]]["question"],
                  "reason": row["reason"], "requirement_ids": row["requirement_ids"], "scenario_ids": row["scenario_ids"]}
                 for row in rows]
    gap = {"schema_version": "skill-refiner.technical-coverage/v1", "status": "coverage_gap",
           "analysis_sha256": digest(analysis), "questions": questions, "observation": local_observation_probe(),
           "resolution": "Close only after independent evidence covers the required actions in the actual execution scope."}
    validate_value(gap, schema_ref("skill-refiner.technical-coverage"), subject="technical coverage")
    technical_ids = {row["question_id"] for row in rows}
    derived = copy.deepcopy(analysis)
    derived["blocking_questions"] = [row for row in analysis["blocking_questions"] if row["id"] not in technical_ids]
    return derived, gap

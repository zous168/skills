"""Business analysis and first implementation, gated by the real refinement pipeline."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time

from .agent import Agent
from .common import Cancelled, RefinerError, digest, manifest, read_json, snapshot, write_json
from .pipeline import apply_changes, run as refine
from .profiles import load_config
from .schemas import PLAN, REQUIREMENTS, REVIEW, RISK_DIMENSIONS, SCENARIO_INTENT, SCENARIO_DETAIL_V3
from .standards import AUTHORING, discover_project_standards, lint
from .stages import StageManifest, CREATE_EVENTS
from .plan import write_test_scenarios, write_work_plan
from .preflight import build_runtime_preflight, emit_runtime_preflight
from .obligations import (effective_requirements, revision_requirements, standard_sources,
                          validate_obligations, validate_obligation_review)

STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING}
IDS = {"type": "array", "minItems": 1, "uniqueItems": True, "items": STRING}
PYTHON_OBSERVER_GUIDE = """For Python business scripts, cases may set helpers=[\"python_probe\"].
The controller supplies frozen refiner-python-probe.py in each task workspace; do not put its
code in files or modify it. Invocation: python3 refiner-python-probe.py --evidence observed.json
--track input.json --protect-inputs --deny-network --deny-subprocess INSTALLED_SCRIPT input.json.
Resolve INSTALLED_SCRIPT under the authoritative installed root. The helper really executes it
as __main__ with its argv and forwards complete stdout unchanged. Observe the helper command,
not an unobservable inner shell command. tool_output_matches can check its forwarded stdout.
The helper's own exit code is zero for observation transport; assert observed.json /exit_code=0
using file_json_path_equal to check the BUSINESS script's actual exit code. Other evidence keys:
stdout, stdout_json (parsed JSON or null), stderr, inputs_unchanged, write_events, network_events,
subprocess_events, script, script_sha256, argv, input_sha256_before, input_sha256_after.
For no-input-change requirements assert /inputs_unchanged=true, and file_text_equal can independently
check exact UTF-8 input content including whitespace. For no-network tasks assert /network_events=[]
and /subprocess_events=[] with the deny flags. Attempts are recorded and blocked by Python audit
hooks. This observes the tested Python execution, not arbitrary native code or an OS read barrier;
do not invent an absolute guarantee or declare a finite behavioral task impossible for that reason.
Use file_json_path_equal /stdout_json with independent business expectations and response checks.
For multiple invocations use distinct evidence files. Several concrete invalid fixtures may share
one category case; forward complete stdout in invocation order and use json_sequence_equal if
multiple JSON values are requested, with an array of independent expected JSON values. Do not
use text_equal for insignificant key order or whitespace. tool_output_matches supports ordered complete outputs; do not wrap them in
an array unless the script's business contract does. A synthetic invalid-UTF8 fixture can be
created by a small UTF-8 setup Python file writing bytes; the executor runs setup before the
observer. This does not require external binary assets. Keep invalid-byte inputs otherwise
schema-valid if a permissive decoder ignores/replaces the bad byte. Never claim exhaustive coverage from category
labels alone. Do not require an exact newline count or extra features absent from frozen business
requirements. Direct managed command success is already based on actual parsed exit status zero;
observer invocation success alone does not establish the observed script's exit status.
"""
ANALYSIS_V1 = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "goal", "requirements", "workflow", "scenarios", "assumptions",
                 "external_dependencies", "blocking_questions"],
    "properties": {
        "name": {"type": "string", "maxLength": 64, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "goal": STRING, "requirements": REQUIREMENTS,
        "workflow": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "action", "inputs", "outputs", "completion_check", "failure_behavior", "requirement_ids"],
            "properties": {"id": STRING, "action": STRING, "inputs": STRING, "outputs": STRING,
                           "completion_check": {"type": "string", "minLength": 8},
                           "failure_behavior": STRING, "requirement_ids": IDS}}},
        "scenarios": {"type": "array", "minItems": 3, "items": SCENARIO_INTENT},
        "assumptions": STRINGS, "external_dependencies": STRINGS, "blocking_questions": STRINGS,
    },
}
ANALYSIS_REVIEW_V1 = {
    **REVIEW,
    "properties": {**REVIEW["properties"], "requirement_assessments": {
        "type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["requirement_id", "grounded", "acceptance_criteria_atomic",
                         "criteria_observable", "basis"],
            "properties": {"requirement_id": STRING, "grounded": {"type": "boolean"},
                           "acceptance_criteria_atomic": {"type": "boolean"},
                           "criteria_observable": {"type": "boolean"}, "basis": STRING}}},
        "scenario_assessments": {"type": "array", "minItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["scenario_id", "detail_executable", "risk_coverage_complete",
                         "traceability_complete", "basis"],
            "properties": {"scenario_id": STRING, "detail_executable": {"type": "boolean"},
                           "risk_coverage_complete": {"type": "boolean"},
                           "traceability_complete": {"type": "boolean"}, "basis": STRING}}},
    },
    "required": ["approved", "issues", "requirement_assessments", "scenario_assessments"],
}
ANALYSIS_V2 = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "name", "goal", "context", "scope", "actors", "requirements",
                 "workflow", "scenarios", "risk_assessments", "assumptions", "external_dependencies",
                 "blocking_questions"],
    "properties": {
        "schema_version": {"const": "skill-refiner.analysis/v2"},
        "name": {"type": "string", "maxLength": 64, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "goal": {"type": "string", "minLength": 8},
        "context": {"type": "object", "additionalProperties": False,
            "required": ["user_intent", "usage_context", "primary_user_ids"],
            "properties": {"user_intent": {"type": "string", "minLength": 8},
                           "usage_context": {"type": "string", "minLength": 8},
                           "primary_user_ids": IDS}},
        "scope": {"type": "object", "additionalProperties": False,
            "required": ["in_scope", "out_of_scope", "constraints"],
            "properties": {"in_scope": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "out_of_scope": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "constraints": STRINGS}},
        "actors": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "role", "needs", "permission_boundary"],
            "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                           "role": {"type": "string", "minLength": 3},
                           "needs": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "permission_boundary": {"type": "string", "minLength": 8}}}},
        "requirements": REQUIREMENTS,
        "workflow": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "order", "action", "actor_ids", "preconditions", "inputs", "outputs",
                         "completion_check", "failure_behavior", "requirement_ids", "dependency_ids"],
            "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                           "order": {"type": "integer", "minimum": 1},
                           "action": {"type": "string", "minLength": 3},
                           "actor_ids": IDS,
                           "preconditions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "inputs": {"type": "string", "minLength": 3},
                           "outputs": {"type": "string", "minLength": 3},
                           "completion_check": {"type": "string", "minLength": 8},
                           "failure_behavior": {"type": "string", "minLength": 8},
                           "requirement_ids": IDS,
                           "dependency_ids": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}}}},
        "scenarios": {"type": "array", "minItems": 3, "items": SCENARIO_INTENT},
        "risk_assessments": {"type": "array", "minItems": len(RISK_DIMENSIONS), "items": {
            "type": "object", "additionalProperties": False,
            "required": ["dimension", "applicable", "basis", "scenario_ids"],
            "properties": {"dimension": {"enum": RISK_DIMENSIONS}, "applicable": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12},
                           "scenario_ids": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}},
            "allOf": [{"if": {"properties": {"applicable": {"const": True}}},
                       "then": {"properties": {"scenario_ids": {"minItems": 1}}}},
                      {"if": {"properties": {"applicable": {"const": False}}},
                       "then": {"properties": {"scenario_ids": {"maxItems": 0}}}}]}},
        "assumptions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "statement", "impact", "validation"],
            "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                           "statement": {"type": "string", "minLength": 3},
                           "impact": {"type": "string", "minLength": 3},
                           "validation": {"type": "string", "minLength": 3}}}},
        "external_dependencies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "name", "purpose", "required", "availability", "failure_behavior", "requirement_ids"],
            "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                           "name": {"type": "string", "minLength": 2},
                           "purpose": {"type": "string", "minLength": 5},
                           "required": {"type": "boolean"},
                           "availability": {"type": "string", "minLength": 5},
                           "failure_behavior": {"type": "string", "minLength": 8},
                           "requirement_ids": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}}}},
        "blocking_questions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "question", "decision_owner", "impact"],
            "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                           "question": {"type": "string", "minLength": 8},
                           "decision_owner": {"type": "string", "minLength": 2},
                           "impact": {"type": "string", "minLength": 8}}}},
    },
}
ANALYSIS_V3 = copy.deepcopy(ANALYSIS_V2)
ANALYSIS_V3["properties"]["schema_version"] = {"const": "skill-refiner.analysis/v3"}
_scenario_v3 = ANALYSIS_V3["properties"]["scenarios"]["items"]
for _field in ("requirement_ids", "acceptance_criteria_ids"):
    _scenario_v3["properties"][_field].pop("minItems")
_detail_v3 = copy.deepcopy(SCENARIO_DETAIL_V3)
_excluded_detail_v3 = copy.deepcopy(_detail_v3)
for _field in ("preconditions", "actions", "expected_observables", "forbidden_outcomes", "risk_dimensions"):
    _excluded_detail_v3["properties"][_field].pop("minItems", None)
_scenario_v3["properties"]["detail"] = {"anyOf": [_excluded_detail_v3, {"type": "null"}]}
_scenario_v3["allOf"].append({
    "if": {"properties": {"applicable": {"const": True}}, "required": ["applicable"]},
    "then": {"properties": {"requirement_ids": {"minItems": 1}, "acceptance_criteria_ids": {"minItems": 1},
                            "detail": _detail_v3}}})
ANALYSIS_V4 = read_json(Path(__file__).resolve().parents[2] / 'schemas/v4/analysis.schema.json')
ANALYSIS = ANALYSIS_V4
ANALYSIS_REVIEW_V2 = {
    **ANALYSIS_REVIEW_V1,
    "required": ANALYSIS_REVIEW_V1["required"] + ["schema_version", "context_scope_assessment",
        "workflow_assessments", "risk_assessments", "dependencies_assessed", "blocking_decisions_actionable"],
    "properties": {**ANALYSIS_REVIEW_V1["properties"],
        "schema_version": {"const": "skill-refiner.analysis-review/v2"},
        "context_scope_assessment": {"type": "object", "additionalProperties": False,
            "required": ["intent_grounded", "actors_complete", "scope_clear", "constraints_complete", "basis"],
            "properties": {"intent_grounded": {"type": "boolean"}, "actors_complete": {"type": "boolean"},
                           "scope_clear": {"type": "boolean"}, "constraints_complete": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}}},
        "workflow_assessments": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["workflow_id", "order_and_actors_clear", "inputs_outputs_complete",
                         "completion_and_failure_testable", "requirement_traceable", "basis"],
            "properties": {"workflow_id": {"type": "string"},
                           "order_and_actors_clear": {"type": "boolean"},
                           "inputs_outputs_complete": {"type": "boolean"},
                           "completion_and_failure_testable": {"type": "boolean"},
                           "requirement_traceable": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}}}},
        "risk_assessments": {"type": "array", "minItems": len(RISK_DIMENSIONS), "items": {
            "type": "object", "additionalProperties": False,
            "required": ["dimension", "applicability_justified", "scenario_links_complete", "basis"],
            "properties": {"dimension": {"enum": RISK_DIMENSIONS}, "applicability_justified": {"type": "boolean"},
                           "scenario_links_complete": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}}}},
        "dependencies_assessed": {"type": "boolean"},
        "blocking_decisions_actionable": {"type": "boolean"},
    },
}
ANALYSIS_REVIEW = ANALYSIS_REVIEW_V2
ANALYSIS_REVIEW_V3 = read_json(Path(__file__).resolve().parents[2] / 'schemas/v3/analysis-review.schema.json')
ANALYSIS_REVIEW_V4 = read_json(Path(__file__).resolve().parents[2] / 'schemas/v4/analysis-review.schema.json')
DRAFT = {
    "type": "object", "additionalProperties": False,
    "required": ["implementation_notes", "files"],
    "properties": {"implementation_notes": STRING, "files": {"type": "array", "minItems": 1,
        "maxItems": 20, "items": {"type": "object", "additionalProperties": False,
            "required": ["path", "content", "reason", "requirement_ids"],
            "properties": {"path": STRING, "content": {"type": "string"},
                           "reason": {"type": "string", "minLength": 8}, "requirement_ids": IDS}}}},
}
DRAFT_INSTRUCTION = (
    'Create the complete first implementation of the requested Agent Skill from the approved business '
    'analysis and original requirements. Implement the effective_contract: business R and applicable target O '
    'obligations retain distinct real sources. Implement the declared target schema/validator and bounded '
    'self-review interfaces without adding unrequested public output fields. Include O IDs in relevant files '
    'requirement_ids. Planned mechanisms are designs, never evidence of passed behavior. '
    'All file paths are relative to the Skill root: use SKILL.md, '
    'scripts/NAME.py and references/NAME.md without a surrounding Skill-name directory. '
    'Return full UTF-8 files, including SKILL.md with valid YAML '
    'name/description matching analysis.name, focused instructions, discoverable references, and working '
    'support scripts wherever deterministic business work benefits from them. Implement the actual workflow, '
    'not placeholders, TODOs, mock results, hardcoded inputs or documentation-only scaffolding. Use the local '
    'runtime and standard library where sufficient. Define concrete invocation, input/output and error '
    'contracts consistent with the user request; record nonmaterial assumptions. Write all user-facing Skill '
    'instructions and human-readable package text in Simplified Chinese by default, including SKILL.md, references, '
    'examples, prompts, and user-visible script messages. Keep code identifiers, schema property names, protocol '
    'literals, source quotations, and business output in the language explicitly required by the workflow. Honor an '
    'explicit user language requirement when it differs. A local script should '
    'return machine-checkable results and instructions must require using it when it implements the workflow. '
    'Preserve the requested error output and exit-status contract rather than inventing one. '
    'When a missing business choice, permission, runtime setup, configuration, or dependency can materially '
    'block correct or safe use, include a conditional help-request procedure in the Skill: inspect available '
    'evidence first, explain the concrete blocker and smallest safe next action, ask through the host native '
    'input tool when available (otherwise conversation), and rerun affected checks after the answer. Ask only '
    'for decisions the user must make; record safe local configuration steps, and never request secrets in chat. '
    'Do not add questions for deterministic details that can be handled as explicit assumptions. '
    'Do not instruct an executor to edit its installed Skill. Do not create tests/evals, graders, credentials, '
    'global configuration or fabricated service adapters. Independent tests are generated later. Every file '
    'must cite valid requirement IDs. Include the bounded self-review protocol in SKILL.md: freeze requirement-linked '
    'checks before execution, cite evidence at handoff/final response, allow one targeted correction, then stop '
    'blocked if evidence still fails. Do not add self-review loops or expose hidden chain-of-thought.'
)


def normalize_draft(draft, name):
    """Accept one named packaging directory, preserving every file's contents."""
    result = copy.deepcopy(draft)
    paths = [f['path'] for f in result['files']]
    prefix = name + '/'
    wrapped = prefix + 'SKILL.md' in paths and all(path.startswith(prefix) for path in paths)
    mapping = {}
    for file in result['files']:
        original = file['path']
        if wrapped:
            file['path'] = original[len(prefix):]
        mapping[original] = file['path']
    return result, mapping


def validate_analysis(analysis, requested_name=None, source_ids=None, *, standards=None):
    import jsonschema
    version = analysis.get("schema_version")
    if version not in (None, "skill-refiner.analysis/v1", "skill-refiner.analysis/v2", "skill-refiner.analysis/v3", "skill-refiner.analysis/v4"):
        raise RefinerError(f"Unsupported business analysis schema version: {version}")
    schema = (ANALYSIS_V1 if version in (None, "skill-refiner.analysis/v1") else
              ANALYSIS_V2 if version == "skill-refiner.analysis/v2" else
              ANALYSIS_V4 if version == "skill-refiner.analysis/v4" else ANALYSIS_V3)
    jsonschema.validate(analysis, schema)
    if requested_name and analysis["name"] != requested_name:
        raise RefinerError("Business analysis changed the requested Skill name")
    validate_obligations(analysis, standards)
    requirements = effective_requirements(analysis)
    ids = [r["id"] for r in requirements]
    if source_ids and any(not any(r['source'].startswith(source + ':') for source in source_ids)
                          for r in analysis['requirements']):
        raise RefinerError('Business requirements must cite an original business source ID followed by a colon; '
                           'target standards use the separate target_obligations inventory')
    if len(set(ids)) != len(ids) or any(not x.strip() for x in ids):
        raise RefinerError("Business requirement IDs must be nonempty and unique")
    if not {"normal", "boundary", "error"} <= {s["kind"] for s in analysis["scenarios"]}:
        raise RefinerError("Analysis must assess normal, boundary and error scenario categories")
    if not any(s["kind"] == "normal" and s["applicable"] for s in analysis["scenarios"]):
        raise RefinerError("Normal business behavior must have an executable scenario")
    criteria_by_requirement = {r["id"]: {c["id"] for c in r["acceptance_criteria"]}
                               for r in requirements}
    criterion_ids = [criterion["id"] for req in requirements
                     for criterion in req["acceptance_criteria"]]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise RefinerError("Acceptance criterion IDs must be globally unique")
    scenario_ids = [s["id"] for s in analysis["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise RefinerError("Scenario IDs must be unique")
    scenario_signatures = [json.dumps({"kind": row["kind"], "situation": row["situation"],
                                       "detail": row["detail"]}, ensure_ascii=False,
                                      sort_keys=True).casefold()
                           for row in analysis["scenarios"]]
    if len(scenario_signatures) != len(set(scenario_signatures)):
        raise RefinerError("Scenario matrix contains duplicate situations and execution details")
    for item in analysis["workflow"] + analysis["scenarios"]:
        if not set(item["requirement_ids"]) <= set(ids):
            raise RefinerError("Workflow or scenario references an unknown business requirement")
    for scenario in analysis["scenarios"]:
        allowed_criteria = set().union(*(criteria_by_requirement[rid]
                                         for rid in scenario["requirement_ids"])) if scenario["requirement_ids"] else set()
        if not set(scenario["acceptance_criteria_ids"]) <= allowed_criteria:
            raise RefinerError(f"Scenario {scenario['id']} references an acceptance criterion outside its requirements")
        if scenario["applicable"] and (not scenario["requirement_ids"] or not scenario["acceptance_criteria_ids"]):
            raise RefinerError(f"Applicable scenario {scenario['id']} must map to requirements and acceptance criteria")
        if not scenario["applicable"] and not scenario["exclusion_reason"].strip():
            raise RefinerError(f"Excluded scenario {scenario['id']} needs a scope-based reason")
    required_criteria = {c["id"] for r in requirements if r["required"]
                         for c in r["acceptance_criteria"]}
    covered_criteria = {criterion_id for scenario in analysis["scenarios"] if scenario["applicable"]
                        for criterion_id in scenario["acceptance_criteria_ids"]}
    if not required_criteria <= covered_criteria:
        raise RefinerError("Scenario matrix omits required acceptance criteria: " +
                           ", ".join(sorted(required_criteria - covered_criteria)))
    if version in ("skill-refiner.analysis/v2", "skill-refiner.analysis/v3", "skill-refiner.analysis/v4"):
        actor_ids = [row["id"] for row in analysis["actors"]]
        if len(actor_ids) != len(set(actor_ids)):
            raise RefinerError("Analysis actor IDs must be unique")
        if not set(analysis["context"]["primary_user_ids"]) <= set(actor_ids):
            raise RefinerError("Analysis context references an unknown primary user/actor")
        dependency_ids = [row["id"] for row in analysis["external_dependencies"]]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise RefinerError("External dependency IDs must be unique")
        for dependency in analysis["external_dependencies"]:
            if not set(dependency["requirement_ids"]) <= set(ids):
                raise RefinerError(f"Dependency {dependency['id']} references an unknown requirement")
        workflow_ids = [row["id"] for row in analysis["workflow"]]
        workflow_orders = [row["order"] for row in analysis["workflow"]]
        if len(workflow_ids) != len(set(workflow_ids)):
            raise RefinerError("Workflow step IDs must be unique")
        if sorted(workflow_orders) != list(range(1, len(workflow_orders) + 1)):
            raise RefinerError("Workflow order values must be unique and contiguous from 1")
        if set(workflow_ids) & set(dependency_ids):
            raise RefinerError("Workflow step and external dependency IDs must be distinct")
        step_orders = {row["id"]: row["order"] for row in analysis["workflow"]}
        for step in analysis["workflow"]:
            if not set(step["actor_ids"]) <= set(actor_ids):
                raise RefinerError(f"Workflow step {step['id']} references an unknown actor or dependency")
            for dependency_id in step["dependency_ids"]:
                if dependency_id in step_orders:
                    if step_orders[dependency_id] >= step["order"]:
                        raise RefinerError(f"Workflow step {step['id']} must depend only on earlier workflow steps: {dependency_id}")
                elif dependency_id not in dependency_ids:
                    raise RefinerError(f"Workflow step {step['id']} references an unknown actor or dependency: {dependency_id}")
        risk_rows = analysis["risk_assessments"]
        assessed = [row["dimension"] for row in risk_rows]
        if len(assessed) != len(set(assessed)) or set(assessed) != set(RISK_DIMENSIONS):
            raise RefinerError("Risk assessment must assess every supported risk dimension exactly once")
        scenarios_by_id = {row["id"]: row for row in analysis["scenarios"]}
        for risk in risk_rows:
            linked = [scenarios_by_id.get(sid) for sid in risk["scenario_ids"]]
            if any(row is None for row in linked):
                raise RefinerError(f"Risk dimension {risk['dimension']} references an unknown scenario")
            if risk["applicable"]:
                if any(not row["applicable"] or risk["dimension"] not in row["detail"]["risk_dimensions"]
                       for row in linked):
                    raise RefinerError(f"Risk dimension {risk['dimension']} links to an inapplicable or mismatched scenario")
                expected_linked = {row["id"] for row in analysis["scenarios"] if row["applicable"]
                                   and risk["dimension"] in row["detail"]["risk_dimensions"]}
                if set(risk["scenario_ids"]) != expected_linked:
                    raise RefinerError(f"Risk dimension {risk['dimension']} must link every applicable matching scenario")
            elif any(risk["dimension"] in row["detail"]["risk_dimensions"]
                     for row in analysis["scenarios"] if row["applicable"]):
                raise RefinerError(f"Risk dimension {risk['dimension']} is marked inapplicable but appears in a scenario")
        risk_map = {row["dimension"]: row for row in risk_rows}
        for scenario in analysis["scenarios"]:
            if not scenario["applicable"]:
                continue
            for dimension in scenario["detail"]["risk_dimensions"]:
                risk = risk_map[dimension]
                if not risk["applicable"] or scenario["id"] not in risk["scenario_ids"]:
                    raise RefinerError(f"Scenario {scenario['id']} conflicts with risk applicability for {dimension}")
        assumption_ids = [row["id"] for row in analysis["assumptions"]]
        decision_ids = [row["id"] for row in analysis["blocking_questions"]]
        if len(assumption_ids) != len(set(assumption_ids)) or len(decision_ids) != len(set(decision_ids)):
            raise RefinerError("Assumption and blocking-decision IDs must be unique")


class CreationCapacityError(RefinerError):
    """Configured suite capacity cannot support a valid frozen plan."""


def preflight_creation_case_capacity(analysis, max_cases, acceptance_cases):
    """Validate configured minima, then defer actual case counts to the frozen plan.

    Analysis scenarios are coverage intents, not executable cases. Only the
    schema-validated plan and its requirement, criterion, and scenario links
    can establish the number of distinct cases needed by either suite.
    """
    applicable_count = sum(bool(row["applicable"]) for row in analysis["scenarios"])
    development_capacity = int(max_cases)
    acceptance_capacity = int(acceptance_cases)
    minimum = 2
    if development_capacity < minimum or acceptance_capacity < minimum:
        raise CreationCapacityError(
            "Configured development and acceptance case capacities must each fit the "
            f"minimum {minimum}-case plan; "
            f"(development capacity {development_capacity}, acceptance capacity {acceptance_capacity})"
        )
    return {
        "status": "deferred_to_frozen_plan",
        "applicable_scenario_count": applicable_count,
        "case_capacities": {
            "development": development_capacity,
            "acceptance": acceptance_capacity,
        },
        "decision_source": "frozen plan case IDs and their requirement/criterion/scenario links",
    }


CONTROLLER_CAPACITY_OWNER = "Refiner 控制器容量配置"


def route_controller_capacity_questions(analysis):
    """Remove the reserved controller-capacity question from business clarification.

    Capacity is decided from frozen suite case IDs later in the pipeline. This
    reserved owner value routes that question internally; it cannot become a
    user-facing business question or override a frozen-plan capacity diagnosis.
    """
    questions = analysis.get("blocking_questions", [])
    routed = [row for row in questions if row.get("decision_owner") == CONTROLLER_CAPACITY_OWNER]
    if not routed:
        return analysis, []
    result = copy.deepcopy(analysis)
    result["blocking_questions"] = [row for row in questions
                                    if row.get("decision_owner") != CONTROLLER_CAPACITY_OWNER]
    return result, routed


class ClarificationGateError(RefinerError):
    """Stop when a clarification cannot be independently and safely reviewed."""


class ClarificationRevisionGateError(RefinerError):
    """Stop when a proposed clarification correction changes frozen analysis."""


class AnalysisRevisionGateError(RefinerError):
    """Stop when an independent review did not justify a coverage reduction."""


def validate_clarification_review(review, analysis, business_sources):
    """Require complete question coverage and citations into the original inputs."""
    from .artifact_validation import schema_ref, validate_value
    try:
        validate_value(review, schema_ref("skill-refiner.clarification-review"), subject="clarification review")
    except ValueError as exc:
        raise RefinerError(f"Clarification review schema failed: {exc}") from exc
    question_ids = [row["id"] for row in analysis["blocking_questions"]]
    rows = review["question_assessments"]
    reviewed_ids = [row["question_id"] for row in rows]
    if len(reviewed_ids) != len(set(reviewed_ids)) or set(reviewed_ids) != set(question_ids):
        raise RefinerError("Clarification review must assess each blocking question exactly once")
    sources = {row["id"]: row["content"] for row in business_sources}
    required_rows = [row for row in analysis["requirements"] if row["required"]]
    required_ids = {row["id"] for row in required_rows}
    required_source_ids = {source_id for source_id in sources for requirement in required_rows
                           if source_id + ":" in requirement["source"]}
    for row in rows:
        for citation in row["evidence"]:
            if (not any(character.isalnum() for character in citation["quote"])
                    or citation["source_id"] not in sources or citation["quote"] not in sources[citation["source_id"]]):
                raise RefinerError(f"Clarification review for {row['question_id']} cites absent business source text")
        if not row["reason"].strip() or (row["classification"] == "unsupported_extra" and not row["out_of_scope"].strip()):
            raise RefinerError(f"Clarification review for {row['question_id']} lacks a concrete scope basis")
        if row["classification"] == "unsupported_extra":
            if set(row["required_requirement_ids_examined"]) != required_ids:
                raise RefinerError(f"Clarification review for {row['question_id']} did not examine every required requirement")
            if not required_source_ids <= {citation["source_id"] for citation in row["evidence"]}:
                raise RefinerError(f"Clarification review for {row['question_id']} did not cite every required business source")


def validate_clarification_revision(previous, revised, review, *, allow_v3_to_v4=False):
    """Freeze all business elements except reviewed out-of-scope clarifications."""
    assessments = review["question_assessments"]
    extras = {row["question_id"] for row in assessments if row["classification"] == "unsupported_extra"}
    retained = [question for question in previous["blocking_questions"] if question["id"] not in extras]
    if revised.get("blocking_questions") != retained:
        raise RefinerError("Clarification revision changed a required or uncertain question")
    expected_scope = previous["scope"]["out_of_scope"] + [row["out_of_scope"] for row in assessments
                                                           if row["classification"] == "unsupported_extra"]
    if revised.get("scope", {}).get("out_of_scope") != expected_scope:
        raise RefinerError("Clarification revision must append exactly the reviewed exclusions")
    old, new = copy.deepcopy(previous), copy.deepcopy(revised)
    old.pop("blocking_questions", None)
    new.pop("blocking_questions", None)
    old["scope"].pop("out_of_scope", None)
    new["scope"].pop("out_of_scope", None)
    if (allow_v3_to_v4 and old.get('schema_version') == 'skill-refiner.analysis/v3'
            and new.get('schema_version') == 'skill-refiner.analysis/v4'):
        # The v4 standard-obligations inventory is independently schema- and
        # review-gated. Permit only its own IDs in existing link arrays, while
        # comparing the original v3 business links and every other field exactly.
        inventory = new.get('target_obligations', {})
        obligations = inventory.get('obligations', []) if isinstance(inventory, dict) else []
        target = [row for row in obligations if isinstance(row, dict)
                  and row.get('subject') == 'target' and row.get('applicable') is True]
        target_ids = {row.get('id') for row in target}
        target_criteria = {criterion.get('id') for row in target
                           for criterion in row.get('acceptance_criteria', []) if isinstance(criterion, dict)}
        for kind in ('workflow', 'scenarios'):
            for row in new.get(kind, []):
                if isinstance(row, dict) and isinstance(row.get('requirement_ids'), list):
                    row['requirement_ids'] = [value for value in row['requirement_ids'] if value not in target_ids]
                if kind == 'scenarios' and isinstance(row, dict) and isinstance(row.get('acceptance_criteria_ids'), list):
                    row['acceptance_criteria_ids'] = [value for value in row['acceptance_criteria_ids'] if value not in target_criteria]
        old.pop('schema_version')
        new.pop('schema_version')
        new.pop('target_obligations', None)
    if old != new:
        raise RefinerError("Clarification revision changed frozen business analysis or scenarios")


def _latest_resume_clarification(work, inputs, history, sealed, stages, *, verified_boundary=0):
    """Select a newer authenticated pair or fail closed on incomplete evidence."""
    from .analysis_recovery import _call
    from .technical_recovery import clarification_history
    last_full = max((int(row['review_call'].split('-', 1)[0]) for row in history), default=0)
    sealed_review = int(sealed['pair']['review_call'].split('-', 1)[0]) if sealed else 0
    boundary = max(last_full, verified_boundary, max((int(call.split('-', 1)[0]) for call in sealed['calls']), default=0)
                   if sealed else 0)
    calls = work / 'agent-calls'
    later = sorted((p for p in calls.iterdir() if p.is_dir() and p.name.split('-', 1)[0].isdigit()
                    and int(p.name.split('-', 1)[0]) > boundary
                    and p.name.endswith(('-analyze', '-analysis-review'))),
                   key=lambda p: int(p.name.split('-', 1)[0])) if calls.is_dir() else []
    if not later:
        return sealed if sealed_review > last_full else None
    stage = next((row for row in stages.get('stages', []) if row.get('id') == 'analysis'), None)
    if not stage or stage.get('status') not in {'blocked', 'error', 'pending'}:
        raise RefinerError('Latest active clarification lacks incomplete stage evidence')
    pairs, retained = clarification_history(work, inputs, after_call=boundary, source_manifest=stages)
    if not retained or not retained[0].endswith('-analyze'):
        raise RefinerError('Latest active clarification lacks its initial analysis evidence')
    recorded = {row['path']: row['sha256'] for row in stage.get('artifacts', [])}
    request, _ = _call(work, calls / retained[0], 'analyze', recorded)
    payload = request.get('input', {})
    if sealed and sealed_review > last_full:
        if (payload.get('previous_analysis') != sealed['pair']['analysis']
                or payload.get('clarification_review') != sealed['pair']['review']):
            raise RefinerError('Latest clarification does not follow the authenticated prior pair')
        validate_clarification_revision(sealed['pair']['analysis'], pairs[0]['analysis'],
                                        sealed['pair']['review'], allow_v3_to_v4=True)
    elif not history or payload.get('previous_analysis') != history[-1]['analysis']:
        raise RefinerError('Latest clarification does not follow the latest full review')
    if payload.get('previous_analysis_reviews') != [row['review'] for row in history]:
        raise RefinerError('Latest clarification lost its full review history')
    extra_rounds = int(bool(sealed and sealed_review > last_full and any(
        item.get('classification') == 'unsupported_extra' for item in sealed['pair']['review']['question_assessments'])))
    extra_rounds += sum(any(item.get('classification') == 'unsupported_extra'
                            for item in pair['review']['question_assessments']) for pair in pairs)
    if extra_rounds > 1:
        raise RefinerError('Authenticated clarification history exhausted its correction budget')
    return {'pair': pairs[-1], 'calls': retained}


def _changed_paths(before, after, path=''):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            child = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key not in before or key not in after:
                result.append(child)
            else:
                result.extend(_changed_paths(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            child = path + '/' + str(index)
            if index >= len(before) or index >= len(after):
                result.append(child)
            else:
                result.extend(_changed_paths(before[index], after[index], child))
        return result
    return [path or '/'] if before != after else []


def analysis_revision_delta(previous, revised):
    """Expose all changed IDs and identify coverage reductions for independent review."""
    old_requirements = revision_requirements(previous)
    new_requirements = revision_requirements(revised)
    old_scenarios = {row["id"]: row for row in previous["scenarios"]}
    new_scenarios = {row["id"]: row for row in revised["scenarios"]}
    changes = []
    for kind, old, new in (("requirement", old_requirements, new_requirements),
                           ("scenario", old_scenarios, new_scenarios)):
        for key in sorted(old):
            if key in new and old[key] == new[key]:
                continue
            before, after = copy.deepcopy(old[key]), copy.deepcopy(new.get(key))
            changes.append({"kind": kind, "id": key, "before": before, "after": after,
                            "before_sha256": digest(before), "after_sha256": digest(after) if after is not None else None,
                            "changed_paths": _changed_paths(before, after)})
    return {"schema_version": "skill-refiner.analysis-revision-delta/v2", "changes": changes,
            "previous_sha256": digest(previous), "revised_sha256": digest(revised),
            "requirements_added": sorted(new_requirements.keys() - old_requirements.keys()),
            "requirements_removed": sorted(old_requirements.keys() - new_requirements.keys()),
            "requirements_changed": sorted(key for key in old_requirements.keys() & new_requirements.keys()
                                           if old_requirements[key] != new_requirements[key]),
            "required_downgraded": sorted(key for key, row in old_requirements.items() if row["required"]
                                         and (key not in new_requirements or not new_requirements[key]["required"])),
            "scenarios_added": sorted(new_scenarios.keys() - old_scenarios.keys()),
            "scenarios_removed": sorted(old_scenarios.keys() - new_scenarios.keys()),
            "scenarios_changed": sorted(key for key in old_scenarios.keys() & new_scenarios.keys()
                                        if old_scenarios[key] != new_scenarios[key]),
            "applicable_excluded": sorted(key for key, row in old_scenarios.items() if row["applicable"]
                                          and (key not in new_scenarios or not new_scenarios[key]["applicable"]))}


def validate_analysis_revision_review(review, delta, sources=None, revised_analysis=None):
    """Require the independent reviewer to address each lost requirement or scenario."""
    if delta.get('schema_version') == 'skill-refiner.analysis-revision-delta/v2':
        changes = delta['changes']
        assessments = review.get('revision_assessments')
        if not isinstance(assessments, list):
            raise AnalysisRevisionGateError('Independent review must assess each changed requirement and scenario')
        expected = {(row['kind'], row['id']): row for row in changes}
        actual = {(row.get('kind'), row.get('id')): row for row in assessments}
        if len(expected) != len(changes) or len(actual) != len(assessments) or actual.keys() != expected.keys():
            raise AnalysisRevisionGateError('Independent review must assess every changed ID exactly once')
        source_map = {row['id']: row['content'] for row in (sources or [])}
        if not isinstance(revised_analysis, dict):
            raise AnalysisRevisionGateError('Revision review requires the complete revised analysis')
        requirements = effective_requirements(revised_analysis)
        valid_refs = ({('requirement', row['id']) for row in requirements if row['required']}
                      | {('acceptance_criterion', criterion['id'])
                         for row in requirements if row['required']
                         for criterion in row['acceptance_criteria']}
                      | {('scenario', row['id']) for row in revised_analysis['scenarios'] if row['applicable']})
        for key, change in expected.items():
            row = actual[key]
            if (digest(change['before']) != change['before_sha256']
                    or (digest(change['after']) if change['after'] is not None else None) != change['after_sha256']
                    or _changed_paths(change['before'], change['after']) != change['changed_paths']):
                raise AnalysisRevisionGateError(f'Revision delta does not match its before/after content: {key}')
            if (row.get('before_sha256') != change['before_sha256'] or row.get('after_sha256') != change['after_sha256']):
                raise AnalysisRevisionGateError(f'Independent assessment is not bound to the actual change: {key}')
            reason = row.get('reason', '')
            if not isinstance(reason, str) or len(reason.strip()) < 8 or not any(ch.isalnum() for ch in reason):
                raise AnalysisRevisionGateError(f'Independent assessment lacks a concrete reason: {key}')
            evidence = row.get('source_evidence', [])
            if not evidence:
                raise AnalysisRevisionGateError(f'Independent assessment lacks source evidence: {key}')
            for item in evidence:
                quote, source = item.get('quote', ''), source_map.get(item.get('source_id'))
                if (not isinstance(quote, str) or not quote.strip() or not any(ch.isalnum() for ch in quote)
                        or not isinstance(source, str) or quote not in source):
                    raise AnalysisRevisionGateError(f'Independent assessment cites invalid source evidence: {key}')
            refs = row.get('replacement_refs', [])
            keys = [(item.get('kind'), item.get('id')) for item in refs]
            if len(keys) != len(set(keys)) or not set(keys) <= valid_refs:
                raise AnalysisRevisionGateError(f'Independent assessment has invalid replacement coverage: {key}')
            if row.get('outcome') in {'equivalent', 'justified_change'} and not refs:
                raise AnalysisRevisionGateError(f'Independent assessment lacks replacement coverage: {key}')
            if review.get('approved') and row.get('outcome') not in {'equivalent', 'justified_change', 'excluded_by_source'}:
                raise AnalysisRevisionGateError(f'Approved revision has unresolved coverage: {key}')
        return
    reductions = set(delta["required_downgraded"]) | set(delta["scenarios_removed"]) | set(delta["applicable_excluded"])
    if review.get("approved") and reductions:
        basis = review.get("context_scope_assessment", {}).get("basis", "")
        missing = [key for key in sorted(reductions) if not re.search(r'(?<![A-Za-z0-9._-])' + re.escape(key) +
                                                                     r'(?![A-Za-z0-9._-])', basis)]
        if missing:
            raise AnalysisRevisionGateError("Independent review did not justify each coverage reduction: " + ", ".join(missing))


def validate_analysis_review(review, analysis):
    import jsonschema
    version = analysis.get("schema_version")
    review_schema = (ANALYSIS_REVIEW_V4 if version == 'skill-refiner.analysis/v4' else
                     ANALYSIS_REVIEW_V3 if review.get('schema_version') == 'skill-refiner.analysis-review/v3' else
                     ANALYSIS_REVIEW_V2 if version in ("skill-refiner.analysis/v2", "skill-refiner.analysis/v3") else ANALYSIS_REVIEW_V1)
    jsonschema.validate(review, review_schema)
    rows = review["requirement_assessments"]
    expected = {r["id"] for r in effective_requirements(analysis)}
    if len(rows) != len(expected) or {r["requirement_id"] for r in rows} != expected:
        raise RefinerError("Analysis review must assess every requirement exactly once")
    scenarios = review["scenario_assessments"]
    expected_scenarios = {row["id"] for row in analysis["scenarios"]}
    assessed_scenarios = [row["scenario_id"] for row in scenarios]
    if len(assessed_scenarios) != len(set(assessed_scenarios)) or set(assessed_scenarios) != expected_scenarios:
        raise RefinerError("Analysis review must assess every scenario exactly once")
    req_ok = all(all(row[key] for key in ("grounded", "acceptance_criteria_atomic", "criteria_observable"))
                 for row in rows)
    applicability = {row["id"]: row["applicable"] for row in analysis["scenarios"]}
    scenario_ok = all(all(row[key] for key in
                          (("risk_coverage_complete",) if version in ("skill-refiner.analysis/v3", "skill-refiner.analysis/v4")
                           and not applicability[row["scenario_id"]] else
                           ("detail_executable", "risk_coverage_complete", "traceability_complete")))
                      for row in scenarios)
    if review["approved"] and not (req_ok and scenario_ok):
        raise RefinerError("Analysis approval contradicts requirement or scenario completeness assessments")
    validate_obligation_review(review, analysis)
    if version in ("skill-refiner.analysis/v2", "skill-refiner.analysis/v3", "skill-refiner.analysis/v4"):
        context = review["context_scope_assessment"]
        if review["approved"] and not all(context[key] for key in (
                "intent_grounded", "actors_complete", "scope_clear", "constraints_complete")):
            raise RefinerError("Analysis approval contradicts context or scope assessment")
        workflow_rows = review["workflow_assessments"]
        expected_workflows = {row["id"] for row in analysis["workflow"]}
        workflow_ids = [row["workflow_id"] for row in workflow_rows]
        if len(workflow_ids) != len(set(workflow_ids)) or set(workflow_ids) != expected_workflows:
            raise RefinerError("Analysis review must assess every workflow step exactly once")
        workflow_ok = all(all(row[key] for key in (
            "order_and_actors_clear", "inputs_outputs_complete", "completion_and_failure_testable",
            "requirement_traceable")) for row in workflow_rows)
        risk_rows = review["risk_assessments"]
        expected_risks = set(RISK_DIMENSIONS)
        risk_ids = [row["dimension"] for row in risk_rows]
        if len(risk_ids) != len(set(risk_ids)) or set(risk_ids) != expected_risks:
            raise RefinerError("Analysis review must assess every risk dimension exactly once")
        risk_ok = all(row["applicability_justified"] and row["scenario_links_complete"] for row in risk_rows)
        decision_ok = review["dependencies_assessed"] and review["blocking_decisions_actionable"]
        if review["approved"] and not (workflow_ok and risk_ok and decision_ok):
            raise RefinerError("Analysis approval contradicts workflow, risk, dependency or decision assessment")
        if review["approved"] and analysis["blocking_questions"]:
            raise RefinerError("Analysis with unresolved blocking decisions cannot be approved")


def analysis_markdown(analysis):
    lines = ["# Business analysis", "", analysis["goal"]]
    if analysis.get("schema_version") in ("skill-refiner.analysis/v2", "skill-refiner.analysis/v3", "skill-refiner.analysis/v4"):
        context, scope = analysis["context"], analysis["scope"]
        actor_map = {row["id"]: row for row in analysis["actors"]}
        lines += ["", "## User and operating context", "",
                  f"- User intent: {context['user_intent']}", f"- Usage context: {context['usage_context']}",
                  "- Primary users: " + ", ".join(context["primary_user_ids"]), "", "## Scope and constraints", "",
                  "- In scope: " + "; ".join(scope["in_scope"]),
                  "- Out of scope: " + "; ".join(scope["out_of_scope"]),
                  "- Constraints: " + ("; ".join(scope["constraints"]) or "None stated"), "", "## Actors", ""]
        for actor in analysis["actors"]:
            lines.append(f"- {actor['id']} — {actor['role']}: needs={'; '.join(actor['needs'])}; "
                         f"permission boundary={actor['permission_boundary']}")
    lines += ["", "## Requirements", ""]
    for req in analysis["requirements"]:
        lines.append(f"- {req['id']}: {req['behavior']} (source: {req['source']}; required={req['required']})")
        lines.extend(f"  - {criterion['id']}: {criterion['statement']} Evidence: {criterion['evidence']}"
                     for criterion in req["acceptance_criteria"])
    if analysis.get("schema_version") == "skill-refiner.analysis/v4":
        lines += ["", "## 标准义务及覆盖缺口", ""]
        for row in analysis["target_obligations"]["obligations"]:
            lines.append("- " + json.dumps(row, ensure_ascii=False))
        for gap in analysis["target_obligations"]["coverage_gaps"]:
            lines.append("- 覆盖缺口: " + json.dumps(gap, ensure_ascii=False))
    lines += ["", "## Workflow", ""]
    for step in analysis["workflow"]:
        lines += [f"- {step.get('order', '')}: {step['id']}: {step['action']}",
                  f"  Input: {step['inputs']}; output: {step['outputs']}",
                  f"  Completion check: {step['completion_check']}",
                  f"  Failure: {step['failure_behavior']}; requirements: {', '.join(step['requirement_ids'])}"]
        if "actor_ids" in step:
            lines.append(f"  Actors: {', '.join(step['actor_ids'])}; preconditions: {'; '.join(step['preconditions'])}; "
                         f"dependencies: {', '.join(step['dependency_ids']) or 'none'}")
    lines += ["", "## Detailed scenario matrix", ""]
    for scenario in analysis["scenarios"]:
        detail = scenario["detail"]
        if detail is None:
            lines += [f"- {scenario['id']} ({scenario['kind']}; applicable=False): {scenario['situation']}",
                      f"  Requirements: {', '.join(scenario['requirement_ids'])}; criteria: "
                      f"{', '.join(scenario['acceptance_criteria_ids'])}",
                      f"  Basis: {scenario['basis']}", f"  Excluded because: {scenario['exclusion_reason']}"]
            continue
        lines += [f"- {scenario['id']} ({scenario['kind']}; applicable={scenario['applicable']}): {scenario['situation']}",
                  f"  Requirements: {', '.join(scenario['requirement_ids'])}; criteria: "
                  f"{', '.join(scenario['acceptance_criteria_ids'])}",
                  f"  Preconditions: {'; '.join(detail['preconditions'])}",
                  f"  Stimulus: {detail['stimulus']}",
                  f"  Actions: {'; '.join(detail['actions'])}",
                  f"  Expected: {'; '.join(detail['expected_observables'])}",
                  f"  Forbidden: {'; '.join(detail['forbidden_outcomes'])}",
                  f"  Risk dimensions: {', '.join(detail['risk_dimensions'])}; basis: {scenario['basis']}"]
        if not scenario["applicable"]:
            lines.append(f"  Excluded because: {scenario['exclusion_reason']}")
    for title, key in [("Assumptions", "assumptions"), ("External dependencies", "external_dependencies"),
                       ("Blocking questions", "blocking_questions")]:
        lines += ["", "## " + title, ""]
        for item in analysis[key]:
            if isinstance(item, str):
                lines.append("- " + item)
            else:
                lines.append("- " + json.dumps(item, ensure_ascii=False))
    if analysis.get("schema_version") in ("skill-refiner.analysis/v2", "skill-refiner.analysis/v3", "skill-refiner.analysis/v4"):
        lines += ["", "## Risk assessment", ""]
        lines.extend(f"- {row['dimension']}: {'applicable' if row['applicable'] else 'not applicable'}; "
                     f"basis: {row['basis']}; scenarios: {', '.join(row['scenario_ids']) or 'none'}"
                     for row in analysis["risk_assessments"])
    return "\n".join(lines) + "\n"


CHILD_BUDGET_TERMINAL = {"passed", "unchanged", "planned", "error", "cancelled", "planning_error",
                         "plan_not_passed", "execution_error", "not_converged", "no_progress",
                         "failed_acceptance", "verification_failed", "coverage_gap"}


def _budget_number(value, label, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or (integer and type(value) is not int)):
        raise RefinerError(f"Unreconciled child budget: invalid {label}")
    return value


def _read_child_budget(work, *, require_settled=True):
    """Read consumption, never infer process termination from a last heartbeat."""
    from .pipeline import _evaluation_accounting
    root = Path(work).resolve()
    child = root / "refinement"
    empty = {"elapsed_seconds": 0.0, "agent_calls": 0, "target_evaluations": 0,
             "settled": True, "unresolved": []}
    if not child.resolve().is_relative_to(root):
        raise RefinerError("Child refinement checkpoint is outside its creation workspace")
    if not (child / "state.json").is_file():
        if child.is_dir() and any(child.iterdir()):
            raise RefinerError("Child evidence exists without a budget state; consumption is unknown")
        return empty
    try:
        saved = read_json(child / "state.json")
        if not isinstance(saved, dict):
            raise ValueError("child state is not an object")
        elapsed = _budget_number(saved.get("elapsed_seconds", 0), "child elapsed_seconds")
        saved_calls = _budget_number(saved.get("agent_calls", 0), "child agent_calls", integer=True)
        controller = Agent(child)
        calls = max(saved_calls, controller.calls)
        unresolved = []
        if saved_calls > controller.calls:
            unresolved.append("child call counter exceeds its retained call records")
        try:
            controller._assert_no_unsettled_calls()
        except RefinerError as exc:
            unresolved.append(str(exc))
        calls_dir = child / "agent-calls"
        latest_finished_at = 0.0

        def finished_timestamp(value, label):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("missing timezone")
                return parsed.timestamp()
            except (ValueError, TypeError, AttributeError) as exc:
                raise RefinerError(f"Unreconciled terminal timing: {label}") from exc

        if calls_dir.is_dir():
            for directory in calls_dir.iterdir():
                if not directory.is_dir() or not directory.name.split("-", 1)[0].isdigit():
                    continue
                execution_path = directory / "execution.json"
                manifest_path = directory / "request-manifest.json"
                if not execution_path.is_file():
                    manifest_record = read_json(manifest_path) if manifest_path.is_file() else {}
                    if manifest_record.get("status") != "rejected_context_budget":
                        unresolved.append(f"missing terminal call timing: {directory.name}")
                    continue
                execution_record = read_json(execution_path)
                if (execution_record.get("status") not in {"completed", "execution_error", "cancelled", "invalid_response"}
                        or not execution_record.get("finished_at")):
                    unresolved.append(f"no terminal call timing: {directory.name}")
                else:
                    latest_finished_at = max(latest_finished_at, finished_timestamp(
                        execution_record["finished_at"], directory.name))
                seconds = _budget_number(execution_record.get("seconds"), f"{directory.name} seconds")
                if seconds > elapsed:
                    unresolved.append(f"child elapsed counter omits a completed call duration: {directory.name}")
                process_path = directory / "process.execution.json"
                if process_path.is_file():
                    process = read_json(process_path)
                    if (process.get("status") in {"starting", "running", "termination_unconfirmed"}
                            or not process.get("finished_at")):
                        unresolved.append(f"no verified process end timing: {directory.name}")
                    else:
                        latest_finished_at = max(latest_finished_at, finished_timestamp(
                            process["finished_at"], directory.name + " process"))
        accounting = _evaluation_accounting(child, saved)
        unresolved.extend("unsettled evaluation reservation: " + path for path in accounting["unresolved_attempts"])
        for paths in accounting["ledger_paths"].values():
            for relative in paths:
                cost_record = read_json(child / relative)
                if cost_record["status"] != "running":
                    latest_finished_at = max(latest_finished_at, finished_timestamp(cost_record["finished_at"], relative))
        if latest_finished_at > (child / "state.json").stat().st_mtime + 0.001:
            unresolved.append("child elapsed state predates the last completed call/evaluation; actual time requires reconciliation")
        stage_path = child / "stages.json"
        stage = read_json(stage_path) if stage_path.is_file() else {}
        if (saved.get("status") not in CHILD_BUDGET_TERMINAL or stage.get("status") != saved.get("status")
                or stage.get("active_stage") is not None):
            unresolved.append("child orchestrator has no closed terminal state/stage timing checkpoint")
        result = {"elapsed_seconds": elapsed, "agent_calls": calls,
                  "target_evaluations": accounting["total_cost"],
                  "settled": not unresolved, "unresolved": unresolved}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RefinerError(f"Child budget evidence cannot be reconciled: {child}: {exc}") from exc
    if require_settled and unresolved:
        raise RefinerError("Child execution requires verified termination and actual elapsed-time reconciliation; "
                           "a last heartbeat is not its total consumed time: " + "; ".join(unresolved))
    return result


def _record_child_budget_entry(work, state, *, parent_elapsed, parent_calls, child_usage):
    """Freeze the overlap boundary before entering the child workflow."""
    if not child_usage["settled"]:
        raise RefinerError("Cannot enter a child workflow with unresolved execution evidence")
    state["child_budget"] = {
        "schema_version": "skill-refiner.child-budget/v1",
        "workspace": str(Path(work).resolve() / "refinement"),
        "parent_elapsed_at_entry": _budget_number(parent_elapsed, "parent entry elapsed"),
        "child_elapsed_at_entry": child_usage["elapsed_seconds"],
        "parent_agent_calls_at_entry": _budget_number(parent_calls, "parent entry calls", integer=True),
        "child_agent_calls_at_entry": child_usage["agent_calls"],
        "child_evaluations_at_entry": child_usage["target_evaluations"],
        "observed_child_elapsed_seconds": child_usage["elapsed_seconds"],
        "observed_child_agent_calls": child_usage["agent_calls"],
        "observed_child_target_evaluations": child_usage["target_evaluations"],
        "reconciliation_required": False,
    }
    state.update(elapsed_seconds=parent_elapsed, creation_agent_calls=parent_calls,
                 agent_calls=parent_calls + child_usage["agent_calls"],
                 target_evaluations=child_usage["target_evaluations"])
    write_json(Path(work) / "state.json", state)


def _apply_child_budget(work, state, usage, *, parent_elapsed, parent_calls):
    anchor = state.get("child_budget")
    if not anchor:
        if any(usage[key] for key in ("elapsed_seconds", "agent_calls", "target_evaluations")):
            raise RefinerError("Existing child consumption has no parent/child overlap checkpoint; explicit reconciliation is required")
        return state
    if (not isinstance(anchor, dict) or anchor.get("schema_version") != "skill-refiner.child-budget/v1"
            or anchor.get("workspace") != str(Path(work).resolve() / "refinement")):
        raise RefinerError("Parent/child budget checkpoint changed")
    for key in ("parent_elapsed_at_entry", "child_elapsed_at_entry", "observed_child_elapsed_seconds"):
        _budget_number(anchor.get(key), key)
    for key in ("parent_agent_calls_at_entry", "child_agent_calls_at_entry", "child_evaluations_at_entry",
                "observed_child_agent_calls", "observed_child_target_evaluations"):
        _budget_number(anchor.get(key), key, integer=True)
    if (parent_elapsed < anchor["parent_elapsed_at_entry"] or parent_calls < anchor["parent_agent_calls_at_entry"]
            or usage["elapsed_seconds"] < max(anchor["child_elapsed_at_entry"], anchor["observed_child_elapsed_seconds"])
            or usage["agent_calls"] < max(anchor["child_agent_calls_at_entry"], anchor["observed_child_agent_calls"])
            or usage["target_evaluations"] < max(anchor["child_evaluations_at_entry"], anchor["observed_child_target_evaluations"])):
        raise RefinerError("Child budget counters declined below their retained checkpoint")
    elapsed = max(parent_elapsed, state.get("elapsed_seconds", 0))
    if usage["settled"]:
        elapsed = max(elapsed, anchor["parent_elapsed_at_entry"]
                      + usage["elapsed_seconds"] - anchor["child_elapsed_at_entry"])
    anchor.update(observed_child_elapsed_seconds=usage["elapsed_seconds"],
                  observed_child_agent_calls=usage["agent_calls"],
                  observed_child_target_evaluations=usage["target_evaluations"],
                  reconciliation_required=not usage["settled"])
    if usage["unresolved"]:
        anchor["unresolved"] = usage["unresolved"]
    else:
        anchor.pop("unresolved", None)
    state.update(elapsed_seconds=round(elapsed, 3), creation_agent_calls=parent_calls,
                 agent_calls=max(state.get("agent_calls", 0), parent_calls + usage["agent_calls"]),
                 target_evaluations=max(state.get("target_evaluations", 0), usage["target_evaluations"]))
    return state


def _reconcile_child_budget(work, previous_state):
    state = copy.deepcopy(previous_state)
    usage = _read_child_budget(work)
    elapsed = _budget_number(state.get("elapsed_seconds", 0), "parent elapsed_seconds")
    parent_calls = _budget_number(state.get("creation_agent_calls", state.get("agent_calls", 0)),
                                  "parent creation_agent_calls", integer=True)
    if (state.get("agent_calls", 0) > parent_calls + usage["agent_calls"]
            or state.get("target_evaluations", 0) > usage["target_evaluations"]):
        raise RefinerError("Parent usage exceeds retained child evidence; explicit budget reconciliation is required")
    return _apply_child_budget(work, state, usage, parent_elapsed=elapsed, parent_calls=parent_calls), usage


def run(args):
    resume = Path(args.resume_from).resolve() if getattr(args,'resume_from',None) else None
    resume_run = Path(args.resume_run).resolve() if getattr(args, 'resume_run', None) else None
    resume_was_verified = False
    if resume and resume_run:
        raise RefinerError("Use either --resume-from or --resume-run, not both")
    if resume and (resume / "analysis.json").is_file():
        legacy_version = read_json(resume / "analysis.json").get("schema_version")
        if legacy_version in (None, "skill-refiner.analysis/v1", "skill-refiner.analysis/v2", "skill-refiner.analysis/v3"):
            raise RefinerError(
                "Cannot start a new v4 execution from legacy analysis/draft checkpoints: their target standard "
                "obligations were not independently traced. The old evidence is preserved. There is currently "
                "no verified incremental standard-obligation migration entry for these completed stages; "
                "this command cannot claim to resume or reuse them.")
    if not (args.description or args.spec or resume or resume_run):
        raise RefinerError("create needs a business description or --spec")
    if args.name and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise RefinerError("--name must be kebab-case")
    if args.name and len(args.name) > 64:
        raise RefinerError("--name must be at most 64 characters")
    config = load_config(args.config)
    if config and (args.agent_command or args.engine_config or args.engine is not None):
        raise RefinerError("Use --config alone for backend selection")
    if not config and not args.agent_command:
        raise RefinerError("Select a controller backend with --config or --agent-command")
    if not config and not args.engine_config and args.engine is None:
        raise RefinerError("Select an evaluation backend with --config, --engine-config or --engine")
    if resume_run:
        work = resume_run
        if not work.is_dir() or not (work / "state.json").is_file():
            raise RefinerError("--resume-run must name an existing creation workspace")
        if args.output and Path(args.output).resolve() != work:
            raise RefinerError("--output must match --resume-run when continuing a creation workspace")
        standard_paths = []
        destination = None
        inputs = read_json(work / "inputs.json")
        previous_contract = read_json(work / "creation-resume-contract.json")
        previous_state = read_json(work / "state.json")
        resume_was_verified = (previous_state.get("status") == "created"
                               and previous_state.get("verification_status") == "verified")
        if args.description is not None and args.description != inputs.get("business_description"):
            raise RefinerError("Resume business description differs from the frozen creation inputs")
        if args.name and args.name != inputs.get("requested_name"):
            raise RefinerError("Resume name differs from the frozen creation inputs")
        if args.spec:
            supplied = [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.spec]
            if supplied != inputs.get("requirements"):
                raise RefinerError("Resume requirements differ from the frozen creation inputs")
        if args.standard:
            supplied = [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.standard]
            frozen_standards = {row.get("source"): row.get("content")
                                for row in inputs.get("standards", [])[1:] if isinstance(row, dict)}
            if any(frozen_standards.get(row["source"]) != row["content"] for row in supplied):
                raise RefinerError("Resume standards differ from the frozen creation inputs")
        saved_destination = previous_contract.get("destination")
        if args.destination and str(Path(args.destination).resolve()) != saved_destination:
            raise RefinerError("Resume destination differs from the frozen creation contract")
        destination = Path(saved_destination).resolve() if saved_destination else None
    else:
        standard_target = getattr(args, "destination", None) or getattr(args, "output", None)
        standard_paths = discover_project_standards(standard_target, getattr(args, "standard", ()), Path.cwd())
        for reference in args.spec + standard_paths:
            if not Path(reference).is_file():
                raise RefinerError("Reference file does not exist: " + reference)
        work = Path(args.output).resolve() if args.output else Path.cwd() / '.refiner-runs' / (
            'create-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        if work.exists():
            raise RefinerError("Creation output must be a new directory")
        destination = Path(args.destination).resolve() if args.destination else None
        if destination and (destination.exists() or destination.is_relative_to(work) or work.is_relative_to(destination)):
            raise RefinerError("Destination must be new and outside the creation workspace")
        work.mkdir(parents=True)
    started = time.monotonic()
    recovered_child_usage = {"elapsed_seconds": 0.0, "agent_calls": 0, "target_evaluations": 0,
                             "settled": True, "unresolved": []}
    if resume_run:
        previous_state, recovered_child_usage = _reconcile_child_budget(work, previous_state)
    elapsed_before = float(previous_state.get("elapsed_seconds", 0.0)) if resume_run else 0.0
    deadline = started + max(0.0, args.max_seconds - elapsed_before)
    if resume_run:
        state = copy.deepcopy(previous_state)
        state.setdefault("resume_history", []).append({"previous_status": previous_state.get("status"),
                                                        "elapsed_seconds": elapsed_before})
        state.update(status="resuming", elapsed_seconds=elapsed_before)
        for key in ("error", "verification_status", "verification_error"):
            state.pop(key, None)
    else:
        state = {"status": "analyzing", "mode": "create", "workspace": str(work), "questions": []}
    inherited_calls = 0
    if resume_run and (work / "input-migration.json").exists() and not previous_contract.get("input_migration_sha256"):
        raise RefinerError("Input migration provenance is missing from the creation contract")
    if resume_run and previous_contract.get("input_migration_sha256"):
        from .checkpoint_migration import verify_migration
        inherited_calls = verify_migration(work, previous_contract, previous_state)
    if resume_run and (work / "analysis-recovery.json").exists() and not previous_contract.get("analysis_recovery_sha256"):
        raise RefinerError("Analysis recovery provenance is missing from the creation contract")
    if inherited_calls:
        agent = Agent(work, args.agent_command, args.timeout, config=config, inherited_calls=inherited_calls)
    else:
        agent = Agent(work, args.agent_command, args.timeout, config=config)
    if resume_run and previous_state.get("creation_agent_calls", 0) > agent.calls:
        raise RefinerError("Parent controller calls exceed retained call evidence")
    if resume_run:
        # Completed analysis/draft may skip every parent ask(), so the guard must
        # run at the recovery entry before a nested evaluator can be reached.
        agent._assert_no_unsettled_calls()
    agent.max_calls, agent.deadline = args.max_agent_calls - recovered_child_usage["agent_calls"], deadline
    if not resume_run:
        inputs = {"business_description": args.description or "", "requested_name": args.name,
                  "requirements": [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.spec],
                  "standards": [AUTHORING] + [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in standard_paths]}
        if resume:
            historical_inputs = read_json(resume / "inputs.json")
            if "_response_contract_v7" in historical_inputs:
                inputs["_response_contract_v7"] = historical_inputs["_response_contract_v7"]
        else:
            inputs["_response_contract_v7"] = True
    engine_config = read_json(args.engine_config) if args.engine_config else None
    execution = {"backend_config": config, "agent_command": args.agent_command, "engine": args.engine,
                 "engine_config": engine_config, "skill_up": args.skill_up, "timeout": args.timeout,
                 "parallelism": args.parallelism,
                 "agent_parallelism": getattr(args, "agent_parallelism", 2)}
    options = {"max_rounds": args.max_rounds, "planning_attempts": args.planning_attempts,
               "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    limits = {"max_seconds": args.max_seconds, "max_agent_calls": args.max_agent_calls,
              "max_evaluations": args.max_evaluations, "max_rounds": args.max_rounds,
              "planning_attempts": args.planning_attempts, "max_cases": args.max_cases,
              "acceptance_cases": args.acceptance_cases}
    if (resume_run and previous_contract.get("input_migration_sha256")
            and (options != previous_contract.get("options") or limits != previous_contract.get("limits"))):
        raise RefinerError("Migrated checkpoint execution options and limits are frozen; a new explicitly reviewed migration is required")
    state["limits"] = limits
    state["agent_parallelism"] = getattr(args, "agent_parallelism", 2)
    state.setdefault("evaluation_history", [])
    controller_root = Path(__file__).resolve().parent.parent
    controller_sha256 = digest({path.relative_to(controller_root).as_posix():
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(controller_root.rglob("*.py")) if "__pycache__" not in path.parts})
    if resume_run:
        if (previous_contract.get("schema_version") != "skill-refiner.creation-resume/v1"
                or previous_contract.get("controller_sha256") != controller_sha256):
            raise RefinerError("Creation controller or resume contract changed; start a new creation")
        if previous_contract.get("inputs_sha256") != digest(inputs):
            raise RefinerError("Frozen creation inputs changed; start a new creation")
        if previous_contract.get("execution_sha256") != digest(execution):
            raise RefinerError("Resume backend or execution settings differ from the original creation")
        prior_options = previous_contract.get("options", {})
        if (options["max_cases"] != prior_options.get("max_cases")
                or options["acceptance_cases"] != prior_options.get("acceptance_cases")
                or options["planning_attempts"] < prior_options.get("planning_attempts", 1)
                or options["max_rounds"] < prior_options.get("max_rounds", 1)):
            raise RefinerError("Resume may increase planning/round budgets, but must keep frozen case limits")
        prior_limits = previous_contract.get("limits", {})
        if any(limits[name] < prior_limits.get(name, 1) for name in limits):
            raise RefinerError("Resume budgets cannot be lower than the original creation")
        if resume_was_verified:
            from .gate import verify_run
            verify_run(work, deadline=started + max(0, args.max_seconds - elapsed_before))
            return previous_state
        previous_contract["options"], previous_contract["limits"] = options, limits
        write_json(work / "creation-resume-contract.json", previous_contract)
    else:
        previous_contract = {"schema_version": "skill-refiner.creation-resume/v1",
                             "inputs_sha256": digest(inputs), "execution_sha256": digest(execution),
                             "controller_sha256": controller_sha256, "destination": str(destination) if destination else None,
                             "options": options, "limits": limits}
        write_json(work / "creation-resume-contract.json", previous_contract)
    resumed_analysis, resumed_draft, resumed_suite = None, None, None
    if resume:
        previous_inputs = read_json(resume/'inputs.json')
        resumed_analysis = read_json(resume/'analysis.json')
        previous_lock = read_json(resume/'business.lock.json')
        if previous_lock['sha256'] != digest({'inputs':previous_inputs,'analysis':resumed_analysis}) or previous_lock['input_sha256'] != digest(previous_inputs):
            raise RefinerError('Resume business contract hash mismatch')
        if (args.description or args.spec or standard_paths or args.name) and inputs != previous_inputs:
            raise RefinerError('Resume requires identical original business inputs; start a new creation for changed requirements')
        inputs = previous_inputs
        previous_state = read_json(resume/'state.json')
        resumed_draft = Path(previous_state['draft_root'])
        if manifest(resumed_draft) != read_json(resume/'draft-manifest.json'):
            raise RefinerError('Resume first-draft hash mismatch')
        validate_analysis(resumed_analysis, inputs['requested_name'], standards=inputs.get('standards'))
        if resumed_analysis['blocking_questions'] or any(f['severity']=='error' for f in lint(resumed_draft,resumed_analysis['name'])):
            raise RefinerError('Resume requires approved analysis and a statically valid first draft')
        state['creation_provenance'] = {'workspace':str(resume),'business_lock':previous_lock['sha256'],
                                        'prior_creation_agent_calls':previous_state.get('creation_agent_calls',0),
                                        'reuses_verdicts':False}
        if (resume/'refinement/oracle.lock.json').is_file():
            resumed_suite = resume/'refinement'
        review_path = resume / 'analysis-review.json'
        if review_path.is_file():
            shutil.copy2(review_path, work / 'analysis-review.json')
    if not resume_run:
        previous_contract["inputs_sha256"] = digest(inputs)
        write_json(work / "creation-resume-contract.json", previous_contract)
    resume_analysis_complete = False
    resume_analysis_lock = None
    if resume_run and (work / "analysis.json").is_file() and (work / "business.lock.json").is_file():
        import jsonschema
        try:
            resumed_analysis = read_json(work / "analysis.json")
            saved_business_lock = read_json(work / "business.lock.json")
            saved_review = read_json(work / "analysis-review.json")
            validate_analysis(resumed_analysis, inputs["requested_name"], standards=inputs.get('standards'))
            validate_analysis_review(saved_review, resumed_analysis)
            resume_analysis_complete = (
                not resumed_analysis["blocking_questions"] and saved_review["approved"] is True
                and saved_business_lock.get("input_sha256") == digest(inputs)
                and saved_business_lock.get("sha256") == digest({"inputs": inputs, "analysis": resumed_analysis}))
            if resume_analysis_complete:
                resume_analysis_lock = saved_business_lock["sha256"]
        except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError, jsonschema.SchemaError):
            resumed_analysis = None
            resume_analysis_complete = False
    if resume_run and resume_analysis_complete and (work / "draft-manifest.json").is_file():
        expected_draft_manifest = read_json(work / "draft-manifest.json")
        draft_candidates = []
        if previous_state.get("draft_root"):
            draft_candidates.append(Path(previous_state["draft_root"]).resolve())
        draft_candidates.extend(sorted((work / "drafts").glob("*/" + resumed_analysis["name"]), reverse=True)
                                if (work / "drafts").is_dir() else [])
        for candidate_draft in draft_candidates:
            if (candidate_draft.is_relative_to(work) and candidate_draft.is_dir()
                    and manifest(candidate_draft) == expected_draft_manifest
                    and not any(f["severity"] == "error" for f in lint(candidate_draft, resumed_analysis["name"]))):
                resumed_draft = candidate_draft
                break
    rejected_analysis = []
    verified_history = []
    pending_analyses = []
    unvalidated_analyses = []
    if resume_run:
        from .analysis_recovery import rejected_history, verify_recovery
        if previous_contract.get("analysis_recovery_sha256"):
            verified_history = verify_recovery(work, previous_contract, inputs,
                                                pending_analysis=pending_analyses,
                                                invalid_analysis=unvalidated_analyses,
                                                source_manifest=read_json(work / "stages.json"))
            if not resume_analysis_complete:
                rejected_analysis = verified_history
        elif not resume_analysis_complete and (work / "stages.json").is_file():
            rejected_analysis = rejected_history(work, inputs, source_manifest=read_json(work / "stages.json"),
                                                 pending_analysis=pending_analyses,
                                                 invalid_analysis=unvalidated_analyses)
    pending_analysis = pending_analyses[-1] if pending_analyses else None
    pending_analysis_attempt = None
    if pending_analysis is not None:
        matches = []
        for path in work.glob("analysis-attempt-*.json"):
            try:
                if read_json(path) == pending_analysis["analysis"]:
                    suffix = path.stem.rsplit("-", 1)[1]
                    if suffix.isdigit():
                        matches.append(int(suffix))
            except (OSError, ValueError, TypeError):
                continue
        if not matches:
            raise RefinerError("Validated pending analysis has no retained attempt artifact")
        pending_analysis_attempt = max(matches)
    pending_technical = None
    technical_coverage = None
    if resume_run:
        from .artifact_validation import schema_ref, validate_value
        from .technical_recovery import verify_technical_recovery
        technical_record = work / 'technical-recovery.json'
        if technical_record.is_file() != bool(previous_contract.get('technical_recovery_sha256')):
            raise RefinerError('Technical recovery contract and evidence disagree')
        if previous_contract.get('technical_recovery_sha256'):
            pending_technical = verify_technical_recovery(work, previous_contract, inputs)
            if any(row['analysis_call'] == pending_technical['analysis_call'] for row in rejected_analysis):
                # This candidate already received a failed full review: revise it with that feedback.
                pending_technical = None
        gap_file = work / 'technical-coverage.json'
        if gap_file.is_file() != bool(state.get('technical_coverage_sha256')):
            raise RefinerError('Technical coverage state and evidence disagree')
        if gap_file.is_file():
            technical_coverage = read_json(gap_file)
            validate_value(technical_coverage, schema_ref('skill-refiner.technical-coverage'), subject='technical coverage')
            if digest(technical_coverage) != state['technical_coverage_sha256']:
                raise RefinerError('Technical coverage evidence changed')
    latest_clarification_state = None
    latest_required_questions = {}
    if resume_run:
        from .clarification_recovery import verify_clarification_recovery
        history = verified_history or rejected_analysis
        sealed_clarification = verify_clarification_recovery(work, previous_contract, inputs, history)
        technical_boundary = max((int(call.split('-', 1)[0]) for call in read_json(technical_record)['calls']), default=0) \
            if previous_contract.get('technical_recovery_sha256') else 0
        if previous_contract.get('analysis_recovery_sha256'):
            sealed_files = read_json(work / 'analysis-recovery.json')['files']
            sealed_calls = [path.split('/')[1].split('-', 1)[0] for path in sealed_files
                            if path.startswith('agent-calls/') and len(path.split('/')) > 2]
            technical_boundary = max(technical_boundary, *(int(n) for n in sealed_calls if n.isdigit()), 0)
        latest_clarification_state = _latest_resume_clarification(
            work, inputs, history, sealed_clarification, read_json(work / 'stages.json'),
            verified_boundary=technical_boundary)
        if latest_clarification_state and not resume_analysis_complete:
            pair = latest_clarification_state["pair"]
            last_reviewed_analysis = max((int(row["review_call"].split("-", 1)[0])
                                          for row in rejected_analysis), default=0)
            if int(pair["analysis_call"].split("-", 1)[0]) > last_reviewed_analysis:
                # Reuse a fully verified pair without unsupported questions;
                # otherwise the completed pair is the correction's input.
                pending_technical = (pair if not any(
                    row['classification'] == 'unsupported_extra'
                    for row in pair['review']['question_assessments']) else None)
                required = {q["question_id"] for q in pair["review"]["question_assessments"]
                            if q["classification"] != "unsupported_extra"}
                latest_required_questions = {q["id"]: q for q in pair["analysis"]["blocking_questions"]
                                             if q["id"] in required}
    has_stage_manifest = resume_run and (work / "stages.json").is_file()
    stages = StageManifest(work, "create", resume=bool(has_stage_manifest))
    print(json.dumps({"status": "started", "workspace": str(work),
                      "work_plan": str(work / "work-plan.md"),
                      "state_file": str(work / "state.json"),
                       "stage_manifest": str(work / "stages.json"), "limits": limits,
                      "max_rounds": args.max_rounds}, ensure_ascii=False), flush=True)
    materialize_inputs = not stages.is_completed("inputs")
    if materialize_inputs:
        write_json(work / "inputs.json", inputs)
        (work / 'inputs').mkdir(exist_ok=True)
    request_file = work / 'inputs/business-request.md'
    if materialize_inputs:
        request_file.write_text(inputs['business_description'] + '\n', encoding='utf-8')
    spec_paths = []
    for index, requirement in enumerate(inputs['requirements']):
        path = work / 'inputs' / f'requirement-{index+1}.md'
        if materialize_inputs:
            path.write_text(requirement['content'], encoding='utf-8')
        spec_paths.append(str(path))
    standard_paths = []
    for index, standard in enumerate(inputs['standards'][1:]):
        path = work / 'inputs' / f'standard-{index+1}.md'
        if materialize_inputs:
            path.write_text(standard['content'], encoding='utf-8')
        standard_paths.append(str(path))
    if materialize_inputs:
        stages.complete("inputs")
    input_hash = digest(inputs)
    business_sources = ([{'id': 'business_description', 'content': inputs['business_description']}] if inputs['business_description'] else []) + [
        {'id': f'spec-{index+1}', 'source': row['source'], 'content': row['content']}
        for index, row in enumerate(inputs['requirements'])]
    frozen_standard_sources = standard_sources(inputs.get('standards', []))
    review_sources = business_sources + frozen_standard_sources
    refinement = None
    child_usage = copy.deepcopy(recovered_child_usage)
    analysis = resumed_analysis
    locked = resume_analysis_lock
    agent.bind_state(state, elapsed=lambda: elapsed_before + time.monotonic() - started,
                     extra_calls=lambda: child_usage["agent_calls"])

    def observe_child_budget():
        if not state.get("child_budget") and not (work / "refinement" / "state.json").is_file():
            return
        try:
            observed = _read_child_budget(work, require_settled=False)
            _apply_child_budget(work, state, observed,
                                parent_elapsed=elapsed_before + time.monotonic() - started,
                                parent_calls=agent.calls)
            child_usage.update(observed)
            state.pop("child_budget_error", None)
        except RefinerError as exc:
            state["child_budget_error"] = str(exc)
            if isinstance(state.get("child_budget"), dict):
                state["child_budget"]["reconciliation_required"] = True

    def runtime_preflight(phase, analysis_value=None):
        reused = []
        if analysis_value is not None:
            reused += ["analyze", "analysis-review"]
        if resumed_draft:
            reused.append("draft")
        child_checkpoint = work / "refinement" / "resume-contract.json"
        if (resume_run and child_checkpoint.is_file()) or resumed_suite:
            # The child validates its own oracle/caches before determining its
            # remaining path. Do not charge a second fresh planning path here.
            print(json.dumps({"stage": "runtime_preflight", "status": "deferred",
                              "phase": phase, "reason": "validated_by_refinement_child_before_execution"}), flush=True)
            return
        report = build_runtime_preflight(
            mode="create", phase=phase, limits=limits,
            usage={"agent_calls": agent.calls,
                   "elapsed_seconds": elapsed_before + time.monotonic() - started,
                   "target_evaluations": state.get("target_evaluations", 0)},
            config=config, validated_reused_steps=reused,
            executor_count=len(config["executors"]) if config else 1,
            plan_only=bool(getattr(args, "plan_only", False)),
            launch_context=getattr(args, "launch_context", None))
        emit_runtime_preflight(report, work, f"runtime-preflight-{phase}.json")

    def event(status, **extra):
        observe_child_budget()
        if status in CREATE_EVENTS:
            stage_id = CREATE_EVENTS[status]
            if not (resume_run and stages.is_completed(stage_id)):
                stages.activate(stage_id)
        state.update(status=status, elapsed_seconds=max(state.get("elapsed_seconds", 0), round(elapsed_before + time.monotonic() - started, 3)),
                     creation_agent_calls=agent.calls, agent_calls=max(state.get("agent_calls", 0), agent.calls + child_usage["agent_calls"]),
                     cached_agent_calls=agent.cached_calls,
                     target_evaluations=max(state.get("target_evaluations", 0), child_usage["target_evaluations"]), **extra)
        write_json(work / 'state.json', state)
        print(json.dumps({'status': status, 'elapsed_seconds': state['elapsed_seconds'],
                          'max_seconds': args.max_seconds, **extra}, ensure_ascii=False), flush=True)

    def integrity():
        if digest(read_json(work / 'inputs.json')) != input_hash or request_file.read_text(encoding="utf-8") != inputs['business_description'] + '\n':
            raise RefinerError('Frozen business inputs changed')
        if locked and digest({'inputs': inputs, 'analysis': read_json(work / 'analysis.json')}) != locked:
            raise RefinerError('Frozen business contract changed')
        if time.monotonic() >= deadline or elapsed_before + time.monotonic() - started > args.max_seconds:
            raise RefinerError('Creation wall-clock budget exhausted')

    event('resuming' if resume or resume_run else 'analyzing')
    try:
        runtime_preflight("before-analysis", resumed_analysis if resume_analysis_complete or resume else None)
        feedback = [issue for row in rejected_analysis for issue in row['review'].get('issues', [])]
        previous_analysis = rejected_analysis[-1]['analysis'] if rejected_analysis else None
        if pending_analysis is None and unvalidated_analyses:
            latest_invalid = unvalidated_analyses[-1]
            previous_analysis = latest_invalid['analysis']
            feedback.append('Resume the latest completed analysis from ' + latest_invalid['analysis_call'] +
                            ' as a targeted correction input; its controller validation failed: ' +
                            latest_invalid['validation_error'])
        previous_reviews = [row['review'] for row in rejected_analysis]
        if latest_clarification_state:
            feedback += ['Latest pending clarification (' + row['question_id'] + ', ' + row['classification'] + '): ' +
                         row['reason'] for row in latest_clarification_state['pair']['review']['question_assessments']]
        legacy_technical = (pending_technical if pending_technical and
                            pending_technical['analysis'].get('schema_version') != 'skill-refiner.analysis/v4' else None)
        legacy_required_questions = {}
        if legacy_technical:
            required_question_ids = {row['question_id'] for row in legacy_technical['review']['question_assessments']
                                     if row['classification'] != 'unsupported_extra'}
            legacy_required_questions = {q['id']: q for q in legacy_technical['analysis']['blocking_questions']
                                         if q['id'] in required_question_ids}
            previous_analysis = previous_analysis or legacy_technical['analysis']
            feedback += ['Replan the retained legacy technical analysis under analysis/v4. Preserve every original '
                         'required/uncertain blocking question verbatim until independently routed to a retained '
                         'technical gap. Keep all historical reviews and consumption; add separately sourced standard '
                         'obligations without inventing spec citations or claiming old standards evidence is complete.']
        rejected_fingerprints = {digest(row['analysis']) for row in rejected_analysis}
        existing_attempts = [int(p.stem.rsplit('-', 1)[1]) for p in work.glob('analysis-attempt-*.json')
                             if p.stem.rsplit('-', 1)[1].isdigit()]
        attempt_offset = max([len(rejected_analysis), *existing_attempts], default=0)
        if legacy_technical and not technical_coverage:
            # Preserve the legacy source chain's actual technical derivation before
            # asking for a new-format analysis. It is evidence of a gap, never a
            # completed v4 analysis or permission to start target tests.
            minimum_calls = 8 if getattr(args, 'plan_only', False) else 9
            if args.max_agent_calls - agent.calls < minimum_calls:
                raise RefinerError('Legacy technical upgrade needs one independent triage plus the new v4 '
                                   f'workflow minimum ({minimum_calls} remaining calls); budget is not expanded')
            from .technical_triage import route_technical_questions
            triage_schema = read_json(Path(__file__).resolve().parents[2] / 'schemas/v1/technical-triage.schema.json')
            triage = agent.ask('analysis-review',
                'Independently classify the retained legacy blocking questions before schema upgrade. '
                'technical_evidence only concerns actual observation capability for already required behavior. '
                'Never downgrade a business decision; uncertain stays unresolved. Cite the existing required '
                'requirement and applicable scenario IDs. This only retains a coverage gap, not proof.',
                {'business_sources': business_sources, 'analysis': legacy_technical['analysis'],
                 'clarification_review': legacy_technical['review']}, triage_schema)
            triage_path = work / f'technical-triage-{attempt_offset + 1}.json'
            if triage_path.exists():
                raise RefinerError('Legacy triage evidence exists without settled coverage; reconcile before retry')
            write_json(triage_path, triage)
            derived, retained_gap = route_technical_questions(
                legacy_technical['analysis'], triage, legacy_technical['review'])
            if retained_gap:
                technical_coverage = retained_gap
                write_json(work / f'analysis-derived-technical-{attempt_offset + 1}.json', derived)
                write_json(work / 'technical-coverage.json', technical_coverage)
                state['technical_coverage_sha256'] = digest(technical_coverage)
                write_json(work / 'state.json', state)
                previous_analysis = previous_analysis or derived
                legacy_required_questions = {q['id']: q for q in derived['blocking_questions']
                                             if q['id'] in required_question_ids}
        elif legacy_technical and technical_coverage:
            from .technical_triage import route_technical_questions
            matches = []
            for path in sorted(work.glob('technical-triage-*.json')):
                try:
                    derived, gap = route_technical_questions(legacy_technical['analysis'], read_json(path),
                                                            legacy_technical['review'])
                except RefinerError:
                    continue
                if gap == technical_coverage:
                    matches.append(derived)
            if len(matches) != 1:
                raise RefinerError('Legacy coverage needs exactly one verified triage before v4 upgrade')
            legacy_required_questions = {q['id']: q for q in matches[0]['blocking_questions']
                                         if q['id'] in required_question_ids}
        # An independently rejected analysis and an independently identified
        # unsupported clarification are different correction paths. Each is
        # bounded once; neither spends the other's correction opportunity.
        analysis_revisions = 0
        clarification_revisions = 0
        clarification_correction = None
        if latest_clarification_state:
            pair = latest_clarification_state['pair']
            if any(row['classification'] == 'unsupported_extra'
                   for row in pair['review']['question_assessments']):
                # The recovered pair has already spent the clarification review
                # call. Continue with its narrowly constrained correction rather
                # than regenerating or re-reviewing the completed draft.
                clarification_correction = (copy.deepcopy(pair['analysis']), copy.deepcopy(pair['review']))
                clarification_revisions = 1
        for attempt in range(0 if resume or resume_analysis_complete else 3):
            review = None
            revision_delta = None
            reuse_technical = bool(pending_technical and not legacy_technical and attempt == 0)
            reuse_pending_analysis = bool(pending_analysis and attempt == 0 and not reuse_technical)
            analysis = (copy.deepcopy(pending_technical['analysis']) if reuse_technical else
                        copy.deepcopy(pending_analysis['analysis']) if reuse_pending_analysis else agent.ask('analyze',
                'Analyze the supplied business request into a scoped executable Agent Skill. Return schema_version '
                'skill-refiner.analysis/v4 and satisfy every field in the supplied response_schema. Choose a concise kebab-case name '
                'unless requested_name is supplied. Extract all business requirements with stable IDs and concrete source '
                'citations into business_sources. Every requirement.source must begin with its source ID plus a colon '
                '(for example spec-1: followed by the supporting passage). Include only business requirements from '
                'these sources. For every workflow step, include a concrete completion_check that names observable '
                'evidence proving the step is complete; do not use generic wording such as "done". Authoring standards, trigger description quality, script organization and controller '
                'integrity are separately sourced standard obligations; do not add them as business requirements unless the user '
                'explicitly requested that as an externally observable business capability. Distinguish target-owned '
                'runtime schemas, validators and bounded self-review required by supplied authoring standards from '
                'controller-private formats. Fill target_obligations: account for every frozen_standard_sources entry '
                'with its exact supplied content_sha256. Give each obligation an O- ID, exact source quote and hash, '
                'target/controller subject and source-based applicability rationale. Applicable target obligations need '
                'atomic acceptance criteria, global scenario/workflow links, a concrete planned implementation interface '
                'and independent evidence strategy. The global workflow/scenario requirement_ids may reference business '
                'R IDs or applicable target O IDs. Keep controller-only and excluded obligations out of target tests. '
                'Do not hide standard obligations in scope prose. For an unavailable actual implementation/observation '
                'mechanism preserve a concrete coverage_gap linked in mechanism.gap_ids and state blocked. A planned '
                'mechanism is not verified; ordinary future behavior tests are pending, not proof or an automatic gap. '
                'Schema/validator validation, failure paths, bounded self-review correction/stop and dependency failures '
                'must be testable when required by standards, without changing the public business I/O. Preserve standards without '
                'inventing business rules. Never assert an internal record, candidate injection or verification capability '
                'exists without evidence; record unverified mechanisms and coverage gaps for actual investigation. '
                'Split each material business '
                'requirement into small, independently observable acceptance_criteria; each criterion has a stable ID, one '
                'outcome and evidence that could prove it. Do not combine separate decisions, outputs, permissions or side '
                'effects into one criterion. Analyze the full workflow: actors, input variants, preconditions, state transitions, '
                'decisions, outputs, side effects, dependencies, permissions, failure behavior, retries, interruption and '
                'recovery where they apply. Build a scenario inventory with stable IDs; use multiple scenarios per category '
                'when distinct requirements or risk conditions need different evidence. Assess normal, boundary and error '
                'categories, and give a concrete scope reason for exclusions. Each applicable scenario states preconditions, '
                'a concrete stimulus, ordered actions, observable expected outcomes, forbidden outcomes, risk dimensions, '
                'linked requirement IDs and acceptance criterion IDs, and a source-based basis. Every required criterion must '
                'appear in at least one applicable scenario. Distinguish scenarios that exercise different actors, states, '
                'inputs, dependencies, side effects or recovery paths; paraphrases and cosmetic data changes do not add coverage. '
                'These are detailed scenario intents, not frozen executable tests or implementation. '
                'Do not add arbitrary business rules, integrations or approval policies. Explicitly record reasonable '
                'When previous_clarification_history is supplied, retain its complete latest analysis, per-question '
                'feedback and unresolved questions. Its business-only unsupported_extra classification does not waive '
                'independently sourced target standard obligations. Keep the original technical_coverage unresolved. '
                'nonmaterial implementation assumptions; missing material business rules for requested or reasonably implied '
                'behavior belong in blocking_questions. Never make an unrequested speculative capability a blocking '
                'question. Explain a well-grounded exclusion in scope.out_of_scope. Do not dismiss real overlaps or '
                'combinations required by the sources merely because the source does not use the same keywords; '
                'when scope or required behavior is genuinely uncertain, retain the question. Do not invent a business '
                'policy to avoid clarification. '
                'Each blocking question must be concise and answerable, state the material choice needed, and avoid '
                'requesting credentials or asking the user to diagnose an operational failure that can be checked. '
                'Fill context.user_intent and usage_context; list primary users in actors and context.primary_user_ids. '
                'Make scope.in_scope, scope.out_of_scope and scope.constraints explicit; if there is no excluded feature, '
                'say so explicitly in out_of_scope. Give workflow steps contiguous order values, actor_ids, preconditions '
                'and dependency_ids. Dependency IDs may refer to earlier workflow steps or declared real external dependencies; '
                'never use self/forward step references or invent an external dependency. '
                'Assess every supported risk dimension exactly once with a source-based applicability '
                'decision, rationale and scenario links. Structure assumptions, external_dependencies and blocking_questions '
                'according to the schema; use [] only when none exist. Do not claim an external dependency is verified or replace real services with fabricated success. '
                 'Keep dependencies to the user scope. A category is inapplicable only with a concrete scope-based reason; '
                 'normal behavior is always applicable. The analysis scenario inventory is not the executable case count: '
                 'do not infer a one-to-one case count, ask the user to expand capacity, or stop analysis from scenario count. '
                 'Preserve all applicable scenarios and their requirement/criterion links. The frozen development and '
                 'acceptance plans determine their actual distinct case counts and must link each case to the scenarios '
                 'and criteria it meaningfully exercises under the active plan schema. The planning gate checks those '
                 'links and each suite limit before target evaluation; if the complete valid plan exceeds capacity, block '
                 'there with actual and allowed counts. Never omit or falsely exclude a real scenario to fit capacity. '
                 'For an excluded scenario without relevant requirements, use empty '
                'requirement_ids and acceptance_criteria_ids, detail null, and a concrete exclusion_reason; do not invent '
                'test steps or requirement links. Do not implement code or provide test answers at this stage.',
                {**inputs, 'business_sources': business_sources, 'frozen_standard_sources': frozen_standard_sources,
                 'validation_feedback': feedback,
                 **({'previous_technical_clarification': legacy_technical} if legacy_technical else {}),
                 **({'technical_coverage': technical_coverage} if technical_coverage else {}),
                 **({'previous_clarification_history': latest_clarification_state['pair'],
                     'retained_clarification_questions': list(latest_required_questions.values())}
                    if latest_clarification_state else {}),
                 **({'retained_legacy_questions': list(legacy_required_questions.values())} if legacy_technical else {}),
                 'test_case_capacity': {'development': args.max_cases, 'acceptance': args.acceptance_cases},
                 **({'previous_analysis_reviews': previous_reviews} if previous_reviews else {}),
                 **({'previous_analysis': clarification_correction[0],
                     'clarification_review': clarification_correction[1]} if clarification_correction else
                    {'previous_analysis': previous_analysis}
                     if previous_analysis is not None else {})}, ANALYSIS))
            attempt_number = (pending_analysis_attempt if reuse_pending_analysis else
                              attempt_offset + attempt + 1)
            attempt_path = work / f'analysis-attempt-{attempt_number}.json'
            if attempt_path.exists():
                if not reuse_pending_analysis or read_json(attempt_path) != analysis:
                    raise RefinerError('Analysis attempt evidence already exists; refusing to overwrite it')
            else:
                write_json(attempt_path, analysis)
            try:
                validate_analysis(analysis, args.name, {s['id'] for s in business_sources},
                                  standards=inputs.get('standards'))
                analysis, routed_capacity_questions = route_controller_capacity_questions(analysis)
                if routed_capacity_questions:
                    route_path = work / f'controller-capacity-routing-attempt-{attempt_number}.json'
                    if route_path.exists():
                        raise RefinerError('Controller capacity routing evidence already exists; refusing to overwrite it')
                    write_json(route_path, {
                        'schema_version': 'skill-refiner.controller-capacity-routing/v1',
                        'attempt': attempt_number,
                        'routed_questions': routed_capacity_questions,
                        'decision': 'defer_to_frozen_plan_case_ids_and_coverage_links',
                        'user_clarification_requested': False,
                    })
                if legacy_technical:
                    current_questions = {q['id']: q for q in analysis['blocking_questions']}
                    if any(current_questions.get(qid) != q for qid, q in legacy_required_questions.items()):
                        raise RefinerError('Legacy required technical/business questions must be retained until '
                                           'independently reviewed and routed with explicit coverage evidence')
                if latest_required_questions:
                    current_questions = {q['id']: q for q in analysis['blocking_questions']}
                    if any(current_questions.get(qid) != q for qid, q in latest_required_questions.items()):
                        raise RefinerError('Latest required/uncertain clarification questions must be retained; '
                                           'an older technical gap does not resolve them')
                if digest(analysis) in rejected_fingerprints:
                    raise RefinerError('Analysis revision made no progress: this analysis was independently rejected before')
                if clarification_correction:
                    try:
                        validate_clarification_revision(
                            clarification_correction[0], analysis, clarification_correction[1],
                            allow_v3_to_v4=bool(latest_clarification_state))
                    except RefinerError as exc:
                        raise ClarificationRevisionGateError(str(exc)) from exc
                    clarification_correction = None
                if analysis['blocking_questions']:
                    clarification_schema = read_json(Path(__file__).resolve().parents[2] /
                                                     'schemas/v1/clarification-review.schema.json')
                    try:
                        clarification_review = copy.deepcopy(pending_technical['review']) if reuse_technical else agent.ask('analysis-review',
                        'Independently assess each proposed blocking question against the original business sources. '
                        'Classify it as required_in_scope, unsupported_extra, or uncertain. Return each question exactly '
                        'once, and cite exact original source excerpts with their supplied source IDs for required and '
                        'unsupported classifications. A required behavior may be implicit in an explicitly covered '
                        'workflow or a combination/overlap of its rules; never treat missing keywords alone as proof '
                        'of exclusion. Evaluate the combination risk for every question. Distinguish actual overlapping '
                        'conditions affecting one requested behavior from an unrequested feature to aggregate unrelated '
                        'issues. Mark unsupported_extra only when the source-based scope supports that exclusion and no '
                        'requested or reasonably implied behavior depends on it; otherwise use uncertain. For an '
                        'unsupported extra list every required requirement ID examined and cite each original source '
                        'supporting those requirements, in addition to a concrete out_of_scope statement; do not invent a business policy. '
                        'Required or uncertain questions remain blocking. Return the complete supplied JSON schema.',
                            {'business_sources': business_sources, 'analysis': analysis}, clarification_schema)
                        write_json(work / f'clarification-review-attempt-{attempt_number}.json', clarification_review)
                        validate_clarification_review(clarification_review, analysis, business_sources)
                    except Cancelled:
                        raise
                    except RefinerError as exc:
                        raise ClarificationGateError(f'Clarification review could not be verified: {exc}') from exc
                    extras = [row for row in clarification_review['question_assessments']
                              if row['classification'] == 'unsupported_extra']
                    if extras:
                        if clarification_revisions:
                            raise ClarificationGateError('Clarification review did not converge within the analysis revision budget')
                        clarification_revisions += 1
                        clarification_correction = (copy.deepcopy(analysis), copy.deepcopy(clarification_review))
                        feedback = feedback + ['Remove only the independently reviewed unsupported questions and append exactly '
                                               'their out_of_scope statements. Preserve every other analysis field, scenario, '
                                               'requirement and required or uncertain question verbatim; return the full analysis.']
                        continue
                    from .technical_triage import route_technical_questions
                    triage_schema = read_json(Path(__file__).resolve().parents[2] / 'schemas/v1/technical-triage.schema.json')
                    triage = agent.ask('analysis-review',
                        'Independently classify each blocking question by who can resolve it. technical_evidence '
                        'is strictly for verifying controller or test observation capability for behavior already '
                        'required by the supplied business sources; reference the actual required requirement and '
                        'applicable scenario IDs. Never classify a material business rule or user decision as '
                        'technical. If the distinction is uncertain use uncertain. A technical classification '
                        'does not prove a capability or waive any requirement.',
                        {'business_sources': business_sources, 'analysis': analysis,
                         'clarification_review': clarification_review}, triage_schema)
                    write_json(work / f'technical-triage-{attempt_number}.json', triage)
                    derived, new_gap = route_technical_questions(analysis, triage, clarification_review)
                    if new_gap:
                        if technical_coverage and technical_coverage != new_gap:
                            raise RefinerError('Multiple different technical coverage records require explicit reconciliation')
                        technical_coverage = new_gap
                        write_json(work / f'analysis-derived-technical-{attempt_number}.json', derived)
                        write_json(work / 'technical-coverage.json', technical_coverage)
                        state['technical_coverage_sha256'] = digest(technical_coverage)
                        write_json(work / 'state.json', state)
                        analysis = derived
                # Unsupported questions have been reviewed and, if needed,
                # corrected in the bounded pass above. Technical questions have
                # now been converted to explicit controller coverage gaps. Case
                # capacity is also a controller limit and is checked before any
                # material business question is surfaced to the user.
                preflight_creation_case_capacity(analysis, args.max_cases, args.acceptance_cases)
                if analysis['blocking_questions']:
                    clarification_by_id = {
                        row['question_id']: row['classification']
                        for row in clarification_review['question_assessments']
                    }
                    triage_by_id = {row['question_id']: row['domain'] for row in triage['assessments']}
                    business_questions = [
                        row for row in analysis['blocking_questions']
                        if clarification_by_id.get(row['id']) == 'required_in_scope'
                        and triage_by_id.get(row['id']) == 'business_decision'
                    ]
                    unresolved = [row for row in analysis['blocking_questions']
                                  if row['id'] not in {question['id'] for question in business_questions}]
                    if unresolved:
                        details = [
                            {'question_id': row['id'],
                             'clarification_classification': clarification_by_id.get(row['id']),
                             'technical_domain': triage_by_id.get(row['id'])}
                            for row in unresolved
                        ]
                        raise ClarificationGateError(
                            'Remaining blocking questions were not independently established as material '
                            'in-scope business decisions; no user clarification was requested: '
                            + json.dumps(details, ensure_ascii=False)
                        )
                    write_json(work / 'analysis.json', analysis)
                    event('needs_clarification', questions=[row['question'] for row in business_questions])
                    return state
                if previous_analysis is not None:
                    from .artifact_validation import schema_ref, validate_value
                    revision_delta = analysis_revision_delta(previous_analysis, analysis)
                    validate_value(revision_delta, schema_ref('skill-refiner.analysis-revision-delta-v2'),
                                   subject='analysis revision delta')
                    delta_path = work / f'analysis-revision-delta-{attempt_number}.json'
                    if delta_path.exists():
                        raise RefinerError('Analysis revision delta already exists; refusing to overwrite it')
                    write_json(delta_path, revision_delta)
                review = None
                review = agent.ask('analysis-review',
                    ('Return schema_version skill-refiner.analysis-review/v4. Fill revision_assessments for every '
                     'delta change, or [] if none. Assess every frozen standard source for completeness and correct '
                     'subject boundary and every O obligation for exact source/hash, applicability, atomic criteria, '
                     'concrete mechanisms/gaps and scenario/workflow coverage. Do not treat controller-only policy as '
                     'target business behavior. Requirement assessments cover the supplied effective_contract (R+target O). '
                     if analysis.get('schema_version') == 'skill-refiner.analysis/v4' else
                     'Return schema_version skill-refiner.analysis-review/v3 and assess every change separately. '
                     if revision_delta else 'Return schema_version skill-refiner.analysis-review/v2. ') +
                    'Independently review every required analysis '
                    'element against the original request and supplied standards. Fill context_scope_assessment, one '
                    'workflow_assessment per step, one risk_assessment per supported dimension, dependencies_assessed '
                    'and blocking_decisions_actionable from evidence. '
                    'Check every requested behavior is retained, requirements are decomposed into atomic acceptance '
                    'criteria with independently observable evidence, and every criterion is grounded; cite the '
                    'specific request or explicit reasonable implementation assumption for every assessment. Assess every '
                    'scenario ID exactly once. For an excluded scenario, review its scope justification; executable detail '
                    'and requirement links can be absent. Reject applicable scenarios lacking preconditions, a concrete stimulus, ordered actions, '
                    'observable expected outcomes, forbidden outcomes, risk dimensions or requirement/criterion traceability. '
                    'Confirm every required criterion is covered by an applicable scenario; inspect workflow-state, permissions, '
                    'dependencies, cross-requirement effects, side effects, retries/idempotency and recovery when they apply. '
                    'Reject duplicate scenarios that only paraphrase the same situation and evidence. An excluded scenario '
                    'needs a concrete scope-based explanation. Check '
                    'that each workflow step has a concrete observable completion_check, and that its inputs, outputs, '
                    'failure behavior and requirement links are clear. Reject '
                    'invented material business rules, omitted workflows, fabricated dependencies, arbitrary error '
                    'contracts and unjustified exclusions of normal/boundary/error scenarios. Do not require features '
                    'the user excluded. Runtime capacity and validation feedback are controller context, not business '
                    'requirements; recognize their provenance and do not claim they were absent from the supplied context. '
                    'Distinguish target-owned standard obligations from controller-private formats and verify their '
                    'actual observable mechanisms; do not invent business source IDs for authoring standards. '
                    'For a revision, inspect each before/after item and changed path, including unchanged IDs with changed '
                    'business meaning, criteria, risk, evidence or scenarios. In revision_assessments return exactly one '
                    'entry per delta change with its exact hashes, classify the outcome, identify the concrete revised '
                    'requirement, acceptance criterion or applicable scenario that preserves coverage in replacement_refs, '
                    'and explain how it does so. A correction removing an unsupported item may use excluded_by_source '
                    'only with an exact source-based exclusion reason. Cite exact excerpts from review_sources with source IDs. '
                    'Mark missing coverage or uncertainty as such and do not approve it. Equivalent wording may be approved; '
                    'do not accept reduction merely to fit case capacity. Standards-owned obligations are not business '
                    'source requirements; assess their real ownership without inventing a spec citation. '
                    'A technical coverage gap is unresolved controller evidence, not proof; retain all business requirements. '
                    'Approve only a clear, scoped contract suitable for independent test planning.',
                    {'inputs': inputs, 'analysis': analysis, 'technical_coverage': technical_coverage,
                     **({'previous_clarification_history': latest_clarification_state['pair']}
                        if latest_clarification_state else {}),
                     'review_sources': review_sources, 'effective_contract': effective_requirements(analysis),
                     'runtime_context': {'test_case_capacity': {'development': args.max_cases, 'acceptance': args.acceptance_cases},
                                         'validation_feedback': feedback},
                     **({'previous_analysis': previous_analysis, 'previous_analysis_reviews': previous_reviews,
                         'revision_delta': revision_delta} if revision_delta else {})},
                    ANALYSIS_REVIEW_V4 if analysis.get('schema_version') == 'skill-refiner.analysis/v4' else
                    ANALYSIS_REVIEW_V3 if revision_delta else ANALYSIS_REVIEW)
                review_path = work / f'analysis-review-attempt-{attempt_number}.json'
                if review_path.exists():
                    raise RefinerError('Analysis review evidence already exists; refusing to overwrite it')
                write_json(review_path, review)
                validate_analysis_review(review, analysis)
                if revision_delta:
                    validate_analysis_revision_review(review, revision_delta, review_sources, analysis)
                if not review['approved']:
                    raise RefinerError('; '.join(review['issues']))
                canonical_review = work / 'analysis-review.json'
                if canonical_review.is_file():
                    snapshot_path = work / 'analysis-previous' / ('analysis-review-' + hashlib.sha256(canonical_review.read_bytes()).hexdigest() + '.json')
                    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                    if not snapshot_path.exists():
                        shutil.copy2(canonical_review, snapshot_path)
                write_json(canonical_review, review)
            except Cancelled:
                raise
            except RefinerError as exc:
                if isinstance(exc, (ClarificationGateError, ClarificationRevisionGateError,
                                    AnalysisRevisionGateError, CreationCapacityError)) or 'made no progress' in str(exc):
                    raise
                # Analysis-structure repair and clarification repair are separate
                # budgets. A completed clarification correction must not consume
                # the independent analysis correction opportunity.
                if analysis_revisions:
                    raise
                analysis_revisions += 1
                feedback = feedback + [str(exc)]
                if 'review' in locals() and isinstance(review, dict) and review.get('approved') is False:
                    previous_analysis = analysis
                    previous_reviews.append(review)
                    rejected_fingerprints.add(digest(analysis))
                continue
            break
        if not (resume_run and resume_analysis_complete):
            write_json(work / 'analysis.json', analysis)
            (work / 'business-analysis.md').write_text(analysis_markdown(analysis), encoding="utf-8")
        write_work_plan(work, analysis=analysis, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        locked = digest({'inputs': inputs, 'analysis': analysis})
        if not (resume_run and resume_analysis_complete):
            write_json(work / 'business.lock.json', {'sha256': locked, 'input_sha256': input_hash})
        if not stages.is_completed("analysis"):
            stages.complete("analysis")
        runtime_preflight("after-analysis", analysis)
        integrity()
        event('reusing_draft' if resumed_draft else 'creating', skill_name=analysis['name'])
        feedback = []
        previous_draft = None
        if resume_run and resumed_draft:
            draft_root = resumed_draft
        elif resumed_draft:
            draft_root = work/'drafts/reused'/analysis['name']
            snapshot(resumed_draft,draft_root)
        draft_numbers = [int(path.name) for path in (work / "drafts").iterdir()
                         if path.is_dir() and path.name.isdigit()] if (work / "drafts").is_dir() else []
        draft_offset = max(draft_numbers, default=0)
        for attempt in range(0 if resumed_draft else 2):
            draft = agent.ask('draft',
                DRAFT_INSTRUCTION,
                {'inputs': inputs, 'analysis': analysis, 'effective_contract': effective_requirements(analysis),
                 'validation_feedback': feedback,
                 **({'previous_draft': previous_draft} if previous_draft else {})}, DRAFT)
            previous_draft = draft
            draft, mapping = normalize_draft(draft, analysis['name'])
            draft_root = work / 'drafts' / str(draft_offset + attempt + 1) / analysis['name']
            draft_root.mkdir(parents=True)
            write_json(draft_root.parent / 'path-mapping.json', mapping)
            try:
                apply_changes(draft_root, {'changes': [{'path': f['path'], 'content': f['content'],
                    'reason': f['reason'], 'evidence_ids': f['requirement_ids']} for f in draft['files']]},
                    {r['id'] for r in effective_requirements(analysis)})
                findings = lint(draft_root, analysis['name'])
                write_json(draft_root.parent / 'static-findings.json', findings)
                if any(f['severity'] == 'error' for f in findings):
                    raise RefinerError('Invalid first implementation: ' + json.dumps(findings, ensure_ascii=False))
            except RefinerError as exc:
                if attempt:
                    raise
                feedback = [str(exc)]
                continue
            break
        initial_manifest = manifest(draft_root)
        state['draft_root'] = str(draft_root)
        write_json(work / 'draft-manifest.json', initial_manifest)
        integrity()
        remaining = args.max_agent_calls - agent.calls
        if remaining < 1:
            raise RefinerError('No role-call budget left for tests and refinement')
        child = copy.copy(args)
        child.target, child.output, child.reuse_plan = str(draft_root), str(work / 'refinement'), None
        child.resume_from = None
        # Use the saved original requirements, not an implementation-derived spec.
        child.spec = ([str(request_file)] if inputs['business_description'] else []) + spec_paths
        child.standard = standard_paths
        outer_remaining = max(1, int(deadline - time.monotonic()))
        child.max_seconds, child.max_agent_calls = outer_remaining, remaining
        child.plan_only = bool(getattr(args, 'plan_only', False))
        if resume_run and Path(child.output).is_dir():
            child_contract_path = Path(child.output) / "resume-contract.json"
            if child_contract_path.is_file():
                child_state = read_json(Path(child.output) / "state.json")
                child.resume_from = child.output
                child.max_seconds = max(1, int(float(child_state.get("elapsed_seconds", 0.0)) + outer_remaining))
            else:
                attempts = Path(child.output).parent / "refinement-resume-attempts"
                attempts.mkdir(exist_ok=True)
                prior_attempts = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                                  if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
                archived = attempts / f"attempt-{max(prior_attempts, default=0)+1:03}"
                shutil.move(child.output, archived)
        applicable_scenarios = [s for s in analysis['scenarios'] if s['applicable']]
        child.creation_context = {'contract': effective_requirements(analysis), 'business_analysis': analysis,
                                  **({'_response_contract_v7': inputs['_response_contract_v7']}
                                     if '_response_contract_v7' in inputs else {}),
                                  **({'creation_standard_sources': copy.deepcopy(inputs['standards'])}
                                     if analysis.get('schema_version') == 'skill-refiner.analysis/v4' else {}),
                                  'execution_helpers': PYTHON_OBSERVER_GUIDE,
                                  'technical_coverage_gaps': [technical_coverage] if technical_coverage else [],
                                  'required_case_kinds': sorted({s['kind'] for s in applicable_scenarios}),
                                  'required_scenario_ids': [s['id'] for s in applicable_scenarios]}
        if resumed_suite:
            prior_context = read_json(resumed_suite/'inputs.json')
            child.spec = [row['source'] for row in prior_context['requirements']]
            child.standard = [row['source'] for row in prior_context['standards'] if isinstance(row,dict)]
            child.reuse_plan = str(resumed_suite)
        child_usage.update(_read_child_budget(work))
        _record_child_budget_entry(work, state, parent_elapsed=elapsed_before + time.monotonic() - started,
                                   parent_calls=agent.calls, child_usage=child_usage)
        event('refining', refinement_workspace=child.output)
        refinement = refine(child)
        observe_child_budget()
        # A returned verdict is not proof that its processes and reservations
        # closed. The lenient observer is only for retaining costs on failure.
        child_usage.update(_read_child_budget(work))
        child_usage["agent_calls"] = max(child_usage["agent_calls"], refinement.get("agent_calls", 0))
        child_usage["target_evaluations"] = max(child_usage["target_evaluations"], refinement.get("target_evaluations", 0))
        integrity()
        if manifest(draft_root) != initial_manifest:
            raise RefinerError('Original generated draft changed during refinement')
        state['refinement_status'] = refinement['status']
        state['evaluation_history'] = refinement.get('evaluation_history', state.get('evaluation_history', []))
        state['refinement_rounds'] = refinement.get('rounds', [])
        state['target_evaluations'] = max(state.get('target_evaluations', 0), refinement.get('target_evaluations', 0))
        write_work_plan(work, source=draft_root, analysis=analysis, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        if refinement['status'] == 'planned':
            state['refinement_workspace'] = str(child.output)
            state['plan_ready'] = True
            event('plan_ready', refinement_workspace=str(child.output),
                  work_plan=str(Path(child.output) / 'work-plan.md'),
                  development_cases=refinement.get('planned_development_cases'),
                  acceptance_cases=refinement.get('planned_acceptance_cases'),
                  max_rounds=args.max_rounds, max_seconds=args.max_seconds,
                  max_evaluations=args.max_evaluations)
            state.update(status='planned', planned_only=True,
                         elapsed_seconds=round(elapsed_before + time.monotonic() - started, 3))
            write_json(work / 'state.json', state)
            return state
        if refinement['status'] not in {'passed', 'unchanged'}:
            event(refinement['status'], error=refinement.get('error'), questions=refinement.get('gaps', []))
            return state
        final = work / 'final' / analysis['name']
        refinement_final = Path(refinement['final_skill'])
        if final.is_dir() and manifest(final) == manifest(refinement_final):
            pass
        else:
            if final.exists():
                attempts = work / 'final' / 'resume-attempts'
                attempts.mkdir(parents=True, exist_ok=True)
                prior_attempts = [int(path.name.split('-', 1)[1]) for path in attempts.glob('attempt-*')
                                  if path.is_dir() and path.name.split('-', 1)[1].isdigit()]
                archived = attempts / f"attempt-{max(prior_attempts, default=0)+1:03}"
                archived.mkdir()
                shutil.move(str(final), str(archived / final.name))
            snapshot(refinement_final, final)
        # The repair diff may be empty when the first implementation already passes.
        (work / 'final/changes.patch').write_text((work / 'refinement/final/changes.patch').read_text(encoding="utf-8"), encoding="utf-8")
        full_patch = []
        for path in sorted(manifest(final)):
            full_patch.extend(difflib.unified_diff([], (final / path).read_text(encoding="utf-8").splitlines(True),
                                                  '/dev/null', 'created/' + path))
        (work / 'final/creation.patch').write_text(''.join(full_patch), encoding="utf-8")
        write_json(work / 'final/evidence.json', {'business_lock': locked, 'draft_manifest': initial_manifest,
                   'candidate_manifest': manifest(final), 'refinement_evidence': str(work / 'refinement/final/evidence.json'),
                   'refinement_evidence_sha256': digest(read_json(work / 'refinement/final/evidence.json'))})
        event('created', final_skill=str(final))
        return state
    except Cancelled as exc:
        event('cancelled', error=str(exc))
        return state
    except Exception as exc:
        event('error', error=str(exc))
        return state
    finally:
        observe_child_budget()
        if analysis is not None and not (work / 'business-analysis.md').exists():
            (work / 'business-analysis.md').write_text(analysis_markdown(analysis), encoding="utf-8")
        state.update(agent_calls=max(state.get('agent_calls', 0), agent.calls + child_usage['agent_calls']),
                     creation_agent_calls=agent.calls,
                     target_evaluations=max(state.get('target_evaluations', 0), child_usage['target_evaluations']),
                     cached_agent_calls=agent.cached_calls,
                     elapsed_seconds=max(state.get('elapsed_seconds', 0), round(elapsed_before + time.monotonic() - started, 3)))
        write_json(work / 'state.json', state)
        lines = ['# Business Skill creation', '', 'Status: **' + state['status'] + '**', '',
                 'Business analysis: [business-analysis.md](business-analysis.md)',
                 'Design and tests: [work-plan.md](work-plan.md)',
                 'Refinement results: [refinement/report.md](refinement/report.md)', '',
                 'Role calls: ' + str(state['agent_calls']), 'Evaluations: ' + str(state['target_evaluations']), '',
                 'First drafts and original business inputs are preserved. Failed or incomplete runs are not delivered as final Skills.']
        if state.get('final_skill'):
            lines += ['', 'Final candidate: `' + state['final_skill'] + '`',
                      'Creation diff: [final/creation.patch](final/creation.patch)',
                      'Repair diff: [final/changes.patch](final/changes.patch)']
        if state.get('error'):
            lines += ['', 'Error: ' + state['error']]
        lines += ['', '## Questions / gaps', ''] + ['- ' + q for q in state.get('questions', [])]
        (work / 'report.md').write_text('\n'.join(lines) + '\n')
        write_test_scenarios(work)
        if state.get('status') == 'created':
            stages.activate('verification')
            try:
                from .gate import verify_run
                verification = verify_run(work, deadline=started + max(0, args.max_seconds - elapsed_before))
                write_json(work / 'verification.json', verification)
                state['verification_status'] = verification['status']
                if destination:
                    if destination.exists():
                        if not destination.is_dir() or manifest(destination) != manifest(state['final_skill']):
                            raise RefinerError('Saved destination already exists with different contents')
                    else:
                        snapshot(state['final_skill'], destination)
                    state['destination'] = str(destination)
            except Exception as exc:
                state.update(status='verification_failed', verification_error=str(exc))
            write_json(work / 'state.json', state)
            lines.insert(3, 'Verification: **' + state.get('verification_status', 'failed') + '**')
            if state.get('verification_error'):
                lines += ['', 'Verification error: ' + state['verification_error']]
            (work / 'report.md').write_text('\n'.join(lines) + '\n')
        stages.finish(state.get('status', 'error'))

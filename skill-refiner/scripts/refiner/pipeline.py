from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import difflib
import json
import hashlib
import copy
import os
import re
from pathlib import Path
import threading
import time

from .agent import Agent
from .checks import validate_plan
from .common import (Cancelled, PlanControlError, PlanGateError, PlanValidationError, RefinerError, digest, inventory, manifest, read_json, relative_path,
                     snapshot, write_json)
from .schemas import (PLAN, PLAN_V6, PLAN_V7, PLAN_V8, PLAN_REVIEW, PLAN_REVIEW_V2, PLAN_REVIEW_V3,
                      PLAN_REVIEW_V4, PLAN_REVIEW_RECOVERY, PLAN_REVIEW_V7, PLAN_REVIEW_RECOVERY_V7,
                      PLAN_REVIEW_V8, PLAN_REVIEW_RECOVERY_V8, REPAIR, REVIEW)
from .requirement_revision import (unapproved_recovery, requirement_revision, review_schema as requirement_review_schema,
                                   validate_assessments as validate_requirement_assessments, normative_sources)
from .evidence import repair_evidence
from .skillup import SkillUp
from .standards import (AUTHORING, HOST_SUBAGENT_TEST_RULES, SELF_REVIEW_STANDARD,
                        discover_project_standards, lint)
from .profiles import load_config
from .matrix import Matrix, comparison_markdown
from .stages import StageManifest, REFINE_EVENTS
from .plan import parallel_agent_plan, write_test_scenarios, write_work_plan
from .preflight import (REFINE_PLANNING_STEPS, build_runtime_preflight, emit_runtime_preflight,
                        add_worst_case_evaluation_budget)


PHASE_STATUS_ENUM = ("not_started", "pending", "running", "passed", "failed", "error", "skipped", "reused")
PLAN_GATE_STATUS_ENUM = ("pending", "running", "passed", "not_passed")


def initial_phase_results():
    plan_suites = {"development": "pending", "acceptance": "pending"}
    return {
        "schema_version": "skill-refiner.phase-results/v1",
        "status_enums": {"phase": list(PHASE_STATUS_ENUM), "plan_gate": list(PLAN_GATE_STATUS_ENUM)},
        "plan_generation": {"status": "pending", "suites": dict(plan_suites)},
        "plan_self_check": {"status": "pending", "suites": dict(plan_suites)},
        "independent_review": {"status": "pending", "suites": dict(plan_suites)},
        "plan_gate": {"status": "pending", "evidence": []},
        "skill_evaluation": {"status": "not_started", "suites": {
            "baseline": "not_started", "regression": "not_started", "acceptance": "not_started"}},
        "final_verification": {"status": "not_started"},
    }


def set_phase_result(state, name, status, *, suite=None, **details):
    if name not in state.setdefault("phase_results", initial_phase_results()):
        raise RefinerError(f"Unknown phase result: {name}")
    if name == "plan_gate":
        if status not in PLAN_GATE_STATUS_ENUM:
            raise RefinerError(f"Invalid plan-gate status: {status}")
    elif status not in PHASE_STATUS_ENUM:
        raise RefinerError(f"Invalid phase status: {status}")
    entry = state["phase_results"][name]
    if suite is not None:
        if suite not in entry.get("suites", {}):
            raise RefinerError(f"Unknown {name} suite: {suite}")
        if status not in PHASE_STATUS_ENUM:
            raise RefinerError(f"Invalid {name} suite status: {status}")
        entry["suites"][suite] = status
        if name in {"plan_generation", "plan_self_check", "independent_review"}:
            statuses = list(entry["suites"].values())
            if any(value == "error" for value in statuses):
                entry["status"] = "error"
            elif any(value == "failed" for value in statuses):
                entry["status"] = "failed"
            elif any(value == "running" for value in statuses):
                entry["status"] = "running"
            elif all(value in {"passed", "reused"} for value in statuses):
                entry["status"] = "reused" if all(value == "reused" for value in statuses) else "passed"
            else:
                entry["status"] = "pending"
    else:
        entry["status"] = status
    if details:
        entry.setdefault("history", []).append({"status": status, **details})
        entry["history"] = entry["history"][-100:]
    state.setdefault("phase_history", []).append({
        "sequence": len(state["phase_history"]) + 1,
        "phase": name, "status": status, "suite": suite,
        "at": datetime.now(timezone.utc).isoformat(), **details,
    })
    state["phase_history"] = state["phase_history"][-1000:]
    return entry


def close_incomplete_plan_gate(state, reason, *, cause="upstream_error"):
    """Give a started plan gate a terminal result when planning cannot finish."""
    entry = state.get("phase_results", {}).get("plan_gate", {})
    if entry.get("status") not in {"pending", "running"}:
        return False
    set_phase_result(state, "plan_gate", "not_passed", reason=cause, error=str(reason))
    return True


def close_incomplete_planning_phases(state, reason, *, cause="cancelled"):
    """Close any active plan phase when a controller call is cancelled."""
    phases = state.get("phase_results", {})
    changed = False
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        running_suites = [suite for suite, status in entry.get("suites", {}).items()
                          if status == "running"]
        if running_suites:
            for suite in running_suites:
                set_phase_result(state, name, "error", suite=suite,
                                 cancellation=str(reason), cause=cause)
                changed = True
        elif entry.get("status") == "running":
            set_phase_result(state, name, "error", cancellation=str(reason), cause=cause)
            changed = True
    return changed


def require_plan_gate(state):
    phases = state.get("phase_results", {})
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        allowed = {"passed", "reused"} if name == "plan_generation" else {"passed"}
        if entry.get("status") not in allowed or any(
                entry.get("suites", {}).get(suite) not in allowed
                for suite in ("development", "acceptance")):
            raise PlanGateError(f"Target Skill evaluation blocked because {name} is incomplete")
    status = phases.get("plan_gate", {}).get("status")
    if status != "passed":
        raise PlanGateError(f"Target Skill evaluation blocked because plan gate status is {status!r}")


HELPER_EXECUTION_AUDIT = """For every Python helper supplied in case files, inspect its executable source line by line and
trace the full control flow through every assertion to the final stdout and evidence writes. Check
that all assertions can be true together for the documented client behavior and fake-gateway
responses. Track the case working directory and path anchoring explicitly: Path.iterdir() or glob()
on a relative base yields relative paths, which will not equal an absolute saved_paths value unless
both sides are normalized. Verify artifact counts, directory contents, ordering and cleanup against
the exact operations performed. A correct target must not fail because its test helper compares
incompatible path forms or has contradictory expected outputs. Put the concrete helper assertion
and path/control-flow reasoning in the independent case review basis; do not accept only the
planner's summary of what the helper claims to verify."""


CONVERSATIONAL_JSON_AUDIT = """Do not invent a JSON response schema or a string-only collection field solely to make grading
convenient when the Skill contract asks for ordinary conversation. Prefer checking natural-language
answers directly. If a test needs JSON, state every checked field type in the prompt. json_path_regex requires its pointer to resolve
to a string, so do not apply it to an array such as a natural list of
clarification questions. For an unconstrained collection, accept reasonable string or array forms
with whole-response semantic checks; use exact array checks only when the business contract specifies
all elements. A string good_response does not establish a string-only output contract. Also accept
ordinary equivalent phrasing: a request to generate a video from a text description expresses
text-to-video even when it does not say the token `t2v` or the literal phrase “text-to-video”. Use
structured operation fields when exact operation labels are needed, and do not make natural-language
checks depend on one product-specific term. For alias and capability explanations, assert the
relationship between each exact alias and its returned operation; nearby words assigned to a
different alias must not pass. Make bad_response reverse the relevant alias-operation relationship,
and confirm the oracles reject it before freezing the case. For natural-language response checks,
include at least two distinct good-response paraphrase controls, including ordinary wording likely
used by another capable agent. Validate every good control against every response oracle and ensure
the bad control still contradicts a required behavior."""


CAPABILITY_ABSENCE_AUDIT = """For exact alias-scoped capability absence, accept semantically equivalent wording such as
'未提供', '未列出', '不提供', '未返回', '不支持', or '没有'. Verify each positive wording against the
alias-bounded assertion so one alias's sentence cannot satisfy another alias's capability check."""

NATURAL_LANGUAGE_ASSERTION_AUDIT = """For natural-language assertions, keep the related subject, action and negation within the same
sentence or clause; do not let a phrase about a different action later in the response complete a broad
regex. When a user supplies a specific URL, file or other resource, bind a no-use/no-submit assertion to
that supplied resource; a statement about a different or unrelated resource must not satisfy it."""
CONVERSATIONAL_JSON_AUDIT += "\n" + CAPABILITY_ABSENCE_AUDIT + "\n" + NATURAL_LANGUAGE_ASSERTION_AUDIT + "\n"


INPUT_BOUNDARY_AUDIT = """For executable targets, inspect documented input channels and their decoding and validation paths. When
the same request is accepted from a file and stdin, cover malformed JSON and malformed encoded bytes
on the relevant channels with binary fixtures; assert the documented exit result and that rejection
happens before external side effects. Do not infer byte-decoding behavior from JSON-value fixtures.
Do not make a malformed-byte test pass by setting PYTHONIOENCODING to a strict decoder, setting
PYTHONUTF8, changing locale/encoding variables, or configuring a test-only decoder policy in the
subprocess environment. Remove inherited encoding overrides when needed, then test the target's
documented behavior under ordinary runtime defaults. The target must return the documented input
error itself and must not submit media; a harness-forced strict decoder masks that defect."""


PLANNING = """Inspect every relevant supplied Skill file, executable implementation, references and
external requirements. Derive the promised behavior, not the implementation's mistakes. Design
executable user tasks with concrete fixtures and independently justified expected outputs. Never
put the expected answer into the user's prompt. Capability tests should explicitly ask to use the
installed Skill and its script when this is part of the contract; do not let the executor replace
the target implementation with mental computation. Output checks test the final answer or workspace
files, not SKILL.md text. Decompose each requirement into stable, independently observable acceptance
criteria with its own evidence. Treat executable cases as a detailed scenario matrix: give each a
unique scenario_id and identify preconditions, concrete stimulus, ordered actions, expected observable
outcomes, forbidden outcomes, relevant risk dimensions and source basis. Vary scenarios where workflow
state, actors/permissions, input boundaries, dependencies, cross-requirement effects, side effects,
retries/idempotency or recovery change the correct result. Do not count paraphrases or cosmetic input
changes as distinct coverage. Map each case to the exact acceptance criteria it exercises; cover every
required criterion and each explicitly applicable scenario exactly once in the suite. Assess normal,
boundary and error applicability, and state a scope-based reason for exclusions. If the configured
case/evaluation budget cannot cover the justified matrix, report the uncovered items as a plan gap;
never compress distinct risks into a vague case just to meet a cap.
Use tool_command with a regular expression matching the actual interpreter/script invocation when
executing the bundled script is a required behavior. It inspects real tool-call transcript commands,
not the user's prompt. Installed paths vary across engines; do not invent an absolute/relative path
spelling requirement. Tool checks recognize equivalent existing workspace file paths. Only set
literal_command=true when the source contract explicitly requires a literal command/path spelling.
Use tool_command_absent when a required clarification or permission gate forbids executing a particular
submission command. Give control_command a concrete forbidden-command example; the checker proves no
matching tool-call command appears and validates itself against that positive control.
Use tool_call_absent when a task forbids tool actions, including non-command browser, network or upload
tools. Its value matches recorded tool names and control_tool_name supplies a matching positive
control. For a clarification-only task that must not call any tools, require that in the prompt and
use a nonempty-name pattern such as .+; tool_command_absent alone cannot prove that no non-command
tool was called. This checks observed tool calls, not network egress from an allowed command.
When the response must preserve that script's output, use tool_output_matches
in addition to a content assertion. Its value is a command regex and control_command is a concrete
matching example invocation for positive/negative trace controls. This checks that the final JSON/text
actually equals the observed tool output, rejecting manual correction of a defective script. For an
expected nonzero command result, set expected_exit_code to the documented status; the checker then
requires that exact observed exit code as well as matching complete output. Do not set
expected_exit_code=0: a successful tool-result status already proves success. Some skill-up adapter
transcripts expose only success/failed and omit the numeric exit code; if a test must verify an exact
nonzero subprocess code through such an adapter, use a fixture helper that captures the installed
script's actual argv, exit code and complete stdout into structured evidence, then exits successfully.
Assert that helper's complete observed result and the captured nonzero code.
For paraphrasable user-facing explanations, do not require one literal adjective unless the contract
does; use regex alternatives for materially equivalent explanations or assert structured choices and
reasons instead of grading a single keyword.
The planner's good_response is a synthetic checker control, not evidence that the Skill uses its
labels. Ground every exact json_path_equal value for a status/state field in the documented Skill
contract, or state that exact enum in the case prompt; otherwise omit that exact status check. When
you introduce a JSON output shape for a conversational task, specify any required enum values in
the prompt itself. Prefer checking the requested decision and question content without inventing
an unconstrained status vocabulary.
For clarification checks about concepts such as dimensions, include common equivalent wording from
the target language and domain (for example width, height, pixels and resolution), not only the
formal term. A correct question about width and height must pass a dimensions check.
For an unspecified media quantity, include natural phrases such as 生成段数, 生成几段,
需要几份 or the equivalent number-of-outputs wording; do not require only a fixed noun like 数量.
For capability explanations, accept equivalent absence wording such as “no i2v capability,”
“provides no image-to-video support,” “i2v is not listed,” or “only t2v is confirmed.” Pair
user-facing explanation checks with structured evidence for requested and discovered operations;
phrases such as “was not returned by discovery” or “does not appear in the catalog” also establish
absence. Do not require a single negation phrase when the response clearly names the absent capability.
For permission checks, include equivalent natural language such as “有权使用,” “允许使用,” and
“已获得授权,” not only “授权” or “许可.” Build each prose assertion from the behavior required,
not from one preferred wording; review the good_response and bad_response controls against the full
semantic range rather than treating either sample as the only acceptable phrasing.
For generated-media or similar requests, carry every user-specified subject and distinguishing
attribute (such as color) into fixture validation and observed-submission assertions; do not accept
an output that preserves the subject while dropping an explicit attribute.
When explaining a change from image-to-video to text-to-video, a statement that the supplied URL or
link will not be used as a reference, or that no image URL/reference field will be submitted or
attached, clearly explains the lost image input. Treat that as equivalent to naming the image or
photo; do not require the same source noun when the supplied URL uniquely identifies the input.
Use json_path_regex to check paraphrasable text inside a specific JSON string field; its pattern
matches the decoded field value, so embedded escaped quotes cannot truncate the assertion. Use regex
for plain-text answers or when the assertion intentionally spans the whole serialized response.
For duration questions, include natural wording such as 多长时间 and 需要多久 alongside 时长,
多少秒 and other equivalent expressions. Do not require the user-facing response to repeat the
formal field name.
Use text_equal when exact non-whitespace response text is required. Insignificant
JSON formatting differences are not missing coverage unless explicitly required by business rules.
At least one case must have kind=normal and at least one kind=boundary, error or negative.
Use only supported declarative checks. Provide good_response and clearly wrong bad_response controls
for response checks. JSON checks should prefer json_path_equal if the output has optional fields.
Use json_path_matches_file when a user-facing JSON field must exactly match a value captured in a
workspace evidence file, such as a reported saved path. Its shape is
{"type":"json_path_matches_file","pointer":"/response/path","path":"observed.json",
"value":{"file_pointer":"/saved_path"}}; value.pointer is an accepted alias for file_pointer.
For file checks use path (workspace relative). For JSON pointers use RFC6901 pointer. Record all
required behaviors and map them to tests. Do not invent out-of-scope features. Report missing
evidence or required binary/omitted files as coverage gaps. Only uncovered REQUIRED behaviors belong
in coverage_gaps; intentional scope exclusions belong in assumptions. Keep fixtures small. Each oracle_reason
must explain how the expected result follows from source requirements, including arithmetic where
applicable. Sources are file paths plus section/line identifiers. The expected JSON value belongs
in checks.value; expected strings must not be nested as JSON strings unnecessarily.
Execution contract: the installed evaluated Skill and its bundled implementation are read-only.
Using a Skill to create or repair a separate task-workspace candidate is allowed; modifying the
installed copy is an execution error. Inspect every fixture's instructions as well as its code.
A fixture that instructs an inner evaluated agent to repair its installed implementation conflicts
with this contract and cannot be used to demonstrate an ordinary baseline failure. Construct a
coherent fixture before freezing tests. A refinement Skill should produce repaired candidate copies,
while the child Skill being evaluated should specify its business behavior without telling its
executor to self-repair. Do not remove real target capabilities to resolve a fixture contradiction."""
PLANNING += """
For a batch of independently requested JSON outputs, json_sequence_equal compares an ordered
array of expected JSON values against the actual raw JSON stream, ignoring object key order and
insignificant whitespace. Do not require the response to wrap outputs in an array. Combine this
with tool_output_matches for unchanged stdout forwarding. Avoid text_equal for semantic JSON
contracts unless exact serialization is explicitly required. Error fixtures must isolate the
intended predicate: an invalid-byte fixture should otherwise be schema-valid if decoded
permissively; a NaN fixture must not also violate an independent field/type rule.
Make output and trace contracts executable as written. json_equal/json_path_equal and
tool_output_matches require a complete JSON response or unchanged stdout; a prompt must not
permit extra prose, explanations, links or summaries that those checks reject. Say to return
only the requested JSON/stdout. JSON path checks may parse an embedded JSON object and therefore
do not prove the whole answer contains only JSON; add an anchored whole-response format check when
the contract requires JSON with no surrounding prose. tool_command sees direct recorded tool-call commands, not
subprocess argv constructed inside a Python helper or heredoc. If a task legitimately delegates
CLI execution to a verifier helper, check that helper's observable invocation and its independent
captured-result assertions instead of adding an unobservable inner-command regex. A fixture
verifier must inspect actual subprocess argv, exit status and output rather than return an
unsubstantiated attestation. Avoid prescribing an arbitrary invocation style solely to satisfy
a regex when equivalent execution is part of the allowed target behavior. If a task prompt permits
multiple invocation paths, checks must accept each permitted path or the prompt must require the
specific path the check observes. Do not combine a permissive helper prompt with a direct-command-only
trace regex.

When a tool_command check is needed to prove a helper or script was run, require its standalone
invocation explicitly in the case prompt and make setup inputs available as case files or separate
setup steps. Do not require a direct invocation while allowing the executor to wrap it inside an
inline Python program or heredoc. For example, if the check matches `python3 scenario_probe.py ...`,
the case prompt must require that command form and provide request files so the executor does not
need an inline program to construct them. Keep nested subprocess checks in the helper's own captured
evidence, where they are observable.
For json_path_matches_file, a dynamic example value in good_response is a control only: the plan
validator builds temporary matching evidence from it to prove the bad_response is rejected. Do not
require a fixed evidence fixture for values that are unknown until execution. Instead verify that
the supplied helper or executable fixture actually writes the checked evidence file from observed
execution results and that the case prompt requires returning the matching response value.
When another file_json_path_equal check reads the same evidence file, include every checked pointer
in the known-good file fixture with values that agree with good_response. The runtime helper may
delete or overwrite that control fixture before recording fresh evidence; tell the case prompt to
run the helper and return its observed output, not to use the fixture as runtime evidence.

For tool_output_matches, good_response must be the COMPLETE observed stdout, including every
field the actual source or fixture emits. A partial JSON object can satisfy separate field checks
but is not a valid unchanged-output control. Derive the full response from the supplied implementation,
fixture or recorded real run evidence. If the complete output cannot be justified, do not assert
unchanged forwarding; use checks for the concrete observable fields instead."""
PLANNING += "\n" + HELPER_EXECUTION_AUDIT + "\n"
PLANNING += "\n" + CONVERSATIONAL_JSON_AUDIT + "\n"
PLANNING += "\n" + INPUT_BOUNDARY_AUDIT + "\n"
PLANNING += """
Each case must include human-readable scenario and expected_outcome fields separate from prompt.
scenario states the user situation and inputs at a high level; expected_outcome states what observable
success looks like before machine checks; prompt is only the executor task wording. Do not duplicate the
full prompt into scenario or expected_outcome."""
PLANNING += """

Schema-first JSON contract: read the supplied response_schema before planning. Return exactly one JSON
object matching it, without Markdown fences or prose. Use the exact schema_version in response_schema. Every case
must include every required field; always emit helpers as an array, using [] when no helper is needed.
Use only the schema's kind and check-type enums. Give every check a unique stable id. Include an asset_audit
with every fixture/helper path marked required and available, all linked requirement source_refs, a valid
status (ready or not_required), and a concrete basis. Include positive_controls with stable IDs; all good
wordings must be distinct. Include negative_controls for every response assertion; each declares the exact
assertion_id it contradicts and a rationale. A negative control is valid only when that target assertion
fails and every other response assertion passes. Keep good_response equal to the first positive control.
When validation or review returns feedback, preserve every prior requirement ID and case ID; edit the
targeted case in place and keep all unrelated cases and assets. Never return only the examples named by
the latest feedback."""
PLANNING += """

For a user-facing claim that has several valid phrasings, encode the equivalent phrases as a structured
contains_any check (or use a reviewed regex alternation when structure is not applicable). For prohibited
misclassification claims, enumerate materially different wording in not_contains_any or a justified regex
alternation. Include different positive paraphrases and negative responses in the controls. Do not use a
single narrow phrase such as only '不需要补集数' when '无需补集数' has the same meaning, and do not rely on
one not_contains phrase to detect every wording of an operator's incorrect judgment. Controls must show that
the assertion distinguishes business meaning: vary the target behavior while keeping unrelated fields correct."""
PLANNING += """

Every v5 or v6 plan must include stage_contracts for the Skill's complete persisted/handoff workflow. Declare
stable schema IDs, exactly one active version per logical ID, package-relative schemas/vN/<name>.schema.json paths
where N matches the schema version,
validator IDs and executable validator paths, and whether each schema is needed at target runtime. A version is
for an incompatible persisted-format change, not an internal revision; do not list historical or compatibility
versions in the target Skill contract. Only package schemas and validators marked as needed at target runtime.
Map every stage's requirement IDs, inputs, outputs, validation gate,
failure states, observable completion check and resume policy. Every input/output artifact references a
declared schema. Every runtime validator and schema file must be packaged. Every stage validates after generation, before handoff, on resume and at final verification.
Do not claim a schema exists when only planned: name the file to be delivered and have the final Skill include
every schema it needs at runtime. Define artifacts in the Skill package, not in Refiner run records. The
controller's own stage artifacts use the same packaged schema/validator policy with no self-exemption."""
PLANNING += """

Every v5 or v6 plan also includes a schema-valid execution_plan. State the objective, in-scope/out-of-scope work,
constraints and bounded concurrency. Break the work into ordered, stable-ID steps. Every step names its goal,
concrete actions, inputs, output paths with schema ID/version and observable evidence, requirement IDs, case IDs,
dependencies, execution mode, and one responsible Agent role with isolated write scope, merge owner and join gate.
Give every step observable exit criteria and a bounded failure policy with terminal state and same-run recovery policy.
Use contiguous order numbers; all requirement and case IDs must be covered. Only put independent tasks in the same
parallel wave; explain every serial step. If the host or task provides no safe independent work, set concurrency to 1
and explain the serial schedule. The development and held-out acceptance plans must preserve this exact execution_plan.
The human-readable work plan must render these steps from the structured object, not invent a separate schedule."""
PLANNING += "\n\nHost-subagent test execution policy (separate from the controller Agent wave):\n- " + "\n- ".join(HOST_SUBAGENT_TEST_RULES)
HOST_SUBAGENT_TEST_REVIEW = " ".join(HOST_SUBAGENT_TEST_RULES)
PLANNING += "\n\n" + SELF_REVIEW_STANDARD
LANGUAGE_POLICY_PLANNING = (
    "Language acceptance is part of the frozen plan. For a new Skill, default its user-facing instructions and "
    "human-readable package text to Simplified Chinese unless the user specifies another language. For refinement, "
    "preserve the existing Skill language unless translation is requested. Include at least one case that checks "
    "SKILL.md and representative references or user-visible messages in the required language; do not translate "
    "machine identifiers, protocol literals, source quotations, or business outputs whose language is specified by "
    "the workflow."
)
PLANNING += "\n\n" + LANGUAGE_POLICY_PLANNING

PLAN_REVIEW_CASE_BOOLS = (
    "installed_source_unchanged", "fixture_contract_consistent", "response_contract_consistent",
    "trace_checks_observable", "scenario_consistent", "requirement_trace_consistent",
    "expected_outcome_consistent", "machine_checks_consistent", "matrix_complete",
    "asset_audit_consistent", "positive_controls_validated", "negative_controls_targeted",
    "acceptance_criteria_traceable", "scenario_detail_complete", "risk_coverage_complete",
)

SELF_REVIEW_PLAN_REVIEW = (
    "Independently verify that every consequential target workflow has a frozen, evidence-linked self-review "
    "checklist whose checks link to requirements, acceptance criteria and scenarios. Controls must cover a passing "
    "self-check and a missing-evidence/failure path. Verify the record contract and that one targeted correction is "
    "the maximum: a still-failing check becomes blocked. Self-review cannot replace independent acceptance, add "
    "checks during execution, or trigger unbounded test rounds."
)

LANGUAGE_PLAN_REVIEW = (
    "Independently verify the frozen language policy and its test coverage: new Skills default human-readable "
    "instructions to Simplified Chinese, existing Skills retain their language unless translation is requested, "
    "and the language checks leave machine contracts and required business-output languages intact."
)
PLAN_REVIEW_CASE_BOOLS_V2 = PLAN_REVIEW_CASE_BOOLS[:-3]



def _review_target_cases(review, plan):
    """Expand independently reviewed structured issue scope to concrete cases."""
    targets = review.get("revision_targets")
    issues = review.get("issues", [])
    if not isinstance(targets, list) or len(targets) != len(issues):
        raise RefinerError("Every review issue must have exactly one structured revision scope")
    indexes = [row.get("issue_index") for row in targets]
    if len(indexes) != len(set(indexes)) or set(indexes) != set(range(len(issues))):
        raise RefinerError("Structured revision scope must map every issue exactly once")
    case_by_id = {row["id"]: row for row in plan.get("cases", [])}
    requirements = {row["id"] for row in plan.get("requirements", [])}
    scenarios = {row.get("scenario_id") for row in case_by_id.values() if row.get("scenario_id")}
    selected = set()
    for row in targets:
        if not row["scope_known"]:
            raise PlanGateError("Independent review could not determine the affected revision scope")
        case_ids, req_ids, scenario_ids = set(row["case_ids"]), set(row["requirement_ids"]), set(row["scenario_ids"])
        if case_ids - case_by_id.keys() or req_ids - requirements or scenario_ids - scenarios:
            raise RefinerError("Structured revision scope cites an unknown case, requirement or scenario")
        expanded = set(case_ids)
        for case in case_by_id.values():
            if req_ids.intersection(case.get("requirement_ids", [])) or case.get("scenario_id") in scenario_ids:
                expanded.add(case["id"])
        if not expanded:
            raise PlanGateError("Independent review did not identify a verifiable affected case")
        selected.update(expanded)
    return selected

def _provisional_review_schema(base, provisional):
    if not provisional or not provisional.get("removed_case_sources"):
        return base
    schema = copy.deepcopy(base)
    schema["required"].append("provisional_removal_assessments")
    schema["properties"]["provisional_removal_assessments"] = read_json(
        Path(__file__).resolve().parents[2] / "schemas/v7/provisional-deletion-review.schema.json")
    return schema


def validate_plan_review(review, plan, recovery_revision=None, execution_revision=None,
                         provisional=None, source_contents=None, requirements_revision=None, requirement_inputs=None):
    import jsonschema
    version = plan.get("schema_version")
    legacy = version == "skill-refiner.plan/v2"
    review_schema = {"skill-refiner.plan/v2": PLAN_REVIEW_V2,
                     "skill-refiner.plan/v3": PLAN_REVIEW_V3,
                     "skill-refiner.plan/v4": PLAN_REVIEW_V4}.get(version, PLAN_REVIEW)
    if version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        review_schema = PLAN_REVIEW_V7
    if recovery_revision is not None or execution_revision:
        review_schema = PLAN_REVIEW_RECOVERY_V7 if version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else PLAN_REVIEW_RECOVERY
    if version == "skill-refiner.plan/v8":
        review_schema = PLAN_REVIEW_RECOVERY_V8 if recovery_revision is not None or execution_revision else PLAN_REVIEW_V8
    review_schema = requirement_review_schema(_provisional_review_schema(review_schema, provisional), requirements_revision)
    jsonschema.validate(review, review_schema)
    validate_requirement_assessments(review, requirements_revision, requirement_inputs or {})
    if provisional and provisional.get("removed_case_sources"):
        expected = {row["case_id"]: row["source_plan_sha256"] for row in provisional["removed_case_sources"]}
        assessments = review["provisional_removal_assessments"]
        ids = [row["case_id"] for row in assessments]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise RefinerError("Independent review must assess each removed provisional case exactly once")
        for row in assessments:
            if row["source_plan_sha256"] != expected[row["case_id"]]:
                raise RefinerError("Provisional deletion review cites the wrong source plan")
            if not isinstance(source_contents, dict) or any(
                    not e["quote"].strip() or e["quote"] not in source_contents.get(e["source"], "")
                    for e in row["evidence"]):
                raise RefinerError("Provisional deletion review lacks verified source evidence")
            if review["approved"] and not row["allow_removal"]:
                raise RefinerError("Independent review rejected a provisional case removal")
    if version == "skill-refiner.plan/v8":
        expected_dependencies = {(case["id"], item["id"]) for case in plan["cases"] for item in case["planned_target_dependencies"]}
        assessed = [(row["case_id"], row["dependency_id"]) for row in review["planned_dependency_assessments"]]
        if len(assessed) != len(set(assessed)) or set(assessed) != expected_dependencies:
            raise RefinerError("Independent review must assess every planned target dependency exactly once")
        required_flags = ("source_grounded", "interface_executable", "scenarios_discriminate", "implementation_link_valid")
        if review["approved"] and (not review["planned_dependency_coverage_complete"] or
                any(not all(row[key] for key in required_flags) or len(row["basis"].strip()) < 12
                    for row in review["planned_dependency_assessments"])):
            raise RefinerError("Independent review rejected an unsupported planned target dependency or coverage gap")
    if version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        _review_target_cases(review, plan)
    if recovery_revision is not None or execution_revision:
        expected_changes = ({row["id"] for row in recovery_revision["changes"]} if recovery_revision else set())
        expected_changes.update(row["id"] for row in (execution_revision or []))
        assessments = review["recovery_assessments"]
        ids = [row["change_id"] for row in assessments]
        if len(ids) != len(set(ids)) or set(ids) != expected_changes:
            raise RefinerError("Independent recovery review must assess every revision change exactly once")
        if review["approved"] and any(not row["preserves_requirements"] for row in assessments):
            raise RefinerError("Independent recovery review found an unsupported semantic change")
    if version == "skill-refiner.plan/v4" and not all(
            review[key] for key in ("stage_contracts_assessed", "stage_contracts_complete",
                                    "schema_paths_safe", "validator_lifecycle_covered")):
        raise RefinerError("Independent plan review rejected incomplete Skill stage schema contracts")
    if version in {"skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        required_plan_flags = ("stage_contracts_assessed", "stage_contracts_complete", "schema_paths_safe",
                               "validator_lifecycle_covered", "execution_plan_assessed", "execution_plan_complete",
                               "dependency_graph_valid", "concurrency_safe", "failure_recovery_bounded")
        if not all(review[key] for key in required_plan_flags):
            raise RefinerError("Independent plan review rejected incomplete stage schema contracts or execution plan elements")
        step_reviews = review["step_assessments"]
        expected_steps = {step["id"] for step in plan["execution_plan"]["steps"]}
        assessed_steps = [row["step_id"] for row in step_reviews]
        if len(assessed_steps) != len(set(assessed_steps)) or set(assessed_steps) != expected_steps:
            raise RefinerError("Independent plan review must assess every execution step exactly once")
        flags = ("goal_actions_clear", "inputs_outputs_evidenced", "requirement_case_traceable",
                 "dependencies_valid", "agent_scope_safe", "exit_and_recovery_clear")
        if review["approved"] and any(not all(row[key] for key in flags) for row in step_reviews):
            raise RefinerError("Approved plan review contradicts an execution step assessment")
    contracts = review["case_contracts"]
    expected = {c["id"] for c in plan["cases"]}
    assessed = [c["case_id"] for c in contracts]
    if len(assessed) != len(set(assessed)):
        raise RefinerError("Plan review contains duplicate case assessments")
    for entry in contracts:
        if entry["case_id"] not in expected:
            raise RefinerError(f"Plan review references unknown case: {entry['case_id']}")
    if len(contracts) != len(expected) or set(assessed) != expected:
        raise RefinerError("Plan review must assess the execution contract of every case exactly once")
    required_flags = PLAN_REVIEW_CASE_BOOLS_V2 if legacy else PLAN_REVIEW_CASE_BOOLS
    if review["approved"] and any(not all(entry[k] for k in required_flags) for entry in contracts):
        raise RefinerError("Approved plan review contains a conflicting execution contract")


def recovery_plan_revision(recovery, plan):
    """Make a complete, bounded old/new delta without freezing invalid output."""
    old = recovery["previous_plan"]
    errors = recovery.get("contract_errors", [])
    invalid_id_paths = {e.get("path") for e in errors if isinstance(e, dict) and e.get("constraint") == "pattern"}
    def keyed(rows, kind):
        if not isinstance(rows, list):
            raise RefinerError(f"Recovered {kind} are not a list")
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in result:
                raise RefinerError(f"Recovered {kind} have missing or duplicate IDs")
            result[row["id"]] = row
        return result
    before_requirements, after_requirements = keyed(old.get("requirements"), "requirements"), keyed(plan.get("requirements"), "requirements")
    before_cases, after_cases = keyed(old.get("cases"), "cases"), keyed(plan.get("cases"), "cases")
    if set(before_requirements) - set(after_requirements):
        raise RefinerError("Recovered requirement IDs were dropped")
    mapping, used = {}, set()
    for index, row in enumerate(old["cases"]):
        old_id = row["id"]
        if old_id in after_cases:
            candidate = old_id
        elif (f"/cases/{index}/id" in invalid_id_paths and old_id.lower() in after_cases
              and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", old_id.lower())):
            candidate = old_id.lower()
        else:
            raise RefinerError("Recovered case ID has no proven one-to-one mapping: " + old_id)
        if candidate in used:
            raise RefinerError("Recovered case ID normalization is ambiguous")
        mapping[old_id] = candidate
        used.add(candidate)
    changes = []
    def add(kind, identity, field, old_value, new_value):
        if old_value != new_value:
            changes.append({"id": f"D{len(changes)+1:04d}", "kind": kind, "identity": identity,
                            "field": field, "old": old_value, "new": new_value})
    def field_state(row, field):
        return {"present": field in row, "value": row[field] if field in row else None}
    for identity, row in before_requirements.items():
        new = after_requirements[identity]
        for field in sorted(set(row) | set(new)):
            add("requirement", identity, field, field_state(row, field), field_state(new, field))
    for identity in sorted(set(after_requirements) - set(before_requirements)):
        add("requirement_added", identity, "record", None, after_requirements[identity])
    for old_id, new_id in mapping.items():
        before, after = before_cases[old_id], after_cases[new_id]
        for field in sorted(set(before) | set(after)):
            add("case", old_id + "->" + new_id, field, field_state(before, field), field_state(after, field))
    for identity in sorted(set(after_cases) - used):
        add("case_added", identity, "record", None, after_cases[identity])
    for field in sorted((set(old) | set(plan)) - {"requirements", "cases"}):
        add("plan", "plan", field, field_state(old, field), field_state(plan, field))
    if len(changes) > 256 or len(json.dumps(changes, ensure_ascii=False).encode("utf-8")) > 200000:
        raise RefinerError("Recovery revision exceeds the bounded independent review handoff")
    return {"schema_version": "skill-refiner.recovery-plan-revision/v1",
            "previous_plan_sha256": digest(old), "plan_sha256": digest(plan),
            "case_id_mapping": mapping, "changes": changes}


def verified_response_sources(work, context=None, *, include_inputs=False):
    """Bind source identifiers to the content inside authenticated inputs.json.

    Identifiers are never opened as paths. The caller's run directory and its
    frozen resume contract establish the allowed input snapshot.
    """
    root = Path(work)
    def safe_read(name):
        path = root / name
        for item in (root, path):
            try:
                stat = item.lstat()
            except FileNotFoundError as exc:
                raise RefinerError("Frozen response source evidence is missing") from exc
            except OSError as exc:
                raise RefinerError(f"Cannot access frozen response source evidence: {exc}") from exc
            if item.is_symlink() or (getattr(stat, "st_file_attributes", 0) & 0x400):
                raise RefinerError("Frozen response source evidence uses a symlink or junction")
        if not path.is_file() or path.resolve().parent != root.resolve():
            raise RefinerError("Frozen response source evidence is outside the run")
        return read_json(path)
    inputs = safe_read("inputs.json")
    resume = safe_read("resume-contract.json")
    if not isinstance(inputs, dict) or digest(inputs) != resume.get("inputs_sha256"):
        raise RefinerError("Frozen response source input hash does not match the resume contract")
    if context is not None:
        for name in ("requirements", "files", "standards", "business_description"):
            if name in context and context[name] != inputs.get(name):
                raise RefinerError("Planning source content differs from the authenticated inputs")
    result = {}
    def add(ref, content):
        if not isinstance(ref, str) or not ref or any(ord(c) < 32 for c in ref):
            raise RefinerError("Invalid response source identifier")
        if not isinstance(content, str):
            raise RefinerError("Response source content must be text")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if ref in result and result[ref] != sha:
            raise RefinerError("Conflicting content for response source identifier")
        result[ref] = sha
    for row in inputs.get("requirements", []):
        if isinstance(row, dict):
            add(row.get("source"), row.get("content"))
    for row in inputs.get("files", []):
        if not isinstance(row, dict) or not isinstance(row.get("content"), str):
            continue
        name = row.get("path", "").replace("\\", "/")
        if (not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise RefinerError("Unsafe target source identifier in frozen inputs")
        add("target-file:" + name, row["content"])
    if isinstance(inputs.get("business_description"), str) and inputs["business_description"]:
        add("business_description", inputs["business_description"])
    for index, standard in enumerate(inputs.get("standards", [])):
        if isinstance(standard, str):
            add(f"standard:{index}", standard)
    return (result, inputs) if include_inputs else result


def response_contract_applicability(inputs, plan, *, allow_unresolved=False):
    """Find response-specific JSON obligations in authenticated source text.

    This conservative recognizer handles unqualified and explicit normal/error/
    clarification wording. Other conditional response formats remain a gap.
    """
    files = inputs.get("files", [])
    entry = next((row.get("content", "") for row in files if isinstance(row, dict)
                  and row.get("path", "").replace("\\", "/") == "SKILL.md"), "")
    sources = [(row.get("source", ""), row.get("content", "")) for row in inputs.get("requirements", [])
               if isinstance(row, dict) and isinstance(row.get("content"), str)]
    if entry:
        sources.append(("target-file:SKILL.md", entry))
    if isinstance(inputs.get("business_description"), str) and inputs["business_description"]:
        sources.append(("business_description", inputs["business_description"]))
    schema_paths = [row.get("path", "").replace("\\", "/") for row in files if isinstance(row, dict)
                    and isinstance(row.get("path"), str) and row["path"].lower().endswith(".schema.json")]
    required_kinds, global_json, explicit_text, global_text, details, conditional_unresolved = set(), False, False, False, [], []
    action = r"(?:输出|返回|给出|提供|生成|响应|回复|答复|答案|作答|回答|结果|return|output|respond|response|reply|answer)"
    output_format = r"(?:\bjson\b|\.schema\.json)"
    text_format = r"(?:纯文本|纯文字|自然语言|文字摘要|中文说明|纯中文说明|plain[ -]?text|natural language)"
    input_role = r"(?:输入|入参|传入|上传|请求|内部|中间|日志|配置示例|input|incoming|request|upload|internal|log)"
    text_rule = re.compile(r"(?:" + action + r"[^。；;\n]{0,80}" + text_format + r"|" + text_format + r"[^。；;\n]{0,80}" + action + r")", re.I)
    json_rule = re.compile(r"(?:" + action + r"[^。；;\n]{0,160}" + output_format + r"|" + output_format + r"[^。；;\n]{0,160}" + action + r")", re.I)
    action_rule = re.compile(action, re.I)
    condition_rule = re.compile(r"(?:当|如果|若|仅当|只有|情况下|情形下|时候|之时|时|\bwhen\b|\bunless\b|\bif\b|\bonly\b|\bon failure\b|\bon error\b)", re.I)
    negative_rule = re.compile(r"(?:不得|禁止|不要|无需|不应|不能|不是|不需要|不允许|不使用|不要求|请勿|勿|非|don't|do not|must not|should not|never|without|no\s+json)", re.I)
    for source, content in sources:
        for part in re.split(r"[。；;\n]+", content):
            if text_rule.search(part):
                explicit_text = True
                if not condition_rule.search(part) and not re.search(
                        r"正常|成功|错误|异常|失败|澄清|边界|normal|success|error|fail", part, re.I):
                    global_text = True
            clauses = re.split(r"[，,]|\s+and\s+|且", part, flags=re.I)
            for index, clause in enumerate(clauses):
                if not re.search(output_format, clause, re.I):
                    continue
                previous = clauses[index - 1] if index else ""
                # Preserve a condition separated by a comma from its result.
                prefix = previous if previous.strip() and not action_rule.search(previous) and not re.search(output_format, previous, re.I) else ""
                following = clauses[index + 1] if index + 1 < len(clauses) else ""
                suffix = following if condition_rule.search(following) and not action_rule.search(following) else ""
                scoped = prefix + " " + clause + " " + suffix
                json_position = re.search(output_format, clause, re.I).start()
                negative = negative_rule.search(clause)
                if negative and negative.start() < json_position:
                    # Only direct, unambiguous prohibitions can establish a
                    # non-JSON response. E.g. "never omit" and "不得不" are
                    # NOT prohibitions; ambiguous negation needs review.
                    direct_negative = re.search(
                        r"(?:不要|不得|禁止|无需|不能|不应|不使用|不要求|"
                        r"don't|do not|must not|should not|never|without|no)"
                        r"\s*(?:(?:将)?(?:结果|回复|响应)\s*)?"
                        r"(?:输出|返回|生成|使用|以|是|为|return|output|respond|reply|use|be)?"
                        r"\s*(?:为|以|是|格式)?\s*json\b|(?:输出|返回|回复|回答)\s*非\s*json\b", clause, re.I)
                    if (not direct_negative or
                            len(list(negative_rule.finditer(clause[:json_position]))) > 1 or
                            condition_rule.search(scoped[:scoped.find(clause) + negative.start()])):
                        conditional_unresolved.append({"source": source, "quote": part.strip()})
                    else:
                        explicit_text = True
                    continue
                if re.search(r"(?:json\s*文件|json\s+file)", clause, re.I):
                    # A generated file is not automatically the textual agent
                    # response; only separate, explicit natural-language output
                    # evidence resolves its role without semantic review.
                    if not (following and text_rule.search(following) and
                            re.search(r"(?:最终|用户|final|user)", following, re.I)):
                        conditional_unresolved.append({"source": source, "quote": part.strip()})
                    continue
                action_match = action_rule.search(clause)
                if re.search(input_role, clause, re.I) and not action_match:
                    if re.search(r"(?:也|同样|相同格式|相同结构|相同方式|also|same format)", following, re.I) and action_rule.search(following):
                        conditional_unresolved.append({"source": source, "quote": part.strip()})
                    continue
                if (re.search(r"(?:日志|内部|中间文件|审计记录|\blog\b|\binternal\b)", clause, re.I)
                        and not re.search(r"(?:用户|最终|回复|答复|回答|\buser\b|\bfinal\b|\bresponse\b)", clause, re.I)):
                    continue
                if re.search(r"(?:说明如何|解释如何|explain how|describe how)", clause, re.I) and not action_match:
                    continue
                if not action_match:
                    # A format mention without an identifiable response role
                    # may refer to a split or inverted output requirement.
                    conditional_unresolved.append({"source": source, "quote": part.strip()})
                    continue
                # Output may precede or follow JSON. Conditions can also appear
                # after it (e.g. 'JSON ... only on failure'); never infer global
                # scope from an action alone.
                action_start = scoped.find(clause) + action_match.start()
                scope_text = scoped[:action_start]
                kinds = set()
                conditional = condition_rule.search(scoped) or bool(prefix.strip())
                clean_scope = re.sub(r"[\s，,：:、]+", "", scope_text)
                simple_scope = re.fullmatch(
                    r"(?:(?:当|如果|若)(?:发生|出现|遇到|处于)?)?"
                    r"(?:正常|成功|错误|异常|失败|澄清|信息不足|边界|normal|success|error|failure|clarification|boundary)"
                    r"(?:情况|情形|状态|发生|出现)?(?:下|时|时候)?(?:请)?", clean_scope, re.I)
                if conditional and (not simple_scope or condition_rule.search(scoped[action_start:])):
                    conditional_unresolved.append({"source": source, "quote": part.strip()})
                    continue
                if simple_scope:
                    if re.search(r"正常|成功|normal|success", scope_text, re.I):
                        kinds |= {"normal", "regression"}
                    if re.search(r"错误|异常|失败|error|fail", scope_text, re.I):
                        kinds |= {"error", "negative"}
                    if re.search(r"澄清|信息不足|边界|clarif|boundary", scope_text, re.I):
                        kinds.add("boundary")
                if kinds:
                    required_kinds |= kinds
                    details.append({"source": source, "scope": sorted(kinds), "quote": part.strip()})
                else:
                    global_json = True
                    details.append({"source": source, "scope": "all_cases", "quote": part.strip()})
    if global_text and (global_json or required_kinds):
        conditional_unresolved.append({"source": "multiple", "quote": "JSON and natural-language response requirements overlap or conflict", "kind": "conflict"})
    if conditional_unresolved and not allow_unresolved:
        raise RefinerError("Conditional JSON response applicability needs evidence-based classification: "
                           + json.dumps(conditional_unresolved, ensure_ascii=False))
    unclassified = []
    for path in schema_paths:
        mentions = [part for _, content in sources for part in re.split(r"[。；;\n]+", content)
                    if path in part.replace("\\", "/")]
        nonresponse = any(re.search(r"(?:输入|入参|传入|内部|中间|日志|input|internal|log)", fragment, re.I)
                          and not json_rule.search(fragment)
                          for part in mentions for fragment in re.split(r"[，,]", part)
                          if path in fragment.replace("\\", "/"))
        if not nonresponse:
            unclassified.append(path)
    if unclassified and not global_json and not required_kinds and not global_text:
        if not allow_unresolved:
            raise RefinerError("JSON Schema files exist but their response/input/internal role is not established")
        conditional_unresolved.extend({"source": "target-file:" + path, "quote": "Schema role is not established"} for path in unclassified)
    required = sorted(case["id"] for case in plan["cases"]
                      if global_json or case.get("kind") in required_kinds)
    if not global_json:
        case_kinds = {case.get("kind") for case in plan["cases"]}
        if any(isinstance(row["scope"], list) and not case_kinds.intersection(row["scope"])
               for row in details):
            raise RefinerError("Conditional JSON response applicability has no matching case; independent semantic classification is required")
    return {"required_case_ids": required, "evidence": details,
            "non_json_response_explicit": explicit_text, "unclassified_schema_files": unclassified,
            "unresolved": conditional_unresolved}



def _response_source_contents(inputs):
    """Return only authorized content already inside the frozen input snapshot."""
    result = {}
    def add(ref, value):
        if isinstance(ref, str) and isinstance(value, str):
            if ref in result and result[ref] != value:
                raise RefinerError("Conflicting frozen response source content")
            result[ref] = value
    for row in inputs.get("requirements", []):
        if isinstance(row, dict):
            add(row.get("source"), row.get("content"))
    for row in inputs.get("files", []):
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            add("target-file:" + row["path"].replace("\\", "/"), row.get("content"))
    if isinstance(inputs.get("business_description"), str) and inputs["business_description"]:
        add("business_description", inputs["business_description"])
    for index, item in enumerate(inputs.get("standards", [])):
        if isinstance(item, str):
            add(f"standard:{index}", item)
    return result


def _response_analysis_schema():
    return read_json(Path(__file__).resolve().parents[2] /
                     "schemas/v7/response-applicability-analysis.schema.json")



def _captured_role_call(work, role, payload, response):
    """Find the controller's terminal persisted call for this exact request."""
    root = Path(work) / "agent-calls"
    if not root.is_dir() or root.is_symlink():
        raise RefinerError("Response classification call evidence is missing")
    for directory in sorted(root.glob("*-" + role), reverse=True):
        try:
            if directory.is_symlink() or not re.fullmatch(r"[0-9]+-" + re.escape(role), directory.name):
                continue
            paths = [directory / name for name in ("request.json", "response.json", "request-manifest.json", "execution.json")]
            if any(path.is_symlink() or not path.is_file() for path in paths):
                continue
            request, saved_response, manifest, execution = [read_json(path) for path in paths]
            if (request.get("role") != role or request.get("input") != payload or saved_response != response or
                    manifest.get("role") != role or manifest.get("status") != "completed" or
                    execution.get("status") != "completed" or manifest.get("response_sha256") != digest(response) or
                    manifest.get("request_sha256") != hashlib.sha256(json.dumps(request, ensure_ascii=False).encode("utf-8")).hexdigest()):
                continue
            return {"call_dir": directory.name, "request": request, "manifest": manifest, "execution": execution}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    raise RefinerError("A completed controller call for response applicability could not be verified")


def _validate_applicability_assessment(response, inputs, plan, label):
    import jsonschema
    try:
        jsonschema.validate(response, _response_analysis_schema())
    except jsonschema.ValidationError as exc:
        raise RefinerError(f"Invalid {label} response applicability schema") from exc
    expected = {case["id"] for case in plan["cases"]}
    ids = [row["case_id"] for row in response["assessments"]]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise RefinerError(f"{label} must assess every planned case exactly once")
    contents = _response_source_contents(inputs)
    classification = {}
    for row in response["assessments"]:
        if not row["reason"].strip() or any(not item["quote"].strip() for item in row["evidence"]):
            raise RefinerError(f"{label} contains empty reasoning or evidence")
        if row["decision"] == "uncertain":
            raise RefinerError(f"{label} marked response applicability uncertain for {row['case_id']}")
        for evidence in row["evidence"]:
            text = contents.get(evidence["source"])
            if text is None or evidence["quote"] not in text:
                raise RefinerError(f"{label} cited content not found in the frozen source")
        classification[row["case_id"]] = row["decision"]
    mechanical = response_contract_applicability(inputs, plan, allow_unresolved=True)
    required = {case_id for case_id, decision in classification.items() if decision == "required"}
    if not set(mechanical["required_case_ids"]).issubset(required):
        raise RefinerError("Semantic analysis cannot waive an established response contract requirement")
    return classification

def validate_response_applicability_decision(record, inputs, plan, source_bindings):
    """Recheck independent, blind classifications against the frozen inputs.

    This validates provenance and agreement, not the truth of natural language
    interpretation. Unknown or disagreement always blocks target evaluation.
    """
    import jsonschema
    try:
        jsonschema.validate(record, read_json(Path(__file__).resolve().parents[2] /
                                             "schemas/v7/response-applicability-decision.schema.json"))
    except jsonschema.ValidationError as exc:
        raise RefinerError("Malformed response applicability decision record") from exc
    if (record["schema_version"] != "skill-refiner.response-applicability-decision/v1" or
            record["inputs_sha256"] != digest(inputs) or record["plan_sha256"] != digest(plan) or
            record["analysis_schema_sha256"] != digest(_response_analysis_schema()) or
            record["decision_schema_sha256"] != digest(read_json(Path(__file__).resolve().parents[2] /
                                                      "schemas/v7/response-applicability-decision.schema.json")) or
            record["sources"] != source_bindings):
        raise RefinerError("Response applicability decision is not bound to frozen inputs, plan and sources")
    contents = _response_source_contents(inputs)
    if {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in contents.items()} != source_bindings:
        raise RefinerError("Response applicability source hashes do not match frozen input content")
    expected = {case["id"] for case in plan["cases"]}
    mechanical = response_contract_applicability(inputs, plan, allow_unresolved=True)
    if any(row.get("kind") == "conflict" for row in mechanical["unresolved"]):
        raise RefinerError("Conflicting response format requirements cannot be waived by semantic review")
    expected_payload = {"frozen_sources": contents, "frozen_source_sha256": source_bindings,
                        "plan": plan, "unresolved": mechanical["unresolved"],
                        "recognized_requirements": mechanical["evidence"],
                        "canonical_response_schema": _response_analysis_schema()}
    decisions = []
    for label in ("analysis", "independent_review"):
        evidence = record["call_evidence"][label]
        role = "analysis-review" if label == "analysis" else "plan-review"
        request, manifest, execution = evidence["request"], evidence["manifest"], evidence["execution"]
        expected_prefix = ("Independently classify EVERY planned case" if label == "analysis" else
                           "Perform a fresh independent review. Independently classify EVERY planned case")
        if (not re.fullmatch(r"[0-9]+-" + re.escape(role), evidence["call_dir"]) or
                not isinstance(request.get("instruction"), str) or not request["instruction"].startswith(expected_prefix) or
                request.get("role") != role or request.get("input") != expected_payload or
                manifest.get("role") != role or manifest.get("status") != "completed" or
                execution.get("status") != "completed" or manifest.get("response_sha256") != digest(record[label]) or
                manifest.get("request_sha256") != hashlib.sha256(json.dumps(request, ensure_ascii=False).encode("utf-8")).hexdigest()):
            raise RefinerError(f"{label} controller call evidence is invalid")
        response = record[label]
        try:
            jsonschema.validate(response, _response_analysis_schema())
        except jsonschema.ValidationError as exc:
            raise RefinerError(f"Invalid {label} response applicability schema") from exc
        ids = [row["case_id"] for row in response["assessments"]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise RefinerError(f"{label} must assess every planned case exactly once")
        classification = {}
        for row in response["assessments"]:
            if not row["reason"].strip() or any(not item["quote"].strip() for item in row["evidence"]):
                raise RefinerError(f"{label} contains empty reasoning or evidence")
            if row["decision"] == "uncertain":
                raise RefinerError(f"{label} marked response applicability uncertain for {row['case_id']}")
            for evidence in row["evidence"]:
                text = contents.get(evidence["source"])
                if text is None or evidence["quote"] not in text:
                    raise RefinerError(f"{label} cited content not found in the frozen source")
            classification[row["case_id"]] = row["decision"]
        decisions.append(classification)
    if decisions[0] != decisions[1]:
        raise RefinerError("Independent response applicability review disagreed with analysis")
    required = sorted(case_id for case_id, decision in decisions[0].items() if decision == "required")
    if not set(mechanical["required_case_ids"]).issubset(required):
        raise RefinerError("Semantic analysis cannot waive an established response contract requirement")
    return {"required_case_ids": required, "decision_sha256": digest(record),
            "mechanical": mechanical}



def _response_decision_path(work, plan):
    root = Path(work)
    path = root / "plan-controls" / "response-applicability" / (digest(plan) + ".json")
    for item in (root, root / "plan-controls", path.parent, path):
        try:
            stat = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or (getattr(stat, "st_file_attributes", 0) & 0x400):
            raise RefinerError("Response applicability evidence uses a symlink or junction")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RefinerError("Response applicability evidence is outside the run")
    return path

def resolve_response_applicability(agent, work, inputs, plan, source_bindings):
    """Use no more than two blind, budgeted controller calls for an ambiguity."""
    mechanical = response_contract_applicability(inputs, plan, allow_unresolved=True)
    if any(row.get("kind") == "conflict" for row in mechanical["unresolved"]):
        raise RefinerError("Conflicting response format requirements need resolution before evaluation")
    if not mechanical["unresolved"]:
        return mechanical
    path = _response_decision_path(work, plan)
    if path.is_file():
        return validate_response_applicability_decision(read_json(path), inputs, plan, source_bindings)
    contents = _response_source_contents(inputs)
    if {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in contents.items()} != source_bindings:
        raise RefinerError("Response applicability sources do not match authenticated input snapshot")
    payload = {"frozen_sources": contents, "frozen_source_sha256": source_bindings,
               "plan": plan, "unresolved": mechanical["unresolved"],
               "recognized_requirements": mechanical["evidence"],
               "canonical_response_schema": _response_analysis_schema()}
    instruction = (
        "Independently classify EVERY planned case against the frozen source requirements. "
        "Return required only if that case's final textual response must be JSON; "
        "return not_required only when the source and case establish that it need not be JSON. "
        "Input data, logs, internal files and separate deliverable files are not automatically the final response. "
        "Do not use an existing target schema as business truth if it conflicts with requirements. "
        "Never infer a missing business rule. Use uncertain for ambiguity, conditional scope not derivable "
        "from this case, contradictory evidence or insufficient source. For each case provide a meaningful "
        "reason and exact verbatim supporting quote from an authorized frozen source. "
        "Assess independently: do not assume the plan's current response_contract is correct."
    )
    # Each call sees the same original sources and plan, never the other's answer.
    # Reserve the whole group atomically before starting either uncached call.
    schema = _response_analysis_schema()
    reviewer_instruction = "Perform a fresh independent review. " + instruction
    requests = [
        {"role": "analysis-review", "instruction": instruction, "payload": payload,
         "schema": schema, "_attempt": 1},
        {"role": "plan-review", "instruction": reviewer_instruction, "payload": payload,
         "schema": schema, "_attempt": 1},
    ]
    if not callable(getattr(agent, "reserve_call_group", None)):
        raise RefinerError("Agent does not support atomic applicability budget reservation")
    with agent.reserve_call_group(requests) as tickets:
        analysis = agent.ask("analysis-review", instruction, payload, schema,
                             _attempt=1, _reservation=tickets[0])
        _validate_applicability_assessment(analysis, inputs, plan, "analysis")
        review = agent.ask("plan-review", reviewer_instruction, payload, schema,
                           _attempt=1, _reservation=tickets[1])
    record = {"schema_version": "skill-refiner.response-applicability-decision/v1",
              "inputs_sha256": digest(inputs), "plan_sha256": digest(plan),
              "analysis_schema_sha256": digest(_response_analysis_schema()),
              "decision_schema_sha256": digest(read_json(Path(__file__).resolve().parents[2] /
                                                  "schemas/v7/response-applicability-decision.schema.json")),
              "sources": source_bindings, "analysis": analysis, "independent_review": review,
              "call_evidence": {"analysis": _captured_role_call(work, "analysis-review", payload, analysis),
                                "independent_review": _captured_role_call(work, "plan-review", payload, review)}}
    result = validate_response_applicability_decision(record, inputs, plan, source_bindings)
    write_json(path, record)
    return result


def load_response_applicability(work, inputs, plan, source_bindings):
    mechanical = response_contract_applicability(inputs, plan, allow_unresolved=True)
    if not mechanical["unresolved"]:
        return mechanical
    path = _response_decision_path(work, plan)
    if not path.is_file():
        raise RefinerError("Independent response applicability decision is missing")
    return validate_response_applicability_decision(read_json(path), inputs, plan, source_bindings)


def response_applicability_lock(work, inputs, plans, source_bindings):
    """Bind all semantic decisions to the frozen oracle lock by plan hash."""
    hashes = {}
    for suite, plan in plans.items():
        if plan.get("schema_version") not in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
            continue
        result = load_response_applicability(work, inputs, plan, source_bindings)
        if result.get("decision_sha256"):
            hashes[suite] = result["decision_sha256"]
    return hashes


def response_contract_migration(work, context, recovery, contract, *, create=False):
    """Bind v7 selection to an already authenticated, unfrozen plan recovery."""
    import jsonschema
    root = Path(work)
    path = root / "response-contract-migration.json"
    for item in (root, path):
        try:
            stat = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or (getattr(stat, "st_file_attributes", 0) & 0x400):
            raise RefinerError("Response contract migration evidence uses a symlink or junction")
    if not isinstance(recovery, dict) or not contract.get("refine_plan_recovery_sha256"):
        raise RefinerError("Response contract migration requires authenticated prior plan recovery")
    prior = recovery.get("previous_plan", {})
    version = prior.get("schema_version") if isinstance(prior, dict) else None
    if not isinstance(version, str) or not version:
        # An invalid draft may omit its own version. Preserve that fact rather
        # than inventing a source schema; the original response remains bound
        # by the authenticated recovery record.
        version = "not_declared_in_invalid_draft"
    schema_path = Path(__file__).resolve().parents[2] / "schemas/v7/response-contract-migration.schema.json"
    record = {"schema_version": "skill-refiner.response-contract-migration/v1",
              "migration_schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
              "inputs_sha256": digest(context),
              "recovery_sha256": contract["refine_plan_recovery_sha256"],
              "source_plan_schema_version": version,
              "selected_plan_schema_version": "skill-refiner.plan/v7",
              "usage": recovery["usage"]}
    schema = read_json(schema_path)
    jsonschema.validate(record, schema)
    if path.exists():
        saved = read_json(path)
        jsonschema.validate(saved, schema)
        if saved != record:
            raise RefinerError("Response contract migration evidence differs from the authenticated recovery")
    elif create:
        write_json(path, record)
    else:
        raise RefinerError("Frozen response contract migration evidence is missing")
    return record


def _validation_error_case_ids(plan, error):
    """Locate only validator diagnostics that identify an exact frozen case."""
    message = str(error)
    found = set()
    for case in plan.get("cases", []):
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            continue
        case_id = case["id"]
        checks = case.get("checks", [])
        assertion_ids = [row.get("id") for row in checks if isinstance(row, dict)]
        controls = case.get("positive_controls", []) + case.get("negative_controls", [])
        control_ids = [row.get("id") for row in controls if isinstance(row, dict)]
        exact = {
            f"Duplicate assertion id in case {case_id}: {assertion_ids}",
            f"Duplicate control id in case {case_id}: {control_ids}",
            f"Positive controls must use distinct response wording: {case_id}",
            f"Negative controls must use distinct response wording: {case_id}",
            f"A response cannot be both a positive and negative control: {case_id}",
            f"Canonical positive control mismatch: {case_id}",
            f"Canonical bad_response must be one of the negative controls: {case_id}",
            f"Legacy good_responses do not match positive_controls: {case_id}",
        }
        if message in exact:
            found.add(case_id)
    return sorted(found)


def _validation_error_diagnostic(plan, error):
    """Derive bounded, repeatable locations and values from the rejected plan."""
    message = str(error)
    ids = _validation_error_case_ids(plan, error)
    paths, details = [], []
    steps = plan.get("execution_plan", {}).get("steps", [])
    # The validator stops at the first violation. Once that exact diagnostic is
    # reproduced, independently enumerate every instance of the same predicate.
    reproduced = any(isinstance(step, dict) and isinstance(step.get("id"), str) and
                     message == f"Execution step {step['id']} stops but does not record a blocked recovery policy"
                     and isinstance(step.get("failure_policy"), dict)
                     and step["failure_policy"].get("action") == "stop"
                     and step["failure_policy"].get("recovery_policy") != "blocked" for step in steps)
    if reproduced:
        for step in steps:
            policy = step.get("failure_policy", {}) if isinstance(step, dict) else {}
            if (isinstance(step, dict) and isinstance(step.get("id"), str) and isinstance(policy, dict)
                    and policy.get("action") == "stop" and policy.get("recovery_policy") != "blocked"):
                paths.append({"step_id": step["id"], "field_path": "failure_policy.recovery_policy",
                              "actual": policy.get("recovery_policy"), "expected": "blocked"})
    if message.startswith("Legacy good_responses do not match positive_controls: "):
        ids = []
        for case in plan.get("cases", []):
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                continue
            positives = case.get("positive_controls", [])
            legacy = case.get("good_responses", [])
            complete = [row.get("response") for row in positives if isinstance(row, dict)]
            if "good_responses" in case and legacy not in (complete, complete[1:]):
                ids.append(case["id"])
                details.append({"case_id": case["id"], "field_path": "good_responses",
                                "expected_condition": "match all positive control responses or their canonical-excluded tail, in order",
                                "expected_alternatives": [complete, complete[1:]], "actual": legacy})
    return {"type": type(error).__name__, "message": message, "case_ids": sorted(ids),
            "execution_paths": paths, "details": details}


def _validation_error_feedback(diagnostic):
    additions = {key: diagnostic[key] for key in ("execution_paths", "details") if diagnostic.get(key)}
    text = diagnostic["message"]
    if additions:
        data = json.dumps(additions, ensure_ascii=False)
        if len(data.encode("utf-8")) > 150_000:
            raise RefinerError("Complete validation feedback exceeds its bounded request limit")
        text += "\nStructured validation feedback: " + data
    return text


def _authorized_execution_revision(previous_plan, plan, feedback_sources, *, verified_capacity=None, unapproved_history=False):
    """Authorize only authenticated recovery corrections; keep every unrelated plan field frozen.

    Legacy plans retain their historical stop-policy/addition rules. A v8 successor
    to an authenticated, unapproved v7 draft may also add cases and execution steps
    bound to recorded coverage gaps, and may apply the separately verified capacity
    amendment. Final gate validation recomputes these diffs and independently reviews
    every changed item.
    """
    if not previous_plan:
        return []
    original = previous_plan.get("execution_plan", {})
    proposed = plan.get("execution_plan", {})
    normalized = copy.deepcopy(original)
    by_id = {row.get("id"): row for row in normalized.get("steps", []) if isinstance(row, dict)}
    target_steps = {row.get("id"): row for row in proposed.get("steps", []) if isinstance(row, dict)}
    changes = []
    for source in feedback_sources:
        if source.get("kind") != "validation_error":
            continue
        for path in source.get("execution_paths", []):
            step_id = path.get("step_id")
            step, target = by_id.get(step_id), target_steps.get(step_id)
            if (path.get("field_path") != "failure_policy.recovery_policy" or path.get("expected") != "blocked"
                    or not isinstance(step, dict) or not isinstance(target, dict)
                    or step.get("failure_policy", {}).get("action") != "stop"
                    or step.get("failure_policy", {}).get("recovery_policy") != path.get("actual")
                    or target.get("failure_policy", {}).get("recovery_policy") != "blocked"):
                continue
            if step["failure_policy"]["recovery_policy"] == "blocked":
                continue
            changes.append({"id": "execution-policy:" + step_id + ":failure_policy.recovery_policy",
                            "step_id": step_id, "field_path": "failure_policy.recovery_policy",
                            "before": path["actual"], "after": "blocked", "source_attempt": source["attempt"]})
            step["failure_policy"]["recovery_policy"] = "blocked"
    for source in feedback_sources:
        if (source.get("kind") == "historical_scope_change" and normalized.get("scope") == source.get("before")
                and proposed.get("scope") == source.get("after")):
            changes.append({"id": "execution-scope:constraints", "field_path": "scope.constraints",
                            "before": copy.deepcopy(source["before"]), "after": copy.deepcopy(source["after"]),
                            "source_attempt": source["attempt"]})
            normalized["scope"] = copy.deepcopy(source["after"])
    gap_sources = [row for row in feedback_sources if row.get("kind") == "coverage_gaps" and row.get("gaps")]
    criterion_sources = [row for row in feedback_sources
                         if row.get("kind") == "criterion_coverage" and row.get("criterion_ids")]
    old_case_ids = {row.get("id") for row in previous_plan.get("cases", []) if isinstance(row, dict)}
    new_case_ids = {row.get("id") for row in plan.get("cases", []) if isinstance(row, dict)} - old_case_ids
    old_steps = normalized.get("steps", [])
    proposed_steps = proposed.get("steps", [])
    # Only a newly generated v8 successor to an authenticated v7 recovery
    # receives this rule. Earlier v7 replay retains its historical semantics.
    # The gate calls the same function using feedback reconstructed from saved
    # control records, and independent review assesses every recorded change.
    recovered_v8 = (unapproved_history is True
                    and previous_plan.get("schema_version") == "skill-refiner.plan/v7"
                    and plan.get("schema_version") == "skill-refiner.plan/v8"
                    and any(source.get("kind") == "historical_draft_diagnostics"
                            and source.get("origin") == "recovery"
                            and re.fullmatch(r"[a-f0-9]{64}", str(source.get("record_sha256", "")))
                            for source in feedback_sources))
    if recovered_v8 and gap_sources and new_case_ids:
        old_cases = {row.get("id"): row for row in previous_plan.get("cases", []) if isinstance(row, dict)}
        cases = {row.get("id"): row for row in plan.get("cases", []) if isinstance(row, dict)}
        old_requirements = {row.get("id") for row in previous_plan.get("requirements", []) if isinstance(row, dict)}
        requirements = {row.get("id") for row in plan.get("requirements", []) if isinstance(row, dict)}
        if not set(old_cases) <= set(cases) or not old_requirements <= requirements:
            return None
        def mentioned(text, identity):
            return bool(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(identity) + r"(?![A-Za-z0-9_.-])", text))
        def references(case_id):
            case = cases[case_id]
            criteria = case.get("acceptance_criteria_ids", [])
            if not isinstance(criteria, list): return []
            result = []
            for source in gap_sources:
                if type(source.get("attempt")) is not int or source["attempt"] < 1: continue
                for index, gap in enumerate(source["gaps"]):
                    if not isinstance(gap, str) or not gap: continue
                    if mentioned(gap, case_id) or any(isinstance(cid,str) and mentioned(gap,cid) for cid in criteria):
                        result.append({"attempt": source["attempt"], "gap_index": index, "gap_sha256": digest(gap)})
            return result
        links = {case_id: references(case_id) for case_id in new_case_ids}
        if any(not links[case_id] for case_id in new_case_ids): return None
        if not isinstance(old_steps,list) or not isinstance(proposed_steps,list): return None
        old_ids = [step.get("id") for step in old_steps if isinstance(step,dict)]
        new_ids = [step.get("id") for step in proposed_steps if isinstance(step,dict)]
        if (len(old_ids) != len(old_steps) or len(new_ids) != len(proposed_steps)
                or len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids)
                or [item for item in new_ids if item in set(old_ids)] != old_ids
                or not old_ids or new_ids[-1] != old_ids[-1]): return None
        old_by_id = {step["id"]:step for step in old_steps}
        new_by_id = {step["id"]:step for step in proposed_steps}
        added_ids = set(new_ids) - set(old_ids)
        if sorted(step.get("order") for step in proposed_steps) != list(range(1,len(proposed_steps)+1)):
            return None
        by_order = sorted(proposed_steps,key=lambda step: step["order"])
        if [step["id"] for step in by_order] != new_ids: return None
        ancestry = {}
        for step in proposed_steps:
            deps = step.get("depends_on")
            if not isinstance(deps,list) or len(deps) != len(set(deps)) or any(dep not in ancestry for dep in deps):
                return None
            ancestry[step["id"]] = set(deps)
            for dep in deps: ancestry[step["id"]].update(ancestry[dep])
        last = old_ids[-1]
        if not added_ids <= ancestry[last]: return None
        covered_added_cases = set()
        for step_id in added_ids:
            step = new_by_id[step_id]
            case_ids = set(step.get("case_ids", []))
            if (not case_ids or not case_ids <= new_case_ids or not ancestry[step_id].intersection(old_ids)
                    or not set(step.get("requirement_ids", [])) <=
                        set().union(*(set(cases[cid].get("requirement_ids", [])) for cid in case_ids))):
                return None
            covered_added_cases.update(case_ids)
        if covered_added_cases != new_case_ids: return None
        def chinese_number(value):
            digits="零一二三四五六七八九"
            if value < 10: return digits[value]
            if value < 100:
                return (digits[value//10] if value//10 > 1 else "") + "十" + (digits[value%10] if value%10 else "")
            return str(value)
        def authorized_capacity_replacement(before, after):
            c = verified_capacity
            if not isinstance(c,dict): return False
            old, current = c.get("old_limits",{}), c.get("current_limits",{})
            old_count, new_count = old.get("development_cases"), current.get("development_cases")
            sha = c.get("amendment_sha256")
            source_ok = any(row.get("kind") == "historical_draft_diagnostics" and row.get("origin") == "recovery"
                            and row.get("record_sha256") == c.get("source_control_sha256")
                            and row.get("plan_sha256") == c.get("source_plan_sha256") for row in feedback_sources)
            if (not source_ok or not isinstance(sha,str) or not re.fullmatch(r"[a-f0-9]{64}",sha)
                    or type(old_count) is not int or type(new_count) is not int or not 0 < old_count < new_count
                    or len(cases) > new_count): return False
            for left in {str(old_count),chinese_number(old_count)}:
                for right in {str(new_count),chinese_number(new_count)}:
                    token = left + "例"
                    if before.count(token) == 1 and before.replace(token,right+"例",1) == after:
                        return True
            return False
        for step_id in old_ids:
            before, after = old_by_id[step_id], new_by_id[step_id]
            rest_before = {k:v for k,v in before.items() if k not in {"case_ids","actions","order","depends_on"}}
            rest_after = {k:v for k,v in after.items() if k not in {"case_ids","actions","order","depends_on"}}
            if rest_before != rest_after: return None
            old_step_cases, next_step_cases = before.get("case_ids"), after.get("case_ids")
            if not isinstance(old_step_cases,list) or not isinstance(next_step_cases,list): return None
            appended = next_step_cases[len(old_step_cases):]
            if (next_step_cases[:len(old_step_cases)] != old_step_cases or len(set(next_step_cases)) != len(next_step_cases)
                    or not set(appended) <= new_case_ids): return None
            before_actions, after_actions = before.get("actions"), after.get("actions")
            if not isinstance(before_actions,list) or not isinstance(after_actions,list): return None
            if len(after_actions) < len(before_actions): return None
            capacity_changed = False
            for old_action, new_action in zip(before_actions, after_actions):
                if old_action == new_action: continue
                if (step_id != old_ids[0] or before.get("agent",{}).get("role") != "controller"
                        or not isinstance(old_action,str) or not isinstance(new_action,str)
                        or not authorized_capacity_replacement(old_action,new_action)): return None
                capacity_changed = True
            extra_actions = after_actions[len(before_actions):]
            if (appended or extra_actions) and before.get("agent",{}).get("role") != "controller": return None
            for action in extra_actions:
                if not isinstance(action,str) or not any(mentioned(action,cid) or any(mentioned(action,criterion)
                        for criterion in cases[cid].get("acceptance_criteria_ids",[])) for cid in new_case_ids):
                    return None
            if step_id != last:
                if before.get("depends_on") != after.get("depends_on"): return None
            else:
                if not set(before.get("depends_on", [])) <= ancestry[last]: return None
                if any(dep not in set(before.get("depends_on", [])) | added_ids for dep in after.get("depends_on", [])):
                    return None
            for field in ("case_ids","actions","order","depends_on"):
                if before.get(field) != after.get(field):
                    if field == "case_ids":
                        relevant_cases = set(appended)
                    elif field == "actions":
                        relevant_cases = {cid for cid in new_case_ids if any(
                            mentioned(action,cid) or any(mentioned(action,criterion)
                                for criterion in cases[cid].get("acceptance_criteria_ids",[]))
                            for action in extra_actions)}
                    else:
                        relevant_cases = set(new_case_ids)
                    changes.append({"id": "execution-step:"+step_id+":"+field, "step_id":step_id,
                        "field_path":field, "before":copy.deepcopy(before.get(field)),
                        "after":copy.deepcopy(after.get(field)),
                        **({"capacity_amendment_sha256":verified_capacity["amendment_sha256"],
                            "capacity_amendment_effect":"supersedes_old_capacity_only"}
                           if field == "actions" and capacity_changed else {}),
                        "gap_bindings": [{"case_id":cid,"sources":links[cid]} for cid in sorted(relevant_cases)],
                        "source_attempt":max(ref["attempt"] for cid in new_case_ids for ref in links[cid])})
        for step_id in new_ids:
            if step_id in added_ids:
                case_ids = sorted(new_by_id[step_id]["case_ids"])
                changes.append({"id":"execution-add:"+step_id, "step_id":step_id, "field_path":"steps",
                    "before":None, "after":copy.deepcopy(new_by_id[step_id]),
                    "gap_bindings":[{"case_id":cid,"sources":links[cid]} for cid in case_ids],
                    "source_attempt":max(ref["attempt"] for cid in case_ids for ref in links[cid])})
        normalized["steps"] = copy.deepcopy(proposed_steps)
    else:
        additions = proposed_steps[len(old_steps):] if isinstance(proposed_steps, list) else []
        if additions and (gap_sources or criterion_sources):
            original_step_ids = {row.get("id") for row in old_steps if isinstance(row, dict)}
            linked = set()
            for step in additions:
                ids = set(step.get("case_ids", [])) if isinstance(step, dict) else set()
                if not ids or not ids <= new_case_ids or step.get("id") in original_step_ids:
                    return None
                linked.update(ids)
                changes.append({"id": "execution-add:" + step["id"], "step_id": step["id"],
                                "field_path": "steps", "before": None, "after": copy.deepcopy(step),
                                "source_attempt": (criterion_sources[-1]["attempt"] if criterion_sources
                                                   else gap_sources[-1]["attempt"])})
            if linked != new_case_ids:
                return None
            if criterion_sources:
                uncovered = {criterion_id for source in criterion_sources
                             for criterion_id in source.get("criterion_ids", [])
                             if isinstance(criterion_id, str)}
                criteria_to_requirements = {
                    criterion.get("id"): requirement.get("id")
                    for requirement in previous_plan.get("requirements", []) if isinstance(requirement, dict)
                    for criterion in requirement.get("acceptance_criteria", []) if isinstance(criterion, dict)
                }
                if not uncovered or not uncovered <= set(criteria_to_requirements):
                    return None
                new_cases = {case.get("id"): case for case in plan.get("cases", [])
                             if isinstance(case, dict) and case.get("id") in new_case_ids}
                covered = set()
                for case_id, case in new_cases.items():
                    criterion_ids = set(case.get("acceptance_criteria_ids", []))
                    linked_criteria = criterion_ids & uncovered
                    if (not linked_criteria
                            or any(criteria_to_requirements.get(cid) not in case.get("requirement_ids", [])
                                   for cid in linked_criteria)):
                        return None
                    covered.update(linked_criteria)
                    changes.append({"id": "execution-add-case:" + case_id, "case_id": case_id,
                                    "field_path": "cases", "before": None, "after": copy.deepcopy(case),
                                    "criterion_ids": sorted(linked_criteria),
                                    "source_attempt": criterion_sources[-1]["attempt"]})
                if covered != uncovered:
                    return None
                if any(not (set(step.get("case_ids", [])) & new_case_ids)
                       or any(case_id not in new_cases for case_id in step.get("case_ids", []))
                       for step in additions):
                    return None
            normalized["steps"].extend(copy.deepcopy(additions))
    if normalized != proposed:
        return None
    # Clearing a gap needs independent per-gap review, even when the repair
    # affects only an existing case rather than adding a workflow step.
    remaining = set(plan.get("coverage_gaps", []))
    seen = set()
    for source in gap_sources:
        for index, gap in enumerate(source["gaps"]):
            if gap in remaining or gap in seen:
                continue
            seen.add(gap)
            related = (sorted(cid for cid in new_case_ids if any(
                ref["attempt"] == source["attempt"] and ref["gap_index"] == index and ref["gap_sha256"] == digest(gap)
                for ref in links[cid])) if recovered_v8 and new_case_ids and gap_sources else sorted(new_case_ids))
            gap_id = (f"coverage-gap:{source['attempt']}:{index}:{digest(gap)}" if recovered_v8
                      else f"coverage-gap:{source['attempt']}:{index}")
            changes.append({"id": gap_id,
                            "field_path": "coverage_gaps", "before": gap,
                            "added_case_ids": related, "source_attempt": source["attempt"]})
    return changes


def _compact_historical_revision_failures(row, baseline, *, paths_only=False):
    """Point duplicated full fields to the complete in-request history chain."""
    plan = row.get("plan_snapshot", {})
    failures = copy.deepcopy(row.get("revision_failures", []))
    mapping = {"changed_frozen_execution_plan": "execution_plan", "changed_frozen_requirement_contract": "requirements"}
    for item in failures:
        if not isinstance(item, dict):
            continue
        field = mapping.get(item.get("kind"))
        if field and isinstance(plan, dict) and item.get("actual_value") == plan.get(field):
            item["actual_value"] = {"representation": "in_request_history_plan",
                                    "plan_sha256": digest(plan), "field": field,
                                    "value_sha256": digest(plan[field])}
    return failures


def _history_patch(source, target):
    """Produce deterministic reverse operations from one complete JSON plan to another."""
    def walk(a, b, path):
        if digest(a) == digest(b):
            return []
        if isinstance(a, dict) and isinstance(b, dict):
            result = []
            for key in sorted(a.keys() | b.keys()):
                token = str(key).replace("~", "~0").replace("/", "~1")
                pointer = path + "/" + token
                if key not in b:
                    result.append(["delete", pointer])
                elif key not in a:
                    result.append(["set", pointer, copy.deepcopy(b[key])])
                else:
                    result.extend(walk(a[key], b[key], pointer))
            return result
        if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            return [change for index, (old, new) in enumerate(zip(a, b))
                    for change in walk(old, new, path + "/" + str(index))]
        return [["set", path, copy.deepcopy(b)]]
    return walk(source, target, "")


def _apply_history_patch(plan, operations):
    result = copy.deepcopy(plan)
    for op in operations:
        if not isinstance(op, list) or len(op) not in (2, 3) or op[0] not in {"set", "delete"}:
            raise RefinerError("Historical plan patch operation is malformed")
        name, pointer = op[:2]
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise RefinerError("Historical plan patch path is malformed")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
        parent = result
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        if name == "delete":
            if len(op) != 2:
                raise RefinerError("Historical delete operation has an unexpected value")
            del parent[key]
        else:
            if len(op) != 3:
                raise RefinerError("Historical set operation is missing its value")
            parent[key] = copy.deepcopy(op[2])
    return result


def _replay_history_bundle(recovery, latest_draft):
    entries = recovery.get("subsequent_controls", []) if isinstance(recovery, dict) else []
    if not entries or latest_draft is None:
        return None
    # Failure references concern requirement rows and execution plans. Preserve
    # those historical fields losslessly while keeping every complete source
    # plan and control record in the authenticated recovery evidence.
    fields = ("requirements", "execution_plan")
    def projection(plan):
        return {name: copy.deepcopy(plan[name]) for name in fields}
    plans = []
    for item in entries:
        plan = item["record"]["plan_snapshot"]
        if not plans or digest(plans[-1]) != digest(plan):
            plans.append(plan)
    base = projection(latest_draft)
    steps = []
    for plan in reversed(plans):
        target = projection(plan)
        operations = _history_patch(base, target)
        try:
            rebuilt = _apply_history_patch(base, operations)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RefinerError("Historical plan patch cannot be reconstructed") from exc
        if digest(rebuilt) != digest(target):
            raise RefinerError("Historical plan patch hash differs from its authenticated snapshot")
        steps.append({"from_projection_sha256": digest(base), "to_projection_sha256": digest(target),
                      "source_plan_sha256": digest(plan), "operations": operations})
        base = rebuilt
    return {"schema_version": "skill-refiner.in-request-history/v1",
            "base_field": "latest_unapproved_draft", "base_plan_sha256": digest(latest_draft),
            "fields": list(fields), "base_projection_sha256": digest(projection(latest_draft)),
            "instructions": "Start with the listed fields from latest_unapproved_draft. Apply each step in order to the preceding projection. Operations are [set, JSON pointer, value] or [delete, JSON pointer]. A set replaces a value or adds an object key; arrays with changed lengths are replaced as a whole. Each result must match to_projection_sha256; source_plan_sha256 identifies the complete archived draft.",
            "steps": steps}


def _recovery_feedback_material(recovery, work, inputs, bindings):
    """Recheck authenticated historical drafts with the current validator."""
    rows = recovery.get("control_records", []) if recovery else []
    if not rows:
        return {"feedback": [], "sources": [], "case_ids": [], "execution_revision": [], "reference": recovery}
    hashes = recovery.get("control_record_hashes")
    source_root = recovery.get("source_evidence_root")
    if (not isinstance(hashes, list) or len(hashes) != len(rows) or not isinstance(source_root, str)
            or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{64}", x) for x in hashes)):
        raise RefinerError("Recovered control history lacks verified source bindings")
    snapshots = []
    sources, targeted, historic_changes = [], set(), []
    feedback = list(recovery.get("validation_feedback", []))
    if any(not isinstance(item, str) for item in feedback):
        raise RefinerError("Recovered validation feedback is malformed")
    for index, row in enumerate(rows):
        plan = row.get("plan_snapshot") if isinstance(row, dict) else None
        if not isinstance(plan, dict) or row.get("plan_sha256") != digest(plan):
            raise RefinerError("Recovered control history does not bind its complete plan")
        if index:
            prior = snapshots[-1]
            if prior.get("requirements") != plan.get("requirements") or prior.get("cases") != plan.get("cases"):
                raise RefinerError("Recovered intermediate plan changes require a separate authenticated semantic review")
            prior_scope = prior.get("execution_plan", {}).get("scope", {})
            next_scope = plan.get("execution_plan", {}).get("scope", {})
            if prior_scope != next_scope:
                old_constraints = prior_scope.get("constraints", [])
                new_constraints = next_scope.get("constraints", [])
                same_other = all(prior_scope.get(key) == next_scope.get(key) for key in ("in_scope", "out_of_scope"))
                if (not same_other or not isinstance(old_constraints, list) or not isinstance(new_constraints, list)
                        or new_constraints[:len(old_constraints)] != old_constraints or len(new_constraints) != len(old_constraints) + 1):
                    raise RefinerError("Recovered historical scope change is not an independently reviewable single addition")
                source = {"kind": "historical_scope_change", "attempt": row["attempt"],
                          "before": copy.deepcopy(prior_scope), "after": copy.deepcopy(next_scope),
                          "origin": "recovery", "record_sha256": hashes[index]}
                sources.append(source)
                feedback.append("Historical execution scope changed; do not assume approval. Independently review or restore: " +
                                json.dumps(source, ensure_ascii=False))
            correction = _authorized_execution_revision(prior, plan, sources)
            if correction is None:
                raise RefinerError("Recovered intermediate execution plan changed outside verified feedback")
            # These changes remain provisional; they are reviewed against the
            # first historical baseline when the new candidate is proposed.
        snapshots.append(plan)
        app = load_response_applicability(source_root, inputs, plan, bindings)
        try:
            validate_plan(plan, work, 30, verified_response_sources=bindings,
                          required_response_contract_cases=app["required_case_ids"])
        except PlanValidationError as exc:
            historical_error = row.get("validation_error", {})
            if str(historical_error.get("message", "")).startswith("Legacy good_responses do not match positive_controls: "):
                feedback.append("Historical good_responses format diagnosis is superseded: the complete positive list and the canonical-excluded tail are both valid when ordered and identical to positive_controls. Preserve those valid examples; repair only the current controls and other verified gaps.")
            ids = sorted({item.get("case_id") for item in exc.evidence.get("control_failures", [])
                          if isinstance(item, dict) and isinstance(item.get("case_id"), str)})
            targeted.update(ids)
            sources.append({"kind": "control_failure", "attempt": row["attempt"], "case_ids": ids,
                            "origin": "recovery", "record_sha256": hashes[index]})
            feedback.append("Current full controls for recovered plan: " + str(exc))
        except RefinerError as exc:
            diagnostic = _validation_error_diagnostic(plan, exc)
            old = row.get("validation_error", {})
            if diagnostic["execution_paths"] and old.get("message") != str(exc):
                raise RefinerError("Recovered execution diagnosis does not reproduce") from exc
            if diagnostic["execution_paths"]:
                sources.append({"kind": "validation_error", "attempt": row["attempt"],
                                "case_ids": diagnostic["case_ids"], "execution_paths": diagnostic["execution_paths"],
                                "origin": "recovery", "record_sha256": hashes[index]})
                feedback.append(_validation_error_feedback(diagnostic))
            elif index == len(rows) - 1:
                raise RefinerError("Current validation of recovered plan cannot be classified safely: " + str(exc)) from exc
        else:
            feedback.append("Current full controls for recovered plan passed; historical failures remain recorded.")
    if snapshots[-1] != recovery.get("previous_plan"):
        raise RefinerError("Recovered latest plan differs from its verified control history")
    gaps = snapshots[-1].get("coverage_gaps", [])
    if gaps:
        sources.append({"kind": "coverage_gaps", "attempt": rows[-1]["attempt"], "gaps": copy.deepcopy(gaps),
                        "origin": "recovery", "record_sha256": hashes[-1]})
        feedback.append("Unresolved declared coverage gaps: " + json.dumps(gaps, ensure_ascii=False))
    for item in recovery.get("subsequent_controls", []):
        row = item.get("record") if isinstance(item, dict) else None
        sha = item.get("sha256") if isinstance(item, dict) else None
        if (not isinstance(row, dict) or row.get("plan_sha256") != digest(row.get("plan_snapshot"))
                or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)):
            raise RefinerError("Recovered subsequent diagnostics are not source bound")
        diagnostic = {"plan_sha256": row["plan_sha256"], "status": row.get("status"),
                      "validation_error": row.get("validation_error"),
                      "control_failures": row.get("control_failures", []),
                      "coverage_failures": row.get("coverage_failures", []),
                      "revision_failures": _compact_historical_revision_failures(row, snapshots[0], paths_only=True),
                      "coverage_gaps": row["plan_snapshot"].get("coverage_gaps", [])}
        feedback.append("Authenticated historical draft diagnostics: " + json.dumps(diagnostic, ensure_ascii=False))
        sources.append({"kind": "historical_draft_diagnostics", "origin": "recovery", "record_sha256": sha,
                        "plan_sha256": row["plan_sha256"]})
    if len(json.dumps(feedback, ensure_ascii=False).encode("utf-8")) > 300_000:
        raise RefinerError("Recovered complete feedback exceeds its bounded request limit")
    reference = {**recovery, "previous_plan": snapshots[0]}
    return {"feedback": feedback, "sources": sources, "case_ids": sorted(targeted),
            "execution_revision": historic_changes, "reference": reference}

def _compact_planning_feedback(feedback, recovery):
    """Replace repeated full gap strings by reversible in-request fragments.

    This is a transport encoding only: the complete original feedback remains
    in controller evidence and can be reconstructed byte-for-byte. Every
    fragment is present in the same request; no file access is needed.
    """
    if not isinstance(feedback, list) or any(not isinstance(x, str) for x in feedback):
        raise RefinerError("Planning feedback is not a string list")
    gaps = {gap for item in recovery.get("subsequent_controls", [])
            for gap in item.get("record", {}).get("plan_snapshot", {}).get("coverage_gaps", [])
            if isinstance(gap, str) and gap}
    replacements = []
    for gap in sorted(gaps, key=lambda x: (-len(x), x)):
        count = sum(text.count(gap) for text in feedback)
        if count > 1 and len(gap.encode("utf-8")) > 40:
            replacements.append(gap)
    tokens = ["<REFINER-GAP-" + str(index) + ">" for index in range(len(replacements))]
    if any(token in text for token in tokens for text in feedback):
        raise RefinerError("Planning feedback fragment token collides with source text")
    compact = list(feedback)
    fragments = []
    for token, gap in zip(tokens, replacements):
        compact = [text.replace(gap, token) for text in compact]
        fragments.append({"token": token, "text": gap,
                          "sha256": hashlib.sha256(gap.encode("utf-8")).hexdigest()})
    restored = list(compact)
    for row in fragments:
        restored = [text.replace(row["token"], row["text"]) for text in restored]
    if restored != feedback:
        raise RefinerError("Planning feedback fragment encoding did not round-trip")
    schema_path = Path(__file__).resolve().parents[2] / "schemas/v8/feedback-fragments.schema.json"
    schema = read_json(schema_path)
    fragment_record = {"schema_version": "skill-refiner.feedback-fragments/v1", "fragments": fragments,
                       "schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
                       "feedback_sha256": digest(feedback),
                       "instructions": "Replace each token wherever it appears in validation_feedback with its exact text from fragments before reasoning. Every fragment is in this request and is SHA-256 bound. This encoding preserves every original feedback entry in order."}
    import jsonschema
    jsonschema.Draft202012Validator(schema).validate(fragment_record)
    return compact, fragment_record


def _verified_planning_capacity(work, recovery, max_cases):
    """Expose only a hash-bound, source-matched capacity amendment to planning."""
    root = Path(work)
    contract_path = root / "resume-contract.json"
    amendment_path = root / "refine-budget-amendment.json"
    if not contract_path.exists():
        if amendment_path.exists():
            raise RefinerError("Capacity amendment lacks a resume contract")
        return None
    for item in (root, contract_path, amendment_path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RefinerError("Planning capacity evidence uses a linked or reparse path")
    contract = read_json(contract_path)
    expected = contract.get("refine_budget_amendment_sha256")
    if not expected:
        if amendment_path.exists():
            raise RefinerError("Unbound capacity amendment cannot be used for planning")
        return None
    if not isinstance(recovery, dict) or not contract.get("refine_plan_recovery_sha256"):
        raise RefinerError("Capacity amendment requires verified plan recovery")
    try:
        raw = amendment_path.read_bytes()
    except OSError as exc:
        raise RefinerError("Planning capacity amendment is missing or unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RefinerError("Planning capacity amendment differs from the resume contract")
    amendment = json.loads(raw)
    import jsonschema
    schema_path = Path(__file__).resolve().parents[2] / "schemas/v1/refine-budget-amendment.schema.json"
    if hashlib.sha256(schema_path.read_bytes()).hexdigest() != contract.get("refine_budget_amendment_schema_sha256"):
        raise RefinerError("Planning capacity amendment schema differs from the frozen contract")
    jsonschema.Draft202012Validator(read_json(schema_path)).validate(amendment)
    limits, options = contract.get("limits", {}), contract.get("options", {})
    effective_limits = contract.get("effective_limits", limits)
    assessment = amendment["capacity_assessment"]
    historical = {"development_cases": amendment["new_max_cases"], "acceptance_cases": amendment["acceptance_cases"],
                  "max_evaluations": amendment["new_max_evaluations"]}
    current = {**historical, "max_evaluations": effective_limits.get("max_evaluations", historical["max_evaluations"])}
    if (max_cases != current["development_cases"] or limits.get("max_cases") != max_cases
            or options.get("max_cases") != max_cases or options.get("acceptance_cases") != current["acceptance_cases"]
            or limits.get("acceptance_cases") != current["acceptance_cases"]
            or limits.get("max_evaluations") != historical["max_evaluations"]
            or current["max_evaluations"] > historical["max_evaluations"]
            or type(current["max_evaluations"]) is not int or current["max_evaluations"] < 1
            or assessment.get("authorized_limits") != {"max_cases": max_cases,
                "acceptance_cases": historical["acceptance_cases"], "max_evaluations": historical["max_evaluations"]}):
        raise RefinerError("Planning capacity differs from the authenticated amendment or active limits")
    evidence = [item for item in recovery.get("subsequent_controls", [])
                if isinstance(item, dict) and item.get("sha256") == amendment["source_control_sha256"]
                and isinstance(item.get("record"), dict)
                and digest(item["record"].get("plan_snapshot")) == amendment["source_plan_sha256"]]
    if len(evidence) != 1:
        raise RefinerError("Capacity amendment source plan is absent or ambiguous in recovered history")
    return {"amendment_sha256": expected, "source_plan_sha256": amendment["source_plan_sha256"],
            "source_control_sha256": amendment["source_control_sha256"],
            "old_limits": {"development_cases": amendment["old_max_cases"],
                           "max_evaluations": amendment["old_max_evaluations"]},
            "current_limits": current, "assessment": copy.deepcopy(assessment),
            "historical_capacity_gap": amendment["capacity_gap"],
            "interpretation": "The old case-capacity and evaluation limits are superseded by current_limits. Historical feedback about the old capacity is preserved for provenance only; reassess remaining coverage under current_limits. This amendment does not approve the plan or erase other genuine gaps."}


def _budget_cap_migration_schema():
    path = Path(__file__).resolve().parents[2] / "schemas/v1/refine-budget-cap-migration.schema.json"
    try:
        raw = path.read_bytes()
        schema = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise RefinerError("Approved budget-cap migration schema is missing or unreadable") from exc
    return path, raw, schema


def _sha256_file_bytes(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise RefinerError(f"Budget migration evidence is missing or unreadable: {path}") from exc


def _reject_budget_reparse_path(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except OSError as exc:
            raise RefinerError("Budget migration evidence path is unavailable") from exc
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RefinerError("Budget migration evidence uses a linked or reparse path")
    return path.resolve(strict=True)


def _validate_refine_budget_approval(record, source_state, source_run):
    """Validate the approval artifact and bind it to the zero-evaluation lineage state."""
    path = Path(record["approval_artifact_path"])
    if not path.is_absolute():
        raise RefinerError("Budget migration approval artifact path must be absolute")
    path = _reject_budget_reparse_path(path)
    parts = [part.casefold() for part in path.parts]
    try:
        marker = parts.index(".tasks")
    except ValueError as exc:
        raise RefinerError("Budget migration approval must come from a GoalExecutor task artifact") from exc
    if (marker + 2 >= len(parts) or parts[marker + 1] != "artifacts"
            or marker + 3 != len(parts) - 1 or path.suffix.casefold() != ".json"):
        raise RefinerError("Budget migration approval artifact must be under .tasks/artifacts/<task_id>/")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["approval_artifact_sha256"]:
        raise RefinerError("Budget migration approval artifact hash mismatch")
    try:
        approval = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RefinerError("Budget migration approval artifact is not valid JSON") from exc
    if (not isinstance(approval, dict)
            or approval.get("schema_version") != "goalexecutor.budget-approval/v1"
            or approval.get("approval_id") != record["approval_id"]
            or approval.get("decision") != "approved_limited_extension"
            or "existing create and refine run lineages" not in approval.get("scope", "")
            or "without resetting or rerunning completed stages" not in approval.get("scope", "")):
        raise RefinerError("Budget migration is not bound to an approved continuation decision")
    if (not isinstance(source_state, dict)
            or source_state.get("status") != "plan_not_passed"
            or source_state.get("workspace") != str(Path(source_run).resolve())
            or source_state.get("phase_results", {}).get("plan_gate", {}).get("status") != "not_passed"
            or source_state.get("target_evaluations") != 0
            or source_state.get("evaluation_history") not in (None, [])
            or source_state.get("active_evaluation") not in (None, {})):
        raise RefinerError("Budget-cap migration requires the same plan_not_passed, zero-evaluation run")
    try:
        accounting = _evaluation_accounting(source_run, source_state)
    except Exception as exc:
        raise RefinerError("Cannot verify zero target evaluations before budget-cap migration") from exc
    if accounting.get("total_cost") != 0 or accounting.get("unresolved_attempts"):
        raise RefinerError("Budget-cap migration is blocked after any target evaluation or unresolved reservation")
    usage = approval.get("prior_usage", {}).get("refine", {})
    if (usage.get("agent_calls") != source_state.get("agent_calls")
            or usage.get("target_evaluations") != 0
            or type(usage.get("seconds_approx")) not in (int, float)
            or abs(float(usage["seconds_approx"]) - float(source_state.get("elapsed_seconds", -1000000))) > 0.05):
        raise RefinerError("Budget approval prior usage does not match the preserved run state")
    caps = approval.get("caps", {}).get("refine", {})
    authorized = {"max_agent_calls": caps.get("cumulative_max_agent_calls"),
                  "max_seconds": caps.get("cumulative_max_seconds"),
                  "max_evaluations": caps.get("max_target_evaluations")}
    if (any(type(value) is not int or value < 1 for value in authorized.values())
            or record.get("approved_limits") != authorized):
        raise RefinerError("Budget migration limits differ from the user's approved refine caps")
    constraints = approval.get("constraints", [])
    if "Do not reset counters or rewrite historical records." not in constraints:
        raise RefinerError("Budget approval does not preserve the existing cumulative counters")
    remaining = approval.get("remaining", {}).get("refine", {})
    if (remaining.get("agent_calls") != authorized["max_agent_calls"] - usage["agent_calls"]
            or remaining.get("target_evaluations") != authorized["max_evaluations"]
            or type(remaining.get("seconds_approx")) not in (int, float)
            or abs(float(remaining["seconds_approx"]) -
                   (authorized["max_seconds"] - float(usage["seconds_approx"]))) > 0.05):
        raise RefinerError("Budget approval remaining allowance does not match its cumulative caps")
    return authorized


def _load_refine_budget_cap_migration(root, resume_contract, source_state):
    """Read an independently approved, file-hash-bound cap for a zero-evaluation run."""
    root = _reject_budget_reparse_path(root)
    migration_path = root / "refine-budget-cap-migration.json"
    contract_path = root / "resume-contract.json"
    state_path = root / "state.json"
    archive_state = root / "resume-history" / "budget-cap-source-state.json"
    archive_contract = root / "resume-history" / "budget-cap-source-resume-contract.json"
    bound_hash = resume_contract.get("refine_budget_cap_migration_sha256")
    bound_schema_hash = resume_contract.get("refine_budget_cap_migration_schema_sha256")
    if not migration_path.is_file():
        if bound_hash or bound_schema_hash:
            raise RefinerError("Resume contract references a missing budget-cap migration")
        return None
    if bool(bound_hash) != bool(bound_schema_hash):
        raise RefinerError("Resume contract has an incomplete budget-cap migration binding")
    for item in (migration_path, contract_path, state_path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RefinerError("Budget-cap migration source uses a linked or reparse path")
        _reject_budget_reparse_path(item)
    try:
        record_raw = migration_path.read_bytes()
        record = json.loads(record_raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RefinerError("Budget-cap migration record is missing, unreadable or invalid JSON") from exc
    schema_path, schema_raw, schema = _budget_cap_migration_schema()
    import jsonschema
    try:
        jsonschema.Draft202012Validator(schema).validate(record)
    except jsonschema.ValidationError as exc:
        raise RefinerError("Budget-cap migration record does not satisfy its packaged schema") from exc
    record_hash = hashlib.sha256(record_raw).hexdigest()
    schema_hash = hashlib.sha256(schema_raw).hexdigest()
    if bound_hash:
        if (record_hash != bound_hash or schema_hash != bound_schema_hash
                or not archive_state.is_file() or not archive_contract.is_file()):
            raise RefinerError("Budget-cap migration differs from its frozen resume binding")
        source_state_raw = archive_state.read_bytes()
        source_contract_raw = archive_contract.read_bytes()
        source_state = json.loads(source_state_raw)
        source_contract = json.loads(source_contract_raw)
    else:
        source_state_raw = state_path.read_bytes()
        source_contract_raw = contract_path.read_bytes()
        if json.loads(source_state_raw) != source_state:
            raise RefinerError("Budget-cap migration state object differs from its hashed source file")
        source_contract = json.loads(source_contract_raw)
        if source_contract != resume_contract:
            raise RefinerError("Budget-cap migration resume-contract object differs from its hashed source file")
        if (hashlib.sha256(source_state_raw).hexdigest() != record["source_state_sha256"]
                or hashlib.sha256(source_contract_raw).hexdigest() != record["source_resume_contract_sha256"]):
            raise RefinerError("Budget-cap migration source state or resume-contract hash mismatch")
    if (hashlib.sha256(source_state_raw).hexdigest() != record["source_state_sha256"]
            or hashlib.sha256(source_contract_raw).hexdigest() != record["source_resume_contract_sha256"]
            or record["source_run"] != str(root)
            or record["lineage_id"].casefold() != root.name.casefold()):
        raise RefinerError("Budget-cap migration is not bound to this unchanged refine lineage")
    if source_contract.get("schema_version") != "skill-refiner.resume/v1":
        raise RefinerError("Budget-cap migration source contract has an unsupported version")
    if bound_hash and any(
            resume_contract.get(field) != source_contract.get(field)
            for field in ("limits", "refine_budget_amendment_sha256",
                          "refine_budget_amendment_schema_sha256", "execution_sha256")):
        raise RefinerError("Budget-cap migration changed the historical resume contract or amendment")
    _validate_refine_budget_approval(record, source_state, root)
    return {"record": record, "record_sha256": record_hash, "schema_sha256": schema_hash,
            "approved_limits": record["approved_limits"], "first_application": not bool(bound_hash),
            "source_state_raw": source_state_raw, "source_contract_raw": source_contract_raw,
            "archive_state": archive_state, "archive_contract": archive_contract}


def _apply_refine_budget_cap_migration(root, resume_contract, previous_state, requested_limits):
    """Apply one user-approved evaluation-cap reduction while preserving historical budgets."""
    migration = _load_refine_budget_cap_migration(root, resume_contract, previous_state)
    if migration is None:
        prior_limits = resume_contract.get("effective_limits", resume_contract.get("limits", {}))
        if any(requested_limits[key] < prior_limits.get(key, 1) for key in requested_limits):
            raise RefinerError("Resume budgets cannot be lower than the original run without a bound user-approved migration")
        return False
    approved = migration["approved_limits"]
    budget_keys = ("max_agent_calls", "max_seconds", "max_evaluations")
    prior_limits = resume_contract.get("effective_limits", resume_contract.get("limits", {}))
    reduced = {key for key in requested_limits if requested_limits[key] < prior_limits.get(key, 1)}
    if migration["first_application"]:
        if (reduced != {"max_evaluations"}
                or any(requested_limits.get(key) != approved[key] for key in budget_keys)):
            raise RefinerError("New budget-cap migration must apply the exact user-approved cumulative limits")
        for key in requested_limits:
            if key != "max_evaluations" and requested_limits[key] < prior_limits.get(key, 1):
                raise RefinerError("Budget-cap migration cannot reduce other historical limits")
        if migration["archive_state"].exists() or migration["archive_contract"].exists():
            raise RefinerError("Budget-cap migration source archives already exist without a frozen binding")
        migration["archive_state"].parent.mkdir(parents=True, exist_ok=True)
        migration["archive_state"].write_bytes(migration["source_state_raw"])
        migration["archive_contract"].write_bytes(migration["source_contract_raw"])
        resume_contract["effective_limits"] = copy.deepcopy(requested_limits)
        resume_contract["refine_budget_cap_migration_sha256"] = migration["record_sha256"]
        resume_contract["refine_budget_cap_migration_schema_sha256"] = migration["schema_sha256"]
        return True
    if reduced:
        raise RefinerError("Resume budgets cannot be lowered after the approved cap migration")
    if any(requested_limits.get(key, 0) > approved[key] for key in budget_keys):
        raise RefinerError("Resume request exceeds the user-approved cumulative budget caps")
    effective = resume_contract.get("effective_limits")
    if (not isinstance(effective, dict)
            or any(effective.get(key) != approved[key] for key in budget_keys)):
        raise RefinerError("Resume contract active limits differ from the approved cap migration")
    resume_contract["effective_limits"] = copy.deepcopy(requested_limits)
    return True


def _uncovered_acceptance_criterion_ids(evidence):
    """Return the stable atomic-criterion gap set from machine control evidence."""
    values = set()
    for row in evidence.get("coverage_failures", []) if isinstance(evidence, dict) else []:
        if not isinstance(row, dict) or row.get("field_path") != "requirements[].acceptance_criteria[].case_coverage":
            continue
        actual = row.get("actual_value")
        ids = actual.get("uncovered_criterion_ids") if isinstance(actual, dict) else None
        if isinstance(ids, list):
            values.update(value for value in ids if isinstance(value, str) and value)
    return sorted(values)


def _criterion_coverage_repair_request(plan, criterion_ids):
    """Build deterministic, requirement-backed case instructions for missing IDs."""
    requirements = {row.get("id"): row for row in plan.get("requirements", []) if isinstance(row, dict)}
    criteria = {}
    for req_id, row in requirements.items():
        for criterion in row.get("acceptance_criteria", []):
            if isinstance(criterion, dict) and isinstance(criterion.get("id"), str):
                criteria[criterion["id"]] = (req_id, criterion)
    used_ids = {row.get("id") for row in plan.get("cases", []) if isinstance(row, dict)}
    numeric_ids = [int(match.group(1)) for value in used_ids
                   if isinstance(value, str) and (match := re.fullmatch(r"c([1-9][0-9]*)", value))]
    next_number = max(numeric_ids, default=0) + 1
    rows = []
    for criterion_id in criterion_ids:
        req_id, criterion = criteria.get(criterion_id, (None, {}))
        while f"c{next_number}" in used_ids:
            next_number += 1
        case_id = f"c{next_number}"
        used_ids.add(case_id)
        next_number += 1
        rows.append({"criterion_id": criterion_id, "requirement_id": req_id,
                     "statement": criterion.get("statement", ""),
                     "evidence": criterion.get("evidence", ""),
                     "suggested_case_id": case_id})
    return {"schema_version": "skill-refiner.criterion-coverage-repair/v1",
            "uncovered_criterion_ids": list(criterion_ids),
            "required_case_repairs": rows,
            "instructions": [
                "Add one distinct, executable case for every listed criterion, using the suggested case ID unless it conflicts with an existing ID.",
                "Each case must link the exact criterion ID and its parent requirement ID, with concrete inputs, an observable expected outcome, and a forbidden outcome.",
                "Include positive controls and a negative control that falsifies the same semantic assertion; preserve all prior cases and requirements.",
                "Do not claim coverage solely through coverage_gaps, prose, stage_contracts, or an unexecuted self-review declaration.",
                "If evidence does not support an executable case, retain the gap and stop with plan_not_passed rather than inventing behavior."
            ]}


def _load_local_plan_attempt_history(work, suite):
    """Read and verify plan-control attempts already persisted in this run directory."""
    attempts_dir = Path(work) / "plan-controls" / "attempts"
    if not attempts_dir.exists():
        return []
    for item in (Path(work), attempts_dir):
        try:
            info = item.lstat()
        except OSError as exc:
            raise PlanGateError("Local plan-control history is unavailable") from exc
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PlanGateError("Local plan-control history uses a linked or reparse path")
    paths = []
    for path in attempts_dir.glob(f"{suite}-*.json"):
        match = re.fullmatch(re.escape(suite) + r"-([0-9]+)\.json", path.name)
        if match:
            paths.append((int(match.group(1)), path))
    records = []
    for number, path in sorted(paths):
        try:
            info = path.lstat()
        except OSError as exc:
            raise PlanGateError("Local plan-control attempt is missing or unreadable") from exc
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PlanGateError("Local plan-control attempt uses a linked or reparse path")
        record = read_json(path)
        if (not isinstance(record, dict)
                or record.get("schema_version") != "skill-refiner.plan-controls/v1"
                or record.get("suite") != suite or record.get("attempt") != number
                or record.get("status") not in {"failed", "invalid_coverage", "validation_error", "passed"}
                or not isinstance(record.get("plan_snapshot"), dict)
                or record.get("plan_sha256") != digest(record["plan_snapshot"])):
            raise PlanGateError(f"Local plan-control attempt {path.name} is not bound to its complete plan")
        records.append(record)
    return records


def make_plan(agent, context, work, max_cases, role="plan", max_attempts=2,
              *, phase_callback=None, suite="development", on_self_check_passed=None, recovery=None):
    class _PlanInfrastructureFailure(Exception):
        def __init__(self, original):
            self.original = original

    feedback = []
    feedback_targets = set()
    feedback_sources = []
    previous_plan = None
    latest_draft = None
    latest_draft_attempt = None
    provisional_case_ids = set()
    provisional_sources = {}
    uncovered_criterion_fingerprints = set()
    pending_criterion_coverage_repair = None
    use_response_contracts = context.get("_response_contract_v7") is True
    use_planned_dependencies = context.get("_planned_target_dependencies_v8") is True
    if use_planned_dependencies and not use_response_contracts:
        raise RefinerError("v8 planned dependencies require the v7 response-contract gate")
    plan_schema = (PLAN_V8 if use_planned_dependencies else PLAN_V7 if use_response_contracts else
                   PLAN_V6 if context.get("business_analysis", {}).get("schema_version") in
                   ("skill-refiner.analysis/v3", "skill-refiner.analysis/v4") else PLAN)
    def phase(name, status, **detail):
        if phase_callback:
            if name == "plan_gate":
                phase_callback(name, status, **detail)
            else:
                phase_callback(name, status, suite=suite, **detail)
    def preserve_draft(plan, attempt_number):
        nonlocal latest_draft, latest_draft_attempt
        path = Path(work) / "plan-controls" / "attempts" / f"{suite}-{attempt_number:02}.json"
        if not path.is_file():
            raise PlanGateError("Cannot continue a failed plan without its complete control-attempt checkpoint")
        record = read_json(path)
        if record.get("plan_sha256") != digest(plan) or record.get("plan_snapshot") != plan:
            raise PlanGateError("Failed plan checkpoint does not bind its complete draft")
        latest_draft = copy.deepcopy(plan)
        latest_draft_attempt = attempt_number
        provisional_sources.update({row["id"]: {"case_id": row["id"], "source_plan_sha256": digest(plan), "case": copy.deepcopy(row)}
                                    for row in plan.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)})
        provisional_case_ids.update(row["id"] for row in plan.get("cases", [])
                                    if isinstance(row, dict) and isinstance(row.get("id"), str))
    try:
        source_bindings, source_inputs = (verified_response_sources(work, context, include_inputs=True)
                                         if use_response_contracts else (None, None))
    except Exception as exc:
        phase("plan_generation", "error", attempt=0, reason=str(exc))
        raise
    recovery_material = (_recovery_feedback_material(recovery, work, source_inputs, source_bindings)
                         if recovery and recovery.get("control_records") and use_response_contracts and suite == "development"
                         else {"feedback": [], "sources": [], "case_ids": [], "execution_revision": [], "reference": recovery})
    feedback.extend(recovery_material["feedback"])
    feedback_sources.extend(recovery_material["sources"])
    feedback_targets.update(recovery_material["case_ids"])
    planning_capacity = (_verified_planning_capacity(work, recovery, max_cases)
                         if recovery and suite == "development" else None)
    replay_plans = []
    replay_control_records = []
    historical_replay_count = 0
    if recovery and (recovery.get("pending_plan") is not None or recovery.get("subsequent_controls")):
        if not use_response_contracts or suite != "development":
            raise PlanGateError("Authenticated draft replay requires the v7 development self-check")
        entries = recovery.get("subsequent_controls")
        if entries is None:
            entries = [{"record": recovery.get("interrupted_control_record"),
                        "sha256": recovery.get("interrupted_control_sha256"),
                        "source_evidence_root": recovery.get("pending_source_evidence_root")}]
        if not isinstance(entries, list) or not entries:
            raise PlanGateError("Recovered draft history has no authenticated controls")
        import jsonschema
        for entry in entries:
            row = entry.get("record") if isinstance(entry, dict) else None
            source_root = entry.get("source_evidence_root") if isinstance(entry, dict) else None
            if (not isinstance(row, dict) or row.get("status") not in {"failed", "invalid_coverage", "validation_error"}
                    or row.get("plan_sha256") != digest(row.get("plan_snapshot"))
                    or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                    or not isinstance(source_root, str)):
                raise PlanGateError("Recovered draft control is not bound to authenticated source evidence")
            old_plan = row["plan_snapshot"]
            try:
                jsonschema.validate(old_plan, PLAN_V7 if use_planned_dependencies and old_plan.get("schema_version") == "skill-refiner.plan/v7" else plan_schema)
            except jsonschema.ValidationError as exc:
                raise PlanGateError("Authenticated historical draft does not satisfy the plan schema") from exc
            mechanical = response_contract_applicability(source_inputs, old_plan, allow_unresolved=True)
            if mechanical["unresolved"]:
                load_response_applicability(source_root, source_inputs, old_plan, source_bindings)
                write_json(_response_decision_path(work, old_plan), read_json(_response_decision_path(source_root, old_plan)))
            replay_plans.append(copy.deepcopy(old_plan))
            replay_control_records.append(copy.deepcopy(row))
        historical_replay_count = len(replay_plans)
        if recovery.get("pending_plan") is not None:
            pending_request = recovery.get("pending_plan_request")
            allowed_bases = [recovery_material["reference"]["previous_plan"], recovery.get("previous_plan")] + replay_plans
            if (not isinstance(pending_request, dict) or pending_request.get("role") != role
                    or not isinstance(pending_request.get("input"), dict)
                    or not any(pending_request["input"].get("previous_plan") == item for item in allowed_bases)
                    or not isinstance(pending_request["input"].get("validation_feedback"), list)):
                raise PlanGateError("Pending plan is not bound to its authenticated interrupted history")
            try:
                jsonschema.validate(recovery["pending_plan"], PLAN_V7 if use_planned_dependencies and recovery["pending_plan"].get("schema_version") == "skill-refiner.plan/v7" else plan_schema)
            except jsonschema.ValidationError as exc:
                raise PlanGateError("Authenticated pending draft does not satisfy the plan schema") from exc
            replay_plans.append(copy.deepcopy(recovery["pending_plan"]))
            replay_control_records.append(None)
    # Old authenticated controls may already contain the same atomic coverage gap.
    # Reconstruct one deterministic repair request from the latest such draft. Legacy
    # controls did not receive this structured request, so they permit one fresh,
    # explicitly guided correction after replay. A control that records the new
    # request as already applied is terminal when it reproduces the same gap.
    authenticated_control_records = (
        recovery_material["reference"].get("control_records", [])
        if recovery and recovery.get("control_records") and use_response_contracts and suite == "development"
        else [])
    local_control_records = _load_local_plan_attempt_history(work, suite)
    local_attempt_offset = max((row["attempt"] for row in local_control_records), default=0)
    historical_records = [row for row in authenticated_control_records if isinstance(row, dict)]
    historical_records.extend(row for row in replay_control_records if isinstance(row, dict))
    historical_records.extend(local_control_records)
    latest_historical_coverage = None
    if historical_records:
        record = historical_records[-1]
        ids = _uncovered_acceptance_criterion_ids(record)
        plan_snapshot = record.get("plan_snapshot")
        if ids and isinstance(plan_snapshot, dict):
            latest_historical_coverage = (record, plan_snapshot, ids)
    if latest_historical_coverage:
        record, plan_snapshot, ids = latest_historical_coverage
        pending_criterion_coverage_repair = _criterion_coverage_repair_request(
            plan_snapshot, ids)
        if not any(source.get("kind") == "criterion_coverage" and source.get("criterion_ids") == ids
                   for source in feedback_sources):
            feedback_sources.append({"kind": "criterion_coverage", "attempt": record.get("attempt", 1),
                                 "criterion_ids": ids})
        if previous_plan is None:
            previous_plan = copy.deepcopy(plan_snapshot)
        if record in local_control_records:
            preserve_draft(plan_snapshot, record["attempt"])
        uncovered_criterion_fingerprints.add(tuple(ids))
        if (record.get("criterion_coverage_repair_applied") is True
                and record.get("criterion_coverage_ids") == ids):
            phase("plan_gate", "not_passed", reason="repeated_criterion_coverage_failure",
                  uncovered_criterion_ids=ids, attempts=record.get("attempt", 1))
            raise PlanGateError(
                "PLAN_NOT_PASSED: authenticated history shows the structured repair already reproduced "
                "uncovered acceptance-criterion IDs " + json.dumps(ids, ensure_ascii=False)
                + "; stopped before another plan-agent call")
    # Replaying authenticated already-paid drafts reconstructs evidence; it is
    # not a new planning correction and must not consume the new revision budget.
    attempt_limit = max_attempts + len(replay_plans)
    for attempt in range(attempt_limit):
        attempt_number = local_attempt_offset + attempt + 1
        is_historical_replay = attempt < historical_replay_count
        attempt_criterion_coverage_repair = (copy.deepcopy(pending_criterion_coverage_repair)
                                             if not is_historical_replay else None)
        phase("plan_generation", "running", attempt=attempt_number)
        model_feedback, feedback_fragments = (feedback, None)
        if use_planned_dependencies and recovery and recovery.get("subsequent_controls"):
            model_feedback, feedback_fragments = _compact_planning_feedback(feedback, recovery)
        try:
            plan = copy.deepcopy(replay_plans[attempt]) if attempt < len(replay_plans) else agent.ask(role, PLANNING + f" Generate 2 to {max_cases} cases, never more. " +
                         ("Before interpreting validation_feedback, replace every <REFINER-GAP-N> token with its exact text from validation_feedback_fragments; entries and original order are preserved, and all fragments are supplied in this request. " if feedback_fragments else "") +
                         ("In verified_planning_capacity, current_limits supersede the old capacity; historical budget gaps remain provenance, not current limits. " if planning_capacity is not None else "") +
                         ("Prior rejected drafts and execution-plan snapshots are not approved frozen contracts: preserve all existing requirements/cases, make source-backed corrections and submit every change to independent review. " if recovery is not None else "") +
                         ("For every case explicitly provide planned_target_dependencies as an array. For each future target file give its safe package-relative target_path, asset_kind, linked requirement_ids, verified source_refs, implementation_step_id, input_contract, output_contract, distinct positive and negative scenarios and assertion IDs. Use [] when none apply. These are planned target deliverables, not existing case fixtures: never place them into available_paths until actually present. The runtime checks the actual target, records missing dependencies as case FAIL without executing that case, and still checks behavior after files exist. " if use_planned_dependencies else "") +
                         ("Plan future required implementation before it exists: define its proposed interface, inputs, fixtures, expected outputs, source-backed assertions and delivery dependencies, without claiming that it has been created, executed or passed. Never claim a planned deliverable is already an available asset; require actual implementation and validation before evaluation. If the necessary behavior or safe observable interface cannot be grounded in supplied requirements or standards, leave a precise coverage gap. ") +
                         ("Copy the supplied contract requirement list exactly, preserving IDs and all properties. "
                          if "contract" in context else "") +
                         ("Include at least one executable case of EACH required_case_kinds category: "
                          + ", ".join(context["required_case_kinds"]) + ". "
                          if context.get("required_case_kinds") else "") +
                         ("Include one distinct executable case for each applicable required_scenario_ids entry; "
                          "copy the source scenario details and keep the scenario_id stable. "
                          if context.get("required_scenario_ids") else "") +
                         ("For each case set response_contract to null only if the sourced requirements do not specify a JSON output; otherwise provide exact schema_json derived from requirements, source_refs only from verified_response_source_ids, and explicitly justified allow_markdown_fence. Add a contract-targeted negative control plus a separate contract-valid negative for every semantic response assertion. Never adopt a conflicting existing target schema as the user requirement. " if use_response_contracts else "") +
                         ("Copy expected_execution_plan exactly; development and independent acceptance use one frozen work plan. "
                          if context.get("expected_execution_plan") is not None else "") +
                         ("Use different inputs from common examples; these are independent acceptance cases. "
                          "Copy the supplied contract requirement list exactly, preserving IDs and all properties."
                          if role == "acceptance-plan" else ""),
                         {**context, "validation_feedback": model_feedback, **({"validation_feedback_fragments": feedback_fragments} if feedback_fragments else {}), **({"verified_planning_capacity": planning_capacity} if planning_capacity is not None else {}), **(
                             {"verified_response_source_ids": sorted(source_bindings)} if source_bindings is not None else {}), **(
                             {'previous_plan': previous_plan} if previous_plan else {}), **(
                              {"required_criterion_coverage_repair": pending_criterion_coverage_repair}
                              if pending_criterion_coverage_repair else {}), **(
                             {"latest_unapproved_draft": latest_draft,
                              "latest_unapproved_draft_sha256": digest(latest_draft),
                              **({"historical_draft_reconstruction": _replay_history_bundle(recovery, latest_draft)}
                                 if recovery and recovery.get("subsequent_controls") else {}),
                              "provisional_case_ids": sorted(provisional_case_ids),
                              "draft_revision_instruction": "The latest draft is unapproved evidence, not a frozen contract. Preserve all applicable proposed cases while correcting their content and all validation_feedback against the separately supplied stable previous_plan. Explain and submit any removal for independent review; return the complete matrix."}
                             if latest_draft is not None else {}), **(
                             {"previous_plan": recovery["previous_plan"],
                              "previous_invalid_response": recovery["previous_invalid_response"],
                              "contract_errors": recovery["contract_errors"],
                              "revision_instruction": "Correct every listed schema error; preserve all prior requirements, cases and valid behavior; return the complete plan. If a case ID fails only the lowercase pattern, normalize it to its lowercase ID with no collisions, preserving its semantic identity."}
                             if recovery and not previous_plan else {})}, plan_schema)
        except RefinerError as exc:
            invalid_contract = "invalid contract" in str(exc).lower()
            phase("plan_generation", "failed" if invalid_contract else "error",
                  attempt=attempt_number, reason=str(exc))
            if invalid_contract:
                raise PlanGateError(f"Plan generation did not satisfy the JSON schema: {exc}") from exc
            raise
        current_plan = plan
        if previous_plan is None and recovery and recovery.get("control_records"):
            previous_plan = recovery_material["reference"]["previous_plan"]
        phase("plan_generation", "passed", attempt=attempt_number,
              case_ids=[case.get("id") for case in plan.get("cases", []) if isinstance(case, dict)])
        phase("plan_self_check", "running", attempt=attempt_number)
        revision_failures = []
        requirements_revision = None
        provisional_context = {"schema_version": "skill-refiner.provisional-plan/v1",
                               "source_attempt": latest_draft_attempt,
                               "latest_plan_sha256": digest(latest_draft) if latest_draft is not None else None,
                               "case_ids": sorted(provisional_case_ids)}
        current_case_ids = {row["id"] for row in plan.get("cases", [])
                            if isinstance(row, dict) and isinstance(row.get("id"), str)}
        provisional_context["removed_case_ids"] = sorted(provisional_case_ids - current_case_ids)
        provisional_context["added_case_ids"] = sorted(current_case_ids - provisional_case_ids)
        provisional_context["removed_case_sources"] = [copy.deepcopy(provisional_sources[x]) for x in provisional_context["removed_case_ids"]] if use_response_contracts else []
        provisional_context["removal_schema_sha256"] = (digest(read_json(Path(__file__).resolve().parents[2] / "schemas/v7/provisional-deletion-review.schema.json"))
                                                          if provisional_context["removed_case_sources"] else None)
        try:
            recovery_revision = None
            if recovery:
                try:
                    recovery_revision = recovery_plan_revision(recovery_material["reference"], plan)
                except RefinerError as exc:
                    revision_failures.append({"kind": "dropped_recovered_matrix_entries",
                                              "field_path": "cases[]",
                                              "expected_condition": "recover each prior case and requirement through a proven one-to-one mapping",
                                              "actual_value": str(exc)})
            if previous_plan:
                previous_requirements = {row["id"] for row in previous_plan.get("requirements", [])
                                         if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_requirements = {row["id"] for row in plan.get("requirements", [])
                                        if isinstance(row, dict) and isinstance(row.get("id"), str)}
                previous_cases = {row["id"] for row in previous_plan.get("cases", [])
                                  if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_cases = {row["id"] for row in plan.get("cases", [])
                                 if isinstance(row, dict) and isinstance(row.get("id"), str)}
                missing_requirements = sorted(previous_requirements - current_requirements)
                missing_cases = sorted(previous_cases - current_cases)
                if missing_requirements or missing_cases:
                    revision_failures.append({
                        "kind": "dropped_frozen_matrix_entries",
                        "missing_requirement_ids": missing_requirements,
                        "missing_case_ids": missing_cases,
                        "expected_condition": "preserve every prior requirement and case ID",
                        "actual_value": {"requirement_ids": sorted(current_requirements),
                                         "case_ids": sorted(current_cases)}})
                if use_response_contracts and suite == "development" and unapproved_recovery(recovery):
                    recovery_requirement_base = recovery_material["reference"]["previous_plan"]
                    try:
                        # Preserve both the original source and all later locally checked IDs.
                        requirement_revision(previous_plan, plan)
                        if plan.get("requirements") != recovery_requirement_base.get("requirements"):
                            requirements_revision = requirement_revision(recovery_requirement_base, plan)
                    except RefinerError as exc:
                        revision_failures.append({"kind": "invalid_requirement_revision", "field_path": "requirements",
                                                  "expected_condition": "preserve prior required IDs and criteria before independent review",
                                                  "actual_value": str(exc)})
                elif plan.get("requirements") != previous_plan.get("requirements"):
                    revision_failures.append({
                        "kind": "changed_frozen_requirement_contract",
                        "field_path": "requirements", "expected_condition": "byte-for-byte stable requirement rows",
                        "actual_value": plan.get("requirements")})
                feedback_text = "\n".join(feedback)
                targeted_case_ids = (previous_cases.intersection(feedback_targets) if use_response_contracts else {
                    case_id for case_id in previous_cases
                    if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(case_id)}(?![A-Za-z0-9_-])",
                                 feedback_text, flags=re.IGNORECASE)
                })
                previous_by_id = {row["id"]: row for row in previous_plan.get("cases", [])
                                  if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_by_id = {row["id"]: row for row in plan.get("cases", [])
                                 if isinstance(row, dict) and isinstance(row.get("id"), str)}
                changed_unrelated = sorted(
                    case_id for case_id, old_case in previous_by_id.items()
                    if case_id in current_by_id and case_id not in targeted_case_ids
                    and current_by_id[case_id] != old_case
                )
                if changed_unrelated:
                    revision_failures.append({
                        "kind": "modified_cases_outside_feedback_scope",
                        "field_path": "cases[]", "targeted_case_ids": sorted(targeted_case_ids),
                        "expected_condition": "all unmentioned prior cases remain unchanged",
                        "actual_value": changed_unrelated})
            if "contract" in context and plan["requirements"] != context["contract"]:
                revision_failures.append({"kind": "changed_frozen_capability_contract",
                                          "field_path": "requirements",
                                          "expected_condition": context["contract"],
                                          "actual_value": plan["requirements"]})
            if ("stage_contracts" in context
                    and plan.get("stage_contracts") != context["stage_contracts"]):
                revision_failures.append({"kind": "changed_frozen_stage_artifact_contract",
                                          "field_path": "stage_contracts",
                                          "expected_condition": "exactly preserve the frozen Skill stage/schema map",
                                          "actual_value": plan.get("stage_contracts")})
            if plan.get("coverage_gaps"):
                revision_failures.append({"kind": "declared_coverage_gaps", "gaps": copy.deepcopy(plan["coverage_gaps"])})
            current_execution_revision = _authorized_execution_revision(previous_plan, plan, feedback_sources,
                                                                        verified_capacity=planning_capacity,
                                                                        unapproved_history=unapproved_recovery(recovery))
            execution_revision = (recovery_material["execution_revision"] + current_execution_revision
                                  if current_execution_revision is not None else None)
            if execution_revision is None:
                revision_failures.append({"kind": "changed_frozen_execution_plan",
                                          "field_path": "execution_plan",
                                          "expected_condition": "preserve the complete approved step/dependency/agent/recovery plan",
                                          "actual_value": plan.get("execution_plan")})
            missing_kinds = set(context.get("required_case_kinds", [])) - {c["kind"] for c in plan["cases"]}
            if missing_kinds:
                revision_failures.append({"kind": "missing_required_scenario_categories",
                                          "field_path": "cases[].kind",
                                          "expected_condition": sorted(context.get("required_case_kinds", [])),
                                          "actual_value": sorted({c["kind"] for c in plan["cases"]})})
            base_cases = {row["id"]: row for row in (previous_plan or {}).get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
            next_cases = {row["id"]: row for row in plan.get("cases", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}
            revision_context = {"schema_version": "skill-refiner.plan-revision-context/v1",
                                "base_plan_sha256": digest(previous_plan) if previous_plan else None,
                                "feedback": list(feedback), "feedback_sources": copy.deepcopy(feedback_sources),
                                "targeted_case_ids": sorted(targeted_case_ids) if previous_plan else [],
                                "case_changes": [{"case_id": case_id, "before": base_cases.get(case_id), "after": next_cases.get(case_id)}
                                                 for case_id in sorted(base_cases.keys() | next_cases.keys())
                                                 if previous_plan and base_cases.get(case_id) != next_cases.get(case_id)],
                                "requirements_changed": bool(previous_plan and previous_plan.get("requirements") != plan.get("requirements")),
                                "execution_plan_changed": bool(previous_plan and previous_plan.get("execution_plan") != plan.get("execution_plan"))}
            if len(json.dumps(revision_context, ensure_ascii=False).encode("utf-8")) > 300_000:
                raise RefinerError("Plan revision context exceeds its bounded evidence limit")
            business_analysis = context.get("business_analysis") or {}
            expected_scenarios = business_analysis.get("scenarios") or None
            required_scenario_ids = context.get("required_scenario_ids")
            if required_scenario_ids is None and expected_scenarios:
                required_scenario_ids = [row["id"] for row in expected_scenarios if row["applicable"]]
            # Resolve source evidence and any independent applicability calls outside
            # the structure-validation boundary. Their failures are operational,
            # not feedback about the generated case matrix.
            try:
                applicability = (resolve_response_applicability(agent, work, source_inputs, plan, source_bindings)
                                 if use_response_contracts else None)
                active_sources = verified_response_sources(work, context) if use_response_contracts else None
            except Exception as exc:
                raise _PlanInfrastructureFailure(exc) from exc
            try:
                control_results = validate_plan(
                    plan, work, max_cases,
                    required_case_kinds=context.get("required_case_kinds", []),
                    required_scenario_ids=required_scenario_ids or [],
                    expected_scenarios=expected_scenarios,
                    expected_requirements=context.get("contract"),
                    expected_execution_plan=context.get("expected_execution_plan"),
                    verified_response_sources=active_sources,
                    required_response_contract_cases=(applicability["required_case_ids"] if applicability is not None else None))
            except OSError as exc:
                raise _PlanInfrastructureFailure(exc) from exc
            except PlanValidationError as exc:
                failed_evidence = {**exc.evidence, "suite": suite, "attempt": attempt_number,
                                   "plan_sha256": digest(plan), "plan_snapshot": plan,
                                   "revision_failures": revision_failures, "revision_context": revision_context,
                                   "provisional_context": provisional_context, "requirement_revision": requirements_revision, "execution_plan_revision": execution_revision}
                write_json(Path(work) / "plan-controls" / "attempts" /
                           f"{suite}-{attempt_number:02}.json", failed_evidence)
                write_json(Path(work) / "plan-controls" / f"{suite}.json", failed_evidence)
                raise
            except RefinerError as exc:
                if isinstance(exc, Cancelled):
                    raise
                diagnostic = _validation_error_diagnostic(plan, exc)
                failed_evidence = {"schema_version": "skill-refiner.plan-controls/v1", "suite": suite,
                                   "attempt": attempt_number, "status": "validation_error",
                                   "plan_sha256": digest(plan), "plan_snapshot": plan,
                                   "case_control_results": [], "control_failures": [], "coverage_failures": [],
                                   "revision_failures": revision_failures, "revision_context": revision_context,
                                   "provisional_context": provisional_context, "requirement_revision": requirements_revision, "validation_error": diagnostic, "execution_plan_revision": execution_revision}
                write_json(Path(work) / "plan-controls" / "attempts" / f"{suite}-{attempt_number:02}.json", failed_evidence)
                write_json(Path(work) / "plan-controls" / f"{suite}.json", failed_evidence)
                raise
            control_evidence = {"schema_version": "skill-refiner.plan-controls/v1", "suite": suite,
                                "attempt": attempt_number, "status": "failed" if revision_failures else "passed", "plan_sha256": digest(plan),
                                "plan_snapshot": plan,
                                "case_control_results": control_results["case_control_results"],
                                "control_failures": [], "coverage_failures": [],
                                "revision_failures": revision_failures, "revision_context": revision_context,
                                "provisional_context": provisional_context, "requirement_revision": requirements_revision, "execution_plan_revision": execution_revision}
            write_json(Path(work) / "plan-controls" / "attempts" /
                       f"{suite}-{attempt_number:02}.json", control_evidence)
            write_json(Path(work) / "plan-controls" / f"{suite}.json", control_evidence)
            if revision_failures:
                raise RefinerError("Plan revision self-check failed after full controls: "
                                   + json.dumps(revision_failures, ensure_ascii=False))
        except _PlanInfrastructureFailure as failure:
            phase("plan_self_check", "error", attempt=attempt_number, reason=str(failure.original))
            raise failure.original
        except (Cancelled, OSError) as exc:
            phase("plan_self_check", "error", attempt=attempt_number, reason=str(exc))
            raise
        except Exception as exc:
            phase("plan_self_check", "failed", attempt=attempt_number, reason=str(exc))
            uncovered_criteria = _uncovered_acceptance_criterion_ids(
                exc.evidence if isinstance(exc, PlanValidationError) else None)
            coverage_fingerprint = tuple(uncovered_criteria)
            repeated_coverage = bool(coverage_fingerprint and not is_historical_replay and
                                     coverage_fingerprint in uncovered_criterion_fingerprints)
            historical_record = (replay_control_records[attempt]
                                 if is_historical_replay and attempt < len(replay_control_records) else None)
            historical_repair_already_failed = bool(
                is_historical_replay and historical_record
                and historical_record.get("criterion_coverage_repair_applied") is True
                and historical_record.get("criterion_coverage_ids") == uncovered_criteria)
            if coverage_fingerprint and not is_historical_replay and not repeated_coverage:
                uncovered_criterion_fingerprints.add(coverage_fingerprint)
                pending_criterion_coverage_repair = _criterion_coverage_repair_request(
                    current_plan, uncovered_criteria)
                feedback_sources.append({"kind": "criterion_coverage", "attempt": attempt_number,
                                         "criterion_ids": uncovered_criteria})
            elif coverage_fingerprint and is_historical_replay:
                pending_criterion_coverage_repair = _criterion_coverage_repair_request(
                    current_plan, uncovered_criteria)
                if not any(source.get("kind") == "criterion_coverage"
                           and source.get("criterion_ids") == uncovered_criteria
                           for source in feedback_sources):
                    feedback_sources.append({"kind": "criterion_coverage", "attempt": attempt_number,
                                             "criterion_ids": uncovered_criteria})
                if attempt + 1 == historical_replay_count:
                    uncovered_criterion_fingerprints.add(coverage_fingerprint)
            if not is_historical_replay:
                attempt_record_path = Path(work) / "plan-controls" / "attempts" / f"{suite}-{attempt_number:02}.json"
                if attempt_record_path.is_file():
                    attempt_record = read_json(attempt_record_path)
                    attempt_record["criterion_coverage_ids"] = uncovered_criteria
                    attempt_record["criterion_coverage_repair_applied"] = bool(
                        attempt_criterion_coverage_repair)
                    attempt_record["criterion_coverage_repair"] = attempt_criterion_coverage_repair
                    write_json(attempt_record_path, attempt_record)
                    write_json(Path(work) / "plan-controls" / f"{suite}.json", attempt_record)
            if historical_repair_already_failed:
                phase("plan_gate", "not_passed", reason="repeated_criterion_coverage_failure",
                      uncovered_criterion_ids=uncovered_criteria, attempts=attempt_number)
                raise PlanGateError(
                    "PLAN_NOT_PASSED: recovered control already applied the structured coverage repair and "
                    "repeated uncovered acceptance-criterion IDs "
                    + json.dumps(uncovered_criteria, ensure_ascii=False)
                    + "; stopped before another plan-agent call") from exc
            if repeated_coverage:
                recorded = Path(work) / "plan-controls" / "attempts" / f"{suite}-{attempt_number:02}.json"
                if recorded.is_file():
                    preserve_draft(current_plan, attempt_number)
                phase("plan_gate", "not_passed", reason="repeated_criterion_coverage_failure",
                      uncovered_criterion_ids=uncovered_criteria, attempts=attempt_number)
                raise PlanGateError(
                    "PLAN_NOT_PASSED: repeated uncovered acceptance-criterion IDs "
                    + json.dumps(uncovered_criteria, ensure_ascii=False)
                    + "; stopped before another plan-agent call") from exc
            # One bounded correction for malformed coverage/oracles; never silently accept a bad suite.
            if attempt + 1 >= attempt_limit:
                raise PlanGateError(f"Invalid autonomous test plan: {exc}") from exc
            recorded = Path(work) / "plan-controls" / "attempts" / f"{suite}-{attempt_number:02}.json"
            if not recorded.is_file():
                # Preserve the actual validation/infrastructure failure when no
                # complete draft checkpoint could be created; do not invent one.
                raise
            diagnostic = read_json(recorded).get("validation_error")
            feedback_failures = (_compact_historical_revision_failures(
                {"plan_snapshot": current_plan, "revision_failures": revision_failures}, previous_plan)
                if recovery and recovery.get("subsequent_controls") else revision_failures)
            if diagnostic:
                feedback.append(_validation_error_feedback(diagnostic))
            elif revision_failures and str(exc).startswith("Plan revision self-check failed after full controls: "):
                feedback.append("Plan revision self-check failed after full controls: " +
                                ("see complete plan revision failures below" if recovery and recovery.get("subsequent_controls")
                                 else json.dumps(feedback_failures, ensure_ascii=False)))
            else:
                feedback.append(str(exc))
            if revision_failures:
                feedback.append("Complete plan revision failures: " + json.dumps(feedback_failures, ensure_ascii=False))
            if isinstance(exc, PlanValidationError):
                ids = sorted({row.get("case_id") for row in exc.evidence.get("control_failures", [])
                              if isinstance(row, dict) and isinstance(row.get("case_id"), str)})
                feedback_targets.update(ids)
                feedback_sources.append({"kind": "control_failure", "attempt": attempt_number, "case_ids": ids})
            elif isinstance(exc, RefinerError):
                if diagnostic is not None:
                    ids = diagnostic["case_ids"]
                    feedback_targets.update(ids)
                    feedback_sources.append({"kind": "validation_error", "attempt": attempt_number, "case_ids": ids,
                                             "execution_paths": diagnostic["execution_paths"]})
            if plan.get("coverage_gaps"):
                gaps = copy.deepcopy(plan["coverage_gaps"])
                feedback.append("Unresolved declared coverage gaps: " + json.dumps(gaps, ensure_ascii=False))
                feedback_sources.append({"kind": "coverage_gaps", "attempt": attempt_number, "gaps": gaps})
            preserve_draft(current_plan, attempt_number)
            # Do not let a rejected revision replace the last preserved matrix:
            # otherwise a dropped case can disappear permanently on the next try.
            if previous_plan is None:
                previous_plan = current_plan
            continue
        if attempt < historical_replay_count:
            raise PlanGateError("Recovered interrupted draft no longer reproduces its failed control; independent audit required")
        phase("plan_self_check", "passed", attempt=attempt_number,
              controls=len(control_results.get("case_control_results", [])))
        phase("independent_review", "running", attempt=attempt_number)
        if on_self_check_passed:
            on_self_check_passed(plan)
        try:
            review = agent.ask("plan-review", "Independently review the test plan against requirements and "
                           "the source Skill. For every requirement_revision change independently assess exact before and after, cite an exact frozen normative source quote with its sha256, and return requirement_revision_assessments bound to both plan hashes; reject unsupported additions or any weakening. "
                           "For each provisional_revision removed_case_sources, independently provide exactly one provisional_removal_assessments decision with matching case_id and source_plan_sha256, allow_removal, reason, and exact source/quote evidence from the supplied frozen requirements. Reject any removal that loses required coverage. Reject superficial assertions, circular implementation-derived "
                           "oracles, missing critical coverage, answer leakage in prompts, impossible fixtures, "
                           "and claims that require unprovided binary/context files. No stylistic nitpicks. "
                           "control_command is a synthetic example used only to validate grader discrimination; "
                           "it is never executed. An illustrative installed path in that field is valid when it "
                           "matches the command regex. Do not confuse it with the actual user task or executor path. "
                           "Reject invented absolute/relative path spelling requirements when both identify the installed script or fixture. "
                           "A known-good control must reflect correct behavior. For EACH case inspect the fixture "
                           "Skill instructions, supporting implementation and expected executor actions. Installed "
                           "source is immutable; task-workspace candidate copies are writable. Reject a fixture that "
                           "tells its evaluated agent to edit its own installed implementation, including indirect "
                           "repair-on-failure directions that turn the intended baseline FAIL into ERROR. Cite the "
                           "specific fixture instruction and executable behavior in case_contracts.basis, and mark "
                           "conflicting cases false. Never approve solely because the surrounding prompt prohibits "
                           "edits while the fixture itself demands them. Separately assess response_contract_consistent: "
                           "JSON and unchanged-stdout checks reject extra prose, so reject prompts allowing optional "
                           "explanations/links/summaries alongside the answer. For every tool_output_matches check, "
                           "compare good_response with the complete observed output justified by the supplied source, "
                           "fixture or observed run evidence; reject every expected_exit_code=0 value because a success "
                           "status is sufficient and adapters may omit numeric codes. When a command legitimately exits nonzero, ensure the selected "
                           "adapter exposes its exact status or require a helper that records actual subprocess argv, "
                           "exit code and stdout. Reject partial JSON controls even if individual field "
                           "checks pass. For dynamic values such as saved paths, prefer json_path_matches_file "
                           "to compare the response with captured evidence instead of checking only its shape. "
                           "For paraphrasable user-facing explanations, reject regex or contains oracles that demand "
                           "one exact adjective when equivalent wording fulfills the contract; require a justified "
                           "alternation or structure the expected choices instead. For prose in JSON string fields, "
                           "prefer json_path_regex, which matches decoded values and handles escaped quotes; reject "
                           "field regexes that stop at an escaped quote. For duration questions, accept common "
                           "wording such as 多长时间, 多长, 每段分别多长 or 需要多久 as well as 时长/秒数. Treat good_response as a "
                           "synthetic control, never as evidence of the target's output "
                           "vocabulary. Every exact status/state value must be stated in the case prompt or grounded "
                           "in the documented Skill contract; implementation literals alone do not define the "
                           "evaluated agent's response vocabulary. Otherwise reject the invented enum/check. If a test adds "
                           "a conversational JSON response schema, its prompt must state the allowed status enum. "
                           "For discovery explanations, count 'was not returned by discovery' and 'does not appear "
                           "in the catalog' as valid absence wording. For consent checks, include '有权使用', "
                           "'允许使用', and '已获得授权' as equivalent permission wording. Evaluate text checks "
                           "against the complete meaning required, rather than requiring the synthetic good_response "
                           "wording. When an alternative media mode needs user consent, ensure the question tells "
                           "the user what material or capability will be dropped and retains the requested subject "
                           "when the available source gives it. "
                           "For missing media quantity, accept wording such as '生成段数', '生成几段', '每段分别多长' "
                           "or '每段的时长', not only "
                           "the generic noun '数量'. When the supplied reference URL is explicitly identified as "
                           "the image input, accept 'this URL/link will not be used as a reference' or 'no image "
                           "URL/reference field will be submitted or attached' as a clear explanation that the "
                           "image is not used. Include equivalent Chinese such as '不提交图片 URL', '不附带参考图', "
                           "'不将该链接作为图片输入', '不把该链接提交为参考图', or '不支持将图片作为生成输入'; "
                           "do not require the word 'image' or 'photo' when the omitted input is explicit. "
                           "For natural-language response checks, include multiple distinct good-response paraphrase "
                           "controls, including wording likely used by another capable agent; validate every control "
                           "against every response oracle and ensure the bad control still contradicts a required behavior. "
                           "When a case requires JSON only, "
                           "verify the checks reject prose before or after the JSON object; json_path_equal alone can "
                           "pass on embedded JSON. "
                           "Assess trace_checks_observable: tool_command and tool_command_absent see direct command strings, "
                           "while tool_call_absent matches recorded tool names, including non-command tools. For a "
                           "clarification-only task that explicitly forbids every tool call, require that in the prompt "
                           "and use tool_call_absent with a nonempty-name pattern and control_tool_name; this proves "
                           "no observed tool action occurred. It does not prove OS-level network isolation for an "
                           "allowed command. A CLI invoked through a helper's Python "
                           "subprocess argv or heredoc is not observable as a literal shell CLI command. If that is "
                           "allowed by the task, an inner-command regex makes a valid execution fail; instead check "
                           "the visible helper and its actual captured-process assertions. Cite concrete prompt and "
                           "checker pairs in the basis. Ensure each trace check accepts every invocation path the "
                           "prompt allows. If tool_command is used for a helper, verify the case prompt requires its "
                           "standalone invocation and supplies required setup files; a heredoc or inline wrapper can "
                           "hide that invocation from the transcript. For dimensions and similar clarification "
                           "concepts, accept common user wording such as width/height and resolution, not just the "
                           "formal label. For generated media requests, verify the fixture checks all salient "
                           "user-specified attributes in the actual submitted prompt, including color when given. "
                           "For capability explanations, accept equivalent absence wording such as no i2v capability "
                           "or provides no image-to-video support when structured checks independently establish "
                           "the discovered and requested operations. "
                           "For json_path_matches_file, good_response may contain dynamic example values: the plan "
                           "validator synthesizes matching control evidence, while the runtime fixture must write "
                           "the real observed evidence. Do not demand a static expected-path fixture for dynamic "
                           "values. If file_json_path_equal checks inspect other fields in the same file, ensure the "
                           "known-good files fixture supplies every checked pointer consistently with good_response; "
                           "the runtime helper must overwrite/remove that control fixture and capture fresh evidence. "
                           "Approve only a useful executable suite. For EACH case set scenario_consistent, "
                           "requirement_trace_consistent, expected_outcome_consistent and machine_checks_consistent "
                           "to true only when the frozen scenario and expected_outcome align with linked requirement "
                           "IDs and their sources and every check's expected value actually verifies that outcome. "
                           "Also inspect asset_audit_consistent, matrix_complete, positive_controls_validated and "
                           "negative_controls_targeted against the supplied machine control results. Every positive "
                           "control must pass every response assertion. Every negative control must fail exactly its "
                           "declared assertion_id while all other response assertions pass. Reject a control that "
                           "fails only because a status/state field differs while its target business-meaning "
                           "assertion still passes. Confirm every previous case ID remains in a revision. "
                           "Reject approval when any case is unassessed, duplicated, unknown, or marked false. "
                           "For each case also assess acceptance_criteria_traceable, scenario_detail_complete, "
                           "and risk_coverage_complete. Map scenario_id to the matching detailed scenario; map the "
                           "exact atomic acceptance criteria to linked requirements; and require observable checks "
                           "for each expected result. Inspect preconditions, stimulus, ordered actions, expected "
                           "observables, forbidden outcomes and risk dimensions. Require separate cases when actor, "
                           "workflow state, permissions, dependencies, side effects or recovery paths change the result. "
                           "Reject paraphrased duplicates or omitted risks used to fit a case cap. If creation "
                           "business_analysis is supplied, preserve every applicable scenario ID and its frozen detail "
                           "in both development and acceptance suites; missing coverage blocks approval. "
                           + HELPER_EXECUTION_AUDIT + " "
                           + CONVERSATIONAL_JSON_AUDIT + " " + INPUT_BOUNDARY_AUDIT
                           + (" For every issue provide exactly one revision_targets entry with the zero-based issue_index, explicit affected case_ids and/or requirement_ids and/or scenario_ids, scope_known and a concrete basis. Link shared issues to all affected cases via the frozen requirements/scenarios. If the impact cannot be established, set scope_known false; never authorize all cases by default. Approved reviews with no issues use an empty revision_targets array." if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else "")
                           + (" Independently review every planned target dependency: provide exactly one planned_dependency_assessments entry per (case_id, dependency_id), assess source grounding, executable interface, positive/negative discrimination, and implementation link with concrete basis; report planned_dependency_coverage_complete false if required future assets or scenarios are missing. Reject approval on any false assessment. " if plan.get("schema_version") == "skill-refiner.plan/v8" else "")
                           + (" Independently assess every v7 response_contract against the supplied source requirements and verify its scope; reject unsupported inferred fields, target schema conflicts, unjustified omissions or a null contract where JSON is required. Schema-only negative controls validate only the shared contract; each semantic negative must satisfy the contract and independently fail its target semantic assertion." if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else "")
                           + (" Assess each recovery_revision and execution_plan_revision change separately against the original requirements and structural error; return exactly one recovery_assessments entry per change_id, with preserves_requirements false for unjustified semantic weakening. Only explicitly recorded targeted changes are allowed. For every coverage-gap item verify its source requirement, original gap and actual case/check evidence; return preserves_requirements=false if the gap is not demonstrably closed, and never equate removing the gap text with coverage." if recovery_revision or execution_revision else ""),
                           {"context": context, "plan": plan, "control_results": control_results,
                            "provisional_revision": provisional_context, "latest_unapproved_draft": latest_draft,
                            "requirement_revision": requirements_revision, "normative_requirement_sources": normative_sources(source_inputs) if requirements_revision else {},
                            "previous_case_ids": sorted({row["id"] for row in previous_plan.get("cases", [])})
                           if previous_plan else sorted(recovery_revision["case_id_mapping"].values()) if recovery_revision else [],
                            **({"recovery_revision": recovery_revision} if recovery_revision else {}),
                            **({"execution_plan_revision": execution_revision} if execution_revision else {}),
                            **({"response_contract_applicability": applicability}
                               if use_response_contracts else {})},
                           requirement_review_schema(_provisional_review_schema((PLAN_REVIEW_RECOVERY_V8 if recovery_revision or execution_revision else PLAN_REVIEW_V8)
                           if plan.get("schema_version") == "skill-refiner.plan/v8" else
                           (PLAN_REVIEW_RECOVERY_V7 if recovery_revision or execution_revision else PLAN_REVIEW_V7)
                           if plan.get("schema_version") == "skill-refiner.plan/v7" else
                           (PLAN_REVIEW_RECOVERY if recovery_revision or execution_revision else PLAN_REVIEW), provisional_context), requirements_revision))
        except RefinerError as exc:
            invalid_contract = "invalid contract" in str(exc).lower()
            phase("independent_review", "failed" if invalid_contract else "error",
                  attempt=attempt_number, reason=str(exc))
            if invalid_contract:
                raise PlanGateError(f"Independent review did not satisfy its JSON schema: {exc}") from exc
            raise
        try:
            validate_plan_review(review, plan, recovery_revision, execution_revision, provisional_context,
                                 _response_source_contents(source_inputs) if use_response_contracts else None, requirements_revision, source_inputs)
        except PlanGateError:
            phase("independent_review", "failed", attempt=attempt_number, reason="unresolved revision scope")
            raise
        except RefinerError as exc:
            phase("independent_review", "failed", attempt=attempt_number, reason=str(exc))
            write_json(Path(work) / "plan-reviews" / "invalid" / f"{suite}-{attempt_number:02}.json",
                       {"schema_version": "skill-refiner.invalid-plan-review/v1", "suite": suite,
                        "attempt": attempt_number, "plan_sha256": digest(plan), "plan_snapshot": plan,
                        "review": review, "error": str(exc),
                        **({"recovery_revision": recovery_revision} if recovery_revision is not None else {}),
                        **({"execution_plan_revision": execution_revision} if execution_revision else {})})
            if attempt + 1 >= attempt_limit:
                raise PlanGateError(f"Independent test review did not pass its schema/coverage gate: {exc}") from exc
            feedback.append(str(exc))
            preserve_draft(current_plan, attempt_number)
            # A locally controlled matrix remains the baseline even when its
            # independent review is malformed; a later plan cannot drop it.
            if previous_plan is None:
                previous_plan = current_plan
            continue
        _persist_plan_review(work, suite, attempt_number, plan, review, recovery_revision=recovery_revision,
                             execution_revision=execution_revision, requirements_revision=requirements_revision)
        if review["approved"]:
            phase("independent_review", "passed", attempt=attempt_number,
                  assessed_cases=len(review["case_contracts"]))
            return plan
        phase("independent_review", "failed", attempt=attempt_number, issues=review["issues"])
        if attempt + 1 >= attempt_limit:
            raise PlanGateError("Independent test review rejected the plan: " + "; ".join(review["issues"]))
        feedback.extend(review["issues"])
        preserve_draft(current_plan, attempt_number)
        if use_response_contracts:
            affected = sorted(_review_target_cases(review, plan))
            feedback_targets.update(affected)
            feedback_sources.append({"kind": "independent_review", "attempt": attempt_number,
                                     "review_sha256": digest(review), "case_ids": affected})
        previous_plan = current_plan
    raise RefinerError("Unable to construct a valid plan")


def make_plan_suites(agent, context, work, max_cases, acceptance_cases, *, max_attempts=2,
                     phase_callback=None, agent_parallelism=2, wave_callback=None, recovery=None):
    """Plan development and acceptance; overlap only after requirements are frozen."""
    def make_acceptance(plan):
        return make_plan(agent, {**context, "contract": plan["requirements"],
                                 "stage_contracts": plan["stage_contracts"],
                                 "expected_execution_plan": plan["execution_plan"]}, work,
                         acceptance_cases, "acceptance-plan", max_attempts,
                         phase_callback=phase_callback, suite="acceptance")

    if agent_parallelism not in {1, 2}:
        raise RefinerError("The safe planning wave supports agent_parallelism 1 or 2")
    if agent_parallelism == 1:
        if wave_callback:
            wave_callback("serial", **parallel_agent_plan(1))
        development_status, acceptance_status = "not_started", "not_started"
        try:
            if wave_callback:
                wave_callback("task_started", task_id="AG-DEV-REVIEW", task_status="running")
            plan = make_plan(agent, context, work, max_cases, max_attempts=max_attempts,
                             phase_callback=phase_callback, suite="development", recovery=recovery)
            development_status = "passed"
            if wave_callback:
                wave_callback("task_finished", task_id="AG-DEV-REVIEW", task_status="passed")
                wave_callback("task_started", task_id="AG-ACCEPTANCE-PLAN", task_status="running")
            acceptance = make_acceptance(plan)
            acceptance_status = "passed"
            if wave_callback:
                wave_callback("task_finished", task_id="AG-ACCEPTANCE-PLAN", task_status="passed")
        except Exception as exc:
            if development_status == "passed":
                acceptance_status = "failed"
            else:
                development_status = "failed"
            if wave_callback:
                wave_callback("failed", task_results=[
                    {"id": "AG-DEV-REVIEW", "status": development_status,
                     **({"error": str(exc)} if development_status == "failed" else {})},
                    {"id": "AG-ACCEPTANCE-PLAN", "status": acceptance_status,
                     **({"error": str(exc)} if acceptance_status == "failed" else {})},
                ], error=str(exc))
            raise
        if wave_callback:
            wave_callback("passed", task_results=[
                {"id": "AG-DEV-REVIEW", "status": "passed"},
                {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"},
            ], development_status="passed", acceptance_status="passed")
        if plan["execution_plan"] != acceptance["execution_plan"]:
            raise PlanGateError("Development and independent acceptance plans do not share the same frozen execution_plan")
        return plan, acceptance
    acceptance_future = None
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="skill-refiner-plan") as pool:
        def start_acceptance(plan):
            nonlocal acceptance_future
            if acceptance_future is not None:
                return
            if wave_callback:
                wave_callback("running", **parallel_agent_plan(2))
            acceptance_future = pool.submit(make_acceptance, plan)

        try:
            plan = make_plan(agent, context, work, max_cases, max_attempts=max_attempts,
                             phase_callback=phase_callback, suite="development",
                             on_self_check_passed=start_acceptance, recovery=recovery)
            if wave_callback and acceptance_future is not None:
                wave_callback("task_finished", task_id="AG-DEV-REVIEW", task_status="passed")
        except Exception as exc:
            peer_error = None
            if acceptance_future is not None:
                try:
                    acceptance_future.result()
                except Exception as other:
                    peer_error = other
            if wave_callback and acceptance_future is not None:
                wave_callback("failed", error=str(peer_error or exc),
                              development_error=str(exc), acceptance_error=str(peer_error) if peer_error else None,
                              task_results=[
                                  {"id": "AG-DEV-REVIEW", "status": "failed", "error": str(exc)},
                                  {"id": "AG-ACCEPTANCE-PLAN",
                                   "status": "failed" if peer_error else "passed",
                                   **({"error": str(peer_error)} if peer_error else {})},
                              ])
            if peer_error and not isinstance(exc, Cancelled):
                raise PlanGateError(f"Development planning failed: {exc}; concurrent acceptance planning also failed: {peer_error}") from exc
            raise
        if acceptance_future is None:
            acceptance = make_acceptance(plan)
        else:
            try:
                acceptance = acceptance_future.result()
                if wave_callback:
                    wave_callback("task_finished", task_id="AG-ACCEPTANCE-PLAN", task_status="passed")
            except Exception as exc:
                if wave_callback:
                    wave_callback("failed", error=str(exc), development_status="passed", acceptance_status="failed",
                                  task_results=[{"id": "AG-DEV-REVIEW", "status": "passed"},
                                                {"id": "AG-ACCEPTANCE-PLAN", "status": "failed", "error": str(exc)}])
                raise
    if wave_callback and agent_parallelism == 2:
        wave_callback("passed", development_status="passed", acceptance_status="passed",
                      task_results=[{"id": "AG-DEV-REVIEW", "status": "passed"},
                                    {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"}])
    if plan["execution_plan"] != acceptance["execution_plan"]:
        raise PlanGateError("Development and independent acceptance plans do not share the same frozen execution_plan")
    return plan, acceptance


def _review_frozen_plan(agent, plan, context, control_results, *, phase_callback, suite):
    def phase(status, **detail):
        if phase_callback:
            phase_callback("independent_review", status, suite=suite, **detail)

    phase("running", case_ids=[case["id"] for case in plan["cases"]])
    try:
        review_schema = {"skill-refiner.plan/v2": PLAN_REVIEW_V2,
                         "skill-refiner.plan/v3": PLAN_REVIEW_V3,
                         "skill-refiner.plan/v4": PLAN_REVIEW_V4}.get(
                             plan.get("schema_version"), PLAN_REVIEW_V7
                             if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else PLAN_REVIEW)
        if plan.get("schema_version") == "skill-refiner.plan/v8":
            review_schema = PLAN_REVIEW_V8
        review = agent.ask("plan-review",
                           "Independently review this already-frozen Skill test plan against the source "
                           "requirements, scenarios, expected outcomes, assets, response assertions and "
                           "the complete positive/negative/auxiliary control results. Verify every case "
                           "exactly once. Every response assertion must express the linked business meaning; "
                           "every positive paraphrase must pass all response assertions; each negative control "
                           "must fail its declared assertion and pass every other assertion. A status-field "
                           "failure cannot substitute for detecting the intended business error. Check that all "
                           "available assets are supported by the original inventory or declared fixtures. "
                           "Do not narrow checks to a single wording when equivalent phrases meet the contract. "
                           "Return exactly the supplied PLAN_REVIEW JSON schema. Cite case and assertion IDs "
                           "in each issue. For v3 cases assess atomic-criterion traceability, concrete scenario "
                           "detail and workflow-specific risk coverage. Verify preconditions, stimulus, ordered "
                           "actions, observable expected results and forbidden outcomes; reject broad, duplicated "
                            "or paraphrased scenarios. For v4/v5/v6, independently assess every stage/artifact schema link, "
                            "safe schema path, target-runtime packaging need and all four validator lifecycle points; "
                            "set each stage_contracts review flag from evidence. For v5/v6, also inspect the full execution_plan: "
                            "assess every ordered step exactly once, verify requirement/case traceability, input/output evidence, "
                            "dependency DAG, nonoverlapping parallel write scopes, explicit serial reasons, bounded failure "
                            "attempts and same-run recovery. Set all execution_plan review flags and step_assessments from "
                            "the plan evidence; approval is allowed only when every required flag is true and the complete "
                            "plan is useful. " + HOST_SUBAGENT_TEST_REVIEW + " " +
                            SELF_REVIEW_PLAN_REVIEW + " " + LANGUAGE_PLAN_REVIEW +
                            (" For v8 assess every planned target dependency and completeness using the required structured assessment fields, with a concrete source-based basis. " if plan.get("schema_version") == "skill-refiner.plan/v8" else "") +
                            (" For v7, provide revision_targets for each issue with explicit affected cases, requirements or scenarios, scope_known and a source-based basis; use [] when there are no issues." if plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else ""),
                           {"context": context, "plan": plan, "control_results": control_results,
                            "previous_case_ids": [case["id"] for case in plan["cases"]]}, review_schema)
        _persist_plan_review(agent.work, suite, 1, plan, review)
        validate_plan_review(review, plan)
    except RefinerError as exc:
        phase("failed", reason=str(exc))
        raise PlanGateError(f"Independent review did not pass for reused {suite} plan: {exc}") from exc
    if not review["approved"]:
        phase("failed", issues=review["issues"])
        raise PlanGateError(f"Independent review rejected reused {suite} plan: " + "; ".join(review["issues"]))
    phase("passed", assessed_cases=len(review["case_contracts"]), reused=True)
    return review


def _persist_plan_review(work, suite, attempt, plan, review, *, recovery_revision=None, execution_revision=None, requirements_revision=None):
    record = {"schema_version": "skill-refiner.plan-review/v1", "suite": suite,
              "attempt": attempt, "status": "passed" if review.get("approved") else "rejected",
              "plan_sha256": digest(plan), "plan_snapshot": plan, "review": review,
              **({"recovery_revision": recovery_revision} if recovery_revision is not None else {}),
              **({"execution_plan_revision": execution_revision} if execution_revision else {}),
              **({"requirement_revision": requirements_revision} if requirements_revision else {})}
    write_json(Path(work) / "plan-reviews" / "attempts" / f"{suite}-{attempt:02}.json", record)
    if review.get("approved"):
        write_json(Path(work) / "plan-reviews" / f"{suite}.json", record)


def validate_changes(candidate, response, evidence_ids):
    import yaml
    # Protect native suite graders even when they live outside evals/.
    protected = set()
    eval_dir = Path(candidate) / "evals"
    for config in list(eval_dir.rglob("*.yaml")) + list(eval_dir.rglob("*.yml")):
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"script_path", "golden_file"} and isinstance(item, str):
                        path = Path(item)
                        if not path.is_absolute():
                            protected.add(path.as_posix())
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(yaml.safe_load(config.read_text(encoding="utf-8")))
    changes = response["changes"]
    if len(changes) > 20 or sum(len(c["content"].encode()) for c in changes) > 500_000:
        raise RefinerError("Repair exceeds bounded patch size")
    seen, validated = set(), []
    # Validate every path before writing any file.
    for change in changes:
        rel = change["path"]
        path = relative_path(candidate, rel)
        if (rel in seen or rel in protected or Path(rel).parts[0] in {"evals", "tests", ".git", ".refiner", ".codex", ".agents", ".cursor", ".claude"}
                or Path(rel).name.startswith("test_") or ".test." in Path(rel).name
                or Path(rel).stem.endswith("_test")):
            raise RefinerError(f"Repair attempted to modify protected/duplicate path: {rel}")
        if not set(change["evidence_ids"]) <= evidence_ids:
            raise RefinerError(f"Repair lacks valid failure/standards evidence: {rel}")
        if path.suffix not in {".md", ".py", ".sh", ".json", ".yaml", ".yml", ".txt", ".js", ".ts", ".toml"}:
            raise RefinerError(f"Unsupported repair file type: {rel}")
        seen.add(rel)
        validated.append((path, change))
    return validated


def expected_candidate_manifest(base, response, evidence_ids):
    base = Path(base)
    expected = manifest(base)
    for path, change in validate_changes(base, response, evidence_ids):
        expected[str(path.relative_to(base))] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()
    return expected


def apply_changes(candidate, response, evidence_ids):
    for path, change in validate_changes(candidate, response, evidence_ids):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change["content"], encoding="utf-8")


def resumable_candidate(base, response, evidence_ids, destination):
    """Reuse a complete candidate, finish a clean snapshot, or preserve partial work and retry safely."""
    base, destination = Path(base), Path(destination)
    expected = expected_candidate_manifest(base, response, evidence_ids)
    if destination.exists():
        current = manifest(destination)
        if current == expected:
            return destination
        if current == manifest(base):
            apply_changes(destination, response, evidence_ids)
            if manifest(destination) == expected:
                return destination
        attempts = destination.parent / "resume-attempts"
        attempts.mkdir(exist_ok=True)
        numbers = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                   if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
        destination = attempts / f"attempt-{max(numbers, default=0)+1:03}" / destination.name
    snapshot(base, destination)
    apply_changes(destination, response, evidence_ids)
    if manifest(destination) != expected:
        raise RefinerError("Resumed repair candidate does not match its frozen patch")
    return destination


def resumable_snapshot(source, destination, *, keep_destination=False):
    source, destination = Path(source), Path(destination)
    expected = manifest(source)
    if destination.exists():
        if manifest(destination) == expected:
            return destination
        attempts = destination.parent / "resume-attempts"
        attempts.mkdir(exist_ok=True)
        numbers = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                   if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
        attempt = attempts / f"attempt-{max(numbers, default=0)+1:03}" / destination.name
        if keep_destination:
            run_root = destination.parent.parent.resolve()
            if (destination.is_symlink() or not destination.resolve().is_relative_to(run_root)
                    or not attempt.resolve().is_relative_to(run_root)):
                raise RefinerError("Cannot safely preserve an incomplete snapshot outside its run directory")
            attempt.parent.mkdir(parents=True, exist_ok=True)
            destination.rename(attempt)
        else:
            destination = attempt
    snapshot(source, destination)
    if manifest(destination) != expected:
        raise RefinerError("Resumed snapshot differs from its source")
    return destination


def standards_review(agent, target, context, work, expected_name):
    findings = lint(target, expected_name)
    review = agent.ask("standards-review", "Review this candidate against the supplied authoring principles "
                       "and external standards. Check preservation of identity, scope, discoverable references "
                       "and correctness of script/documentation relationships. Do not require arbitrary headings, "
                       "examples, length padding or new features. Only reject concrete material violations, "
                       "quoting the file/section and applicable rule in each issue. Do not grade execution "
                       "by reading prose; behavior is evaluated separately. Inspect the independently sourced target "
                       "obligation interfaces against effective_contract without recasting controller policies as target "
                       "business requirements. Planned mechanisms are not passed runtime evidence. "
                       "Missing necessary evidence is a gap.",
                       {"files": inventory(target, max_bytes=300_000), "standards": context["standards"],
                        "requirements": context["requirements"], "static_findings": findings,
                        "effective_contract": context.get("contract"),
                        "target_obligations": context.get("business_analysis", {}).get("target_obligations")}, REVIEW)
    for i, issue in enumerate(review["issues"] if not review["approved"] else []):
        findings.append({"id": f"N-{i+1:03}", "rule": "authoring.review", "path": "SKILL.md",
                         "message": issue, "severity": "error"})
    write_json(Path(work) / "standards.json", {"findings": findings, "review": review})
    return findings


def score(result, findings):
    return (sum(r["status"] == "ERROR" for r in result["cases"]),
            sum(f["severity"] == "error" for f in findings),
            sum(not r["passed"] for r in result["cases"]))


def qualifies(result, findings, unresolved=()):
    return not unresolved and score(result, findings) == (0, 0, 0)


def source_references(rows, default_prefix):
    """Carry provenance for large source artifacts without repeating their contents."""
    result = []
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            source, content = row.get("source", f"{default_prefix}:{index+1}"), row.get("content", "")
        else:
            source, content = f"{default_prefix}:{index+1}", str(row)
        data = content.encode("utf-8")
        result.append({"source": source, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return result


def linked_source_paths(root, findings):
    """Prioritize the entrypoint, cited files and local references linked by the Skill."""
    root = Path(root)
    paths = {"SKILL.md"}
    paths.update(str(item.get("path")) for item in findings if item.get("path"))
    scan = [root / "SKILL.md"]
    scan.extend((root / "references").glob("*.md") if (root / "references").is_dir() else [])
    for source in scan:
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for ref in re.findall(r"\]\(([^)]+)\)", content):
            value = ref.split("#", 1)[0]
            if not value or "://" in value or value.startswith(("mailto:", "/")):
                continue
            candidate = (source.parent / value).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                paths.add(candidate.relative_to(root.resolve()).as_posix())
    return paths


def compare_candidate(baseline_passed, best_result, best_findings, current, current_findings,
                      best_unresolved=(), current_unresolved=()):
    protected = baseline_passed | {r["id"] for r in best_result["cases"] if r["passed"]}
    current_passed = {r["id"] for r in current["cases"] if r["passed"]}
    regressed = sorted(protected - current_passed)
    # Self-reported work remaining is not a newly observed standards violation.
    # Retain proven partial improvement, but gate final qualification separately.
    before = (*score(best_result, best_findings), len(best_unresolved))
    after = (*score(current, current_findings), len(current_unresolved))
    return not regressed and after < before, regressed


def combine_results(development, acceptance):
    """Compare a candidate across both frozen suites without conflating case IDs."""
    if acceptance is None:
        return development
    extra = []
    for row in acceptance.get("cases", []):
        case_id = row.get("case_id", row["id"])
        extra.append({**row, "id": f"acceptance::{case_id}", "case_id": case_id,
                      "suite": "acceptance"})
    return {**development, "passed": development.get("passed", False) and acceptance.get("passed", False),
            "cases": [*development.get("cases", []), *extra],
            "acceptance_report": acceptance.get("upstream_report")}


def _evaluation_suite_roots(work):
    """Only controller-owned suite locations may contribute budget evidence."""
    import re
    root = Path(work).resolve()
    suites = [root / "baseline"]
    for parent, pattern, child in ((root / "iterations", r"round-[0-9]+", "regression"),
                                   (root / "acceptance" / "attempts", r"(?:initial|round-[0-9]+)", "evaluation")):
        if parent.exists():
            if not parent.is_dir() or not parent.resolve().is_relative_to(root):
                raise RefinerError(f"Unsafe evaluation evidence directory: {parent}")
            suites.extend(path / child for path in parent.iterdir()
                          if path.is_dir() and re.fullmatch(pattern, path.name))
    result = []
    for suite in suites:
        if not suite.resolve().is_relative_to(root):
            raise RefinerError(f"Evaluation evidence is outside its workspace: {suite}")
        if suite.exists() and not suite.is_dir():
            raise RefinerError(f"Evaluation suite evidence is not a directory: {suite}")
        if suite.is_dir():
            result.append(suite)
    return sorted(result)


def _evaluation_attempt_roots(work, suite):
    import re
    root, suite = Path(work).resolve(), Path(suite)
    attempts = [suite]
    parent = suite / "resume-attempts"
    if parent.exists():
        if not parent.is_dir() or not parent.resolve().is_relative_to(root):
            raise RefinerError(f"Unsafe evaluation attempts directory: {parent}")
        attempts.extend(path for path in parent.iterdir()
                        if path.is_dir() and re.fullmatch(r"attempt-[0-9]+", path.name))
    for attempt in attempts:
        if not attempt.resolve().is_relative_to(root):
            raise RefinerError(f"Evaluation attempt is outside its workspace: {attempt}")
    return sorted(attempts)


def _budget_integer(value, label):
    if type(value) is not int or value < 0:
        raise RefinerError(f"Invalid persisted evaluation budget at {label}")
    return value


def _read_evaluation_cache(path):
    try:
        cache = read_json(path)
        if not isinstance(cache, dict) or cache.get("schema_version") != "skill-refiner.evaluation-cache/v1":
            raise ValueError("unsupported cache schema")
        _budget_integer(cache.get("evaluations_used"), str(path) + "/evaluations_used")
        for field in ("cache_sha256", "result_sha256"):
            value = cache.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"invalid {field}")
        covered = cache.get("accounted_ledger_paths", [])
        if (not isinstance(covered, list) or any(not isinstance(p, str) for p in covered)
                or len(set(covered)) != len(covered)):
            raise ValueError("invalid accounted_ledger_paths")
        return cache
    except (OSError, ValueError, TypeError) as exc:
        raise RefinerError(f"Damaged evaluation budget cache: {path}: {exc}") from exc


def _read_evaluation_cost(path):
    import jsonschema
    try:
        record = read_json(path)
        schema_path = Path(__file__).resolve().parents[2] / "schemas/v1/evaluation-cost.schema.json"
        jsonschema.Draft202012Validator(read_json(schema_path), format_checker=jsonschema.FormatChecker()).validate(record)
        if record["status"] == "running" and record["charged_count"] != record["reserved_count"]:
            raise ValueError("a running attempt must retain its entire reservation")
        return record
    except (OSError, ValueError, TypeError, jsonschema.ValidationError) as exc:
        raise RefinerError(f"Damaged evaluation cost ledger: {path}: {exc}") from exc


def _evaluation_accounting(work, state=None):
    """Deduplicate cumulative old caches and attempt ledgers; retain real usage.

    A running ledger is conservatively charged, but is not process-liveness or
    termination evidence. The caller must block recovery until it is reconciled.
    """
    root = Path(work).resolve()
    if state is None:
        try:
            state = read_json(root / "state.json") if (root / "state.json").is_file() else {}
        except (OSError, ValueError, TypeError) as exc:
            raise RefinerError(f"Damaged evaluation state: {root / 'state.json'}") from exc
    if not isinstance(state, dict):
        raise RefinerError("Persisted evaluation state must be an object")
    state_floor = _budget_integer(state.get("target_evaluations", 0), "state.target_evaluations")
    suite_costs, ledger_paths, unresolved = {}, {}, []
    latest_global_floor = 0
    for suite in _evaluation_suite_roots(root):
        label = suite.relative_to(root).as_posix()
        cache_path = suite / "resume-cache.json"
        cache = _read_evaluation_cache(cache_path) if cache_path.is_file() else None
        ledgers = {}
        for attempt in _evaluation_attempt_roots(root, suite):
            path = attempt / "evaluation-cost.json"
            if not path.is_file():
                continue
            record = _read_evaluation_cost(path)
            if record["suite"] != label:
                raise RefinerError(f"Evaluation ledger belongs to another suite: {path}")
            relative = path.relative_to(root).as_posix()
            ledgers[relative] = record
            latest_global_floor = max(latest_global_floor,
                                      record["cumulative_cost_before"] + record["charged_count"])
            if record["status"] == "running":
                unresolved.append(relative)
        covered = set(cache.get("accounted_ledger_paths", [])) if cache else set()
        if not covered <= ledgers.keys():
            raise RefinerError(f"Evaluation cache references missing or foreign cost ledgers: {cache_path}")
        cached_cost = cache["evaluations_used"] if cache else 0
        if sum(ledgers[p]["charged_count"] for p in covered) > cached_cost:
            raise RefinerError(f"Evaluation cache undercounts its covered ledgers: {cache_path}")
        suite_costs[label] = cached_cost + sum(record["charged_count"] for path, record in ledgers.items()
                                               if path not in covered)
        ledger_paths[label] = sorted(ledgers)
    return {"total_cost": max(state_floor, sum(suite_costs.values()), latest_global_floor),
            "suite_costs": suite_costs, "ledger_paths": ledger_paths,
            "unresolved_attempts": sorted(unresolved)}


def _persisted_evaluation_cost(work):
    return _evaluation_accounting(work)["total_cost"]


def _begin_evaluation_cost(work, suite, attempt, reserved_count, cumulative_cost_before):
    import re
    root, suite, attempt = Path(work).resolve(), Path(suite).resolve(), Path(attempt).resolve()
    if not suite.is_relative_to(root) or not attempt.is_relative_to(suite):
        raise RefinerError("Unsafe evaluation reservation path")
    relative_suite = suite.relative_to(root).as_posix()
    relative_attempt = attempt.relative_to(suite).parts
    if (not re.fullmatch(r"(?:baseline|iterations/round-[0-9]+/regression|acceptance/attempts/(?:initial|round-[0-9]+)/evaluation)", relative_suite)
            or (relative_attempt and (len(relative_attempt) != 2 or relative_attempt[0] != "resume-attempts"
                                      or not re.fullmatch(r"attempt-[0-9]+", relative_attempt[1])))):
        raise RefinerError("Evaluation reservation is not at a controller-owned suite attempt")
    path = attempt / "evaluation-cost.json"
    if path.exists():
        raise RefinerError(f"Refusing to overwrite an evaluation reservation: {path}")
    record = {"schema_version": "skill-refiner.evaluation-cost/v1",
              "suite": relative_suite, "status": "running",
              "reserved_count": _budget_integer(reserved_count, "reserved_count"),
              "charged_count": reserved_count,
              "cumulative_cost_before": _budget_integer(cumulative_cost_before, "cumulative_cost_before"),
              "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
    write_json(path, record)
    _read_evaluation_cost(path)
    return path


def _finish_evaluation_cost(path, charged_count, *, status, error=None):
    if status not in {"completed", "error"}:
        raise RefinerError("Evaluation completion status must be completed or error")
    record = _read_evaluation_cost(path)
    if record["status"] != "running":
        raise RefinerError(f"Evaluation attempt was already finalized: {path}")
    record.update(status=status, charged_count=_budget_integer(charged_count, "charged_count"),
                  finished_at=datetime.now(timezone.utc).isoformat())
    if error is not None:
        record["error"] = str(error)[:2000]
    write_json(path, record)
    _read_evaluation_cost(path)
    return record


def _evaluation_identity(target, cases, selected, existing, execution, checker_hash, helper_hash):
    return {"target_manifest": manifest(target), "cases_sha256": digest(cases),
            "selected": sorted(selected) if selected is not None else None,
            "existing": bool(existing), "execution_sha256": digest(execution),
            "checker_sha256": checker_hash, "python_probe_sha256": helper_hash}


def _existing_evaluation_count(target, existing=True):
    if not existing:
        return 0
    import yaml
    directory = Path(target) / "evals"
    path = directory / "eval.yaml"
    if not path.exists():
        if (directory / "evals.json").exists():
            raise RefinerError("Existing Anthropic evals.json must first be imported with skill-up import")
        return 0
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        files = value["cases"]["files"]
        if (not isinstance(value, dict) or value.get("environment", {}).get("type", "none") != "none"
                or not isinstance(files, list) or not files
                or any(not isinstance(p, str) or not p.strip() for p in files)):
            raise ValueError("invalid native evaluation suite or empty case file list")
        # Skill-up resolves case paths from the target root. Check the exact
        # counted files now, before any generated target case can be run.
        # This is a shallow integrity check, not a replacement for skill-up's
        # complete validation immediately before the native suite executes.
        root = Path(target).resolve()
        for name in files:
            raw = Path(name.replace("\\", "/"))
            case_path = (raw if raw.is_absolute() else root / raw).resolve()
            if not case_path.is_relative_to(root) or not case_path.is_file():
                raise ValueError(f"native case file is missing, unsafe or not a regular file: {name}")
            case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
            if not isinstance(case, dict) or not isinstance(case.get("input"), dict):
                raise ValueError(f"native case must be a mapping with input: {name}")
            entry = case["input"]
            prompt = entry.get("prompt")
            turns = entry.get("turns")
            if not ((isinstance(prompt, str) and prompt.strip()) or
                    (isinstance(turns, list) and bool(turns))):
                raise ValueError(f"native case must supply an input prompt or turns: {name}")
        return len(files)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as exc:
        raise RefinerError(f"Cannot reserve existing evaluation cost for {path}: {exc}") from exc


def _validated_evaluation_cache(run_dir, identity, case_ids, executor_ids, *, existing_case_count=0):
    """A charged attempt is not reusable unless every case/executor has a result."""
    cache_path, summary_path = Path(run_dir) / "resume-cache.json", Path(run_dir) / "summary.json"
    if not cache_path.is_file() or not summary_path.is_file():
        return None
    cache = _read_evaluation_cache(cache_path)
    try:
        result = read_json(summary_path)
    except (OSError, ValueError, TypeError) as exc:
        raise RefinerError(f"Damaged evaluation result: {summary_path}") from exc
    if cache["cache_sha256"] != digest(identity):
        return None
    if cache["result_sha256"] != digest(result):
        raise RefinerError(f"Evaluation result hash differs from its cache: {summary_path}")
    if not isinstance(result, dict) or not isinstance(result.get("cases"), list):
        return None
    expected = {(executor, case) for executor in executor_ids for case in case_ids}
    observed, existing_rows = set(), {executor: set() for executor in executor_ids}
    for row in result["cases"]:
        if (not isinstance(row, dict) or row.get("status") not in {"PASS", "FAIL"}
                or type(row.get("passed")) is not bool
                or row["passed"] != (row["status"] == "PASS")):
            return None
        executor = row.get("executor", "default" if executor_ids == ["default"] else None)
        case = row.get("case_id", row.get("id"))
        if executor not in executor_ids or not isinstance(case, str):
            return None
        pair = (executor, case)
        if case.startswith("existing:"):
            if case in existing_rows[executor]:
                return None
            existing_rows[executor].add(case)
        elif pair not in expected or pair in observed:
            return None
        else:
            observed.add(pair)
    if (observed != expected or any(len(rows) != existing_case_count for rows in existing_rows.values())
            or type(result.get("passed")) is not bool
            or result["passed"] != all(row["passed"] for row in result["cases"])):
        return None
    cost = (len(case_ids) + existing_case_count) * len(executor_ids)
    if cache["evaluations_used"] < cost:
        raise RefinerError(f"Reusable evaluation cache undercounts observed case results: {cache_path}")
    return result


def _pending_evaluation_cost(work, original, plan, acceptance, execution, checker_hash, helper_hash,
                             executor_ids, *, resume=False, validated_acceptance_target=None):
    """Return a lower bound after validating actual cache identities, never usage counts."""
    root = Path(work).resolve()
    baseline_native = _existing_evaluation_count(original)
    baseline_cost = (len(plan["cases"]) + baseline_native) * len(executor_ids)
    acceptance_cost = (len(acceptance["cases"]) + baseline_native) * len(executor_ids)
    reused, cache_paths = 0, []
    if resume:
        identity = _evaluation_identity(original, plan["cases"], None, True, execution, checker_hash, helper_hash)
        if _validated_evaluation_cache(root / "baseline", identity,
                                       [case["id"] for case in plan["cases"]], executor_ids,
                                       existing_case_count=baseline_native) is not None:
            reused += baseline_cost
            cache_paths.append("baseline")
        for suite in _evaluation_suite_roots(root):
            if validated_acceptance_target is None:
                break
            if "acceptance" not in suite.relative_to(root).parts:
                continue
            target_path = suite.parent / "target.json"
            if not target_path.is_file():
                continue
            try:
                target = read_json(target_path)
                if any(target.get(key) != validated_acceptance_target.get(key) for key in
                       ("candidate", "candidate_manifest", "candidate_sha256", "suite_sha256")):
                    continue
                candidate = relative_path(root, target["candidate"])
                candidate_manifest = manifest(candidate)
                if (target.get("candidate_manifest") != candidate_manifest
                        or target.get("candidate_sha256") != digest(candidate_manifest)
                        or target.get("suite_sha256") != digest(acceptance["cases"])):
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue
            native = _existing_evaluation_count(candidate)
            identity = _evaluation_identity(candidate, acceptance["cases"], None, True,
                                            execution, checker_hash, helper_hash)
            if _validated_evaluation_cache(suite, identity, [case["id"] for case in acceptance["cases"]],
                                           executor_ids, existing_case_count=native) is not None:
                # Later evaluate() checks the chosen candidate identity again.
                acceptance_cost = (len(acceptance["cases"]) + native) * len(executor_ids)
                reused += acceptance_cost
                cache_paths.append(suite.relative_to(root).as_posix())
                break
    return {"full_cost": baseline_cost + acceptance_cost, "reusable_cost": reused,
            "minimum_new_cost": baseline_cost + acceptance_cost - reused,
            "validated_cache_paths": cache_paths}


def run(args):
    config = load_config(getattr(args, "config", None))
    if config and (args.agent_command or args.engine_config or args.engine is not None):
        raise RefinerError("Use --config alone for backend selection; legacy engine/command flags cannot be mixed")
    if not config and not args.agent_command:
        raise RefinerError("Select a controller backend with --config or --agent-command")
    if not config and not args.engine_config and args.engine is None:
        raise RefinerError("Select an evaluation backend with --config, --engine-config or --engine")
    source = Path(args.target).resolve()
    if not (source / "SKILL.md").is_file():
        raise RefinerError(f"No SKILL.md in {source}")
    resume_from = Path(args.resume_from).resolve() if getattr(args, "resume_from", None) else None
    if resume_from and getattr(args, "reuse_plan", None):
        raise RefinerError("--reuse-plan starts a new run; do not combine it with --resume-from")
    standard_paths = [] if resume_from else discover_project_standards(source, getattr(args, "standard", ()))
    if not resume_from:
        for reference in args.spec + standard_paths:
            if not Path(reference).is_file():
                raise RefinerError(f"Reference file does not exist: {reference}")
    if resume_from:
        if args.output and Path(args.output).resolve() != resume_from:
            raise RefinerError("--output cannot point somewhere else when resuming; --resume-from owns the run directory")
        work = resume_from
        if not work.is_dir() or work.is_relative_to(source):
            raise RefinerError("--resume-from must name an existing run directory outside the target Skill")
    else:
        work = Path(args.output).resolve() if args.output else source.parent / (
            source.name + "-refiner-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        if work.exists() or work.is_relative_to(source):
            raise RefinerError("Output must be a new directory outside the target Skill")
        work.mkdir(parents=True)
    controller_root = Path(__file__).resolve().parent.parent
    controller_sha256 = digest({path.relative_to(controller_root).as_posix():
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(controller_root.rglob("*.py"))
        if "__pycache__" not in path.parts})
    started = time.monotonic()
    previous_state, resume_contract = None, None
    plan_recovery = None
    elapsed_before = 0.0
    if resume_from:
        contract_path = work / "resume-contract.json"
        if not contract_path.is_file():
            raise RefinerError("This run predates resumable checkpoints or has no resume contract; start a new run")
        resume_contract = read_json(contract_path)
        if resume_contract.get("schema_version") != "skill-refiner.resume/v1":
            raise RefinerError("Unsupported run resume contract")
        if resume_contract.get("controller_sha256") != controller_sha256:
            raise RefinerError("Refiner implementation changed since this run; start a new run")
        previous_state = read_json(work / "state.json")
        if resume_contract.get("refine_plan_recovery_sha256"):
            from .refine_plan_recovery import verify_recovery
            plan_recovery = verify_recovery(work, resume_contract)
            inherited = plan_recovery["usage"]
            if (previous_state.get("agent_calls", 0) < inherited["agent_calls"]
                    or previous_state.get("elapsed_seconds", 0) < inherited["elapsed_seconds"]
                    or previous_state.get("target_evaluations", 0) < inherited["target_evaluations"]):
                raise RefinerError("Recovered usage is lower than authenticated historical usage")
        if previous_state.get("source") != str(source) or resume_contract.get("source") != str(source):
            raise RefinerError("Resume target path differs from the original run")
        original_manifest = resume_contract.get("source_manifest")
        original = work / "original" / source.name
        if (manifest(source) != original_manifest or not original.is_dir()
                or manifest(original) != original_manifest):
            raise RefinerError("Resume source or immutable baseline changed; refusing to reuse checkpoints")
        context = read_json(work / "inputs.json")
        if digest(context) != resume_contract.get("inputs_sha256"):
            raise RefinerError("Resume inputs changed; start a new run with the updated requirements")
        if (context.get("_response_contract_v7") is not True and not plan_recovery
                and not (work / "oracle.lock.json").exists()):
            raise RefinerError("Legacy unfinished plan requires authenticated recovery before v7 migration")
        if plan_recovery and context.get("_response_contract_v7") is not True:
            response_contract_migration(work, context, plan_recovery, resume_contract,
                                        create=not (work / "oracle.lock.json").exists())
        elif (work / "response-contract-migration.json").exists():
            raise RefinerError("Response contract migration is not bound to an authenticated recovery")
        if (work / "oracle.lock.json").is_file():
            saved_lock = read_json(work / "oracle.lock.json")
            saved_plan = read_json(work / "test-plan.json")
            saved_acceptance = read_json(work / "acceptance-plan.json")
            if (saved_lock.get("source_manifest") != original_manifest
                    or saved_lock.get("sha256") != digest({"plan": saved_plan,
                        "acceptance": saved_acceptance, "inputs": context})):
                raise RefinerError("Frozen plans or oracle lock changed; refusing to resume")
        elapsed_before = float(previous_state.get("elapsed_seconds", 0.0))
        if previous_state.get("status") in {"passed", "unchanged"} and previous_state.get("verification_status") == "verified":
            from .gate import verify_run
            verify_run(work, deadline=started + max(0, args.max_seconds - elapsed_before))
            return previous_state
    else:
        original_manifest = manifest(source)
        original = work / "original" / source.name
        snapshot(source, original)
        context = {"files": inventory(
            original, max_bytes=275_000,
            priority_paths=("scripts/refiner/pipeline.py", "scripts/refiner/checks.py",
                            "scripts/refiner/plan.py", "scripts/refiner/schemas.py",
                            "scripts/refiner/stages.py", "SKILL.md")),
            "requirements": [], "standards": [AUTHORING]}
        context.update(getattr(args, "creation_context", {}))
        reuse_path = getattr(args, "reuse_plan", None)
        if reuse_path:
            # Preserve the exact historical input shape; in particular do not
            # insert a v7 marker into a frozen v2-v6 plan's input snapshot.
            prior_inputs = read_json(Path(reuse_path).resolve() / "inputs.json")
            if "_response_contract_v7" in prior_inputs:
                context["_response_contract_v7"] = prior_inputs["_response_contract_v7"]
            else:
                context.pop("_response_contract_v7", None)
        else:
            context["_response_contract_v7"] = True
        for path in args.spec:
            context["requirements"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text(encoding="utf-8")})
        for path in standard_paths:
            context["standards"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text(encoding="utf-8")})
        if context.get("business_analysis", {}).get("schema_version") == "skill-refiner.analysis/v4":
            frozen_creation_standards = context.get("creation_standard_sources")
            if frozen_creation_standards:
                # Inherited frozen authoring text is authoritative even after an
                # explicit controller migration. Path metadata for project files
                # remains as before so ordinary resume input checks stay strict.
                context["standards"][0] = copy.deepcopy(frozen_creation_standards[0])
        write_json(work / "inputs.json", context)
    state = {"status": "planning", "source": str(source), "workspace": str(work),
             "rounds": [], "limitations": ["Uses local environment.type=none; no container isolation",
             "Agent role separation uses fresh contexts; it is not an OS-level read barrier",
             "Declarative response/file checks only; no automatic domain-specific executable graders",
             "Small acceptance sets provide observed evidence, not statistical reliability proof"],
             "phase_results": initial_phase_results()}
    if resume_from:
        state = previous_state
        state.setdefault("rounds", [])
        history_dir = work / "resume-history"
        history_dir.mkdir(exist_ok=True)
        resume_number = len(list(history_dir.glob("state-*.json"))) + 1
        write_json(history_dir / f"state-{resume_number:03}.json", previous_state)
        stage_data = read_json(work / "stages.json")
        state.setdefault("resume_history", []).append({"resume_number": resume_number,
            "previous_status": previous_state.get("status"), "previous_active_stage": stage_data.get("active_stage"),
            "elapsed_seconds": elapsed_before})
        state.update(status="resuming", elapsed_seconds=elapsed_before)
        for key in ("error", "gaps", "verification_status", "verification_error", "final_skill"):
            state.pop(key, None)
    else:
        write_json(work / "state.json", state)
    state.setdefault("phase_results", initial_phase_results())
    state["agent_parallelism"] = getattr(args, "agent_parallelism", 2)
    agent = Agent(work, args.agent_command, args.timeout, config=config)
    agent.max_calls = args.max_agent_calls
    engine_config = read_json(args.engine_config) if args.engine_config else None
    execution = {"backend_config": config, "agent_command": args.agent_command, "engine": args.engine,
                 "engine_config": engine_config, "skill_up": args.skill_up, "timeout": args.timeout,
                 "parallelism": args.parallelism,
                 "agent_parallelism": getattr(args, "agent_parallelism", 2)}
    options = {"max_rounds": args.max_rounds, "planning_attempts": getattr(args, "planning_attempts", 2),
               "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    limits = {"max_seconds": args.max_seconds, "max_agent_calls": args.max_agent_calls,
              "max_evaluations": args.max_evaluations, "max_rounds": args.max_rounds,
              "planning_attempts": getattr(args, "planning_attempts", 2),
              "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    state["limits"] = limits
    state.setdefault("evaluation_history", [])
    if resume_from:
        if digest(execution) != resume_contract.get("execution_sha256"):
            raise RefinerError("Resume backend or execution settings differ from the original run")
        prior_options = resume_contract.get("options", {})
        if (options["max_cases"] != prior_options.get("max_cases")
                or options["acceptance_cases"] != prior_options.get("acceptance_cases")
                or options["planning_attempts"] < prior_options.get("planning_attempts", 1)
                or options["max_rounds"] < prior_options.get("max_rounds", 1)):
            raise RefinerError("Resume may increase planning/round budgets, but must keep frozen case limits")
        prior_limits = resume_contract.get("effective_limits", resume_contract.get("limits", {}))
        budget_cap_migration = _apply_refine_budget_cap_migration(
            work, resume_contract, previous_state, limits)
        if not budget_cap_migration and any(limits[name] < prior_limits.get(name, 1) for name in limits):
            raise RefinerError("Resume budgets cannot be lower than the original run; pass equal or larger limits")
        resume_contract["options"] = options
        if budget_cap_migration:
            # Keep the prior amendment's authorized capacity untouched. The
            # separate effective limit is the user's smaller active run cap.
            resume_contract["effective_limits"] = limits
        else:
            resume_contract["limits"] = limits
        write_json(work / "resume-contract.json", resume_contract)
    else:
        resume_contract = {"schema_version": "skill-refiner.resume/v1", "source": str(source),
                           "source_manifest": original_manifest, "inputs_sha256": digest(context),
                           "execution_sha256": digest(execution), "controller_sha256": controller_sha256,
                           "options": options, "limits": limits}
        write_json(work / "resume-contract.json", resume_contract)
    remaining_seconds = max(0.0, args.max_seconds - elapsed_before)
    agent.deadline = started + remaining_seconds
    stages = StageManifest(work, "refine", resume=bool(resume_from))
    stages.complete("inputs")
    print(json.dumps({"status": "started", "workspace": str(work),
                      "work_plan": str(work / "work-plan.md"),
                      "state_file": str(work / "state.json"),
                       "stage_manifest": str(work / "stages.json"), "limits": limits,
                      "max_rounds": args.max_rounds}, ensure_ascii=False), flush=True)
    locked = None
    checker_file = Path(__file__).with_name("checks.py")
    checker_hash = hashlib.sha256(checker_file.read_bytes()).hexdigest()
    response_checker_file = Path(__file__).with_name("response_contract.py")
    response_checker_hash = hashlib.sha256(response_checker_file.read_bytes()).hexdigest()
    response_contract_needed = False
    helper_file = Path(__file__).with_name('python_probe.py')
    helper_hash = hashlib.sha256(helper_file.read_bytes()).hexdigest()
    if resume_from and (work / "oracle.lock.json").is_file():
        prior_lock = read_json(work / "oracle.lock.json")
        if prior_lock.get("checker_sha256") != checker_hash:
            raise RefinerError("Resume controller grader differs from the frozen run; start a new run")
        if prior_lock.get("python_probe_sha256") != helper_hash:
            raise RefinerError("Resume Python observer differs from the frozen run; start a new run")
        if (read_json(work / "test-plan.json").get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}
                and prior_lock.get("response_contract_sha256") != response_checker_hash):
            raise RefinerError("Resume response contract validator differs from the frozen run")

    def evaluate(target, cases, run_dir, *, phase, selected=None, existing=True, round_number=0):
        if phase not in {"baseline", "regression", "acceptance"}:
            raise RefinerError("Evaluation phase must be explicit")
        if type(round_number) is not int or round_number < 0:
            raise RefinerError("Evaluation round must be a nonnegative integer")
        require_plan_gate(state)
        if response_contract_needed:
            budget()
        run_dir = Path(run_dir)
        suite_name = run_dir.relative_to(work).as_posix()
        evaluation_phase = phase
        suite = "acceptance" if phase == "acceptance" else "development"
        set_phase_result(state, "skill_evaluation", "running", suite=evaluation_phase,
                         path=suite_name)
        state["phase_results"]["skill_evaluation"]["status"] = "running"
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "phase", "phase": "skill_evaluation",
                          "status": "running", "suite": evaluation_phase}, ensure_ascii=False), flush=True)
        selected_ids = sorted(c["id"] for c in cases if selected is None or c["id"] in selected)
        cache_identity = _evaluation_identity(target, cases, selected, existing, execution, checker_hash, helper_hash)
        cache_sha = digest(cache_identity)
        cache_path, summary_path = run_dir / "resume-cache.json", run_dir / "summary.json"
        executor_ids = [profile["id"] for profile in config["executors"]] if config else ["default"]
        native_case_count = _existing_evaluation_count(target, existing)
        if resume_from:
            saved_result = _validated_evaluation_cache(run_dir, cache_identity, selected_ids,
                                                      executor_ids, existing_case_count=native_case_count)
            if saved_result is not None:
                print(json.dumps({"stage": "evaluation", "cache_hit": True,
                                  "suite": suite_name, "path": str(run_dir)}, ensure_ascii=False), flush=True)
                rows = saved_result.get("cases", [])
                state.setdefault("evaluation_history", []).append({
                    "suite": suite, "label": suite_name, "result": "cached",
                    "cache_hit": True, "cases": compact_case_rows(rows),
                    "seconds": saved_result.get("seconds", 0)})
                state["evaluation_history"] = state["evaluation_history"][-100:]
                state["target_evaluations"] = backend.evaluations
                state["agent_calls"] = agent.calls
                state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
                set_phase_result(state, "skill_evaluation",
                                 "passed" if saved_result.get("passed") else "failed",
                                 suite=evaluation_phase, cache_hit=True)
                write_json(work / "state.json", state)
                return saved_result
        accounting = _evaluation_accounting(work, state)
        if accounting["unresolved_attempts"]:
            raise RefinerError("Unresolved running evaluation reservations require verified process termination "
                               "and explicit reconciliation before recovery: "
                               + ", ".join(accounting["unresolved_attempts"]))
        run_dir.mkdir(parents=True, exist_ok=True)
        actual_dir = run_dir
        if any(run_dir.iterdir()):
            attempts = run_dir / "resume-attempts"
            attempts.mkdir(exist_ok=True)
            numbers = [int(p.name.split("-", 1)[1]) for p in attempts.glob("attempt-*")
                       if p.is_dir() and p.name.split("-", 1)[1].isdigit()]
            actual_dir = attempts / f"attempt-{max(numbers, default=0)+1:03}"
            actual_dir.mkdir()
        expected_engines = len(executor_ids)
        before = backend.evaluations
        reserved_count = (len(selected_ids) + native_case_count) * expected_engines
        if before + reserved_count > args.max_evaluations:
            raise RefinerError(f"Target-evaluation budget cannot cover suite {suite_name}: "
                               f"requires {reserved_count}, remaining {args.max_evaluations - before}")
        cost_path = _begin_evaluation_cost(work, run_dir, actual_dir, reserved_count, before)
        state["active_evaluation"] = {"suite": evaluation_phase, "round": round_number, "path": suite_name,
                                       "case_count": len(selected_ids),
                                       "case_ids": selected_ids, "started_at": datetime.now(timezone.utc).isoformat(),
                                       "executors": ([profile["id"] for profile in config["executors"]]
                                                     if config else ["default"]),
                                       "expected_case_evaluations": reserved_count,
                                       "cost_ledger": cost_path.relative_to(work).as_posix()}
        state["target_evaluations"] = backend.evaluations
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "evaluation", "status": "started", "suite": suite_name,
                          "case_ids": selected_ids, "expected_case_evaluations": len(selected_ids) * expected_engines,
                          "max_evaluations": args.max_evaluations,
                          "time_remaining_seconds": max(0, round(args.max_seconds - state["elapsed_seconds"]))},
                         ensure_ascii=False), flush=True)
        backend.case_progress_context = {"suite": state["active_evaluation"]["suite"],
                                         "round": state["active_evaluation"]["round"],
                                         **({"executor": "default"} if not config else {})}
        try:
            if config:
                result = backend.evaluate(target, cases, actual_dir, selected=selected, existing=existing)
            else:
                result = backend.evaluate(target, cases, actual_dir, selected=selected)
                prior = backend.evaluate_existing(target, actual_dir / "existing") if existing else None
                if prior:
                    result["cases"].extend(prior["cases"])
                    result["passed"] = result["passed"] and prior["passed"]
                    result["existing_report"] = prior["upstream_report"]
        except BaseException as exc:
            backend.case_progress_context = None
            _finish_evaluation_cost(cost_path, backend.evaluations - before, status="error", error=exc)
            state["active_evaluation"]["status"] = "error"
            state["active_evaluation"]["error"] = str(exc)
            state["target_evaluations"] = backend.evaluations
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            set_phase_result(state, "skill_evaluation", "error", suite=evaluation_phase,
                             error=str(exc))
            state["phase_results"]["skill_evaluation"]["status"] = "error"
            write_json(work / "state.json", state)
            raise
        charged_count = backend.evaluations - before
        result_error = any(row.get("status") == "ERROR" for row in result.get("cases", []))
        _finish_evaluation_cost(cost_path, charged_count,
                                status="error" if result_error or charged_count > reserved_count else "completed",
                                error=("Backend charged more than its reservation" if charged_count > reserved_count
                                       else "Backend returned an execution ERROR" if result_error else None))
        if charged_count > reserved_count:
            raise RefinerError("Backend evaluation cost exceeded its persisted reservation")
        write_json(summary_path, result)
        if "engines" in result:
            (run_dir / "comparison.md").write_text(comparison_markdown(result), encoding="utf-8")
        accounting = _evaluation_accounting(work, state)
        write_json(cache_path, {"schema_version": "skill-refiner.evaluation-cache/v1",
                   "cache_sha256": cache_sha, "result_sha256": digest(result),
                   "evaluations_used": accounting["suite_costs"].get(suite_name, 0),
                   "accounted_ledger_paths": accounting["ledger_paths"].get(suite_name, [])})
        state.setdefault("evaluation_history", []).append({
            "suite": suite, "label": suite_name, "result": "passed" if result.get("passed") else "failed",
            "cache_hit": False, "cases": compact_case_rows(result.get("cases", [])),
            "seconds": result.get("seconds", 0)})
        state["evaluation_history"] = state["evaluation_history"][-100:]
        backend.case_progress_context = None
        state.pop("active_evaluation", None)
        state["target_evaluations"] = backend.evaluations
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
        set_phase_result(state, "skill_evaluation",
                         "passed" if result.get("passed") else "failed",
                         suite=evaluation_phase, passed=result.get("passed"),
                         cases=len(result.get("cases", [])), cache_hit=False)
        write_json(work / "state.json", state)
        return result

    def event(status, **data):
        if status in REFINE_EVENTS:
            stage_id = REFINE_EVENTS[status]
            # A resumed controller may replay deterministic orchestration to
            # rebuild in-memory state. Keep completed stage checkpoints closed;
            # their external agent/evaluation work is loaded from validated caches.
            if not (resume_from and stages.is_completed(stage_id)):
                stages.activate(stage_id)
        state.update(status=status, agent_calls=agent.calls, cached_agent_calls=agent.cached_calls,
                     elapsed_seconds=round(elapsed_before + time.monotonic()-started, 2), **data)
        write_json(work / "state.json", state)
        print(json.dumps({"status": status, "elapsed_seconds": state["elapsed_seconds"],
                          "max_seconds": args.max_seconds, **data}, ensure_ascii=False), flush=True)

    phase_lock = threading.RLock()
    agent.bind_state(state, elapsed=lambda: elapsed_before + time.monotonic() - started,
                     lock=phase_lock)

    def phase_progress(name, status, **data):
        suite = data.pop("suite", None)
        with phase_lock:
            set_phase_result(state, name, status, suite=suite, **data)
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "phase", "phase": name, "status": status,
                              "suite": suite, **data}, ensure_ascii=False), flush=True)

    def planning_wave_progress(status, **data):
        with phase_lock:
            waves = state.setdefault("planning_waves", [])
            if status == "running":
                record = {"sequence": len(waves) + 1, "status": "running",
                          "started_at": datetime.now(timezone.utc).isoformat(),
                          "task_results": [{"id": task["id"], "status": "running"}
                                           for task in data.get("tasks", [])], **data}
                waves.append(record)
            elif status == "serial":
                waves.append({"sequence": len(waves) + 1, "status": "serial",
                              "task_results": [{"id": task["id"], "status": "pending"}
                                               for task in data.get("tasks", [])], **data})
            elif status in {"task_started", "task_finished"}:
                record = next((item for item in reversed(waves)
                               if item.get("status") in {"running", "serial"}), None)
                if record:
                    task_id = data["task_id"]
                    task_status = data["task_status"]
                    result = next((item for item in record.setdefault("task_results", [])
                                   if item.get("id") == task_id), None)
                    if result is None:
                        result = {"id": task_id}
                        record["task_results"].append(result)
                    result.update(status=task_status,
                                  **{key: value for key, value in data.items()
                                     if key not in {"task_id", "task_status"}})
            else:
                record = next((item for item in reversed(waves)
                               if item.get("status") in {"running", "serial"}), None)
                if record:
                    record.update(status=status, completed_at=datetime.now(timezone.utc).isoformat(), **data)
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "planning_wave", "status": status, **data},
                             ensure_ascii=False), flush=True)

    def budget():
        if elapsed_before + time.monotonic() - started > args.max_seconds or agent.calls > args.max_agent_calls:
            raise RefinerError("Iteration budget exhausted")
        if manifest(source) != original_manifest:
            raise RefinerError("Original Skill changed during the run; refusing adoption")
        if manifest(original) != original_manifest:
            raise RefinerError("Immutable baseline snapshot changed during the run")
        if locked and digest({"plan": read_json(work / "test-plan.json"),
                              "acceptance": read_json(work / "acceptance-plan.json"),
                              "inputs": read_json(work / "inputs.json")}) != locked:
            raise RefinerError("Frozen tests, acceptance or standards were modified")
        if hashlib.sha256(checker_file.read_bytes()).hexdigest() != checker_hash:
            raise RefinerError("Grader implementation changed during the run")
        if hashlib.sha256(helper_file.read_bytes()).hexdigest() != helper_hash:
            raise RefinerError('Frozen Python observer changed during the run')
        if response_contract_needed and hashlib.sha256(response_checker_file.read_bytes()).hexdigest() != response_checker_hash:
            raise RefinerError("Frozen response contract validator changed during the run")

    def compact_case_rows(rows):
        return [{key: row[key] for key in ("id", "case_id", "executor", "status", "passed") if key in row}
                for row in rows if isinstance(row, dict)]

    def record_round(record):
        for index, previous in enumerate(state["rounds"]):
            if (previous.get("number") == record["number"]
                    and previous.get("trigger") == record.get("trigger")):
                state["rounds"][index] = record
                break
        else:
            state["rounds"].append(record)

    def runtime_preflight(phase, *, reused=(), plans=None, planning_only=True):
        scenarios = context.get("required_scenario_ids")
        if scenarios is None and context.get("business_analysis"):
            scenarios = [row["id"] for row in context["business_analysis"]["scenarios"] if row["applicable"]]
        validated_acceptance_target = (read_json(work / "acceptance/target.json")
                                       if stages.is_completed("acceptance") else None)
        pending_cost = (_pending_evaluation_cost(
            work, original, plans[0], plans[1], execution, checker_hash, helper_hash,
            [profile["id"] for profile in config["executors"]] if config else ["default"],
            resume=bool(resume_from), validated_acceptance_target=validated_acceptance_target)
                        if plans is not None and not planning_only else None)
        reusable_cost = pending_cost["reusable_cost"] if pending_cost else 0
        if reusable_cost > backend.evaluations:
            raise RefinerError("Reusable evaluation evidence exceeds the persisted charged budget")
        reused = list(reused)
        if (pending_cost and "baseline" in pending_cost["validated_cache_paths"]
                and stages.is_completed("baseline") and "standards-review" not in reused):
            reused.append("standards-review")
        report = build_runtime_preflight(
            mode="refine", phase=phase, limits=limits,
            usage={"agent_calls": agent.calls,
                   "elapsed_seconds": elapsed_before + time.monotonic() - started,
                   "target_evaluations": backend.evaluations},
            config=config, validated_reused_steps=reused,
            applicable_scenario_count=len(scenarios) if scenarios is not None else None,
            executor_count=len(config["executors"]) if config else 1,
            plan_only=planning_only,
            validated_completed_evaluations=reusable_cost,
            planned_case_counts=({"development": len(plans[0]["cases"]),
                                  "acceptance": len(plans[1]["cases"])} if plans else None),
            launch_context=getattr(args, "launch_context", None))
        if pending_cost is not None:
            # Native existing cases are charged too, but do not enlarge the
            # generated-case caps. Keep the exact validated-cache credit separate
            # from the total budget already consumed by failed or partial attempts.
            report["minimum_required"]["case_evaluations"] = pending_cost["minimum_new_cost"]
            report["minimum_required"]["full_run_case_evaluation_floor"] = pending_cost["full_cost"]
            report["issues"] = [issue for issue in report["issues"]
                                if issue["code"] != "INSUFFICIENT_EVALUATIONS"]
            if report["remaining_budget"]["target_evaluations"] < pending_cost["minimum_new_cost"]:
                report["issues"].append({"code": "INSUFFICIENT_EVALUATIONS",
                    "field": "remaining_budget.target_evaluations",
                    "expected": pending_cost["minimum_new_cost"],
                    "actual": report["remaining_budget"]["target_evaluations"],
                    "message": "Remaining budget cannot cover uncached baseline and acceptance case/executor pairs."})
            report["warnings"].append({"code": "EVALUATION_CACHE_CREDIT_VERIFIED",
                "field": "validated_completed_evaluations", "expected": "identity/hash/complete case-executor results",
                "actual": pending_cost["validated_cache_paths"],
                "message": "Only these validated cache results reduce the minimum new evaluation cost."})
            report["status"] = "failed" if report["issues"] else "passed"
        if phase == "before-evaluation" and not planning_only:
            if plans is None or pending_cost is None:
                raise RefinerError("Frozen suites and verified evaluation accounting are required before evaluation")
            add_worst_case_evaluation_budget(
                report, development_cases=len(plans[0]["cases"]), acceptance_cases=len(plans[1]["cases"]),
                native_cases=_existing_evaluation_count(original),
                executor_count=len(config["executors"]) if config else 1, max_rounds=args.max_rounds,
                validated_baseline_reused="baseline" in pending_cost["validated_cache_paths"])
        emit_runtime_preflight(report, work, f"runtime-preflight-{phase}.json")

    try:
        backend = (Matrix(config["executors"], work, args.skill_up, args.timeout, args.parallelism) if config else
                   SkillUp(args.skill_up, args.engine, engine_config, args.timeout, args.parallelism))
        from .obligations import validate_obligation_plans
        validate_obligation_plans(context, {}, require_closed=False)
        if config:
            state["backends"] = config
            write_json(work / "backend-config.json", config)
        backend.deadline = started + remaining_seconds
        backend.max_evaluations = args.max_evaluations
        if resume_from:
            accounting = _evaluation_accounting(work, state)
            backend.evaluations = accounting["total_cost"]
            state["target_evaluations"] = backend.evaluations
            write_json(work / "state.json", state)
            if accounting["unresolved_attempts"]:
                raise RefinerError("Unresolved running evaluation reservations require verified process termination "
                                   "and explicit reconciliation before recovery: "
                                   + ", ".join(accounting["unresolved_attempts"]))
        def evaluation_progress(elapsed, remaining, case_count, executor, evaluation_count):
            active = state.get("active_evaluation")
            if active:
                active.update(heartbeat_elapsed_seconds=elapsed,
                              heartbeat_remaining_timeout_seconds=remaining,
                              current_executor=executor,
                              reserved_case_evaluations=case_count)
            state["target_evaluations"] = evaluation_count
            state["agent_calls"] = agent.calls
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
        backend.progress_callback = evaluation_progress
        event("planning")
        phase_progress("plan_gate", "running")
        reuse = getattr(args, "reuse_plan", None)
        frozen_plan = False
        if (resume_from and all((work / name).is_file() for name in
                                ("test-plan.json", "acceptance-plan.json", "oracle.lock.json"))):
            prior_lock = read_json(work / "oracle.lock.json")
            plan = read_json(work / "test-plan.json")
            acceptance = read_json(work / "acceptance-plan.json")
            expected_lock = digest({"plan": plan, "acceptance": acceptance, "inputs": context})
            if (prior_lock.get("source_manifest") != original_manifest
                    or prior_lock.get("sha256") != expected_lock
                    or prior_lock.get("checker_sha256") != checker_hash
                    or prior_lock.get("python_probe_sha256") != helper_hash):
                raise RefinerError("Frozen plans or oracle lock changed; refusing to resume")
            for suite, frozen in (("development", plan), ("acceptance", acceptance)):
                control_path = work / "plan-controls" / f"{suite}.json"
                if not control_path.is_file():
                    raise PlanGateError(f"Cannot resume: frozen {suite} plan has no completed control evidence")
                control_record = read_json(control_path)
                if (control_record.get("status") != "passed"
                        or control_record.get("plan_sha256") != digest(frozen)
                        or control_record.get("plan_snapshot") != frozen):
                    raise PlanGateError(f"Cannot resume: frozen {suite} plan control checkpoint does not match")
                for phase_name in ("plan_generation", "plan_self_check", "independent_review"):
                    phase_entry = state.get("phase_results", {}).get(phase_name, {})
                    if phase_entry.get("suites", {}).get(suite) not in {"passed", "reused"}:
                        raise PlanGateError(f"Cannot resume: {suite} {phase_name} checkpoint is incomplete")
            frozen_plan = True
            runtime_preflight("before-planning", reused=REFINE_PLANNING_STEPS,
                              plans=(plan, acceptance),
                              planning_only=bool(getattr(args, "plan_only", False)))
        elif reuse:
            previous = Path(reuse).resolve()
            prior_lock = read_json(previous / "oracle.lock.json")
            previous_inputs = read_json(previous / "inputs.json")
            plan = read_json(previous / "test-plan.json")
            acceptance = read_json(previous / "acceptance-plan.json")
            if prior_lock["source_manifest"] != original_manifest or previous_inputs != context:
                raise RefinerError("Cached plans refer to different Skill inputs or standards")
            if prior_lock["sha256"] != digest({"plan": plan, "acceptance": acceptance, "inputs": context}):
                raise RefinerError("Cached plan lock mismatch")
            if (plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}
                    and previous_inputs.get("_response_contract_v7") is not True):
                raise RefinerError("Reusing migrated v7 plans requires portable authenticated migration provenance")
            if (plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}
                    and prior_lock.get("response_contract_sha256") != response_checker_hash):
                raise RefinerError("Cached plan response contract validator changed")
            runtime_preflight("before-planning", reused=("plan", "acceptance-plan"),
                              plans=(plan, acceptance),
                              planning_only=bool(getattr(args, "plan_only", False)))
            if any(c.get('helpers') for c in plan['cases'] + acceptance['cases']) and prior_lock.get('python_probe_sha256') != helper_hash:
                raise RefinerError('Cached Python observer hash mismatch')
            state["plan_provenance"] = str(previous)
            state['plan_provenance_checker_sha256'] = prior_lock.get('checker_sha256')
            for suite, frozen, limit, suite_context in (
                    ("development", plan, args.max_cases, context),
                    ("acceptance", acceptance, args.acceptance_cases,
                     {**context, "contract": plan["requirements"]})):
                phase_progress("plan_generation", "reused", suite=suite,
                               source_run=str(previous), case_ids=[case["id"] for case in frozen["cases"]])
                phase_progress("plan_self_check", "running", suite=suite, reused=True)
                try:
                    if frozen.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
                        bindings, frozen_inputs = verified_response_sources(work, context, include_inputs=True)
                        source_resolution = load_response_applicability(previous, frozen_inputs, frozen, bindings)
                        source_hash = source_resolution.get("decision_sha256")
                        if source_hash:
                            if prior_lock.get("response_applicability_sha256", {}).get(suite) != source_hash:
                                raise RefinerError("Reused response applicability decision is not frozen by its source oracle")
                            source_path = _response_decision_path(previous, frozen)
                            destination = _response_decision_path(work, frozen)
                            record = read_json(source_path)
                            if destination.exists() and read_json(destination) != record:
                                raise RefinerError("Reused response applicability destination differs")
                            write_json(destination, record)
                    business_analysis = context.get("business_analysis") or {}
                    source_scenarios = business_analysis.get("scenarios") or None
                    required_scenario_ids = context.get("required_scenario_ids")
                    if required_scenario_ids is None and source_scenarios:
                        required_scenario_ids = [row["id"] for row in source_scenarios if row["applicable"]]
                    controls = validate_plan(
                        frozen, work, limit,
                        required_case_kinds=context.get("required_case_kinds", []),
                        required_scenario_ids=required_scenario_ids or [],
                        expected_scenarios=source_scenarios,
                        expected_requirements=context.get("contract"),
                        verified_response_sources=(verified_response_sources(work, context)
                                                   if frozen.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else None),
                        required_response_contract_cases=(load_response_applicability(
                            work, verified_response_sources(work, context, include_inputs=True)[1], frozen,
                            verified_response_sources(work, context))["required_case_ids"]
                            if frozen.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else None))
                except PlanValidationError as exc:
                    failed_evidence = {**exc.evidence, "suite": suite, "attempt": 1,
                                       "plan_sha256": digest(frozen), "plan_snapshot": frozen}
                    write_json(work / "plan-controls" / "attempts" / f"{suite}-01.json", failed_evidence)
                    write_json(work / "plan-controls" / f"{suite}.json", failed_evidence)
                    phase_progress("plan_self_check", "failed", suite=suite, reused=True, reason=str(exc))
                    raise PlanGateError(f"Reused {suite} plan failed its complete control suite: {exc}") from exc
                control_evidence = {"schema_version": "skill-refiner.plan-controls/v1", "suite": suite,
                                    "attempt": 1, "status": "passed", "plan_sha256": digest(frozen),
                                    "plan_snapshot": frozen,
                                    "case_control_results": controls["case_control_results"],
                                    "control_failures": [], "coverage_failures": [],
                                    **({"revision_context": {"schema_version":"skill-refiner.plan-revision-context/v1",
                                          "base_plan_sha256":None,"feedback":[],"feedback_sources":[],
                                          "targeted_case_ids":[],"case_changes":[],"requirements_changed":False,
                                          "execution_plan_changed":False}}
                                       if frozen.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} else {})}
                write_json(work / "plan-controls" / "attempts" / f"{suite}-01.json", control_evidence)
                write_json(work / "plan-controls" / f"{suite}.json", control_evidence)
                phase_progress("plan_self_check", "passed", suite=suite, reused=True,
                               controls=len(controls["case_control_results"]))
                _review_frozen_plan(agent, frozen, suite_context, controls,
                                    phase_callback=phase_progress, suite=suite)
        else:
            runtime_preflight("before-planning", planning_only=bool(getattr(args, "plan_only", False)))
            planning_context = ({**context, "_response_contract_v7": True}
                                if plan_recovery and (work / "response-contract-migration.json").is_file()
                                else context)
            if plan_recovery and plan_recovery.get("planned_dependency_migration"):
                planning_context = {**planning_context, "_planned_target_dependencies_v8": True}
            plan, acceptance = make_plan_suites(
                agent, planning_context, work, args.max_cases, args.acceptance_cases,
                max_attempts=getattr(args, 'planning_attempts', 2),
                phase_callback=phase_progress,
                agent_parallelism=getattr(args, "agent_parallelism", 2),
                wave_callback=planning_wave_progress, recovery=plan_recovery)
            budget()
        if not frozen_plan:
            write_json(work / "test-plan.json", plan)
            write_json(work / "acceptance-plan.json", acceptance)
        development_inputs = {digest(c["files"] or {"prompt": c["prompt"]}) for c in plan["cases"]}
        if all(digest(c["files"] or {"prompt": c["prompt"]}) in development_inputs for c in acceptance["cases"]):
            raise PlanGateError("Independent acceptance plan contains no fresh task inputs")
        validate_obligation_plans(context, {"development": plan, "acceptance": acceptance}, require_closed=False)
        obligation_gaps = context.get("business_analysis", {}).get("target_obligations", {}).get("coverage_gaps", [])
        technical_gaps = context.get("technical_coverage_gaps", [])
        if plan["coverage_gaps"] or acceptance["coverage_gaps"] or technical_gaps or obligation_gaps:
            gaps = plan["coverage_gaps"] + acceptance["coverage_gaps"] + [
                "Unresolved technical evidence: " + row["resolution"] for row in technical_gaps] + [
                "Unresolved target standard evidence: " + row["id"] + ": " + row["reason"] for row in obligation_gaps]
            state["target_obligation_gaps"] = obligation_gaps
            state["technical_coverage_gaps"] = technical_gaps
            phase_progress("plan_gate", "not_passed", reason="coverage_gap", gaps=gaps)
            state.update(status="plan_not_passed", gaps=gaps,
                         error="Plan gate did not pass because required coverage is missing.")
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "plan_gate", "status": "not_passed", "gaps": gaps,
                              "skill_evaluation": "not_started"}, ensure_ascii=False), flush=True)
            return state
        response_contract_needed = plan.get("schema_version") in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}
        if response_contract_needed and acceptance.get("schema_version") != plan.get("schema_version"):
            raise RefinerError("Development and acceptance must use the same response contract plan version")
        planned_v8 = plan.get("schema_version") == "skill-refiner.plan/v8"
        if planned_v8 and acceptance.get("schema_version") != "skill-refiner.plan/v8":
            raise RefinerError("Development and acceptance must use the same v8 dependency contract")
        planned_validator_hash = hashlib.sha256(Path(__file__).with_name("planned_dependencies.py").read_bytes()).hexdigest() if planned_v8 else None
        planned_migration = (plan_recovery.get("planned_dependency_migration") if planned_v8 and plan_recovery else None)
        if planned_v8 and context.get("_planned_target_dependencies_v8") is not True and not planned_migration:
            raise RefinerError("v8 plan requires an input-frozen selection or authenticated migration")
        if frozen_plan and planned_v8 and (prior_lock.get("planned_dependency_validator_sha256") != planned_validator_hash or
                prior_lock.get("planned_dependency_migration_sha256") != (digest(planned_migration) if planned_migration else None)):
            raise RefinerError("Frozen planned-dependency contract or migration changed")
        locked = digest({"plan": plan, "acceptance": acceptance, "inputs": context})
        migration_hash = None
        if response_contract_needed and plan_recovery and context.get("_response_contract_v7") is not True:
            migration_hash = digest(response_contract_migration(work, context, plan_recovery, resume_contract))
        applicability_hashes = (response_applicability_lock(
            work, verified_response_sources(work, context, include_inputs=True)[1],
            {"development": plan, "acceptance": acceptance}, verified_response_sources(work, context))
            if response_contract_needed else {})
        if frozen_plan and (prior_lock.get("response_applicability_sha256", {}) != applicability_hashes
                            or prior_lock.get("response_contract_migration_sha256") != migration_hash):
            raise RefinerError("Frozen response applicability decisions or migration changed or are missing")
        write_json(work / "oracle.lock.json", {"sha256": locked, "source_manifest": original_manifest,
                   "requirements": plan["requirements"], "checker_sha256": checker_hash,
                   "python_probe_sha256": helper_hash, "engine": backend.engine,
                   **({"planned_dependency_validator_sha256": planned_validator_hash,
                       "planned_dependency_migration_sha256": digest(planned_migration) if planned_migration else None} if planned_v8 else {}),
                   **({"response_contract_sha256": response_checker_hash,
                       "response_applicability_sha256": applicability_hashes,
                       "response_contract_migration_sha256": migration_hash} if response_contract_needed else {})})
        from .gate import _verify_plan_control_artifacts, _verify_plan_review_artifacts
        _verify_plan_control_artifacts(work, {"development": plan, "acceptance": acceptance})
        _verify_plan_review_artifacts(work, {"development": plan, "acceptance": acceptance})
        stages.complete("planning")
        phase_progress("plan_gate", "passed", development_cases=len(plan["cases"]),
                       acceptance_cases=len(acceptance["cases"]), oracle_lock_sha256=locked)
        state["plan_ready"] = True
        write_work_plan(work, source=source, plan=plan, acceptance=acceptance, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        write_test_scenarios(work, plan=plan, acceptance=acceptance)
        event("plan_ready", work_plan=str(work / "work-plan.md"),
              requirements=len(plan["requirements"]), development_cases=len(plan["cases"]),
              acceptance_cases=len(acceptance["cases"]), max_rounds=args.max_rounds,
              max_seconds=args.max_seconds, max_evaluations=args.max_evaluations)
        if getattr(args, "plan_only", False):
            state.update(status="planned", planned_only=True,
                         planned_development_cases=len(plan["cases"]),
                         planned_acceptance_cases=len(acceptance["cases"]),
                         skill_evaluation="not_started",
                         elapsed_seconds=round(elapsed_before + time.monotonic()-started, 2))
            write_json(work / "state.json", state)
            return state
        runtime_preflight("before-evaluation", reused=REFINE_PLANNING_STEPS,
                          plans=(plan, acceptance), planning_only=False)
        if planned_v8 and any(case.get('planned_target_dependencies') for suite in (plan,acceptance) for case in suite['cases']):
            from .probe_budget import ProbeBudget
            probe_config = ProbeBudget.configuration((plan,acceptance), len(config['executors']) if config else 1,
                                                     args.max_rounds)
            if resume_from and state.get('target_evaluations', 0) > 0 and not (work / 'validator-probe-budget' / 'config.json').is_file():
                raise RefinerError('Cannot restore validator budget after evaluations: persistent accounting is missing')
            backend.probe_budget = ProbeBudget(work, probe_config, backend.deadline)

        expected_name = next((f["content"] for f in context["files"] if f["path"] == "SKILL.md"), "")
        import yaml
        import re
        m = re.match(r"---\s*\n(.*?)\n---", expected_name, re.S)
        try:
            fm = yaml.safe_load(m[1]) if m else {}
        except yaml.YAMLError:
            fm = {}
        expected_name = fm.get("name") if isinstance(fm, dict) else None
        event("baseline")
        require_plan_gate(state)
        set_phase_result(state, "skill_evaluation", "running")
        set_phase_result(state, "skill_evaluation", "running", suite="baseline")
        write_json(work / "state.json", state)
        baseline_dir = work / "baseline"
        baseline_dir.mkdir(exist_ok=True)
        result = evaluate(original, plan["cases"], baseline_dir, phase="baseline")
        set_phase_result(state, "skill_evaluation", "passed" if result.get("passed") else "failed",
                         suite="baseline", passed=result.get("passed"), cases=len(result.get("cases", [])))
        findings = standards_review(agent, original, context, baseline_dir, expected_name)
        original_result = result
        best, best_result, best_findings, best_unresolved = original, result, findings, []
        best_round = 0
        baseline_passed = {r["id"] for r in result["cases"] if r["passed"]}
        no_progress = 0
        for number in range(1, args.max_rounds + 1):
            budget()
            if qualifies(best_result, best_findings, best_unresolved):
                break
            if any(r["status"] == "ERROR" for r in best_result["cases"]):
                event("execution_error", message="Resolve engine/environment errors before modifying the Skill")
                set_phase_result(state, "skill_evaluation", "error", reason="execution_error")
                write_json(work / "state.json", state)
                return state
            event("repairing", round=number)
            round_dir = work / "iterations" / f"round-{number}"
            round_dir.mkdir(parents=True, exist_ok=True)
            failing = [r for r in best_result["cases"] if not r["passed"]]
            errors = [f for f in best_findings if f["severity"] == "error"]
            unresolved_evidence = [{"id": f"U-{i+1:03}", "message": issue,
                                    "origin": "previous repair report; not a standards finding"}
                                   for i, issue in enumerate(best_unresolved)]
            evidence_ids = ({r["id"] for r in failing} | {f["id"] for f in errors}
                            | {u["id"] for u in unresolved_evidence})
            repair_schema = copy.deepcopy(REPAIR)
            repair_schema["properties"]["changes"]["items"]["properties"]["evidence_ids"]["items"]["enum"] = sorted(evidence_ids)
            response = agent.ask("repair", "Diagnose the failed cases and material standards violations from "
                                 "the supplied frozen requirement contract and cited findings. Large source documents "
                                 "are retained as hash-addressed artifacts; do not assume omitted source text. "
                                 "Return narrow full-file replacements with concrete reasons and evidence_ids. "
                                 "Preserve the Skill's identity, scope and valid existing behavior. Fix supporting "
                                 "scripts if they cause failure, not just prose. Never edit evals, tests, requirements "
                                 "or grading criteria, and never hardcode known test inputs or expected answers. "
                                 "Do not add capabilities not required by the supplied contract. Return changes=[] "
                                 "and unresolved issues if repair cannot be justified. unresolved lists only remaining "
                                 "required behaviors, not intentional scope exclusions. The controller runs your patch "
                                 "afterwards: not having executed it yourself is not an unresolved behavior. "
                                 "Use only allowed_evidence_ids, not requirement IDs. Held-out acceptance cases are omitted "
                                 "until the candidate first qualifies on the development suite.",
                                 {"files": inventory(best, max_bytes=120_000,
                                                     priority_paths=linked_source_paths(best, errors)),
                                  "requirements": plan["requirements"],
                                  "requirement_sources": source_references(context["requirements"], "requirement"),
                                  "standard_sources": source_references(context["standards"], "standard"),
                                  "allowed_evidence_ids": sorted(evidence_ids),
                                  "failures": failing, "findings": errors,
                                  "previous_unresolved": unresolved_evidence,
                                  "failure_evidence": repair_evidence(failing, max_bytes=60_000),
                                  "failing_cases": [c for c in plan["cases"] if c["id"] in
                                                    {r.get("case_id", r["id"]) for r in failing}]}, repair_schema)
            write_json(round_dir / "changes.json", response)
            if not response["changes"]:
                event("no_progress", unresolved=response["unresolved"])
                return state
            candidate = work / "candidates" / str(number) / source.name
            candidate = resumable_candidate(best, response, evidence_ids, candidate)
            if manifest(candidate) == manifest(best):
                event("no_progress", message="Repair did not change any source file")
                return state
            budget()
            budget()
            event("regression", round=number)
            current = evaluate(candidate, plan["cases"], round_dir / "regression", phase="regression", round_number=number)
            current_findings = standards_review(agent, candidate, context, round_dir, expected_name)
            accepted, regressed = compare_candidate(baseline_passed, best_result, best_findings, current,
                                                   current_findings, best_unresolved, response["unresolved"])
            record_round({"number": number, "accepted": accepted, "regressed_cases": regressed,
                                    "score": score(current, current_findings), "candidate": str(candidate),
                                    "unresolved": response["unresolved"],
                                    "retained_for_next_round": accepted,
                                    "qualified_for_acceptance": qualifies(current, current_findings, response["unresolved"])})
            if accepted:
                best, best_result, best_findings = candidate, current, current_findings
                best_round = number
                best_unresolved = response["unresolved"]
                no_progress = 0
            else:
                no_progress += 1
            write_json(work / "state.json", state)
            if no_progress >= 2:
                break
        budget()
        if not qualifies(best_result, best_findings, best_unresolved):
            event("not_converged", best_candidate=str(best), unresolved=best_unresolved)
            return state
        acceptance_dir = work / "acceptance"
        acceptance_dir.mkdir(parents=True, exist_ok=True)
        def acceptance_target(target, label):
            target_manifest = manifest(target)
            record = {"candidate": target.resolve().relative_to(work.resolve()).as_posix(),
                      "candidate_manifest": target_manifest,
                      "candidate_sha256": digest(target_manifest),
                      "suite_sha256": digest(acceptance["cases"])}
            attempt_dir = acceptance_dir / "attempts" / label
            attempt_dir.mkdir(parents=True, exist_ok=True)
            write_json(attempt_dir / "target.json", record)
            return record, attempt_dir

        accepted_manifest, initial_acceptance_dir = acceptance_target(best, "initial")
        write_json(acceptance_dir / "target.json", accepted_manifest)
        event("acceptance", attempt="initial")
        final_checks = evaluate(best, acceptance["cases"], initial_acceptance_dir / "evaluation", phase="acceptance", round_number=best_round)
        write_json(acceptance_dir / "summary.json", final_checks)
        budget()
        if any(row["status"] == "ERROR" for row in final_checks["cases"]):
            event("execution_error", message="Resolve acceptance engine/environment errors before modifying the Skill")
            return state

        # An independent acceptance failure is actionable evidence. Route only its failed cases
        # back through the same evidence-bound repair and full development regression gates.
        last_development_round = max((row.get("number", 0) for row in state["rounds"]
                                     if row.get("trigger") != "acceptance"), default=0)
        for number in range(last_development_round + 1, args.max_rounds + 1):
            if final_checks["passed"] and not best_unresolved:
                break
            failing_acceptance = [row for row in final_checks["cases"] if not row["passed"]]
            if not failing_acceptance and not best_unresolved:
                break
            event("repairing", round=number, trigger="acceptance")
            round_dir = work / "iterations" / f"round-{number}"
            round_dir.mkdir(parents=True, exist_ok=True)
            scoped_failures = []
            for row in failing_acceptance:
                case_id = row.get("case_id", row["id"])
                scoped_failures.append({**row, "id": f"acceptance::{case_id}",
                                        "case_id": case_id, "suite": "acceptance"})
            unresolved_evidence = [{"id": f"U-{i+1:03}", "message": issue,
                                    "origin": "previous repair report; not a standards finding"}
                                   for i, issue in enumerate(best_unresolved)]
            evidence_ids = ({row["id"] for row in scoped_failures}
                            | {finding["id"] for finding in best_findings if finding["severity"] == "error"}
                            | {item["id"] for item in unresolved_evidence})
            repair_schema = copy.deepcopy(REPAIR)
            repair_schema["properties"]["changes"]["items"]["properties"]["evidence_ids"]["items"]["enum"] = sorted(evidence_ids)
            response = agent.ask(
                "repair",
                "Diagnose these failures from the independently planned acceptance suite against the frozen "
                "requirements and cited source material. The exact failed acceptance cases are included because "
                "a previously qualified candidate failed final acceptance. Return narrow full-file replacements "
                "only for demonstrated target defects. Preserve identity, scope and valid behavior; fix supporting "
                "scripts when they cause a failure. Never edit tests, plans, requirements or grading criteria, and "
                "never hardcode fixture inputs or expected answers. If an observed response already satisfies the "
                "contract but a narrow oracle rejects equivalent wording, do not change target output to appease it; "
                "return changes=[] and describe the oracle problem as unresolved. Use only allowed_evidence_ids, "
                "not requirement IDs. The controller will run the complete development regression suite and rerun "
                "acceptance after applying a patch.",
                {"files": inventory(best, max_bytes=120_000,
                                    priority_paths=linked_source_paths(best, [])),
                 "requirements": plan["requirements"],
                 "requirement_sources": source_references(context["requirements"], "requirement"),
                 "standard_sources": source_references(context["standards"], "standard"),
                 "allowed_evidence_ids": sorted(evidence_ids),
                 "failures": scoped_failures,
                 "acceptance_failures": scoped_failures,
                 "findings": [finding for finding in best_findings if finding["severity"] == "error"],
                 "previous_unresolved": unresolved_evidence,
                 "failure_evidence": repair_evidence(scoped_failures, max_bytes=60_000),
                 "failing_cases": [case for case in acceptance["cases"]
                                   if case["id"] in {row["case_id"] for row in scoped_failures}]},
                repair_schema)
            write_json(round_dir / "changes.json", response)
            if not response["changes"]:
                event("failed_acceptance", best_candidate=str(best),
                      failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                      unresolved=response["unresolved"], message="Acceptance repair produced no changes")
                return state
            candidate = work / "candidates" / str(number) / source.name
            candidate = resumable_candidate(best, response, evidence_ids, candidate)
            if manifest(candidate) == manifest(best):
                event("failed_acceptance", best_candidate=str(best),
                      failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                      unresolved=response["unresolved"], message="Acceptance repair did not change any source file")
                return state
            budget()
            event("regression", round=number)
            current = evaluate(candidate, plan["cases"], round_dir / "regression", phase="regression", round_number=number)
            current_findings = standards_review(agent, candidate, context, round_dir, expected_name)
            if any(row["status"] == "ERROR" for row in current["cases"]):
                event("execution_error", message="Resolve regression engine/environment errors before continuing")
                return state
            candidate_acceptance_record, candidate_acceptance_dir = acceptance_target(candidate, f"round-{number}")
            event("acceptance", attempt=f"round-{number}")
            candidate_acceptance = evaluate(candidate, acceptance["cases"],
                                            candidate_acceptance_dir / "evaluation", phase="acceptance", round_number=number)
            budget()
            if any(row["status"] == "ERROR" for row in candidate_acceptance["cases"]):
                event("execution_error", message="Resolve acceptance engine/environment errors before continuing")
                return state
            before_result = combine_results(best_result, final_checks)
            after_result = combine_results(current, candidate_acceptance)
            accepted, regressed = compare_candidate(baseline_passed, before_result, best_findings,
                                                    after_result, current_findings,
                                                    best_unresolved, response["unresolved"])
            record_round({"number": number, "accepted": accepted,
                                    "regressed_cases": regressed,
                                    "score": score(after_result, current_findings),
                                    "candidate": str(candidate),
                                    "unresolved": response["unresolved"],
                                    "retained_for_next_round": accepted,
                                    "qualified_for_acceptance": (
                                        qualifies(current, current_findings, response["unresolved"])
                                        and candidate_acceptance["passed"]),
                                     "trigger": "acceptance"})
            if accepted:
                best, best_result, best_findings = candidate, current, current_findings
                best_unresolved = response["unresolved"]
                final_checks = candidate_acceptance
                write_json(acceptance_dir / "target.json", candidate_acceptance_record)
                write_json(acceptance_dir / "summary.json", final_checks)
                no_progress = 0
            else:
                no_progress += 1
            write_json(work / "state.json", state)
            if no_progress >= 2:
                break
        if not final_checks["passed"] or best_unresolved:
            event("failed_acceptance", best_candidate=str(best),
                  failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                  unresolved=best_unresolved)
            return state
        stages.complete("acceptance")
        final = work / "final" / source.name
        final = resumable_snapshot(best, final, keep_destination=True)
        patch = []
        before, after = manifest(original), manifest(final)
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) == after.get(path):
                continue
            old = (original/path).read_text(encoding="utf-8") if path in before else ""
            new = (final/path).read_text(encoding="utf-8") if path in after else ""
            patch.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "original/"+path, "candidate/"+path))
        (work / "final" / "changes.patch").write_text("".join(patch), encoding="utf-8")
        write_json(work / "final" / "evidence.json", {"original_manifest": original_manifest,
                   "candidate_manifest": manifest(final), "oracle_hash": locked,
                   "baseline": original_result, "regression": best_result,
                   "acceptance": final_checks, "standards": best_findings})
        event("passed" if before != after else "unchanged", final_skill=str(final))
        for stage_id in ("planning", "baseline"):
            stages.complete(stage_id)
        if before != after:
            stages.complete("repair")
            stages.complete("regression")
        stages.complete("acceptance")
        stages.complete("delivery")
        return state
    except PlanGateError as exc:
        phase_progress("plan_gate", "not_passed", reason=str(exc))
        if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "not_started":
            state["status"] = "plan_not_passed"
            state["skill_evaluation"] = "not_started"
        state["error"] = str(exc)
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "plan_gate", "status": "not_passed",
                          "reason": str(exc), "skill_evaluation": "not_started"},
                         ensure_ascii=False), flush=True)
        return state
    except Cancelled as exc:
        event("cancelled", error=str(exc))
        close_incomplete_planning_phases(state, str(exc))
        close_incomplete_plan_gate(state, str(exc), cause="cancelled")
        if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
            set_phase_result(state, "skill_evaluation", "error", cancellation=str(exc))
        write_json(work / "state.json", state)
        return state
    except Exception as exc:
        if state.get("phase_results", {}).get("plan_gate", {}).get("status") != "passed" and \
                state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "not_started":
            phases = state.get("phase_results", {})
            for name in ("plan_generation", "plan_self_check", "independent_review"):
                phase_entry = phases.get(name, {})
                if phase_entry.get("status") == "running":
                    phase_progress(name, "error", reason=str(exc))
                    break
            close_incomplete_plan_gate(state, str(exc))
            state.update(status="planning_error", error=str(exc), skill_evaluation="not_started")
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "planning", "status": "error", "reason": str(exc),
                              "skill_evaluation": "not_started"}, ensure_ascii=False), flush=True)
        else:
            event("error", error=str(exc))
            if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
                set_phase_result(state, "skill_evaluation", "error", reason=str(exc))
        return state
    finally:
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic()-started, 2)
        state["cached_agent_calls"] = agent.cached_calls
        if "backend" in locals():
            state["target_evaluations"] = backend.evaluations
        write_json(work / "state.json", state)
        write_report(work, state)
        if state.get("status") in {"passed", "unchanged"}:
            set_phase_result(state, "skill_evaluation", "passed")
            set_phase_result(state, "final_verification", "running")
            write_json(work / "state.json", state)
            stages.activate("verification")
            try:
                from .gate import verify_run
                verification = verify_run(work, probe_budget=getattr(backend, 'probe_budget', None),
                                          deadline=backend.deadline)
                write_json(work / "verification.json", verification)
                state["verification_status"] = verification["status"]
                set_phase_result(state, "final_verification", "passed",
                                 verification_status=verification["status"])
            except Exception as exc:
                state.update(status="verification_failed", verification_error=str(exc))
                set_phase_result(state, "final_verification", "failed", reason=str(exc))
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic()-started, 2)
            write_json(work / "state.json", state)
            write_report(work, state)
        elif state.get("phase_results", {}).get("plan_gate", {}).get("status") == "passed" and \
                state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
            outcome = "error" if state.get("status") in {"error", "cancelled", "execution_error"} else "failed"
            set_phase_result(state, "skill_evaluation", outcome, reason=state.get("error", state.get("status")))
            write_json(work / "state.json", state)
            write_report(work, state)
        stages.finish(state.get("status", "error"))


def write_report(work, state):
    lines = ["# Skill Refiner result", "", f"Status: **{state['status']}**", "",
             "[Design and test plan](work-plan.md)", "",
             f"Original: `{state['source']}`", f"Agent calls: {state.get('agent_calls', 0)}",
             f"Case-executor evaluations: {state.get('target_evaluations', 0)}",
             f"Elapsed: {state.get('elapsed_seconds', 0)} seconds", ""]
    if state.get("verification_status"):
        lines.insert(3, f"Verification: **{state['verification_status']}**")
    elif state.get("verification_error"):
        lines.insert(3, "Verification: **failed** — " + state["verification_error"])
    if state.get("final_skill"):
        lines += [f"Candidate: `{state['final_skill']}`", "Original files were not overwritten.", ""]
    phase_results = state.get("phase_results", {})
    if phase_results:
        lines += ["## Phase results", "", "| Phase | Status | Suite results |", "|---|---|---|"]
        phase_labels = [
            ("plan_generation", "Plan generation"),
            ("plan_self_check", "Plan self-check"),
            ("independent_review", "Independent review"),
            ("plan_gate", "Plan gate"),
            ("skill_evaluation", "Target Skill evaluation"),
            ("final_verification", "Final Refiner verify"),
        ]
        for key, label in phase_labels:
            entry = phase_results.get(key, {})
            status = entry.get("status", "not_started")
            if key == "plan_gate" and status == "not_passed":
                status = "计划未通过 (not_passed)"
            suites = entry.get("suites", {})
            suite_text = ", ".join(f"{name}={value}" for name, value in suites.items()) or "—"
            lines.append(f"| {label} | {status} | {suite_text} |")
        lines.append("")
        if phase_results.get("plan_gate", {}).get("status") != "passed":
            lines.append("Target Skill evaluation: **not started**; the plan gate did not pass.")
            lines.append("")
    phases = [("Baseline", work/"baseline"/"summary.json")]
    accepted_rounds = [r for r in state["rounds"] if r["accepted"]]
    if accepted_rounds:
        phases.append(("Full regression", work/"iterations"/f"round-{accepted_rounds[-1]['number']}"/"regression"/"summary.json"))
    phases.append(("Acceptance", work/"acceptance"/"summary.json"))
    for phase, path in phases:
        if path.exists():
            result = read_json(path)
            if "engines" in result:
                comparison = path.parent / "comparison.md"
                rendered = comparison.read_text(encoding="utf-8") if comparison.is_file() else comparison_markdown(result)
                lines += [f"## {phase}", "", rendered, ""]
                continue
            lines += [f"## {phase}", "", "| Case | Status |", "|---|---|"]
            lines += [f"| {r['id']} | {r['status']} |" for r in result["cases"]]
            lines += [""]
    lines += ["## Iterations", ""]
    for r in state["rounds"]:
        lines.append(f"- Round {r['number']}: accepted={r['accepted']}, score={r['score']}, regressions={r['regressed_cases']}")
    if accepted_rounds:
        lines += ["", "## Changes and evidence", ""]
        for r in accepted_rounds:
            changes = read_json(work/"iterations"/f"round-{r['number']}"/"changes.json")
            lines.append("Diagnosis: " + changes["diagnosis"])
            for change in changes["changes"]:
                lines.append(f"- `{change['path']}`: {change['reason']} (evidence: {', '.join(change['evidence_ids'])})")
    if (work/"test-plan.json").exists():
        plan = read_json(work/"test-plan.json")
        lines += ["", "## Planned requirement coverage", "", "| Requirement | Cases |", "|---|---|"]
        for requirement in plan["requirements"]:
            ids = [c["id"] for c in plan["cases"] if requirement["id"] in c["requirement_ids"]]
            lines.append(f"| {requirement['id']}: {requirement['behavior']} | {', '.join(ids)} |")
    if state.get("gaps"):
        lines += ["", "## Coverage gaps", ""] + ["- " + gap for gap in state["gaps"]]
    if state.get("error"):
        lines += ["", "Error: " + state["error"]]
    lines += ["", "## Limits", ""] + ["- " + x for x in state["limitations"]]
    (work / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


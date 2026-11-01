"""Local feasibility checks; this module never launches a backend or edits a plan.

The caller supplies only checkpoint facts it has already validated. A file merely
existing is not evidence that a step or evaluation can be reused. ``passed`` means
the known minimum fits the declared limits, not that inference or coverage passed.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import jsonschema

from .common import RefinerError, write_json


SCHEMA_VERSION = "skill-refiner.runtime-preflight/v1"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/v1/runtime-preflight.schema.json"
CREATE_PLANNING_STEPS = (
    "analyze", "analysis-review", "draft", "plan", "development-plan-review",
    "acceptance-plan", "acceptance-plan-review",
)
REFINE_PLANNING_STEPS = CREATE_PLANNING_STEPS[3:]
STANDARDS_REVIEW_STEP = "standards-review"
DECLARED_PROFILE_FIELDS = (
    "backend", "model", "timeout", "reasoning_effort", "child_model",
    "codex_sandbox", "max_turns", "max_request_bytes",
)


def _number(value, name, *, integer=False, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (integer and not isinstance(value, int))
            or (nonnegative and value < 0)):
        raise ValueError(f"{name} must be a finite {'nonnegative ' if nonnegative else ''}"
                         f"{'integer' if integer else 'number'}")
    return value


def _configuration_origin(context):
    context = copy.deepcopy({} if context is None else context)
    allowed = {"launch_manifest_path", "configured_defaults", "applied_defaults", "explicit_flags"}
    if not isinstance(context, dict) or set(context) - allowed:
        raise ValueError("launch_context has unsupported fields")
    origin = {"launch_manifest_path": context.get("launch_manifest_path"),
              "configured_defaults": context.get("configured_defaults", {}),
              "applied_defaults": context.get("applied_defaults", {}),
              "explicit_flags": context.get("explicit_flags", [])}
    path = origin["launch_manifest_path"]
    if path is not None and (not isinstance(path, str) or not path):
        raise ValueError("launch_manifest_path must be a nonempty string or null")
    for source in ("configured_defaults", "applied_defaults"):
        defaults = origin[source]
        if not isinstance(defaults, dict) or any(not isinstance(key, str) for key in defaults):
            raise ValueError(f"{source} must be an object with string keys")
        for key, value in defaults.items():
            if isinstance(value, (dict, list)) or not (value is None or isinstance(value, (str, bool, int, float))):
                raise ValueError(f"{source} values must be JSON scalars")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{source} numbers must be finite")
            # Provenance is metadata, never a place to persist credentials.
            if any(word in key.lower().replace("-", "_") for word in
                   ("token", "secret", "password", "api_key", "authorization")):
                defaults[key] = "[REDACTED]"
    flags = origin["explicit_flags"]
    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag for flag in flags):
        raise ValueError("explicit_flags must be an array of nonempty strings")
    return origin


def _profile_report(config):
    """Describe approved declarations without exposing keys, URLs or prompt text."""
    profiles, warnings = [], []
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise ValueError("config must be an object or null")
    agents, executors = config.get("agents", {}), config.get("executors", [])
    if not isinstance(agents, dict) or not isinstance(executors, list):
        raise ValueError("config agents/executors have invalid types")
    if any(not isinstance(role, str) or not role for role in agents):
        raise ValueError("Agent profile names must be nonempty strings")
    if any(not isinstance(profile, dict) for profile in executors):
        raise ValueError("Every executor profile must be an object")
    rows = [("agent:" + role, value) for role, value in agents.items()]
    rows += [("executor:" + str(value.get("id", index)), value)
             for index, value in enumerate(executors)]
    for label, profile in rows:
        if not isinstance(profile, dict):
            raise ValueError(f"{label} profile must be an object")
        declared = {key: copy.deepcopy(profile[key]) for key in DECLARED_PROFILE_FIELDS if key in profile}
        unresolved = []
        if profile.get("backend") in {"codex", "codex_subagent", "cursor", "claude_code"}:
            if not profile.get("model"):
                unresolved.append("model")
            if profile.get("backend") in {"codex", "codex_subagent"}:
                if not profile.get("reasoning_effort"):
                    unresolved.append("reasoning_effort")
                # The runtime does not pass a per-run service-tier override.
                unresolved.append("service_tier")
        profiles.append({"profile": label, "declared_settings": declared,
                         "unresolved_inherited_fields": unresolved,
                         "call_timeout_policy": "remaining_run_time_capped_by_declared_role_timeout",
                         "inference_verification": "not_live_tested"})
        if unresolved:
            warnings.append({"code": "INHERITED_CONFIGURATION_UNRESOLVED",
                             "field": label,
                             "message": "Inherited settings have not been resolved for this invocation.",
                             "expected": "explicit or separately verified effective settings",
                             "actual": unresolved})
    if not rows:
        warnings.append({"code": "PROFILE_CONFIGURATION_NOT_INSPECTED", "field": "config",
                         "message": "No backend profile declarations were supplied.",
                         "expected": "declared backend settings", "actual": None})
    return profiles, warnings


def build_runtime_preflight(*, mode, phase, limits, usage=None, config=None,
                            validated_reused_steps=(), applicable_scenario_count=None,
                            executor_count=1, plan_only=False,
                            validated_completed_evaluations=0, launch_context=None,
                            planned_case_counts=None):
    """Return a schema-bound feasibility report from explicit, validated facts.

    Planning requires seven create or four refine role calls on an uncached path.
    A full successful run additionally requires a standards review. Repair rounds
    are optional work, so their configured maximum is not a completion guarantee.
    Each suite needs at least two cases. Scenario inventory does not determine case
    count; frozen plans and their requirement/criterion/scenario links do. Once both
    plans are frozen, pass their actual case counts. The full-run
    evaluation floor then covers those baseline and held-out acceptance cases;
    otherwise it is only the known coverage minimum. Repair/regression remain
    separately bounded work, never promised by this minimum-cost check.
    """
    if mode not in {"create", "refine"}:
        raise ValueError("mode must be create or refine")
    if not isinstance(phase, str) or not phase:
        raise ValueError("phase must be a nonempty string")
    if not isinstance(limits, dict) or (usage is not None and not isinstance(usage, dict)):
        raise ValueError("limits and usage must be objects")
    if type(plan_only) is not bool:
        raise ValueError("plan_only must be boolean")
    usage = usage or {}
    declared_limits = {key: _number(limits[key], key, integer=key != "max_seconds", nonnegative=True)
                       for key in ("max_seconds", "max_agent_calls", "max_evaluations",
                                   "max_cases", "acceptance_cases")}
    used = {"agent_calls": _number(usage.get("agent_calls", 0), "agent_calls", integer=True, nonnegative=True),
            "elapsed_seconds": _number(usage.get("elapsed_seconds", 0), "elapsed_seconds", nonnegative=True),
            "target_evaluations": _number(usage.get("target_evaluations", 0), "target_evaluations",
                                           integer=True, nonnegative=True)}
    _number(executor_count, "executor_count", integer=True, nonnegative=True)
    if executor_count < 1:
        raise ValueError("executor_count must be at least one")
    if applicable_scenario_count is not None:
        _number(applicable_scenario_count, "applicable_scenario_count", integer=True, nonnegative=True)
    _number(validated_completed_evaluations, "validated_completed_evaluations", integer=True, nonnegative=True)
    if validated_completed_evaluations > used["target_evaluations"]:
        raise ValueError("Validated completed evaluations cannot exceed recorded target_evaluations")
    if planned_case_counts is not None:
        if (not isinstance(planned_case_counts, dict)
                or set(planned_case_counts) != {"development", "acceptance"}):
            raise ValueError("planned_case_counts requires exactly development and acceptance")
        planned_case_counts = {
            suite: _number(planned_case_counts[suite], "planned_case_counts." + suite,
                           integer=True, nonnegative=True)
            for suite in ("development", "acceptance")
        }

    planning_steps = CREATE_PLANNING_STEPS if mode == "create" else REFINE_PLANNING_STEPS
    all_steps = planning_steps + (STANDARDS_REVIEW_STEP,)
    reused = list(validated_reused_steps)
    if len(set(reused)) != len(reused) or any(step not in all_steps for step in reused):
        raise ValueError("validated_reused_steps must contain unique known step IDs for this mode")
    required_steps = planning_steps if plan_only else all_steps
    pending_steps = [step for step in required_steps if step not in reused]
    # Scenario rows are coverage intents, not a one-to-one case requirement. The
    # applicable count is reported as context only; only frozen plan case IDs may
    # establish actual suite capacity and evaluation cost.
    cases = 2
    suite_counts = planned_case_counts or {"development": cases, "acceptance": cases}
    full_evaluation_floor = sum(suite_counts.values()) * executor_count
    evaluations = (0 if plan_only else max(0, full_evaluation_floor - validated_completed_evaluations))
    remaining = {"seconds": declared_limits["max_seconds"] - used["elapsed_seconds"],
                 "agent_calls": declared_limits["max_agent_calls"] - used["agent_calls"],
                 "target_evaluations": declared_limits["max_evaluations"] - used["target_evaluations"]}
    issues = []

    def insufficient(code, field, expected, actual, message):
        issues.append({"code": code, "field": field, "message": message,
                       "expected": expected, "actual": actual})

    if remaining["seconds"] <= 0:
        insufficient("RUN_TIME_EXHAUSTED", "remaining_budget.seconds", "> 0", remaining["seconds"],
                     "No run time remains for another stage or backend call.")
    if remaining["agent_calls"] < len(pending_steps):
        insufficient("INSUFFICIENT_AGENT_CALLS", "remaining_budget.agent_calls", len(pending_steps),
                     remaining["agent_calls"], "Remaining controller calls cannot cover the known mandatory path.")
    for field in ("max_cases", "acceptance_cases"):
        if declared_limits[field] < cases:
            insufficient("INSUFFICIENT_CASE_CAPACITY", "limits." + field, cases, declared_limits[field],
                         "The configured suite cap is below the two-case structural minimum.")
    if planned_case_counts is not None:
        for suite, field in (("development", "max_cases"), ("acceptance", "acceptance_cases")):
            actual = suite_counts[suite]
            if actual < cases:
                insufficient("INSUFFICIENT_PLANNED_COVERAGE", "planned_case_counts." + suite,
                             cases, actual, "The frozen suite is below the two-case structural minimum.")
            if actual > declared_limits[field]:
                insufficient("PLANNED_CASES_EXCEED_LIMIT", "planned_case_counts." + suite,
                             declared_limits[field], actual,
                             "The frozen suite has more cases than its configured maximum.")
    if remaining["target_evaluations"] < evaluations:
        insufficient("INSUFFICIENT_EVALUATIONS", "remaining_budget.target_evaluations", evaluations,
                     remaining["target_evaluations"], "Remaining evaluations cannot cover baseline and acceptance.")
    profiles, warnings = _profile_report(config)
    if planned_case_counts is None:
        warnings.append({"code": "SCENARIO_CASE_MAPPING_DEFERRED", "field": "planned_case_counts",
                         "message": "Scenario inventory does not determine suite size; verify scenario and criterion links after plans are frozen.",
                         "expected": "validated development and acceptance case IDs with coverage links",
                         "actual": None})
    report = {
        "schema_version": SCHEMA_VERSION, "status": "failed" if issues else "passed",
        "mode": mode, "phase": phase, "plan_only": plan_only,
        "limits": declared_limits, "usage": used, "remaining_budget": remaining,
        "minimum_required": {"agent_calls": len(pending_steps), "pending_steps": pending_steps,
                             "development_cases": suite_counts["development"],
                             "acceptance_cases": suite_counts["acceptance"],
                             "case_evaluations": evaluations,
                             "full_run_case_evaluation_floor": full_evaluation_floor},
        "validated_reused_steps": reused,
        "validated_completed_evaluations": validated_completed_evaluations,
        "planned_case_counts": planned_case_counts,
        "coverage": {"applicable_scenario_count": applicable_scenario_count,
                     "minimum_cases_per_suite": cases,
                     "executor_count": executor_count,
                     "verification": "known_minimum_only"},
        "execution_profiles": profiles, "configuration_origin": _configuration_origin(launch_context),
        "verification": {"inference": "not_live_tested", "effective_configuration": "not_resolved",
                         "time_feasibility": "positive_remaining_time_only",
                         "repair_rounds_guaranteed": False},
        "issues": issues, "warnings": warnings,
    }
    validate_runtime_preflight(report)
    return report



def add_worst_case_evaluation_budget(report, *, development_cases, acceptance_cases,
                                     native_cases, executor_count, max_rounds,
                                     validated_baseline_reused):
    """Gate the worst remaining full-suite cost using only authenticated inputs.

    Native cases run with every generated suite on every configured executor.
    All repairs share one max_rounds budget; each can incur full development
    regression and a full acceptance rerun. Only the exact baseline cache may be
    credited: a cached acceptance target can change after a future repair.
    """
    validate_runtime_preflight(report)
    if report.get("plan_only") or report.get("phase") != "before-evaluation":
        raise ValueError("Worst-case evaluation preflight requires a final pre-evaluation report")
    values = {"development_cases": development_cases, "acceptance_cases": acceptance_cases,
              "native_cases": native_cases, "executor_count": executor_count, "max_rounds": max_rounds}
    for name, value in values.items():
        _number(value, name, integer=True, nonnegative=True)
    if executor_count < 1 or development_cases < 1 or acceptance_cases < 1:
        raise ValueError("Frozen suites and executor_count must be nonempty")
    if type(validated_baseline_reused) is not bool:
        raise ValueError("validated_baseline_reused must be a verified boolean")
    expected_counts = report.get("planned_case_counts")
    if expected_counts != {"development": development_cases, "acceptance": acceptance_cases}:
        raise ValueError("Worst-case costs require the exact frozen plan counts")
    if report["coverage"]["executor_count"] != executor_count:
        raise ValueError("Worst-case executor count differs from the runtime preflight")
    baseline_cost = (development_cases + native_cases) * executor_count
    acceptance_cost = (acceptance_cases + native_cases) * executor_count
    per_round = baseline_cost + acceptance_cost
    full_cost = baseline_cost + acceptance_cost + max_rounds * per_round
    baseline_credit = baseline_cost if validated_baseline_reused else 0
    if baseline_credit > report["usage"]["target_evaluations"]:
        raise ValueError("Verified baseline credit exceeds already charged evaluations")
    required_new = full_cost - baseline_credit
    remaining = report["remaining_budget"]["target_evaluations"]
    breakdown = {**values, "baseline_cost": baseline_cost, "initial_acceptance_cost": acceptance_cost,
                 "each_repair_worst_cost": per_round, "configured_worst_total": full_cost,
                 "validated_baseline_credit": baseline_credit, "required_remaining": required_new,
                 "remaining_budget": remaining,
                 "acceptance_cache_credit": 0,
                 "formula": "E * ((D + N) + (A + N) + R * (D + A + 2*N)) - verified_baseline_credit"}
    report["warnings"].append({"code": "WORST_CASE_EVALUATION_BUDGET", "field": "remaining_budget.target_evaluations",
        "message": "Worst case includes baseline, initial acceptance, every regression and potential acceptance rerun; only a validated baseline cache is credited.",
        "expected": "Sufficient remaining budget for the configured worst case", "actual": breakdown})
    if remaining < required_new:
        report["issues"].append({"code": "INSUFFICIENT_WORST_CASE_EVALUATIONS", "field": "remaining_budget.target_evaluations",
            "message": "Remaining evaluation budget cannot cover all configured repair and acceptance paths.",
            "expected": required_new, "actual": remaining})
    report["status"] = "failed" if report["issues"] else "passed"
    validate_runtime_preflight(report)
    return report

def validate_runtime_preflight(report):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(report)
    if (report["status"] == "passed") != (not report["issues"]):
        raise ValueError("Preflight status must match its blocking issues")
    return report


def require_runtime_preflight(report):
    validate_runtime_preflight(report)
    if report["status"] != "passed":
        details = "; ".join(f"{issue['code']} at {issue['field']}: expected {issue['expected']!r}, "
                            f"actual {issue['actual']!r}" for issue in report["issues"])
        raise RefinerError("Runtime preflight failed: " + details)
    return report


def emit_runtime_preflight(report, work, filename="runtime-preflight.json", *, require_pass=True):
    """Persist before rejecting so a failed preflight remains reviewable."""
    validate_runtime_preflight(report)
    root = Path(work).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or path == root or path.suffix != ".json":
        raise ValueError("Preflight report path must be a JSON file inside the workspace")
    # Reports describe this attempt's remaining budget. Preserve earlier attempts
    # instead of changing a completed stage's evidence during recovery.
    original = path
    sequence = 2
    while path.exists():
        path = original.with_name(f"{original.stem}-{sequence:02}{original.suffix}")
        sequence += 1
    write_json(path, report)
    print(json.dumps({"stage": "runtime_preflight", "status": report["status"],
                      "phase": report["phase"], "report": str(path),
                      "issues": report["issues"], "warnings": report["warnings"]},
                     ensure_ascii=False), flush=True)
    if require_pass:
        require_runtime_preflight(report)
    return path

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import itertools
import tempfile

from .common import PlanControlError, PlanValidationError, RefinerError, relative_path
from .artifact_validation import schema_ref, validate_value


def command_forms(command, root=None, literal=False):
    """Regexes describe the command, not its platform-added `sh -lc '...'` quoting."""
    forms = [command]
    try:
        words = shlex.split(command)
    except ValueError:
        return forms
    if len(words) >= 3 and Path(words[0]).name in {"sh", "bash", "zsh", "dash", "ksh", "fish"}:
        if words[1].startswith("-") and "c" in words[1]:
            forms.append(words[2])
    if literal:
        return forms
    # A shell list separator can immediately follow an argument (`file.json;`).
    # Match the observed simple command independently of that separator, while
    # retaining quoted semicolons inside arguments. Never execute the text.
    for form in list(forms):
        try:
            lexer = shlex.shlex(form, posix=True, punctuation_chars=";&|\n")
            lexer.whitespace = ' \t\r'
            lexer.whitespace_split = True
            lexer.commenters = ""
            group, groups = [], []
            for token in lexer:
                if token and set(token) <= set(';&|\n'):
                    if group:
                        groups.append(group)
                        group = []
                else:
                    group.append(token)
            if group:
                groups.append(group)
            for group in groups:
                forms.append(shlex.join(group))
                if root is None:
                    continue
                options = []
                base = Path(root).resolve()
                for token in group:
                    aliases = [token]
                    path = Path(token)
                    try:
                        resolved = (path if path.is_absolute() else base / path).resolve()
                        if resolved.is_relative_to(base) and resolved.is_file():
                            relative = resolved.relative_to(base).as_posix()
                            aliases += [str(resolved), relative, "./" + relative]
                    except (OSError, ValueError):
                        pass
                    options.append(list(dict.fromkeys(aliases)))
                # Bound expansion for long shell commands. Every alias points
                # to an existing file inside this actual case workspace.
                for variant in itertools.islice(itertools.product(*options), 64):
                    forms.append(shlex.join(variant))
        except ValueError:
            pass
    return forms


def parse_json_output(text):
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    return json.loads(text)


def parse_json_sequence(text):
    decoder, values, offset = json.JSONDecoder(), [], 0
    while text[offset:].strip():
        offset += len(text[offset:]) - len(text[offset:].lstrip())
        value, offset = decoder.raw_decode(text, offset)
        values.append(value)
    return values


def json_values_equal(left, right):
    """Compare JSON values without treating booleans as numbers.

    JSON has one numeric category: 1 and 1.0 compare equal, including inside
    arrays or objects, while true/false never equal 1/0. Array order is significant.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (isinstance(left, (int, float)) and isinstance(right, (int, float))
                and left == right)
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys()
                and all(json_values_equal(value, right[key]) for key, value in left.items()))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                and all(json_values_equal(a, b) for a, b in zip(left, right)))
    return type(left) is type(right) and left == right

def json_pointer_tokens(pointer):
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def json_pointer_get(value, pointer):
    for token in json_pointer_tokens(pointer):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def json_pointer_set(value, pointer, replacement):
    tokens = json_pointer_tokens(pointer)
    if not tokens:
        return replacement
    current = value
    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if isinstance(current, list):
            slot = int(token)
            while len(current) <= slot:
                current.append(None)
            if current[slot] is None:
                current[slot] = [] if next_token.isdigit() else {}
            current = current[slot]
        else:
            current = current.setdefault(token, [] if next_token.isdigit() else {})
    final = tokens[-1]
    if isinstance(current, list):
        slot = int(final)
        while len(current) <= slot:
            current.append(None)
        current[slot] = replacement
    else:
        current[final] = replacement
    return value


def json_path_matches_file_pointer(check):
    value = check.get("value")
    if not isinstance(value, dict):
        raise ValueError("value must identify the evidence-file JSON pointer")
    file_pointer = value.get("file_pointer", value.get("pointer"))
    if "file_pointer" in value and "pointer" in value and value["file_pointer"] != value["pointer"]:
        raise ValueError("conflicting value.file_pointer and value.pointer")
    if not isinstance(file_pointer, str):
        raise ValueError("value.file_pointer (or value.pointer) is required")
    return file_pointer


def check_one(check, response, root, transcript=None, *, synthetic_control=False):
    kind, expected = check["type"], check["value"]
    actual = response
    try:
        if kind in {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
            if transcript is None:
                transcript_path = os.environ.get("EVAL_TRANSCRIPT_PATH")
                if not transcript_path or not Path(transcript_path).is_file():
                    return False, "Execution transcript unavailable; cannot prove command execution"
                transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
            commands, tool_names, matched_ids, matched_order, results = [], [], set(), [], {}
            native_observations = []
            command_exit_evidence = []

            def visit(value):
                if isinstance(value, list):
                    for entry in value:
                        visit(entry)
                elif isinstance(value, dict):
                    if value.get("role") == "system" and isinstance(value.get("content"), str):
                        content = value["content"]
                        if "skill_refiner_native_observation" in content:
                            try:
                                observation = json.loads(content)["skill_refiner_native_observation"]
                                if not isinstance(observation, dict):
                                    raise ValueError("native observation is not an object")
                            except (ValueError, KeyError, TypeError):
                                observation = {"status": "incomplete"}
                            native_observations.append(observation)
                        if "skill_refiner_command_exit_evidence" in content:
                            try:
                                record = json.loads(content)["skill_refiner_command_exit_evidence"]
                                if not isinstance(record, dict):
                                    raise ValueError("command exit evidence is not an object")
                            except (ValueError, KeyError, TypeError):
                                record = {"status": "incomplete"}
                            command_exit_evidence.append(record)
                    if value.get("role") == "tool_call":
                        call = value.get("tool_call", {})
                        tool_name = call.get("name", "")
                        if isinstance(tool_name, str):
                            tool_names.append(tool_name)
                            if kind == "tool_call_absent" and re.search(str(expected), tool_name):
                                call_id = call.get("id")
                                matched_ids.add(call_id if call_id is not None else tool_name)
                        args = call.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        # skill-up's Codex rollout parser wraps native function-call JSON
                        # in arguments.arguments, while its stdout parser emits command directly.
                        if isinstance(args, dict) and isinstance(args.get("arguments"), str):
                            try:
                                args = json.loads(args["arguments"])
                            except ValueError:
                                args = {}
                        command = args.get("command") if isinstance(args, dict) else None
                        if command is None and call.get("name", "").split(".")[-1] == "exec_command" and isinstance(args, dict):
                            command = args.get("cmd")
                        if isinstance(command, str):
                            commands.append(command)
                            if kind != "tool_call_absent" and any(re.search(str(expected), form) for form in command_forms(
                                    command, root, check.get("literal_command", False))):
                                call_id = call.get("id")
                                matched_ids.add(call_id)
                                if call_id is not None and call_id not in matched_order:
                                    matched_order.append(call_id)
                    if value.get("role") == "tool_result":
                        result = value.get("tool_result", {})
                        results[result.get("call_id")] = result
                    for key, entry in value.items():
                        if key not in {"content", "arguments"}:
                            visit(entry)
            visit(transcript)
            def absence_coverage_error():
                if not native_observations:
                    if synthetic_control:
                        return None
                    return ("Tool observation is unavailable; cannot prove absence of external calls or side effects; "
                            "a coverage gap must be recorded")
                for observation in native_observations:
                    if (observation.get("status") != "complete" or observation.get("unclassified_tools") != []
                            or observation.get("absence_of_external_calls_proven") is not True
                            or observation.get("absence_of_side_effects_proven") is not True):
                        return ("Native tool observation does not establish absence of external calls or side effects; "
                                "unknown tools, incomplete capture, or unobserved process effects require a coverage gap")
                return None
            if kind == "tool_command":
                return bool(matched_ids), f"Observed tool commands: {commands!r}"
            if kind == "tool_command_absent":
                if not matched_ids and (coverage_error := absence_coverage_error()):
                    return False, coverage_error
                return not matched_ids, ("No observed tool command matched the forbidden pattern"
                                         if not matched_ids else
                                         f"Forbidden tool commands were observed: {commands!r}")
            if kind == "tool_call_absent":
                if not matched_ids and (coverage_error := absence_coverage_error()):
                    return False, coverage_error
                return not matched_ids, ("No observed tool call name matched the forbidden pattern"
                                         if not matched_ids else
                                         f"Forbidden tool calls were observed: {tool_names!r}")
            expected_exit_code = check.get("expected_exit_code")
            if expected_exit_code is not None:
                import hashlib
                observed_exit_codes = {}
                for record in command_exit_evidence:
                    if (record.get("producer") != "skill-refiner.runtime"
                            or record.get("schema_version") != "skill-refiner.command-exit-evidence/v1"
                            or record.get("status") != "complete" or not isinstance(record.get("results"), dict)):
                        return False, "Runtime command exit evidence is incomplete or conflicting"
                    for call_id, evidence in record["results"].items():
                        result = results.get(call_id)
                        if not isinstance(evidence, dict) or not isinstance(result, dict):
                            continue
                        code, content = evidence.get("exit_code"), result.get("content")
                        if (type(code) is not int or not isinstance(content, str)
                                or evidence.get("status") != ("success" if code == 0 else "error")
                                or result.get("status") != evidence["status"]
                                or hashlib.sha256(content.encode("utf-8")).hexdigest() != evidence.get("content_sha256")):
                            return False, "Runtime command exit evidence does not match the observed tool result"
                        if ((call_id in observed_exit_codes and observed_exit_codes[call_id] != code)
                                or (result.get("exit_code") is not None and result["exit_code"] != code)):
                            return False, "Runtime command exit evidence conflicts with another observed exit code"
                        observed_exit_codes[call_id] = code
                        results[call_id] = {**result, "exit_code": code}
            matching_results = []
            for call_id in matched_order:
                result = results.get(call_id)
                if not isinstance(result, dict):
                    continue
                if expected_exit_code is None:
                    if result.get("status") == "success":
                        matching_results.append(result)
                elif (result.get("exit_code") == expected_exit_code
                      and result.get("status") == ("success" if expected_exit_code == 0 else "error")):
                    matching_results.append(result)
            matching_outputs = [item.get("content", "") for item in matching_results]
            if expected_exit_code is not None and len(matching_results) != len(matched_order):
                return False, (f"Expected exit code {expected_exit_code} from every matching command; observed "
                               f"{[results.get(i, {}).get('exit_code') for i in matched_order]!r}")
            for output in matching_outputs:
                # Match parsed JSON values for structured replies, otherwise exact stripped text.
                try:
                    actual_json = parse_json_output(response)
                    try:
                        output_json = parse_json_output(output)
                    except ValueError:
                        candidates = []
                        for line in output.splitlines():
                            try:
                                candidates.append(json.loads(line))
                            except ValueError:
                                pass
                        if not candidates:
                            continue
                        # Shell tools may include a preceding input-file dump. The
                        # final structured output must still be the forwarded value.
                        output_json = candidates[-1]
                    if json_values_equal(actual_json, output_json):
                        return True, (f"Final JSON equals matching tool output with exit code {expected_exit_code}"
                                      if expected_exit_code is not None else
                                      "Final JSON equals successful matching tool output")
                except ValueError:
                    if response.strip() == output.strip():
                        return True, "Final text equals successful matching tool output"
            # JSON streams have the same insignificant formatting as one JSON
            # value. Preserve all recorded values and their order/count; never
            # require an invented newline between complete business outputs.
            for output in matching_outputs:
                try:
                    if parse_json_sequence(response) and json_values_equal(parse_json_sequence(response), parse_json_sequence(output)):
                        return True, 'Final JSON stream equals successful matching tool output'
                except ValueError:
                    pass
            if len(matching_outputs) == len(matched_order) and matching_outputs:
                try:
                    actual_values = [value for output in matching_outputs for value in parse_json_sequence(output)]
                    if parse_json_sequence(response) and json_values_equal(parse_json_sequence(response), actual_values):
                        return True, 'Final JSON stream equals all ordered complete matching outputs'
                except ValueError:
                    pass
            # A task can request several separate helper invocations and forward
            # each complete stdout in order. Require every matching invocation to
            # have a successful paired result; never fill missing/failed outputs
            # from the assistant's answer or assemble partial JSON fragments.
            if (len(matching_outputs) > 1 and len(matching_outputs) == len(matched_order)
                    and all(isinstance(output, str) for output in matching_outputs)):
                combined = "\n".join(output.rstrip("\r\n") for output in matching_outputs)
                if response.strip() == combined.strip():
                    return True, "Final text equals ordered complete outputs of all matching successful calls"
            exit_context = f" at exit code {expected_exit_code}" if expected_exit_code is not None else ""
            return False, f"Final answer differs from matching tool output{exit_context}: {matching_outputs!r}"
        if kind.startswith("file_"):
            p = relative_path(root, check["path"])
            if kind == "file_exists":
                return p.is_file(), f"file exists: {check['path']}"
            actual = p.read_bytes().decode("utf-8")
        if kind in {"contains", "file_contains"}:
            passed = str(expected) in actual
        elif kind == "contains_any":
            passed = isinstance(expected, list) and any(str(value) in actual for value in expected)
        elif kind == "not_contains":
            passed = str(expected) not in actual
        elif kind == "not_contains_any":
            passed = isinstance(expected, list) and not any(str(value) in actual for value in expected)
        elif kind == "regex":
            passed = re.search(str(expected), actual) is not None
        elif kind == "json_path_regex":
            actual = json_pointer_get(parse_json_output(actual), check["pointer"])
            if not isinstance(actual, str):
                return False, f"json_path_regex: pointer {check['pointer']!r} is not a string"
            passed = re.search(str(expected), actual) is not None
        elif kind == "file_text_equal":
            passed = actual == str(expected)
        elif kind == "text_equal":
            passed = actual.strip() == str(expected).strip()
        elif kind == 'json_sequence_equal':
            values = parse_json_sequence(actual)
            passed = isinstance(expected, list) and json_values_equal(values, expected)
            actual = values
        elif kind == "json_path_matches_file":
            response_value = json_pointer_get(parse_json_output(actual), check["pointer"])
            observed = json.loads(relative_path(root, check["path"]).read_text(encoding="utf-8"))
            source_pointer = json_path_matches_file_pointer(check)
            file_value = json_pointer_get(observed, source_pointer)
            passed = json_values_equal(response_value, file_value)
            actual = {"response": response_value, "file": file_value}
        elif kind in {"json_equal", "file_json_equal", "json_path_equal", "file_json_path_equal"}:
            actual = parse_json_output(actual)
            if kind in {"json_path_equal", "file_json_path_equal"}:
                actual = json_pointer_get(actual, check.get("pointer", ""))
            passed = json_values_equal(actual, expected)
        else:
            raise RefinerError(f"Unknown checker: {kind}")
        return passed, f"{kind}: expected={expected!r}; actual={str(actual)[:1500]!r}"
    except (ValueError, KeyError, IndexError, OSError, TypeError, re.error, RefinerError) as exc:
        return False, f"{kind}: {exc}"


def assertion_diagnostic(check, response, root, *, passed=None, transcript=None, synthetic_control=False):
    """Return a stable, field-level result for reports and plan-control failures."""
    if passed is None:
        passed, evidence = check_one(check, response, root, transcript, synthetic_control=synthetic_control)
    else:
        _, evidence = check_one(check, response, root, transcript, synthetic_control=synthetic_control)
    kind = check["type"]
    pointer = check.get("pointer")
    path = check.get("path")
    if pointer is not None:
        field_path = (f"file:{path}#{pointer}" if kind.startswith("file_") else f"response#{pointer}")
    elif path is not None:
        field_path = f"file:{path}"
    elif kind in {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
        field_path = "tool_transcript"
    else:
        field_path = "response"
    expected = {"operator": kind, "value": check.get("value")}
    for key in ("path", "pointer", "expected_exit_code", "control_command", "control_tool_name"):
        if key in check:
            expected[key] = check[key]
    try:
        if kind.startswith("file_"):
            target = relative_path(root, path)
            if kind == "file_exists":
                actual = target.is_file()
            elif not target.is_file():
                actual = "<missing file>"
            else:
                raw = target.read_bytes().decode("utf-8")
                actual = raw
                if kind in {"file_json_equal", "file_json_path_equal"}:
                    actual = json.loads(raw)
                    if kind == "file_json_path_equal":
                        actual = json_pointer_get(actual, pointer)
        elif kind in {"json_equal", "json_sequence_equal", "json_path_equal", "json_path_regex",
                      "json_path_matches_file"}:
            actual = parse_json_output(response)
            if kind in {"json_path_equal", "json_path_regex", "json_path_matches_file"}:
                actual = json_pointer_get(actual, pointer)
        elif kind in {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
            actual = evidence
        else:
            actual = response
    except (ValueError, KeyError, IndexError, OSError, TypeError, RefinerError) as exc:
        actual = f"<unavailable: {type(exc).__name__}: {exc}>"
    if isinstance(actual, str) and len(actual) > 1500:
        actual = actual[:1500] + "…"
    return {"assertion_id": check["id"], "field_path": field_path,
            "expected_condition": expected, "actual_value": actual,
            "passed": bool(passed), "evidence": evidence}


def _case_response_contract(case):
    """Deterministically freeze the case recipe preserved in the plan/oracle."""
    from .response_contract import freeze_response_contract
    recipe = case.get("response_contract")
    if recipe is None:
        return None
    return freeze_response_contract(recipe["schema_json"], recipe["source_refs"],
                                    allow_markdown_fence=recipe["allow_markdown_fence"])


def grade(case, response, root):
    assertions = []
    status = None
    for check in case["checks"]:
        result = assertion_diagnostic(check, response, root)
        assertions.append({"check": check, **result})
    if case.get("response_contract") is not None:
        from .response_contract import ResponseContractError, validate_response
        try:
            contract = _case_response_contract(case)
            outcome = validate_response(contract, response)
            status = outcome["status"]
            assertions.append({"check": {"id": "response_contract", "type": "json_schema"},
                               "assertion_id": "response_contract", "field_path": "response",
                               "expected_condition": {"schema_sha256": contract["schema_sha256"],
                                                      "contract_sha256": contract["contract_sha256"]},
                               "actual_value": outcome["errors"], "passed": status == "pass",
                               "evidence": "Frozen response contract: " + status})
        except (ResponseContractError, KeyError, TypeError, ValueError) as exc:
            status = "error"
            assertions.append({"check": {"id": "response_contract", "type": "json_schema"},
                               "assertion_id": "response_contract", "field_path": "response",
                               "expected_condition": "valid frozen response contract",
                               "actual_value": type(exc).__name__, "passed": False,
                               "evidence": "Response contract validation error"})
    result = {"case_id": case["id"], "passed": all(a["passed"] for a in assertions),
              "assertions": assertions}
    if status == "error":
        result["status"] = "ERROR"
    return result


def validate_plan(plan, root, max_cases=8, *, required_case_kinds=None,
                  required_scenario_ids=None, expected_scenarios=None, expected_requirements=None,
                  expected_execution_plan=None, verified_response_sources=None,
                  required_response_contract_cases=None):
    """Validate real coverage, oracle paths and discriminating output assertions."""
    from .schemas import PLAN, PLAN_V2, PLAN_V3, PLAN_V4, PLAN_V5, PLAN_V6, PLAN_V7, PLAN_V8
    import jsonschema
    plan_version = plan.get("schema_version")
    legacy = plan_version == "skill-refiner.plan/v2"
    plan_schema = {"skill-refiner.plan/v2": PLAN_V2, "skill-refiner.plan/v3": PLAN_V3,
                   "skill-refiner.plan/v4": PLAN_V4, "skill-refiner.plan/v5": PLAN_V5,
                   "skill-refiner.plan/v6": PLAN_V6, "skill-refiner.plan/v7": PLAN_V7,
                   "skill-refiner.plan/v8": PLAN_V8}.get(plan_version, PLAN)
    jsonschema.validate(plan, plan_schema)
    if plan_version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        if not isinstance(required_response_contract_cases, (list, tuple, set)):
            raise RefinerError("Response contract applicability was not authenticated")
        required_contract_ids = set(required_response_contract_cases)
        if not required_contract_ids <= {case["id"] for case in plan["cases"]}:
            raise RefinerError("Response contract applicability references unknown cases")
        missing_contract_ids = sorted(case["id"] for case in plan["cases"]
                                      if case["id"] in required_contract_ids and case["response_contract"] is None)
        if missing_contract_ids:
            raise RefinerError("Required JSON response contract missing for cases: " + ", ".join(missing_contract_ids))
    if plan_version in {"skill-refiner.plan/v4", "skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        validate_skill_stage_contracts(plan["stage_contracts"], {row["id"] for row in plan["requirements"]})
    if plan_version in {"skill-refiner.plan/v5", "skill-refiner.plan/v6", "skill-refiner.plan/v7", "skill-refiner.plan/v8"}:
        validate_execution_plan(plan["execution_plan"], plan)
        if expected_execution_plan is not None and plan["execution_plan"] != expected_execution_plan:
            raise RefinerError("Independent plan changed the frozen schema-validated execution_plan")
    if plan_version == "skill-refiner.plan/v8":
        from .planned_dependencies import validate_planned_dependencies
        validate_planned_dependencies(plan, verified_response_sources)
    if len(plan["cases"]) > max_cases:
        raise RefinerError(f"Plan exceeds {max_cases} cases")
    # Exact status/state labels are especially easy to invent when an agent
    # fabricates a JSON response contract for a conversational Skill. A
    # generated good_response is only a checker control, not evidence that
    # the target actually uses that label. Require the enum in the task prompt
    # or a documented source contract; implementation literals alone may
    # describe a CLI result rather than the evaluated agent's response.
    contract_text = []
    original_root = Path(root) / "original"
    if original_root.is_dir():
        for source_path in original_root.rglob("*"):
            if source_path.is_file() and source_path.suffix.lower() in {".md", ".yaml", ".yml", ".json"}:
                try:
                    contract_text.append(source_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
    documented_contract = "\n".join(contract_text)

    def accepted_bad_response_checks(checks, response, check_root):
        """Name response oracles that still accept a deliberately wrong control."""
        matched = []
        for index, check in enumerate(checks, 1):
            passed, _ = check_one(check, response, check_root)
            if not passed:
                continue
            item = {"index": index, "type": check["type"]}
            if "pointer" in check:
                item["pointer"] = check["pointer"]
            if "value" in check:
                item["value"] = str(check["value"])[:240]
            matched.append(item)
        return matched

    requirements = {r["id"]: r for r in plan["requirements"]}
    if len(requirements) != len(plan["requirements"]):
        raise RefinerError("Duplicate requirement IDs")
    criteria_by_requirement = ({req_id: {item["id"] for item in row["acceptance_criteria"]}
                                for req_id, row in requirements.items()} if not legacy else {})
    criterion_list = [item["id"] for row in requirements.values()
                      for item in row.get("acceptance_criteria", [])]
    if not legacy and len(criterion_list) != len(set(criterion_list)):
        raise RefinerError("Duplicate acceptance criterion IDs")
    criterion_to_requirement = {criterion_id: req_id for req_id, values in criteria_by_requirement.items()
                                for criterion_id in values}
    covered, covered_criteria, ids = set(), set(), set()
    control_evidence, control_failures, asset_audit_failures = [], [], []
    scenario_failures, scenario_ids, scenario_signatures = [], set(), set()
    if not legacy and expected_requirements is not None and plan["requirements"] != expected_requirements:
        scenario_failures.append({"field_path": "requirements",
                                  "expected_condition": "exactly preserve the frozen source requirement contract",
                                  "actual_value": plan["requirements"]})
    for case in plan["cases"]:
        if case["id"] in ids:
            raise RefinerError(f"Duplicate case ID: {case['id']}")
        ids.add(case["id"])
        for field in ("scenario", "expected_outcome"):
            value = case.get(field, "")
            if not isinstance(value, str) or not value.strip():
                raise RefinerError(f"Case {case['id']} missing required {field}")
            if value.strip() == case["prompt"].strip():
                raise RefinerError(f"Case {case['id']} {field} must be separate from the executor prompt")
        if not set(case["requirement_ids"]) <= requirements.keys():
            raise RefinerError(f"Unknown requirement reference: {case['id']}")
        covered.update(case["requirement_ids"])
        if not legacy:
            case_criteria = set(case["acceptance_criteria_ids"])
            unknown_criteria = sorted(case_criteria - criterion_to_requirement.keys())
            wrong_requirement_links = sorted(criterion_id for criterion_id in case_criteria
                                             if criterion_id in criterion_to_requirement
                                             and criterion_to_requirement[criterion_id] not in case["requirement_ids"])
            criteria_requirements = {criterion_to_requirement[criterion_id] for criterion_id in case_criteria
                                     if criterion_id in criterion_to_requirement}
            if unknown_criteria or wrong_requirement_links or criteria_requirements != set(case["requirement_ids"]):
                scenario_failures.append({"case_id": case["id"], "field_path": "acceptance_criteria_ids",
                                          "expected_condition": "known atomic criteria whose parent requirement IDs equal requirement_ids",
                                          "actual_value": {"criterion_ids": sorted(case_criteria),
                                                           "unknown": unknown_criteria,
                                                           "criteria_for_other_requirements": wrong_requirement_links,
                                                           "criterion_parent_requirements": sorted(criteria_requirements),
                                                           "requirement_ids": sorted(case["requirement_ids"])}})
            covered_criteria.update(case_criteria)
            scenario_id = case["scenario_id"]
            if scenario_id in scenario_ids:
                scenario_failures.append({"case_id": case["id"], "field_path": "scenario_id",
                                          "expected_condition": "a unique concrete scenario ID per executable case",
                                          "actual_value": scenario_id})
            scenario_ids.add(scenario_id)
            detail = case["scenario_detail"]
            signature = json.dumps({key: detail[key] for key in
                                    ("title", "preconditions", "stimulus", "actions", "risk_dimensions")},
                                   ensure_ascii=False, sort_keys=True).casefold()
            if signature in scenario_signatures:
                scenario_failures.append({"case_id": case["id"], "field_path": "scenario_detail",
                                          "expected_condition": "a meaningfully distinct scenario, not a paraphrase or duplicate",
                                          "actual_value": {"scenario_id": scenario_id, "signature": signature}})
            scenario_signatures.add(signature)
        linked_sources = {requirements[item]["source"] for item in case["requirement_ids"]}
        audit = case["asset_audit"]
        required_assets = set(audit["required_paths"])
        available_assets = set(audit["available_paths"])
        fixture_assets = set(case["files"])
        helper_assets = {"helper:" + item for item in case["helpers"]}
        missing_assets = sorted(required_assets - available_assets)
        unlisted_assets = sorted((fixture_assets | helper_assets) - required_assets)
        unprovided_fixtures = sorted((fixture_assets | helper_assets) - available_assets)
        missing_sources = sorted(linked_sources - set(audit["source_refs"]))
        extra_sources = sorted(set(audit["source_refs"]) - linked_sources)
        expected_asset_status = "ready" if required_assets else "not_required"
        unavailable_claims = []
        for asset_path in sorted(available_assets):
            if asset_path in fixture_assets or asset_path in helper_assets:
                continue
            try:
                if not relative_path(original_root, asset_path).is_file():
                    unavailable_claims.append(asset_path)
            except RefinerError:
                unavailable_claims.append(asset_path)
        if (missing_assets or unlisted_assets or unprovided_fixtures or unavailable_claims
                or missing_sources or extra_sources
                or audit["status"] != expected_asset_status):
            asset_audit_failures.append({
                "case_id": case["id"], "field_path": "asset_audit",
                "expected_condition": {"status": expected_asset_status,
                                       "all_required_available": True,
                                        "available_source_assets_exist": True,
                                       "fixtures_and_helpers_listed": True,
                                       "source_refs_equal_linked_sources": True},
                "actual_value": {"status": audit["status"], "missing_assets": missing_assets,
                                 "unlisted_assets": unlisted_assets,
                                 "unprovided_fixtures": unprovided_fixtures,
                                  "unavailable_claims": unavailable_claims,
                                 "missing_sources": missing_sources, "extra_sources": extra_sources}})
        linked_requirements = [requirements[item]["behavior"] for item in case["requirement_ids"]]
        boundary_contract = "\n".join([case["prompt"], *linked_requirements]).lower()
        if re.search(r"(?:invalid|malformed|bad|non.?utf|encoding|encoded).{0,35}(?:utf.?8|byte)|(?:utf.?8|byte).{0,35}(?:invalid|malformed|decode|encoding)", boundary_contract):
            override = re.compile(
                r"(?is)(?:\b(?:PYTHONIOENCODING|PYTHONUTF8|LC_ALL|LC_CTYPE|PYTHONCOERCECLOCALE)\b"
                r"\s*=\s*['\"][^'\"]+['\"]|['\"](?:PYTHONIOENCODING|PYTHONUTF8|LC_ALL|LC_CTYPE|"
                r"PYTHONCOERCECLOCALE)['\"]\s*:\s*['\"][^'\"]+['\"])")
            for filename, content in case["files"].items():
                if override.search(content):
                    raise RefinerError(
                        f"Boundary fixture {case['id']} overrides the runtime decoding environment in {filename}; "
                        "test invalid bytes under ordinary runtime defaults"
                    )
        for name in case["files"]:
            if name == 'refiner-python-probe.py':
                raise RefinerError('Fixture cannot replace the frozen Python observer')
            relative_path(root, name)
            if (name.split("/")[0] in {".codex", ".agents", ".refiner", "evals", ".cursor", ".claude"}
                    or Path(name).name in {"AGENTS.md", "CLAUDE.md", ".cursorrules", ".mcp.json"}
                    or name.endswith("SKILL.md")):
                raise RefinerError(f"Fixture cannot inject Skill instructions or oracles: {name}")
        case_contract = None
        source_bindings = {}
        if plan_version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} and case["response_contract"] is not None:
            from .response_contract import ResponseContractError
            if not isinstance(verified_response_sources, dict):
                raise RefinerError("Response contract source evidence has not been verified")
            refs = case["response_contract"]["source_refs"]
            for ref in refs:
                source_hash = verified_response_sources.get(ref)
                if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
                    raise RefinerError("Response contract source is missing or unauthenticated: " + ref)
                source_bindings[ref] = source_hash
            try:
                case_contract = _case_response_contract(case)
            except (ResponseContractError, KeyError, TypeError, ValueError) as exc:
                raise RefinerError("Response contract is unsupported or invalid") from exc
        assertion_ids = [check["id"] for check in case["checks"]]
        if plan_version in {"skill-refiner.plan/v7", "skill-refiner.plan/v8"} and "response_contract" in assertion_ids:
            raise RefinerError("response_contract is reserved for the frozen contract assertion")
        if len(assertion_ids) != len(set(assertion_ids)):
            raise RefinerError(f"Duplicate assertion id in case {case['id']}: {assertion_ids}")
        positive_controls = case["positive_controls"]
        negative_controls = case["negative_controls"]
        control_ids = [control["id"] for control in positive_controls + negative_controls]
        if len(control_ids) != len(set(control_ids)):
            raise RefinerError(f"Duplicate control id in case {case['id']}: {control_ids}")
        positive_texts = [control["response"].strip() for control in positive_controls]
        negative_texts = [control["response"].strip() for control in negative_controls]
        if len(positive_texts) != len(set(positive_texts)):
            raise RefinerError(f"Positive controls must use distinct response wording: {case['id']}")
        if len(negative_texts) != len(set(negative_texts)):
            raise RefinerError(f"Negative controls must use distinct response wording: {case['id']}")
        if set(positive_texts) & set(negative_texts):
            raise RefinerError(f"A response cannot be both a positive and negative control: {case['id']}")
        if not positive_controls or positive_controls[0]["response"] != case["good_response"]:
            raise RefinerError(f"Canonical positive control mismatch: {case['id']}")
        if case["bad_response"] not in {control["response"] for control in negative_controls}:
            raise RefinerError(f"Canonical bad_response must be one of the negative controls: {case['id']}")
        if "good_responses" in case:
            responses = [control["response"] for control in positive_controls]
            # Both complete and canonical-excluded forms describe the same
            # positives; every positive control is still evaluated.
            if case["good_responses"] not in (responses, responses[1:]):
                raise RefinerError(f"Legacy good_responses do not match positive_controls: {case['id']}")
        for check in case["checks"]:
            if (check["type"] == "json_path_equal"
                    and check.get("pointer", "").rsplit("/", 1)[-1].lower() in {"status", "state"}
                    and isinstance(check.get("value"), str)
                    and check["value"] not in case["prompt"]
                    and not re.search(
                        rf"(?i)\bstatus\b[^\n]{{0,120}}\b{re.escape(check['value'])}\b",
                        documented_contract)):
                raise RefinerError(
                    f"Ungrounded exact status/state value in {case['id']}: {check['value']!r}; "
                    "state the enum in the prompt or ground it in the documented Skill contract"
                )
            if "expected_exit_code" in check and check["type"] != "tool_output_matches":
                raise RefinerError(f"expected_exit_code is only valid for tool_output_matches: {case['id']}")
            if "expected_exit_code" in check and (type(check["expected_exit_code"]) is not int
                                                    or check["expected_exit_code"] < 0):
                raise RefinerError(f"Invalid expected_exit_code: {case['id']}")
            if check.get("expected_exit_code") == 0:
                raise RefinerError(
                    f"Redundant expected_exit_code=0 in {case['id']}; successful tool-result status proves success, "
                    "and some adapters omit numeric exit codes"
                )
            if check["type"].startswith("file_"):
                relative_path(root, check.get("path", ""))
            elif check["type"] == "json_path_matches_file":
                relative_path(root, check.get("path", ""))
                try:
                    json_pointer_tokens(check.get("pointer", ""))
                    file_pointer = json_path_matches_file_pointer(check)
                    json_pointer_tokens(file_pointer)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RefinerError(f"Invalid JSON pointer in {case['id']}: {exc}") from exc
            elif check["type"] == "json_path_regex":
                try:
                    json_pointer_tokens(check.get("pointer", ""))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RefinerError(f"Invalid JSON pointer in {case['id']}: {exc}") from exc
            if check["type"] in {"regex", "json_path_regex", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}:
                try:
                    pattern = re.compile(check["value"])
                except (re.error, TypeError) as exc:
                    raise RefinerError(f"Invalid checker regex: {case['id']}: {exc}") from exc
                if pattern.search(""):
                    raise RefinerError(f"Checker regex matches empty evidence: {case['id']}")
            if check["type"] in {"contains", "not_contains", "file_contains"} and not str(check["value"]):
                raise RefinerError(f"Empty content check: {case['id']}")
            if check["type"] in {"contains_any", "not_contains_any"} and (
                    not isinstance(check["value"], list) or len(check["value"]) < 2
                    or any(not isinstance(value, str) or not value.strip() for value in check["value"])):
                raise RefinerError(f"{check['type']} needs at least two nonempty equivalent phrases: {case['id']}")
        substantive = [c for c in case["checks"] if c["type"] not in {
            "file_exists", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}]
        if not substantive:
            raise RefinerError(f"Case {case['id']} only tests file/command existence, not task results")
        non_response_types = {"file_exists", "tool_command", "tool_command_absent",
                              "tool_call_absent", "tool_output_matches"}
        response_checks = [c for c in substantive if c["type"] not in non_response_types
                           and not c["type"].startswith("file_")]
        if not response_checks:
            raise RefinerError(f"Case {case['id']} needs at least one response assertion for its business outcome")
        if response_checks:
            text_only_oracle = (
                any(c["type"] in {"contains", "contains_any", "not_contains", "not_contains_any", "regex", "text_equal"}
                    for c in response_checks)
                and not any(c["type"].startswith("json_") for c in response_checks)
            )
            if text_only_oracle and len(positive_controls) < 3:
                raise RefinerError(
                    f"Natural-language case {case['id']} needs at least three distinct positive controls"
                )
            dynamic = [c for c in response_checks if c["type"] == "json_path_matches_file"]
            def control_root_for(response, control_root):
                if not dynamic:
                    return
                good = parse_json_output(response)
                observed_by_path = {}
                for check in dynamic:
                    value = json_pointer_get(good, check["pointer"])
                    relative = check["path"]
                    observed = observed_by_path.setdefault(relative, {})
                    observed_by_path[relative] = json_pointer_set(
                        observed, json_path_matches_file_pointer(check), value)
                for relative, observed in observed_by_path.items():
                    path = relative_path(control_root, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(observed), encoding="utf-8")

            control_results = []
            def response_contract_result(response):
                if case_contract is None:
                    return None
                from .response_contract import ResponseContractError, validate_response
                try:
                    return validate_response(case_contract, response)
                except (ResponseContractError, ValueError, TypeError) as exc:
                    raise RefinerError("Response contract validation could not complete") from exc

            for control in positive_controls:
                setup_error = None
                try:
                    with tempfile.TemporaryDirectory() as temp_root:
                        control_root_for(control["response"], temp_root)
                        results = [assertion_diagnostic(check, control["response"], temp_root)
                                   for check in response_checks]
                except (KeyError, TypeError, ValueError) as exc:
                    setup_error = f"{type(exc).__name__}: {exc}"
                    results = [assertion_diagnostic(check, control["response"], root)
                               for check in response_checks]
                contract_result = response_contract_result(control["response"])
                control_results.append({"control_id": control["id"], "polarity": "positive",
                                        "assertions": results,
                                        **({"response_contract": contract_result} if contract_result else {})})
                failures = [row for row in results if not row["passed"]]
                if contract_result and contract_result["status"] != "pass":
                    failures.append({"assertion_id": "response_contract", "field_path": "response",
                                     "expected_condition": "passes the frozen response contract",
                                     "actual_value": contract_result})
                if failures or setup_error:
                    control_failures.append({
                        "case_id": case["id"], "control_id": control["id"],
                        "expected_condition": "every response assertion passes",
                        "actual_value": failures, "setup_error": setup_error})

            semantic_ids = {check["id"] for check in response_checks}
            required_targets = semantic_ids | ({"response_contract"} if case_contract else set())
            targeted_ids = {control["assertion_id"] for control in negative_controls}
            unknown_targets = sorted(targeted_ids - required_targets)
            uncovered_assertions = sorted(required_targets - targeted_ids)
            if unknown_targets or uncovered_assertions:
                control_failures.append({
                    "case_id": case["id"], "field_path": "negative_controls[].assertion_id",
                    "expected_condition": {"targets_exactly": sorted(required_targets)},
                    "actual_value": {"unknown_targets": unknown_targets,
                                     "assertions_without_negative_control": uncovered_assertions}})
            for control in negative_controls:
                if case_contract and control["assertion_id"] == "response_contract":
                    contract_result = response_contract_result(control["response"])
                    control_results.append({"control_id": control["id"], "polarity": "negative",
                                            "target_assertion_id": "response_contract",
                                            "assertions": [], "response_contract": contract_result})
                    if contract_result["status"] != "fail":
                        control_failures.append({"case_id": case["id"], "control_id": control["id"],
                            "target_assertion_id": "response_contract", "field_path": "response",
                            "expected_condition": "contract rejects the response",
                            "actual_value": contract_result})
                    continue
                setup_error = None
                try:
                    with tempfile.TemporaryDirectory() as temp_root:
                        control_root_for(positive_controls[0]["response"], temp_root)
                        results = [assertion_diagnostic(check, control["response"], temp_root)
                                   for check in response_checks]
                except (KeyError, TypeError, ValueError) as exc:
                    setup_error = f"{type(exc).__name__}: {exc}"
                    results = [assertion_diagnostic(check, control["response"], root)
                               for check in response_checks]
                actual_failures = {row["assertion_id"] for row in results if not row["passed"]}
                expected_failures = {control["assertion_id"]}
                contract_result = response_contract_result(control["response"])
                control_results.append({"control_id": control["id"], "polarity": "negative",
                                        "target_assertion_id": control["assertion_id"],
                                        "assertions": results,
                                        **({"response_contract": contract_result} if contract_result else {})})
                if contract_result and contract_result["status"] != "pass":
                    control_failures.append({"case_id": case["id"], "control_id": control["id"],
                        "target_assertion_id": control["assertion_id"], "field_path": "response",
                        "expected_condition": "semantic negative must satisfy the response contract",
                        "actual_value": contract_result})
                if actual_failures != expected_failures:
                    control_failures.append({
                        "case_id": case["id"], "control_id": control["id"],
                        "target_assertion_id": control["assertion_id"],
                        "field_path": next((row["field_path"] for row in results
                                            if row["assertion_id"] == control["assertion_id"]), "checks"),
                        "expected_condition": {"failed_assertion_ids": sorted(expected_failures),
                                               "all_other_assertions": "pass"},
                        "actual_value": {"failed_assertion_ids": sorted(actual_failures),
                                         "assertions": results}, "setup_error": setup_error})
        auxiliary_results = []

        def check_auxiliary(control_id, check, response, check_root, transcript, expected_pass):
            observed, _ = check_one(check, response, check_root, transcript, synthetic_control=True)
            result = assertion_diagnostic(check, response, check_root,
                                          passed=observed, transcript=transcript, synthetic_control=True)
            auxiliary_results.append({"control_id": control_id, "polarity": "auxiliary",
                                      "assertions": [result]})
            if observed != expected_pass:
                control_failures.append({
                    "case_id": case["id"], "control_id": control_id,
                    "assertion_id": result["assertion_id"], "field_path": result["field_path"],
                    "expected_condition": {"passed": expected_pass,
                                           "condition": result["expected_condition"]},
                    "actual_value": result["actual_value"]})
        for check in case["checks"]:
            if check["type"] == "tool_command_absent":
                command = check.get("control_command", "")
                if not command or not re.search(str(check["value"]), command):
                    control_failures.append({"case_id": case["id"], "assertion_id": check["id"],
                                             "field_path": "control_command",
                                             "expected_condition": {"matches": check["value"]},
                                             "actual_value": command})
                    continue
                check_auxiliary("positive-no-command", check, positive_controls[0]["response"],
                                root, [], True)
                trace = [{"role": "tool_call", "tool_call": {"id": "forbidden", "name": "command_execution",
                          "arguments": {"command": command}}}]
                check_auxiliary("negative-forbidden-command", check, positive_controls[0]["response"],
                                root, trace, False)
            if check["type"] == "tool_call_absent":
                tool_name = check.get("control_tool_name", "")
                if not tool_name or not re.search(str(check["value"]), tool_name):
                    control_failures.append({"case_id": case["id"], "assertion_id": check["id"],
                                             "field_path": "control_tool_name",
                                             "expected_condition": {"matches": check["value"]},
                                             "actual_value": tool_name})
                    continue
                check_auxiliary("positive-no-tool-call", check, positive_controls[0]["response"],
                                root, [], True)
                trace = [{"role": "tool_call", "tool_call": {"id": "forbidden", "name": tool_name,
                          "arguments": {}}}]
                check_auxiliary("negative-forbidden-tool-call", check, positive_controls[0]["response"],
                                root, trace, False)
            if check["type"] != "tool_output_matches":
                continue
            command = check.get("control_command", "")
            if not command or not re.search(str(check["value"]), command):
                control_failures.append({"case_id": case["id"], "assertion_id": check["id"],
                                         "field_path": "control_command",
                                         "expected_condition": {"matches": check["value"]},
                                         "actual_value": command})
                continue
            def trace(output):
                exit_code = check.get("expected_exit_code", 0)
                return [{"role": "tool_call", "tool_call": {"id": "control", "name": "command_execution",
                         "arguments": {"command": command}}},
                        {"role": "tool_result", "tool_result": {"call_id": "control",
                         "status": "success" if exit_code == 0 else "error", "exit_code": exit_code,
                         "content": output}}]
            check_auxiliary("positive-observed-tool-output", check, positive_controls[0]["response"],
                            root, trace(positive_controls[0]["response"]), True)
            check_auxiliary("negative-mismatched-tool-output", check, case["good_response"],
                            root, trace(case["bad_response"]), False)
        for check in substantive:
            if not check["type"].startswith("file_"):
                continue
            with tempfile.TemporaryDirectory() as control_root:
                p = relative_path(control_root, check["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                value = check['value']
                if check['type'] == 'file_json_path_equal':
                    tokens = [t.replace('~1','/').replace('~0','~') for t in check.get('pointer','').strip('/').split('/') if t]
                    for token in reversed(tokens):
                        if token.isdigit():
                            index = int(token)
                            if index > 1000:
                                raise RefinerError('Control JSON pointer index too large')
                            value = [None]*index+[value]
                        else:
                            value = {token:value}
                content = json.dumps(value) if check['type'] in {'file_json_equal','file_json_path_equal'} else str(value)
                p.write_bytes(content.encode('utf-8'))
                check_auxiliary("positive-file-fixture", check, "", control_root, None, True)
                p.unlink()
                check_auxiliary("negative-missing-file", check, "", control_root, None, False)
        control_evidence.append({"case_id": case["id"], "controls": control_results,
                                 "auxiliary_assertions": auxiliary_results,
                                 **({"response_contract_sha256": case_contract["contract_sha256"],
                                     "source_content_sha256": source_bindings} if case_contract else {})})
    uncovered = [r["id"] for r in plan["requirements"] if r["required"] and r["id"] not in covered]
    kinds = {c["kind"] for c in plan["cases"]}
    coverage_failures = list(asset_audit_failures)
    if uncovered:
        coverage_failures.append({"field_path": "requirements[].case_coverage",
                                  "expected_condition": "every required requirement maps to a case",
                                  "actual_value": {"uncovered_requirement_ids": uncovered}})
    if (not required_case_kinds and
            ("normal" not in kinds or not kinds & {"boundary", "error", "negative"})):
        coverage_failures.append({"field_path": "cases[].kind",
                                  "expected_condition": "normal plus boundary/error/negative scenarios",
                                  "actual_value": sorted(kinds)})
    if not legacy:
        required_criteria = {criterion_id for req_id, criterion_ids in criteria_by_requirement.items()
                             if requirements[req_id]["required"] for criterion_id in criterion_ids}
        uncovered_criteria = sorted(required_criteria - covered_criteria)
        if uncovered_criteria:
            coverage_failures.append({"field_path": "requirements[].acceptance_criteria[].case_coverage",
                                      "expected_condition": "each required atomic acceptance criterion maps to at least one case",
                                      "actual_value": {"uncovered_criterion_ids": uncovered_criteria}})
        missing_kinds = sorted(set(required_case_kinds or []) - kinds)
        if missing_kinds:
            coverage_failures.append({"field_path": "cases[].kind",
                                      "expected_condition": "cover every explicitly applicable scenario category",
                                      "actual_value": {"missing_kinds": missing_kinds, "present_kinds": sorted(kinds)}})
        required_scenario_ids = set(required_scenario_ids or [])
        missing_scenarios = sorted(required_scenario_ids - scenario_ids)
        if missing_scenarios:
            coverage_failures.append({"field_path": "cases[].scenario_id",
                                      "expected_condition": "cover every applicable frozen scenario ID",
                                      "actual_value": {"missing_scenario_ids": missing_scenarios}})
        if expected_scenarios is not None:
            source_by_id = {row["id"]: row for row in expected_scenarios}
            for case in plan["cases"]:
                source = source_by_id.get(case["scenario_id"])
                if source is None or not source["applicable"]:
                    scenario_failures.append({"case_id": case["id"], "field_path": "scenario_id",
                                              "expected_condition": "an applicable scenario in the frozen business analysis",
                                              "actual_value": case["scenario_id"]})
                    continue
                if (case["kind"] != source["kind"]
                        or case["requirement_ids"] != source["requirement_ids"]
                        or case["acceptance_criteria_ids"] != source["acceptance_criteria_ids"]
                        or case["scenario"].strip() != source["situation"].strip()
                        or any(case["scenario_detail"][key] != source["detail"][key]
                               for key in ("title", "expected_observables", "forbidden_outcomes",
                                           "risk_dimensions", "basis"))):
                    scenario_failures.append({"case_id": case["id"], "field_path": "scenario_detail",
                                              "expected_condition": "preserve the frozen scenario ID, trace, expected outcomes, forbidden outcomes and risk dimensions; use fresh concrete stimuli",
                                              "actual_value": {"scenario_id": case["scenario_id"],
                                                               "source_scenario_id": source["id"]}})
            unknown_source_ids = sorted(scenario_ids - set(source_by_id))
            if unknown_source_ids:
                coverage_failures.append({"field_path": "cases[].scenario_id",
                                          "expected_condition": "only scenario IDs from the frozen analysis",
                                          "actual_value": {"unknown_scenario_ids": unknown_source_ids}})
    coverage_failures.extend(scenario_failures)
    evidence = {"schema_version": "skill-refiner.plan-controls/v1",
                "status": "failed" if control_failures else "invalid_coverage" if coverage_failures else "passed",
                "case_control_results": control_evidence, "control_failures": control_failures,
                "coverage_failures": coverage_failures}
    if control_failures:
        raise PlanControlError("Frozen plan controls failed: " + json.dumps(control_failures, ensure_ascii=False), evidence)
    if coverage_failures:
        raise PlanValidationError("Plan coverage self-check failed: " + json.dumps(coverage_failures, ensure_ascii=False), evidence)
    return {"status": "passed", "case_control_results": control_evidence}


def validate_execution_plan(execution_plan, plan):
    """Validate step traceability, the dependency DAG, safe parallel waves and recovery bounds."""
    try:
        validate_value(execution_plan, schema_ref("skill-refiner.execution-plan"),
                       subject="execution plan")
    except ValueError as exc:
        raise RefinerError(str(exc)) from exc
    steps = execution_plan["steps"]
    step_ids = [step["id"] for step in steps]
    orders = [step["order"] for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise RefinerError("Execution plan step IDs must be unique")
    if sorted(orders) != list(range(1, len(steps) + 1)):
        raise RefinerError("Execution plan order must be unique and contiguous from 1")
    ordered = sorted(steps, key=lambda row: row["order"])
    by_id = {step["id"]: step for step in ordered}
    requirement_ids = {row["id"] for row in plan["requirements"]}
    case_ids = {row["id"] for row in plan["cases"]}
    covered_requirements, covered_cases = set(), set()
    declared_schemas = {(row["id"], row["version"])
                        for row in plan["stage_contracts"]["schemas"]}
    output_paths = []
    for step in ordered:
        if not set(step["requirement_ids"]) <= requirement_ids:
            raise RefinerError(f"Execution step {step['id']} references an unknown requirement")
        if not set(step["case_ids"]) <= case_ids:
            raise RefinerError(f"Execution step {step['id']} references an unknown test case")
        covered_requirements.update(step["requirement_ids"])
        covered_cases.update(step["case_ids"])
        for dependency in step["depends_on"]:
            if dependency not in by_id:
                raise RefinerError(f"Execution step {step['id']} depends on unknown step {dependency}")
            if by_id[dependency]["order"] >= step["order"]:
                raise RefinerError(f"Execution dependency {dependency} -> {step['id']} is cyclic or out of order")
        for output in step["outputs"]:
            output_path = Path(output["path"])
            if output_path.is_absolute() or ".." in output_path.parts or "\\" in output["path"]:
                raise RefinerError(f"Execution step {step['id']} has unsafe output path {output['path']}")
            if (output["schema_id"], output["schema_version"]) not in declared_schemas:
                raise RefinerError(f"Execution step {step['id']} output {output['path']} references an undeclared schema")
            output_paths.append(output["path"].casefold())
        failure = step["failure_policy"]
        if failure["action"] == "stop" and failure["recovery_policy"] != "blocked":
            raise RefinerError(f"Execution step {step['id']} stops but does not record a blocked recovery policy")
    missing_requirements = sorted(requirement_ids - covered_requirements)
    missing_cases = sorted(case_ids - covered_cases)
    if missing_requirements or missing_cases:
        raise RefinerError("Execution plan omits requirements or cases: " + json.dumps(
            {"missing_requirement_ids": missing_requirements, "missing_case_ids": missing_cases},
            ensure_ascii=False))
    if len(output_paths) != len(set(output_paths)):
        raise RefinerError("Execution plan output artifact paths must be unique")
    waves = {}
    for step in ordered:
        if step["execution_mode"] == "parallel":
            waves.setdefault(step["parallel_wave"], []).append(step)
    if waves and execution_plan["max_parallel_agents"] == 1:
        raise RefinerError("Execution plan schedules parallel work with max_parallel_agents=1")
    for wave_id, members in waves.items():
        if len(members) < 2:
            raise RefinerError(f"Parallel wave {wave_id} must contain at least two independent tasks")
        if len(members) > execution_plan["max_parallel_agents"]:
            raise RefinerError(f"Parallel wave {wave_id} exceeds max_parallel_agents")
        member_ids = {step["id"] for step in members}
        occupied = []
        for step in members:
            if member_ids.intersection(step["depends_on"]):
                raise RefinerError(f"Parallel wave {wave_id} contains dependent tasks")
            for scope in step["agent"]["write_scope"]:
                normalized = scope.replace("\\", "/").strip("/").casefold()
                for prior, prior_step in occupied:
                    if (normalized == prior or normalized.startswith(prior + "/")
                            or prior.startswith(normalized + "/")):
                        raise RefinerError(f"Parallel tasks {prior_step} and {step['id']} have overlapping write scopes")
                occupied.append((normalized, step["id"]))


def validate_skill_stage_contracts(contract, requirement_ids):
    """Check cross-Skill phase and artifact schema links after structural validation."""
    try:
        validate_value(contract, schema_ref("skill-refiner.skill-stage-contracts"),
                       subject="Skill stage contracts")
    except ValueError as exc:
        raise RefinerError(str(exc)) from exc
    stages = contract["stages"]
    stage_ids = [stage["id"] for stage in stages]
    if len(stage_ids) != len(set(stage_ids)):
        raise RefinerError("Skill stage contracts contain duplicate stage IDs")
    schemas = contract["schemas"]
    schema_ids = [row["id"] for row in schemas]
    if len(schema_ids) != len(set(schema_ids)):
        raise RefinerError(
            "Skill stage contracts must declare exactly one active version per logical schema ID; "
            "remove historical or compatibility versions from the target Skill contract"
        )
    schema_keys = [(row["id"], row["version"]) for row in schemas]
    if len(schema_keys) != len(set(schema_keys)):
        raise RefinerError("Skill stage contracts contain duplicate schema id/version pairs")
    schema_map = {(row["id"], row["version"]): row for row in schemas}
    required_points = {"after_generation", "before_handoff", "on_resume", "final_verification"}
    used_schemas = set()
    for stage in stages:
        unknown_requirements = set(stage["requirement_ids"]) - requirement_ids
        if unknown_requirements:
            raise RefinerError(f"Stage {stage['id']} references unknown requirements: "
                               + ", ".join(sorted(unknown_requirements)))
        if set(stage["validation_points"]) != required_points:
            raise RefinerError(f"Stage {stage['id']} must validate after generation, before handoff, "
                               "on resume, and during final verification")
        output_paths = [row["path"] for row in stage["outputs"]]
        if len(output_paths) != len(set(output_paths)):
            raise RefinerError(f"Stage {stage['id']} declares duplicate output paths")
        for artifact in [*stage["inputs"], *stage["outputs"]]:
            key = (artifact["schema_id"], artifact["schema_version"])
            schema = schema_map.get(key)
            if schema is None:
                raise RefinerError(f"Stage {stage['id']} artifact {artifact['path']} references an undeclared schema")
            used_schemas.add(key)
            path = Path(artifact["path"])
            if path.is_absolute() or ".." in path.parts:
                raise RefinerError(f"Stage {stage['id']} artifact path is unsafe: {artifact['path']}")
            schema_path = Path(schema["path"])
            validator_path = Path(schema["validator_path"])
            if (schema_path.is_absolute() or ".." in schema_path.parts
                    or validator_path.is_absolute() or ".." in validator_path.parts):
                raise RefinerError(f"Skill schema path is unsafe: {schema['path']}")
            if (len(schema_path.parts) < 3 or schema_path.parts[0] != "schemas"
                    or schema_path.parts[1] != schema["version"]):
                raise RefinerError(
                    f"Skill schema path version directory must match its declared version: "
                    f"{schema['path']} vs {schema['version']}"
                )
            if schema.get("media_type") and artifact["media_type"] != schema["media_type"]:
                raise RefinerError(f"Stage {stage['id']} artifact {artifact['path']} media type conflicts "
                                   f"with schema {schema['id']}")
    unused = sorted(set(schema_keys) - used_schemas)
    if unused:
        raise RefinerError("Skill stage contract declares unused schemas: "
                           + ", ".join(f"{schema_id}@{version}" for schema_id, version in unused))


def grader_main(case_path, expected_sha256=None, expected_case_id=None):
    import hashlib
    try:
        raw = Path(case_path).read_bytes()
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RefinerError("Frozen oracle hash mismatch")
        case = json.loads(raw.decode("utf-8"))
        if expected_case_id is not None and case.get("id") != expected_case_id:
            raise RefinerError("Frozen oracle case identity mismatch")
        result = grade(case, os.environ.get("EVAL_FINAL_MESSAGE", ""), Path.cwd())
    except (OSError, ValueError, TypeError, RefinerError):
        result = {"case_id": expected_case_id, "status": "ERROR", "passed": False,
                  "error": "Frozen oracle or response contract could not be validated"}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("status") == "ERROR" else (0 if result["passed"] else 1)


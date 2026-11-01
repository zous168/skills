"""Internal contracts, deliberately separate from skill-up's native YAML schema."""

import copy

CHECK = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "type", "value"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "type": {"enum": ["contains", "contains_any", "not_contains", "not_contains_any", "regex", "text_equal", "tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches", "json_equal", "json_sequence_equal", "json_path_equal", "json_path_regex",
                           "json_path_matches_file", "file_exists", "file_contains", "file_text_equal", "file_json_equal", "file_json_path_equal"]},
        "value": {}, "path": {"type": "string"}, "pointer": {"type": "string"},
        "control_command": {"type": "string"}, "control_tool_name": {"type": "string"},
        "expected_exit_code": {"type": "integer", "minimum": 0},
        "literal_command": {"type": "boolean"},
    },
    "allOf": [{
        "if": {"properties": {"type": {"enum": ["contains_any", "not_contains_any"]}}},
        "then": {"properties": {"value": {"type": "array", "minItems": 2,
                                                "uniqueItems": True,
                                                "items": {"type": "string", "minLength": 1}}}},
    }, {
        "if": {"properties": {"type": {"const": "json_path_matches_file"}}},
        "then": {
            "required": ["path", "pointer"],
            "properties": {"value": {
                "type": "object",
                "oneOf": [
                    {"required": ["file_pointer"], "properties": {"file_pointer": {"type": "string"}}},
                    {"required": ["pointer"], "properties": {"pointer": {"type": "string"}}},
                ],
            }},
        },
    }, {
        "if": {"properties": {"type": {"const": "json_path_regex"}}},
        "then": {"required": ["pointer"], "properties": {"value": {"type": "string"}}},
    }, {
        "if": {"properties": {"type": {"const": "tool_call_absent"}}},
        "then": {"required": ["control_tool_name"], "properties": {"value": {"type": "string"}}},
    }],
}
ASSET_AUDIT = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "required_paths", "available_paths", "source_refs", "basis"],
    "properties": {
        "status": {"enum": ["ready", "not_required", "missing"]},
        "required_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "available_paths": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "source_refs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "basis": {"type": "string", "minLength": 12},
    },
}
POSITIVE_CONTROL = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "response"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "response": {"type": "string"},
    },
}
NEGATIVE_CONTROL = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "assertion_id", "response", "rationale"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "assertion_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "response": {"type": "string"},
        "rationale": {"type": "string", "minLength": 12},
    },
}
ACCEPTANCE_CRITERION = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "statement", "evidence"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "statement": {"type": "string", "minLength": 8},
        "evidence": {"type": "string", "minLength": 8},
    },
}
REQUIREMENTS = {"type": "array", "minItems": 1, "items": {
    "type": "object", "additionalProperties": False,
    "required": ["id", "behavior", "source", "required", "acceptance_criteria"],
    "properties": {"id": {"type": "string"}, "behavior": {"type": "string", "minLength": 8},
                   "source": {"type": "string", "minLength": 3}, "required": {"type": "boolean"},
                   "acceptance_criteria": {"type": "array", "minItems": 1,
                                           "items": ACCEPTANCE_CRITERION}},
}}
RISK_DIMENSIONS = ["input_variation", "workflow_state", "role_permission", "dependency_failure",
                   "cross_requirement", "side_effect", "retry_recovery", "idempotency",
                   "ambiguity_conflict", "asset_handling", "safety_boundary", "other"]
SCENARIO_DETAIL = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "preconditions", "stimulus", "actions", "expected_observables",
                 "forbidden_outcomes", "risk_dimensions", "basis"],
    "properties": {
        "title": {"type": "string", "minLength": 8},
        "preconditions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
        "stimulus": {"type": "string", "minLength": 8},
        "actions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
        "expected_observables": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 5}},
        "forbidden_outcomes": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 5}},
        "risk_dimensions": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"enum": RISK_DIMENSIONS}},
        "basis": {"type": "string", "minLength": 12},
    },
}
SCENARIO_DETAIL_V3 = copy.deepcopy(SCENARIO_DETAIL)
# Chinese conveys many complete labels in fewer codepoints than English.
SCENARIO_DETAIL_V3["properties"]["title"] = {"type": "string", "allOf": [
    {"if": {"not": {"pattern": "[\\u3400-\\u9fff]{2}"}}, "then": {"minLength": 8}}]}
SCENARIO_DETAIL_V3["properties"]["forbidden_outcomes"]["items"] = {"type": "string", "allOf": [
    {"if": {"not": {"pattern": "[\\u3400-\\u9fff]{2}"}}, "then": {"minLength": 5}}]}
SCENARIO_INTENT = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "kind", "applicable", "situation", "requirement_ids",
                 "acceptance_criteria_ids", "detail", "basis", "exclusion_reason"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "kind": {"enum": ["normal", "boundary", "error", "negative", "regression"]},
        "applicable": {"type": "boolean"}, "situation": {"type": "string", "minLength": 8},
        "requirement_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"type": "string"}},
        "acceptance_criteria_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                    "items": {"type": "string"}},
        "detail": SCENARIO_DETAIL, "basis": {"type": "string", "minLength": 12},
        "exclusion_reason": {"type": "string"},
    },
    "allOf": [{"if": {"properties": {"applicable": {"const": False}}},
               "then": {"properties": {"exclusion_reason": {"type": "string", "minLength": 12}}}}],
}
CASE_V2 = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "requirement_ids", "kind", "scenario", "expected_outcome", "prompt", "files", "checks",
                 "helpers", "asset_audit", "positive_controls", "negative_controls",
                 "oracle_reason", "good_response", "bad_response"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "requirement_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "kind": {"enum": ["normal", "boundary", "error", "negative", "regression"]},
        "scenario": {"type": "string", "minLength": 1},
        "expected_outcome": {"type": "string", "minLength": 1},
        "prompt": {"type": "string", "minLength": 1},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "helpers": {"type": "array", "uniqueItems": True, "items": {"enum": ["python_probe"]}},
        "asset_audit": ASSET_AUDIT,
        "positive_controls": {"type": "array", "minItems": 1, "items": POSITIVE_CONTROL},
        "negative_controls": {"type": "array", "minItems": 1, "items": NEGATIVE_CONTROL},
        "checks": {"type": "array", "minItems": 1, "items": CHECK},
        "oracle_reason": {"type": "string", "minLength": 12},
        "good_response": {"type": "string"},
        "good_responses": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "bad_response": {"type": "string"},
    },
}
CASE = {
    **CASE_V2,
    "required": CASE_V2["required"] + ["scenario_id", "acceptance_criteria_ids", "scenario_detail"],
    "properties": {**CASE_V2["properties"],
                    "scenario_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                    "acceptance_criteria_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                                "items": {"type": "string"}},
                    "scenario_detail": SCENARIO_DETAIL},
}
PLAN_V2 = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "requirements", "cases", "coverage_gaps", "assumptions"],
    "properties": {
        "schema_version": {"const": "skill-refiner.plan/v2"},
        "requirements": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "behavior", "source", "required"],
            "properties": {"id": {"type": "string"}, "behavior": {"type": "string"},
                           "source": {"type": "string"}, "required": {"type": "boolean"}},
        }},
        "cases": {"type": "array", "minItems": 2, "items": CASE},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
}
PLAN_V4 = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "requirements", "cases", "coverage_gaps", "assumptions", "stage_contracts"],
    "properties": {
        "schema_version": {"const": "skill-refiner.plan/v4"},
        "requirements": REQUIREMENTS,
        "cases": {"type": "array", "minItems": 2, "items": CASE},
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "stage_contracts": {
            "type": "object", "additionalProperties": False,
            "required": ["schema_version", "schemas", "stages"],
            "properties": {
                "schema_version": {"const": "skill-refiner.skill-stage-contracts/v1"},
                "schemas": {"type": "array", "minItems": 1,
                            "description": "Declare one active version per stable logical schema ID. Keep historical compatibility formats inside the authoring controller, not in a delivered target Skill contract.",
                            "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "version", "path", "validator_id", "validator_path", "runtime_needed"],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                        "version": {"type": "string", "pattern": "^v[0-9]+$"},
                        "path": {"type": "string", "pattern": "^schemas/v[0-9]+/[A-Za-z0-9._/-]+\\.schema\\.json$",
                                  "description": "The vN directory must match the declared schema version."},
                        "validator_id": {"type": "string", "minLength": 1},
                        "validator_path": {"type": "string", "minLength": 1},
                        "runtime_needed": {"type": "boolean"},
                        "media_type": {"type": "string", "minLength": 1},
                    },
                }},
                "stages": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "goal", "requirement_ids", "inputs", "outputs", "validation_gate",
                                 "validation_points", "failure_states", "completion_check", "resume_policy"],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                        "goal": {"type": "string", "minLength": 8},
                        "requirement_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                            "items": {"type": "string"}},
                        "inputs": {"type": "array", "items": {"$ref": "#/$defs/stage_artifact"}},
                        "outputs": {"type": "array", "minItems": 1,
                                    "items": {"$ref": "#/$defs/stage_artifact"}},
                        "validation_gate": {"type": "string", "minLength": 8},
                        "validation_points": {"type": "array", "minItems": 4, "uniqueItems": True,
                            "items": {"enum": ["after_generation", "before_handoff", "on_resume", "final_verification"]}},
                        "failure_states": {"type": "array", "minItems": 1, "uniqueItems": True,
                                           "items": {"type": "string", "minLength": 1}},
                        "completion_check": {"type": "string", "minLength": 8},
                        "resume_policy": {"enum": ["reuse_if_contract_hashes_match", "retry_incomplete", "restart_stage"]},
                    },
                }},
            },
        },
    },
    "$defs": {"stage_artifact": {
        "type": "object", "additionalProperties": False,
        "required": ["path", "media_type", "schema_id", "schema_version", "required"],
        "properties": {
            "path": {"type": "string", "minLength": 1}, "media_type": {"type": "string", "minLength": 1},
            "schema_id": {"type": "string"}, "schema_version": {"type": "string", "pattern": "^v[0-9]+$"},
            "required": {"type": "boolean"},
        },
    }},
}
PLAN_V3 = {
    **PLAN_V4,
    "required": ["schema_version", "requirements", "cases", "coverage_gaps", "assumptions"],
    "properties": {**PLAN_V4["properties"], "schema_version": {"const": "skill-refiner.plan/v3"}},
}
EXECUTION_PLAN = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "objective", "scope", "max_parallel_agents", "steps"],
    "properties": {
        "schema_version": {"const": "skill-refiner.execution-plan/v1"},
        "objective": {"type": "string", "minLength": 8},
        "scope": {"type": "object", "additionalProperties": False,
            "required": ["in_scope", "out_of_scope", "constraints"],
            "properties": {"in_scope": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "out_of_scope": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                           "constraints": {"type": "array", "items": {"type": "string", "minLength": 1}}}},
        "max_parallel_agents": {"type": "integer", "minimum": 1, "maximum": 2},
        "steps": {"type": "array", "minItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "order", "title", "goal", "actions", "inputs", "outputs",
                         "requirement_ids", "case_ids", "depends_on", "execution_mode", "parallel_wave",
                         "serial_reason", "agent", "exit_criteria", "failure_policy"],
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                "order": {"type": "integer", "minimum": 1},
                "title": {"type": "string", "minLength": 3},
                "goal": {"type": "string", "minLength": 8},
                "actions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 5}},
                "inputs": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 3}},
                "outputs": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["path", "schema_id", "schema_version", "evidence"],
                    "properties": {"path": {"type": "string", "minLength": 1},
                                   "schema_id": {"type": "string", "minLength": 1},
                                   "schema_version": {"type": "string", "pattern": "^v[0-9]+$"},
                                   "evidence": {"type": "string", "minLength": 5}}}},
                "requirement_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                     "items": {"type": "string"}},
                "case_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                             "items": {"type": "string"}},
                "depends_on": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                "execution_mode": {"enum": ["serial", "parallel"]},
                "parallel_wave": {"type": ["string", "null"]},
                "serial_reason": {"type": "string"},
                "agent": {"type": "object", "additionalProperties": False,
                    "required": ["role", "scope", "write_scope", "merge_owner", "join_gate"],
                    "properties": {"role": {"type": "string", "minLength": 3},
                                   "scope": {"type": "string", "minLength": 8},
                                   "write_scope": {"type": "array", "minItems": 1, "uniqueItems": True,
                                                   "items": {"type": "string", "minLength": 1}},
                                   "merge_owner": {"type": "string", "minLength": 2},
                                   "join_gate": {"type": "string", "minLength": 8}}},
                "exit_criteria": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "condition", "evidence"],
                    "properties": {"id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
                                   "condition": {"type": "string", "minLength": 8},
                                   "evidence": {"type": "string", "minLength": 5}}}},
                "failure_policy": {"type": "object", "additionalProperties": False,
                    "required": ["action", "max_attempts", "terminal_state", "recovery_policy"],
                    "properties": {"action": {"enum": ["stop", "bounded_retry", "replan", "block"]},
                                   "max_attempts": {"type": "integer", "minimum": 0, "maximum": 2},
                                   "terminal_state": {"enum": ["blocked", "error", "not_passed"]},
                                   "recovery_policy": {"enum": ["reuse_verified", "rerun_incomplete", "restart_step", "blocked"]}}},
            },
            "allOf": [
                {"if": {"properties": {"execution_mode": {"const": "serial"}}},
                 "then": {"required": ["serial_reason"], "properties": {"serial_reason": {"minLength": 8}, "parallel_wave": {"type": "null"}}}},
                {"if": {"properties": {"execution_mode": {"const": "parallel"}}},
                 "then": {"required": ["parallel_wave"], "properties": {"parallel_wave": {"type": "string", "minLength": 1}}}},
                {"if": {"properties": {"failure_policy": {"properties": {"action": {"const": "stop"}}}}},
                 "then": {"properties": {"failure_policy": {"properties": {"max_attempts": {"const": 0}}}}}},
                {"if": {"properties": {"failure_policy": {"properties": {"action": {"enum": ["bounded_retry", "replan"]}}}}},
                 "then": {"properties": {"failure_policy": {"properties": {"max_attempts": {"minimum": 1}}}}}},
            ],
        }},
    },
}
PLAN_V5 = {
    **PLAN_V4,
    "required": PLAN_V4["required"] + ["execution_plan"],
    "properties": {**PLAN_V4["properties"],
                    "schema_version": {"const": "skill-refiner.plan/v5"},
                    "execution_plan": EXECUTION_PLAN},
}
PLAN_V6 = copy.deepcopy(PLAN_V5)
PLAN_V6["properties"]["schema_version"] = {"const": "skill-refiner.plan/v6"}
PLAN_V6["properties"]["cases"]["items"]["properties"]["scenario_detail"] = SCENARIO_DETAIL_V3
# v7 declares each case's independent response contract.
PLAN_V7 = copy.deepcopy(PLAN_V6)
PLAN_V7["properties"]["schema_version"] = {"const": "skill-refiner.plan/v7"}
PLAN_V7["properties"]["cases"]["items"]["required"].append("response_contract")
PLAN_V7["properties"]["cases"]["items"]["properties"]["response_contract"] = {
    "anyOf": [{"type": "null"}, {
        "type": "object", "additionalProperties": False,
        "required": ["schema_json", "source_refs", "allow_markdown_fence"],
        "properties": {
            "schema_json": {"type": "string", "minLength": 2},
            "source_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1}},
            "allow_markdown_fence": {"type": "boolean"},
        },
    }],
}
# v8 adds explicit not-yet-created target dependencies per case. An empty array
# means the case has no such target dependencies; it never implies availability.
PLAN_V8 = copy.deepcopy(PLAN_V7)
PLAN_V8["$id"] = "skill-refiner.plan-v8"
PLAN_V8["x-schema-version"] = "v8"
PLAN_V8["properties"]["schema_version"] = {"const": "skill-refiner.plan/v8"}
_dependency_scenario = {
    "type": "object", "additionalProperties": False,
    "required": ["stimulus", "expected", "assertion_ids"],
    "properties": {
        "stimulus": {"type": "string", "minLength": 8},
        "expected": {"type": "string", "minLength": 8},
        "assertion_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                          "items": {"type": "string", "minLength": 1}},
    },
}
PLAN_V8["properties"]["cases"]["items"]["required"].append("planned_target_dependencies")
PLAN_V8["properties"]["cases"]["items"]["properties"]["planned_target_dependencies"] = {
    "type": "array", "maxItems": 40, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "target_path", "asset_kind", "requirement_ids", "source_refs", "implementation_step_id",
                     "input_contract", "output_contract", "positive_scenario", "negative_scenario", "execution_contract"],
        "properties": {
            "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            "target_path": {"type": "string", "minLength": 1, "maxLength": 240},
            "asset_kind": {"enum": ["script", "schema", "reference", "other"]},
            "requirement_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "source_refs": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "implementation_step_id": {"type": "string", "minLength": 1},
            "input_contract": {"type": "string", "minLength": 8},
            "output_contract": {"type": "string", "minLength": 8},
            "positive_scenario": copy.deepcopy(_dependency_scenario),
            "negative_scenario": copy.deepcopy(_dependency_scenario),
            "execution_contract": {
                "type": "object", "additionalProperties": False,
                "required": ["runner", "argv", "expected_exit_code", "expected_stdout", "positive_control", "negative_control"],
                "properties": {
                    "runner": {"const": "docker-python-response"},
                    "argv": {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 4096}},
                    "expected_exit_code": {"type": "integer", "minimum": 0, "maximum": 124},
                    "expected_stdout": {"type": "string", "maxLength": 65536},
                    "positive_control": {"$ref": "#/$defs/target_execution_control"},
                    "negative_control": {"$ref": "#/$defs/target_execution_control"},
                },
            },
        },
    },
}
PLAN_V8.setdefault("$defs", {})["target_execution_control"] = {
    "type": "object", "additionalProperties": False,
    "required": ["input", "expected_exit_code", "expected_stdout"],
    "properties": {"input": {"type": "string", "maxLength": 65536},
                   "expected_exit_code": {"type": "integer", "minimum": 0, "maximum": 124},
                   "expected_stdout": {"type": "string", "maxLength": 65536}},
}
PLAN = PLAN_V5
REVIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["approved", "issues"],
    "properties": {"approved": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}},
    "allOf": [{"if": {"properties": {"approved": {"const": False}}},
               "then": {"properties": {"issues": {"minItems": 1}}},
               "else": {"properties": {"issues": {"maxItems": 0}}}}],
}
PLAN_REVIEW_V2 = {
    **REVIEW,
    "required": ["approved", "issues", "case_contracts"],
    "properties": {**REVIEW["properties"], "case_contracts": {
        "type": "array", "minItems": 2, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["case_id", "installed_source_unchanged", "fixture_contract_consistent",
                         "response_contract_consistent", "trace_checks_observable",
                         "scenario_consistent", "requirement_trace_consistent",
                         "expected_outcome_consistent", "machine_checks_consistent",
                         "matrix_complete", "asset_audit_consistent",
                         "positive_controls_validated", "negative_controls_targeted", "basis"],
            "properties": {"case_id": {"type": "string"},
                           "installed_source_unchanged": {"type": "boolean"},
                           "fixture_contract_consistent": {"type": "boolean"},
                           "response_contract_consistent": {"type": "boolean"},
                           "trace_checks_observable": {"type": "boolean"},
                           "scenario_consistent": {"type": "boolean"},
                           "requirement_trace_consistent": {"type": "boolean"},
                           "expected_outcome_consistent": {"type": "boolean"},
                           "machine_checks_consistent": {"type": "boolean"},
                           "matrix_complete": {"type": "boolean"},
                           "asset_audit_consistent": {"type": "boolean"},
                           "positive_controls_validated": {"type": "boolean"},
                           "negative_controls_targeted": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}},
        },
    }},
}
PLAN_REVIEW_V3 = {
    **PLAN_REVIEW_V2,
    "properties": {**PLAN_REVIEW_V2["properties"], "case_contracts": {
        **PLAN_REVIEW_V2["properties"]["case_contracts"],
        "items": {**PLAN_REVIEW_V2["properties"]["case_contracts"]["items"],
                  "required": PLAN_REVIEW_V2["properties"]["case_contracts"]["items"]["required"] +
                  ["acceptance_criteria_traceable", "scenario_detail_complete", "risk_coverage_complete"],
                  "properties": {**PLAN_REVIEW_V2["properties"]["case_contracts"]["items"]["properties"],
                                 "acceptance_criteria_traceable": {"type": "boolean"},
                                 "scenario_detail_complete": {"type": "boolean"},
                                 "risk_coverage_complete": {"type": "boolean"}}},
    }},
}
PLAN_REVIEW_V4 = {
    **PLAN_REVIEW_V3,
    "required": PLAN_REVIEW_V3["required"] + ["stage_contracts_assessed", "stage_contracts_complete",
                                                 "schema_paths_safe", "validator_lifecycle_covered"],
    "properties": {**PLAN_REVIEW_V3["properties"],
                    "stage_contracts_assessed": {"type": "boolean"},
                    "stage_contracts_complete": {"type": "boolean"},
                    "schema_paths_safe": {"type": "boolean"},
                    "validator_lifecycle_covered": {"type": "boolean"}},
}
PLAN_REVIEW = {
    **PLAN_REVIEW_V4,
    "required": PLAN_REVIEW_V4["required"] + ["execution_plan_assessed", "execution_plan_complete",
        "dependency_graph_valid", "concurrency_safe", "failure_recovery_bounded", "step_assessments"],
    "properties": {**PLAN_REVIEW_V4["properties"],
        "execution_plan_assessed": {"type": "boolean"},
        "execution_plan_complete": {"type": "boolean"},
        "dependency_graph_valid": {"type": "boolean"},
        "concurrency_safe": {"type": "boolean"},
        "failure_recovery_bounded": {"type": "boolean"},
        "step_assessments": {"type": "array", "minItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["step_id", "goal_actions_clear", "inputs_outputs_evidenced",
                         "requirement_case_traceable", "dependencies_valid", "agent_scope_safe",
                         "exit_and_recovery_clear", "basis"],
            "properties": {"step_id": {"type": "string"},
                           "goal_actions_clear": {"type": "boolean"},
                           "inputs_outputs_evidenced": {"type": "boolean"},
                           "requirement_case_traceable": {"type": "boolean"},
                           "dependencies_valid": {"type": "boolean"},
                           "agent_scope_safe": {"type": "boolean"},
                           "exit_and_recovery_clear": {"type": "boolean"},
                           "basis": {"type": "string", "minLength": 12}}}},
    },
}
PLAN_REVIEW_RECOVERY = copy.deepcopy(PLAN_REVIEW)
PLAN_REVIEW_RECOVERY["required"].append("recovery_assessments")
PLAN_REVIEW_RECOVERY["properties"]["recovery_assessments"] = {
    "type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["change_id", "preserves_requirements", "reason"],
        "properties": {"change_id": {"type": "string", "minLength": 1},
                       "preserves_requirements": {"type": "boolean"},
                       "reason": {"type": "string", "minLength": 12}},
    },
}
# V7 review explicitly maps each rejected issue to affected scenarios/cases.
# A reviewer must mark uncertainty instead of silently authorizing all cases.
PLAN_REVIEW_V7 = copy.deepcopy(PLAN_REVIEW)
PLAN_REVIEW_RECOVERY_V7 = copy.deepcopy(PLAN_REVIEW_RECOVERY)
for _review in (PLAN_REVIEW_V7, PLAN_REVIEW_RECOVERY_V7):
    _review["required"].append("revision_targets")
    _review["properties"]["revision_targets"] = {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["issue_index", "case_ids", "requirement_ids", "scenario_ids", "scope_known", "basis"],
            "properties": {
                "issue_index": {"type": "integer", "minimum": 0},
                "case_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "requirement_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "scenario_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "scope_known": {"type": "boolean"},
                "basis": {"type": "string", "minLength": 12},
            }}}
# v8 independently reviews every planned target dependency and coverage.
PLAN_REVIEW_V8 = copy.deepcopy(PLAN_REVIEW_V7)
PLAN_REVIEW_RECOVERY_V8 = copy.deepcopy(PLAN_REVIEW_RECOVERY_V7)
for _review8 in (PLAN_REVIEW_V8, PLAN_REVIEW_RECOVERY_V8):
    _review8["required"].extend(["planned_dependency_coverage_complete", "planned_dependency_assessments"])
    _review8["properties"]["planned_dependency_coverage_complete"] = {"type": "boolean"}
    _review8["properties"]["planned_dependency_assessments"] = {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
          "required": ["case_id", "dependency_id", "source_grounded", "interface_executable", "scenarios_discriminate", "implementation_link_valid", "basis"],
          "properties": {"case_id": {"type": "string", "minLength": 1}, "dependency_id": {"type": "string", "minLength": 1},
             "source_grounded": {"type": "boolean"}, "interface_executable": {"type": "boolean"},
             "scenarios_discriminate": {"type": "boolean"}, "implementation_link_valid": {"type": "boolean"},
             "basis": {"type": "string", "minLength": 12}}}}
REPAIR = {
    "type": "object", "additionalProperties": False,
    "required": ["diagnosis", "changes", "unresolved"],
    "properties": {
        "diagnosis": {"type": "string"}, "unresolved": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "content", "reason", "evidence_ids"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                           "reason": {"type": "string", "minLength": 8},
                           "evidence_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
        }},
    },
}


from __future__ import annotations


OBJECT = {"type": "object"}

CASE_ASSERTION_SCHEMA = {
    "type": "object",
    "required": ["id", "kind", "expected"],
    "properties": {
        "id": {"type": "string", "pattern": r"^A-[A-Za-z0-9._-]+$"},
        "kind": {
            "enum": [
                "exit_code",
                "stdout_contains",
                "json_schema",
                "file_exists",
            ]
        },
        "expected": {},
    },
    "additionalProperties": False,
}

TEST_CASE_SCHEMA = {
    "type": "object",
    "required": ["id", "kind", "requirement_ids", "argv", "assertions"],
    "properties": {
        "id": {"type": "string", "pattern": r"^C-[A-Za-z0-9._-]+$"},
        "kind": {
            "enum": ["normal", "boundary", "error", "negative", "regression", "acceptance"]
        },
        "requirement_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "argv": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
        },
        "stdin": {"type": "string"},
        "assertions": {
            "type": "array",
            "minItems": 1,
            "items": CASE_ASSERTION_SCHEMA,
        },
        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
    },
    "additionalProperties": False,
}

WORK_ITEM_SCHEMA = {
    "type": "object",
    "required": [
        "id", "title", "scope", "requirement_ids", "depends_on", "done_when",
        "development_case_ids", "acceptance_case_ids",
    ],
    "properties": {
        "id": {"type": "string", "pattern": r"^W-[A-Za-z0-9._-]+$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "scope": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "requirement_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "depends_on": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": r"^W-[A-Za-z0-9._-]+$"}},
        "done_when": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "development_case_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "pattern": r"^C-[A-Za-z0-9._-]+$"}},
        "acceptance_case_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "pattern": r"^C-[A-Za-z0-9._-]+$"}},
    },
    "additionalProperties": False,
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["requirements"],
    "properties": {"requirements": {"type": "array", "minItems": 1, "items": {"type": "object"}}},
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "required": ["requirements", "work_items", "development_cases", "acceptance_cases"],
    "properties": {
        "requirements": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "work_items": {"type": "array", "minItems": 1, "items": WORK_ITEM_SCHEMA},
        "development_cases": {"type": "array", "minItems": 1, "items": TEST_CASE_SCHEMA},
        "acceptance_cases": {"type": "array", "minItems": 1, "items": TEST_CASE_SCHEMA},
    },
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["approved", "issues", "checks"],
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "object",
            "required": [
                "requirement_coverage", "atomic_work_items", "observable_done_when",
                "evidence_mapping", "dependency_order", "progress_handoff",
            ],
            "properties": {
                "requirement_coverage": {"type": "boolean"},
                "atomic_work_items": {"type": "boolean"},
                "observable_done_when": {"type": "boolean"},
                "evidence_mapping": {"type": "boolean"},
                "dependency_order": {"type": "boolean"},
                "progress_handoff": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["changes", "summary"],
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "operation"],
                "properties": {
                    "path": {"type": "string"},
                    "operation": {"enum": ["write", "delete"]},
                    "content": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

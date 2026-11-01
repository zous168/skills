"""Frozen, source-linked response contracts for use by plan and runtime graders.

This module does not infer a contract from an existing target. The plan gate and
skill-up grader use it for v7 response contracts; callers must verify source
references and independently review the contract against frozen requirements.
Unsupported schema features and unavailable evidence fail closed.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
from urllib.parse import unquote

import jsonschema
from .native_tools import sanitize_tool_value


VERSION = "skill-refiner.response-contract/v1"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
MAX_BYTES = 100_000
MAX_SCHEMA_NODES = 2000
MAX_DEPTH = 64
CONTRACT_ASSERTION = "response_contract"


class ResponseContractError(ValueError):
    pass


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResponseContractError("Contract is not finite JSON") from exc


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_schema(schema):
    if not isinstance(schema, dict):
        raise ResponseContractError("Response schema must be a JSON object")
    if len(_canonical(_decimal_safe(schema))) > MAX_BYTES:
        raise ResponseContractError("Response schema exceeds byte limit")
    if schema.get("$schema", DRAFT) != DRAFT:
        raise ResponseContractError("Only JSON Schema Draft 2020-12 is supported")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except (jsonschema.SchemaError, RecursionError) as exc:
        raise ResponseContractError("Invalid response JSON Schema") from exc

    allowed = {"$schema", "$id", "$defs", "$ref", "$comment", "title", "description",
               "default", "examples", "deprecated", "readOnly", "writeOnly", "type", "enum",
               "const", "required", "properties", "patternProperties", "additionalProperties",
               "items", "prefixItems", "minItems", "maxItems", "uniqueItems", "contains",
               "minContains", "maxContains", "minLength", "maxLength", "pattern", "minimum",
               "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minProperties",
               "maxProperties", "propertyNames", "dependentRequired", "dependentSchemas",
               "unevaluatedProperties", "unevaluatedItems", "allOf", "anyOf", "oneOf", "not",
               "if", "then", "else", "format"}
    singles = {"additionalProperties", "items", "contains", "propertyNames",
               "unevaluatedProperties", "unevaluatedItems", "not", "if", "then", "else"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    maps = {"$defs", "properties", "patternProperties", "dependentSchemas"}
    nodes = 0

    def resolve(ref):
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise ResponseContractError("External schema references are forbidden")
        fragment = unquote(ref[1:])
        if fragment and not fragment.startswith("/"):
            raise ResponseContractError("Only local JSON Pointer references are supported")
        value = schema
        for part in fragment.split("/")[1:] if fragment else []:
            if any(part[i] == "~" and (i + 1 == len(part) or part[i+1] not in "01")
                   for i in range(len(part))):
                raise ResponseContractError("Invalid local JSON Pointer reference")
            token = part.replace("~1", "/").replace("~0", "~")
            try:
                value = value[int(token)] if isinstance(value, list) else value[token]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ResponseContractError("Local schema reference does not resolve") from exc
        if not isinstance(value, (dict, bool)):
            raise ResponseContractError("Local schema reference does not identify a schema")
        return value

    def visit(node, depth=0, references=()):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SCHEMA_NODES or depth > MAX_DEPTH:
            raise ResponseContractError("Response schema exceeds structure limit")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ResponseContractError("Invalid nested response schema")
        unsupported = [k for k in node if k not in allowed and not k.startswith("x-")]
        if unsupported:
            raise ResponseContractError("Unsupported response schema keyword: " + str(unsupported[0]))
        if depth and "$id" in node:
            raise ResponseContractError("Nested schema identifiers are not supported")
        if node.get("$schema", DRAFT) != DRAFT:
            raise ResponseContractError("Nested schema uses a different draft")
        if any(key in node for key in ("pattern", "patternProperties", "format")):
            raise ResponseContractError("Regex and format constraints require a timeout-isolated validator")
        if "$ref" in node:
            ref = node["$ref"]
            target = resolve(ref)
            try:
                jsonschema.Draft202012Validator.check_schema(target)
            except (jsonschema.SchemaError, RecursionError) as exc:
                raise ResponseContractError("Referenced value is not a valid JSON Schema") from exc
            if ref in references:
                raise ResponseContractError("Recursive schema references are not supported")
            visit(target, depth + 1, references + (ref,))
        for key in singles:
            if key in node:
                visit(node[key], depth + 1, references)
        for key in arrays:
            for child in node.get(key, []):
                visit(child, depth + 1, references)
        for key in maps:
            for child in node.get(key, {}).values():
                visit(child, depth + 1, references)

    visit(schema)


def _contains_float(value):
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_float(child) for child in value)
    return False


def _parse_schema_json(text):
    try:
        result = json.loads(text, object_pairs_hook=_no_duplicate_pairs,
                            parse_constant=_bad_constant, parse_float=_decimal_number, parse_int=_int_number)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ResponseContractError("Schema source is not supported exact JSON") from exc
    return result


def freeze_response_contract(schema, source_refs, *, allow_markdown_fence=False):
    """Freeze the exact supplied schema and unverified source locators.

    Input with fractional numeric literals must be supplied as its original JSON
    text/bytes; a float-containing dict may already have lost source precision.
    The caller must independently verify allowed paths, content hashes and
    symlinks and pin contract_sha256 in an externally frozen plan.
    """
    if isinstance(schema, bytes):
        try:
            schema_text = schema.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResponseContractError("Schema source must be UTF-8") from exc
    elif isinstance(schema, str):
        schema_text = schema
    else:
        if _contains_float(schema):
            raise ResponseContractError("Float schema input requires exact original JSON text or bytes")
        schema_text = _canonical(schema).decode("utf-8")
    if len(schema_text.encode("utf-8")) > MAX_BYTES:
        raise ResponseContractError("Response schema exceeds byte limit")
    parsed = _parse_schema_json(schema_text)
    _validate_schema(parsed)
    if not isinstance(source_refs, list) or not source_refs or any(
            not isinstance(ref, str) or not ref.strip() for ref in source_refs):
        raise ResponseContractError("Response contract needs nonempty source references")
    if type(allow_markdown_fence) is not bool:
        raise ResponseContractError("Markdown fence policy must be boolean")
    frozen = {"schema_version": VERSION, "media_type": "application/json",
              "schema_json": schema_text, "unverified_source_refs": list(source_refs),
              "allow_markdown_fence": allow_markdown_fence,
              "schema_sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest()}
    frozen["contract_sha256"] = _hash(frozen)
    return frozen


def _check_frozen(contract):
    if not isinstance(contract, dict) or set(contract) != {
            "schema_version", "media_type", "schema_json", "unverified_source_refs", "allow_markdown_fence",
            "schema_sha256", "contract_sha256"}:
        raise ResponseContractError("Invalid frozen response contract record")
    if contract.get("schema_version") != VERSION or contract.get("media_type") != "application/json":
        raise ResponseContractError("Unsupported frozen response contract")
    content = {key: value for key, value in contract.items() if key != "contract_sha256"}
    if not isinstance(contract["schema_json"], str):
        raise ResponseContractError("Frozen schema source must be text")
    if (contract["contract_sha256"] != _hash(content) or
            contract["schema_sha256"] != hashlib.sha256(contract["schema_json"].encode("utf-8")).hexdigest()):
        raise ResponseContractError("Frozen response contract hash mismatch")
    if (not isinstance(contract["unverified_source_refs"], list) or not contract["unverified_source_refs"]
            or any(not isinstance(ref, str) or not ref.strip() for ref in contract["unverified_source_refs"])
            or type(contract["allow_markdown_fence"]) is not bool):
        raise ResponseContractError("Invalid response contract provenance or presentation policy")
    if len(contract["schema_json"].encode("utf-8")) > MAX_BYTES:
        raise ResponseContractError("Response schema exceeds byte limit")
    parsed = _parse_schema_json(contract["schema_json"])
    _validate_schema(parsed)
    return parsed


def _no_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError("non-finite JSON number")



class UnsupportedJSONNumber(ValueError):
    pass


def _int_number(text):
    if len(text.lstrip("-")) > 1024:
        raise UnsupportedJSONNumber("integer exceeds exact validation limits")
    return int(text)


def _decimal_number(text):
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise UnsupportedJSONNumber("invalid decimal") from exc
    digits = value.as_tuple().digits
    if not value.is_finite() or len(digits) > 1024 or abs(value.as_tuple().exponent) > 10000:
        raise UnsupportedJSONNumber("number exceeds exact validation limits")
    return value


def _validation_value(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_validation_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _validation_value(child) for key, child in value.items()}
    return value


def _decimal_safe(value):
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dict):
        return {key: _decimal_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_decimal_safe(child) for child in value]
    return value


def _is_number(checker, value):
    return not isinstance(value, bool) and isinstance(value, (int, float, Decimal))


def _is_integer(checker, value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, Decimal) and value == value.to_integral_value()


def _multiple_of(validator, divisor, instance, schema):
    if not validator.is_type(instance, "number"):
        return
    try:
        quotient = Fraction(instance) / Fraction(divisor)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise ResponseContractError("Could not evaluate exact multipleOf") from exc
    if quotient.denominator != 1:
        yield jsonschema.ValidationError("number is not a multiple of the required divisor")


_EXACT_TYPES = jsonschema.Draft202012Validator.TYPE_CHECKER.redefine_many(
    {"number": _is_number, "integer": _is_integer})
_EXACT_VALIDATOR = jsonschema.validators.extend(jsonschema.Draft202012Validator,
                                                  {"multipleOf": _multiple_of},
                                                  type_checker=_EXACT_TYPES)

def _safe_actual(value):
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (dict, list)):
        encoded = _canonical(_decimal_safe(value))
        return {"type": type(value).__name__, "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    return sanitize_tool_value(value)


def validate_response(contract, response):
    """Validate one response; undeclared contracts never force JSON."""
    if contract is None:
        return {"status": "not_applicable", "schema_sha256": None, "errors": []}
    schema = _check_frozen(contract)
    if not isinstance(response, str):
        return {"status": "fail", "schema_sha256": contract["schema_sha256"],
                "errors": [{"path": "", "constraint": "response_type", "actual": type(response).__name__}]}
    text = response.strip()
    if contract["allow_markdown_fence"] and text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```" and lines[0].strip() in ("```", "```json"):
            text = "\n".join(lines[1:-1])
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_pairs, parse_constant=_bad_constant,
                           parse_float=_decimal_number, parse_int=_int_number)
    except UnsupportedJSONNumber:
        return {"status": "error", "schema_sha256": contract["schema_sha256"],
                "errors": [{"path": "", "constraint": "numeric_precision_unsupported",
                            "actual": "<number exceeds exact validation limits>"}]}
    except (ValueError, TypeError, RecursionError):
        return {"status": "fail", "schema_sha256": contract["schema_sha256"],
                "errors": [{"path": "", "constraint": "json_parse", "actual": "<invalid JSON>"}]}
    try:
        validator = _EXACT_VALIDATOR(schema)
        errors = []
        for error in validator.iter_errors(value):
            if len(errors) >= 256:
                raise ResponseContractError("Response validation exceeds error limit")
            parts = list(error.absolute_path)
            if error.validator == "required" and isinstance(error.instance, dict):
                missing = [name for name in error.validator_value if name not in error.instance]
                for name in missing:
                    path = "".join("/" + str(part).replace("~", "~0").replace("/", "~1")
                                   for part in parts + [name])
                    errors.append({"path": sanitize_tool_value(path), "constraint": "required",
                                   "expected": True, "actual": {"missing": True}})
            else:
                path = "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)
                errors.append({"path": sanitize_tool_value(path), "constraint": error.validator or "schema",
                               "expected": _safe_actual(error.validator_value),
                               "actual": _safe_actual(error.instance)})
        if len(errors) > 256:
            raise ResponseContractError("Response validation exceeds error limit")
    except ResponseContractError:
        raise
    except Exception as exc:
        raise ResponseContractError("Response schema could not be evaluated safely") from exc
    return {"status": "fail" if errors else "pass", "schema_sha256": contract["schema_sha256"],
            "errors": errors}


def audit_controls(contract, positives, negatives, evaluate_semantics, *, expected_assertion_ids):
    """Run one frozen contract and a complete, explicitly bound semantic set.

    Every semantic assertion requires its own targeted negative control. A
    contract failure cannot stand in for a failed semantic assertion. A contract
    control validates the contract independently without requiring JSON-aware
    semantic callbacks to parse deliberately malformed input.
    """
    if (not isinstance(expected_assertion_ids, (list, tuple)) or not expected_assertion_ids
            or any(not isinstance(value, str) or not value for value in expected_assertion_ids)
            or len(set(expected_assertion_ids)) != len(expected_assertion_ids)
            or CONTRACT_ASSERTION in expected_assertion_ids):
        raise ResponseContractError("Expected semantic assertion IDs must be explicit, nonempty and unique")
    expected = set(expected_assertion_ids)
    if not positives or not negatives:
        raise ResponseContractError("Controls must include positive and negative examples")
    all_rows = [("positive", row) for row in positives] + [("negative", row) for row in negatives]
    if any(not isinstance(row, dict) for _, row in all_rows):
        raise ResponseContractError("Control must be an object")
    ids = [row.get("id") for _, row in all_rows]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ResponseContractError("Control IDs must be nonempty and unique")
    if any(not isinstance(row.get("assertion_id"), str) or not row["assertion_id"] for row in negatives):
        raise ResponseContractError("Each negative control needs an assertion ID")
    targets = {row["assertion_id"] for row in negatives}
    required_targets = expected | ({CONTRACT_ASSERTION} if contract is not None else set())
    if targets != required_targets:
        raise ResponseContractError("Negative controls must cover each expected assertion and the applicable contract")
    results = []
    for polarity, row in all_rows:
        if not isinstance(row.get("response"), str):
            raise ResponseContractError("Control response must be text")
        outcome = validate_response(contract, row["response"])
        target = row.get("assertion_id")
        semantic, semantic_error = None, None
        if not (polarity == "negative" and target == CONTRACT_ASSERTION):
            try:
                semantic = evaluate_semantics(row["response"])
                if (not isinstance(semantic, dict) or set(semantic) != expected or
                        any(not isinstance(k, str) or type(v) is not bool for k, v in semantic.items())):
                    raise ResponseContractError("Semantic results do not match the expected assertion IDs")
            except Exception as exc:
                semantic_error = type(exc).__name__
        if outcome["status"] == "error" or semantic_error:
            status = "error"
        elif polarity == "positive":
            status = "pass" if outcome["status"] in {"pass", "not_applicable"} and all(semantic.values()) else "fail"
        elif target == CONTRACT_ASSERTION:
            status = "pass" if outcome["status"] == "fail" else "fail"
        else:
            status = ("pass" if outcome["status"] in {"pass", "not_applicable"} and not semantic[target]
                      and all(value for key, value in semantic.items() if key != target) else "fail")
        results.append({"control_id": row["id"], "polarity": polarity, "target_assertion_id": target,
                        "contract": outcome, "semantic": semantic, "semantic_error": semantic_error,
                        "status": status, "passed": status == "pass"})
    return {"passed": all(item["passed"] for item in results),
            "status": "error" if any(item["status"] == "error" for item in results) else
                      ("pass" if all(item["passed"] for item in results) else "fail"),
            "controls": results}

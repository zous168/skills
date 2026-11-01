"""Offline strict-output reference controls; no model or network is used."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from refiner.agent import Agent
from refiner.common import RefinerError
from refiner.runtime import codex_output_schema, decode_codex_response
from refiner.schemas import PLAN_V3, PLAN_V4, PLAN_V5, PLAN_V6
from test_plan_contract import _minimal_plan


def wire_value(value, schema):
    """Build test wire data from its advertised shape, without resolving refs."""
    if value is None:
        return None
    if "anyOf" in schema:
        branch = next(item for item in schema["anyOf"] if item.get("type") != "null")
        return wire_value(value, branch)
    if schema.get("type") == "object":
        result = {}
        for name, child in schema["properties"].items():
            if name.endswith("_json") and name[:-5] in value:
                result[name] = json.dumps(value[name[:-5]], ensure_ascii=False)
            else:
                result[name] = wire_value(value[name], child) if name in value else None
        return result
    if schema.get("type") == "array":
        if isinstance(value, dict):
            return [{"key": key, "value": wire_value(item, schema["items"]["properties"]["value"])}
                    for key, item in value.items()]
        return [wire_value(item, schema["items"]) for item in value]
    return value


class CodexLocalReferenceTests(unittest.TestCase):
    def round_trip(self, canonical, schema):
        before = copy.deepcopy(schema)
        jsonschema.Draft202012Validator(schema).validate(canonical)
        wire_schema = codex_output_schema(schema)
        encoded = wire_value(canonical, wire_schema)
        jsonschema.Draft202012Validator(wire_schema).validate(encoded)
        restored = decode_codex_response(encoded, schema)
        jsonschema.Draft202012Validator(schema).validate(restored)
        self.assertEqual(restored, canonical)
        self.assertEqual(schema, before)
        return wire_schema

    def test_actual_plan_v3_to_v6_artifacts_round_trip(self):
        for version, schema in ((3, PLAN_V3), (4, PLAN_V4), (5, PLAN_V5), (6, PLAN_V6)):
            with self.subTest(version=version):
                plan = _minimal_plan()
                plan["schema_version"] = "skill-refiner.plan/v" + str(version)
                if version in (3, 4):
                    plan.pop("execution_plan")
                wire = self.round_trip(plan, schema)
                contracts = wire["properties"]["stage_contracts"]
                if "anyOf" in contracts:
                    contracts = next(item for item in contracts["anyOf"] if item.get("type") == "object")
                stage = contracts["properties"]["stages"]["items"]
                for name in ("inputs", "outputs"):
                    self.assertEqual(stage["properties"][name]["items"]["type"], "object")

    def test_reference_chain_and_escaped_json_pointer(self):
        schema = {"type": "object", "properties": {"count": {"$ref": "#/$defs/alias"}},
                  "required": ["count"], "additionalProperties": False,
                  "$defs": {"alias": {"$ref": "#/$defs/a~1b~0c"}, "a/b~c": {"type": "integer"}}}
        self.round_trip({"count": 7}, schema)

    def test_percent_encoded_fragment_and_array_pointer(self):
        schema = {"type": "object", "properties": {"count": {"$ref": "#/$defs/%E4%B8%AD%E6%96%87/anyOf/0"}},
                  "required": ["count"], "additionalProperties": False,
                  "$defs": {"中文": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}
        self.round_trip({"count": 7}, schema)

    def test_referenced_dynamic_objects_and_json_values_are_decoded_symmetrically(self):
        schema = {"type": "object", "required": ["payload"], "additionalProperties": False,
                  "properties": {"payload": {"$ref": "#/$defs/map"}},
                  "$defs": {"map": {"type": "object", "additionalProperties": {"$ref": "#/$defs/entry"}},
                            "entry": {"type": "object", "required": ["value"], "additionalProperties": False,
                                      "properties": {"value": {}, "optional": {"type": "string"}}}}}
        self.round_trip({"payload": {"a": {"value": {"priority": None, "中文": [True, 7]}}}}, schema)

    def test_reference_siblings_keep_disjoint_constraints_and_reject_conflicts(self):
        schema = {"$ref": "#/$defs/text", "minLength": 2, "$defs": {"text": {"type": "string"}}}
        self.round_trip("中文", schema)
        conflict = {"$ref": "#/$defs/text", "type": "integer", "$defs": {"text": {"type": "string"}}}
        for operation in (lambda: codex_output_schema(conflict), lambda: decode_codex_response(7, conflict)):
            with self.assertRaisesRegex(RefinerError, "Conflicting"):
                operation()

    def test_references_inside_instance_literals_are_not_followed(self):
        schema = {"type": "object", "properties": {"text": {"type": "string",
                  "default": {"$ref": "https://example.invalid/not-a-schema"}}},
                  "required": ["text"], "additionalProperties": False}
        self.round_trip({"text": "literal"}, schema)

    def test_external_anchors_missing_paths_and_invalid_targets_fail_closed(self):
        for reference in ("https://example.invalid/schema.json", "file:///secret.json", "relative.json#/x",
                          "#anchor", "#/$defs/missing", "#/$defs/bad~2escape", "#/$defs/%GG",
                          "#/$defs/%FF", 7, "#/$defs/scalar", "#/$defs/boolean"):
            with self.subTest(reference=reference):
                schema = {"type": "object", "properties": {"x": {"$ref": reference}},
                          "$defs": {"scalar": 7, "boolean": False}}
                with patch("urllib.request.urlopen", side_effect=AssertionError("no network")) as network:
                    for operation in (lambda: codex_output_schema(schema),
                                      lambda: decode_codex_response({"x": "value"}, schema)):
                        with self.assertRaises(RefinerError):
                            operation()
                    network.assert_not_called()

    def test_direct_and_indirect_cycles_are_rejected_by_both_paths(self):
        for schema in ({"$ref": "#"},
                       {"$ref": "#/$defs/a", "$defs": {"a": {"$ref": "#/$defs/b"},
                                                        "b": {"$ref": "#/$defs/a"}}}):
            for operation in (lambda: codex_output_schema(schema), lambda: decode_codex_response({}, schema)):
                with self.assertRaisesRegex(RefinerError, "Cyclic"):
                    operation()

    def test_nested_resource_cannot_change_reference_scope(self):
        schema = {"$defs": {"nested": {"$id": "nested.json", "type": "string"}},
                  "$ref": "#/$defs/nested"}
        with self.assertRaisesRegex(RefinerError, "nested schema resources"):
            codex_output_schema(schema)

    def test_unsupported_reference_keywords_and_hidden_schema_locations_fail_early(self):
        forbidden = "https://example.invalid/external.json"
        schemas = [
            {"$dynamicRef": forbidden, "type": "object", "properties": {"x": {"type": "string"}}},
            {"$recursiveRef": "#", "type": "object"},
            {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
             "dependencies": {"x": {"$ref": forbidden}}},
            {"type": "object", "dependencies": {"x": {"$ref": forbidden}}},
            {"type": "string", "contentSchema": {"$ref": forbidden}},
            {"type": "array", "items": [{"$ref": forbidden}]},
        ]
        for schema in schemas:
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as temporary:
                agent = Agent(temporary, config={"agents": {"default": {"backend": "codex", "model": "offline"}}})
                with patch("urllib.request.urlopen", side_effect=AssertionError("no network")) as network, \
                        patch("refiner.runtime.execute", side_effect=AssertionError("no model")) as backend:
                    for operation in (lambda: codex_output_schema(schema),
                                      lambda: decode_codex_response({}, schema),
                                      lambda: agent.ask("plan", "offline reference control", {}, schema)):
                        with self.assertRaises(RefinerError):
                            operation()
                    network.assert_not_called()
                    backend.assert_not_called()
                self.assertEqual(agent.calls, 0)
                self.assertFalse((Path(temporary) / "agent-calls").exists())

    def test_property_dependencies_are_data_and_cannot_trigger_reference_loading(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"],
                  "additionalProperties": False, "dependencies": {"x": ["$ref", "$dynamicRef"]}}
        self.round_trip({"x": "literal"}, schema)

    def test_expansion_depth_node_and_byte_budgets_are_enforced(self):
        definitions = {"n0": {"type": "string"}}
        for index in range(1, 17):
            reference = {"$ref": "#/$defs/n" + str(index - 1)}
            definitions["n" + str(index)] = {"type": "object", "properties": {"a": reference, "b": reference}}
        expansive = {"$ref": "#/$defs/n16", "$defs": definitions}
        deep = {"type": "string"}
        for _ in range(66):
            deep = {"type": "array", "items": deep}
        large = {"type": "string", "description": "x" * 4_000_001}
        for schema in (expansive, deep, large):
            for operation in (lambda: codex_output_schema(schema), lambda: decode_codex_response({}, schema)):
                with self.assertRaisesRegex(RefinerError, "budget"):
                    operation()

    def test_bad_reference_is_rejected_before_backend_and_call_counter(self):
        schema = {"type": "object", "properties": {"x": {"$ref": "file:///secret.json"}}}
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(temporary, config={"agents": {"default": {"backend": "codex", "model": "offline"}}})
            with patch("refiner.runtime.execute", side_effect=AssertionError("no model")) as backend:
                with self.assertRaises(RefinerError):
                    agent.ask("plan", "offline reference control", {}, schema)
                backend.assert_not_called()
            self.assertEqual(agent.calls, 0)
            self.assertFalse((Path(temporary) / "agent-calls").exists())


if __name__ == "__main__":
    unittest.main()

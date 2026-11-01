"""Regression tests for JSON comparisons used by the frozen-plan oracle."""
import json
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from refiner.checks import check_one, json_values_equal, validate_plan
from test_plan_contract import _minimal_plan


class JsonTypeEqualityTests(unittest.TestCase):
    def test_json_value_comparison_respects_boolean_type_recursively(self):
        pairs = [(True, 1), (False, 0), ({'v': True}, {'v': 1}),
                 ([False], [0]), ({'a': [{'b': True}]}, {'a': [{'b': 1}]}),
                 (True, 'true'), (None, False), ([1, 2], [2, 1])]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertFalse(json_values_equal(left, right))
                self.assertFalse(json_values_equal(right, left))
        for left, right in [(True, True), (False, False), (1, 1.0),
                            ({'a': [1, 2.0, True]}, {'a': [1.0, 2, True]}),
                            (None, None), ('1', '1')]:
            with self.subTest(left=left, right=right):
                self.assertTrue(json_values_equal(left, right))

    def test_response_json_checks_use_type_sensitive_equality(self):
        with tempfile.TemporaryDirectory() as root:
            for kind, expected, pointer, good, bad in [
                ('json_equal', {'v': [True]}, None, '{"v":[true]}', '{"v":[1]}'),
                ('json_path_equal', False, '/v', '{"v":false}', '{"v":0}'),
                ('json_sequence_equal', [{'v': True}, 1], None,
                 '{"v":true} 1.0', '{"v":1} 1.0'),
            ]:
                check = {'type': kind, 'value': expected}
                if pointer is not None:
                    check['pointer'] = pointer
                with self.subTest(kind=kind):
                    self.assertTrue(check_one(check, good, root)[0])
                    self.assertFalse(check_one(check, bad, root)[0])

    def test_file_json_checks_and_cross_file_match(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'data.json').write_text('{"v":[1.0,true]}', encoding='utf-8')
            checks = [
                ({'type': 'file_json_equal', 'path': 'data.json', 'value': {'v': [1, True]}}, True),
                ({'type': 'file_json_equal', 'path': 'data.json', 'value': {'v': [1, 1]}}, False),
                ({'type': 'file_json_path_equal', 'path': 'data.json', 'pointer': '/v/1', 'value': 1}, False),
                ({'type': 'json_path_matches_file', 'path': 'data.json', 'pointer': '/v',
                  'value': {'file_pointer': '/v'}}, False),
            ]
            for check, expected in checks:
                with self.subTest(check=check):
                    self.assertEqual(check_one(check, '{"v":[1,1]}', root)[0], expected)
            check = checks[-1][0]
            self.assertTrue(check_one(check, '{"v":[1,true]}', root)[0])

    def test_tool_output_json_comparison_requires_same_json_types(self):
        trace = [
            {'role': 'tool_call', 'tool_call': {'id': 'c1', 'name': 'exec_command',
                                                 'arguments': {'cmd': 'fixture-command'}}},
            {'role': 'tool_result', 'tool_result': {'call_id': 'c1', 'status': 'success',
                                                     'content': '{"result":[true,1.0]}'}}
        ]
        check = {'type': 'tool_output_matches', 'value': 'fixture-command'}
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(check_one(check, '{"result":[true,1]}', root, trace)[0])
            self.assertFalse(check_one(check, '{"result":[1,1]}', root, trace)[0])

    def test_full_positive_and_negative_control_matrix_detects_numeric_false(self):
        plan = _minimal_plan()
        case = plan['cases'][0]
        case['checks'] = [{'id': 'A-FLAG', 'type': 'json_path_equal',
                           'pointer': '/needs_input', 'value': False}]
        good = '{"needs_input":false,"note":"complete"}'
        alternative = '{"note":"done","needs_input":false}'
        bad = '{"needs_input":0,"note":"complete"}'
        case['good_response'] = good
        case['good_responses'] = [alternative]
        case['bad_response'] = bad
        case['positive_controls'] = [{'id': 'P1', 'response': good},
                                     {'id': 'P2', 'response': alternative}]
        case['negative_controls'] = [{'id': 'N1', 'assertion_id': 'A-FLAG', 'response': bad,
                                      'rationale': 'A number is not a JSON boolean.'}]
        with tempfile.TemporaryDirectory() as root:
            result = validate_plan(plan, root)
        controls = result['case_control_results'][0]['controls']
        self.assertEqual(len(controls), 3)
        for control in controls:
            self.assertEqual(control['assertions'][0]['passed'], control['polarity'] == 'positive')


if __name__ == '__main__':
    unittest.main()

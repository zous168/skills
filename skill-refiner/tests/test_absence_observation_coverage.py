"""Tool absence requires real observation; plan controls are explicit simulations."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_plan_contract import _minimal_plan
from refiner.checks import check_one, grade, validate_plan


def observation(**updates):
    value = {'status': 'complete', 'unclassified_tools': [],
             'absence_of_external_calls_proven': True, 'absence_of_side_effects_proven': True}
    value.update(updates)
    return {'role': 'system', 'content': json.dumps({'skill_refiner_native_observation': value})}


class AbsenceCoverageTests(unittest.TestCase):
    def test_real_absence_requires_complete_observation(self):
        for kind in ('tool_call_absent', 'tool_command_absent'):
            check = {'type': kind, 'value': 'forbidden'}
            for trace in ([], {}, [{'role': 'assistant', 'content': 'no calls'}],
                          [observation(status='incomplete')],
                          [observation(absence_of_external_calls_proven=False)],
                          [observation(absence_of_side_effects_proven=False)],
                          [observation(unclassified_tools=['unknown'])],
                          [{'role': 'system', 'content': '{"skill_refiner_native_observation":'}]):
                with self.subTest(kind=kind, trace=trace):
                    passed, diagnostic = check_one(check, '', '.', trace)
                    self.assertFalse(passed)
                    self.assertIn('coverage gap', diagnostic)
            self.assertTrue(check_one(check, '', '.', [observation()])[0])

    def test_observed_forbidden_event_fails_and_synthetic_mode_is_explicit(self):
        for kind, call in (
            ('tool_call_absent', {'id': '1', 'name': 'forbidden', 'arguments': {}}),
            ('tool_command_absent', {'id': '1', 'name': 'shell', 'arguments': {'command': 'forbidden'}}),
        ):
            check = {'type': kind, 'value': 'forbidden'}
            trace = [{'role': 'tool_call', 'tool_call': call}, observation()]
            self.assertFalse(check_one(check, '', '.', trace)[0])
            self.assertFalse(check_one(check, '', '.', [])[0])
            self.assertTrue(check_one(check, '', '.', [], synthetic_control=True)[0])
            self.assertFalse(check_one(check, '', '.', trace, synthetic_control=True)[0])
            self.assertFalse(check_one(check, '', '.', [observation(status='incomplete')],
                                       synthetic_control=True)[0])

    def test_actual_grader_reads_untrusted_empty_trace_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'transcript.json'
            path.write_text('[]', encoding='utf-8')
            case = {'id': 'c', 'checks': [{'id': 'a', 'type': 'tool_call_absent', 'value': 'forbidden'}]}
            with patch.dict(os.environ, {'EVAL_TRANSCRIPT_PATH': str(path)}):
                self.assertFalse(grade(case, '', tmp)['passed'])

    def test_plan_self_check_runs_synthetic_positive_and_negative_controls(self):
        for check in (
            {'id': 'no-command', 'type': 'tool_command_absent', 'value': 'forbidden',
             'control_command': 'forbidden'},
            {'id': 'no-call', 'type': 'tool_call_absent', 'value': 'forbidden',
             'control_tool_name': 'forbidden'},
        ):
            with self.subTest(kind=check['type']), tempfile.TemporaryDirectory() as tmp:
                plan = copy.deepcopy(_minimal_plan())
                plan['cases'][0]['checks'].append(check)
                result = validate_plan(plan, tmp)
                rows = result['case_control_results'][0]['auxiliary_assertions']
                controls = {r['control_id']: r['assertions'][0]['passed'] for r in rows}
                self.assertEqual(sorted(controls.values()), [False, True])


if __name__ == '__main__':
    unittest.main()

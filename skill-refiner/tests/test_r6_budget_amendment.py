"""r6 capacity amendment against captured evidence, without models or source writes."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_refine_plan_recovery_chain import HOME, ART, ANCESTOR
from refiner.agent import Agent
from refiner.common import RefinerError, digest, read_json
from refiner.refine_plan_recovery import (_budget_amendment, _chain_tree, _explicit_zero_evaluation_lineage, _forward_unevaluated_migration,
                                         _io_path, migrate, verify_recovery)

SOURCE = HOME / 'refine-continuation-20261031-v7-r6'
ARCHIVE = ART / 'controller-before-r6-capacity/skill-refiner'
SNAPSHOT = ART / 'refine-v7-r6-observed-hashes.json'


@unittest.skipUnless(all(p.exists() for p in (SOURCE, ARCHIVE, SNAPSHOT, ANCESTOR)), 'terminal r6 unavailable')
class CapacityAmendmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _chain_tree(SOURCE)
        cls.tmp = Path(tempfile.mkdtemp(prefix='r6-budget-test-'))
        cls.target = cls.tmp / 'next'
        history = HOME / 'refine-continuation-20261031-v7-r5'
        plan = read_json(history / 'agent-calls/019-plan/response.json')
        control = history / 'plan-controls/attempts/development-04.json'
        cls.assessment = {'schema_version': 'skill-refiner.refine-capacity-assessment/v1',
                          'decision_by': 'main_agent', 'verdict': 'coverage_capacity_unproven',
                          'source_plan_sha256': digest(plan),
                          'source_control_sha256': hashlib.sha256(control.read_bytes()).hexdigest(),
                          'gap_index': len(plan['coverage_gaps']) - 1, 'gap_quote': plan['coverage_gaps'][-1],
                          'rationale': '目前无法证明剩余案例容量足以覆盖全部必要风险，先扩容供规划。',
                          'authorized_limits': {'max_cases': 24, 'acceptance_cases': 12, 'max_evaluations': 120}}
        cls.assessment_path = cls.tmp / 'assessment.json'
        cls.assessment_path.write_text(json.dumps(cls.assessment, ensure_ascii=False), encoding='utf8')
        cls.result = migrate(SOURCE, ARCHIVE, ANCESTOR, cls.target, source_snapshot=SNAPSHOT,
                             amend_max_cases=24, amend_max_evaluations=120,
                             capacity_assessment=cls.assessment_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_io_path(cls.tmp, force=True))

    def verified(self):
        return verify_recovery(self.target, read_json(self.target / 'resume-contract.json'))

    def test_capacity_contract_preserves_history_and_usage(self):
        recovered = self.verified()
        old = read_json(SOURCE / 'resume-contract.json')
        contract = read_json(self.target / 'resume-contract.json')
        state = read_json(self.target / 'state.json')
        amendment = read_json(self.target / 'refine-budget-amendment.json')
        self.assertEqual(len(self.before), 1543)
        self.assertEqual(_chain_tree(SOURCE), self.before)
        self.assertEqual(recovered['usage'], {'agent_calls': 21, 'elapsed_seconds': 1799.02, 'target_evaluations': 0})
        self.assertEqual(len(recovered['subsequent_controls']), 4)
        self.assertEqual(recovered['latest_control_plan']['cases'][-1]['id'], 'c9')
        self.assertEqual((amendment['old_max_cases'], amendment['new_max_cases']), (12, 24))
        self.assertEqual((amendment['old_max_evaluations'], amendment['new_max_evaluations']), (80, 120))
        self.assertEqual(amendment['known_worst_case_evaluations'], 108)
        self.assertIn('预算', amendment['capacity_gap'])
        self.assertEqual(amendment['capacity_assessment'], self.assessment)
        self.assertEqual(len(amendment['zero_evaluation_lineage']), 8)
        self.assertEqual(contract['options'], {**old['options'], 'max_cases': 24})
        self.assertEqual(contract['limits'], {**old['limits'], 'max_cases': 24, 'max_evaluations': 120})
        self.assertEqual(state['limits'], contract['limits'])
        self.assertEqual(self.result['plan_status'], 'not_passed')
        self.assertEqual(_forward_unevaluated_migration(self.target, state,
                         read_json(self.target / 'stages.json'), recovered,
                         read_json(self.target / 'refine-plan-recovery.json')), '019-plan')

    def test_amendment_is_bound_and_fails_on_changed_evidence(self):
        record = self.target / 'refine-budget-amendment.json'
        prior = record.read_bytes()
        try:
            record.write_bytes(prior + b' ')
            with self.assertRaises(RefinerError):
                self.verified()
        finally:
            record.write_bytes(prior)
        contract_path = self.target / 'resume-contract.json'
        prior = contract_path.read_bytes()
        try:
            altered = read_json(contract_path)
            altered['limits']['max_seconds'] += 1
            contract_path.write_text(__import__('json').dumps(altered), encoding='utf8')
            with self.assertRaises(RefinerError):
                self.verified()
        finally:
            contract_path.write_bytes(prior)

    def test_authorized_limits_do_not_permit_insufficient_or_unknown_cost(self):
        recovered = self.verified()
        old = read_json(SOURCE / 'resume-contract.json')
        state = read_json(SOURCE / 'state.json')
        with self.assertRaisesRegex(RefinerError, 'explicit structured assessment'):
            _budget_amendment(SOURCE, str(SOURCE), old, state, recovered, 24, 120)
        for cases, evaluations in ((24, 107), (25, 120), (24, 201)):
            with self.subTest(cases=cases, evaluations=evaluations), self.assertRaises(RefinerError):
                _budget_amendment(SOURCE, str(SOURCE), old, state, recovered, cases, evaluations, self.assessment)
        altered = copy.deepcopy(recovered)
        altered['latest_control_plan']['coverage_gaps'] = ['No capacity gap is recorded.']
        with self.assertRaises(RefinerError):
            _budget_amendment(SOURCE, str(SOURCE), old, state, altered, 24, 120, self.assessment)
        with patch('refiner.refine_plan_recovery.os.walk', return_value=[('C:/fixture/evals', [], ['eval.yaml'])]):
            with self.assertRaisesRegex(RefinerError, 'native evaluations'):
                _budget_amendment(SOURCE, str(SOURCE), old, state, recovered, 24, 120, self.assessment)

    def test_agent_reaches_next_call_without_resetting_budget(self):
        agent = Agent(self.target, command='offline-agent', inherited_calls=21)
        with patch.object(agent, '_call_timeout', side_effect=RuntimeError('offline boundary')):
            with self.assertRaisesRegex(RuntimeError, 'offline boundary'):
                agent.ask('plan', 'offline only', {'case': 'test'}, {'type': 'object', 'properties': {}})
        self.assertEqual(agent.calls, 22)

    def test_missing_boolean_and_float_ancestor_counts_are_not_zero_evidence(self):
        import refiner.refine_plan_recovery as recovery
        real_read = recovery.read_json
        for invalid in ('missing', False, 0.0):
            def fake_read(path):
                value = real_read(path)
                if Path(path).name == 'recovery-ancestor-state.json':
                    value = copy.deepcopy(value)
                    if invalid == 'missing':
                        value.pop('target_evaluations', None)
                    else:
                        value['target_evaluations'] = invalid
                return value
            with self.subTest(invalid=invalid), patch.object(recovery, 'read_json', side_effect=fake_read):
                with self.assertRaisesRegex(RefinerError, 'explicit integer-zero'):
                    _explicit_zero_evaluation_lineage(SOURCE)


if __name__ == '__main__':
    unittest.main()

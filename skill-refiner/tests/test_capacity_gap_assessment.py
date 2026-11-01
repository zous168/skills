"""Explicit, source-bound decisions replace automatic prose classification."""
import copy
import tempfile
import unittest
from pathlib import Path

from test_refine_plan_recovery_chain import HOME
from refiner.common import RefinerError, digest
from refiner.refine_plan_recovery import _validate_capacity_assessment, _reject_evaluation_evidence


class CapacityGapAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'coverage_gaps': ['需要进一步评估案例容量是否能覆盖所有风险。']}
        self.control = 'a' * 64
        self.assessment = {
            'schema_version': 'skill-refiner.refine-capacity-assessment/v1',
            'decision_by': 'main_agent', 'verdict': 'coverage_capacity_unproven',
            'source_plan_sha256': digest(self.plan), 'source_control_sha256': self.control,
            'gap_index': 0, 'gap_quote': self.plan['coverage_gaps'][0],
            'rationale': '现有案例容量能否覆盖全部风险尚未得到充分证明。',
            'authorized_limits': {'max_cases': 24, 'acceptance_cases': 12, 'max_evaluations': 120},
        }

    def test_no_automatic_authorization_from_sufficient_or_conflicting_prose(self):
        for gap in ('案例预算不足的问题已经解决，当前可以覆盖所有场景。',
                    '案例预算不足，但剩余名额完全满足要求。',
                    'case budget was insufficient but has since been resolved for all cases.',
                    '案例预算不足。无需扩容。',
                    'The case budget is insufficient. No need to expand.'):
            with self.subTest(gap=gap), self.assertRaises(RefinerError):
                _validate_capacity_assessment(None, {'coverage_gaps': [gap]}, self.control, 24, 12, 120)

    def test_assessment_requires_exact_plan_control_quote_and_limits(self):
        self.assertEqual(_validate_capacity_assessment(self.assessment, self.plan, self.control, 24, 12, 120),
                         self.plan['coverage_gaps'][0])
        variants = []
        for key, value in (('source_plan_sha256', 'b' * 64), ('source_control_sha256', 'c' * 64),
                           ('gap_index', 1), ('gap_quote', 'different'), ('verdict', 'insufficient'),
                           ('decision_by', 'independent_reviewer')):
            changed = copy.deepcopy(self.assessment); changed[key] = value; variants.append(changed)
        changed = copy.deepcopy(self.assessment); changed['authorized_limits']['max_evaluations'] = 121; variants.append(changed)
        for altered in variants:
            with self.subTest(altered=altered), self.assertRaises(RefinerError):
                _validate_capacity_assessment(altered, self.plan, self.control, 24, 12, 120)
        old_plan = {'coverage_gaps': ['旧计划另一条缺口']}
        with self.assertRaises(RefinerError):
            _validate_capacity_assessment(self.assessment, old_plan, self.control, 24, 12, 120)

    def test_candidates_directory_conflicts_with_unstarted_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'candidates/1/demo').mkdir(parents=True)
            with self.assertRaisesRegex(RefinerError, 'candidates'):
                _reject_evaluation_evidence(root, {'target_evaluations': 0}, {'stages': []})


if __name__ == '__main__':
    unittest.main()

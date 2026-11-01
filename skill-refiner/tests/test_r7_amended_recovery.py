"""Offline recovery of the real amended r7 run, with no model calls."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_refine_plan_recovery_chain import HOME, ART, ANCESTOR
from refiner.common import RefinerError, digest, read_json
from refiner.refine_plan_recovery import (
    _chain_tree, _io_path, _merge_replayed_controls, migrate, verify_recovery,
)


class ReplayMergeTests(unittest.TestCase):
    def test_prefix_is_kept_once_and_changed_missing_or_reordered_plans_fail(self):
        def entry(n):
            return {"record": {"plan_sha256": str(n), "plan_snapshot": {"n": n}}, "sha256": str(n)}
        a, b, c = (entry(n) for n in range(3))
        self.assertEqual(_merge_replayed_controls([a, b], [a, b, c]), [a, b, c])
        self.assertEqual(_merge_replayed_controls([], [a]), [a])
        for later in ([a], [b, a, c], [a, {"record": {"plan_sha256": "1", "plan_snapshot": {"n": 9}}}, c]):
            with self.subTest(later=later), self.assertRaises(RefinerError):
                _merge_replayed_controls([a, b], later)


SOURCE = HOME / 'refine-continuation-20261031-v7-r7'
ARCHIVE = ART / 'controller-before-r7-handoff/skill-refiner'
SNAPSHOT = ART / 'refine-v7-r7-observed-hashes.json'


@unittest.skipUnless(all(p.exists() for p in (SOURCE, ARCHIVE, SNAPSHOT, ANCESTOR)), 'terminal r7 unavailable')
class AmendedRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _chain_tree(SOURCE)
        cls.tmp = Path(tempfile.mkdtemp(prefix='r7-recovery-test-'))
        cls.target = cls.tmp / 'next'
        try:
            cls.result = migrate(SOURCE, ARCHIVE, ANCESTOR, cls.target, source_snapshot=SNAPSHOT,
                                 planned_dependency_migration=True)
        except BaseException:
            shutil.rmtree(_io_path(cls.tmp, force=True))
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_io_path(cls.tmp, force=True))

    def test_amendment_history_prelaunch_and_usage_are_inherited(self):
        recovered = verify_recovery(self.target, read_json(self.target / 'resume-contract.json'))
        self.assertEqual(recovered['usage'], {'agent_calls': 27, 'elapsed_seconds': 2453.82, 'target_evaluations': 0})
        self.assertEqual(len(self.before), 1977)
        self.assertEqual(_chain_tree(SOURCE), self.before)
        self.assertEqual(recovered['pending_prelaunch_call'], '013-analysis-review')
        self.assertNotIn('pending_plan', recovered)
        self.assertEqual(len(recovered['control_records']), 2)
        controls = recovered['subsequent_controls']
        self.assertEqual(len(controls), 6)
        for entry, call in zip(controls, ('009', '012', '016', '019', '022', '025')):
            self.assertEqual(entry['record']['plan_snapshot'], read_json(SOURCE / f'agent-calls/{call}-plan/response.json'))
        self.assertEqual(recovered['latest_control_plan'], read_json(SOURCE / 'agent-calls/025-plan/response.json'))
        original_contract = read_json(SOURCE / 'resume-contract.json')
        contract = read_json(self.target / 'resume-contract.json')
        self.assertEqual(contract['options']['max_cases'], 24)
        self.assertEqual(contract['options']['acceptance_cases'], 12)
        self.assertEqual(contract['limits']['max_evaluations'], 120)
        for key in ('refine_budget_amendment_sha256', 'refine_budget_amendment_schema_sha256',
                    'refine_capacity_assessment_schema_sha256'):
            self.assertEqual(contract[key], original_contract[key])
        amendment = (self.target / 'refine-budget-amendment.json').read_bytes()
        self.assertEqual(hashlib.sha256(amendment).hexdigest(), contract['refine_budget_amendment_sha256'])
        self.assertEqual(amendment, (SOURCE / 'refine-budget-amendment.json').read_bytes())
        marker = recovered['planned_dependency_migration']
        self.assertEqual(marker['source_recovery_sha256'], original_contract['refine_plan_recovery_sha256'])
        self.assertEqual(marker['source_plan_schema_version'], 'skill-refiner.plan/v7')
        self.assertEqual(marker['selected_plan_schema_version'], 'skill-refiner.plan/v8')
        self.assertEqual(marker['source_plan_sha256'], digest(read_json(SOURCE / 'agent-calls/025-plan/response.json')))
        self.assertEqual(marker['source_control_sha256'],
                         hashlib.sha256((SOURCE / 'plan-controls/attempts/development-06.json').read_bytes()).hexdigest())
        self.assertEqual(marker['inputs_sha256'], original_contract['inputs_sha256'])
        self.assertEqual(marker['usage'], recovered['usage'])
        self.assertEqual(marker['options'], original_contract['options'])
        self.assertEqual(marker['limits'], original_contract['limits'])
        self.assertEqual(marker['selected_plan_schema_sha256'], hashlib.sha256(
            (Path(__file__).resolve().parents[1] / 'schemas/v8/plan.schema.json').read_bytes()).hexdigest())
        self.assertEqual(hashlib.sha256((self.target / 'planned-dependency-migration.json').read_bytes()).hexdigest(),
                         contract['planned_dependency_migration_sha256'])

    def test_tampered_marker_even_with_updated_contract_hash_is_rejected(self):
        marker_path = self.target / 'planned-dependency-migration.json'
        contract_path = self.target / 'resume-contract.json'
        old_marker, old_contract = marker_path.read_bytes(), contract_path.read_bytes()
        try:
            marker = json.loads(old_marker)
            marker['source_plan_sha256'] = '0' * 64
            marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding='utf-8')
            contract = json.loads(old_contract)
            contract['planned_dependency_migration_sha256'] = hashlib.sha256(marker_path.read_bytes()).hexdigest()
            contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'differs from authenticated evidence'):
                verify_recovery(self.target, contract)
        finally:
            marker_path.write_bytes(old_marker)
            contract_path.write_bytes(old_contract)


if __name__ == '__main__':
    unittest.main()

"""Real captured r3 recovery in a private temporary workspace, without model calls."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from test_refine_plan_recovery_chain import HOME, ART, ANCESTOR
from refiner.common import RefinerError, read_json
from refiner.refine_plan_recovery import migrate, verify_recovery, _chain_tree, _io_path

SOURCE = HOME / 'refine-continuation-20261031-v7-r3'
ARCHIVE = ART / 'controller-before-r3-fix/skill-refiner'
SNAPSHOT = ART / 'refine-v7-r3-observed-hashes.json'


@unittest.skipUnless(all(p.exists() for p in (SOURCE, ARCHIVE, ANCESTOR, SNAPSHOT)), 'captured r3 unavailable')
class InterruptedDraftRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _chain_tree(SOURCE)
        cls.tmp = tempfile.TemporaryDirectory(prefix='refiner-r3-draft-')
        cls.target = Path(cls.tmp.name) / 'next-run'
        cls.result = migrate(SOURCE, ARCHIVE, ANCESTOR, cls.target, source_snapshot=SNAPSHOT)

    @classmethod
    def tearDownClass(cls):
        if cls.target.exists():
            shutil.rmtree(_io_path(cls.target, force=True))
        cls.tmp.cleanup()

    def verified(self):
        return verify_recovery(self.target, read_json(self.target / 'resume-contract.json'))

    def test_preserves_unvalidated_draft_history_and_budget(self):
        r = self.verified()
        self.assertEqual(r['usage'], {'agent_calls': 13, 'elapsed_seconds': 1136.81, 'target_evaluations': 0})
        self.assertEqual(len(self.before), 594)
        self.assertEqual(self.before, _chain_tree(SOURCE))
        self.assertEqual(r['pending_plan_call'], '012-plan')
        self.assertEqual(r['pending_plan'], read_json(SOURCE / 'agent-calls/012-plan/response.json'))
        self.assertEqual(r['pending_plan_request'], read_json(SOURCE / 'agent-calls/012-plan/request.json'))
        self.assertEqual(r['interrupted_control_record'], read_json(SOURCE / 'plan-controls/attempts/development-01.json'))
        self.assertEqual(r['interrupted_control_record']['status'], 'invalid_coverage')
        self.assertEqual(len(r['control_records']), 2)
        self.assertEqual(len(r['previous_plan']['cases']), 6)
        self.assertEqual(len(r['pending_plan']['cases']), 9)
        self.assertEqual(len(r['pending_plan']['coverage_gaps']), 7)
        self.assertEqual(self.result['plan_status'], 'not_passed')
        self.assertEqual(self.result['reused_stages'], ['inputs'])
        pending_root = Path(r['pending_source_evidence_root'])
        self.assertTrue((pending_root / 'plan-controls/response-applicability').is_dir())
        self.assertEqual(len(r['pending_source_files']), len(self.before))

    def test_preserved_interrupted_manifest_change_is_rejected(self):
        r = self.verified()
        manifest = Path(r['pending_source_evidence_root']) / 'agent-calls/013-analysis-review/request-manifest.json.tmp'
        previous = manifest.read_bytes()
        try:
            manifest.write_bytes(previous + b' ')
            with self.assertRaises(RefinerError):
                self.verified()
        finally:
            manifest.write_bytes(previous)

    @unittest.skipUnless(os.name == 'nt', 'Windows CreateProcess cwd path limit')
    def test_nested_archived_controllers_verify_at_deeper_than_real_r4_path(self):
        base = Path(tempfile.mkdtemp(prefix='r3-deep-regression-'))
        target = base / ('x' * 80) / 'refine-continuation-20261031-v7-r4'
        target.parent.mkdir()
        try:
            self.assertGreater(len(str(target)), len(str(HOME / 'refine-continuation-20261031-v7-r4')))
            result = migrate(SOURCE, ARCHIVE, ANCESTOR, target, source_snapshot=SNAPSHOT)
            recovery = verify_recovery(target, read_json(target / 'resume-contract.json'))
            self.assertEqual(result['plan_status'], 'not_passed')
            self.assertEqual(recovery['usage'], {'agent_calls': 13, 'elapsed_seconds': 1136.81, 'target_evaluations': 0})
            self.assertEqual(recovery['pending_plan_call'], '012-plan')
            self.assertEqual(recovery['pending_plan'], read_json(SOURCE / 'agent-calls/012-plan/response.json'))
            self.assertTrue(Path(recovery['source_evidence_root']).is_relative_to(_io_path(target, force=True)))
            self.assertTrue(Path(recovery['pending_source_evidence_root']).is_relative_to(_io_path(target, force=True)))
            self.assertEqual(self.before, _chain_tree(SOURCE))
        finally:
            shutil.rmtree(_io_path(base, force=True))


if __name__ == '__main__':
    unittest.main()

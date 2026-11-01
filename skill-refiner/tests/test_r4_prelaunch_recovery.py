"""Recover real r4 into a temporary workspace, with no backend execution."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_refine_plan_recovery_chain import HOME, ANCESTOR
from refiner.agent import Agent
from refiner.common import RefinerError, read_json
from refiner.refine_plan_recovery import _chain_tree, _io_path, migrate, verify_recovery

SOURCE = HOME / 'refine-continuation-20261031-v7-r4'
ARCHIVE = Path('D:/work/skills/.tasks/artifacts/GE-yzhe-20261030-184954-fdd3/controller-before-r4-recovery/skill-refiner')


@unittest.skipUnless(SOURCE.exists() and ARCHIVE.exists() and ANCESTOR.exists(), 'captured r4 unavailable')
class PrelaunchRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _chain_tree(SOURCE)
        cls.tmp = Path(tempfile.mkdtemp(prefix='r4-prelaunch-test-'))
        snapshot = cls.tmp / 'source-snapshot.json'
        snapshot.write_text(json.dumps({'source': str(SOURCE), 'files': [
            {'path': k, 'sha256': sha, 'bytes': _io_path(SOURCE / k, force=True).stat().st_size}
            for k, sha in cls.before.items()]}, ensure_ascii=False), encoding='utf-8')
        cls.target = cls.tmp / 'next-run'
        cls.result = migrate(SOURCE, ARCHIVE, ANCESTOR, cls.target, source_snapshot=snapshot)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_io_path(cls.tmp, force=True))

    def test_inherited_elapsed_and_unstarted_call_remain_auditable(self):
        recovery = verify_recovery(self.target, read_json(self.target / 'resume-contract.json'))
        self.assertEqual(recovery['usage'], {'agent_calls': 13, 'elapsed_seconds': 1144.47, 'target_evaluations': 0})
        self.assertEqual(recovery['pending_prelaunch_call'], '013-analysis-review')
        self.assertEqual(recovery['pending_plan_call'], '012-plan')
        self.assertEqual(self.result['plan_status'], 'not_passed')
        self.assertEqual(self.before, _chain_tree(SOURCE))
        self.assertEqual(read_json(self.target / 'state.json')['elapsed_seconds'], 1144.47)

    def test_no_tampering_or_started_process_can_bypass_ledger(self):
        directory = self.target / 'agent-calls/013-analysis-review'
        manifest = directory / 'request-manifest.json.tmp'
        old = manifest.read_bytes()
        try:
            manifest.write_bytes(old + b' ')
            with self.assertRaises(RefinerError):
                Agent(self.target, command='offline-agent')._assert_no_unsettled_calls()
        finally:
            manifest.write_bytes(old)
        process = directory / 'process.execution.json'
        try:
            process.write_text('{"status":"running"}', encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'no reconciled terminal evidence'):
                Agent(self.target, command='offline-agent')._assert_no_unsettled_calls()
        finally:
            process.unlink()
        execution = directory / 'execution.json'
        try:
            execution.write_text('{"status":"completed"}', encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'no reconciled terminal evidence'):
                Agent(self.target, command='offline-agent')._assert_no_unsettled_calls()
        finally:
            execution.unlink()
        prepared = directory / 'request-manifest.json'
        original = prepared.read_bytes()
        try:
            submitted = json.loads(original)
            submitted['status'] = 'submitted'
            prepared.write_text(json.dumps(submitted), encoding='utf-8')
            execution.write_text('{"status":"completed"}', encoding='utf-8')
            with self.assertRaises(RefinerError):
                Agent(self.target, command='offline-agent')._assert_no_unsettled_calls()
        finally:
            prepared.write_bytes(original)
            execution.unlink()
        Agent(self.target, command='offline-agent')._assert_no_unsettled_calls()

    def test_real_ask_reaches_backend_boundary_and_repeated_restart_is_safe(self):
        call_dir = self.target / 'agent-calls/013-analysis-review'
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in call_dir.iterdir()}
        for expected in (14, 15):
            agent = Agent(self.target, command='offline-agent', inherited_calls=13)
            with patch.object(agent, '_call_timeout', side_effect=RuntimeError('offline backend boundary')):
                with self.assertRaisesRegex(RuntimeError, 'offline backend boundary'):
                    agent.ask('analysis-review', 'offline only', {'round': expected},
                              {'type': 'object', 'properties': {}})
            self.assertEqual(agent.calls, expected)
            self.assertEqual(read_json(self.target / f'agent-calls/{expected:03}-analysis-review/execution.json')['status'],
                             'execution_error')
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in call_dir.iterdir()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()

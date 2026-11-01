"""Verify real terminal r5 later-plan recovery without running a model."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_refine_plan_recovery_chain import HOME, ART, ANCESTOR
from refiner.agent import Agent
from refiner.common import RefinerError, digest, read_json, write_json
from refiner.refine_plan_recovery import _chain_control_records, _chain_tree, _io_path, migrate, verify_recovery

SOURCE = HOME / 'refine-continuation-20261031-v7-r5'
ARCHIVE = ART / 'controller-before-r5-recovery/skill-refiner'
SNAPSHOT = ART / 'refine-v7-r5-observed-hashes.json'


@unittest.skipUnless(all(p.exists() for p in (SOURCE, ARCHIVE, SNAPSHOT, ANCESTOR)), 'terminal r5 unavailable')
class LaterPlanRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _chain_tree(SOURCE)
        cls.tmp = Path(tempfile.mkdtemp(prefix='r5-recovery-test-'))
        cls.target = cls.tmp / 'next'
        cls.result = migrate(SOURCE, ARCHIVE, ANCESTOR, cls.target, source_snapshot=SNAPSHOT)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_io_path(cls.tmp, force=True))

    def test_all_recorded_plans_and_decisions_remain_bound(self):
        recovered = verify_recovery(self.target, read_json(self.target / 'resume-contract.json'))
        self.assertEqual(recovered['usage'], {'agent_calls': 21, 'elapsed_seconds': 1799.02, 'target_evaluations': 0})
        self.assertEqual(len(self.before), 1201)
        self.assertEqual(_chain_tree(SOURCE), self.before)
        self.assertEqual(self.result['plan_status'], 'not_passed')
        self.assertEqual(recovered['pending_prelaunch_call'], '013-analysis-review')
        self.assertNotIn('pending_plan', recovered)
        self.assertEqual(len(recovered['control_records']), 2)
        self.assertEqual(len(recovered['subsequent_controls']), 4)
        for i, (control, call) in enumerate(zip(recovered['subsequent_controls'], ('009', '012', '016', '019')), 1):
            with self.subTest(call=call):
                record = control['record']
                root = Path(control['source_evidence_root'])
                path = root / f'plan-controls/attempts/development-{i:02}.json'
                self.assertEqual(control['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(record, read_json(path))
                self.assertEqual(record['plan_snapshot'], read_json(SOURCE / f'agent-calls/{call}-plan/response.json'))
                self.assertTrue((root / 'plan-controls/response-applicability' /
                                 (digest(record['plan_snapshot']) + '.json')).is_file())
        self.assertEqual(recovered['latest_control_plan'],
                         read_json(SOURCE / 'agent-calls/019-plan/response.json'))
        self.assertEqual(len(recovered['latest_control_plan']['coverage_gaps']), 9)

    def test_real_agent_reaches_next_backend_without_repeating_old_call(self):
        agent = Agent(self.target, command='offline-agent', inherited_calls=21)
        with patch.object(agent, '_call_timeout', side_effect=RuntimeError('offline boundary')):
            with self.assertRaisesRegex(RuntimeError, 'offline boundary'):
                agent.ask('plan', 'offline only', {'case': 'test'}, {'type': 'object', 'properties': {}})
        self.assertEqual(agent.calls, 22)
        self.assertEqual(read_json(self.target / 'agent-calls/022-plan/execution.json')['status'], 'execution_error')
        self.assertEqual(_chain_tree(SOURCE), self.before)


class LaterTerminalCallTests(unittest.TestCase):
    def test_charged_failed_calls_require_confirmed_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = {'cases': [], 'requirements': []}
            def call(number, role, status, process):
                directory = root / 'agent-calls' / f'{number:03}-{role}'
                directory.mkdir(parents=True)
                request = {'role': role, 'input': {}}
                encoded = json.dumps(request, ensure_ascii=False).encode('utf-8')
                write_json(directory / 'request.json', request)
                (directory / 'transmission.txt').write_bytes(encoded)
                meta = {'role': role, 'status': status, 'request_sha256': hashlib.sha256(encoded).hexdigest(),
                        'request_bytes': len(encoded), 'transmitted_sha256': hashlib.sha256(encoded).hexdigest(),
                        'transmitted_bytes': len(encoded)}
                if role == 'plan':
                    write_json(directory / 'response.json', plan)
                    meta['response_sha256'] = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
                write_json(directory / 'request-manifest.json', meta)
                write_json(directory / 'execution.json', {'role': role, 'status': status, 'finished_at': 'fixture'})
                write_json(directory / 'process.execution.json', process)
                return directory
            call(1, 'plan', 'completed', {'status': 'completed', 'exit_code': 0, 'finished_at': 'fixture'})
            failed = call(2, 'analysis-review', 'execution_error',
                          {'status': 'timed_out', 'exit_code': 1, 'finished_at': 'fixture'})
            path = 'plan-controls/attempts/development-01.json'
            write_json(root / path, {'schema_version': 'skill-refiner.plan-controls/v1', 'suite': 'development',
                                     'attempt': 1, 'status': 'validation_error',
                                     'plan_snapshot': plan, 'plan_sha256': digest(plan)})
            (root / 'plan-controls/development.json').write_bytes((root / path).read_bytes())
            self.assertEqual(_chain_control_records(root, [path], 2)[1], '001-plan')
            for process in ({'status': 'termination_unconfirmed', 'exit_code': None, 'finished_at': None},
                            {'status': 'timed_out', 'exit_code': None, 'finished_at': 'fixture'}):
                write_json(failed / 'process.execution.json', process)
                with self.assertRaises(RefinerError):
                    _chain_control_records(root, [path], 2)
            write_json(failed / 'process.execution.json',
                       {'status': 'timed_out', 'exit_code': 1, 'finished_at': 'fixture'})
            plan = {'cases': [{'id': 'new'}], 'requirements': []}
            call(3, 'plan', 'completed', {'status': 'completed', 'exit_code': 0, 'finished_at': 'fixture'})
            records, last_checked, pending = _chain_control_records(root, [path], 3, allow_pending=True)
            self.assertEqual(len(records), 1)
            self.assertEqual(last_checked, '001-plan')
            self.assertEqual(pending, ('003-plan', plan))


if __name__ == '__main__':
    unittest.main()

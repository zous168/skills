import json
import io
import os
import pathlib
import tempfile
import time
from unittest.mock import patch
import unittest

from refiner.common import RefinerError
from refiner.docker_target_probe import execute_python_probe, validate_case_dependencies
from refiner.probe_budget import ProbeBudget
import refiner.docker_target_probe as probe
from refiner.skillup import _verify_generated_judge

class DockerProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        self.script = self.root / 'scripts' / 'check.py'
        self.dep = {'id':'dep', 'asset_kind':'script', 'target_path':'scripts/check.py'}
        self.contract = {'argv':[], 'stdin':'', 'expected_exit_code':0, 'expected_stdout':'OK\n'}
    def tearDown(self): self.tmp.cleanup()
    def run_probe(self): return execute_python_probe(self.root, 'case-1', self.dep, self.contract, timeout=20)
    def test_real_script_response_and_wrong_response(self):
        self.script.write_text("import sys; print('OK' if sys.stdin.read() == 'good' else 'BAD')\n", encoding='utf8')
        self.contract['stdin'] = 'good'
        result = self.run_probe()
        self.assertTrue(result['passed'])
        self.assertEqual(result['target_path'], 'scripts/check.py')
        self.contract['stdin'] = 'FAKE'
        self.assertFalse(self.run_probe()['passed'])
    def test_deadline_expires_during_asset_read_before_docker_launch(self):
        self.script.write_text('pass',encoding='utf8')
        case={'id':'case','planned_target_dependencies':[dict(self.dep, execution_contract={
            'runner':'docker-python-response','argv':[], 'expected_exit_code':0,'expected_stdout':'OK',
            'positive_control':{'input':'a','expected_exit_code':0,'expected_stdout':'OK'},
            'negative_control':{'input':'b','expected_exit_code':1,'expected_stdout':'BAD'}})]}
        deadline=time.monotonic()+1.6
        budget=ProbeBudget(self.root,ProbeBudget.configuration(({'cases':[case]},{'cases':[]}),1,0),deadline)
        original=probe.inspect_target_dependencies
        def slow(*args,**kwargs): time.sleep(1.7); return original(*args,**kwargs)
        with patch.object(probe,'inspect_target_dependencies',side_effect=slow), patch.object(probe.shutil,'which',return_value='docker'), \
             patch.object(probe,'_run_bounded') as launch, patch.object(probe.subprocess,'run') as cleanup:
            with self.assertRaisesRegex(RefinerError,'deadline exhausted'):
                validate_case_dependencies(case,self.root,'a',deadline=deadline,probe_budget=budget)
            launch.assert_not_called();cleanup.assert_not_called()
    def test_case_validator_uses_run_deadline_without_per_probe_cap(self):
        self.script.write_text('pass\n', encoding='utf8')
        case={'id':'case','planned_target_dependencies':[dict(self.dep, execution_contract={
            'runner':'docker-python-response','argv':[], 'expected_exit_code':0,'expected_stdout':'OK',
            'positive_control':{'input':'a','expected_exit_code':0,'expected_stdout':'OK'},
            'negative_control':{'input':'b','expected_exit_code':1,'expected_stdout':'BAD'}})]}
        deadline=time.monotonic()+120
        budget=ProbeBudget(self.root,ProbeBudget.configuration(({'cases':[case]},{'cases':[]}),1,0),deadline)
        with patch.object(probe,'execute_python_probe',return_value={'status':'ready','passed':True}) as execute:
            rows=validate_case_dependencies(case,self.root,'actual',deadline=deadline,probe_budget=budget)
        self.assertEqual(len(rows),3)
        self.assertEqual(execute.call_count,3)
        self.assertTrue(all(call.kwargs.get('timeout') is None for call in execute.call_args_list))
        self.assertTrue(all(call.kwargs['deadline']==deadline for call in execute.call_args_list))
    def test_process_creation_crossing_deadline_kills_before_threads(self):
        state={'killed':False}
        class Fake:
            def __init__(self,*a,**k):
                time.sleep(.20);self.stdout=io.BytesIO();self.stderr=io.BytesIO();self.stdin=io.BytesIO()
            def kill(self): state['killed']=True
            def wait(self,timeout=None):return 0
        with patch.object(probe.subprocess,'Popen',Fake):
            with self.assertRaisesRegex(RefinerError,'deadline exhausted during Docker launch'):
                probe._run_bounded(['docker','run'],b'',1,deadline=time.monotonic()+.1)
        self.assertTrue(state['killed'])
    def test_success_return_crossing_deadline_is_rejected(self):
        state={'killed':False}
        class Fake:
            def __init__(self,*a,**k):
                self.stdout=io.BytesIO(b'OK');self.stderr=io.BytesIO();self.stdin=io.BytesIO()
            def kill(self):state['killed']=True
            def wait(self,timeout=None): time.sleep(.20);return 0
        with patch.object(probe.subprocess,'Popen',Fake):
            with self.assertRaisesRegex(RefinerError,'deadline exhausted before Docker result returned'):
                probe._run_bounded(['docker','run'],b'',1,deadline=time.monotonic()+.1)
        self.assertTrue(state['killed'])
    def test_cleanup_crossing_deadline_cannot_return_success(self):
        self.script.write_text("print('OK')\n",encoding='utf8')
        def run(*args,**kwargs):
            kwargs['on_started']()
            return 0,b'OK\n',b''
        def cleanup(*args,**kwargs):time.sleep(.2)
        with patch.object(probe.shutil,'which',return_value='docker'), \
             patch.object(probe,'_run_bounded',side_effect=run),patch.object(probe.subprocess,'run',side_effect=cleanup):
            with self.assertRaisesRegex(RefinerError,'deadline exhausted before validator result returned'):
                execute_python_probe(self.root,'case-1',self.dep,self.contract,deadline=time.monotonic()+.1)
    def test_cleanup_phases_share_one_bounded_grace(self):
        events=[]
        class SlowClose(io.BytesIO):
            def close(self):events.append('close-start');time.sleep(.15);super().close()
        class Fake:
            def __init__(self,*a,**k):
                time.sleep(.06);self.stdout=io.BytesIO();self.stderr=io.BytesIO();self.stdin=SlowClose()
            def kill(self):events.append('kill');time.sleep(.04)
            def wait(self,timeout=None):events.append('wait');time.sleep(.04);return 0
        started=time.monotonic();deadline=started+.04
        window=probe._CleanupWindow(deadline,grace=.15)
        with patch.object(probe.subprocess,'Popen',Fake):
            with self.assertRaisesRegex(RefinerError,'cleanup_incomplete'):
                probe._run_bounded(['docker','run'],b'',1,deadline=deadline,cleanup=window)
        self.assertTrue(window.incomplete)
        self.assertLess(time.monotonic()-deadline,.20)
        self.assertEqual(events[:3],['kill','wait','close-start'])
        self.assertFalse(window.call('later Docker cleanup',lambda _:events.append('later')))
        self.assertNotIn('later',events)
    def test_engine_removal_precedes_local_reap_when_kill_hangs(self):
        self.script.write_text("print('OK')\n",encoding='utf8')
        events=[]
        class Fake:
            def __init__(self,argv,**kwargs):
                self.stdout=io.BytesIO();self.stderr=io.BytesIO();self.stdin=io.BytesIO()
                self.argv=argv;events.append('launch');events.append('auto-rm' if '--rm' in argv else 'missing-auto-rm')
            def kill(self):events.append('kill');time.sleep(.3)
            def wait(self,timeout=None):events.append('wait');time.sleep(.09);return 0
        def remove(*args,**kwargs):events.append('docker-rm');return type('Result',(),{'returncode':0})()
        started=time.monotonic()
        with patch.object(probe,'CLEANUP_GRACE_SECONDS',.4),patch.object(probe.shutil,'which',return_value='docker'), \
             patch.object(probe.subprocess,'Popen',Fake),patch.object(probe.subprocess,'run',side_effect=remove):
            with self.assertRaisesRegex(RefinerError,'cleanup_incomplete'):
                execute_python_probe(self.root,'case-1',self.dep,self.contract,deadline=started+.06)
        self.assertLess(time.monotonic()-started,.55)
        self.assertIn('docker-rm',events)
        self.assertIn('auto-rm',events)
        self.assertEqual(events.count('wait'),1)
        self.assertLess(events.index('kill'),events.index('docker-rm'))
    def test_engine_removal_timeout_reports_incomplete(self):
        self.script.write_text("print('OK')\n",encoding='utf8')
        events=[]
        class Fake:
            def __init__(self,*a,**k):self.stdout=io.BytesIO();self.stderr=io.BytesIO();self.stdin=io.BytesIO()
            def kill(self):events.append('kill')
            def wait(self,timeout=None):events.append('wait');time.sleep(.08);return 0
        def remove(*args,**kwargs):events.append('docker-rm');time.sleep(.4);return type('Result',(),{'returncode':0})()
        started=time.monotonic()
        with patch.object(probe,'CLEANUP_GRACE_SECONDS',.25),patch.object(probe.shutil,'which',return_value='docker'), \
             patch.object(probe.subprocess,'Popen',Fake),patch.object(probe.subprocess,'run',side_effect=remove):
            with self.assertRaisesRegex(RefinerError,'cleanup_incomplete'):
                execute_python_probe(self.root,'case-1',self.dep,self.contract,deadline=started+.04)
        self.assertLess(time.monotonic()-started,.38)
        self.assertIn('docker-rm',events)
        self.assertEqual(events.count('wait'),1)
    def test_missing_is_not_ready(self):
        self.assertEqual(self.run_probe()['status'], 'not_ready')
    def test_readonly_and_network_controls(self):
        self.script.write_text('''import os, socket
for p in ('/tmp/probe', '/dev/shm/probe', '/asset/other'):
    try:
        open(p, 'w').write('x')
    except OSError:
        pass
    else:
        raise SystemExit('write unexpectedly succeeded: ' + p)
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('1.1.1.1', 80))
except OSError:
    pass
else:
    raise SystemExit('network unexpectedly succeeded')
print('OK')
''',encoding='utf8')
        self.assertTrue(self.run_probe()['passed'])
    def test_link_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = pathlib.Path(other) / 'evil.py'
            outside.write_text("print('OK')\n", encoding='utf8')
            try:
                self.script.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest('Symlink creation unavailable: ' + str(exc))
            with self.assertRaisesRegex(RefinerError, 'linked path'):
                self.run_probe()
    def test_stdin_and_exit_are_bound(self):
        self.script.write_text("import sys; print(sys.stdin.read()); sys.exit(3)\n", encoding='utf8')
        self.contract.update(stdin='DATA', expected_stdout='DATA\n', expected_exit_code=3)
        self.assertTrue(self.run_probe()['passed'])
        self.contract['expected_exit_code'] = 0
        self.assertFalse(self.run_probe()['passed'])
    def test_same_basename_elsewhere_is_not_authority(self):
        self.script.write_text("print('BAD')\n", encoding='utf8')
        (self.root / 'check.py').write_text("print('OK')\n", encoding='utf8')
        self.assertFalse(self.run_probe()['passed'])

if __name__ == '__main__': unittest.main()

"""Windows/POSIX judge locking and real pinned skill-up with offline sessions."""
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import RefinerError
from refiner.skillup import local_judge_lock, SkillUp

class LocalJudgeLockTests(unittest.TestCase):
    def child(self,script):
        env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'scripts')
        return subprocess.Popen([sys.executable,'-B','-X','utf8','-c',script],stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,text=True,encoding='utf8',env=env)
    def test_separate_processes_are_mutually_exclusive(self):
        script = '''import json,time
from refiner.skillup import local_judge_lock
with local_judge_lock(time.monotonic()+10):
 a=time.monotonic();time.sleep(0.35);b=time.monotonic()
print(json.dumps([a,b]),flush=True)
'''
        processes=[self.child(script),self.child(script)]
        intervals=[]
        for process in processes:
            stdout,stderr=process.communicate(timeout=15)
            self.assertEqual(process.returncode,0,stderr);intervals.append(json.loads(stdout))
        intervals.sort()
        self.assertGreaterEqual(intervals[1][0],intervals[0][1])
    def test_timeout_and_exception_release_cross_process_lock(self):
        script = '''import time
from refiner.skillup import local_judge_lock
from refiner.common import RefinerError
try:
 with local_judge_lock(time.monotonic()+0.25): print("acquired")
except RefinerError: print("timeout")
'''
        with local_judge_lock(time.monotonic()+5):
            process=self.child(script);stdout,stderr=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,stderr);self.assertEqual(stdout.strip(),'timeout')
        with self.assertRaisesRegex(RuntimeError,'fixture'):
            with local_judge_lock(time.monotonic()+5):raise RuntimeError('fixture')
        process=self.child(script);stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,stderr);self.assertEqual(stdout.strip(),'acquired')
    def test_unsafe_lock_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            name='skill-refiner-judge.lock' if os.name=='nt' else f'skill-refiner-judge-{os.getuid()}.lock'
            (Path(directory)/name).mkdir()
            with patch('refiner.skillup.tempfile.gettempdir',return_value=directory):
                with self.assertRaises(RefinerError):
                    with local_judge_lock(time.monotonic()+2):pass
    def test_hardlinked_lock_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            name='skill-refiner-judge.lock' if os.name=='nt' else f'skill-refiner-judge-{os.getuid()}.lock'
            protected=base/'protected.txt';protected.write_bytes(b'unchanged')
            os.link(protected,base/name)
            with patch('refiner.skillup.tempfile.gettempdir',return_value=directory):
                with self.assertRaises(RefinerError):
                    with local_judge_lock(time.monotonic()+2):pass
            self.assertEqual(protected.read_bytes(),b'unchanged')
    def test_terminated_process_releases_system_lock(self):
        script = '''import time
from refiner.skillup import local_judge_lock
with local_judge_lock(time.monotonic()+10):
 print("locked",flush=True)
 time.sleep(30)
'''
        process = self.child(script)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
        finally:
            process.kill()
            process.communicate(timeout=10)
        with local_judge_lock(time.monotonic()+3):
            pass

class RealSkillUpOfflineEvaluateTests(unittest.TestCase):
    def test_two_suites_through_real_skillup_evaluate(self):
        executable=Path.home()/'.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe'
        if not executable.is_file():self.skipTest('Pinned installed skill-up unavailable')
        bash=Path('C:/Program Files/Git/bin/bash.exe')
        if os.name=='nt' and not bash.is_file():self.skipTest('Git Bash required by pinned local judge')
        with tempfile.TemporaryDirectory(prefix='refiner-eval-lock-') as temp:
            base=Path(temp);stub=base/'stub.py'
            stub.write_text('import json,pathlib,sys\n'
                'output=pathlib.Path(sys.argv[2]);output.parent.mkdir(parents=True,exist_ok=True)\n'
                'output.write_text(json.dumps({"exit_code":0,"final_message":"offline-ok","transcript":[],"artifacts":{"files":[]}}),encoding="utf-8")\n',encoding='utf8')
            input_file,output_file='$'+'{input_file}','$'+'{output_file}'
            engine={'name':'offline-local-session','custom':{'transport':'local','response_format':'session_result','local':{
                'command':sys.executable,'args':[str(stub),input_file,output_file],
                'input_file':input_file,'output_file':output_file}}}
            backend=SkillUp(str(executable),engine_config=engine,timeout=30,parallelism=2)
            backend.env['PYTHONDONTWRITEBYTECODE']='1'
            if os.name=='nt':backend.env['PATH']=str(bash.parent)+os.pathsep+backend.env.get('PATH','')
            for suite in ('development','acceptance'):
                target=base/('target-'+suite);target.mkdir()
                (target/'SKILL.md').write_text('---\nname: fixture-skill\ndescription: 离线执行测试。\n---\n返回结果。',encoding='utf8')
                cases=[{'id':suite+'-'+str(i),'prompt':'offline fixture','files':{},
                        'checks':[{'id':'A1','type':'text_equal','value':'offline-ok'}]} for i in (1,2)]
                result=backend.evaluate(target,cases,base/('run-'+suite))
                self.assertTrue(result['passed'],json.dumps(result,ensure_ascii=False))
                self.assertEqual({row['id'] for row in result['cases']},{row['id'] for row in cases})
                self.assertTrue((base/('run-'+suite)/'summary.json').is_file())
            self.assertEqual(backend.evaluations,4)

if __name__=='__main__':unittest.main()

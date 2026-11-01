"""Fine-grained skill-up progress; fake transport never calls a real model."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from refiner.common import RefinerError
from refiner.skillup import SkillUp, _CaseProgress, _verify_generated_judge


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith('{')
            and json.loads(line).get('stage') == 'case_progress']


class ProgressParserTests(unittest.TestCase):
    def test_chunks_crlf_ansi_unknown_ids_and_duplicates(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            parser = _CaseProgress(['case-one', 'case-two'], 'executor-1')
            for chunk in ['  \x1b[32m⏳ [1/', '2] case-one: case-', 'one...\r', '\n',
                          '  ✅ [1/2] case-one: PA', 'SS (100.0%)\n',
                          '  ✅ [1/2] case-one: PASS (100.0%)\n',
                          '  FAKE TOOL OUTPUT ✅ [2/2] case-two: PASS (100%)\n',
                          '  ✅ [2/2] case-two: PASS (100%)\n',
                          '  ⏳ [2/2] unknown: unknown...\n',
                          '  ⏳ [1/2] case-two: case-two...\n',
                          '  ⏳ [2/3] case-two: case-two...\n',
                          '  ⏳ [2/2] case-two: case-two...\r',
                          '  ❌ [2/2] case-two: FAIL (0.0%)\n',
                          '  ✅ [2/2] case-two: PASS (100.0%)\n']:
                parser.feed('stdout', chunk)
            parser.feed('stderr', '  ✅ [1/2] case-one: PASS (100.0%)\n')
            parser.finish()
        rows = events(output.getvalue())
        self.assertEqual([(r['case_id'], r['status']) for r in rows], [
            ('case-one','started'),('case-one','provisional_pass'),
            ('case-two','started'),('case-two','provisional_fail')])
        self.assertTrue(all(r['engine']=='executor-1' for r in rows))

    def test_invalid_encoding_is_ignored_and_long_lines_do_not_inject(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            parser = _CaseProgress(['case-one'], 'secret value not an allowed engine')
            parser.feed('stdout', b'\xff [1/1] case-one: case-one...\n')
            parser.feed('stdout', 'x' * 70000)
            parser.feed('stdout', '[1/1] case-one: PASS (100.0%)\n')
            parser.feed('stdout', '❌ [1/1] case-one: FAIL (0.0%)')
            parser.finish()
        rows = events(output.getvalue())
        self.assertEqual([r['status'] for r in rows], ['started','provisional_fail'])
        self.assertTrue(all(r['engine']=='configured' for r in rows))

    def test_abort_never_turns_provisional_pass_into_final_pass(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            parser = _CaseProgress(['case-one','case-two'], 'executor')
            parser.feed('stdout', '⏳ [1/2] case-one: case-one...\n✅ [1/2] case-one: PASS (100%)\n')
            parser.abort(); parser.abort()
        self.assertEqual([e['status'] for e in events(output.getvalue())],
                         ['started','provisional_pass','final_error'])


class ProgressIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'target'; self.target.mkdir()
        (self.target / 'SKILL.md').write_text('# fixture', encoding='utf-8')
        self.binary = self.root / 'skill-up'; self.binary.write_text('fixture', encoding='utf-8')
        self.backend = SkillUp(str(self.binary), engine_config={'name':'fixture'}, timeout=2)
        self.cases = [{'id':'case-one','prompt':'fixture','files':{},
                       'checks':[{'id':'a1','type':'text_equal','value':'ok'}]}]

    def test_process_exception_converts_seen_progress_to_error(self):
        output = io.StringIO()
        def fake_process(argv, **kwargs):
            if 'validate' in argv:
                return 0, '', '', 0
            callback = kwargs['on_output']
            callback('stdout', '⏳ [1/1] case-one: case-one...\n✅ [1/1] case-one: PASS (100.0%)\n')
            raise RefinerError('fixture error')
        with patch('refiner.skillup.run_process', side_effect=fake_process), contextlib.redirect_stdout(output):
            with self.assertRaises(RefinerError):
                self.backend.evaluate(self.target, self.cases, self.root/'run')
        self.assertEqual([e['status'] for e in events(output.getvalue())],
                         ['started','provisional_pass','final_error'])
        self.assertEqual(self.backend.evaluations, 1)

    def test_missing_judge_identity_or_changed_oracle_cannot_finalize_pass(self):
        for attack in ('missing-identity','changed-oracle','changed-grader'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temp:
                run = Path(temp)/'run'
                output = io.StringIO()
                def fake_execute(config, run_dir, count, *, required_contract_hashes, case_progress, lock_held):
                    case_progress.feed('stdout','⏳ [1/1] case-one: case-one...\n✅ [1/1] case-one: PASS (100%)\n')
                    grader = run_dir/'case-one.judge.sh'
                    evidence = {'case_id':'case-one','passed':True,'assertions':[{'assertion_id':'a1','check':self.cases[0]['checks'][0],'passed':True}]}
                    if attack=='missing-identity': evidence['case_id']='other'
                    if attack=='changed-oracle': (run_dir/'case-one.oracle.json').write_text('{}',encoding='utf8')
                    if attack=='changed-grader': grader.write_text('tampered',encoding='utf8')
                    assertion = {'text':'script: '+str(grader),'passed':True,'evidence':json.dumps(evidence)}
                    row={'id':'case-one','status':'PASS','passed':True,'evidence':{
                        'case_id':'case-one','grading':{'status':'PASS','assertion_results':[assertion]}}}
                    return {'passed':True,'cases':[row],'seconds':0,'upstream_exit_code':0}
                with patch('refiner.skillup.run_process',return_value=(0,'','',0)), \
                     patch.object(self.backend,'_execute',side_effect=fake_execute), contextlib.redirect_stdout(output):
                    result=self.backend.evaluate(self.target,self.cases,run)
                self.assertFalse(result['passed'])
                self.assertEqual(result['cases'][0]['status'],'ERROR')
                self.assertEqual([e['status'] for e in events(output.getvalue())],
                                 ['started','provisional_pass','final_error'])

    def test_full_oracle_assertion_set_and_contract_hash_required(self):
        import hashlib
        grader=self.root/'judge.sh';grader.write_text('fixture',encoding='utf8')
        oracle=self.root/'oracle.json';oracle.write_text('{}',encoding='utf8')
        definition=self.root/'definition.json';definition.write_text('{}',encoding='utf8')
        check={'id':'a1','type':'text_equal','value':'ok'}
        frozen={'case-one':{'oracle':oracle,'oracle_sha256':hashlib.sha256(oracle.read_bytes()).hexdigest(),
                            'grader':grader,'grader_sha256':hashlib.sha256(grader.read_bytes()).hexdigest(),
                            'definition':definition,'definition_sha256':hashlib.sha256(definition.read_bytes()).hexdigest(),
                            'active_definition':definition,'active_definition_sha256':hashlib.sha256(definition.read_bytes()).hexdigest(),
                            'checks':{'a1':check},'contract_sha256':'a'*64}}
        normal={'assertion_id':'a1','check':check,'passed':True}
        contract={'assertion_id':'response_contract','check':{'id':'response_contract','type':'json_schema'},
                  'passed':True,'expected_condition':{'contract_sha256':'a'*64}}
        def row(checks):
            evidence={'case_id':'case-one','passed':True,'assertions':checks}
            return {'id':'case-one','status':'PASS','passed':True,'evidence':{'grading':{
                'status':'PASS','assertion_results':[{'text':'script: '+str(grader),'passed':True,
                                                       'evidence':json.dumps(evidence)}]}}}
        with patch("refiner.skillup._replay_observation", return_value={"a1": True, "response_contract": True}):
            value = row([normal, contract]); value["evidence"]["response"] = "ok"
            self.assertIsNone(_verify_generated_judge(value, frozen))
        bad_contract={**contract,'expected_condition':{'contract_sha256':'b'*64}}
        bad_false={**normal,'passed':False}
        changed={**normal,'check':{'id':'a1','type':'text_equal','value':'changed'}}
        other={'assertion_id':'extra','check':{'id':'extra'},'passed':True}
        for checks in ([],[normal],[contract],[normal,normal,contract],[normal,contract,other],
                       [bad_false,contract],[changed,contract],[normal,bad_contract]):
            with self.subTest(checks=checks):
                self.assertIsNotNone(_verify_generated_judge(row(checks),frozen))


    def test_same_target_generation_is_serial_and_preexisting_is_preserved(self):
        entered=threading.Event(); release=threading.Event(); second_entered=threading.Event(); failures=[]
        backend2=SkillUp(str(self.binary),engine_config={'name':'fixture2'},timeout=4)
        def first(*args,**kwargs):
            entered.set(); release.wait(5)
            return {'passed':False,'cases':[{'id':'case-one','status':'ERROR','passed':False,'evidence':{}}], 'seconds':0}
        def second(*args,**kwargs):
            second_entered.set()
            return {'passed':False,'cases':[{'id':'case-one','status':'ERROR','passed':False,'evidence':{}}], 'seconds':0}
        def run(backend,run_dir):
            try: backend.evaluate(self.target,self.cases,run_dir)
            except BaseException as exc: failures.append(exc)
        with patch('refiner.skillup.run_process',return_value=(0,'','',0)), \
             patch.object(self.backend,'_execute',side_effect=first),patch.object(backend2,'_execute',side_effect=second), \
             contextlib.redirect_stdout(io.StringIO()):
            t1=threading.Thread(target=run,args=(self.backend,self.root/'run-a'));t1.start()
            self.assertTrue(entered.wait(3))
            generated=self.target/'evals/.refiner-generated'; before=set(generated.iterdir())
            t2=threading.Thread(target=run,args=(backend2,self.root/'run-b'));t2.start()
            self.assertFalse(second_entered.wait(0.4));self.assertEqual(set(generated.iterdir()),before)
            release.set();t1.join(8);t2.join(8)
        self.assertFalse(t1.is_alive());self.assertFalse(t2.is_alive());self.assertFalse(failures,str(failures))
        self.assertTrue(second_entered.is_set())
        generated.mkdir(); marker=generated/'preserve';marker.write_text('keep',encoding='utf8')
        with self.assertRaises(RefinerError):
            self.backend.evaluate(self.target,self.cases,self.root/'run-c')
        self.assertEqual(marker.read_text(encoding='utf8'),'keep')

    def test_duplicate_or_unexpected_result_never_finalizes_pass(self):
        for attack in ('duplicate','unknown'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temp:
                output=io.StringIO()
                def fake_execute(config,run_dir,count,*,required_contract_hashes,case_progress,lock_held):
                    case_progress.feed('stdout','⏳ [1/1] case-one: case-one...\n✅ [1/1] case-one: PASS (100%)\n')
                    row={'id':'case-one','status':'PASS','passed':True,'evidence':{}}
                    return {'passed':True,'cases':[row,row] if attack=='duplicate' else [{**row,'id':'other'}],
                            'seconds':0,'upstream_exit_code':0}
                with patch('refiner.skillup.run_process',return_value=(0,'','',0)), \
                     patch.object(self.backend,'_execute',side_effect=fake_execute),contextlib.redirect_stdout(output):
                    with self.assertRaises(RefinerError):
                        self.backend.evaluate(self.target,self.cases,Path(temp)/'run')
                self.assertEqual([e['status'] for e in events(output.getvalue())],
                                 ['started','provisional_pass','final_error'])


class RealSkillUpProgressTests(unittest.TestCase):
    def test_live_progress_precedes_pinned_tool_completion(self):
        exe = Path.home()/'.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe'
        bash = Path('C:/Program Files/Git/bin/bash.exe')
        if not exe.is_file() or (os.name=='nt' and not bash.is_file()):
            self.skipTest('Pinned skill-up or local judge shell unavailable')
        with tempfile.TemporaryDirectory(prefix='refiner-real-progress-') as tmp:
            root=Path(tmp);target=root/'target';target.mkdir()
            (target/'SKILL.md').write_text('---\nname: progress-fixture\ndescription: Offline fixture.\n---\nReturn ok.\n',encoding='utf8')
            stub=root/'stub.py'
            stub.write_text('import json,pathlib,sys,time\n'
                'time.sleep(1.5)\n'
                'p=pathlib.Path(sys.argv[2]);p.parent.mkdir(parents=True,exist_ok=True)\n'
                'p.write_text(json.dumps({"exit_code":0,"final_message":"ok","transcript":[],"artifacts":{"files":[]}}),encoding="utf8")\n',encoding='utf8')
            inp,out='$'+'{input_file}','$'+'{output_file}'
            engine={'name':'offline-progress','custom':{'transport':'local','response_format':'session_result',
                    'local':{'command':sys.executable,'args':[str(stub),inp,out],
                             'input_file':inp,'output_file':out}}}
            backend=SkillUp(str(exe),engine_config=engine,timeout=20,parallelism=2)
            if os.name=='nt':backend.env['PATH']=str(bash.parent)+os.pathsep+backend.env.get('PATH','')
            cases=[{'id':'case-one','prompt':'first','files':{},'checks':[{'id':'a1','type':'text_equal','value':'ok'}]},
                   {'id':'case-two','prompt':'second','files':{},'checks':[{'id':'a2','type':'text_equal','value':'other'}]}]
            class Capture(io.StringIO):
                def __init__(self):
                    super().__init__(); self.started=threading.Event()
                def write(self, data):
                    result=super().write(data)
                    if '"status": "started"' in data:self.started.set()
                    return result
            outbuf=Capture();result=[];failure=[]
            def worker():
                try: result.append(backend.evaluate(target,cases,root/'run'))
                except BaseException as exc: failure.append(exc)
            with contextlib.redirect_stdout(outbuf):
                thread=threading.Thread(target=worker);thread.start()
                observed=outbuf.started.wait(15)
                still_running=thread.is_alive()
                thread.join(45)
            self.assertFalse(thread.is_alive())
            self.assertTrue(observed);self.assertTrue(still_running)
            self.assertFalse(failure, str(failure))
            self.assertEqual(backend.evaluations,2)
            rows=events(outbuf.getvalue())
            self.assertEqual([(r['case_id'],r['status']) for r in rows], [
                ('case-one','started'),('case-one','provisional_pass'),
                ('case-two','started'),('case-two','provisional_fail'),
                ('case-one','final_pass'),('case-two','final_fail')])
            self.assertEqual([r['status'] for r in result[0]['cases']],['PASS','FAIL'])
            self.assertTrue((root/'run/summary.json').is_file())
            for row in rows:
                if row["status"].startswith("final_"):
                    self.assertEqual(Path(row["evidence_ref"]), (root/"run/summary.json").resolve())
                    self.assertTrue(Path(row["evidence_ref"]).is_file())
                else:
                    self.assertNotIn("evidence_ref", row)


class ObservationSecurityTests(unittest.TestCase):
    def test_sensitive_keys_paths_and_reserved_fixtures(self):
        from refiner.skillup import _sensitive_observation, _observation_source
        secret = 'crsr_' + 'A' * 32
        self.assertTrue(_sensitive_observation({secret: 'ok'}))
        self.assertTrue(_sensitive_observation(json.dumps({secret: 'ok'})))
        self.assertTrue(_sensitive_observation('mail someone@real-domain.com'))
        self.assertFalse(_sensitive_observation('mail demo@example.com or test@support.invalid'))
        self.assertFalse(_sensitive_observation('202-555-0142'))
        with tempfile.TemporaryDirectory() as tmp:
            case = {'checks':[{'id':'a','type':'file_exists','path':secret+'.txt'}]}
            with self.assertRaises(RefinerError):
                _observation_source(case, Path(tmp), 'ok')

    def test_absence_requires_authenticated_coverage_not_empty_trace(self):
        from refiner.skillup import _observation_source
        for kind in ('tool_call_absent', 'tool_command_absent'):
            case={'checks':[{'id':'a','type':kind,'value':'forbidden'}]}
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EVAL_TRANSCRIPT_PATH':''}):
                with self.assertRaisesRegex(RefinerError, 'coverage proof unavailable'):
                    _observation_source(case,Path(tmp),'ok')

    def test_real_skillup_replay_tool_file_and_sensitive_json_key(self):
        exe=Path.home()/'.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe'
        bash=Path('C:/Program Files/Git/bin/bash.exe')
        if not exe.is_file() or (os.name == 'nt' and not bash.is_file()):
            self.skipTest('pinned skill-up or shell unavailable')
        with tempfile.TemporaryDirectory(prefix='refiner-observation-') as tmp:
            root=Path(tmp); target=root/'target'; target.mkdir()
            (target/'SKILL.md').write_text('---\nname: fixture\ndescription: Offline fixture.\n---\nReturn ok.\n',encoding='utf8')
            session={'exit_code':0,'final_message':'ok','transcript':[
                {'role':'tool_call','tool_call':{'id':'c1','name':'shell','arguments':{'command':'fixture-command'}}},
                {'role':'tool_result','tool_result':{'call_id':'c1','status':'success','content':'fixture-output'}}],
                'artifacts':{'files':[]}}
            fixture=root/'session.json';fixture.write_text(json.dumps(session),encoding='utf8')
            stub=root/'stub.py';stub.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[2]);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(pathlib.Path(sys.argv[3]).read_bytes())\n',encoding='utf8')
            inp,out=chr(36)+'{input_file}',chr(36)+'{output_file}'
            config={'name':'offline-observation','custom':{'transport':'local','response_format':'session_result',
                'local':{'command':sys.executable,'args':[str(stub),inp,out,str(fixture)],'input_file':inp,'output_file':out}}}
            backend=SkillUp(str(exe),engine_config=config,timeout=30,parallelism=1)
            if os.name=='nt':backend.env['PATH']=str(bash.parent)+os.pathsep+backend.env.get('PATH','')
            secret='crsr_'+'A'*32
            specs=[('pass','fixture-file','fixture-file','PASS'),('fail','fixture-file','wrong','FAIL'),
                   ('sensitive',json.dumps({secret:'fixture-file'}),'fixture-file','ERROR'),
                   ('tamper-file','fixture-file','fixture-file','ERROR'),
                   ('tamper-response','fixture-file','fixture-file','ERROR')]
            for name,content,expected,status in specs:
                case={'id':'case-one','prompt':'fixture','files':{'evidence.txt':content},'checks':[
                    {'id':'a1','type':'tool_command','value':'fixture-command'},
                    {'id':'a2','type':'file_exists','path':'evidence.txt','value':''},
                    {'id':'a3','type':'file_text_equal','path':'evidence.txt','value':expected}]}
                original_execute=backend._execute
                def mutate(*args, **kwargs):
                    result=original_execute(*args, **kwargs)
                    if name=='tamper-file':
                        (root/name/'case-one.observation/files/evidence.txt').write_text('tampered',encoding='utf8')
                    if name=='tamper-response':
                        result['cases'][0]['evidence']['response']='wrong'
                    return result
                with patch.object(backend,'_execute',side_effect=mutate), contextlib.redirect_stdout(io.StringIO()):
                    result=backend.evaluate(target,[case],root/name)
                self.assertEqual(result['cases'][0]['status'],status, str(result))
                observed=root/name/'case-one.observation'
                if name=='sensitive': self.assertFalse(observed.exists())
                elif status!='ERROR':
                    self.assertTrue((observed/'observation.json').is_file())
                    self.assertTrue((observed/'transcript.json').is_file())
                    self.assertEqual((observed/'files/evidence.txt').read_text(encoding='utf8'),content)


if __name__ == '__main__':
    unittest.main()

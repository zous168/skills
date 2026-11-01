"""Offline recovery-chain tests using the locally captured terminal run; no model calls."""
import copy, hashlib, json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from refiner.refine_plan_recovery import migrate,verify_recovery,_controller_hash,_chain_tree,_io_path,_chain_temporary_directory
from refiner.common import read_json,write_json,RefinerError

HOME=Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3')
ART=Path('D:/work/skills/.tasks/artifacts/GE-yzhe-20261030-184954-fdd3')
SOURCE=HOME/'refine-continuation-20261031-v7';ARCH=ART/'controller-before-v7-followup/skill-refiner'
ANCESTOR=HOME/'refine-run-composer/run';ANCHOR=ART/'refine-v7-observed-hashes.json'

def filehash(path):return hashlib.sha256(path.read_bytes()).hexdigest()

@unittest.skipUnless(all(p.exists() for p in (SOURCE,ARCH,ANCESTOR,ANCHOR)), 'local terminal sample unavailable')
class RecoveryChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='refiner-chain-test-');cls.root=Path(cls.tmp.name)
        cls.out=cls.root/'first';cls.before=_chain_tree(SOURCE)
        cls.result=migrate(SOURCE,ARCH,ANCESTOR,cls.out,source_snapshot=ANCHOR)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def verify(self):return verify_recovery(self.out,read_json(self.out/'resume-contract.json'))
    def test_real_source_restores_complete_feedback_and_usage(self):
        x=self.verify();self.assertEqual(x['usage'],{'agent_calls':8,'elapsed_seconds':683.08,'target_evaluations':0})
        self.assertEqual(len(x['control_records']),2);self.assertEqual(len(x['coverage_gaps']),8)
        self.assertEqual([p['id'] for p in x['previous_plan']['cases']],['c1','c2','c3','c4','c5','c6'])
        self.assertIn('Execution step E2',x['validation_feedback'][0]);self.assertIn('Legacy good_responses',x['validation_feedback'][1])
        self.assertIn('coverage gaps',x['validation_feedback'][-1]);self.assertEqual(self.before,_chain_tree(SOURCE))
        self.assertEqual(self.result['ancestor']['agent_calls'],1)
    def test_tamper_archived_response_control_and_controller_fail_closed(self):
        root=Path(self.verify()['source_evidence_root'])
        for p in [root/'agent-calls/006-plan/response.json',root/'plan-controls/attempts/development-02.json',self.out/'recovery-chain/archive/scripts/refiner/agent.py']:
            with self.subTest(p=p):
                prior=p.read_bytes()
                try:
                    p.write_bytes(prior+b' ')
                    with self.assertRaises(RefinerError):self.verify()
                finally:p.write_bytes(prior)
        self.verify()
    def test_tamper_live_input_or_extra_archived_file_fail_closed(self):
        root=Path(self.verify()['source_evidence_root']);p=root/'unexpected-file';p.write_text('fixture')
        try:
            with self.assertRaises(RefinerError):self.verify()
        finally:p.unlink()
        p=self.out/'inputs.json';prior=p.read_bytes()
        try:
            p.write_bytes(prior+b' ')
            with self.assertRaises(RefinerError):self.verify()
        finally:p.write_bytes(prior)
    def test_source_snapshot_or_ancestor_identity_mismatch_rejected(self):
        snapshot=read_json(ANCHOR);snapshot['files'][0]['sha256']='0'*64;path=self.root/'bad-snapshot.json';write_json(path,snapshot)
        with self.assertRaises(RefinerError):migrate(SOURCE,ARCH,ANCESTOR,self.root/'bad',source_snapshot=path)
        with self.assertRaises(RefinerError):migrate(SOURCE,ARCH,self.root,self.root/'bad2',source_snapshot=ANCHOR)
        self.assertFalse((self.root/'bad').exists());self.assertFalse((self.root/'bad2').exists())

    def test_real_windows_deep_path_and_failed_migration_cleanup(self):
        import refiner.refine_plan_recovery as recovery
        base=self.root/('deep-'+'x'*85);base.mkdir()
        target=base/'refine-continuation-20261031-v7-r2'
        result=migrate(SOURCE,ARCH,ANCESTOR,target,source_snapshot=ANCHOR)
        self.assertEqual(result['inherited']['agent_calls'],8)
        self.assertEqual(len(verify_recovery(target,read_json(target/'resume-contract.json'))['coverage_gaps']),8)
        # Avoid stdlib tempfile's Windows MAX_PATH-limited cleanup in the fixture.
        shutil.rmtree(_io_path(target,force=True))
        original=recovery._archived_verify;calls=[]
        def fail_on_preserved(archive,source):
            calls.append(1)
            if len(calls)==2:raise RefinerError('injected original migration error')
            return original(archive,source)
        with patch.object(recovery,'_archived_verify',side_effect=fail_on_preserved):
            with self.assertRaisesRegex(RefinerError,'injected original migration error'):
                migrate(SOURCE,ARCH,ANCESTOR,target,source_snapshot=ANCHOR)
        self.assertFalse(target.exists())
        self.assertFalse(any(p.name.startswith('.refine-chain-') for p in base.iterdir()))
        self.assertEqual(self.before,_chain_tree(SOURCE))

    def test_cleanup_failure_never_masks_primary_exception(self):
        with patch('refiner.refine_plan_recovery.shutil.rmtree',side_effect=OSError('fixture cleanup')):
            with self.assertRaisesRegex(RefinerError,'primary failure') as result:
                with _chain_temporary_directory(self.root):
                    raise RefinerError('primary failure')
        self.assertTrue(any('cleanup failed' in note for note in result.exception.__notes__))

    def test_later_controller_migration_preserves_chain_and_counts_once(self):
        source=self.root/'second-source';shutil.copytree(self.out,source)
        archive=self.root/'new-archive';shutil.copytree(ROOT/'scripts',archive/'scripts',ignore=shutil.ignore_patterns('__pycache__'));shutil.copytree(ROOT/'schemas',archive/'schemas')
        marker=archive/'scripts/refiner/runtime.py';marker.write_bytes(marker.read_bytes()+b'\n# offline next controller fixture\n')
        contract=read_json(source/'resume-contract.json');contract['controller_sha256']=_controller_hash(archive);write_json(source/'resume-contract.json',contract)
        state=read_json(source/'state.json');state['agent_calls']=9;state['elapsed_seconds']=690.0;state['workspace']=str(source);write_json(source/'state.json',state)
        stages=read_json(source/'stages.json');stages['run_id']=source.name
        original=self.verify()['previous_plan'];plan=copy.deepcopy(original);plan['execution_plan']['objective']+=' 测试延续。'
        d=source/'agent-calls/009-plan';d.mkdir(parents=True);request={'role':'plan','input':{},'instruction':'fixture','response_schema':{}}
        raw=json.dumps(request,ensure_ascii=False).encode('utf8');write_json(d/'request.json',request);(d/'transmission.txt').write_bytes(raw)
        write_json(d/'response.json',plan);rsp=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode('utf8')).hexdigest()
        write_json(d/'request-manifest.json',{'role':'plan','status':'completed','request_sha256':hashlib.sha256(raw).hexdigest(),'request_bytes':len(raw),'transmitted_sha256':hashlib.sha256(raw).hexdigest(),'transmitted_bytes':len(raw),'response_sha256':rsp})
        write_json(d/'execution.json',{'role':'plan','status':'completed','finished_at':'fixture'})
        write_json(d/'process.execution.json',{'status':'completed','exit_code':0,'finished_at':'fixture'})
        from refiner.common import digest
        ctrl={'schema_version':'skill-refiner.plan-controls/v1','suite':'development','attempt':1,'status':'validation_error','plan_sha256':digest(plan),'plan_snapshot':plan,'validation_error':{'type':'RefinerError','message':'fixture unresolved','case_ids':[]},'revision_context':{'feedback':[]}}
        control=source/'plan-controls/attempts/development-01.json';write_json(control,ctrl);write_json(source/'plan-controls/development.json',ctrl)
        template=next(x for x in read_json(SOURCE/'stages.json')['stages'][1]['artifacts'] if x['path']=='plan-controls/development.json')
        rows=[]
        for path in (control,source/'plan-controls/development.json',d/'response.json'):
            row=copy.deepcopy(template);row.update(path=path.relative_to(source).as_posix(),sha256=filehash(path),bytes=path.stat().st_size);rows.append(row)
        stages['stages'][1]['artifacts']=rows;write_json(source/'stages.json',stages)
        inventory=_chain_tree(source);snapshot={'source':str(source),'files':[{'path':k,'bytes':(source/k).stat().st_size,'sha256':v} for k,v in inventory.items()]}
        snap=self.root/'second-snapshot.json';write_json(snap,snapshot);dest=self.root/'third'
        result=migrate(source,archive,ANCESTOR,dest,source_snapshot=snap)
        self.assertEqual(result['inherited']['agent_calls'],9);self.assertEqual(result['ancestor']['agent_calls'],1)
        x=verify_recovery(dest,read_json(dest/'resume-contract.json'));self.assertEqual(x['usage']['agent_calls'],9)
        self.assertEqual(x['previous_plan'],plan);self.assertEqual(len(x['coverage_gaps']),8)
        lineage=read_json(dest/'refine-plan-recovery.json')['lineage'];self.assertEqual(len(lineage),4);self.assertEqual(len(set(lineage)),4)


R2=HOME/'refine-continuation-20261031-v7-r2'
R2_ARCH=ART/'controller-before-r2-fix/skill-refiner'
R2_ANCHOR=ART/'refine-v7-r2-observed-hashes.json'

@unittest.skipUnless(all(p.exists() for p in (R2,R2_ARCH,R2_ANCHOR,ANCESTOR)), 'r2 terminal sample unavailable')
class R2CarryForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='refiner-r2-carry-');cls.root=Path(cls.tmp.name)
        cls.before=_chain_tree(R2)
        base=cls.root/('deep-'+'x'*75);base.mkdir()
        cls.out=base/'refine-continuation-20261031-v7-r3'
        cls.result=migrate(R2,R2_ARCH,ANCESTOR,cls.out,source_snapshot=R2_ANCHOR)
    @classmethod
    def tearDownClass(cls):
        if cls.out.exists():shutil.rmtree(_io_path(cls.out,force=True))
        cls.tmp.cleanup()
    def verify(self):return verify_recovery(self.out,read_json(self.out/'resume-contract.json'))
    def test_real_terminal_source_through_mock_pipeline(self):
        from refiner.pipeline import make_plan
        rec=self.verify();self.assertEqual(rec['usage'],{'agent_calls':8,'elapsed_seconds':685.25,'target_evaluations':0})
        self.assertEqual(len(rec['control_records']),2);self.assertEqual(len(rec['coverage_gaps']),8)
        self.assertEqual(self.result['ancestor']['agent_calls'],1)
        self.assertTrue(str(rec['source_evidence_root']).startswith('\\\\?\\') if os.name=='nt' else True)
        context=read_json(self.out/'inputs.json');context['_response_contract_v7']=True
        calls=[]
        class Fake:
            def ask(self,role,instruction,payload,schema):
                calls.append((role,payload));raise RefinerError('mock first plan call')
        with self.assertRaisesRegex(RefinerError,'mock first plan call'):
            make_plan(Fake(),context,self.out,12,recovery=rec)
        self.assertEqual(len(calls),1);role,payload=calls[0];self.assertEqual(role,'plan')
        self.assertEqual(len(payload['previous_plan']['cases']),6)
        self.assertEqual(len(payload['previous_plan']['coverage_gaps']),8)
        feedback=payload['validation_feedback']
        prefix='Current full controls for recovered plan: Frozen plan controls failed: '
        failures=json.loads(next(row for row in feedback if row.startswith(prefix))[len(prefix):])
        self.assertEqual(len(failures),22)
        self.assertEqual({item['case_id'] for item in failures},{'c1','c2','c3','c4','c5','c6'})
        diagnostic=next(row for row in feedback if 'Structured validation feedback: ' in row)
        policies=json.loads(diagnostic.split('Structured validation feedback: ',1)[1])['execution_paths']
        self.assertEqual([item['step_id'] for item in policies],['E2','E3','E4','E5','E6','E7'])
        self.assertTrue(all(gap in '\n'.join(feedback) for gap in payload['previous_plan']['coverage_gaps']))
        self.assertEqual(self.before,_chain_tree(R2))
    def test_decision_or_elapsed_tamper_fails_before_mock_call(self):
        rec=self.verify();root=Path(rec['source_evidence_root']);plan=rec['control_records'][1]['plan_snapshot']
        from refiner.common import digest
        decision=root/'plan-controls/response-applicability'/(digest(plan)+'.json')
        prior=decision.read_bytes()
        try:
            decision.write_bytes(prior+b' ')
            with self.assertRaises(RefinerError):self.verify()
        finally:decision.write_bytes(prior)
        source=self.out/'recovery-chain/source'/R2.name
        state=_io_path(source/'state.json',force=True);prior=state.read_bytes()
        try:
            value=json.loads(prior);value['elapsed_seconds']=683.08;write_json(state,value)
            with self.assertRaises(RefinerError):self.verify()
        finally:state.write_bytes(prior)
    def test_cannot_reuse_if_calls_or_error_changed(self):
        import refiner.refine_plan_recovery as r
        state=read_json(R2/'state.json');stages=read_json(R2/'stages.json');data=read_json(R2/'refine-plan-recovery.json')
        prior=r._archived_verify(R2_ARCH,R2)
        for change in ({'agent_calls':9},{'error':'other error'}):
            altered={**state,**change}
            with self.assertRaises(RefinerError):r._carry_forward_evidence(R2,altered,stages,prior,data)


if __name__=='__main__':unittest.main()

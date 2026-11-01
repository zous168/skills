import copy, hashlib, json, shutil, sys, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.agent import Agent
from refiner.cli import main
from refiner.common import read_json, write_json
from refiner.creation import ANALYSIS_V3 as ANALYSIS
from refiner.stages import StageManifest
from refiner.technical_recovery import clarification_history
from refiner.common import RefinerError
import test_checkpoint_migration as checkpoint_fixture
import test_analysis_contract_v3 as analysis_fixture


def incomplete_v4(candidate, standards):
    """A deliberately rejected inventory for protocol/migration controls, never a pass."""
    from refiner.obligations import standard_sources
    authority = standard_sources(standards)[0]
    value = copy.deepcopy(candidate)
    value.update(schema_version='skill-refiner.analysis/v4', blocking_questions=[])
    value['target_obligations'] = {
        'schema_version': 'skill-refiner.target-obligations/v1',
        'sources': [{'source_id': authority['id'], 'content_sha256': authority['content_sha256'],
                     'obligation_ids': ['O-INCOMPLETE']}],
        'obligations': [{'id': 'O-INCOMPLETE', 'subject': 'controller', 'applicable': True,
            'applicability_reason': 'Synthetic rejected inventory for migration controls only.',
            'behavior': 'A deliberately incomplete standard assessment, never accepted.',
            'source': {'source_id': authority['id'], 'content_sha256': authority['content_sha256'],
                       'quote': authority['content'][:60]},
            'acceptance_criteria': [], 'scenario_ids': [], 'workflow_ids': [],
            'mechanism': {'state': 'not_applicable', 'interface': 'No target interface in this rejected fixture.',
                         'evidence_strategy': 'Independent review must reject incomplete standard scope.', 'gap_ids': []}}],
        'coverage_gaps': []}
    return value


class TechnicalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = checkpoint_fixture.CheckpointMigrationTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        _,_,previous,_=f._rejected_analysis_fixture()
        inputs=read_json(f.old/'inputs.json')
        candidate=copy.deepcopy(previous)
        candidate['goal']+=' Further independently reviewable scope.'
        candidate['blocking_questions']=[{'id':'BQ-T','question':'能否独立观察测试装置的动作？','decision_owner':'执行维护者','impact':'确定验证工具可观察的范围与缺口。'}]
        candidate['blocking_questions'] += copy.deepcopy(getattr(self, 'additional_questions', []))
        sources=[{'id':'business_description','content':'业务需求'}]
        review={'schema_version':'skill-refiner.clarification-review/v1','question_assessments':[
          {'question_id':'BQ-T','classification':'required_in_scope','evidence':[{'source_id':'business_description','quote':'业务需求'}],
           'reason':'该问题检查已要求行为的独立证据。','combination_risk_assessed':True,
           'required_requirement_ids_examined':['R1'],'out_of_scope':''}]}
        for question in getattr(self, 'additional_questions', []):
            row = copy.deepcopy(review['question_assessments'][0])
            row.update(question_id=question['id'],
                       classification=getattr(self, 'additional_classifications', {}).get(question['id'], 'required_in_scope'),
                       reason='This material business choice or uncertainty remains unresolved.')
            review['question_assessments'].append(row)
        schema=read_json(Path(__file__).resolve().parents[1]/'schemas/v1/clarification-review.schema.json')
        stage=StageManifest(f.old,'create',resume=True);stage.activate('analysis')
        agent=Agent(f.old,config=read_json(f.config),inherited_calls=4)
        responses=iter([json.dumps(candidate,ensure_ascii=False),'{"invalid":true}',json.dumps(review,ensure_ascii=False)])
        def fake(*args,**kwargs): return 0,next(responses),'',0.01
        with patch('refiner.agent.run_process',side_effect=fake):
            agent.ask('analyze','fixture',{**inputs,'business_sources':sources},ANALYSIS)
            agent.ask('analysis-review','fixture',{'business_sources':sources,'analysis':candidate},schema)
        write_json(f.old/'analysis.json',candidate)
        stage.finish('needs_clarification')
        state=read_json(f.old/'state.json');state.update(agent_calls=7,creation_agent_calls=7);write_json(f.old/'state.json',state)
        self.args=[x if x!='4' else '7' for x in f.import_cmd]+['--carry-analysis-feedback','--carry-technical-clarification']
        self.candidate=candidate
    def test_mixed_chain_carries_invalid_retry_and_preserves_source(self):
        f=self.fixture
        call=f.old/'agent-calls/005-analyze'
        (call/'backend-result.json').write_text('unverified',encoding='utf8')
        (call/'response.txt').write_text('unverified',encoding='utf8')
        (call/'cwd').mkdir(exist_ok=True)
        (call/'cwd/response-schema.json').write_text('unverified',encoding='utf8')
        unknown=call/'unknown.json';unknown.write_text('{}',encoding='utf8')
        self.assertEqual(main(self.args),2)
        self.assertFalse(f.out.exists())
        unknown.unlink()
        before={p.relative_to(f.old).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in f.old.rglob('*') if p.is_file()}
        self.assertEqual(main(self.args),0)
        after={p.relative_to(f.old).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in f.old.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        omissions=read_json(f.out/'analysis-previous/unsealed-omissions.json')
        for filename in ('backend-result.json','response.txt','cwd/response-schema.json'):
            relative='agent-calls/005-analyze/'+filename
            self.assertIn(relative,omissions['paths'])
            self.assertFalse((f.out/'analysis-previous/technical-history'/relative).exists())
        rec=read_json(f.out/'technical-recovery.json')
        self.assertEqual(rec['calls'],['005-analyze','006-analysis-review','007-analysis-review'])
        self.assertEqual(read_json(f.out/'state.json')['agent_calls'],7)
        self.assertNotEqual(next(x for x in read_json(f.out/'stages.json')['stages'] if x['id']=='analysis')['status'],'completed')
        with patch('refiner.agent.Agent.ask',side_effect=AssertionError('no backend before integrity check')):
            file=f.out/'analysis-previous/technical-history/agent-calls/007-analysis-review/response.json'
            file.write_text('{}',encoding='utf8')
            self.assertEqual(main(['create','--resume-run',str(f.out)]+f.options),2)
    def test_tampered_source_call_cannot_be_imported(self):
        f=self.fixture
        path=f.old/'agent-calls/006-analysis-review/raw-response.txt'
        path.write_text('modified',encoding='utf8')
        self.assertEqual(main(self.args),2)
        self.assertFalse(f.out.exists())
    def test_invalid_response_requires_settled_execution_and_correct_roles(self):
        f=self.fixture
        call=f.old/'agent-calls/006-analysis-review'
        inputs=read_json(f.old/'inputs.json')
        request_path=call/'request.json'
        manifest_path=call/'request-manifest.json'
        execution_path=call/'execution.json'
        process_path=call/'process.execution.json'
        request, manifest, execution = [read_json(p) for p in (request_path, manifest_path, execution_path)]
        for mode in ('request_role','manifest_role','execution_role','execution_running','execution_unfinished',
                     'process_running','process_unfinished','process_nonzero'):
            with self.subTest(mode=mode):
                req, meta, exe = copy.deepcopy(request), copy.deepcopy(manifest), copy.deepcopy(execution)
                if mode=='request_role': req['role']='analyze'
                if mode=='manifest_role': meta['role']='analyze'
                if mode=='execution_role': exe['role']='analyze'
                if mode=='execution_running': exe['status']='running'
                if mode=='execution_unfinished': exe['finished_at']=None
                write_json(request_path,req)
                meta['request_sha256']=hashlib.sha256(json.dumps(req,ensure_ascii=False).encode('utf8')).hexdigest()
                write_json(manifest_path,meta);write_json(execution_path,exe)
                if mode.startswith('process_'):
                    process={'status':'completed','exit_code':0,'finished_at':'2026-10-31T01:00:00Z'}
                    if mode=='process_running': process['status']='running'
                    if mode=='process_unfinished': process['finished_at']=None
                    if mode=='process_nonzero': process['exit_code']=1
                    write_json(process_path,process)
                with self.assertRaises(RefinerError):
                    clarification_history(f.old,inputs,after_call=4)
                if process_path.exists(): process_path.unlink()
        write_json(request_path,request);write_json(manifest_path,manifest);write_json(execution_path,execution)
        pairs,calls=clarification_history(f.old,inputs,after_call=4)
        self.assertEqual(calls,['005-analyze','006-analysis-review','007-analysis-review'])
        self.assertEqual(len(pairs),1)
    def test_changed_controller_migration_preserves_derived_review_and_gap(self):
        f=self.fixture
        self.assertEqual(main(self.args),0)
        candidate=self.candidate
        scenario=next(row['id'] for row in candidate['scenarios']
                      if row['applicable'] and 'R1' in row['requirement_ids'])
        count=[]
        def fake_process(*args,**kwargs):
            request=json.loads(kwargs['input_text']);count.append(request)
            if len(count)==1:
                value={'schema_version':'skill-refiner.technical-triage/v1','assessments':[
                    {'question_id':'BQ-T','domain':'technical_evidence',
                     'reason':'核验执行装置是否能够独立观察已要求行为。',
                     'requirement_ids':['R1'],'scenario_ids':[scenario]}]}
            elif len(count)==2:
                self.assertEqual(request['role'], 'analyze')
                self.assertEqual(request['response_schema']['properties']['schema_version']['const'],
                                 'skill-refiner.analysis/v4')
                self.assertEqual(request['input']['previous_technical_clarification']['analysis'], candidate)
                self.assertEqual(request['input']['technical_coverage']['status'], 'coverage_gap')
                value=incomplete_v4(candidate, request['input']['standards'])
            elif len(count)==3:
                analysis=request['input']['analysis']
                value=analysis_fixture.AnalysisV3Tests.review(analysis)
                authority=analysis['target_obligations']['sources'][0]
                value.update(schema_version='skill-refiner.analysis-review/v4',revision_assessments=[],
                    standard_source_assessments=[{'source_id':authority['source_id'],
                        'content_sha256':authority['content_sha256'],'all_obligations_captured':False,
                        'subject_boundary_correct':False,'basis':'The synthetic inventory deliberately omits target obligations.'}],
                    obligation_assessments=[{'obligation_id':'O-INCOMPLETE','content_sha256':authority['content_sha256'],
                        'source_verified':True,'subject_applicability_justified':False,'criteria_atomic':True,
                        'mechanism_and_gaps_explicit':True,'scenario_coverage_complete':True,
                        'basis':'The incomplete source assessment must not be promoted to target acceptance.'}])
                value.update(approved=False,issues=['新增独立复核反馈必须继续携带'])
                value['context_scope_assessment']['basis']='已核对来源和修订：'+str(request['input'].get('revision_delta',{}))
            else:
                return 1,'','offline stop',0.01
            return 0,json.dumps(value,ensure_ascii=False),'',0.01
        with patch('refiner.agent.run_process',side_effect=fake_process):
            self.assertEqual(main(['create','--resume-run',str(f.out)]+f.options),2)
        self.assertEqual([r['role'] for r in count],['analysis-review','analyze','analysis-review','analyze'])
        old=f.out
        archive=f.base/'previous-controller'
        shutil.copytree(checkpoint_fixture.ROOT/'scripts',archive/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(checkpoint_fixture.ROOT/'schemas',archive/'schemas')
        shutil.copytree(checkpoint_fixture.ROOT/'references',archive/'references')
        marker=archive/'scripts/refiner/technical_recovery.py'
        marker.write_text(marker.read_text(encoding='utf8')+'\n# Synthetic previous-controller version.\n',encoding='utf8')
        contract_path=old/'creation-resume-contract.json'
        contract=read_json(contract_path)
        contract['controller_sha256']=checkpoint_fixture._controller_hash(archive)
        self.assertNotEqual(contract['controller_sha256'],checkpoint_fixture._controller_hash(checkpoint_fixture.ROOT))
        write_json(contract_path,contract)
        before={p.relative_to(old).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in old.rglob('*') if p.is_file()}
        next_run=f.base/'second-continuation'
        reconcile=f.base/'second-reconciliation.json'
        write_json(reconcile,{'query':f'processes for {old.name}','exit_code':0,'processes':[],
                   'checked_at':'2026-10-31T00:00:00Z','historical_evidence':'fixture process terminated',
                   'limitation':'offline fixture only'})
        second_options=[str(next_run) if x==str(old) else x for x in f.options]
        args=['import-create-inputs','--source-run',str(old),'--controller-archive',str(archive),
              '--process-reconciliation',str(reconcile),'--prior-seconds',str(read_json(old/'state.json')['elapsed_seconds']+1),
              '--prior-agent-calls','11',
              '--prior-evaluations','0','--carry-analysis-feedback','--carry-technical-clarification']+second_options
        self.assertEqual(main(args),0)
        self.assertEqual(before,{p.relative_to(old).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in old.rglob('*') if p.is_file()})
        self.assertTrue((next_run/'technical-coverage.json').is_file())
        self.assertEqual(read_json(next_run/'state.json')['agent_calls'],11)
        self.assertEqual(read_json(next_run/'analysis-previous/technical-history/agent-calls/005-analyze/response.json'),candidate)
        seen=[]
        def inspect(agent,role,instruction,payload,schema):
            seen.append((role,payload));raise RefinerError('offline stop after inspecting migration')
        with patch('refiner.agent.Agent.ask',inspect):
            self.assertEqual(main(['create','--resume-run',str(next_run)]+second_options),2)
        self.assertEqual(seen[0][0],'analyze')
        self.assertIn('新增独立复核反馈必须继续携带',seen[0][1]['validation_feedback'])
        self.assertEqual(len(seen[0][1]['previous_analysis_reviews']),3)

class MixedLegacyQuestionTests(unittest.TestCase):
    setUp = TechnicalRecoveryTests.setUp
    additional_questions = [
        {'id': 'BQ-B', 'question': '这项业务冲突应采用哪个结果？', 'decision_owner': '业务用户',
         'impact': '该业务决策会改变最终输出与验收期望。'},
        {'id': 'BQ-U', 'question': '这个边界条件的真实业务范围是什么？', 'decision_owner': '业务用户',
         'impact': '当前范围不确定，不能依据技术缺口自行移除。'}]
    additional_classifications = {'BQ-U': 'uncertain'}

    def test_technical_gap_does_not_remove_business_or_uncertain_questions(self):
        f = self.fixture
        self.assertEqual(main(self.args), 0)
        before = {p.relative_to(f.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in f.old.rglob('*') if p.is_file()}
        roles = []
        def process(*args, **kwargs):
            request = json.loads(kwargs['input_text'])
            roles.append(request['role'])
            if len(roles) == 1:
                scenario = next(s['id'] for s in self.candidate['scenarios'] if s['applicable'])
                value = {'schema_version': 'skill-refiner.technical-triage/v1', 'assessments': [
                    {'question_id': qid, 'domain': domain, 'reason': 'Respect the independently identified question domain.',
                     'requirement_ids': ['R1'], 'scenario_ids': [scenario]}
                    for qid, domain in [('BQ-T', 'technical_evidence'), ('BQ-B', 'business_decision'), ('BQ-U', 'uncertain')]]}
            else:
                self.assertEqual(request['role'], 'analyze', 'No full review/draft/target execution after dropped questions')
                self.assertEqual({q['id'] for q in request['input']['retained_legacy_questions']}, {'BQ-B', 'BQ-U'})
                self.assertIn('technical_coverage', request['input'])
                # Valid new schema, but an invalid semantic downgrade: both
                # material questions disappear merely because another gap exists.
                value = incomplete_v4(self.candidate, request['input']['standards'])
            return 0, json.dumps(value, ensure_ascii=False), '', .01
        with patch('refiner.agent.run_process', side_effect=process):
            self.assertEqual(main(['create', '--resume-run', str(f.out)] + f.options), 2)
        state = read_json(f.out / 'state.json')
        self.assertIn('must be retained', state['error'])
        self.assertEqual(roles, ['analysis-review', 'analyze', 'analyze'])
        self.assertEqual(state['agent_calls'], 10)
        self.assertGreaterEqual(state['elapsed_seconds'], 346.671)
        self.assertEqual(state['target_evaluations'], 0)
        self.assertFalse((f.out / 'drafts').exists())
        self.assertFalse((f.out / 'business.lock.json').exists())
        self.assertTrue((f.out / 'technical-coverage.json').is_file())
        self.assertEqual(before, {p.relative_to(f.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in f.old.rglob('*') if p.is_file()})


if __name__=='__main__': unittest.main()

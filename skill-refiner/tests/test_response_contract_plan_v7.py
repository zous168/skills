import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from refiner.checks import validate_plan,grade,grader_main
from refiner.common import PlanControlError,RefinerError,digest,write_json
from refiner.pipeline import verified_response_sources, response_contract_applicability, recovery_plan_revision, make_plan
from refiner.schemas import PLAN_V7
from refiner.artifact_validation import content_schema_id
from refiner.skillup import SkillUp
from refiner.gate import _verify_response_contract_results
from test_plan_contract import _minimal_plan, _approved_review


def sample():
    plan=_minimal_plan()
    plan['schema_version']='skill-refiner.plan/v7'
    schema={'type':'object','additionalProperties':False,
            'required':['priority','reason','next_action','need_clarification'],
            'properties':{'priority':{'enum':['P0','P1','P2','P3',None]},
                          'reason':{'type':'string'},'next_action':{'type':'string'},
                          'need_clarification':{'type':'boolean'}}}
    def val(priority):
        return json.dumps({'priority':priority,'reason':'ok','next_action':'next','need_clarification':False})
    for case in plan['cases']:
        case['response_contract']={'schema_json':json.dumps(schema),
                                  'source_refs':['requirements.md#R1'],'allow_markdown_fence':False}
        case['checks']=[{'id':'A-RESPONSE','type':'json_path_equal','pointer':'/priority','value':'P1'}]
        case['good_response']=val('P1')
        case['good_responses']=[]
        case['bad_response']=val('P2')
        case['positive_controls']=[{'id':'P-1','response':val('P1')}]
        case['negative_controls']=[{'id':'N-1','assertion_id':'A-RESPONSE',
                                    'response':val('P2'),'rationale':'The business priority is incorrect.'},
                                   {'id':'N-SCHEMA','assertion_id':'response_contract',
                                    'response':'{\"priority\":\"P1\"}',
                                    'rationale':'Required response fields are missing.'}]
    return plan


class PlanV7ResponseTests(unittest.TestCase):
    def setUp(self):
        self.source={'requirements.md#R1':hashlib.sha256(b'authorized requirement').hexdigest()}
        self.required=['dev-normal','dev-boundary']

    def test_controls_and_actual_grade_share_one_contract_hash(self):
        plan=sample()
        with tempfile.TemporaryDirectory() as root:
            evidence=validate_plan(plan,root,verified_response_sources=self.source, required_response_contract_cases=self.required)
            self.assertEqual(evidence['status'],'passed')
            for case,row in zip(plan['cases'],evidence['case_control_results']):
                valid=grade(case,case['good_response'],root)
                self.assertTrue(valid['passed'])
                runtime=valid['assertions'][-1]
                self.assertEqual(runtime['expected_condition']['contract_sha256'],row['response_contract_sha256'])
                self.assertFalse(grade(case,'{\"priority\":\"P1\"}',root)['passed'])
                bad=json.loads(case['good_response']);bad['priority']=9
                self.assertFalse(grade(case,json.dumps(bad),root)['passed'])

    def test_missing_or_invalid_source_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for value in (None,{}, {'requirements.md#R1':'not-a-sha'}):
                with self.subTest(value=value), self.assertRaises(RefinerError):
                    validate_plan(sample(),root,verified_response_sources=value, required_response_contract_cases=self.required)

    def test_semantic_negative_must_satisfy_contract_and_target(self):
        plan=sample()
        c=plan['cases'][0]
        c['negative_controls'][0]['response']='{\"priority\":9}'
        c['bad_response']='{\"priority\":9}'
        with tempfile.TemporaryDirectory() as root, self.assertRaises(PlanControlError):
            validate_plan(plan,root,verified_response_sources=self.source, required_response_contract_cases=self.required)

    def test_contract_negative_must_really_break_contract(self):
        plan=sample()
        plan['cases'][0]['negative_controls'][1]['response']=plan['cases'][0]['good_response']
        with tempfile.TemporaryDirectory() as root, self.assertRaises(RefinerError):
            validate_plan(plan,root,verified_response_sources=self.source, required_response_contract_cases=self.required)

    def test_contract_unsupported_fails_before_evaluation(self):
        plan=sample()
        plan['cases'][0]['response_contract']['schema_json']=json.dumps({'type':'string','pattern':'(a+)+$'})
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RefinerError,'unsupported or invalid'):
            validate_plan(plan,root,verified_response_sources=self.source, required_response_contract_cases=self.required)

    def test_nonjson_v7_and_legacy_v5_remain_unforced(self):
        plain=_minimal_plan()
        v7=copy.deepcopy(plain);v7['schema_version']='skill-refiner.plan/v7'
        for c in v7['cases']:c['response_contract']=None
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(validate_plan(plain,root)['status'],'passed')
            self.assertEqual(validate_plan(v7,root,required_response_contract_cases=[])['status'],'passed')
            self.assertTrue(grade(v7['cases'][0],v7['cases'][0]['good_response'],root)['passed'])

    def test_sources_come_from_verified_input_snapshot_not_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as root:
            work=Path(root)
            inputs={'requirements':[{'source':'requirements.md#R1','content':'exact accepted wording'}],
                    'files':[{'path':'schemas/v1/output.json','content':'old target evidence'}], 'standards':[]}
            write_json(work/'inputs.json',inputs)
            write_json(work/'resume-contract.json',{'inputs_sha256':digest(inputs)})
            mapping=verified_response_sources(work,inputs)
            self.assertEqual(mapping['requirements.md#R1'],hashlib.sha256(b'exact accepted wording').hexdigest())
            self.assertEqual(mapping['target-file:schemas/v1/output.json'],hashlib.sha256(b'old target evidence').hexdigest())
            altered=copy.deepcopy(inputs);altered['requirements'][0]['content']='changed'
            with self.assertRaises(RefinerError):verified_response_sources(work,altered)
            write_json(work/'inputs.json',altered)
            with self.assertRaises(RefinerError):verified_response_sources(work)

    def test_unsafe_snapshot_target_path_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            work=Path(root)
            inputs={'requirements':[],'files':[{'path':'../../outside','content':'untrusted'}]}
            write_json(work/'inputs.json',inputs)
            write_json(work/'resume-contract.json',{'inputs_sha256':digest(inputs)})
            with self.assertRaises(RefinerError):verified_response_sources(work)

    def test_v7_packaged_schema_matches_in_memory_and_detects_version(self):
        package=Path(__file__).resolve().parents[1]/'schemas/v7/plan.schema.json'
        self.assertEqual(json.loads(package.read_text(encoding='utf8')),PLAN_V7)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'test-plan.json';write_json(path,sample())
            self.assertEqual(content_schema_id(path),'skill-refiner.plan-v7')

    def test_actual_skillup_transport_applies_frozen_contract(self):
        exe=Path.home()/'.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe'
        if not exe.is_file():self.skipTest('Pinned local skill-up is unavailable')
        if os.name=='nt' and not Path('C:/Program Files/Git/bin/bash.exe').is_file():
            self.skipTest('Git Bash required for script judge')
        with tempfile.TemporaryDirectory(prefix='refiner-v7-transport-') as root:
            root=Path(root);target=root/'skill';target.mkdir();(target/'evals').mkdir()
            (target/'SKILL.md').write_text('---\nname: v7-fixture\ndescription: local test.\n---\nRespond.\n',encoding='utf8')
            cases=sample()['cases']
            cases[0]['prompt']='CASE_GOOD';cases[1]['prompt']='CASE_MISSING'
            stub=root/'stub.py'
            stub.write_text('import json,pathlib,sys\n'
                'r=pathlib.Path(sys.argv[1]).read_text(encoding=\"utf8\")\n'
                'v={\"priority\":\"P1\",\"reason\":\"ok\",\"next_action\":\"next\",\"need_clarification\":False}\n'
                'v={\"priority\":\"P1\"} if \"CASE_MISSING\" in r else v\n'
                'o={\"exit_code\":0,\"final_message\":json.dumps(v),\"transcript\":[],\"artifacts\":{\"files\":[]}}\n'
                'p=pathlib.Path(sys.argv[2]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(o),encoding=\"utf8\")\n',encoding='utf8')
            inp,out='$'+'{input_file}','$'+'{output_file}'
            engine={'name':'offline-v7','custom':{'transport':'local','response_format':'session_result',
                    'local':{'command':sys.executable,'args':[str(stub),inp,out],
                             'input_file':inp,'output_file':out}}}
            evaluator=SkillUp(str(exe),engine_config=engine,timeout=20,parallelism=1)
            if os.name=='nt':evaluator.env['PATH']='C:/Program Files/Git/bin'+os.pathsep+evaluator.env.get('PATH','')
            result=evaluator.evaluate(target,cases,root/'run')
            rows={row['id']:row for row in result['cases']}
            self.assertEqual(rows['dev-normal']['status'],'PASS',rows['dev-normal'])
            self.assertEqual(rows['dev-boundary']['status'],'FAIL',rows['dev-boundary'])

    def test_recovered_ids_and_every_changed_field_reach_independent_review(self):
        plan=_minimal_plan();old=copy.deepcopy(plan)
        old['cases'][0]['id']='DEV-NORMAL';old['cases'][1]['id']='DEV-BOUNDARY'
        old['cases'][0]['expected_outcome']='Earlier required output was different.'
        recovery={'previous_plan':old,'previous_invalid_response':json.dumps(old),
                  'contract_errors':[{'path':'/cases/0/id','constraint':'pattern'},
                                     {'path':'/cases/1/id','constraint':'pattern'}]}
        revision=recovery_plan_revision(recovery,plan)
        self.assertEqual(revision['case_id_mapping'],{'DEV-NORMAL':'dev-normal','DEV-BOUNDARY':'dev-boundary'})
        self.assertTrue(any(x['field']=='expected_outcome' for x in revision['changes']))
        with tempfile.TemporaryDirectory() as root:
            class AgentStub:
                def ask(self,role,instruction,payload,schema):
                    if role=='plan':return copy.deepcopy(plan)
                    self_revision=payload['recovery_revision']
                    review=_approved_review(plan)
                    review['recovery_assessments']=[{'change_id':change['id'],'preserves_requirements':True,
                                                     'reason':'Independently checked against original requirements.'}
                                                    for change in self_revision['changes']]
                    return review
            self.assertEqual(make_plan(AgentStub(),{},root,8,max_attempts=1,recovery=recovery),plan)
            record=json.loads((Path(root)/'plan-reviews/development.json').read_text(encoding='utf8'))
            self.assertEqual(record['recovery_revision'],revision)

    def test_recovery_rejects_unproven_or_ambiguous_id_mapping(self):
        plan=_minimal_plan();old=copy.deepcopy(plan);old['cases'][0]['id']='DEV-NORMAL'
        recovery={'previous_plan':old,'contract_errors':[]}
        with self.assertRaises(RefinerError):recovery_plan_revision(recovery,plan)
        old['cases'][1]['id']='dev-normal'
        recovery['contract_errors']=[{'path':'/cases/0/id','constraint':'pattern'}]
        with self.assertRaises(RefinerError):recovery_plan_revision(recovery,plan)

    def test_independent_recovery_review_cannot_approve_semantic_weakening(self):
        plan=_minimal_plan();old=copy.deepcopy(plan);old['cases'][0]['expected_outcome']='Earlier promised result.'
        recovery={'previous_plan':old,'previous_invalid_response':json.dumps(old),'contract_errors':[]}
        with tempfile.TemporaryDirectory() as root:
            class AgentStub:
                def ask(self,role,instruction,payload,schema):
                    if role=='plan':return copy.deepcopy(plan)
                    review=_approved_review(plan)
                    review['recovery_assessments']=[{'change_id':x['id'],'preserves_requirements':False,
                                                     'reason':'This change weakens a required outcome.'}
                                                    for x in payload['recovery_revision']['changes']]
                    return review
            with self.assertRaises(RefinerError):
                make_plan(AgentStub(),{},root,8,max_attempts=1,recovery=recovery)
            self.assertFalse((Path(root)/'plan-reviews/development.json').exists())

    def test_applicability_detects_global_json_and_blocks_all_null(self):
        plan=sample();inputs={'requirements':[{'source':'req','content':'请输出 JSON，返回机器字段。'}],'files':[]}
        applicability=response_contract_applicability(inputs,plan)
        self.assertEqual(set(applicability['required_case_ids']),{'dev-normal','dev-boundary'})
        for case in plan['cases']:case['response_contract']=None
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RefinerError,'contract missing'):
            validate_plan(plan,root,verified_response_sources=self.source,
                          required_response_contract_cases=applicability['required_case_ids'])

    def test_applicability_handles_mixed_and_internal_schema(self):
        plan=sample();plan['cases'][1]['kind']='error'
        inputs={'requirements':[{'source':'req','content':'正常情况输出 JSON；错误情况输出纯文本。'}],
                'files':[{'path':'schemas/v1/input.schema.json','content':'{}'}]}
        result=response_contract_applicability(inputs,plan)
        self.assertEqual(result['required_case_ids'],['dev-normal'])
        pure={'requirements':[{'source':'req','content':'输入使用内部 schema；最终输出纯文本。'}],
              'files':[{'path':'schemas/v1/input.schema.json','content':'{}'}]}
        self.assertEqual(response_contract_applicability(pure,plan)['required_case_ids'],[])
        uncertain={'requirements':[{'source':'req','content':'没有说明响应格式。'}],
                   'files':[{'path':'schemas/v1/unknown.schema.json','content':'{}'}]}
        with self.assertRaisesRegex(RefinerError,'role is not established'):
            response_contract_applicability(uncertain,plan)

    def test_json_error_field_does_not_restrict_global_contract_to_error_cases(self):
        inputs={'requirements':[{'source':'req','content':'输出 JSON，error 字段记录错误信息。'}],'files':[]}
        result=response_contract_applicability(inputs,sample())
        self.assertEqual(set(result['required_case_ids']),{'dev-normal','dev-boundary'})

    def test_proven_input_schema_and_internal_json_log_do_not_force_json_response(self):
        plan=sample()
        inputs={'requirements':[{'source':'req','content':'使用 schemas/v1/input.schema.json 验证输入。日志输出 JSON。'}],
                'files':[{'path':'schemas/v1/input.schema.json','content':'{}'}]}
        self.assertEqual(response_contract_applicability(inputs,plan)['required_case_ids'],[])

    def test_ambiguous_conditional_json_is_not_silently_ignored(self):
        inputs={'requirements':[{'source':'req','content':'只有用户明确提出时输出 JSON。'}],'files':[]}
        with self.assertRaisesRegex(RefinerError,'Conditional JSON'):
            response_contract_applicability(inputs,sample())

    def test_opt_in_plan_uses_v7_sources_controls_and_independent_review(self):
        plan=sample()
        context={'requirements':[{'source':'requirements.md#R1','content':'authorized requirement'}],
                 'files':[], 'standards':[], '_response_contract_v7':True}
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);write_json(root/'inputs.json',context)
            write_json(root/'resume-contract.json',{'inputs_sha256':digest(context)})
            class AgentStub:
                def ask(self,role,instruction,payload,schema):
                    if role=='plan':
                        self.assert_expected=payload['verified_response_source_ids']
                        assert schema['properties']['schema_version']['const']=='skill-refiner.plan/v7'
                        return copy.deepcopy(plan)
                    assert payload['control_results']['case_control_results']
                    return _approved_review(plan)
            agent=AgentStub()
            result=make_plan(agent,context,root,8,max_attempts=1)
            self.assertEqual(result,plan)
            self.assertEqual(agent.assert_expected,['requirements.md#R1'])
            summary=json.loads((root/'plan-controls/development.json').read_text(encoding='utf8'))
            self.assertEqual(summary['status'],'passed')
            self.assertEqual(summary['case_control_results'][0]['source_content_sha256']['requirements.md#R1'],
                             hashlib.sha256(b'authorized requirement').hexdigest())

    def test_final_gate_rechecks_contract_for_each_executor(self):
        plan=sample()
        def make_result():
            engines=[]
            for executor in ('first','second'):
                rows=[]
                for case in plan['cases']:
                    diagnostic=grade(case,case['good_response'],'.')
                    upstream={'case_id':case['id'],'status':'PASS','grading':{
                        'assertion_results':[{'evidence':json.dumps(diagnostic)}]}}
                    rows.append({'id':case['id'],'status':'PASS','passed':True,'evidence':upstream})
                engines.append({'executor':executor,'cases':rows})
            return {'engines':engines}
        evidence={'regression':make_result(),'acceptance':make_result()}
        plans={'development':plan,'acceptance':plan}
        _verify_response_contract_results(plans,evidence,['first','second'])
        row=evidence['acceptance']['engines'][1]['cases'][0]
        row['evidence']['grading']['assertion_results']=[]
        with self.assertRaisesRegex(RefinerError,'judge evidence'):
            _verify_response_contract_results(plans,evidence,['first','second'])

    def test_oracle_hash_mismatch_is_error(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'oracle.json'
            p.write_text(json.dumps(sample()['cases'][0]),encoding='utf8')
            output=io.StringIO()
            with patch.dict(os.environ,{'EVAL_FINAL_MESSAGE':'{}'}),redirect_stdout(output):
                code=grader_main(str(p),'0'*64,'dev-normal')
            self.assertEqual(code,2)
            self.assertEqual(json.loads(output.getvalue())['status'],'ERROR')

if __name__=='__main__':unittest.main()

import copy
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.checks import validate_plan
from refiner.common import PlanGateError, RefinerError, read_json, write_json
from refiner.gate import _verify_plan_control_artifacts, _verify_plan_review_artifacts
from refiner.pipeline import make_plan
from test_plan_contract import _minimal_plan, _approved_review

class AgentStub:
    def __init__(self, plans, omit_review=False): self.plans=iter(plans);self.omit=omit_review
    def ask(self,role,instruction,payload,schema):
        if role=='plan':return copy.deepcopy(next(self.plans))
        plan=payload['plan'];review=_approved_review(plan)
        changes=payload.get('execution_plan_revision',[])
        if changes and not self.omit:
            review['recovery_assessments']=[{'change_id':c['id'],'preserves_requirements':True,
                'reason':'The blocked recovery corrects the independently observed stop-policy error.'} for c in changes]
        return review

class PolicyRevisionTests(unittest.TestCase):
    def setUp(self):
        self.valid=_minimal_plan()
        self.invalid=copy.deepcopy(self.valid)
        self.invalid['execution_plan']['steps'][1]['failure_policy']['recovery_policy']='reuse_verified'
        self.step=self.invalid['execution_plan']['steps'][1]['id']
    def test_proven_recovery_policy_fix_is_recorded_and_independently_reviewed(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);accepted=make_plan(AgentStub([self.invalid,self.valid]),{},root,8,max_attempts=2)
            first=read_json(root/'plan-controls/attempts/development-01.json')
            paths=first['validation_error']['execution_paths']
            self.assertEqual(paths,[{'step_id':self.step,'field_path':'failure_policy.recovery_policy',
                                     'actual':'reuse_verified','expected':'blocked'}])
            second=read_json(root/'plan-controls/attempts/development-02.json')
            self.assertEqual(second['execution_plan_revision'][0]['step_id'],self.step)
            review=read_json(root/'plan-reviews/development.json')
            self.assertEqual(review['execution_plan_revision'],second['execution_plan_revision'])
            self.assertTrue(review['review']['recovery_assessments'][0]['preserves_requirements'])
            _verify_plan_control_artifacts(root,{'development':accepted})
            _verify_plan_review_artifacts(root,{'development':accepted})
    def test_other_step_and_other_policy_fields_cannot_change(self):
        for kind in ('other_step','same_step_other_field'):
            changed=copy.deepcopy(self.valid)
            if kind=='other_step':changed['execution_plan']['steps'][0]['goal']+=' Unrelated change.'
            else:changed['execution_plan']['steps'][1]['failure_policy']['terminal_state']='failed'
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(PlanGateError):make_plan(AgentStub([self.invalid,changed]),{},root,8,max_attempts=2)
    def test_full_and_tail_positive_lists_are_equivalent_but_never_skip_controls(self):
        for kind in ('tail','full'):
            plan=copy.deepcopy(self.valid);case=plan['cases'][0]
            if kind=='full':case['good_responses']=[c['response'] for c in case['positive_controls']]
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                result=validate_plan(plan,root)
                self.assertEqual(len(result['case_control_results'][0]['controls']),4)
        for kind in ('empty','omitted','extra','wrong_order'):
            plan=copy.deepcopy(self.valid);case=plan['cases'][0]
            values=[c['response'] for c in case['positive_controls']]
            case['good_responses']={'empty':[], 'omitted':values[1:2], 'extra':values+['extra'], 'wrong_order':values[::-1]}[kind]
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(RefinerError,'good_responses'):validate_plan(plan,root)
        plan=copy.deepcopy(self.valid);case=plan['cases'][0]
        case['positive_controls'][-1]['response']='A response missing the required token.'
        case['good_responses']=[c['response'] for c in case['positive_controls']]
        with tempfile.TemporaryDirectory() as root, self.assertRaises(RefinerError):validate_plan(plan,root)

    def test_declared_gap_adds_cases_and_steps_without_changing_existing(self):
        from test_plan_contract import _case
        from refiner.common import digest
        for version in ('v5','v7'):
            base=copy.deepcopy(self.valid);base['schema_version']='skill-refiner.plan/'+version
            if version=='v7':
                for case in base['cases']:case['response_contract']=None
            base['coverage_gaps']=['R1 needs an independently observable third scenario.']
            revised=copy.deepcopy(base);revised['coverage_gaps']=[]
            extra=_case('dev-extra','boundary','Operator uses another concrete boundary.',
                        'The additional boundary returns the specified observable.', 'Exercise a distinct boundary.')
            if version=='v7':extra['response_contract']=None
            revised['cases'].append(extra)
            step=copy.deepcopy(revised['execution_plan']['steps'][-1]);step['id']='STEP-EXTRA';step['order']=4
            step['case_ids']=['dev-extra'];step['depends_on']=['STEP-VERIFY'];step['execution_mode']='serial'
            step['outputs'][0]['path']='evidence/extra.json';step['agent']['write_scope']=['evidence/extra/']
            revised['execution_plan']['steps'].append(step)
            inputs={'requirements':[{'source':'requirements.md#R1','content':'Follow the stated request.'}],
                    'files':[],'standards':[],'_response_contract_v7':True} if version=='v7' else {}
            with self.subTest(version=version), tempfile.TemporaryDirectory() as root:
                root=Path(root)
                if inputs:write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs)})
                accepted=make_plan(AgentStub([base,revised]),inputs,root,8,max_attempts=2)
                source=read_json(root/'plan-controls/attempts/development-02.json')['revision_context']['feedback_sources']
                self.assertTrue(any(row['kind']=='coverage_gaps' for row in source))
                changes=read_json(root/'plan-reviews/development.json')['execution_plan_revision']
                self.assertTrue(any(row['id']=='execution-add:STEP-EXTRA' for row in changes))
                self.assertTrue(any(row['id'].startswith('coverage-gap:') for row in changes))
                if inputs:write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
                _verify_plan_control_artifacts(root,{'development':accepted})
                _verify_plan_review_artifacts(root,{'development':accepted})

    def test_removing_gap_without_independent_acceptance_stays_blocked(self):
        base=copy.deepcopy(self.valid);base['coverage_gaps']=['Required boundary has no executable case.']
        revised=copy.deepcopy(base);revised['coverage_gaps']=[]
        class Rejecting(AgentStub):
            def ask(self,role,instruction,payload,schema):
                value=super().ask(role,instruction,payload,schema)
                if role=='plan-review':
                    for row in value.get('recovery_assessments',[]):
                        if row['change_id'].startswith('coverage-gap:'):
                            row['preserves_requirements']=False
                return value
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(PlanGateError,'unsupported semantic change'):
                make_plan(Rejecting([base,revised]),{},root,8,max_attempts=2)

    def test_step_feedback_lists_all_identical_stop_policy_violations(self):
        from refiner.pipeline import _validation_error_diagnostic
        plan=copy.deepcopy(self.invalid)
        plan['execution_plan']['steps'][2]['failure_policy']['recovery_policy']='reuse_verified'
        error=RefinerError(f'Execution step {self.step} stops but does not record a blocked recovery policy')
        ids=[p['step_id'] for p in _validation_error_diagnostic(plan,error)['execution_paths']]
        self.assertEqual(ids,[self.step,plan['execution_plan']['steps'][2]['id']])

    def test_authenticated_history_feeds_complete_errors_and_requires_all_reviews(self):
        import hashlib,json
        from refiner.common import digest
        from test_plan_contract import _case
        original=copy.deepcopy(self.valid);original['schema_version']='skill-refiner.plan/v7'
        for case in original['cases']:case['response_contract']=None
        original['cases'][0]['good_response']='A missing token.'
        original['cases'][0]['positive_controls'][0]['response']='A missing token.'
        for step in original['execution_plan']['steps'][:2]:step['failure_policy']['recovery_policy']='reuse_verified'
        original['coverage_gaps']=['R1 requires another boundary case.']
        latest=copy.deepcopy(original)
        for step in latest['execution_plan']['steps'][:2]:step['failure_policy']['recovery_policy']='blocked'
        latest['execution_plan']['scope']['constraints'].append('Preserve all evidence when a step stops.')
        latest['coverage_gaps']=['R1 requires another boundary case.']
        final=copy.deepcopy(latest);final['coverage_gaps']=[]
        final['cases'][0]=copy.deepcopy(self.valid['cases'][0]);final['cases'][0]['response_contract']=None
        extra=_case('dev-extra','boundary','A further concrete boundary.', 'Another observable result.', 'Run a distinct boundary.')
        extra['response_contract']=None;final['cases'].append(extra)
        step=copy.deepcopy(final['execution_plan']['steps'][-1]);step['id']='STEP-EXTRA';step['order']=4
        step['case_ids']=['dev-extra'];step['depends_on']=['STEP-VERIFY'];step['outputs'][0]['path']='evidence/extra.json'
        step['agent']['write_scope']=['evidence/extra/'];final['execution_plan']['steps'].append(step)
        inputs={'requirements':[{'source':'requirements.md#R1','content':'Follow the stated request.'}],
                'files':[],'standards':[],'_response_contract_v7':True}
        records=[{'attempt':1,'plan_sha256':digest(original),'plan_snapshot':original,
                  'validation_error':{'message':f"Execution step {original['execution_plan']['steps'][0]['id']} stops but does not record a blocked recovery policy"}},
                 {'attempt':2,'plan_sha256':digest(latest),'plan_snapshot':latest,
                  'validation_error':{'message':'Historical error from previous checker'}}]
        recovery={'previous_plan':latest,'previous_invalid_response':json.dumps(latest),'contract_errors':[],
                  'control_records':records,'control_record_hashes':['a'*64,'b'*64],
                  'validation_feedback':['Original complete historical feedback.'],'usage':{}}
        class RecoveredAgent:
            def __init__(self):self.payload=None
            def ask(agent,role,instruction,payload,schema):
                if role=='plan':agent.payload=copy.deepcopy(payload);return copy.deepcopy(final)
                review=_approved_review(final)
                changes=payload['recovery_revision']['changes']+payload['execution_plan_revision']
                review['recovery_assessments']=[{'change_id':row['id'],'preserves_requirements':True,
                    'reason':'The source evidence and resulting test coverage were checked.'} for row in changes]
                return review
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);recovery['source_evidence_root']=str(root)
            write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',
                {'inputs_sha256':digest(inputs),'refine_plan_recovery_sha256':'c'*64})
            agent=RecoveredAgent();accepted=make_plan(agent,inputs,root,8,max_attempts=1,recovery=recovery)
            self.assertEqual(agent.payload['previous_plan'],latest)
            feedback='\n'.join(agent.payload['validation_feedback'])
            self.assertIn('Current full controls for recovered plan',feedback)
            self.assertIn('R1 requires another boundary case.',feedback)
            self.assertIn('Original complete historical feedback.',feedback)
            row=read_json(root/'plan-reviews/development.json')
            changes=row['execution_plan_revision']
            self.assertEqual(len([x for x in changes if x['id'].startswith('execution-policy:')]),2)
            self.assertTrue(any(x['id']=='execution-scope:constraints' for x in changes))
            self.assertTrue(any(x['id'].startswith('coverage-gap:') for x in changes))
            write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
            with patch('refiner.refine_plan_recovery.verify_recovery',return_value=recovery):
                _verify_plan_control_artifacts(root,{'development':accepted})
                _verify_plan_review_artifacts(root,{'development':accepted})

    def test_step_fix_requires_explicit_independent_assessment(self):
        class Rejected(AgentStub):
            def ask(self,role,instruction,payload,schema):
                result=super().ask(role,instruction,payload,schema)
                if role=='plan-review':
                    for item in result.get('recovery_assessments',[]):
                        item['preserves_requirements']=False
                return result
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(PlanGateError,'unsupported semantic change'):
                make_plan(Rejected([self.invalid,self.valid]),{},root,8,max_attempts=2)

if __name__=='__main__':unittest.main()

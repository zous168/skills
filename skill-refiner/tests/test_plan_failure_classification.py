"""Offline plan gate classification: source/transport errors are not plan failures."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.pipeline import make_plan
from refiner.common import Cancelled, PlanGateError, PlanValidationError, RefinerError
from test_plan_contract import _minimal_plan

class FixtureAgent:
    def __init__(self,plan):self.plan=plan;self.roles=[];self.calls=0
    def ask(self,role,*args,**kwargs):
        self.roles.append(role);self.calls+=1
        return copy.deepcopy(self.plan)

class FailureClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name);self.events=[]
        self.plan=_minimal_plan();self.plan['schema_version']='skill-refiner.plan/v7'
        for case in self.plan['cases']:case['response_contract']=None
        self.inputs={'requirements':[{'source':'req','content':'Return plain text.'}],
                     'files':[],'standards':[],'_response_contract_v7':True}
        self.bindings={'req':hashlib.sha256(b'Return plain text.').hexdigest()}
        self.agent=FixtureAgent(self.plan)
    def phase(self,name,status,**info):self.events.append((name,status,info))
    def run_plan(self):return make_plan(self.agent,self.inputs,self.work,8,max_attempts=3,phase_callback=self.phase)
    def first_source(self,*args,**kwargs):
        return (self.bindings,self.inputs) if kwargs.get('include_inputs') else self.bindings
    def assert_infrastructure_error(self,exc):
        self.assertNotIsInstance(exc,PlanGateError)
        self.assertEqual([x[:2] for x in self.events if x[0]=='plan_self_check'],
                         [('plan_self_check','running'),('plan_self_check','error')])
        self.assertFalse((self.work/'plan-controls/attempts/development-01.json').exists())
        self.assertEqual(self.agent.roles,['plan'])

    def test_initial_source_failure_stops_before_any_model_call(self):
        error=RefinerError('initial source unavailable')
        with patch('refiner.pipeline.verified_response_sources',side_effect=error):
            with self.assertRaises(RefinerError) as raised:self.run_plan()
        self.assertIs(raised.exception,error);self.assertEqual(self.agent.roles,[])
        self.assertEqual([x[:2] for x in self.events],[('plan_generation','error')])
        self.assertFalse((self.work/'plan-controls').exists())

    def test_real_frozen_source_read_permission_fault_after_generation(self):
        from refiner.common import digest
        (self.work/'inputs.json').write_text(json.dumps(self.inputs),encoding='utf8')
        (self.work/'resume-contract.json').write_text(json.dumps({'inputs_sha256':digest(self.inputs)}),encoding='utf8')
        from refiner.pipeline import verified_response_sources
        original=Path.lstat; hits=[]; calls=[]
        def verified(*args,**kwargs):
            calls.append(1)
            return verified_response_sources(*args,**kwargs)
        def denied(path,*args,**kwargs):
            if path==self.work/'inputs.json' and len(calls)==2:
                hits.append(path)
                raise PermissionError('simulated input access denied')
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',denied), patch('refiner.pipeline.verified_response_sources',side_effect=verified):
            with self.assertRaisesRegex(RefinerError,'simulated input access denied') as raised:self.run_plan()
        self.assertEqual(len(hits),1)
        self.assertIsInstance(raised.exception.__cause__,PermissionError)
        self.assert_infrastructure_error(raised.exception)

    def test_sources_permission_budget_and_cancel_after_generation(self):
        for error in (RefinerError('permission denied reading frozen input'),
                      RefinerError('budget exhausted'),PermissionError('access denied'),
                      Cancelled('cancelled by user')):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as temp:
                self.work=Path(temp);self.events=[];self.agent=FixtureAgent(self.plan)
                def verified(*args,**kwargs):
                    if kwargs.get('include_inputs'):return self.bindings,self.inputs
                    raise error
                with patch('refiner.pipeline.verified_response_sources',side_effect=verified), \
                     patch('refiner.pipeline.resolve_response_applicability',return_value={'required_case_ids':[]}):
                    with self.assertRaises(type(error)) as raised:self.run_plan()
                self.assertIs(raised.exception,error);self.assert_infrastructure_error(raised.exception)

    def test_applicability_budget_backend_permission_do_not_retry_or_record_structure(self):
        for message in ('Agent-call budget exhausted','backend unavailable','permission denied'):
            with self.subTest(message=message),tempfile.TemporaryDirectory() as temp:
                self.work=Path(temp);self.events=[];self.agent=FixtureAgent(self.plan)
                error=RefinerError(message)
                with patch('refiner.pipeline.verified_response_sources',side_effect=self.first_source), \
                     patch('refiner.pipeline.resolve_response_applicability',side_effect=error):
                    with self.assertRaises(RefinerError) as raised:self.run_plan()
                self.assertIs(raised.exception,error);self.assert_infrastructure_error(raised.exception)

    def test_already_consumed_applicability_call_is_not_refunded(self):
        def unavailable(agent,*args):
            agent.calls+=1
            raise RefinerError('backend unavailable after a call was spent')
        with patch('refiner.pipeline.verified_response_sources',side_effect=self.first_source), \
             patch('refiner.pipeline.resolve_response_applicability',side_effect=unavailable):
            with self.assertRaises(RefinerError) as raised:self.run_plan()
        self.assert_infrastructure_error(raised.exception);self.assertEqual(self.agent.calls,2)

    def test_source_failure_preserves_preexisting_history(self):
        path=self.work/'plan-controls/attempts/development-01.json';path.parent.mkdir(parents=True)
        path.write_text('{"status":"old-evidence"}',encoding='utf8')
        def verified(*args,**kwargs):
            if kwargs.get('include_inputs'):return self.bindings,self.inputs
            raise RefinerError('permission denied')
        with patch('refiner.pipeline.verified_response_sources',side_effect=verified), \
             patch('refiner.pipeline.resolve_response_applicability',return_value={'required_case_ids':[]}):
            with self.assertRaisesRegex(RefinerError,'permission denied'):self.run_plan()
        self.assertEqual(json.loads(path.read_text())['status'],'old-evidence')
        self.assertEqual(self.agent.roles,['plan'])
        self.assertEqual(self.events[-1][1],'error')

    def test_validator_oserror_does_not_record_plan_failure(self):
        error=PermissionError('cannot read case evidence')
        with patch('refiner.pipeline.verified_response_sources',side_effect=self.first_source), \
             patch('refiner.pipeline.resolve_response_applicability',return_value={'required_case_ids':[]}), \
             patch('refiner.pipeline.validate_plan',side_effect=error):
            with self.assertRaises(PermissionError) as raised:self.run_plan()
        self.assertIs(raised.exception,error);self.assert_infrastructure_error(raised.exception)

    def test_structure_error_is_recorded_and_auto_retried(self):
        error=RefinerError('Duplicate case ID: case-one')
        with patch('refiner.pipeline.verified_response_sources',side_effect=self.first_source), \
             patch('refiner.pipeline.resolve_response_applicability',return_value={'required_case_ids':[]}), \
             patch('refiner.pipeline.validate_plan',side_effect=error):
            with self.assertRaises(PlanGateError):self.run_plan()
        self.assertEqual(self.agent.roles,['plan','plan','plan'])
        for i in (1,2,3):
            record=json.loads((self.work/f'plan-controls/attempts/development-{i:02}.json').read_text())
            self.assertEqual(record['status'],'validation_error')
            self.assertEqual(record['validation_error']['message'],str(error))
        self.assertEqual(sum(x[1]=='failed' for x in self.events if x[0]=='plan_self_check'),3)

    def test_plan_control_failure_keeps_existing_diagnostics(self):
        evidence={'schema_version':'skill-refiner.plan-controls/v1','status':'failed',
                  'control_failures':[{'case_id':'case-one'}]}
        error=PlanValidationError('positive control failed',evidence)
        with patch('refiner.pipeline.verified_response_sources',side_effect=self.first_source), \
             patch('refiner.pipeline.resolve_response_applicability',return_value={'required_case_ids':[]}), \
             patch('refiner.pipeline.validate_plan',side_effect=error):
            with self.assertRaises(PlanGateError):self.run_plan()
        record=json.loads((self.work/'plan-controls/attempts/development-01.json').read_text())
        self.assertEqual(record['status'],'failed');self.assertEqual(record['control_failures'],evidence['control_failures'])
        self.assertEqual(self.agent.roles,['plan','plan','plan'])

if __name__=='__main__':unittest.main()

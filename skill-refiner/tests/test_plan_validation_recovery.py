"""Reproduce plan self-check errors without losing verifiable history."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.checks import validate_plan
from refiner.common import digest,read_json,write_json,RefinerError
from refiner.gate import _verify_plan_control_artifacts
from refiner.pipeline import make_plan
from test_plan_contract import _minimal_plan, _approved_review

class PlanningAgent:
    def __init__(self, responses, final): self.responses=iter(responses);self.final=final
    def ask(self,role,instruction,payload,schema):
        return copy.deepcopy(next(self.responses) if role=='plan' else _approved_review(self.final))

class ValidationRecoveryTests(unittest.TestCase):
    def test_legacy_control_failure_authorizes_only_its_case(self):
        plan=_minimal_plan();broken=copy.deepcopy(plan)
        broken['cases'][0]['good_response']='The input response lacks its required token.'
        broken['cases'][0]['positive_controls'][0]['response']=broken['cases'][0]['good_response']
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);accepted=make_plan(PlanningAgent([broken,plan],plan),{},root,8,max_attempts=2)
            _verify_plan_control_artifacts(root,{'development':accepted})
            altered=copy.deepcopy(accepted);altered['cases'][1]['prompt']+=' Unauthorized extra change.'
            controls=validate_plan(altered,root)
            path=root/'plan-controls/attempts/development-02.json';record=read_json(path)
            record.update(plan_snapshot=altered,plan_sha256=digest(altered),case_control_results=controls['case_control_results'])
            write_json(path,record);write_json(root/'plan-controls/development.json',record)
            with self.assertRaisesRegex(RefinerError,'semantic evidence differs'):
                _verify_plan_control_artifacts(root,{'development':altered})

    def test_duplicate_control_error_is_recorded_and_independently_replayed(self):
        plan=_minimal_plan();bad=copy.deepcopy(plan);bad['cases'][0]['negative_controls'][0]['id']='P-1'
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);accepted=make_plan(PlanningAgent([bad,plan],plan),{},root,8,max_attempts=2)
            attempt=read_json(root/'plan-controls/attempts/development-01.json')
            self.assertEqual(attempt['status'],'validation_error')
            self.assertEqual(attempt['validation_error']['case_ids'],['dev-normal'])
            _verify_plan_control_artifacts(root,{'development':accepted})
            attempt['validation_error']['case_ids']=['dev-boundary']
            write_json(root/'plan-controls/attempts/development-01.json',attempt)
            with self.assertRaisesRegex(RefinerError,'diagnostic differs'):
                _verify_plan_control_artifacts(root,{'development':accepted})

    def test_v7_duplicate_control_scope_is_bound_and_recoverable(self):
        plan=_minimal_plan();plan['schema_version']='skill-refiner.plan/v7'
        for case in plan['cases']:case['response_contract']=None
        bad=copy.deepcopy(plan);bad['cases'][0]['negative_controls'][0]['id']='P-1'
        inputs={'requirements':[{'source':'requirements.md#R1','content':'Follow the stated request.'}],
                'files':[],'standards':[],'_response_contract_v7':True}
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs)})
            accepted=make_plan(PlanningAgent([bad,plan],plan),inputs,root,8,max_attempts=2)
            second=read_json(root/'plan-controls/attempts/development-02.json')
            self.assertEqual(second['revision_context']['targeted_case_ids'],['dev-normal'])
            self.assertEqual(second['revision_context']['feedback_sources'][0]['kind'],'validation_error')
            write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
            _verify_plan_control_artifacts(root,{'development':accepted})

if __name__=='__main__':unittest.main()

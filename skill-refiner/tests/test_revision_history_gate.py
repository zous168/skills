"""Frozen test matrix and final gate history must agree."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import PlanGateError, RefinerError, read_json, write_json
from refiner.gate import _verify_plan_control_artifacts
from refiner.pipeline import make_plan
from test_plan_contract import _minimal_plan, _case, _approved_review

class MatrixRevisionTests(unittest.TestCase):
    def setUp(self):
        self.first=_minimal_plan()
        self.dropped=copy.deepcopy(self.first)
        self.dropped['cases'][1]=_case('dev-added','boundary','Operator uses a different case.',
                                      'The added case produces evidence.','Run another boundary path.')
        for step in self.dropped['execution_plan']['steps']:
            step['case_ids']=['dev-normal','dev-added']
    def agent(self, plans):
        class Stub:
            def __init__(self): self.plans=iter(plans);self.reviews=0
            def ask(self, role, instruction, payload, schema):
                if role=='plan':return copy.deepcopy(next(self.plans))
                self.reviews+=1
                review=_approved_review(self_case.first)
                if self.reviews==1:
                    review['approved']=False
                    review['issues']=['Revise dev-normal while preserving every case.']
                return review
        self_case=self
        return Stub()
    def test_rejected_missing_case_cannot_become_new_frozen_matrix(self):
        with tempfile.TemporaryDirectory() as root:
            agent=self.agent([self.first,self.dropped,self.dropped])
            with self.assertRaisesRegex(PlanGateError,'dropped_frozen_matrix_entries'):
                make_plan(agent,{},root,8,max_attempts=3)
            self.assertEqual(agent.reviews,1)
            self.assertEqual(read_json(Path(root)/'plan-controls/attempts/development-03.json')['status'],'failed')
    def test_corrected_matrix_passes_and_gate_accepts_failed_history(self):
        with tempfile.TemporaryDirectory() as root:
            agent=self.agent([self.first,self.dropped,self.first])
            plan=make_plan(agent,{},root,8,max_attempts=3)
            self.assertEqual({case['id'] for case in plan['cases']},{'dev-normal','dev-boundary'})
            second=read_json(Path(root)/'plan-controls/attempts/development-02.json')
            self.assertEqual(second['status'],'failed')
            self.assertTrue(second['revision_failures'])
            self.assertEqual(second['control_failures'],[])
            _verify_plan_control_artifacts(Path(root),{'development':plan})
            # A forged passing status must not erase the original frozen case.
            second_path=Path(root)/'plan-controls/attempts/development-02.json'
            forged=copy.deepcopy(second);forged['status']='passed';forged['revision_failures']=[]
            write_json(second_path,forged)
            write_json(Path(root)/'plan-controls/development.json',forged)
            with self.assertRaisesRegex(RefinerError,'lost a preserved'):
                _verify_plan_control_artifacts(Path(root),{'development':self.dropped})
            write_json(second_path,second)
            write_json(Path(root)/'plan-controls/development.json',read_json(Path(root)/'plan-controls/attempts/development-03.json'))
            last=Path(root)/'plan-controls/attempts/development-03.json'
            record=read_json(last);record['revision_failures']=[{'kind':'dropped_frozen_matrix_entries'}]
            write_json(last,record)
            with self.assertRaisesRegex(RefinerError,'evidence differs'):
                _verify_plan_control_artifacts(Path(root),{'development':plan})

    def test_invalid_first_review_cannot_forget_local_matrix(self):
        class Stub:
            def __init__(self): self.plans=0; self.reviews=0
            def ask(stub,role,instruction,payload,schema):
                if role=='plan':
                    stub.plans+=1
                    return copy.deepcopy(self.first if stub.plans==1 else self.dropped)
                stub.reviews+=1
                review=_approved_review(self.first)
                review['case_contracts'][1]['case_id']=review['case_contracts'][0]['case_id']
                return review
        with tempfile.TemporaryDirectory() as root:
            agent=Stub()
            with self.assertRaisesRegex(PlanGateError,'dropped_frozen_matrix_entries'):
                make_plan(agent,{},root,8,max_attempts=2)
            self.assertEqual(agent.reviews,1)
            self.assertTrue((Path(root)/'plan-reviews/invalid/development-01.json').is_file())

    def test_v7_structured_requirement_scope_supports_shared_issue(self):
        from refiner.common import digest
        plan=copy.deepcopy(self.first);plan['schema_version']='skill-refiner.plan/v7'
        for case in plan['cases']:case['response_contract']=None
        changed=copy.deepcopy(plan);changed['cases'][1]['prompt']+=' An independently scoped clarification.'
        inputs={'requirements':[{'source':'requirements.md#R1','content':'Follow the stated request.'}],
                'files':[], 'standards':[], '_response_contract_v7':True}
        class Stub:
            def __init__(self):self.plans=0;self.reviews=0
            def ask(stub,role,instruction,payload,schema):
                if role=='plan':
                    stub.plans+=1;return copy.deepcopy(plan if stub.plans==1 else changed)
                stub.reviews+=1
                review=_approved_review(plan if stub.reviews==1 else changed)
                if stub.reviews==1:
                    review['approved']=False
                    review['issues']=['共同要求需要澄清所有相关场景。']
                    review['revision_targets']=[{'issue_index':0,'case_ids':[],'requirement_ids':['R1'],
                        'scenario_ids':[],'scope_known':True,'basis':'要求 R1 同时覆盖这两个用例，需明确边界。'}]
                return review
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);write_json(root/'inputs.json',inputs)
            write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs)})
            accepted=make_plan(Stub(),inputs,root,8,max_attempts=2)
            self.assertEqual(accepted,changed)
            record=read_json(root/'plan-controls/attempts/development-02.json')
            self.assertEqual(record['revision_context']['targeted_case_ids'],['dev-boundary','dev-normal'])
            self.assertEqual(record['revision_context']['case_changes'][0]['case_id'],'dev-boundary')
            write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
            _verify_plan_control_artifacts(root,{'development':accepted})
            record['revision_context']['feedback_sources'][0]['case_ids']=['dev-normal']
            write_json(root/'plan-controls/attempts/development-02.json',record)
            with self.assertRaisesRegex(RefinerError,'feedback or complete differences'):
                _verify_plan_control_artifacts(root,{'development':accepted})

    def test_v7_uncertain_issue_scope_blocks_without_replanning(self):
        plan=copy.deepcopy(self.first);plan['schema_version']='skill-refiner.plan/v7'
        for case in plan['cases']:case['response_contract']=None
        inputs={'requirements':[{'source':'requirements.md#R1','content':'Follow the stated request.'}],
                'files':[], 'standards':[], '_response_contract_v7':True}
        from refiner.common import digest
        class Stub:
            def __init__(self):self.plans=0
            def ask(stub,role,instruction,payload,schema):
                if role=='plan':stub.plans+=1;return copy.deepcopy(plan)
                review=_approved_review(plan);review['approved']=False;review['issues']=['范围不清']
                review['revision_targets']=[{'issue_index':0,'case_ids':[],'requirement_ids':[],
                    'scenario_ids':[],'scope_known':False,'basis':'缺少依据，无法判断影响范围。'}]
                return review
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs)})
            agent=Stub()
            with self.assertRaisesRegex(PlanGateError,'scope'):
                make_plan(agent,inputs,root,8,max_attempts=3)
            self.assertEqual(agent.plans,1)

if __name__=='__main__':unittest.main()

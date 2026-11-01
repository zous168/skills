"""Historical v7 decisions remain plan/source-bound on Windows long paths."""
import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import RefinerError,digest,read_json,write_json
from refiner.pipeline import resolve_response_applicability,load_response_applicability,_recovery_feedback_material
from refiner.refine_plan_recovery import _io_path
from test_response_applicability_resolution import FakeAgent
from test_response_contract_plan_v7 import sample

@unittest.skipUnless(os.name=='nt','Windows extended path test')
class HistoricalDecisionLongPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp(prefix='refiner-long-'))
        self.addCleanup(lambda: shutil.rmtree(_io_path(self.tmp,force=True)))
        self.root=_io_path(self.tmp/('segment_a_'*7)/('segment_b_'*7)/('segment_c_'*7)/('segment_d_'*7),force=True)
        self.root.mkdir(parents=True)
        self.text='不得不返回 JSON 格式结果。'
        self.inputs={'requirements':[{'source':'requirements.md#R1','content':self.text}],'files':[]}
        self.bindings={'requirements.md#R1':hashlib.sha256(self.text.encode()).hexdigest()}
        self.second=sample();self.first=copy.deepcopy(self.second)
        self.first['execution_plan']['steps'][0]['failure_policy']['recovery_policy']='reuse_verified'
        self.step=self.first['execution_plan']['steps'][0]['id']
        self.response={'schema_version':'skill-refiner.response-applicability-analysis/v1',
                       'assessments':[{'case_id':c['id'],'decision':'required','reason':'该来源要求响应必须为 JSON。',
                                       'evidence':[{'source':'requirements.md#R1','quote':self.text}]} for c in self.first['cases']]}
        self.agent=FakeAgent([self.response,self.response,self.response,self.response],self.root)
        for plan in (self.first,self.second):
            resolve_response_applicability(self.agent,self.root,self.inputs,plan,self.bindings)
    def test_each_historical_plan_uses_its_own_bound_decision(self):
        for plan in (self.first,self.second):
            result=load_response_applicability(self.root,self.inputs,plan,self.bindings)
            self.assertEqual(set(result['required_case_ids']),{case['id'] for case in plan['cases']})
        first=self.root/'plan-controls/response-applicability'/(digest(self.first)+'.json')
        second=self.root/'plan-controls/response-applicability'/(digest(self.second)+'.json')
        write_json(first,read_json(second))
        with self.assertRaisesRegex(RefinerError,'not bound'):
            load_response_applicability(self.root,self.inputs,self.first,self.bindings)
    def test_recovery_replays_both_decisions_without_new_calls(self):
        records=[{'attempt':1,'plan_sha256':digest(self.first),'plan_snapshot':self.first,
                  'validation_error':{'message':f'Execution step {self.step} stops but does not record a blocked recovery policy'}},
                 {'attempt':2,'plan_sha256':digest(self.second),'plan_snapshot':self.second,'validation_error':{}}]
        recovery={'control_records':records,'control_record_hashes':['a'*64,'b'*64],
                  'source_evidence_root':str(self.root),'previous_plan':self.second,'validation_feedback':[]}
        count=len(self.agent.calls)
        material=_recovery_feedback_material(recovery,self.root,self.inputs,self.bindings)
        self.assertEqual(len(self.agent.calls),count)
        self.assertTrue(any(row['kind']=='validation_error' and row['execution_paths'][0]['step_id']==self.step
                            for row in material['sources']))
        self.assertEqual(material['reference']['previous_plan'],self.first)
        altered=copy.deepcopy(self.inputs);altered['requirements'][0]['content']+=' Changed.'
        with self.assertRaises(RefinerError):_recovery_feedback_material(recovery,self.root,altered,self.bindings)

if __name__=='__main__':unittest.main()

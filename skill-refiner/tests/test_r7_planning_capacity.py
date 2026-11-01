"""r7 amended capacity and future-test planning handoff; never invoke a model."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import RefinerError, PlanValidationError, read_json
from refiner.checks import validate_plan, grade
from test_plan_contract import _minimal_plan
from refiner.pipeline import _verified_planning_capacity, make_plan
from refiner.refine_plan_recovery import verify_recovery
RUN=Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3/refine-continuation-20261031-v7-r7')

@unittest.skipUnless((RUN/'agent-calls/022-plan/request.json').is_file(),'captured r7 unavailable')
class R7PlanningCapacityTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.resume=read_json(RUN/'resume-contract.json');cls.recovery=verify_recovery(RUN,cls.resume)
  cls.request=read_json(RUN/'agent-calls/022-plan/request.json')
  cls.capacity=_verified_planning_capacity(RUN,cls.recovery,24)
 def test_real_amendment_and_historic_feedback_are_distinct(self):
  self.assertEqual(self.capacity['old_limits'],{'development_cases':12,'max_evaluations':80})
  self.assertEqual(self.capacity['current_limits'],{'development_cases':24,'acceptance_cases':12,'max_evaluations':120})
  self.assertIn('superseded',self.capacity['interpretation'])
  self.assertIn('remaining coverage',self.capacity['interpretation'])
  self.assertIn('剩余三个名额','\n'.join(self.request['input']['validation_feedback']))
  self.assertIn('尚不能冻结', '\n'.join(self.request['input']['validation_feedback']))
 def test_amendment_tampering_wrong_limit_or_absent_history_fail(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   for f in ('resume-contract.json','refine-budget-amendment.json'):shutil.copyfile(RUN/f,root/f)
   self.assertEqual(_verified_planning_capacity(root,self.recovery,24),self.capacity)
   with self.assertRaisesRegex(RefinerError,'differs'):_verified_planning_capacity(root,self.recovery,12)
   bad=copy.deepcopy(self.recovery);bad['subsequent_controls']=[]
   with self.assertRaisesRegex(RefinerError,'absent'):_verified_planning_capacity(root,bad,24)
   with (root/'refine-budget-amendment.json').open('ab') as f:f.write(b' ')
   with self.assertRaisesRegex(RefinerError,'differs'):_verified_planning_capacity(root,self.recovery,24)
 def test_next_plan_receives_current_limits_and_preimplementation_guidance(self):
  class Capture(Exception):pass
  class Fake:
   def ask(self,role,instruction,payload,schema):self.role,self.instruction,self.payload=role,instruction,payload;raise Capture()
  fake=Fake(); prior=self.request['input']['previous_plan'];history=self.request['input']['validation_feedback']
  recovery={'previous_plan':prior,'previous_invalid_response':'historical draft','contract_errors':[], 'control_records':[{'status':'validation_error'}]}
  material={'feedback':history,'sources':[],'case_ids':[],'execution_revision':[],'reference':recovery}
  ctx={k:self.request['input'][k] for k in ('files','requirements','standards','_response_contract_v7')}
  with tempfile.TemporaryDirectory() as temp,patch('refiner.pipeline.verified_response_sources',return_value=({},ctx)),       patch('refiner.pipeline._recovery_feedback_material',return_value=material),       patch('refiner.pipeline._verified_planning_capacity',return_value=self.capacity):
   with self.assertRaises(Capture):make_plan(fake,ctx,temp,24,max_attempts=1,recovery=recovery)
  self.assertEqual(fake.payload['verified_planning_capacity'],self.capacity)
  self.assertEqual(fake.payload['validation_feedback'],history)
  self.assertIn('current_limits supersede the old capacity',fake.instruction)
  self.assertIn('Plan future required implementation before it exists',fake.instruction)
  self.assertIn('without claiming that it has been created, executed or passed',fake.instruction)
  self.assertIn('not approved frozen contracts',fake.instruction)

class FutureArtifactCompatibilityTests(unittest.TestCase):
 def plan(self):
  plan=_minimal_plan();plan['schema_version']='skill-refiner.plan/v7'
  for case in plan['cases']:case['response_contract']=None
  plan['cases'][0]['checks'].append({'id':'future-output','type':'file_exists','path':'reports/result.json','value':True})
  return plan
 def test_future_output_contract_plans_without_claiming_runtime_success(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'original').mkdir();plan=self.plan()
   self.assertFalse((root/'original/schemas/v1/result.schema.json').exists())
   self.assertFalse((root/'original/scripts/validate.py').exists())
   self.assertEqual(validate_plan(plan,root,8,verified_response_sources={},required_response_contract_cases=[])['status'],'passed')
   result=grade(plan['cases'][0],plan['cases'][0]['good_response'],root)
   self.assertFalse(next(a for a in result['assertions'] if a['assertion_id']=='future-output')['passed'])
 def test_future_script_cannot_be_misrepresented_as_existing_asset(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'original').mkdir();plan=self.plan()
   plan['cases'][0]['asset_audit'].update(status='ready',required_paths=['scripts/validate.py'],available_paths=['scripts/validate.py'])
   with self.assertRaises(PlanValidationError) as failure:
    validate_plan(plan,root,8,verified_response_sources={},required_response_contract_cases=[])
   self.assertIn('scripts/validate.py',str(failure.exception.evidence['coverage_failures']))

if __name__=='__main__':unittest.main()

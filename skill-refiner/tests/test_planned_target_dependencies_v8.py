import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
import jsonschema
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import RefinerError, digest, write_json
from refiner.schemas import PLAN_V7,PLAN_V8
from refiner.checks import validate_plan
from refiner.skillup import SkillUp
from refiner.pipeline import validate_plan_review, make_plan, _compact_planning_feedback
from refiner.gate import _verify_delivered_planned_dependencies, _verify_plan_control_artifacts, _verify_plan_review_artifacts
from test_plan_contract import _approved_review
from refiner.planned_dependencies import inspect_target_dependencies
from test_plan_contract import _minimal_plan

class PlannedTargetDependenciesTests(unittest.TestCase):
 def plan(self):
  p=_minimal_plan();p['schema_version']='skill-refiner.plan/v8'
  for case in p['cases']:
   case['response_contract']=None;case['planned_target_dependencies']=[]
  c=p['cases'][0];step=next(s for s in p['execution_plan']['steps'] if c['id'] in s['case_ids'])
  c['planned_target_dependencies']=[{'id':'D-VALIDATOR','target_path':'scripts/validate.py','asset_kind':'script',
   'requirement_ids':['R1'],'source_refs':['requirements.md#R1'],'implementation_step_id':step['id'],
   'execution_contract':{'runner':'docker-python-response','argv':[], 'expected_exit_code':0, 'expected_stdout':'OK\n', 'positive_control':{'input':'valid','expected_exit_code':0,'expected_stdout':'OK\n'}, 'negative_control':{'input':'invalid','expected_exit_code':1,'expected_stdout':'BAD\n'}},
   'input_contract':'A JSON result and a declared schema version.',
   'output_contract':'A status and independently recorded validation result.',
   'positive_scenario':{'stimulus':'Provide a conforming JSON result.','expected':'Validation records a passing result.','assertion_ids':['A-RESPONSE']},
   'negative_scenario':{'stimulus':'Provide an invalid JSON result.','expected':'Validation reports failure without acceptance.','assertion_ids':['A-RESPONSE']}}]
  return p
 def validate(self,p,root):
  return validate_plan(p,root,8,verified_response_sources={'requirements.md#R1':hashlib.sha256(b'requirement').hexdigest()},required_response_contract_cases=[])
 def test_v8_plans_missing_future_script_without_claiming_pass(self):
  p=self.plan()
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'original').mkdir()
   self.assertEqual(self.validate(p,root)['status'],'passed')
   self.assertEqual(inspect_target_dependencies(p['cases'][0],root/'original')[0]['status'],'not_ready')
   script=root/'original/scripts/validate.py';script.parent.mkdir();script.write_text('print(1)',encoding='utf8')
   r=inspect_target_dependencies(p['cases'][0],root/'original')[0]
   self.assertEqual(r['status'],'ready');self.assertEqual(r['sha256'],hashlib.sha256(script.read_bytes()).hexdigest())
 def test_unsafe_paths_unknown_sources_and_missing_links_fail_closed(self):
  for field,value in [('target_path','../escape'),('target_path','C:/Windows/a'),('target_path','scripts/../escape'),('target_path','scripts/CON'),('target_path','scripts/file.'),('source_refs',['unknown']),('implementation_step_id','unknown')]:
   p=self.plan();p['cases'][0]['planned_target_dependencies'][0][field]=value
   with tempfile.TemporaryDirectory() as tmp, self.subTest(field=field,value=value),self.assertRaises(RefinerError):self.validate(p,Path(tmp))
  p=self.plan();p['cases'][0]['planned_target_dependencies'][0]['positive_scenario']['assertion_ids']=['unknown']
  with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RefinerError):self.validate(p,Path(tmp))
 def test_schema_is_versioned_and_legacy_plan_unchanged(self):
  p=self.plan();jsonschema.validate(p,PLAN_V8)
  with self.assertRaises(jsonschema.ValidationError):jsonschema.validate(p,PLAN_V7)
  legacy=_minimal_plan();legacy['schema_version']='skill-refiner.plan/v7'
  for c in legacy['cases']:c['response_contract']=None
  jsonschema.validate(legacy,PLAN_V7)
  self.assertEqual(PLAN_V8,json.loads((ROOT/'schemas/v8/plan.schema.json').read_text(encoding='utf8')))
 def test_declared_schema_requires_matching_stage_contract_path(self):
  p=self.plan();d=p['cases'][0]['planned_target_dependencies'][0];d.update(asset_kind='schema',target_path='schemas/v1/other.schema.json')
  with tempfile.TemporaryDirectory() as tmp,self.assertRaisesRegex(RefinerError,'not declared'):self.validate(p,Path(tmp))
 def test_unsupported_execution_is_rejected_during_plan_validation(self):
  for kind in ('schema','reference','other'):
   p=self.plan();dep=p['cases'][0]['planned_target_dependencies'][0]
   dep['asset_kind']=kind
   if kind=='schema':
    dep['target_path']=p['stage_contracts']['schemas'][0]['path']
   with tempfile.TemporaryDirectory() as tmp, self.subTest(kind=kind),self.assertRaisesRegex(RefinerError,'unsupported'):
    self.validate(p,Path(tmp))
  for name in ('scripts/validator.js','scripts/validator.PY','scripts/validator'):
   p=self.plan();p['cases'][0]['planned_target_dependencies'][0]['target_path']=name
   with tempfile.TemporaryDirectory() as tmp, self.subTest(path=name),self.assertRaisesRegex(RefinerError,'unsupported'):
    self.validate(p,Path(tmp))
  p=self.plan();p['cases'][0]['planned_target_dependencies'][0]['execution_contract']['runner']='host-shell'
  with tempfile.TemporaryDirectory() as tmp,self.assertRaises((RefinerError,jsonschema.ValidationError)):
   self.validate(p,Path(tmp))
  with tempfile.TemporaryDirectory() as tmp:
   self.assertEqual(self.validate(self.plan(),Path(tmp))['status'],'passed')
 def test_missing_source_binding_is_not_assumed_empty(self):
  with tempfile.TemporaryDirectory() as tmp,self.assertRaises(RefinerError):
   validate_plan(self.plan(),Path(tmp),8,required_response_contract_cases=[])

class PlannedDependencyExecutionTests(PlannedTargetDependenciesTests):
 def test_missing_case_is_failure_without_model_and_ready_case_still_runs(self):
  p=self.plan(); ready=copy.deepcopy(p['cases'][1]);missing=copy.deepcopy(p['cases'][0])
  class Fake:
   engine={'name':'offline'};parallelism=1;case_progress_context={'suite':'baseline','round':0,'executor':'default'};evaluations=0;max_evaluations=10
   def _evaluate_locked(self,target,cases,run_dir,selected=None):
    self.evaluations+=len(cases)
    return {'passed':True,'cases':[{'id':c['id'],'status':'PASS','passed':True} for c in cases], 'seconds':0}
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);target=root/'target';target.mkdir();runner=Fake()
   result=SkillUp._evaluate_locked(runner,target,[missing,ready],root/'run')
   self.assertFalse(result['passed']);self.assertEqual(runner.evaluations,2)
   first,second=result['cases'];self.assertEqual((first['status'],first['evidence']['executed']),('FAIL',False))
   self.assertEqual(first['evidence']['planned_target_dependencies'][0]['status'],'not_ready')
   self.assertEqual(second['status'],'PASS')
 def test_all_missing_cases_do_not_invoke_executor(self):
  class Fake:
   engine={'name':'offline'};parallelism=1;case_progress_context=None;evaluations=0;max_evaluations=10
   def _evaluate_locked(self,*args,**kwargs):raise AssertionError('must not invoke executor')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);target=root/'target';target.mkdir();case=self.plan()['cases'][0]
   r=SkillUp._evaluate_locked(Fake(),target,[case],root/'run')
   self.assertEqual(r['cases'][0]['status'],'FAIL');self.assertFalse(r['cases'][0]['evidence']['executed'])


class V8GateTests(PlannedTargetDependenciesTests):
 def test_v8_inherits_required_response_contract(self):
  with tempfile.TemporaryDirectory() as tmp,self.assertRaisesRegex(RefinerError,'Required JSON response contract missing'):
   validate_plan(self.plan(),Path(tmp),8,verified_response_sources={'requirements.md#R1':hashlib.sha256(b'requirement').hexdigest()},required_response_contract_cases=['dev-normal'])
 def test_independent_review_must_assess_every_dependency_and_coverage(self):
  p=self.plan();review=_approved_review(p);review['revision_targets']=[]
  review['planned_dependency_coverage_complete']=True
  review['planned_dependency_assessments']=[{'case_id':'dev-normal','dependency_id':'D-VALIDATOR',
   'source_grounded':True,'interface_executable':True,'scenarios_discriminate':True,'implementation_link_valid':True,
   'basis':'The source and scenarios substantiate the executable contract.'}]
  validate_plan_review(review,p)
  for field in ('missing','duplicate','unfounded','uncovered'):
   bad=copy.deepcopy(review)
   if field=='missing':bad['planned_dependency_assessments']=[]
   if field=='duplicate':bad['planned_dependency_assessments']*=2
   if field=='unfounded':bad['planned_dependency_assessments'][0]['source_grounded']=False
   if field=='uncovered':bad['planned_dependency_coverage_complete']=False
   with self.subTest(field=field),self.assertRaises(RefinerError):validate_plan_review(bad,p)
 def test_final_gate_requires_actual_delivered_asset(self):
  p=self.plan()
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   with self.assertRaisesRegex(RefinerError,'not ready'):_verify_delivered_planned_dependencies(root,[p])
   file=root/'scripts/validate.py';file.parent.mkdir();file.write_text('pass',encoding='utf8')
   _verify_delivered_planned_dependencies(root,[p])
   # Presence only proves readiness, not a passed business assertion.
   self.assertEqual(inspect_target_dependencies(p['cases'][0],root)[0]['status'],'ready')


class V8PlanningEndToEndTests(PlannedTargetDependenciesTests):
 def test_frozen_plan_needs_review_then_gate_requires_real_asset(self):
  p=self.plan(); inputs={'requirements':[{'source':'requirements.md#R1','content':'Do the thing using the validation script.'}],
                         'files':[],'standards':[],'_response_contract_v7':True,'_planned_target_dependencies_v8':True}
  class Fake:
   def ask(self,role,instruction,payload,schema):
    if role=='plan':
     self.assert_schema=schema
     return copy.deepcopy(p)
    review=_approved_review(p);review['revision_targets']=[]
    review['planned_dependency_coverage_complete']=True
    review['planned_dependency_assessments']=[{'case_id':'dev-normal','dependency_id':'D-VALIDATOR',
     'source_grounded':True,'interface_executable':True,'scenarios_discriminate':True,'implementation_link_valid':True,
     'basis':'The requirement establishes a concrete testable interface.'}]
    return review
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs)})
   fake=Fake();plan=make_plan(fake,inputs,root,8,max_attempts=1);self.assertEqual(fake.assert_schema,PLAN_V8)
   write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
   _verify_plan_control_artifacts(root,{'development':plan});_verify_plan_review_artifacts(root,{'development':plan})
   final=root/'final';final.mkdir()
   with self.assertRaisesRegex(RefinerError,'not ready'):_verify_delivered_planned_dependencies(final,[plan])
   path=final/'scripts/validate.py';path.parent.mkdir();path.write_text('pass',encoding='utf8')
   _verify_delivered_planned_dependencies(final,[plan])
 def test_gap_fragment_roundtrip_and_token_collision_rejection(self):
  gap='必须覆盖这个长场景的独立正反例，不能把尚未运行的输出标记为通过。'*4
  feedback=['first: '+gap,'second: '+gap,'a different untouched diagnosis']
  rec={'subsequent_controls':[{'record':{'plan_snapshot':{'coverage_gaps':[gap]}}}]}
  compressed,record=_compact_planning_feedback(feedback,rec)
  self.assertTrue(record['fragments']);restored=list(compressed)
  for fragment in record['fragments']:
   self.assertEqual(hashlib.sha256(fragment['text'].encode()).hexdigest(),fragment['sha256'])
   restored=[value.replace(fragment['token'],fragment['text']) for value in restored]
  self.assertEqual(restored,feedback);self.assertEqual(digest(restored),record['feedback_sha256'])
  with self.assertRaisesRegex(RefinerError,'collides'):
   _compact_planning_feedback(feedback+['literal <REFINER-GAP-0> collision'],rec)

if __name__=='__main__':unittest.main()

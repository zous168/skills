import copy
import hashlib
import sys
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path
import jsonschema
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import RefinerError, digest, write_json, read_json
from refiner.requirement_revision import requirement_revision,review_schema,validate_assessments,unapproved_recovery
from test_plan_contract import _minimal_plan, _approved_review
from refiner.pipeline import make_plan
from refiner.gate import _verify_plan_control_artifacts, _verify_plan_review_artifacts

class RequirementRevisionTests(unittest.TestCase):
 def setUp(self):
  self.base=_minimal_plan();self.plan=copy.deepcopy(self.base)
  self.plan['requirements'][0]['acceptance_criteria'].append({'id':'R1.A2','statement':'Handle the stated boundary.','evidence':'Observe boundary output.'})
  self.inputs={'requirements':[{'source':'requirements.md','content':'Handle the stated boundary and return the requested result.'}],'standards':[]}
  self.delta=requirement_revision(self.base,self.plan)
  self.review={'approved':True,'requirement_revision_assessments':[{'change_id':x['change_id'],'previous_plan_sha256':self.delta['previous_plan_sha256'],
   'plan_sha256':self.delta['plan_sha256'],'preserves_prior':True,'supported_by_source':True,'reason':'The requirement explicitly includes the boundary.',
   'evidence':[{'source':'requirements.md','source_sha256':hashlib.sha256(self.inputs['requirements'][0]['content'].encode()).hexdigest(),'quote':'Handle the stated boundary'}]} for x in self.delta['changes']]}
 def test_grounded_addition(self):
  self.assertEqual(self.delta['changes'][0]['kind'],'criterion_added')
  schema=review_schema({'type':'object','required':['approved'],'properties':{'approved':{'type':'boolean'}}},self.delta)
  jsonschema.validate(self.review,schema);validate_assessments(self.review,self.delta,self.inputs)
 def test_missing_duplicate_wrong_hash_bad_quote_or_weakening_rejected(self):
  for kind in ('missing','duplicate','plan','source_hash','quote','weakening','unsupported'):
   r=copy.deepcopy(self.review);rows=r['requirement_revision_assessments']
   if kind=='missing':rows.clear()
   if kind=='duplicate':rows.append(copy.deepcopy(rows[0]))
   if kind=='plan':rows[0]['plan_sha256']='a'*64
   if kind=='source_hash':rows[0]['evidence'][0]['source_sha256']='a'*64
   if kind=='quote':rows[0]['evidence'][0]['quote']='invented'
   if kind=='weakening':rows[0]['preserves_prior']=False
   if kind=='unsupported':rows[0]['supported_by_source']=False
   with self.subTest(kind=kind),self.assertRaises(RefinerError):validate_assessments(r,self.delta,self.inputs)
 def test_prior_requirement_and_criteria_cannot_be_deleted_or_demoted(self):
  for kind in ('requirement','criterion','required'):
   p=copy.deepcopy(self.plan)
   if kind=='requirement':p['requirements']=[]
   if kind=='criterion':p['requirements'][0]['acceptance_criteria'].pop(0)
   if kind=='required':p['requirements'][0]['required']=False
   with self.subTest(kind=kind),self.assertRaises(RefinerError):requirement_revision(self.base,p)
 def test_eligibility_only_all_failed_authenticated_history(self):
  self.assertTrue(unapproved_recovery({'control_records':[{'status':'validation_error'},{'status':'invalid_coverage'}]}))
  self.assertFalse(unapproved_recovery({'control_records':[{'status':'passed'}]}))
  self.assertFalse(unapproved_recovery({'control_records':[]}))

class FullRecoveryReviewTests(unittest.TestCase):
 def test_added_criterion_review_and_tampered_decision(self):
  base=_minimal_plan();base['schema_version']='skill-refiner.plan/v7'
  for case in base['cases']:case['response_contract']=None
  plan=copy.deepcopy(base)
  plan['requirements'][0]['acceptance_criteria'].append({'id':'R1.A2','statement':'Handle the boundary path.','evidence':'Check the boundary output.'})
  plan['cases'][0]['acceptance_criteria_ids'].append('R1.A2')
  inputs={'requirements':[{'source':'requirements.md#R1','content':'Do the thing and handle the boundary path.'}], 'standards':[], 'files':[]}
  context={**inputs,'_response_contract_v7':True}
  recovery={'previous_plan':base,'control_records':[{'status':'validation_error','plan_snapshot':base}], 'previous_invalid_response':'x','contract_errors':[]}
  material={'feedback':[],'sources':[],'case_ids':['dev-normal'],'execution_revision':[],'reference':recovery}
  class Fake:
   def ask(self,role,instruction,payload,schema):
    if role=='plan':return copy.deepcopy(plan)
    review=_approved_review(plan)
    review['recovery_assessments']=[{'change_id':x['id'],'preserves_requirements':True,'reason':'Source-backed correction preserves all prior behavior.'} for x in payload['recovery_revision']['changes']]
    rev=payload['requirement_revision'];text=inputs['requirements'][0]['content']
    review['requirement_revision_assessments']=[{'change_id':x['change_id'],'previous_plan_sha256':rev['previous_plan_sha256'],
     'plan_sha256':rev['plan_sha256'],'preserves_prior':True,'supported_by_source':True,'reason':'The original requirement includes this boundary.',
     'evidence':[{'source':'requirements.md#R1','source_sha256':hashlib.sha256(text.encode()).hexdigest(),'quote':'handle the boundary path'}]} for x in rev['changes']]
    return review
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);write_json(root/'inputs.json',inputs);write_json(root/'resume-contract.json',{'inputs_sha256':digest(inputs),'refine_plan_recovery_sha256':'fixture'})
   with patch('refiner.pipeline._recovery_feedback_material',return_value=material),patch('refiner.refine_plan_recovery.verify_recovery',return_value=recovery):
    result=make_plan(Fake(),context,root,8,max_attempts=1,recovery=recovery)
    self.assertEqual(result,plan)
    write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
    _verify_plan_control_artifacts(root,{'development':result})
    _verify_plan_review_artifacts(root,{'development':result})
    review_path=root/'plan-reviews/development.json';review=read_json(review_path)
    review['review']['requirement_revision_assessments'][0]['evidence'][0]['quote']='fabricated'
    write_json(review_path,review)
    with self.assertRaisesRegex(RefinerError,'unverified normative evidence'):_verify_plan_review_artifacts(root,{'development':result})


class ReplayHistoryEncodingTests(unittest.TestCase):
 def test_projection_reconstructs_each_prior_requirement_and_execution_plan(self):
  from refiner.pipeline import _replay_history_bundle,_apply_history_patch
  base=_minimal_plan(); p1=copy.deepcopy(base);p2=copy.deepcopy(base);p3=copy.deepcopy(base)
  p1['requirements'][0]['behavior']='Original behavior with ~ slash / characters.'
  p2['requirements'][0]['behavior']='Revised behavior with preserved requirement.'
  p2['execution_plan']['steps'][0]['agent']['scope']='Second stage action / review ~ scope.'
  p2['execution_plan']['scope']['constraints']=['one new constraint']
  p3['requirements'][0]['acceptance_criteria'].append({'id':'R1.A2','statement':'Additional criterion.','evidence':'Actual evidence.'})
  p3['execution_plan']['steps'][0]['agent']['write_scope']=['safe/new/']
  plans=[p1,p2,p3];snapshot=copy.deepcopy(plans)
  recovery={'subsequent_controls':[{'record':{'plan_snapshot':p}} for p in plans]}
  data=_replay_history_bundle(recovery,p3)
  self.assertEqual(data['base_plan_sha256'],digest(p3));self.assertEqual(data['fields'],['requirements','execution_plan'])
  current={key:copy.deepcopy(p3[key]) for key in data['fields']}
  self.assertEqual(digest(current),data['base_projection_sha256'])
  by_hash={}
  for step in data['steps']:
   self.assertEqual(digest(current),step['from_projection_sha256'])
   current=_apply_history_patch(current,step['operations'])
   self.assertEqual(digest(current),step['to_projection_sha256'])
   by_hash[step['source_plan_sha256']]=current
  for plan in plans:
   self.assertEqual(by_hash[digest(plan)],{key:plan[key] for key in data['fields']})
  self.assertEqual(plans,snapshot)

 def test_revision_context_limit_preserves_original_error_not_checkpoint_error(self):
  from refiner.pipeline import make_plan
  base=_minimal_plan();base['schema_version']='skill-refiner.plan/v7'
  for case in base['cases']:case['response_contract']=None
  class Fake:
   def ask(self,role,instruction,payload,schema):return copy.deepcopy(base)
  inputs={'requirements':[],'files':[],'standards':[],'_response_contract_v7':True}
  with tempfile.TemporaryDirectory() as tmp:
   with patch('refiner.pipeline.verified_response_sources',return_value=({},inputs)), \
        patch('refiner.pipeline._compact_historical_revision_failures',side_effect=RefinerError('fixture unreachable')):
    # Oversized context can fail before a checkpoint; that error must not be
    # replaced by an invented missing-checkpoint diagnosis.
    recovery={'control_records':[{'status':'validation_error'}], 'previous_plan':base,
              'previous_invalid_response':'fixture', 'contract_errors':[]}
    material={'feedback':['x'*300001],'sources':[],'case_ids':[], 'execution_revision':[],
              'reference':{'previous_plan':base}}
    with patch('refiner.pipeline._recovery_feedback_material',return_value=material):
     with self.assertRaisesRegex(RefinerError,'revision context exceeds'):
      make_plan(Fake(),inputs,Path(tmp),8,max_attempts=2,recovery=recovery)

if __name__=='__main__':unittest.main()

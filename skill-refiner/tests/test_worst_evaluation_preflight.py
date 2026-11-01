import sys
import tempfile
import copy
from unittest.mock import patch
from contextlib import redirect_stderr
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from refiner.common import RefinerError
from refiner.preflight import (build_runtime_preflight,add_worst_case_evaluation_budget,
                               emit_runtime_preflight,REFINE_PLANNING_STEPS)
from refiner.pipeline import _existing_evaluation_count

class WorstEvaluationBudgetTests(unittest.TestCase):
 def report(self,limit,used=0):
  return build_runtime_preflight(mode='refine',phase='before-evaluation',
    limits={'max_seconds':1000,'max_agent_calls':100,'max_evaluations':limit,'max_cases':10,'acceptance_cases':10},
    usage={'target_evaluations':used},validated_reused_steps=REFINE_PLANNING_STEPS,
    applicable_scenario_count=2,executor_count=2,planned_case_counts={'development':4,'acceptance':3})
 def check(self,limit,used=0,native=2,rounds=2,reused=False):
  return add_worst_case_evaluation_budget(self.report(limit,used),development_cases=4,acceptance_cases=3,
    native_cases=native,executor_count=2,max_rounds=rounds,validated_baseline_reused=reused)
 def test_exact_worst_budget_includes_each_native_suite_and_acceptance_rerun(self):
  # baseline=12, acceptance=10, two full regression+acceptance rounds=44.
  report=self.check(66); self.assertEqual(report['status'],'passed')
  w=next(w for w in report['warnings'] if w['code']=='WORST_CASE_EVALUATION_BUDGET')['actual']
  self.assertEqual((w['baseline_cost'],w['initial_acceptance_cost'],w['each_repair_worst_cost'],w['required_remaining']),(12,10,22,66))
  report=self.check(65)
  issue=next(i for i in report['issues'] if i['code']=='INSUFFICIENT_WORST_CASE_EVALUATIONS')
  self.assertEqual((issue['expected'],issue['actual']),(66,65))
  with tempfile.TemporaryDirectory() as root,redirect_stdout(StringIO()):
   with self.assertRaisesRegex(RefinerError,'INSUFFICIENT_WORST_CASE_EVALUATIONS'):emit_runtime_preflight(report,root)
   self.assertTrue((Path(root)/'runtime-preflight.json').is_file())
 def test_consumed_budget_and_verified_baseline_are_not_double_counted(self):
  # 12 charged baseline plus 4 prior failed calls; only verified 12 credit.
  report=self.check(70,used=16,reused=True);w=next(x for x in report['warnings'] if x['code']=='WORST_CASE_EVALUATION_BUDGET')['actual']
  self.assertEqual((w['validated_baseline_credit'],w['required_remaining'],w['remaining_budget']),(12,54,54))
  self.assertEqual(report['status'],'passed')
  self.assertEqual(self.check(69,used=16,reused=True)['status'],'failed')
  self.assertEqual(self.check(70,used=16,reused=False)['status'],'failed')
  with self.assertRaisesRegex(ValueError,'credit exceeds'):self.check(100,used=11,reused=True)
 def test_no_repairs_still_counts_baseline_and_acceptance(self):
  w=next(x for x in self.check(14,native=0,rounds=0)['warnings'] if x['code']=='WORST_CASE_EVALUATION_BUDGET')['actual']
  self.assertEqual((w['required_remaining'],w['acceptance_cache_credit']),(14,0))
 def test_unknown_or_inconsistent_counts_are_not_zero(self):
  for args in ({'native_cases':None},{'native_cases':True},{'executor_count':0},{'max_rounds':-1},{'acceptance_cases':2}):
   options={'development_cases':4,'acceptance_cases':3,'native_cases':0,'executor_count':2,'max_rounds':0,'validated_baseline_reused':False};options.update(args)
   with self.subTest(args=args),self.assertRaises(ValueError):add_worst_case_evaluation_budget(self.report(100),**options)
 def test_native_count_reads_exact_list_and_rejects_unparseable_or_unsupported(self):
  with tempfile.TemporaryDirectory() as root:
   root=Path(root);self.assertEqual(_existing_evaluation_count(root),0)
   d=root/'evals';d.mkdir();(d/'eval.yaml').write_text('environment: {type: none}\ncases: {files: [a.yaml, b.yaml]}\n',encoding='utf8')
   for name in ('a.yaml','b.yaml'):(root/name).write_text('input: {prompt: Offline fixture}',encoding='utf8')
   self.assertEqual(_existing_evaluation_count(root),2)
   old_read=Path.read_text
   def denied(path,*args,**kwargs):
    if path==root/'b.yaml':raise PermissionError('access denied')
    return old_read(path,*args,**kwargs)
   with patch.object(Path,'read_text',denied):
    with self.assertRaisesRegex(RefinerError,'Cannot reserve'): _existing_evaluation_count(root)
   (root/'b.yaml').unlink()
   with self.assertRaisesRegex(RefinerError,'missing'): _existing_evaluation_count(root)
   (root/'b.yaml').write_text('input: [unterminated',encoding='utf8')
   with self.assertRaisesRegex(RefinerError,'Cannot reserve'): _existing_evaluation_count(root)
   (root/'b.yaml').write_text('input: {}',encoding='utf8')
   with self.assertRaisesRegex(RefinerError,'prompt or turns'): _existing_evaluation_count(root)
   (root/'b.yaml').write_bytes(b'\xff')
   with self.assertRaisesRegex(RefinerError,'Cannot reserve'): _existing_evaluation_count(root)
   (root/'b.yaml').write_text('input: {turns: [{prompt: turn}]}',encoding='utf8')
   self.assertEqual(_existing_evaluation_count(root),2)
   (d/'eval.yaml').write_text('cases: {files: []}',encoding='utf8')
   with self.assertRaisesRegex(RefinerError,'empty'): _existing_evaluation_count(root)
   (d/'eval.yaml').write_text('cases: {files: invalid}',encoding='utf8')
   with self.assertRaisesRegex(RefinerError,'Cannot reserve'): _existing_evaluation_count(root)
   (d/'eval.yaml').unlink();(d/'evals.json').write_text('{}',encoding='utf8')
   with self.assertRaisesRegex(RefinerError,'must first be imported'): _existing_evaluation_count(root)

class EvaluationPreflightOrderTests(unittest.TestCase):
 def run_fixture(self, root, *, bad_native=False):
  from refiner.cli import main
  from refiner.common import write_json,read_json
  from refiner.stages import StageManifest
  from refiner.matrix import Matrix
  from test_plan_contract import _minimal_plan
  plan=_minimal_plan();accept=copy.deepcopy(plan)
  for i,case in enumerate(accept['cases']):
   case['id']='acc-'+str(i);case['prompt']+=' distinct acceptance input'
  for step in accept['execution_plan']['steps']:step['case_ids']=['acc-0','acc-1']
  target=root/'target';target.mkdir()
  (target/'SKILL.md').write_text('---\nname: fixture\ndescription: offline\n---\nfixture',encoding='utf8')
  if bad_native:
   (target/'evals').mkdir()
   (target/'evals/eval.yaml').write_text('cases: {files: [evals/missing.yaml]}',encoding='utf8')
  profile={'backend':'command','command':['offline-do-not-run']}
  config=root/'config.json';write_json(config,{'schema_version':1,'agents':{'default':profile},
                                              'executors':[{'id':'offline',**profile}]})
  old_complete=StageManifest.complete
  def complete(self,stage,*args,**kwargs):
   if stage=='planning':return None
   return old_complete(self,stage,*args,**kwargs)
  def fake_plans(*args,**kwargs):
   for suite in ('development','acceptance'):
    for phase in ('plan_generation','plan_self_check','independent_review'):
     kwargs['phase_callback'](phase,'passed',suite=suite)
   return copy.deepcopy(plan),copy.deepcopy(accept)
  output=root/'run'
  with (patch('refiner.pipeline.make_plan_suites',side_effect=fake_plans),
        patch('refiner.gate._verify_plan_control_artifacts'),patch('refiner.gate._verify_plan_review_artifacts'),
        patch.object(StageManifest,'complete',complete),patch.object(Matrix,'evaluate') as evaluate,
        redirect_stdout(StringIO()),redirect_stderr(StringIO())):
   code=main(['run',str(target),'--output',str(output),'--config',str(config),
              '--skill-up',sys.executable,'--max-agent-calls','32','--max-seconds','3600',
              '--max-evaluations','11','--max-rounds','2'])
  self.assertEqual(code,2);evaluate.assert_not_called()
  state=read_json(output/'state.json');self.assertEqual(state['target_evaluations'],0)
  return output,state
 def test_worst_budget_failure_occurs_before_any_target_case(self):
  with tempfile.TemporaryDirectory() as temp:
   root,state=self.run_fixture(Path(temp))
   from refiner.common import read_json
   report=read_json(root/'runtime-preflight-before-evaluation.json')
   self.assertEqual(report['status'],'failed')
   self.assertIn('INSUFFICIENT_WORST_CASE_EVALUATIONS',[x['code'] for x in report['issues']])
   self.assertFalse((root/'baseline').exists())
 def test_missing_native_file_stops_before_generated_target_cases(self):
  with tempfile.TemporaryDirectory() as temp:
   root,state=self.run_fixture(Path(temp),bad_native=True)
   self.assertIn('native case file is missing',state['error'])
   self.assertFalse((root/'baseline').exists())

if __name__=='__main__':unittest.main()

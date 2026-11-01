import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from refiner.common import RefinerError
from refiner.gate import _verify_planned_dependency_results
from refiner.skillup import SkillUp
from refiner.probe_budget import ProbeBudget
from refiner.matrix import Matrix
from test_planned_target_dependencies_v8 import PlannedTargetDependenciesTests

class ValidatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / 'target'; self.target.mkdir()
        script = self.target / 'scripts' / 'validate.py'; script.parent.mkdir()
        script.write_text("import sys\ns=sys.stdin.read()\nprint('OK' if s=='valid' else 'BAD')\nsys.exit(0 if s=='valid' else 1)\n", encoding='utf8')
        self.plan = PlannedTargetDependenciesTests().plan()
        self.case = self.plan['cases'][0]
        self.ordinary = self.plan['cases'][1]
    def tearDown(self): self.tmp.cleanup()
    def run_cases(self, response):
        runner = SkillUp.__new__(SkillUp)
        runner.engine={'name':'fake'};runner.parallelism=1;runner.timeout=30;runner.deadline=None
        runner.evaluations=0;runner.max_evaluations=10;runner.case_progress_context=None;runner.executable='fake'
        runner.deadline=time.monotonic()+120
        config=ProbeBudget.configuration((self.plan,self.plan),1,3)
        runner.probe_budget=ProbeBudget(self.root,config,runner.deadline)
        self.budget=runner.probe_budget
        def fake_execute(config, run_dir, count, **kwargs):
            runner.evaluations += count
            return {'passed':True,'seconds':0,'cases':[
                {'id': self.case['id'], 'status':'PASS','passed':True,'evidence':{'response':response}},
                {'id': self.ordinary['id'], 'status':'PASS','passed':True,'evidence':{'response':'other'}}]}
        runner._execute=fake_execute
        with patch('refiner.skillup.run_process', return_value=(0,'','',0)), patch('refiner.skillup._verify_generated_judge',return_value=None):
            result=runner._evaluate_locked(self.target,[self.case,self.ordinary],self.root/'run')
        self.assertEqual(runner.evaluations,2)
        return result
    def test_runtime_and_final_gate_replay_actual_validator(self):
        result=self.run_cases('valid')
        self.assertTrue(result['passed'])
        self.assertEqual(result['validator_probe_calls'], 3)
        self.assertGreater(result['validator_probe_seconds'], 0)
        self.assertEqual([r['control'] for r in result['cases'][0]['planned_dependency_probes']], ['actual','positive','negative'])
        evidence={'regression':result,'acceptance':result}
        _verify_planned_dependency_results(self.target, {'development':self.plan,'acceptance':self.plan},evidence,[],probe_budget=self.budget,deadline=self.budget.deadline)
        corrupt=copy.deepcopy(evidence)
        corrupt['regression']['cases'][0]['planned_dependency_probes'][0]['probe']['sha256']='0'*64
        with self.assertRaises(RefinerError):
            _verify_planned_dependency_results(self.target, {'development':self.plan,'acceptance':self.plan},corrupt,[],probe_budget=self.budget,deadline=self.budget.deadline)
        changed=copy.deepcopy(evidence)
        changed['regression']['cases'][0]['evidence']['response']='invalid'
        with self.assertRaises(RefinerError):
            _verify_planned_dependency_results(self.target, {'development':self.plan,'acceptance':self.plan},changed,[],probe_budget=self.budget,deadline=self.budget.deadline)
        forged=copy.deepcopy(evidence)
        del forged['regression']['cases'][0]['planned_dependency_probes']
        with self.assertRaises(RefinerError):
            _verify_planned_dependency_results(self.target, {'development':self.plan,'acceptance':self.plan},forged,[],probe_budget=self.budget,deadline=self.budget.deadline)
        (self.target/'scripts'/'validate.py').write_text("print('OK')\n",encoding='utf8')
        with self.assertRaises(RefinerError):
            _verify_planned_dependency_results(self.target, {'development':self.plan,'acceptance':self.plan},evidence,[])
    def test_actual_bad_response_fails_but_other_case_runs(self):
        result=self.run_cases('invalid')
        self.assertEqual([r['status'] for r in result['cases']],['FAIL','PASS'])
        self.assertFalse(result['passed'])
    def test_matrix_shares_same_budget_with_executor(self):
        class Backend:
            evaluations=0
            def evaluate(child,target,cases,run_dir,selected=None):
                self.assertIs(child.probe_budget,self.budget)
                self.assertEqual(child.deadline,self.budget.deadline)
                child.evaluations += len(cases)
                return {'passed':True,'cases':[{'id':c['id'],'status':'PASS','passed':True} for c in cases]}
        self.budget=ProbeBudget(self.root,ProbeBudget.configuration((self.plan,self.plan),1,3),time.monotonic()+120)
        m=Matrix.__new__(Matrix);m.backends=[({'id':'test','backend':'skillup'},Backend())]
        m.probe_budget=self.budget;m.deadline=self.budget.deadline;m.max_evaluations=10;m.evaluations=0
        m.progress_callback=None;m.case_progress_context=None;m.work=self.root;m.engine={'name':'fake'}
        result=m.evaluate(self.target,[self.ordinary],self.root/'matrix',existing=False)
        self.assertTrue(result['passed']);self.assertEqual(m.evaluations,1)
    def test_unsupported_asset_does_not_claim_pass(self):
        self.case['planned_target_dependencies'][0]['asset_kind']='reference'
        result=self.run_cases('valid')
        self.assertEqual([r['status'] for r in result['cases']],['ERROR','PASS'])

if __name__ == '__main__':unittest.main()

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from refiner.common import RefinerError
from refiner.docker_target_probe import validate_case_dependencies
from refiner.probe_budget import ProbeBudget

class ProbeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.plan={'cases':[{'id':'case','planned_target_dependencies':[{'id':'d'}]}]}
        self.config=ProbeBudget.configuration((self.plan,self.plan),1,1)
        self.deadline=time.monotonic()+30
        self.budget=ProbeBudget(self.root,self.config,self.deadline)
    def tearDown(self): self.tmp.cleanup()
    def test_counts_survive_reopen_and_final_is_reserved(self):
        self.assertEqual(self.config['evaluation_limit'],12)
        self.assertEqual(self.config['final_limit'],12)
        self.budget.reserve('evaluation',12)
        reopened=ProbeBudget(self.root,self.config,self.deadline)
        with self.assertRaisesRegex(RefinerError,'budget exhausted'):reopened.reserve('evaluation',1)
        reopened.reserve('final',3)
        self.assertEqual(reopened._counts(),{'evaluation':12,'final':3})
        reopened.reserve('final',9)
        with self.assertRaisesRegex(RefinerError,'budget exhausted'):reopened.reserve('final',1)
    def test_zero_remaining_never_starts_docker(self):
        self.budget.reserve('evaluation',12)
        with patch('refiner.docker_target_probe.execute_python_probe') as execute:
            with self.assertRaisesRegex(RefinerError,'budget exhausted'):
                validate_case_dependencies(self.plan['cases'][0],self.root,'x',
                                           deadline=self.deadline,probe_budget=self.budget)
            execute.assert_not_called()
    def test_expired_deadline_does_not_reserve_or_run(self):
        with patch('refiner.docker_target_probe.execute_python_probe') as execute:
            with self.assertRaisesRegex(RefinerError,'deadline exhausted'):
                validate_case_dependencies(self.plan['cases'][0],self.root,'x',
                                           deadline=time.monotonic()-1,probe_budget=self.budget,phase='final')
            execute.assert_not_called()
        self.assertEqual(self.budget._counts(),{'evaluation':0,'final':0})
    def test_deleted_reservation_does_not_reset_budget(self):
        self.budget.reserve('evaluation',3)
        next(self.budget.directory.glob('reservation-*.json')).unlink()
        with self.assertRaisesRegex(RefinerError,'lost or rolled back'):
            ProbeBudget(self.root,self.config,self.deadline)
    def test_deleted_or_rolled_back_session_does_not_restore_time(self):
        with self.budget.final_session(30,1): time.sleep(0.025)
        path=next(self.budget.directory.glob('verify-session-*.json'))
        original=path.read_text(encoding='utf8')
        path.unlink()
        with self.assertRaisesRegex(RefinerError,'lost or rolled back'):
            with self.budget.final_session(30,1): pass
        path.write_text(original,encoding='utf8')
        import json
        row=json.loads(original);row['elapsed_after']=row['elapsed_before'];row['seconds']=0
        path.write_text(json.dumps(row),encoding='utf8')
        with self.assertRaisesRegex(RefinerError,'lost or rolled back'):
            with self.budget.final_session(30,1): pass
    def test_recorded_elapsed_is_not_charged_twice(self):
        self.budget.deadline=500.0
        with patch('refiner.probe_budget.time.monotonic',side_effect=[100,100,105,105,105,105]):
            with self.budget.final_session(20,0) as first:
                self.assertEqual(first,120)
            with self.budget.final_session(20,5) as second:
                self.assertEqual(second,120)
    def test_verified_create_resume_passes_parent_deadline(self):
        import refiner.creation as creation
        args=SimpleNamespace(resume_from=None,resume_run=str(self.root),description=None,spec=[],name=None,
            config=None,agent_command='fake',engine='fake',engine_config=None,output=None,standard=[],destination=None,
            timeout=10,skill_up='fake',parallelism=1,agent_parallelism=1,max_rounds=1,planning_attempts=2,
            max_cases=2,acceptance_cases=2,max_seconds=100,max_agent_calls=10,max_evaluations=10)
        options={'max_rounds':1,'planning_attempts':2,'max_cases':2,'acceptance_cases':2}
        limits={'max_seconds':100,'max_agent_calls':10,'max_evaluations':10,'max_rounds':1,
                'planning_attempts':2,'max_cases':2,'acceptance_cases':2}
        previous={'status':'created','verification_status':'verified','elapsed_seconds':90,'creation_agent_calls':0}
        contract={'schema_version':'skill-refiner.creation-resume/v1','controller_sha256':'fixed',
                  'inputs_sha256':'fixed','execution_sha256':'fixed','destination':None,'options':options,'limits':limits}
        (self.root/'state.json').write_text('{}',encoding='utf8')
        def read(path):
            return {'state.json':previous,'inputs.json':{},'creation-resume-contract.json':contract}[Path(path).name]
        class Agent:
            calls=0
            def __init__(self,*args,**kwargs):pass
            def _assert_no_unsettled_calls(self):pass
        usage={'elapsed_seconds':0,'agent_calls':0,'target_evaluations':0,'settled':True,'unresolved':[]}
        with patch.object(creation,'read_json',side_effect=read),patch.object(creation,'load_config',return_value=None), \
             patch.object(creation,'digest',return_value='fixed'),patch.object(creation,'Agent',Agent), \
             patch.object(creation,'_reconcile_child_budget',return_value=(previous,usage)), \
             patch('refiner.creation.time.monotonic',return_value=100),patch('refiner.gate.verify_run') as verify:
            self.assertEqual(creation.run(args),previous)
            verify.assert_called_once_with(self.root,deadline=110)
    def test_final_session_preserves_elapsed_and_unresolved_blocks(self):
        with self.budget.final_session(30,1) as deadline:
            self.assertGreater(deadline,time.monotonic())
        rows=list(self.budget.directory.glob('verify-session-*.json'))
        self.assertEqual(len(rows),1)
        self.assertGreaterEqual(self.budget._read(rows[0])['seconds'],0)
        with self.assertRaisesRegex(RefinerError,'deadline exhausted'):
            with self.budget.final_session(1,1):pass
        with self.assertRaisesRegex(RefinerError,'deadline exhausted'):
            self.budget.deadline=time.monotonic()-1
            with self.budget.final_session(30,1):pass
        self.budget.deadline=self.deadline
        (self.budget.directory/'verify-session-interrupted.json').write_text('{"status":"running","sequence":2}',encoding='utf8')
        with self.assertRaisesRegex(RefinerError,'requires reconciliation'):
            with self.budget.final_session(30,1):pass

if __name__ == '__main__':unittest.main()

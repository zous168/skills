import copy
import json
import tempfile
import shutil
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.pipeline import _authorized_execution_revision, _verified_planning_capacity, validate_plan_review
from refiner.checks import validate_execution_plan
from test_plan_contract import _approved_review
from refiner.common import RefinerError

RUN=Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3/refine-continuation-20261031-v7-r7/agent-calls/025-plan')

@unittest.skipUnless((RUN/'request.json').is_file() and (RUN/'response.json').is_file(), 'real r7 evidence unavailable')
class RecoveredExecutionExtensionTests(unittest.TestCase):
    def setUp(self):
        req=json.loads((RUN/'request.json').read_text(encoding='utf8'))['input']
        self.previous=req['previous_plan']
        self.candidate=json.loads((RUN/'response.json').read_text(encoding='utf8'))
        self.candidate['schema_version']='skill-refiner.plan/v8'
        for case in self.candidate['cases']: case['planned_target_dependencies']=[]
        gaps=self.candidate['coverage_gaps']
        self.sources=[{'kind':'historical_draft_diagnostics','origin':'recovery','record_sha256':'a'*64,'plan_sha256':'b'*64},
                      {'kind':'coverage_gaps','attempt':6,'gaps':copy.deepcopy(gaps)}]
        paths=[]
        for step in self.previous['execution_plan']['steps']:
            p=step['failure_policy']
            if p['action']=='stop' and p['recovery_policy']!='blocked':
                paths.append({'step_id':step['id'],'field_path':'failure_policy.recovery_policy',
                              'actual':p['recovery_policy'],'expected':'blocked'})
        self.sources.append({'kind':'validation_error','attempt':1,'execution_paths':paths})
        originals={step['id']:step for step in self.previous['execution_plan']['steps']}
        for step in self.candidate['execution_plan']['steps']:
            old=originals[step['id']]
            for key,value in old.items():
                if key not in {'case_ids','order','depends_on','failure_policy'}:
                    step[key]=copy.deepcopy(value)
        for cid,reqid,criterion in [('c10','R3','R3.A2'),('c11','R5','R5.A3'),('c12','R5','R5.A4')]:
            case=copy.deepcopy(self.candidate['cases'][1])
            case.update(id=cid,requirement_ids=[reqid],acceptance_criteria_ids=[criterion])
            self.candidate['cases'].append(case)
        steps=self.candidate['execution_plan']['steps']
        steps[0]['case_ids']+=['c10','c11','c12']
        steps[0]['actions'].append('为 c10、c11、c12 补充关联缺口的可执行测试。')
        final=steps.pop()
        final['case_ids']+=['c7','c8','c9','c10','c11','c12']
        final['actions'].append('汇总 c10、c11、c12 的逐项执行证据。')
        prior='E7'
        for pos,cid in enumerate(('c7','c8','c9','c10','c11','c12'),start=8):
            step=copy.deepcopy(steps[-1]);step['id']='N'+str(pos);step['order']=pos
            case=next(x for x in self.candidate['cases'] if x['id']==cid)
            step['case_ids']=[cid];step['requirement_ids']=case['requirement_ids']
            step['depends_on']=[prior];step['actions']=['独立测试 '+cid]
            step['outputs'][0]['path']='evidence/{phase}/'+cid+'/report.json'
            step['agent']['role']='tester-'+cid;step['agent']['write_scope']=['evidence/{phase}/'+cid+'/']
            steps.append(step);prior=step['id']
        final['order']=14;final['depends_on']=[prior];steps.append(final)
        self.candidate['coverage_gaps']=[]
    def test_real_unfrozen_matrix_can_be_extended_and_is_reviewable(self):
        changes=_authorized_execution_revision(self.previous,self.candidate,self.sources,unapproved_history=True)
        self.assertIsNotNone(changes)
        byid={row['id']:row for row in changes}
        for key in ('execution-step:E1:case_ids','execution-step:E1:actions',
                    'execution-step:E8:case_ids','execution-step:E8:actions',
                    'execution-step:E8:order','execution-step:E8:depends_on','execution-add:N8'):
            self.assertIn(key,byid)
            self.assertIn('before',byid[key]);self.assertIn('after',byid[key]);self.assertTrue(byid[key]['gap_bindings'])
        self.assertEqual(_authorized_execution_revision(self.previous,self.candidate,self.sources,unapproved_history=True),changes)
        validate_execution_plan(self.candidate['execution_plan'],self.candidate)
        review=_approved_review(self.candidate)
        review.update(revision_targets=[],planned_dependency_coverage_complete=True,planned_dependency_assessments=[],
                      recovery_assessments=[{'change_id':r['id'],'preserves_requirements':True,
                        'reason':'Each revision has source evidence and preserved requirements.'} for r in changes])
        validate_plan_review(review,self.candidate,execution_revision=changes)
        review['recovery_assessments'].pop()
        with self.assertRaisesRegex(RefinerError,'every revision change'):
            validate_plan_review(review,self.candidate,execution_revision=changes)
    def test_legacy_replay_and_unverified_sources_do_not_gain_authority(self):
        self.assertIsNone(_authorized_execution_revision(self.previous,self.candidate,self.sources))
        legacy=copy.deepcopy(self.candidate);legacy['schema_version']='skill-refiner.plan/v7'
        self.assertIsNone(_authorized_execution_revision(self.previous,legacy,self.sources,unapproved_history=True))
        self.assertIsNone(_authorized_execution_revision(self.previous,self.candidate,self.sources[1:],unapproved_history=True))
        self.assertIsNone(_authorized_execution_revision(self.previous,self.candidate,self.sources[:1]+self.sources[2:],unapproved_history=True))
        frozen=copy.deepcopy(self.previous);frozen['schema_version']='skill-refiner.plan/v8'
        self.assertIsNone(_authorized_execution_revision(frozen,self.candidate,self.sources,unapproved_history=True))
    def test_repeated_attempt_numbers_keep_distinct_gap_review_ids(self):
        sources=copy.deepcopy(self.sources)
        sources.append({'kind':'coverage_gaps','attempt':6,'gaps':['R3.A2 has a distinct additional failure.']})
        changes=_authorized_execution_revision(self.previous,self.candidate,sources,unapproved_history=True)
        self.assertIsNotNone(changes)
        ids=[row['id'] for row in changes]
        self.assertEqual(len(ids),len(set(ids)))
    def test_only_authenticated_numeric_capacity_substitution_is_reviewable(self):
        candidate=copy.deepcopy(self.candidate)
        actions=candidate['execution_plan']['steps'][0]['actions']
        index=next(i for i,x in enumerate(actions) if '十二例' in x)
        old=actions[index];actions[index]=old.replace('十二例','二十四例')
        capacity={'amendment_sha256':'c'*64,'source_control_sha256':'a'*64,'source_plan_sha256':'b'*64,
                  'old_limits':{'development_cases':12},'current_limits':{'development_cases':24}}
        self.assertIsNone(_authorized_execution_revision(self.previous,candidate,self.sources,unapproved_history=True))
        changes=_authorized_execution_revision(self.previous,candidate,self.sources,verified_capacity=capacity,unapproved_history=True)
        self.assertIsNotNone(changes)
        change=next(x for x in changes if x['id']=='execution-step:E1:actions')
        self.assertEqual(change['before'][index],old);self.assertEqual(change['after'][index],actions[index])
        self.assertEqual(change['capacity_amendment_sha256'],'c'*64)
        self.assertEqual(change['capacity_amendment_effect'],'supersedes_old_capacity_only')
        bad=copy.deepcopy(capacity);bad['amendment_sha256']='bad'
        self.assertIsNone(_authorized_execution_revision(self.previous,candidate,self.sources,verified_capacity=bad,unapproved_history=True))
        bad=copy.deepcopy(capacity);bad['source_control_sha256']='d'*64
        self.assertIsNone(_authorized_execution_revision(self.previous,candidate,self.sources,verified_capacity=bad,unapproved_history=True))
        tampered=copy.deepcopy(candidate);tampered['execution_plan']['steps'][0]['actions'][index]+=' 且不阻断'
        self.assertIsNone(_authorized_execution_revision(self.previous,tampered,self.sources,verified_capacity=capacity,unapproved_history=True))
        frozen=copy.deepcopy(self.previous);frozen['schema_version']='skill-refiner.plan/v8'
        self.assertIsNone(_authorized_execution_revision(frozen,candidate,self.sources,verified_capacity=capacity,unapproved_history=True))
    def test_capacity_marker_is_verified_against_real_amendment_and_source(self):
        root=RUN.parents[1]
        amendment=json.loads((root/'refine-budget-amendment.json').read_text(encoding='utf8'))
        record=next(json.loads(path.read_text(encoding='utf8')) for path in (root/'plan-controls/attempts').glob('development-*.json')
                    if json.loads(path.read_text(encoding='utf8')).get('plan_sha256')==amendment['source_plan_sha256'])
        recovery={'subsequent_controls':[{'sha256':amendment['source_control_sha256'],'record':record}]}
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            for name in ('resume-contract.json','refine-budget-amendment.json'):shutil.copyfile(root/name,work/name)
            capacity=_verified_planning_capacity(work,recovery,24)
            self.assertEqual((capacity['old_limits']['development_cases'],capacity['current_limits']['development_cases']),(12,24))
            contract=json.loads((work/'resume-contract.json').read_text(encoding='utf8'))
            contract['refine_budget_amendment_sha256']='0'*64
            (work/'resume-contract.json').write_text(json.dumps(contract),encoding='utf8')
            with self.assertRaisesRegex(RefinerError,'differs from the resume contract'):
                _verified_planning_capacity(work,recovery,24)
    def test_unrelated_or_weakened_changes_are_rejected(self):
        for change in ('remove_case','remove_requirement','remove_action','modify_input','weaken_security',
                       'unbound_action','unbound_case','drop_old_dependency','after_final','omit_execution'):
            p=copy.deepcopy(self.candidate);steps=p['execution_plan']['steps'];by={x['id']:x for x in steps}
            if change=='remove_case':p['cases']=[x for x in p['cases'] if x['id']!='c1']
            if change=='remove_requirement':p['requirements']=[x for x in p['requirements'] if x['id']!='R1']
            if change=='remove_action':by['E1']['actions'].pop(0)
            if change=='modify_input':by['E1']['inputs'].append('unverified input')
            if change=='weaken_security':by['E8']['agent']['write_scope'].append('outside/')
            if change=='unbound_action':by['E1']['actions'].append('更改无关设置')
            if change=='unbound_case':next(x for x in p['cases'] if x['id']=='c12')['acceptance_criteria_ids']=['R4.A3']
            if change=='drop_old_dependency':by['N8']['depends_on']=['E1']
            if change=='after_final':steps.append(steps.pop(-2))
            if change=='omit_execution':by['N13']['case_ids']=['c11']
            with self.subTest(change=change):
                self.assertIsNone(_authorized_execution_revision(self.previous,p,self.sources,unapproved_history=True))

if __name__=='__main__':unittest.main()

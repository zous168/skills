"""Latest rejected draft remains available without replacing stable baseline."""
import copy
import sys
import tempfile
import unittest
import jsonschema
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from refiner.common import PlanGateError,RefinerError,read_json,write_json
from refiner.gate import _verify_plan_control_artifacts,_verify_plan_review_artifacts
from refiner.pipeline import make_plan,validate_plan_review
from refiner.common import digest
from test_plan_contract import _minimal_plan,_case,_approved_review

class LatestDraftHandoffTests(unittest.TestCase):
    def setUp(self):
        self.base=_minimal_plan();self.base['coverage_gaps']=['R1 requires a distinct third scenario.']
        self.latest=copy.deepcopy(self.base)
        extra=_case('dev-extra','boundary','A different concrete boundary.','Observable additional result.','Exercise another boundary.')
        self.latest['cases'].append(extra)
        step=copy.deepcopy(self.latest['execution_plan']['steps'][-1]);step['id']='STEP-EXTRA';step['order']=4
        step['case_ids']=['dev-extra'];step['depends_on']=['STEP-VERIFY'];step['outputs'][0]['path']='evidence/extra.json'
        step['agent']['write_scope']=['evidence/extra/'];self.latest['execution_plan']['steps'].append(step)
        self.latest['requirements'][0]['behavior']='Unauthorized altered requirement'
        self.latest['execution_plan']['scope']['constraints'].append('Unauthorized scope edit')
        self.latest['cases'][0]['asset_audit']['source_refs']=[]
        self.final=copy.deepcopy(self.latest)
        self.final['requirements']=copy.deepcopy(self.base['requirements'])
        self.final['execution_plan']['scope']=copy.deepcopy(self.base['execution_plan']['scope'])
        self.final['cases'][0]=copy.deepcopy(self.base['cases'][0]);self.final['coverage_gaps']=[]
        self.final['cases'][2]['prompt']+=' Further justified detail.'
    def agent(self,third):
        outer=self
        class Fake:
            def __init__(self):self.n=0;self.third_payload=None;self.review_payload=None
            def ask(self,role,instruction,payload,schema):
                if role=='plan':
                    self.n+=1
                    if self.n==3:self.third_payload=copy.deepcopy(payload)
                    return copy.deepcopy([outer.base,outer.latest,third][self.n-1])
                self.review_payload=copy.deepcopy(payload)
                review=_approved_review(third)
                review['recovery_assessments']=[{'change_id':row['id'],'preserves_requirements':True,
                    'reason':'Reviewed the original requirement and observable test evidence.'}
                    for row in payload.get('execution_plan_revision',[])]
                return review
        return Fake()
    def test_full_latest_draft_and_all_failures_reach_next_request_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);agent=self.agent(self.final)
            final=make_plan(agent,{},root,8,max_attempts=3)
            self.assertEqual(agent.third_payload['previous_plan'],self.base)
            self.assertEqual(agent.third_payload['latest_unapproved_draft'],self.latest)
            self.assertEqual(agent.third_payload['provisional_case_ids'],['dev-boundary','dev-extra','dev-normal'])
            feedback='\n'.join(agent.third_payload['validation_feedback'])
            self.assertIn('asset_audit',feedback)
            self.assertIn('changed_frozen_requirement_contract',feedback)
            self.assertIn('changed_frozen_execution_plan',feedback)
            self.assertIn('R1 requires a distinct third scenario.',feedback)
            self.assertEqual(final['cases'][2]['id'],'dev-extra')
            _verify_plan_control_artifacts(root,{'development':final})
            _verify_plan_review_artifacts(root,{'development':final})
            path=root/'plan-controls/attempts/development-03.json';record=read_json(path)
            record['provisional_context']['case_ids'].remove('dev-extra');write_json(path,record)
            with self.assertRaisesRegex(RefinerError,'provisional draft handoff'):
                _verify_plan_control_artifacts(root,{'development':final})
    def test_legacy_revision_records_removed_candidate_for_review(self):
        dropped=copy.deepcopy(self.base);dropped['coverage_gaps']=[]
        agent=self.agent(dropped)
        with tempfile.TemporaryDirectory() as root:
            plan=make_plan(agent,{},root,8,max_attempts=3)
            self.assertEqual(plan,dropped)
            self.assertIn('dev-extra',agent.third_payload['provisional_case_ids'])
            self.assertEqual(agent.review_payload['provisional_revision']['removed_case_ids'],['dev-extra'])
            self.assertEqual(agent.review_payload['latest_unapproved_draft'],self.latest)
            _verify_plan_control_artifacts(Path(root),{'development':plan})

class ProvisionalDeletionReviewTests(unittest.TestCase):
    def setUp(self):
        self.plan=_minimal_plan();self.plan['schema_version']='skill-refiner.plan/v7'
        for case in self.plan['cases']:case['response_contract']=None
        self.review=_approved_review(self.plan)
        self.provisional={'removed_case_sources':[{'case_id':'extra','source_plan_sha256':'a'*64,
                                                  'case':{'id':'extra'}}]}
        self.sources={'requirements.md':'The requirement excludes the proposed duplicate scenario.'}
        self.review['provisional_removal_assessments']=[{'case_id':'extra','source_plan_sha256':'a'*64,
            'allow_removal':True,'reason':'The existing case already covers this required boundary.',
            'evidence':[{'source':'requirements.md','quote':'requirement excludes the proposed duplicate scenario'}]}]
    def validate(self,review):
        return validate_plan_review(review,self.plan,provisional=self.provisional,source_contents=self.sources)
    def test_justified_candidate_correction_passes(self):
        self.validate(self.review)
    def test_missing_duplicate_wrong_hash_or_rejected_decision_cannot_pass(self):
        for mutation in ('missing','duplicate','hash','denied','fabricated_evidence'):
            row=copy.deepcopy(self.review)
            if mutation=='missing':row['provisional_removal_assessments']=[]
            if mutation=='duplicate':row['provisional_removal_assessments']*=2
            if mutation=='hash':row['provisional_removal_assessments'][0]['source_plan_sha256']='b'*64
            if mutation=='denied':row['provisional_removal_assessments'][0]['allow_removal']=False
            if mutation=='fabricated_evidence':row['provisional_removal_assessments'][0]['evidence'][0]['quote']='invented'
            with self.subTest(mutation=mutation),self.assertRaises(RefinerError):self.validate(row)


class V7DeletionEndToEndTests(unittest.TestCase):
    def build(self, root):
        base=_minimal_plan();base['schema_version']='skill-refiner.plan/v7'
        base['coverage_gaps']=['A redundant candidate can be assessed against requirements.']
        for case in base['cases']:case['response_contract']=None
        latest=copy.deepcopy(base)
        extra=_case('dev-extra','boundary','A distinct candidate scenario.','Observable additional result.','Exercise another boundary fixture.')
        extra['response_contract']=None;latest['cases'].append(extra)
        final=copy.deepcopy(base);final['coverage_gaps']=[]
        context={'requirements':[{'source':'requirements.md#R1','content':'Return plain text. A redundant candidate may be removed.'}],
                 'files':[],'standards':[],'_response_contract_v7':True}
        write_json(root/'inputs.json',context);write_json(root/'resume-contract.json',{'inputs_sha256':digest(context)})
        write_json(root/'oracle.lock.json',{'response_applicability_sha256':{}})
        class Fake:
            def __init__(self):self.n=0
            def ask(self,role,instruction,payload,schema):
                if role=='plan':
                    self.n+=1;return copy.deepcopy([base,latest,final][self.n-1])
                review=_approved_review(final)
                if payload.get('recovery_revision') or payload.get('execution_plan_revision'):
                    review['recovery_assessments']=[{'change_id':x['id'],'preserves_requirements':True,
                        'reason':'Reviewed against the requirements and executable coverage.'}
                        for x in payload.get('execution_plan_revision',[])]
                review['provisional_removal_assessments']=[{'case_id':x['case_id'],
                    'source_plan_sha256':x['source_plan_sha256'],'allow_removal':True,
                    'reason':'Candidate is redundant and covered by retained cases.',
                    'evidence':[{'source':'requirements.md#R1','quote':'A redundant candidate may be removed.'}]}
                    for x in payload['provisional_revision']['removed_case_sources']]
                return review
        self.assertEqual(make_plan(Fake(),context,root,8,max_attempts=3),final)
        return final

    def test_deletion_review_goes_through_both_real_gates_and_rejects_forgery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);plan=self.build(root)
            _verify_plan_control_artifacts(root,{'development':plan})
            _verify_plan_review_artifacts(root,{'development':plan})
            attempt=root/'plan-reviews/attempts/development-03.json'
            summary=root/'plan-reviews/development.json'
            original=read_json(attempt)
            for mutation in ('missing','duplicate','wrong_hash','denied','wrong_source','invented_quote'):
                with self.subTest(mutation=mutation):
                    changed=copy.deepcopy(original);rows=changed['review']['provisional_removal_assessments']
                    if mutation=='missing':rows.clear()
                    if mutation=='duplicate':rows.append(copy.deepcopy(rows[0]))
                    if mutation=='wrong_hash':rows[0]['source_plan_sha256']='b'*64
                    if mutation=='denied':rows[0]['allow_removal']=False
                    if mutation=='wrong_source':rows[0]['evidence'][0]['source']='fabricated.md'
                    if mutation=='invented_quote':rows[0]['evidence'][0]['quote']='invented assertion'
                    write_json(attempt,changed);write_json(summary,changed)
                    with self.assertRaises((RefinerError,jsonschema.ValidationError)):_verify_plan_control_artifacts(root,{'development':plan})
                    with self.assertRaises((RefinerError,jsonschema.ValidationError)):_verify_plan_review_artifacts(root,{'development':plan})
            write_json(attempt,original);write_json(summary,original)
            _verify_plan_control_artifacts(root,{'development':plan})

    def test_older_v7_prefix_without_context_is_accepted_but_removal_never_silently_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);plan=self.build(root)
            for number in (1,2):
                path=root/f'plan-controls/attempts/development-{number:02}.json'
                record=read_json(path);record.pop('provisional_context');write_json(path,record)
            _verify_plan_control_artifacts(root,{'development':plan})
            last=root/'plan-controls/attempts/development-03.json';record=read_json(last)
            record.pop('provisional_context');write_json(last,record)
            with self.assertRaisesRegex(RefinerError,'provisional draft handoff missing'):
                _verify_plan_control_artifacts(root,{'development':plan})

    def test_new_context_cannot_disappear_on_later_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);plan=self.build(root)
            path=root/'plan-controls/attempts/development-02.json'
            record=read_json(path);record.pop('provisional_context');write_json(path,record)
            with self.assertRaisesRegex(RefinerError,'provisional draft handoff missing'):
                _verify_plan_control_artifacts(root,{'development':plan})


class InterruptedReplayBudgetTests(unittest.TestCase):
    def test_real_009_and_012_replay_do_not_exhaust_one_new_revision(self):
        from test_r3_interrupted_draft_recovery import SOURCE,ARCHIVE,SNAPSHOT,ANCESTOR
        from refiner.refine_plan_recovery import migrate,verify_recovery,_io_path,_chain_tree
        import shutil
        if not all(p.exists() for p in (SOURCE,ARCHIVE,SNAPSHOT,ANCESTOR)):
            self.skipTest('captured r3 replay evidence unavailable')
        class NewPlanRequested(Exception):pass
        class Fake:
            def __init__(self):self.roles=[]
            def ask(self,role,*args,**kwargs):
                self.roles.append(role)
                raise NewPlanRequested('one new revision is available')
        before=_chain_tree(SOURCE)
        with tempfile.TemporaryDirectory(prefix='refiner-replay-budget-') as tmp:
            target=Path(tmp)/'run'
            migrate(SOURCE,ARCHIVE,ANCESTOR,target,source_snapshot=SNAPSHOT)
            try:
                recovery=verify_recovery(target,read_json(target/'resume-contract.json'))
                self.assertEqual(recovery['pending_plan_call'],'012-plan')
                context=read_json(target/'inputs.json');context['_response_contract_v7']=True
                agent=Fake()
                def applicability(a,w,i,plan,b):
                    return {'required_case_ids':[case['id'] for case in plan['cases'] if case.get('response_contract') is not None]}
                with patch('refiner.pipeline.resolve_response_applicability',side_effect=applicability):
                    with self.assertRaisesRegex(NewPlanRequested,'new revision is available'):
                        make_plan(agent,context,target,24,max_attempts=1,recovery=recovery)
                self.assertEqual(agent.roles,['plan'])
                self.assertEqual(sorted(x.name for x in (target/'plan-controls/attempts').glob('*.json')),
                                 ['development-01.json','development-02.json'])
                self.assertEqual(read_json(target/'plan-controls/attempts/development-01.json')['plan_snapshot'],
                                 recovery['interrupted_control_record']['plan_snapshot'])
                self.assertEqual(read_json(target/'plan-controls/attempts/development-02.json')['plan_snapshot'],
                                 recovery['pending_plan'])
            finally:shutil.rmtree(_io_path(target,force=True))
        self.assertEqual(before,_chain_tree(SOURCE))

if __name__=='__main__':unittest.main()

import copy, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from refiner.common import RefinerError
from refiner.creation import analysis_revision_delta, validate_analysis_revision_review, validate_analysis_review
from refiner.artifact_validation import schema_ref, validate_value
import test_analysis_contract_v3 as fixture

class RevisionSemanticCoverageTests(unittest.TestCase):
    def setUp(self):
        self.previous=fixture.AnalysisV3Tests('runTest').analysis()
        self.revised=copy.deepcopy(self.previous)
        self.sources=[{'id':'business_description','content':'用户要求所有工单保持原有优先级判定规则。'}]
    def review(self,delta,*,outcome='equivalent'):
        review=fixture.AnalysisV3Tests.review(self.revised)
        review.update(schema_version='skill-refiner.analysis-review/v3',revision_assessments=[])
        requirement=next(row['id'] for row in self.revised['requirements'] if row['required'])
        scenario=next(row['id'] for row in self.revised['scenarios'] if row['applicable'])
        for change in delta['changes']:
            ref={'kind':'requirement','id':requirement} if change['kind']=='requirement' else {'kind':'scenario','id':scenario}
            review['revision_assessments'].append({'kind':change['kind'],'id':change['id'],
                'before_sha256':change['before_sha256'],'after_sha256':change['after_sha256'],
                'outcome':outcome,'reason':'逐项检查旧覆盖与新映射，并对照原始来源。',
                'replacement_refs':[ref],
                'source_evidence':[{'source_id':'business_description','quote':'优先级判定规则'}]})
        return review
    def test_same_id_changed_meaning_and_criteria_are_independently_reviewed(self):
        self.revised['requirements'][0]['behavior']='只处理部分工单并忽略其他情况。'
        self.revised['requirements'][0]['acceptance_criteria'][0]['statement']='只验证单个案例的结果。'
        self.revised['scenarios'][0]['detail']['expected_observables']=['错误地替换为另一项结果']
        delta=analysis_revision_delta(self.previous,self.revised)
        self.assertIn(self.previous['requirements'][0]['id'],delta['requirements_changed'])
        self.assertIn(self.previous['scenarios'][0]['id'],delta['scenarios_changed'])
        validate_value(delta,schema_ref('skill-refiner.analysis-revision-delta-v2'))
        req=next(row for row in delta['changes'] if row['kind']=='requirement')
        self.assertIn('/behavior',req['changed_paths'])
        self.assertIn('/acceptance_criteria/0/statement',req['changed_paths'])
        review=self.review(delta)
        review['revision_assessments'].pop()
        with self.assertRaisesRegex(RefinerError,'every changed ID'):validate_analysis_revision_review(review,delta,self.sources,self.revised)
        review=self.review(delta,outcome='coverage_gap')
        with self.assertRaisesRegex(RefinerError,'unresolved coverage'):validate_analysis_revision_review(review,delta,self.sources,self.revised)
        review['approved']=False
        validate_analysis_revision_review(review,delta,self.sources,self.revised)
    def test_source_grounded_wording_revision_with_real_mapping_is_allowed(self):
        self.revised['requirements'][0]['behavior']+=' 该语句是等价的清晰表述。'
        delta=analysis_revision_delta(self.previous,self.revised)
        review=self.review(delta)
        validate_value(review,schema_ref('skill-refiner.analysis-review-v3'))
        validate_analysis_review(review,self.revised)
        validate_analysis_revision_review(review,delta,self.sources,self.revised)
        self.assertNotEqual(delta['changes'][0]['before'],delta['changes'][0]['after'])
    def test_missing_forged_source_and_invalid_replacement_are_rejected(self):
        self.revised['scenarios'][0]['situation']+=' 新说明'
        delta=analysis_revision_delta(self.previous,self.revised)
        for variant in ['missing','wrong_quote','unknown_ref','wrong_hash']:
            review=self.review(delta);row=review['revision_assessments'][0]
            if variant=='missing':row['source_evidence']=[]
            elif variant=='wrong_quote':row['source_evidence'][0]['quote']='无此原文'
            elif variant=='unknown_ref':row['replacement_refs']=[{'kind':'scenario','id':'missing'}]
            else:row['before_sha256']='f'*64
            with self.subTest(variant=variant),self.assertRaises(RefinerError):
                validate_analysis_revision_review(review,delta,self.sources,self.revised)
    def test_removed_coverage_must_map_to_real_replacement_or_source_based_exclusion(self):
        self.revised['scenarios'].pop()
        delta=analysis_revision_delta(self.previous,self.revised)
        review=self.review(delta);row=review['revision_assessments'][0]
        row['replacement_refs']=[]
        with self.assertRaisesRegex(RefinerError,'lacks replacement'):validate_analysis_revision_review(review,delta,self.sources,self.revised)
        row['outcome']='excluded_by_source'
        validate_analysis_revision_review(review,delta,self.sources,self.revised)
    def test_tampered_before_after_cannot_match_review(self):
        self.revised['requirements'][0]['behavior']+=' 文字更清楚。'
        delta=analysis_revision_delta(self.previous,self.revised);review=self.review(delta)
        delta['changes'][0]['before']['behavior']='篡改'
        with self.assertRaisesRegex(RefinerError,'does not match'):validate_analysis_revision_review(review,delta,self.sources,self.revised)

if __name__=='__main__':unittest.main()

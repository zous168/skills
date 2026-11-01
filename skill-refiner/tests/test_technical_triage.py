import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.common import RefinerError, digest
from refiner.technical_triage import route_technical_questions

class TechnicalTriageTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {'requirements':[{'id':'R10','required':True}],
                         'scenarios':[{'id':'S16','applicable':True,'requirement_ids':['R10']},
                                      {'id':'S17','applicable':True,'requirement_ids':['R10']}],
                         'blocking_questions':[{'id':'B2','question':'能否观察外部调用？'},
                                               {'id':'B3','question':'业务规则应如何确定？'}]}
        self.triage = {'schema_version':'skill-refiner.technical-triage/v1','assessments':[
            {'question_id':'B2','domain':'technical_evidence','reason':'核验测试装置对网络和写入行为的观察范围。',
             'requirement_ids':['R10'],'scenario_ids':['S16','S17']},
            {'question_id':'B3','domain':'uncertain','reason':'涉及真实业务行为的决定仍需原始授权。',
             'requirement_ids':[],'scenario_ids':[]}]}
    def test_only_technical_question_moves_to_explicit_gap(self):
        prior=copy.deepcopy(self.analysis)
        derived,gap=route_technical_questions(self.analysis,self.triage)
        self.assertEqual(self.analysis,prior)
        self.assertEqual(derived['requirements'],prior['requirements'])
        self.assertEqual(derived['scenarios'],prior['scenarios'])
        self.assertEqual([x['id'] for x in derived['blocking_questions']],['B3'])
        self.assertEqual(gap['status'],'coverage_gap')
        self.assertEqual(gap['analysis_sha256'],digest(prior))
        self.assertEqual(gap['questions'][0]['scenario_ids'],['S16','S17'])
        self.assertTrue(gap['observation']['limitations'])
    def test_business_questions_remain_blocking(self):
        self.triage['assessments'][0]['domain']='business_decision'
        derived,gap=route_technical_questions(self.analysis,self.triage)
        self.assertEqual(derived,self.analysis)
        self.assertIsNone(gap)
    def test_unresolved_scope_cannot_be_reclassified_away(self):
        clarification = {'question_assessments': [
            {'question_id': 'B2', 'classification': 'uncertain'},
            {'question_id': 'B3', 'classification': 'uncertain'}]}
        derived, gap = route_technical_questions(self.analysis, self.triage, clarification)
        self.assertEqual(derived, self.analysis)
        self.assertIsNone(gap)
    def test_missing_questions_and_unrelated_requirements_rejected(self):
        self.triage['assessments'].pop()
        with self.assertRaises(RefinerError): route_technical_questions(self.analysis,self.triage)
        self.triage['assessments'].append({'question_id':'B3','domain':'uncertain','reason':'涉及真实业务行为的决定仍需原始授权。','requirement_ids':[],'scenario_ids':[]})
        self.triage['assessments'][0]['scenario_ids']=['S999']
        with self.assertRaises(RefinerError): route_technical_questions(self.analysis,self.triage)
    def test_scenario_must_cover_referenced_requirement(self):
        self.analysis['scenarios'][0]['requirement_ids']=['other']
        with self.assertRaises(RefinerError): route_technical_questions(self.analysis,self.triage)

if __name__=='__main__': unittest.main()

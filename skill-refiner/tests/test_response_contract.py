import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.response_contract import (ResponseContractError, freeze_response_contract,
                                       validate_response, audit_controls)


class ResponseContractTests(unittest.TestCase):
    def schema(self):
        return {'$schema':'https://json-schema.org/draft/2020-12/schema',
                'type':'object','additionalProperties':False,
                'required':['priority','reason','next_action','need_clarification'],
                'properties':{'priority':{'enum':['P0','P1','P2','P3',None]},
                              'reason':{'type':'string','minLength':1},
                              'next_action':{'type':'string','minLength':1},
                              'need_clarification':{'type':'boolean'}}}

    def value(self, **overrides):
        value={'priority':'P1','reason':'多人功能降级','next_action':'检查故障','need_clarification':False}
        value.update(overrides)
        return json.dumps(value,ensure_ascii=False)

    def contract(self):
        return freeze_response_contract(self.schema(), ['requirements.md:R1-R3','user-decision:null'])

    def test_actual_response_rejects_missing_fields_and_invalid_priority(self):
        contract=self.contract()
        self.assertEqual(validate_response(contract,self.value())['status'],'pass')
        self.assertEqual(validate_response(contract,self.value(priority=None,need_clarification=True))['status'],'pass')
        missing=validate_response(contract, '{"priority":"P1"}')
        self.assertEqual(missing['status'],'fail')
        self.assertEqual({x['path'] for x in missing['errors']},
                         {'/reason','/next_action','/need_clarification'})
        for invalid in (9, 'P9', True):
            result=validate_response(contract,self.value(priority=invalid))
            self.assertEqual(result['status'],'fail')
            self.assertTrue(any(x['path']=='/priority' and x['constraint']=='enum' for x in result['errors']))
        result=validate_response(contract,self.value(need_clarification='false'))
        self.assertTrue(any(x['path']=='/need_clarification' and x['constraint']=='type'
                            for x in result['errors']))

    def test_frozen_hash_detects_schema_sources_and_policy_changes(self):
        contract=self.contract()
        for mutation in ('schema_json','unverified_source_refs','allow_markdown_fence','schema_sha256'):
            item=copy.deepcopy(contract)
            if mutation=='schema_json':item['schema_json']='{}'
            elif mutation=='unverified_source_refs':item['unverified_source_refs'].append('unreviewed')
            elif mutation=='allow_markdown_fence':item[mutation]=True
            else:item[mutation]='0'*64
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ResponseContractError,'hash mismatch'):
                validate_response(item,self.value())

    def test_schema_compilation_rejects_external_unknown_and_unresolvable_refs(self):
        variants=[{'$ref':'https://example.test/schema'}, {'$ref':'file:///sensitive.json'},
                  {'$ref':'#/missing'}, {'$ref':'#'}, {'x-property':True,'typo':'string'},
                  {'type':'string','format':'made-up-format'}, {'type':'not-a-type'},
                  {'$schema':'http://json-schema.org/draft-07/schema','type':'string'}]
        for value in variants:
            with self.subTest(schema=value), self.assertRaises(ResponseContractError):
                freeze_response_contract(value,['requirement:R1'])
        with self.assertRaises(ResponseContractError):
            freeze_response_contract({'type':'string'},[])

    def test_local_refs_are_supported_without_external_fetching(self):
        schema={'$defs':{'priority':{'enum':['P0','P1']}}, '$ref':'#/$defs/priority'}
        contract=freeze_response_contract(schema,['contract#priority'])
        self.assertEqual(validate_response(contract,'"P1"')['status'],'pass')
        self.assertEqual(validate_response(contract,'"P8"')['status'],'fail')

    def test_plain_text_without_a_declared_json_contract_is_not_forced(self):
        result=validate_response(None,'这是普通中文回复，不是 JSON。')
        self.assertEqual(result,{'status':'not_applicable','schema_sha256':None,'errors':[]})

    def test_strict_json_and_explicit_markdown_policy(self):
        contract=self.contract()
        self.assertEqual(validate_response(contract, '{"priority":"P1","priority":"P0"}')['status'],'fail')
        self.assertEqual(validate_response(contract, '{"priority":NaN}')['status'],'fail')
        wrapped='\x60\x60\x60json\n'+self.value()+'\n\x60\x60\x60'
        self.assertEqual(validate_response(contract,wrapped)['status'],'fail')
        fenced=freeze_response_contract(self.schema(),['user:允许代码块'],allow_markdown_fence=True)
        self.assertEqual(validate_response(fenced,wrapped)['status'],'pass')

    def test_controls_apply_same_contract_without_confusing_semantic_target(self):
        contract=self.contract()
        positives=[{'id':'p1','response':self.value()}]
        negatives=[{'id':'semantic','assertion_id':'priority_is_p1','response':self.value(priority='P2')},
                   {'id':'schema','assertion_id':'response_contract','response':self.value(priority=9)}]
        def semantics(text):
            return {'priority_is_p1':json.loads(text).get('priority')=='P1'}
        result=audit_controls(contract,positives,negatives,semantics, expected_assertion_ids=['priority_is_p1'])
        self.assertTrue(result['passed'])
        self.assertTrue(all(x['contract']['schema_sha256']==contract['schema_sha256']
                            for x in result['controls']))
        self.assertFalse(result['controls'][1]['semantic']['priority_is_p1'])
        self.assertEqual(result['controls'][2]['contract']['status'],'fail')
        false_negative=[{'id':'n','assertion_id':'priority_is_p1',
                         'response':self.value(priority=9)},
                        {'id':'schema','assertion_id':'response_contract','response':self.value(priority=9)}]
        misleading=audit_controls(contract,positives,false_negative,lambda _: {'priority_is_p1':True},
                                  expected_assertion_ids=['priority_is_p1'])
        self.assertFalse(misleading['passed'])
        self.assertEqual(misleading['controls'][1]['contract']['status'],'fail')
        self.assertTrue(misleading['controls'][1]['semantic']['priority_is_p1'])

    def test_schema_failure_can_coexist_with_semantic_failure(self):
        contract=self.contract()
        positive=[{'id':'p','response':self.value()}]
        neg=[{'id':'n','assertion_id':'priority_is_p1','response':self.value(priority=9)},
             {'id':'s','assertion_id':'response_contract','response':'{'}]
        check=lambda text: {'priority_is_p1':json.loads(text).get('priority')=='P1'}
        result=audit_controls(contract,positive,neg,check,expected_assertion_ids=['priority_is_p1'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['controls'][1]['contract']['status'],'fail')
        self.assertFalse(result['controls'][1]['semantic']['priority_is_p1'])

    def test_exact_numeric_limits_and_fractional_multipleof(self):
        number=freeze_response_contract({'type':'number'},['spec:number'])
        self.assertEqual(validate_response(number,'1e9999')['status'],'pass')
        positive=freeze_response_contract({'type':'number','exclusiveMinimum':0},['spec:positive'])
        self.assertEqual(validate_response(positive,'1e-9999')['status'],'pass')
        const=freeze_response_contract({'const':9007199254740992},['spec:const'])
        self.assertEqual(validate_response(const,'9007199254740993.0')['status'],'fail')
        fraction=freeze_response_contract('{"type":"number","multipleOf":0.1}',['spec:multiple'])
        self.assertEqual(validate_response(fraction,'0.3')['status'],'pass')
        self.assertEqual(validate_response(fraction,'0.31')['status'],'fail')
        referenced=freeze_response_contract('{"$defs":{"amount":{"type":"number","multipleOf":0.1}},"$ref":"#/$defs/amount"}',['spec:reference'])
        self.assertEqual(validate_response(referenced,'0.3')['status'],'pass')
        self.assertEqual(validate_response(referenced,'0.31')['status'],'fail')
        integer=freeze_response_contract({'type':'integer'},['spec:integer'])
        self.assertEqual(validate_response(integer,'1.0')['status'],'pass')
        self.assertEqual(validate_response(integer,'1.1')['status'],'fail')
        overflow=validate_response(number,'1e10001')
        self.assertEqual(overflow['status'],'error')
        self.assertEqual(overflow['errors'][0]['constraint'],'numeric_precision_unsupported')

    def test_schema_numeric_source_is_parsed_exactly_and_float_dict_rejected(self):
        with self.assertRaisesRegex(ResponseContractError,'exact original JSON'):
            freeze_response_contract({'const':0.1},['schema:unknown-source'])
        source='{"type":"number","const":9007199254740993.0}'
        contract=freeze_response_contract(source.encode('utf8'),['schema:unverified'])
        self.assertEqual(contract['schema_json'],source)
        self.assertIn('unverified_source_refs',contract)
        self.assertEqual(validate_response(contract,'9007199254740993.0')['status'],'pass')
        self.assertEqual(validate_response(contract,'9007199254740992.0')['status'],'fail')
        tiny=freeze_response_contract('{"const":0.300000000000000000000000000001}', ['source'])
        self.assertEqual(validate_response(tiny,'0.300000000000000000000000000001')['status'],'pass')
        self.assertEqual(validate_response(tiny,'0.3')['status'],'fail')

    def test_reference_to_invalid_annotation_schema_is_rejected_when_frozen(self):
        schema={'default':{'type':'not-a-json-schema-type'},'$ref':'#/default'}
        with self.assertRaises(ResponseContractError):
            freeze_response_contract(schema,['spec:bad-ref'])

    def test_regex_constraints_fail_closed_without_timeout_isolation(self):
        for schema in ({'type':'string','pattern':'(a+)+$'},
                       {'type':'object','patternProperties':{'(a+)+$':{'type':'string'}}},
                       {'type':'string','format':'email'}):
            with self.subTest(schema=schema), self.assertRaisesRegex(ResponseContractError,'timeout-isolated'):
                freeze_response_contract(schema,['spec:regex'])

    def test_controls_require_complete_semantics_and_each_negative(self):
        positive=[{'id':'p','response':self.value()}]
        schema_negative=[{'id':'s','assertion_id':'response_contract','response':'{'}]
        with self.assertRaises(ResponseContractError):
            audit_controls(self.contract(),positive,schema_negative,lambda _: {}, expected_assertion_ids=[])
        with self.assertRaises(ResponseContractError):
            audit_controls(self.contract(),positive,schema_negative,lambda _: {}, expected_assertion_ids=['priority'])
        negatives=schema_negative+[{'id':'n','assertion_id':'priority','response':self.value(priority='P2')}]
        outcome=audit_controls(self.contract(),positive,negatives,lambda _: {},expected_assertion_ids=['priority'])
        self.assertFalse(outcome['passed'])
        self.assertEqual(outcome['status'],'error')

    def test_contract_control_does_not_parse_semantics_and_callback_failure_is_error(self):
        positives=[{'id':'p','response':self.value()}]
        negatives=[{'id':'n','assertion_id':'priority','response':self.value(priority='P2')},
                   {'id':'s','assertion_id':'response_contract','response':'{'}]
        def semantic(text):
            return {'priority':json.loads(text).get('priority')=='P1'}
        passed=audit_controls(self.contract(),positives,negatives,semantic,expected_assertion_ids=['priority'])
        self.assertTrue(passed['passed'])
        self.assertIsNone(passed['controls'][2]['semantic'])
        def broken(_):
            raise RuntimeError('private details must not leak')
        failed=audit_controls(self.contract(),positives,negatives,broken,expected_assertion_ids=['priority'])
        self.assertEqual(failed['status'],'error')
        self.assertNotIn('private details',json.dumps(failed))

    def test_controls_reject_unknown_assertion_and_bad_contract(self):
        positive=[{'id':'p','response':self.value()}]
        with self.assertRaises(ResponseContractError):
            audit_controls(self.contract(),positive,[{'id':'n','assertion_id':'missing','response':self.value()}],
                           lambda _: {'a':True},expected_assertion_ids=['a'])
        with self.assertRaises(ResponseContractError):
            audit_controls(None,positive,[{'id':'p','assertion_id':'response_contract','response':'bad'}],lambda _: {},
                           expected_assertion_ids=['a'])

if __name__=='__main__':
    unittest.main()

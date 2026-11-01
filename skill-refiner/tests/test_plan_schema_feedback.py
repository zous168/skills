import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.agent import Agent, _plan_schema_errors, _plan_response_has_secrets
from refiner.common import RefinerError, read_json


class PlanSchemaFeedbackTests(unittest.TestCase):
    def schema(self):
        return {'type':'object', 'required':['cases','tail'], 'properties':{
            'cases': {'type':'array', 'items': {'type':'object', 'required':['id','value'],
                      'properties': {'id':{'type':'string'}, 'value':{'type':'integer'}}}},
            'tail': {'type':'string', 'minLength':1}}}

    def test_full_matrix_and_all_schema_errors_returned_for_both_plan_roles(self):
        for role in ('plan', 'acceptance-plan'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                invalid = {'cases':[{'id':str(n), 'value':'wrong'} for n in range(16)], 'tail':'尾' * 16000}
                valid = {'cases':[{'id':str(n), 'value':n} for n in range(16)], 'tail':invalid['tail']}
                calls=[]
                def process(*args, **kwargs):
                    calls.append(json.loads(kwargs['input_text']))
                    return 0, json.dumps(invalid if len(calls)==1 else valid, ensure_ascii=False), '', 0.01
                agent=Agent(directory, config={'agents':{'default':{'backend':'command','command':['fixture']}}})
                with patch('refiner.agent.run_process', side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(agent.ask(role, 'plan', {'source':'fixture'}, self.schema()), valid)
                for request in calls:
                    self.assertEqual(request['input']['canonical_response_schema'], self.schema())
                feedback=calls[1]['input']
                self.assertEqual(json.loads(feedback['previous_invalid_response']), invalid)
                self.assertGreater(len(feedback['previous_invalid_response']), 12000)
                self.assertEqual(len(feedback['contract_errors']), 16)
                self.assertEqual({e['path'] for e in feedback['contract_errors']},
                                 {f'/cases/{n}/value' for n in range(16)})
                self.assertTrue(all(e['constraint']=='type' and e['actual']=='wrong'
                                    and e['expected']=='integer' for e in feedback['contract_errors']))
                self.assertIn('all cases and execution steps', feedback['revision_instruction'])
                self.assertEqual(read_json(Path(directory)/'agent-calls'/('001-'+role)/'contract-errors.json')
                                 ['errors'], feedback['contract_errors'])

    def test_codex_decoding_collects_every_schema_error_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid={'cases':[{'id':'one','value':'bad'}, {'id':'two','value':'also bad'}], 'tail':'保持' * 8000}
            valid={'cases':[{'id':'one','value':1}, {'id':'two','value':2}], 'tail':invalid['tail']}
            outputs=[{'final_message':json.dumps(invalid,ensure_ascii=False)},
                     {'final_message':json.dumps(valid,ensure_ascii=False)}]
            config={'agents':{'default':{'backend':'codex','model':'offline'}}}
            with patch('refiner.runtime.execute',side_effect=outputs), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(Agent(directory,config=config).ask('plan','plan',{},self.schema()),valid)
            request=read_json(Path(directory)/'agent-calls/002-plan/request.json')
            self.assertEqual(json.loads(request['input']['previous_invalid_response']),invalid)
            self.assertEqual({e['path'] for e in request['input']['contract_errors']},
                             {'/cases/0/value','/cases/1/value'})
            self.assertEqual(request['input']['canonical_response_schema'],self.schema())

    def test_repair_request_over_budget_fails_before_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid={'cases':[{'id':'1','value':'bad'}], 'tail':'长' * 9000}
            calls=[]
            def process(*args, **kwargs):
                calls.append(kwargs['input_text'])
                return 0,json.dumps(invalid, ensure_ascii=False),'',0.01
            config={'agents':{'default':{'backend':'command','command':['fixture'],'max_request_bytes':16384}}}
            with patch('refiner.agent.run_process',side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RefinerError,'exceeding the configured'):
                    Agent(directory,config=config).ask('plan','plan',{},self.schema())
            self.assertEqual(len(calls),1)
            rejected=list((Path(directory)/'rejected-agent-requests').glob('*/request-manifest.json'))
            self.assertEqual(len(rejected),1)
            manifest=read_json(rejected[0])
            self.assertEqual(manifest['status'],'rejected_context_budget')
            request=read_json(rejected[0].parent/'request.json')
            self.assertEqual(json.loads(request['input']['previous_invalid_response']),invalid)
            self.assertFalse((Path(directory)/'agent-calls/002-plan').exists())
            self.assertEqual(read_json(Path(directory)/'agent-calls/001-plan/request-manifest.json')['status'],'invalid_response')
            self.assertEqual(Agent(directory,config=config).calls,1)

    def test_sensitive_failed_plan_is_not_forwarded_or_logged_in_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            secret='crsr_'+'z'*40
            invalid={'cases':[{'id':'1','value':secret}],'tail':'ok'}
            calls=[]
            def process(*args,**kwargs):
                calls.append(1)
                return 0,json.dumps(invalid),'',0.01
            agent=Agent(directory,config={'agents':{'default':{'backend':'command','command':['fixture']}}})
            output=io.StringIO()
            with patch('refiner.agent.run_process',side_effect=process), contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RefinerError,'sensitive credentials'):
                    agent.ask('plan','plan',{},self.schema())
            self.assertEqual(len(calls),1)
            diagnostics=(Path(directory)/'agent-calls/001-plan/contract-errors.json').read_text(encoding='utf8')
            self.assertNotIn(secret,diagnostics+output.getvalue())
            self.assertFalse((Path(directory)/'agent-calls/002-plan').exists())

    def test_sensitive_object_key_neither_leaks_via_path_nor_reaches_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            secret='crsr_'+'z'*40
            schema={'type':'object', 'additionalProperties':{'type':'integer'}}
            invalid={secret:'wrong'}
            calls=[]
            def process(*args,**kwargs):
                calls.append(1)
                return 0,json.dumps(invalid),'',0.01
            agent=Agent(directory,config={'agents':{'default':{'backend':'command','command':['fixture']}}})
            output=io.StringIO()
            with patch('refiner.agent.run_process',side_effect=process), contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RefinerError,'sensitive credentials'):
                    agent.ask('plan','plan',{},schema)
            self.assertEqual(len(calls),1)
            root=Path(directory)/'agent-calls/001-plan'
            diagnostic=(root/'contract-errors.json').read_text(encoding='utf8')
            execution=(root/'execution.json').read_text(encoding='utf8')
            self.assertNotIn(secret,diagnostic+execution+output.getvalue())
            self.assertIn('[REDACTED]',diagnostic)
            self.assertFalse((root.parent/'002-plan').exists())
            self.assertEqual(json.loads((root/'raw-response.txt').read_text(encoding='utf8')),invalid)

    def test_additional_properties_diagnostic_redacts_secret_in_nested_keys(self):
        secret='crsr_'+'z'*40
        schema={'type':'object','additionalProperties':False,'properties':{'expected':{'type':'string'}}}
        errors=_plan_schema_errors(schema, {'expected':'ok',secret:{secret:'x'}})
        self.assertEqual(len(errors),1)
        self.assertNotIn(secret,json.dumps(errors,ensure_ascii=False))
        self.assertIn('[REDACTED]',json.dumps(errors,ensure_ascii=False))

    def test_private_fields_are_redacted_without_missing_key_error(self):
        schema={'type':'object','additionalProperties':False,'properties':{'expected':{'type':'string'}}}
        for field in ('raw_content','encrypted_content','summary_text',
                      'internal_chat_message_metadata_passthrough'):
            with self.subTest(field=field):
                response={'expected':'ok',field:'private-fixture-contents'}
                errors=_plan_schema_errors(schema,response)
                self.assertNotIn('private-fixture-contents',json.dumps(errors,ensure_ascii=False))
                self.assertTrue(_plan_response_has_secrets(json.dumps(response),response))

    def test_codex_invalid_encoded_json_is_reported_as_wire_decode_error(self):
        item={'type':'object','required':['type','value'],
              'properties':{'type':{'type':'string'},'value':{}}}
        schema={'type':'object','required':['cases'], 'properties':{
            'cases':{'type':'array','items':{'type':'object','properties':{
                'checks':{'type':'array','items':item}}}}}}
        invalid={'cases':[{'checks':[{'type':'json_equal','value_json':'not-json'}]}]}
        valid_wire={'cases':[{'checks':[{'type':'json_equal','value_json':'3'}]}]}
        outputs=[{'final_message':json.dumps(invalid)}, {'final_message':json.dumps(valid_wire)}]
        with tempfile.TemporaryDirectory() as directory:
            config={'agents':{'default':{'backend':'codex','model':'offline'}}}
            with patch('refiner.runtime.execute',side_effect=outputs), contextlib.redirect_stdout(io.StringIO()):
                result=Agent(directory,config=config).ask('plan','plan',{},schema)
            self.assertEqual(result['cases'][0]['checks'][0]['value'],3)
            request=read_json(Path(directory)/'agent-calls/002-plan/request.json')
            errors=request['input']['contract_errors']
            self.assertEqual({e['constraint'] for e in errors},{'wire_json_encoding','wire_decode'})
            specific=next(e for e in errors if e['constraint']=='wire_json_encoding')
            self.assertEqual(specific['path'],'/cases/0/checks/0/value_json')
            self.assertEqual(specific['actual'],'not-json')
            self.assertFalse(any(e['constraint']=='required' for e in errors))

    def test_codex_dynamic_duplicate_key_is_reported_as_wire_decode(self):
        schema={'type':'object','required':['files'], 'properties':{
            'files':{'type':'object','additionalProperties':{'type':'string'}}}}
        invalid={'files':[{'key':'dup','value':'one'},{'key':'dup','value':'two'}]}
        valid={'files':[{'key':'dup','value':'one'}]}
        outputs=[{'final_message':json.dumps(invalid)}, {'final_message':json.dumps(valid)}]
        with tempfile.TemporaryDirectory() as directory:
            config={'agents':{'default':{'backend':'codex','model':'offline'}}}
            with patch('refiner.runtime.execute',side_effect=outputs), contextlib.redirect_stdout(io.StringIO()):
                result=Agent(directory,config=config).ask('plan','plan',{},schema)
            self.assertEqual(result['files'],{'dup':'one'})
            errors=read_json(Path(directory)/'agent-calls/002-plan/request.json')['input']['contract_errors']
            self.assertEqual(errors[0]['constraint'],'wire_decode')
            self.assertIn('duplicate key',errors[0]['message'])
            self.assertFalse(any(e['constraint']=='required' for e in errors))

    def test_codex_string_check_allows_literal_non_json_value(self):
        item={'type':'object','required':['type','value'],
              'properties':{'type':{'type':'string'},'value':{}}}
        response={'type':'contains','value_json':'not-json'}
        with tempfile.TemporaryDirectory() as directory:
            config={'agents':{'default':{'backend':'codex','model':'offline'}}}
            with patch('refiner.runtime.execute',return_value={'final_message':json.dumps(response)}), contextlib.redirect_stdout(io.StringIO()):
                result=Agent(directory,config=config).ask('plan','plan',{},item)
            self.assertEqual(result['value'],'not-json')

    def test_backend_error_preserves_original_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'agents':{'default':{'backend':'codex','model':'offline'}}}
            with patch('refiner.runtime.execute',side_effect=RuntimeError('backend unavailable')), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'backend unavailable'):
                    Agent(directory,config=config).ask('plan','plan',{},self.schema())
            self.assertEqual(read_json(Path(directory)/'agent-calls/001-plan/execution.json')['status'],
                             'execution_error')

    def test_error_overflow_fails_closed(self):
        schema={'type':'array','items':{'type':'integer'}}
        with self.assertRaisesRegex(RefinerError,'256-error limit'):
            _plan_schema_errors(schema,['bad']*257)

    def test_json_string_spacing_is_not_a_credential(self):
        obj={'example':'{\"priority\":\"P1\"}'}
        self.assertFalse(_plan_response_has_secrets(json.dumps(obj),obj))

if __name__=='__main__':
    unittest.main()

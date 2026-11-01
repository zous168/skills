import copy
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refiner.common import RefinerError, digest, write_json
from refiner.pipeline import resolve_response_applicability, load_response_applicability, response_applicability_lock

class FakeAgent:
    def __init__(self, outputs, work):
        self.outputs = iter(outputs)
        self.calls = []
        self.work = Path(work)
    @contextmanager
    def reserve_call_group(self, requests):
        yield [object() for _ in requests]
    def ask(self, role, instruction, payload, schema, **kwargs):
        self.calls.append((role, copy.deepcopy(payload)))
        response = copy.deepcopy(next(self.outputs))
        request = {'role': role, 'instruction': instruction, 'input': payload, 'response_schema': schema}
        directory = self.work / 'agent-calls' / f'{len(self.calls):03}-{role}'
        write_json(directory / 'request.json', request)
        write_json(directory / 'response.json', response)
        write_json(directory / 'request-manifest.json', {'role':role, 'status':'completed',
                   'request_sha256':hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest(),
                   'response_sha256':digest(response)})
        write_json(directory / 'execution.json', {'status':'completed'})
        return response

class ApplicabilityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.text = '不得不返回 JSON 格式结果。'
        self.inputs = {'requirements': [{'source': 'req', 'content': self.text}], 'files': []}
        self.sources = {'req': hashlib.sha256(self.text.encode('utf8')).hexdigest()}
        self.plan = {'cases': [{'id': 'normal', 'kind': 'normal'}, {'id': 'error', 'kind': 'error'}]}
        self.response = {'schema_version': 'skill-refiner.response-applicability-analysis/v1',
                         'assessments': [{'case_id': c['id'], 'decision': 'required', 'reason': '输出必须为 JSON',
                                          'evidence': [{'source': 'req', 'quote': self.text}]} for c in self.plan['cases']]}

    def test_two_blind_calls_then_resume_uses_saved_record(self):
        with tempfile.TemporaryDirectory() as root:
            agent = FakeAgent([self.response, self.response], root)
            out = resolve_response_applicability(agent, root, self.inputs, self.plan, self.sources)
            self.assertEqual(out['required_case_ids'], ['error', 'normal'])
            self.assertEqual([x[0] for x in agent.calls], ['analysis-review', 'plan-review'])
            self.assertEqual(agent.calls[0][1], agent.calls[1][1])
            self.assertNotIn('analysis', agent.calls[1][1])
            resumed = load_response_applicability(root, self.inputs, self.plan, self.sources)
            self.assertEqual(out, resumed)
            resolve_response_applicability(agent, root, self.inputs, self.plan, self.sources)
            self.assertEqual(len(agent.calls), 2)

    def test_disagreement_uncertainty_and_invalid_citation_block(self):
        for variant in ('disagreement', 'uncertain', 'quote', 'missing'):
            review = copy.deepcopy(self.response)
            if variant == 'disagreement': review['assessments'][0]['decision'] = 'not_required'
            if variant == 'uncertain': review['assessments'][0]['decision'] = 'uncertain'
            if variant == 'quote': review['assessments'][0]['evidence'][0]['quote'] = 'fabricated'
            if variant == 'missing': review['assessments'].pop()
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(RefinerError):
                    resolve_response_applicability(FakeAgent([self.response, review], root), root, self.inputs, self.plan, self.sources)
                with self.assertRaises(RefinerError): load_response_applicability(root, self.inputs, self.plan, self.sources)

    def test_changed_plan_inputs_or_sources_cannot_reuse_decision(self):
        with tempfile.TemporaryDirectory() as root:
            resolve_response_applicability(FakeAgent([self.response, self.response], root), root, self.inputs, self.plan, self.sources)
            bad_inputs = copy.deepcopy(self.inputs); bad_inputs['requirements'][0]['content'] += ' altered'
            with self.assertRaises(RefinerError): load_response_applicability(root, bad_inputs, self.plan, self.sources)
            bad_plan = copy.deepcopy(self.plan); bad_plan['cases'][0]['kind'] = 'boundary'
            with self.assertRaises(RefinerError): load_response_applicability(root, self.inputs, bad_plan, self.sources)
            with self.assertRaises(RefinerError): load_response_applicability(root, self.inputs, self.plan, {'req': '0'*64})

    def test_recognized_obligation_cannot_be_waived_by_reviews(self):
        inputs = {'requirements': [{'source':'req', 'content':'输出 JSON。不得不返回 JSON。'}], 'files':[]}
        sources = {'req': hashlib.sha256(inputs['requirements'][0]['content'].encode()).hexdigest()}
        wrong = copy.deepcopy(self.response)
        for row in wrong['assessments']:
            row['decision'] = 'not_required'; row['evidence'][0]['quote'] = '输出 JSON'
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RefinerError, 'cannot waive'):
            resolve_response_applicability(FakeAgent([wrong, wrong], root), root, inputs, self.plan, sources)

    def test_final_gate_accepts_locked_decision_and_detects_tampering(self):
        from refiner.checks import validate_plan
        from refiner.gate import _verify_plan_control_artifacts
        from test_response_contract_plan_v7 import sample
        plan = sample()
        text = self.text
        inputs = {'requirements': [{'source':'requirements.md#R1', 'content':text}], 'files':[], 'standards':[]}
        bindings = {'requirements.md#R1': hashlib.sha256(text.encode()).hexdigest()}
        response = {'schema_version':'skill-refiner.response-applicability-analysis/v1',
                    'assessments':[{'case_id':c['id'], 'decision':'required', 'reason':'JSON output required',
                                    'evidence':[{'source':'requirements.md#R1','quote':text}]} for c in plan['cases']]}
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            write_json(root/'inputs.json', inputs)
            write_json(root/'resume-contract.json', {'inputs_sha256':digest(inputs)})
            resolved = resolve_response_applicability(FakeAgent([response,response],root), root, inputs, plan, bindings)
            controls = validate_plan(plan, root, verified_response_sources=bindings,
                                     required_response_contract_cases=resolved['required_case_ids'])
            evidence = {'schema_version':'skill-refiner.plan-controls/v1', 'suite':'development',
                        'attempt':1, 'status':'passed', 'plan_sha256':digest(plan), 'plan_snapshot':plan,
                        'case_control_results':controls['case_control_results'], 'control_failures':[],
                        'coverage_failures':[], 'revision_context': {
                            'schema_version':'skill-refiner.plan-revision-context/v1', 'base_plan_sha256':None,
                            'feedback':[], 'feedback_sources':[], 'targeted_case_ids':[], 'case_changes':[],
                            'requirements_changed':False, 'execution_plan_changed':False}}
            write_json(root/'plan-controls/development.json', evidence)
            write_json(root/'plan-controls/attempts/development-01.json', evidence)
            hashes = response_applicability_lock(root, inputs, {'development':plan}, bindings)
            write_json(root/'oracle.lock.json', {'response_applicability_sha256':hashes})
            _verify_plan_control_artifacts(root, {'development':plan})
            write_json(root/'oracle.lock.json', {'response_applicability_sha256':{'development':'0'*64}})
            with self.assertRaisesRegex(RefinerError, 'decision hash mismatch'):
                _verify_plan_control_artifacts(root, {'development':plan})

    def test_tampered_independent_call_evidence_blocks_resume(self):
        with tempfile.TemporaryDirectory() as root:
            resolve_response_applicability(FakeAgent([self.response,self.response],root), root, self.inputs, self.plan, self.sources)
            path = Path(root)/'plan-controls/response-applicability'/(digest(self.plan)+'.json')
            record = json.loads(path.read_text(encoding='utf8'))
            record['call_evidence']['independent_review']['request']['input']['plan']['cases'][0]['kind'] = 'boundary'
            write_json(path, record)
            with self.assertRaisesRegex(RefinerError, 'call evidence is invalid'):
                load_response_applicability(root, self.inputs, self.plan, self.sources)

    def test_plan_gate_resolves_ambiguity_before_controls_and_independent_review(self):
        from refiner.pipeline import make_plan
        from test_response_contract_plan_v7 import sample
        from test_plan_contract import _approved_review
        plan = sample()
        text = self.text
        context = {'requirements':[{'source':'requirements.md#R1','content':text}],
                   'files':[], 'standards':[], '_response_contract_v7':True}
        response = {'schema_version':'skill-refiner.response-applicability-analysis/v1',
                    'assessments':[{'case_id':c['id'],'decision':'required','reason':'必须返回 JSON',
                                    'evidence':[{'source':'requirements.md#R1','quote':text}]} for c in plan['cases']]}
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            write_json(root/'inputs.json', context)
            write_json(root/'resume-contract.json', {'inputs_sha256':digest(context)})
            classify = FakeAgent([response,response], root)
            class Planner:
                def reserve_call_group(self, requests): return classify.reserve_call_group(requests)
                def ask(self, role, instruction, payload, schema, **kwargs):
                    if role == 'plan': return copy.deepcopy(plan)
                    if kwargs.get('_attempt') == 1:
                        return classify.ask(role, instruction, payload, schema, **kwargs)
                    assert payload['response_contract_applicability']['decision_sha256']
                    return _approved_review(plan)
            result = make_plan(Planner(), context, root, 8, max_attempts=1)
            self.assertEqual(result, plan)
            self.assertEqual(len(classify.calls), 2)
            self.assertEqual(json.loads((root/'plan-controls/development.json').read_text(encoding='utf8'))['status'], 'passed')

    def test_actual_agent_controller_records_two_budgeted_calls_without_model(self):
        from refiner.agent import Agent
        with tempfile.TemporaryDirectory(prefix='refiner-applicability-') as root:
            root = Path(root)
            stub = root/'classifier.py'
            stub.write_text('import json,sys\n'
                'request=json.load(sys.stdin)\n'
                'source=next(iter(request["input"]["frozen_sources"]))\n'
                'quote=request["input"]["frozen_sources"][source]\n'
                'cases=request["input"]["plan"]["cases"]\n'
                'result={"schema_version":"skill-refiner.response-applicability-analysis/v1",'
                '"assessments":[{"case_id":c["id"],"decision":"required","reason":"source requires JSON",'
                '"evidence":[{"source":source,"quote":quote}]} for c in cases]}\n'
                'print(json.dumps(result,ensure_ascii=False))\n', encoding='utf8')
            controller = Agent(root, command=f'{Path(sys.executable).as_posix()} {stub.as_posix()}', timeout=20)
            controller.max_calls = 1
            captured = []
            original_reserve = controller.reserve_call_group
            def capture_group(requests):
                captured[:] = copy.deepcopy(requests)
                return original_reserve(requests)
            controller.reserve_call_group = capture_group
            with self.assertRaisesRegex(RefinerError, 'budget exhausted'):
                resolve_response_applicability(controller, root, self.inputs, self.plan, self.sources)
            self.assertEqual(controller.calls, 0)
            # A separately completed first call is a valid checkpoint on resume.
            first = captured[0]
            controller.ask(first['role'], first['instruction'], first['payload'], first['schema'], _attempt=1)
            self.assertEqual(controller.calls, 1)
            controller.max_calls = 2
            result = resolve_response_applicability(controller, root, self.inputs, self.plan, self.sources)
            self.assertEqual(result['required_case_ids'], ['error','normal'])
            self.assertEqual(controller.calls, 2)
            self.assertEqual(controller.cached_calls, 1)
            self.assertEqual(load_response_applicability(root, self.inputs, self.plan, self.sources), result)

    def test_explicit_conflict_blocks_even_agreeing_models(self):
        text = '最终输出 JSON。最终输出纯文本。'
        inputs = {'requirements':[{'source':'req','content':text}], 'files':[]}
        bindings = {'req':hashlib.sha256(text.encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as root:
            agent = FakeAgent([self.response,self.response],root)
            with self.assertRaisesRegex(RefinerError, 'Conflicting response format'):
                resolve_response_applicability(agent, root, inputs, self.plan, bindings)
            self.assertEqual(agent.calls, [])

    def test_bad_first_analysis_stops_before_independent_review(self):
        for variant in ('uncertain', 'quote', 'missing'):
            first = copy.deepcopy(self.response)
            if variant == 'uncertain': first['assessments'][0]['decision'] = 'uncertain'
            if variant == 'quote': first['assessments'][0]['evidence'][0]['quote'] = 'fabricated'
            if variant == 'missing': first['assessments'].pop()
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                agent = FakeAgent([first, self.response], root)
                with self.assertRaises(RefinerError):
                    resolve_response_applicability(agent, root, self.inputs, self.plan, self.sources)
                self.assertEqual([call[0] for call in agent.calls], ['analysis-review'])
                with self.assertRaises(RefinerError):
                    load_response_applicability(root, self.inputs, self.plan, self.sources)

    def test_resolution_lock_rejects_symlinked_decision_and_parent(self):
        plan = dict(self.plan, schema_version='skill-refiner.plan/v7')
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)/'run'; root.mkdir()
            resolve_response_applicability(FakeAgent([self.response,self.response],root), root, self.inputs, plan, self.sources)
            directory = root/'plan-controls/response-applicability'
            target = directory/(digest(plan)+'.json')
            outside = Path(base)/'outside.json'
            shutil.copyfile(target, outside)
            target.unlink()
            try:
                os.symlink(outside, target)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            try:
                with self.assertRaisesRegex(RefinerError, 'symlink or junction'):
                    load_response_applicability(root, self.inputs, plan, self.sources)
                with self.assertRaisesRegex(RefinerError, 'symlink or junction'):
                    response_applicability_lock(root, self.inputs, {'development':plan}, self.sources)
            finally:
                target.unlink()
            shutil.copyfile(outside, target)
            moved = Path(base)/'moved'; directory.rename(moved)
            try:
                os.symlink(moved, directory, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'directory symlinks unavailable: {exc}')
            try:
                with self.assertRaisesRegex(RefinerError, 'symlink or junction'):
                    load_response_applicability(root, self.inputs, plan, self.sources)
                with self.assertRaisesRegex(RefinerError, 'symlink or junction'):
                    response_applicability_lock(root, self.inputs, {'development':plan}, self.sources)
            finally:
                os.rmdir(directory)

    def test_invalid_first_analysis_releases_unused_atomic_reservation(self):
        from refiner.agent import Agent
        from refiner.pipeline import _response_analysis_schema
        with tempfile.TemporaryDirectory(prefix='refiner-applicability-') as root:
            root = Path(root)
            bad = copy.deepcopy(self.response)
            bad['assessments'][0]['decision'] = 'uncertain'
            stub = root/'classifier.py'
            stub.write_text('import sys\nsys.stdin.read()\nprint('+repr(json.dumps(bad,ensure_ascii=True))+')\n', encoding='utf8')
            controller = Agent(root, command=f'{Path(sys.executable).as_posix()} {stub.as_posix()}', timeout=20)
            controller.max_calls = 2
            with self.assertRaisesRegex(RefinerError, 'uncertain'):
                resolve_response_applicability(controller, root, self.inputs, self.plan, self.sources)
            self.assertEqual(controller.calls, 1)
            self.assertEqual(controller._reserved_calls, 0)
            self.assertFalse((root/'agent-calls/002-plan-review').exists())
            request = {'role':'plan-review', 'instruction':'independent check', 'payload':{},
                       'schema':_response_analysis_schema(), '_attempt':1}
            with controller.reserve_call_group([request]):
                self.assertEqual(controller._reserved_calls, 1)
            self.assertEqual(controller._reserved_calls, 0)

    def test_concurrent_agent_call_cannot_consume_review_reservation(self):
        from refiner.agent import Agent
        from refiner.pipeline import _response_analysis_schema
        entered, release = threading.Event(), threading.Event()
        calls = []
        def local_process(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(5): raise RuntimeError('test release timed out')
            return 0, json.dumps(self.response, ensure_ascii=False), '', 0.01
        with tempfile.TemporaryDirectory() as root:
            controller = Agent(root, command=Path(sys.executable).as_posix(), timeout=20)
            controller.max_calls = 2
            with patch('refiner.agent.run_process', side_effect=local_process), ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(resolve_response_applicability, controller, root, self.inputs, self.plan, self.sources)
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaisesRegex(RefinerError, 'budget exhausted'):
                        controller.ask('plan-review', 'competing request', {}, _response_analysis_schema(), _attempt=1)
                finally:
                    release.set()
                result = task.result(timeout=10)
            self.assertEqual(result['required_case_ids'], ['error','normal'])
            self.assertEqual(controller.calls, 2)
            self.assertEqual(controller._reserved_calls, 0)
            self.assertEqual(len(calls), 2)

if __name__ == '__main__': unittest.main()

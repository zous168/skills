"""Independent analysis and clarification corrections, using offline fake Agents."""
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from refiner.cli import main
from refiner.common import RefinerError, read_json, write_json
from refiner.creation import _latest_resume_clarification, validate_clarification_revision, validate_analysis
from test_clarification_review import ClarificationReviewTests
from test_analysis_contract_v3 import AnalysisV3Tests
from test_target_obligations import TargetObligationTests
from test_technical_recovery import incomplete_v4


class IndependentRevisionBudgetTests(unittest.TestCase):
    def test_real_v3_pair_can_be_projected_to_v4_without_changing_business(self):
        root = Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3/create-continuation-20261031-technical-gap')
        if not root.is_dir():
            self.skipTest('real technical-gap source unavailable')
        old = read_json(root / 'agent-calls/022-analyze/response.json')
        review = read_json(root / 'agent-calls/023-analysis-review/response.json')
        inputs = read_json(root / 'inputs.json')
        new = incomplete_v4(old, inputs['standards'])  # structural fixture, not approval of obligations
        extra = [row for row in review['question_assessments'] if row['classification'] == 'unsupported_extra']
        new['blocking_questions'] = [q for q in old['blocking_questions'] if q['id'] not in {r['question_id'] for r in extra}]
        new['scope']['out_of_scope'] += [row['out_of_scope'] for row in extra]
        validate_analysis(new, inputs['requested_name'], {f'spec-{i+1}' for i in range(len(inputs['requirements']))},
                          standards=inputs['standards'])
        with self.assertRaises(RefinerError):
            validate_clarification_revision(old, new, review)
        validate_clarification_revision(old, new, review, allow_v3_to_v4=True)
        linked = copy.deepcopy(new)
        scenario = next(row for row in linked['scenarios'] if row['applicable'])
        workflow = linked['workflow'][0]
        obligation = linked['target_obligations']['obligations'][0]
        obligation.update(subject='target', applicable=True, scenario_ids=[scenario['id']], workflow_ids=[workflow['id']],
                          acceptance_criteria=[{'id': 'O-INCOMPLETE.A1', 'statement': '检查标准对应的可观察结果。',
                                                'evidence': '记录该标准对应的独立验收证据。'}],
                          mechanism={'state': 'planned', 'interface': '标准验证接口计划单独提供。',
                                     'evidence_strategy': '独立审查后记录实际证据。', 'gap_ids': []})
        scenario['requirement_ids'].append(obligation['id'])
        scenario['acceptance_criteria_ids'].append('O-INCOMPLETE.A1')
        workflow['requirement_ids'].append(obligation['id'])
        validate_analysis(linked, inputs['requested_name'], {f'spec-{i+1}' for i in range(len(inputs['requirements']))},
                          standards=inputs['standards'])
        validate_clarification_revision(old, linked, review, allow_v3_to_v4=True)
        linked['workflow'][0]['requirement_ids'].append('R-UNSOURCED')
        with self.assertRaises(RefinerError):
            validate_clarification_revision(old, linked, review, allow_v3_to_v4=True)
        for mutation in ('business', 'missing_B2', 'missing_B3', 'extra_field'):
            bad = copy.deepcopy(new)
            if mutation == 'business':
                bad['requirements'][0]['behavior'] += ' 篡改原业务。'
            elif mutation.startswith('missing_'):
                bad['blocking_questions'] = [q for q in bad['blocking_questions'] if q['id'] != mutation[8:]]
            else:
                bad['unexpected'] = 'unreviewed'
            with self.subTest(mutation=mutation), self.assertRaises(RefinerError):
                validate_clarification_revision(old, bad, review, allow_v3_to_v4=True)

    def test_resume_selection_ignores_sealed_pair_older_than_full_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); (work / 'agent-calls').mkdir()
            sealed = {'pair': {'analysis_call': '022-analyze', 'review_call': '023-analysis-review'},
                      'calls': ['022-analyze', '023-analysis-review']}
            history = [{'analysis_call': '024-analyze', 'review_call': '026-analysis-review', 'analysis': {}, 'review': {}}]
            self.assertIsNone(_latest_resume_clarification(work, {}, history, sealed, {'stages': []}))

    def test_resume_selects_newer_authenticated_pair_and_rejects_wrong_provenance(self):
        fixture = ClarificationReviewTests('runTest')
        old = fixture.analysis(); review = fixture.review([fixture.row('BQ1', 'required_in_scope'), fixture.row('BQ2', 'unsupported_extra')])
        new = copy.deepcopy(old); new['blocking_questions'] = new['blocking_questions'][:1]
        new['scope']['out_of_scope'].append(review['question_assessments'][1]['out_of_scope'])
        next_review = fixture.review([fixture.row('BQ1', 'required_in_scope')])
        pair = {'analysis_call': '024-analyze', 'review_call': '025-analysis-review', 'analysis': new, 'review': next_review}
        sealed = {'pair': {'analysis_call': '022-analyze', 'review_call': '023-analysis-review', 'analysis': old, 'review': review},
                  'calls': ['022-analyze', '023-analysis-review']}
        history = [{'analysis_call': '020-analyze', 'review_call': '021-analysis-review', 'analysis': {}, 'review': {'approved': False}}]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp); (work / 'agent-calls/024-analyze').mkdir(parents=True)
            stages = {'stages': [{'id': 'analysis', 'status': 'blocked', 'artifacts': []}]}
            request = {'input': {'previous_analysis': old, 'clarification_review': review,
                                 'previous_analysis_reviews': [history[0]['review']]}}
            with patch('refiner.technical_recovery.clarification_history', return_value=([pair], ['024-analyze', '025-analysis-review'])), \
                 patch('refiner.analysis_recovery._call', return_value=(request, new)):
                result = _latest_resume_clarification(work, {}, history, sealed, stages)
                self.assertEqual(result['pair'], pair)
                pair['review']['question_assessments'][0]['classification'] = 'unsupported_extra'
                with self.assertRaisesRegex(RefinerError, 'exhausted its correction budget'):
                    _latest_resume_clarification(work, {}, history, sealed, stages)
                pair['review']['question_assessments'][0]['classification'] = 'required_in_scope'
                request['input']['previous_analysis'] = {'forged': True}
                with self.assertRaisesRegex(RefinerError, 'authenticated prior pair'):
                    _latest_resume_clarification(work, {}, history, sealed, stages)

    def test_real_v3_to_v4_pair_resumes_from_authenticated_live_records(self):
        root = Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3/create-continuation-20261031-technical-gap')
        if not root.is_dir():
            self.skipTest('real technical-gap source unavailable')
        old = read_json(root / 'agent-calls/022-analyze/response.json')
        review = read_json(root / 'agent-calls/023-analysis-review/response.json')
        inputs = read_json(root / 'inputs.json')
        full_review = read_json(root / 'analysis-review-attempt-3.json')
        new = incomplete_v4(old, inputs['standards'])  # only a protocol fixture, not an accepted standard review
        extra = [row for row in review['question_assessments'] if row['classification'] == 'unsupported_extra']
        new['blocking_questions'] = [q for q in old['blocking_questions'] if q['id'] not in {r['question_id'] for r in extra}]
        new['scope']['out_of_scope'] += [row['out_of_scope'] for row in extra]
        next_review = {'schema_version': review['schema_version'], 'question_assessments': [
            row for row in review['question_assessments'] if row['classification'] != 'unsupported_extra']}
        sealed = {'pair': {'analysis_call': '022-analyze', 'review_call': '023-analysis-review', 'analysis': old, 'review': review},
                  'calls': ['022-analyze', '023-analysis-review']}
        history = [{'analysis_call': '020-analyze', 'review_call': '021-analysis-review', 'analysis': {}, 'review': full_review}]
        sources = [{'id': f'spec-{i+1}', 'source': row['source'], 'content': row['content']}
                   for i, row in enumerate(inputs['requirements'])]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            def call(name, role, payload, response):
                directory = work / 'agent-calls' / name; directory.mkdir(parents=True)
                request = {'role': role, 'input': payload}
                req_sha = hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest()
                response_sha = hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                write_json(directory / 'request.json', request)
                write_json(directory / 'response.json', response)
                write_json(directory / 'request-manifest.json', {'role': role, 'status': 'completed',
                           'request_sha256': req_sha, 'response_sha256': response_sha})
                write_json(directory / 'execution.json', {'role': role, 'status': 'completed', 'exit_code': 0,
                           'finished_at': 'fixture'})
                write_json(directory / 'process.execution.json', {'status': 'completed', 'exit_code': 0,
                           'finished_at': 'fixture'})
            call('024-analyze', 'analyze', {**inputs, 'previous_analysis': old, 'clarification_review': review,
                 'previous_analysis_reviews': [full_review]}, new)
            call('025-analysis-review', 'analysis-review', {'analysis': new, 'business_sources': sources}, next_review)
            artifacts = [{'path': p.relative_to(work).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in (work / 'agent-calls').rglob('*') if p.is_file()]
            stages = {'stages': [{'id': 'analysis', 'status': 'blocked', 'artifacts': artifacts}]}
            selected = _latest_resume_clarification(work, inputs, history, sealed, stages)
            self.assertEqual(selected['pair']['analysis_call'], '024-analyze')
            self.assertEqual(selected['pair']['review_call'], '025-analysis-review')
            self.assertEqual(selected['pair']['analysis'], new)
            path = work / 'agent-calls/025-analysis-review/response.json'
            path.write_text('{}', encoding='utf-8')
            with self.assertRaises(RefinerError):
                _latest_resume_clarification(work, inputs, history, sealed, stages)
            with patch('refiner.technical_recovery.clarification_history', side_effect=RefinerError('unverified stage inventory')):
                with self.assertRaisesRegex(RefinerError, 'unverified stage inventory'):
                    _latest_resume_clarification(work, {}, history, sealed, stages)

    def test_real_technical_gap_migration_reaches_new_review_without_replaying_old_calls(self):
        source = Path('D:/work/skills/.refiner-runs/skill-refiner-autotest-20261030-fdd3/create-continuation-20261031-technical-gap')
        artifact = Path('D:/work/skills/.tasks/artifacts/GE-yzhe-20261030-184954-fdd3')
        archive = artifact / 'controller-technical-run-v2/skill-refiner'
        reconciliation = artifact / 'technical-gap-process-reconciliation.json'
        config = artifact / 'recovery-proposal/backends.proposed.json'
        if not all(p.exists() for p in (source, archive, reconciliation, config)):
            self.skipTest('real technical-gap recovery materials unavailable')
        inputs = read_json(source / 'inputs.json')
        old = read_json(source / 'agent-calls/022-analyze/response.json')
        review = read_json(source / 'agent-calls/023-analysis-review/response.json')
        new = incomplete_v4(old, inputs['standards'])  # not a claim of standard-obligation approval
        extra = [row for row in review['question_assessments'] if row['classification'] == 'unsupported_extra']
        new['blocking_questions'] = [q for q in old['blocking_questions'] if q['id'] not in {r['question_id'] for r in extra}]
        new['scope']['out_of_scope'] += [row['out_of_scope'] for row in extra]
        source_before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in source.rglob('*') if p.is_file()}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'next'
            options = ['--output', str(work), '--config', str(config), '--skill-up',
                       'C:/Users/Administrator/.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe',
                       '--parallelism', '1', '--agent-parallelism', '2',
                       '--max-rounds', '2', '--planning-attempts', '2', '--max-cases', '24',
                       '--acceptance-cases', '24', '--max-seconds', '3600', '--max-agent-calls', '32',
                       '--max-evaluations', '80']
            args = ['import-create-inputs', '--source-run', str(source), '--controller-archive', str(archive),
                    '--process-reconciliation', str(reconciliation), '--prior-seconds', '1887.702',
                    '--prior-agent-calls', '23', '--prior-evaluations', '0', '--carry-analysis-feedback',
                    '--carry-technical-clarification', *options]
            self.assertEqual(main(args), 0)
            self.assertEqual(read_json(work / 'state.json')['agent_calls'], 23)
            calls = []
            def fake_ask(agent, role, instruction, payload, schema):
                calls.append((role, copy.deepcopy(payload), agent.calls))
                if role == 'analyze':
                    self.assertEqual(agent.calls, 23)
                    agent.calls += 1
                    return copy.deepcopy(new)
                if role == 'analysis-review':
                    self.assertEqual(payload['analysis'], new)
                    raise RefinerError('offline boundary before new clarification review')
                raise AssertionError('Unexpected target evaluation or draft')
            with patch('refiner.agent.Agent.ask', fake_ask):
                self.assertEqual(main(['create', '--resume-run', str(work), '--plan-only', *options]), 2)
            self.assertEqual([row[0] for row in calls], ['analyze', 'analysis-review'])
            self.assertEqual(calls[0][1]['previous_analysis'], old)
            self.assertEqual(calls[0][1]['clarification_review'], review)
            self.assertEqual(read_json(work / 'analysis-attempt-4.json'), new)
            state = read_json(work / 'state.json')
            self.assertEqual(state['agent_calls'], 24)
            self.assertGreaterEqual(state['elapsed_seconds'], 1887.702)
            self.assertEqual(state['target_evaluations'], 0)
            self.assertEqual(state['status'], 'error')
        self.assertEqual(source_before, {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in source.rglob('*') if p.is_file()})

    def run_analysis_repair_after_clarification(self, *, repair_valid):
        clarification_fixture = ClarificationReviewTests('runTest')
        target_fixture = TargetObligationTests('runTest')
        target_fixture.setUp()
        before = copy.deepcopy(target_fixture.analysis)
        before['name'] = 'demo-skill'
        before['requirements'][0]['source'] = 'business_description: 业务要求处理影响服务的工单'
        before['blocking_questions'] = copy.deepcopy(clarification_fixture.analysis()['blocking_questions'])
        clarification_review = clarification_fixture.review([
            clarification_fixture.row('BQ1', 'required_in_scope'),
            clarification_fixture.row('BQ2', 'unsupported_extra')])
        corrected = copy.deepcopy(before)
        corrected['blocking_questions'] = [corrected['blocking_questions'][0]]
        corrected['scope']['out_of_scope'].append(clarification_review['question_assessments'][1]['out_of_scope'])
        invalid = copy.deepcopy(corrected)
        responses = [before, invalid, corrected if repair_valid else invalid]
        roles, payloads, clarification_calls = [], [], 0

        def bind_standard_inventory(analysis, frozen_sources):
            target_source = next(row for row in frozen_sources
                                 if row['content'].strip() == target_fixture.standards[0])
            target_obligation = copy.deepcopy(target_fixture.obligation)
            target_obligation['source'] = {
                'source_id': target_source['id'],
                'content_sha256': target_source['content_sha256'],
                'quote': target_fixture.standards[0],
            }
            obligations = [target_obligation]
            source_records = []
            controller_index = 0
            for source in frozen_sources:
                if source['id'] == target_source['id']:
                    ids = [target_obligation['id']]
                else:
                    controller_index += 1
                    controller_id = f'O-CTRL-{controller_index:02d}'
                    obligations.append({
                        'id': controller_id, 'subject': 'controller', 'applicable': False,
                        'applicability_reason': '该离线用例只验证目标分析修订，不评估控制器标准实现。',
                        'behavior': '控制器标准在此目标分析修订用例中不适用。',
                        'source': {'source_id': source['id'], 'content_sha256': source['content_sha256'],
                                   'quote': source['content'][:80]},
                        'acceptance_criteria': [], 'scenario_ids': [], 'workflow_ids': [],
                        'mechanism': {'state': 'not_applicable',
                                      'interface': '该控制器行为不在本离线目标用例中执行。',
                                      'evidence_strategy': '本用例只检查独立修订预算和控制器停止边界。',
                                      'gap_ids': []},
                    })
                    ids = [controller_id]
                source_records.append({'source_id': source['id'],
                                       'content_sha256': source['content_sha256'],
                                       'obligation_ids': ids})
            analysis['target_obligations'] = {
                'schema_version': 'skill-refiner.target-obligations/v1',
                'sources': source_records, 'obligations': obligations, 'coverage_gaps': []}
            return analysis

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = root / 'config.json'; work = root / 'run'
            standard_file = root / 'standard.md'
            standard_file.write_text(target_fixture.standards[0], encoding='utf-8')
            write_json(config, {'schema_version': 1,
                                'agents': {'default': {'backend': 'command', 'command': ['fixture']}},
                                'executors': [{'id': 'fake', 'backend': 'command', 'command': ['fixture']}]})
            def fake_ask(agent, role, instruction, payload, schema):
                nonlocal clarification_calls
                if agent.calls >= agent.max_calls:
                    raise RefinerError('Agent-call budget exhausted')
                agent.calls += 1
                roles.append(role); payloads.append(copy.deepcopy(payload))
                if role == 'analyze':
                    index = roles.count('analyze') - 1
                    candidate = bind_standard_inventory(copy.deepcopy(responses[index]),
                                                        payload['frozen_standard_sources'])
                    if index == 1 or (index == 2 and not repair_valid):
                        obligation = next(row for row in candidate['target_obligations']['obligations']
                                          if row['id'] == 'O-RESULT')
                        obligation['workflow_ids'] = ['W-NOT-LINKED']
                    return candidate
                if role != 'analysis-review':
                    raise AssertionError('Unexpected draft or target evaluation')
                schema_id = schema.get('$id')
                if schema_id == 'skill-refiner.clarification-review':
                    clarification_calls += 1
                    if clarification_calls == 1:
                        return copy.deepcopy(clarification_review)
                    return clarification_fixture.review([clarification_fixture.row('BQ1', 'required_in_scope')])
                if schema_id == 'skill-refiner.technical-triage':
                    return {'schema_version': 'skill-refiner.technical-triage/v1', 'assessments': [
                        {'question_id': 'BQ1', 'domain': 'business_decision',
                         'reason': '该问题涉及尚未确定的业务行为及规则。',
                         'requirement_ids': [], 'scenario_ids': []}]}
                return fixtures.AnalysisV3Tests('runTest').review(corrected)
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch('refiner.agent.Agent.ask', fake_ask):
                    result = main(['create', clarification_fixture.source, '--name', 'demo-skill', '--config', str(config),
                                   '--standard', str(standard_file),
                                   '--output', str(work), '--max-cases', '12', '--acceptance-cases', '12',
                                   '--max-agent-calls', '12', '--max-seconds', '7200'])
            finally:
                os.chdir(original_cwd)
            state = read_json(work / 'state.json')
            saved = {p.name: read_json(p) for p in work.glob('analysis-attempt-*.json')}
            return result, state, roles, payloads, saved, corrected, invalid

    def test_analysis_structure_repair_is_independent_of_clarification_repair(self):
        result, state, roles, payloads, saved, corrected, invalid = self.run_analysis_repair_after_clarification(repair_valid=True)
        self.assertEqual(result, 2)
        self.assertEqual(state['status'], 'needs_clarification')
        self.assertEqual(roles.count('analyze'), 3)
        self.assertIn('Obligation O-RESULT has inconsistent scenario/workflow links',
                      payloads[3]['validation_feedback'])
        self.assertEqual(saved['analysis-attempt-1.json']['blocking_questions'][1]['id'], 'BQ2')
        failed_obligation = next(row for row in saved['analysis-attempt-2.json']['target_obligations']['obligations']
                                 if row['id'] == 'O-RESULT')
        self.assertEqual(failed_obligation['workflow_ids'], ['W-NOT-LINKED'])
        repaired_obligation = next(row for row in saved['analysis-attempt-3.json']['target_obligations']['obligations']
                                   if row['id'] == 'O-RESULT')
        self.assertEqual(repaired_obligation['workflow_ids'], ['W1'])
        self.assertEqual([row['id'] for row in saved['analysis-attempt-3.json']['blocking_questions']], ['BQ1'])
        self.assertEqual(state['target_evaluations'], 0)

    def test_second_analysis_structure_failure_stops_after_one_independent_repair(self):
        result, state, roles, payloads, saved, corrected, invalid = self.run_analysis_repair_after_clarification(repair_valid=False)
        self.assertEqual(result, 2)
        self.assertEqual(state['status'], 'error')
        self.assertIn('Obligation O-RESULT has inconsistent scenario/workflow links', state['error'])
        self.assertEqual(roles.count('analyze'), 3)
        failed_obligation = next(row for row in saved['analysis-attempt-3.json']['target_obligations']['obligations']
                                 if row['id'] == 'O-RESULT')
        self.assertEqual(failed_obligation['workflow_ids'], ['W-NOT-LINKED'])
        self.assertEqual(state['target_evaluations'], 0)

    def run_fixture(self, mode, *, max_calls=32, expire_at=None):
        fixture = ClarificationReviewTests('runTest')
        previous = fixture.analysis()
        previous['blocking_questions'] = []
        candidate = fixture.analysis()
        candidate['goal'] += ' 使用已审核的修订。'
        review = fixture.review([fixture.row('BQ1', 'required_in_scope'),
                                 fixture.row('BQ2', 'unsupported_extra')])
        corrected = copy.deepcopy(candidate)
        corrected['blocking_questions'] = corrected['blocking_questions'][:1]
        corrected['scope']['out_of_scope'].append(review['question_assessments'][1]['out_of_scope'])
        roles, payloads = [], []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = root / 'config.json'; work = root / 'run'
            write_json(config, {'schema_version': 1, 'agents': {'default': {'backend': 'command', 'command': ['fixture']}},
                                'executors': [{'id': 'fake', 'backend': 'command', 'command': ['fixture']}]})
            def fake_ask(agent, role, instruction, payload, schema):
                if agent.max_calls is not None and agent.calls >= agent.max_calls:
                    raise RefinerError('Agent-call budget exhausted')
                if expire_at is not None and agent.calls >= expire_at:
                    agent.deadline = time.monotonic() - 1
                if agent.deadline is not None and time.monotonic() >= agent.deadline:
                    raise RefinerError('Run wall-clock budget exhausted before starting the Agent call')
                agent.calls += 1
                roles.append(role); payloads.append(copy.deepcopy(payload))
                if role == 'analyze':
                    n = roles.count('analyze')
                    return copy.deepcopy(previous if n == 1 else candidate if n == 2 else corrected)
                if role != 'analysis-review':
                    raise AssertionError('No target or draft call expected')
                if schema.get('$id') == 'skill-refiner.clarification-review':
                    if roles.count('analyze') == 2:
                        return copy.deepcopy(review)
                    return fixture.review([fixture.row('BQ1', 'unsupported_extra' if mode == 'exhaust' else 'required_in_scope')])
                if schema.get('$id') == 'skill-refiner.technical-triage':
                    return {'schema_version': 'skill-refiner.technical-triage/v1', 'assessments': [
                        {'question_id': 'BQ1', 'domain': 'business_decision',
                         'reason': '真实业务规则需要明确，保留已提供的用户决策。',
                         'requirement_ids': [], 'scenario_ids': []}]}
                return {'approved': False, 'issues': ['需要修订分析覆盖。']}
            with patch('refiner.agent.Agent.ask', fake_ask), \
                 patch('refiner.creation.validate_analysis_review'), \
                 patch('refiner.creation.validate_analysis_revision_review'):
                result = main(['create', fixture.source, '--name', 'demo-skill', '--config', str(config),
                               '--output', str(work), '--max-cases', '12', '--acceptance-cases', '12',
                               '--max-agent-calls', str(max_calls), '--max-seconds', '3600'])
            state = read_json(work / 'state.json')
            saved = {p.name: read_json(p) for p in work.glob('analysis-attempt-*.json')}
            reviews = {p.name: read_json(p) for p in work.glob('clarification-review-attempt-*.json')}
            return result, state, roles, payloads, saved, reviews, candidate, corrected

    def test_rejected_full_review_then_extra_gets_its_own_correction(self):
        result, state, roles, payloads, saved, reviews, candidate, corrected = self.run_fixture('normal')
        self.assertEqual(result, 2)  # the real BQ1 remains unresolved
        self.assertEqual(state['status'], 'needs_clarification')
        self.assertEqual(roles, ['analyze', 'analysis-review', 'analyze', 'analysis-review',
                                 'analyze', 'analysis-review', 'analysis-review'])
        self.assertEqual(payloads[4]['previous_analysis'], candidate)
        self.assertEqual(payloads[4]['clarification_review'], reviews['clarification-review-attempt-2.json'])
        self.assertEqual(saved['analysis-attempt-3.json'], corrected)
        self.assertEqual(state['agent_calls'], 7)
        self.assertEqual(state['target_evaluations'], 0)

    def test_second_unexpected_extra_stops_and_preserves_review(self):
        result, state, roles, _, _, reviews, _, _ = self.run_fixture('exhaust')
        self.assertEqual(result, 2)
        self.assertEqual(state['status'], 'error')
        self.assertIn('Clarification review did not converge', state['error'])
        self.assertEqual(roles.count('analyze'), 3)
        self.assertEqual(reviews['clarification-review-attempt-3.json']['question_assessments'][0]['classification'],
                         'unsupported_extra')
        self.assertEqual(state['agent_calls'], 6)
        self.assertEqual(state['target_evaluations'], 0)

    def test_remaining_call_and_time_limits_are_not_reset(self):
        for options in ({'max_calls': 6}, {'expire_at': 5}):
            with self.subTest(options=options):
                result, state, roles, _, _, _, _, _ = self.run_fixture('normal', **options)
                self.assertEqual(result, 2)
                self.assertEqual(state['status'], 'error')
                self.assertLessEqual(state['agent_calls'], 6)
                self.assertEqual(state['target_evaluations'], 0)
                self.assertLessEqual(len(roles), 6)


if __name__ == '__main__':
    unittest.main()

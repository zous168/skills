"""Offline protocol records for old gap + newer pending clarification recovery."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from refiner.agent import Agent
from refiner.analysis_recovery import verify_recovery
from refiner.analysis_recovery import _call
from refiner.clarification_recovery import latest_clarification, verify_clarification_recovery
from refiner.cli import main
from refiner.common import RefinerError, digest, read_json, write_json
from refiner.creation import ANALYSIS_V3
from refiner.checkpoint_migration import _authenticated_call_file
from refiner.stages import StageManifest
import test_technical_recovery as fixture


class LatestClarificationRecoveryTests(unittest.TestCase):
    def test_unauthenticated_auxiliary_whitelist_and_manifest_bound_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            call = root / 'agent-calls/008-analysis-review'
            call.mkdir(parents=True)
            manifest_path = call / 'request-manifest.json'
            raw = call / 'raw-response.txt'
            raw.write_text('invalid original response', encoding='utf-8')
            manifest = {'status': 'invalid_response', 'response_sha256': hashlib.sha256(raw.read_bytes()).hexdigest()}
            write_json(manifest_path, manifest)
            rel = manifest_path.relative_to(root).as_posix()
            stages = {rel: hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
            omissions = set()
            self.assertTrue(_authenticated_call_file(raw, root, root, stages, {}, omissions, allow_manifest_raw=True))
            raw.write_text('tampered', encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'does not match'):
                _authenticated_call_file(raw, root, root, stages, {}, omissions, allow_manifest_raw=True)
            raw.write_text('invalid original response', encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'not authenticated'):
                _authenticated_call_file(raw, root, root, {}, {}, omissions, allow_manifest_raw=True)
            with self.assertRaisesRegex(RefinerError, 'not authenticated'):
                _authenticated_call_file(raw, root, root, stages, {rel:'f'*64}, omissions, allow_manifest_raw=True)
            text='第一行中文\n第二行中文\n'
            raw.write_bytes(text.replace('\n','\r\n').encode('utf-8'))
            manifest['response_sha256']=hashlib.sha256(text.encode('utf-8')).hexdigest()
            write_json(manifest_path,manifest)
            stages[rel]=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            self.assertNotEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),manifest['response_sha256'])
            self.assertTrue(_authenticated_call_file(raw,root,root,stages,{},omissions,allow_manifest_raw=True))
            raw.write_bytes('第一行篡改\r\n第二行中文\r\n'.encode('utf-8'))
            with self.assertRaisesRegex(RefinerError,'does not match'):
                _authenticated_call_file(raw,root,root,stages,{},omissions,allow_manifest_raw=True)
            for name in ('backend-result.json', 'response.txt', 'cwd/response-schema.json'):
                auxiliary=call/name;auxiliary.parent.mkdir(exist_ok=True);auxiliary.write_text('not trusted',encoding='utf-8')
                self.assertFalse(_authenticated_call_file(auxiliary, root, root, stages, {}, omissions))
                self.assertIn(auxiliary.relative_to(root).as_posix(),omissions)
            unknown=call/'unexpected.txt';unknown.write_text('unexpected',encoding='utf-8')
            with self.assertRaisesRegex(RefinerError, 'Unknown unauthenticated'):
                _authenticated_call_file(unknown, root, root, stages, {}, omissions)
            authenticated=call/'response.json';authenticated.write_text('{}',encoding='utf-8')
            key=authenticated.relative_to(root).as_posix()
            with self.assertRaisesRegex(RefinerError, 'Authenticated analysis evidence changed'):
                _authenticated_call_file(authenticated, root, root, {}, {key:'a'*64}, omissions)

    def test_completed_call_requires_consistent_role_exit_and_finished_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "agent-calls/004-analysis-review"
            directory.mkdir(parents=True)
            request, response = {"role": "analysis-review", "input": {}}, {"approved": False}
            meta = {"role": "analysis-review", "status": "completed",
                    "request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest(),
                    "response_sha256": hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            execution = {"role": "analysis-review", "status": "completed", "exit_code": 0,
                         "finished_at": "2026-10-31T00:00:00Z"}
            process = {"status": "completed", "exit_code": 0, "finished_at": "2026-10-31T00:00:00Z"}
            for filename, value in [("request.json", request), ("response.json", response),
                                    ("request-manifest.json", meta)]:
                write_json(directory / filename, value)
            for variant in ("valid", "execution_role", "execution_exit", "execution_finished", "process_finished"):
                exe, proc = copy.deepcopy(execution), copy.deepcopy(process)
                if variant == "execution_role":
                    exe["role"] = "analyze"
                elif variant == "execution_exit":
                    exe["exit_code"] = 7
                elif variant == "execution_finished":
                    exe["finished_at"] = None
                elif variant == "process_finished":
                    proc["finished_at"] = None
                write_json(directory / "execution.json", exe)
                write_json(directory / "process.execution.json", proc)
                recorded = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in directory.iterdir() if p.is_file()}
                if variant == "valid":
                    self.assertEqual(_call(root, directory, "analysis-review", recorded), (request, response))
                else:
                    with self.subTest(variant=variant), self.assertRaises(RefinerError):
                        _call(root, directory, "analysis-review", recorded)
            write_json(directory / "execution.json", execution)
            write_json(directory / "process.execution.json", process)
            recorded = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in directory.iterdir() if p.is_file()}
            (directory / "process.execution.json").unlink()
            with self.assertRaisesRegex(RefinerError, "Recorded analysis process evidence is missing"):
                _call(root, directory, "analysis-review", recorded)

    def test_old_gap_new_pending_chain_and_budget_survive_migration_and_revalidation(self):
        setup = fixture.TechnicalRecoveryTests("test_changed_controller_migration_preserves_derived_review_and_gap")
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        # Build real Agent request/response records, including a rejected v4
        # review and a charged failed call, without any model or target execution.
        setup.test_changed_controller_migration_preserves_derived_review_and_gap()
        f = setup.fixture
        source = f.out
        inputs, contract = read_json(source / "inputs.json"), read_json(source / "creation-resume-contract.json")
        history = verify_recovery(source, contract, inputs)
        self.assertEqual(len(history), 3)
        candidate = copy.deepcopy(setup.candidate)
        candidate["blocking_questions"] = [
            {"id": "B1", "question": "目标的标准验证及自审载体如何实现？", "decision_owner": "标准与实现负责人",
             "impact": "标准义务仍需独立核实，不能由仅业务审查予以豁免。"},
            {"id": "B2", "question": "真实执行范围的全部动作能否独立观察？", "decision_owner": "执行装置负责人",
             "impact": "原始无副作用要求的证据缺口保持未解决。"},
            {"id": "B3", "question": "实际完整案例矩阵是否超过已冻结容量？", "decision_owner": "规划负责人",
             "impact": "先核对完整需求与案例计数，不得自行删除场景。"}]
        clarification = {"schema_version": "skill-refiner.clarification-review/v1", "question_assessments": [
            {"question_id": qid, "classification": classification,
             "evidence": [{"source_id": "business_description", "quote": "业务需求"}],
             "reason": "该合成审查仅检查业务来源；标准义务必须另行检查，未决证据不得伪称解决。",
             "combination_risk_assessed": True, "required_requirement_ids_examined": ["R1"],
             "out_of_scope": "业务来源未赋予这一目标接口；独立标准审查仍须保留。" if classification == "unsupported_extra" else ""}
            for qid, classification in [("B1", "unsupported_extra"), ("B2", "required_in_scope"), ("B3", "uncertain")]]}
        sources = [{"id": "business_description", "content": inputs["business_description"]}]
        stage = StageManifest(source, "create", resume=True)
        stage.activate("analysis")
        agent = Agent(source, config=read_json(f.config), inherited_calls=11)
        replies = iter([candidate, clarification])
        with patch("refiner.agent.run_process", side_effect=lambda *a, **k: (0, json.dumps(next(replies), ensure_ascii=False), "", .01)):
            agent.ask("analyze", "new pending fixture", {
                **inputs, "business_sources": sources, "previous_analysis": history[-1]["analysis"],
                "previous_analysis_reviews": [row["review"] for row in history]}, ANALYSIS_V3)
            agent.ask("analysis-review", "new pending clarification", {"business_sources": sources, "analysis": candidate},
                      read_json(ROOT / "schemas/v1/clarification-review.schema.json"))
        stage.finish("error")
        for call in ('012-analyze', '013-analysis-review'):
            directory = source / 'agent-calls' / call
            (directory/'backend-result.json').write_text('unverified backend metadata',encoding='utf-8')
            (directory/'response.txt').write_text('unverified response text',encoding='utf-8')
            (directory/'cwd').mkdir(exist_ok=True)
            (directory/'cwd/response-schema.json').write_text('unverified schema',encoding='utf-8')
        state = read_json(source / "state.json")
        state.update(agent_calls=13, creation_agent_calls=13, status="error")
        write_json(source / "state.json", state)
        old_gap = read_json(source / "technical-coverage.json")
        selected = latest_clarification(source, inputs, history, source_manifest=read_json(source / "stages.json"))
        self.assertEqual(selected["calls"], ["012-analyze", "013-analysis-review"])
        self.assertEqual(selected["pair"]["analysis"], candidate)
        destination = f.base / "latest-continuation"
        options = [str(destination) if value == str(source) else value for value in f.options]
        evidence = f.base / "latest-reconciliation.json"
        write_json(evidence, {"query": "processes for " + source.name, "exit_code": 0, "processes": [],
                             "checked_at": "2026-10-31T00:00:00Z", "historical_evidence": "offline fixture terminated",
                             "limitation": "Local protocol fixture only, no model or target execution"})
        arguments = ["import-create-inputs", "--source-run", str(source),
            "--controller-archive", str(f.base / "previous-controller"),
            "--process-reconciliation", str(evidence), "--prior-seconds", str(state["elapsed_seconds"] + 1),
            "--prior-agent-calls", "13", "--prior-evaluations", "0",
            "--carry-analysis-feedback", "--carry-technical-clarification"] + options
        unknown = source/'agent-calls/012-analyze/unexpected.bin'
        unknown.write_bytes(b'unknown source file')
        self.assertEqual(main(arguments), 2)
        self.assertFalse(destination.exists())
        unknown.unlink()
        unknown_provenance = source/'analysis-previous/unsealed-extra.json'
        unknown_provenance.write_text('{}',encoding='utf-8')
        self.assertEqual(main(arguments), 2)
        self.assertFalse(destination.exists())
        unknown_provenance.unlink()
        authenticated = source/'agent-calls/013-analysis-review/response.json'
        saved = authenticated.read_bytes()
        authenticated.write_text('{}',encoding='utf-8')
        self.assertEqual(main(arguments), 2)
        self.assertFalse(destination.exists())
        authenticated.write_bytes(saved)
        before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in source.rglob("*") if p.is_file()}
        self.assertEqual(main(arguments), 0)
        omissions = read_json(destination / 'analysis-previous/unsealed-omissions.json')
        self.assertIn('not an original stage attestation', omissions['provenance'])
        for call in ('012-analyze', '013-analysis-review'):
            for name in ('backend-result.json', 'response.txt', 'cwd/response-schema.json'):
                relative=f'agent-calls/{call}/{name}'
                self.assertIn(relative, omissions['paths'])
                self.assertFalse((destination/relative).exists())
                self.assertFalse((destination/'analysis-previous/latest-clarification'/relative).exists())
        self.assertEqual(before, {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in source.rglob("*") if p.is_file()})
        new_contract, new_inputs = read_json(destination / "creation-resume-contract.json"), read_json(destination / "inputs.json")
        new_history = verify_recovery(destination, new_contract, new_inputs)
        omission_file = destination/'analysis-previous/unsealed-omissions.json'
        omission_bytes = omission_file.read_bytes()
        omission_file.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(RefinerError, 'Analysis recovery evidence changed'):
            verify_recovery(destination,new_contract,new_inputs)
        omission_file.write_bytes(omission_bytes)
        latest = verify_clarification_recovery(destination, new_contract, new_inputs, new_history)
        self.assertEqual(latest["pair"]["analysis"], candidate)
        self.assertEqual(latest["pair"]["review"], clarification)
        self.assertEqual(read_json(destination / "technical-coverage.json"), old_gap)
        self.assertEqual(read_json(destination / "state.json")["agent_calls"], 13)
        # A freshly imported run has no new analysis outputs. Its old root-call
        # copies and latest carrier are nevertheless sealed by recovery hashes;
        # immediately migrating it again must not require a fictitious stage run.
        twice = f.base / "latest-twice"
        second_options = [str(twice) if value == str(destination) else value for value in options]
        second_evidence = f.base / "latest-second-reconciliation.json"
        write_json(second_evidence, {"query": "processes for " + destination.name, "exit_code": 0, "processes": [],
                                    "checked_at": "2026-10-31T00:00:00Z",
                                    "historical_evidence": "No new call; sealed inherited fixture only",
                                    "limitation": "Offline migration control, no model"})
        sealed_before = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in destination.rglob("*") if p.is_file()}
        second_arguments = ["import-create-inputs", "--source-run", str(destination),
            "--controller-archive", str(ROOT), "--process-reconciliation", str(second_evidence),
            "--prior-seconds", str(read_json(destination / "state.json")["elapsed_seconds"] + 1),
            "--prior-agent-calls", "13", "--prior-evaluations", "0",
            "--carry-analysis-feedback", "--carry-technical-clarification"] + second_options
        self.assertEqual(main(second_arguments), 0)
        self.assertEqual(sealed_before, {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in destination.rglob("*") if p.is_file()})
        twice_contract, twice_inputs = read_json(twice / "creation-resume-contract.json"), read_json(twice / "inputs.json")
        twice_history = verify_recovery(twice, twice_contract, twice_inputs)
        self.assertEqual(len(twice_history), 3)
        twice_latest = verify_clarification_recovery(twice, twice_contract, twice_inputs, twice_history)
        self.assertEqual(twice_latest["pair"], latest["pair"])
        self.assertEqual(read_json(twice / "technical-coverage.json"), old_gap)
        self.assertEqual(read_json(twice / "state.json")["agent_calls"], 13)
        self.assertEqual(read_json(twice / "state.json")["target_evaluations"], 0)
        # The sealed boundary is not a blanket exemption for genuinely new calls.
        unknown = destination / "agent-calls/014-analyze"
        unknown.mkdir()
        try:
            for filename in ("request.json", "request-manifest.json", "response.json", "execution.json"):
                (unknown / filename).write_bytes((destination / "agent-calls/012-analyze" / filename).read_bytes())
            with self.assertRaisesRegex(RefinerError, "source stage evidence"):
                latest_clarification(destination, new_inputs, new_history,
                    source_manifest=read_json(destination / "stages.json"),
                    validated_clarification_boundary=13)
        finally:
            for path in unknown.iterdir():
                path.unlink()
            unknown.rmdir()
        # Old root-call copies skipped by active scanning still need their
        # immutable recovery hash, independently of the carrier's second copy.
        old_call_response = destination / "agent-calls/012-analyze/response.json"
        old_bytes = old_call_response.read_bytes()
        try:
            old_call_response.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RefinerError, "Analysis recovery evidence changed"):
                verify_recovery(destination, new_contract, new_inputs)
        finally:
            old_call_response.write_bytes(old_bytes)
        seen = []
        def inspect(agent, role, instruction, payload, schema):
            seen.append((role, payload, agent.calls))
            raise RefinerError("offline context inspection, no model")
        with patch("refiner.agent.Agent.ask", inspect):
            self.assertEqual(main(["create", "--resume-run", str(destination)] + options), 2)
        self.assertEqual(seen[0][0], "analyze")
        payload = seen[0][1]
        self.assertEqual(payload["previous_clarification_history"]["analysis"], candidate)
        self.assertEqual(payload["previous_clarification_history"]["review"], clarification)
        self.assertEqual(payload["previous_analysis"], candidate)
        self.assertEqual(payload["clarification_review"], clarification)
        self.assertEqual({q["id"] for q in payload["retained_clarification_questions"]}, {"B2", "B3"})
        self.assertEqual(payload["technical_coverage"], old_gap)
        self.assertEqual(len(payload["previous_analysis_reviews"]), 3)
        self.assertEqual(seen[0][2], 13)
        self.assertIn("Latest pending clarification (B1, unsupported_extra)", str(payload["validation_feedback"]))
        def drop_questions(*args, **kwargs):
            request = json.loads(kwargs["input_text"])
            self.assertEqual(request["role"], "analyze", "No review or draft can follow lost pending questions")
            value = fixture.incomplete_v4(candidate, request["input"]["standards"])
            value["goal"] += " New wording that has not previously received independent review."
            return 0, json.dumps(value, ensure_ascii=False), "", .01
        with patch("refiner.agent.run_process", side_effect=drop_questions):
            self.assertEqual(main(["create", "--resume-run", str(destination)] + options), 2)
        stopped = read_json(destination / "state.json")
        self.assertIn("Latest required/uncertain clarification questions must be retained", stopped["error"])
        # A malformed correction is rejected immediately, with its one call
        # charged; an unbounded second correction must not be scheduled.
        self.assertEqual(stopped["agent_calls"], 14)
        self.assertGreaterEqual(stopped["elapsed_seconds"], 346.671)
        self.assertEqual(stopped["target_evaluations"], 0)
        self.assertFalse((destination / "drafts").exists())
        # Every recovery revalidates the archive before a controller call.
        copied = destination / "analysis-previous/latest-clarification/agent-calls/013-analysis-review/response.json"
        copied.write_text("{}", encoding="utf-8")
        with patch("refiner.agent.Agent.ask") as ask:
            self.assertEqual(main(["create", "--resume-run", str(destination)] + options), 2)
            ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()

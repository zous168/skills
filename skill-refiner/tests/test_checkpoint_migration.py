"""Offline checkpoint import and continuation tests. No real backend is called."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from refiner.cli import main
from refiner.agent import Agent
from refiner.common import RefinerError, digest, read_json, write_json
from refiner.checkpoint_migration import _controller_hash, _source_artifact_path, verify_migration
from refiner.stages import StageManifest
from refiner.standards import AUTHORING
from refiner.creation import ANALYSIS_V3 as ANALYSIS, ANALYSIS_REVIEW, analysis_revision_delta, validate_analysis_revision_review


class CheckpointMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = self.base / "old"
        self.old.mkdir()
        self.out = self.base / "continuation"
        self.config = self.base / "config.json"
        write_json(self.config, {"schema_version": 1,
                                "agents": {"default": {"backend": "command", "command": ["never-executed"]}},
                                "executors": [{"id": "fake", "backend": "command", "command": ["never-executed"]}]})
        inputs = {"business_description": "业务需求", "requested_name": "example-skill", "requirements": [], "standards": [AUTHORING]}
        write_json(self.old / "inputs.json", inputs)
        (self.old / "inputs").mkdir()
        (self.old / "inputs" / "business-request.md").write_text("业务需求\n", encoding="utf-8")
        stage = StageManifest(self.old, "create")
        stage.complete("inputs")
        write_json(self.old / "creation-resume-contract.json", {"schema_version": "skill-refiner.creation-resume/v1",
                   "inputs_sha256": digest(inputs), "controller_sha256": _controller_hash(ROOT), "destination": None})
        write_json(self.old / "state.json", {"mode": "create", "status": "error", "elapsed_seconds": 10,
                   "agent_calls": 2, "target_evaluations": 0})
        self.evidence = self.base / "reconciliation.json"
        write_json(self.evidence, {"query": "processes for old", "exit_code": 0, "processes": [],
                   "checked_at": "2026-10-30T00:00:00Z", "historical_evidence": "controlled fixture terminated",
                   "limitation": "fixture only"})
        self.options = ["--output", str(self.out), "--config", str(self.config), "--max-seconds", "3600",
                        "--max-agent-calls", "32", "--max-evaluations", "80", "--max-rounds", "2",
                         "--planning-attempts", "2", "--max-cases", "12", "--acceptance-cases", "12",
                         "--parallelism", "1", "--agent-parallelism", "2"]
        self.import_cmd = ["import-create-inputs", "--source-run", str(self.old), "--controller-archive", str(ROOT),
                           "--process-reconciliation", str(self.evidence),
                           "--prior-seconds", "346.671", "--prior-agent-calls", "4", "--prior-evaluations", "0"] + self.options

    def test_import_and_resume_analysis_without_rewriting_inputs_or_fake_call_dirs(self):
        old_hashes = {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.old.rglob("*") if p.is_file()}
        self.assertEqual(main(self.import_cmd), 0)
        self.assertEqual(old_hashes, {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in self.old.rglob("*") if p.is_file()})
        before = (self.out / "inputs.json").read_bytes(), (self.out / "inputs/business-request.md").read_bytes()
        self.assertFalse((self.out / "agent-calls").exists())
        seen = []
        def fake_ask(agent, role, *args, **kwargs):
            seen.append((role, agent.calls))
            raise RefinerError("offline controlled stop")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(seen, [("analyze", 4)])
        self.assertEqual(before, ((self.out / "inputs.json").read_bytes(), (self.out / "inputs/business-request.md").read_bytes()))
        stages = read_json(self.out / "stages.json")["stages"]
        self.assertEqual(stages[0]["status"], "completed")
        self.assertNotEqual(stages[1]["status"], "completed")
        state = read_json(self.out / "state.json")
        self.assertGreaterEqual(state["elapsed_seconds"], 346.671)
        self.assertEqual(state["agent_calls"], 4)

    def test_rejects_modified_or_added_inputs(self):
        (self.old / "inputs/business-request.md").write_text("变更", encoding="utf-8")
        self.assertEqual(main(self.import_cmd), 2)
        (self.old / "inputs/business-request.md").write_text("业务需求\n", encoding="utf-8")
        (self.old / "inputs/extra.md").write_text("extra", encoding="utf-8")
        self.assertEqual(main(self.import_cmd), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_unsettled_or_hidden_evaluation_evidence(self):
        hidden = self.old / "refinement/baseline/attempt-1/evaluation-cost.json"
        hidden.parent.mkdir(parents=True)
        write_json(hidden, {"status": "running"})
        self.assertEqual(main(self.import_cmd), 2)
        self.assertFalse(self.out.exists())

    def test_call_reservation_starts_at_five_and_survives_resume(self):
        self.assertEqual(main(self.import_cmd), 0)
        config = read_json(self.config)
        agent = Agent(self.out, config=config, inherited_calls=4)
        agent.max_calls = 32
        with patch("refiner.agent.run_process", return_value=(0, "{}", "", 0.01)):
            self.assertEqual(agent.ask("analyze", "test", {}, {"type": "object"}), {})
        self.assertTrue((self.out / "agent-calls/005-analyze").is_dir())
        self.assertFalse((self.out / "agent-calls/001-analyze").exists())
        self.assertEqual(Agent(self.out, config=config, inherited_calls=4).calls, 5)

    def test_rejects_missing_or_incomplete_checkpoint(self):
        manifest = read_json(self.old / "stages.json")
        manifest["stages"][0]["status"] = "pending"
        write_json(self.old / "stages.json", manifest)
        self.assertEqual(main(self.import_cmd), 2)
        (self.old / "stages.json").unlink()
        self.assertEqual(main(self.import_cmd), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_wrong_controller(self):
        contract = read_json(self.old / "creation-resume-contract.json")
        contract["controller_sha256"] = "0" * 64
        write_json(self.old / "creation-resume-contract.json", contract)
        self.assertEqual(main(self.import_cmd), 2)

    def test_revalidates_only_completed_inputs_when_source_archive_is_unavailable(self):
        contract = read_json(self.old / "creation-resume-contract.json")
        contract["controller_sha256"] = "0" * 64
        write_json(self.old / "creation-resume-contract.json", contract)
        command = list(self.import_cmd)
        archive_index = command.index("--controller-archive")
        del command[archive_index:archive_index + 2]
        command.insert(1, "--revalidate-source-checkpoint")
        self.assertEqual(main(command), 0)
        migration = read_json(self.out / "input-migration.json")
        self.assertEqual(migration["schema_version"], "skill-refiner.checkpoint-migration/v3")
        self.assertIsNone(migration["controller_archive"])
        self.assertEqual(migration["controller_attestation"]["archive_status"], "unavailable")
        self.assertEqual(migration["controller_attestation"]["validated_completed_stage_ids"], ["inputs"])
        self.assertEqual(migration["controller_attestation"]["untrusted_stage_ids"],
                         ["analysis", "draft", "refinement", "delivery", "verification"])
        self.assertEqual(migration["source_controller_sha256"], "0" * 64)
        self.assertEqual(read_json(self.out / "inputs.json"), read_json(self.old / "inputs.json"))
        self.assertEqual(read_json(self.out / "creation-resume-contract.json")["controller_sha256"], _controller_hash(ROOT))

        seen = []
        def fake_ask(agent, role, *args, **kwargs):
            seen.append((role, agent.calls))
            raise RefinerError("offline controlled stop")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(seen, [("analyze", 4)])
        self.assertEqual(read_json(self.out / "stages.json")["stages"][0]["status"], "completed")

    def test_chained_revalidation_accepts_consistent_historical_controller_attestation(self):
        first = list(self.import_cmd)
        archive_index = first.index("--controller-archive")
        del first[archive_index:archive_index + 2]
        first.insert(1, "--revalidate-source-checkpoint")
        self.assertEqual(main(first), 0)

        migration_path = self.out / "input-migration.json"
        migration = read_json(migration_path)
        contract_path = self.out / "creation-resume-contract.json"
        contract = read_json(contract_path)
        historical_controller = "1" * 64
        migration["controller_attestation"]["validation_controller_sha256"] = historical_controller
        contract["controller_sha256"] = historical_controller
        contract["input_migration_sha256"] = digest(migration)
        write_json(migration_path, migration)
        write_json(contract_path, contract)

        next_output = self.base / "continuation-v2"
        next_evidence = self.base / "continuation-reconciliation.json"
        write_json(next_evidence, {"query": "processes for continuation", "exit_code": 0, "processes": [],
                   "checked_at": "2026-10-30T00:01:00Z", "historical_evidence": "fixture process terminated",
                   "limitation": "fixture only"})
        next_options = list(self.options)
        next_options[next_options.index("--output") + 1] = str(next_output)
        second = ["import-create-inputs", "--source-run", str(self.out), "--revalidate-source-checkpoint",
                  "--process-reconciliation", str(next_evidence), "--prior-seconds", "346.671",
                  "--prior-agent-calls", "4", "--prior-evaluations", "0"] + next_options

        self.assertEqual(main(second), 0)
        next_migration = read_json(next_output / "input-migration.json")
        self.assertEqual(next_migration["controller_attestation"]["validation_controller_sha256"],
                         _controller_hash(ROOT))
        self.assertEqual(read_json(next_output / "inputs.json"), read_json(self.old / "inputs.json"))

    def test_revalidation_rebuilds_inputs_with_current_schema_hashes(self):
        manifest = read_json(self.old / "stages.json")
        for reference in manifest["schema_refs"]:
            reference["sha256"] = "0" * 64
        inputs_stage = manifest["stages"][0]
        for field in ("inputs", "outputs"):
            for declaration in inputs_stage[field]:
                if isinstance(declaration.get("schema_ref"), dict):
                    declaration["schema_ref"]["sha256"] = "0" * 64
        for field in ("artifacts", "input_artifacts"):
            for artifact in inputs_stage.get(field, []):
                if isinstance(artifact.get("schema_ref"), dict):
                    artifact["schema_ref"]["sha256"] = "0" * 64
        write_json(self.old / "stages.json", manifest)
        source_hashes = {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in self.old.rglob("*") if p.is_file()}

        command = list(self.import_cmd)
        archive_index = command.index("--controller-archive")
        del command[archive_index:archive_index + 2]
        command.insert(1, "--revalidate-source-checkpoint")
        self.assertEqual(main(command), 0)

        migration = read_json(self.out / "input-migration.json")
        self.assertEqual(migration["controller_attestation"]["validated_completed_stage_ids"], ["inputs"])
        self.assertEqual(migration["controller_attestation"]["untrusted_stage_ids"],
                         ["analysis", "draft", "refinement", "delivery", "verification"])
        self.assertEqual(read_json(self.out / "inputs.json"), read_json(self.old / "inputs.json"))
        self.assertEqual(read_json(self.out / "stages.json")["stages"][0]["status"], "completed")
        self.assertEqual(source_hashes,
                         {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in self.old.rglob("*") if p.is_file()})

    def test_revalidation_rejects_changed_inputs_stage_contract(self):
        manifest = read_json(self.old / "stages.json")
        manifest["stages"][0]["outputs"][0]["path"] = "renamed-inputs.json"
        write_json(self.old / "stages.json", manifest)
        command = list(self.import_cmd)
        archive_index = command.index("--controller-archive")
        del command[archive_index:archive_index + 2]
        command.insert(1, "--revalidate-source-checkpoint")
        self.assertEqual(main(command), 2)
        self.assertFalse(self.out.exists())

    def test_revalidation_does_not_accept_tampered_completed_input(self):
        contract = read_json(self.old / "creation-resume-contract.json")
        contract["controller_sha256"] = "0" * 64
        write_json(self.old / "creation-resume-contract.json", contract)
        (self.old / "inputs/business-request.md").write_text("changed input\n", encoding="utf-8")
        command = list(self.import_cmd)
        archive_index = command.index("--controller-archive")
        del command[archive_index:archive_index + 2]
        command.insert(1, "--revalidate-source-checkpoint")
        self.assertEqual(main(command), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_understated_budget_and_tampered_provenance(self):
        cmd = copy.copy(self.import_cmd)
        cmd[cmd.index("346.671")] = "9"
        self.assertEqual(main(cmd), 2)
        cmd = copy.copy(self.import_cmd)
        cmd[cmd.index("4")] = "1"
        self.assertEqual(main(cmd), 2)
        self.assertEqual(main(self.import_cmd), 0)
        state = read_json(self.out / "state.json")
        state["elapsed_seconds"] = 1
        write_json(self.out / "state.json", state)
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)

    def test_rejects_schema_tampering_and_nonisolated_output(self):
        manifest = read_json(self.old / "stages.json")
        manifest["schema_refs"][0]["sha256"] = "0" * 64
        write_json(self.old / "stages.json", manifest)
        self.assertEqual(main(self.import_cmd), 2)
        cmd = copy.copy(self.import_cmd)
        cmd[cmd.index(str(self.out))] = str(self.old / "nested")
        self.assertEqual(main(cmd), 2)

    def test_rejects_missing_or_unreconciled_process_evidence(self):
        cmd = copy.copy(self.import_cmd)
        idx = cmd.index("--process-reconciliation")
        del cmd[idx:idx + 2]
        self.assertEqual(main(cmd), 2)
        evidence = read_json(self.evidence)
        evidence["processes"] = [{"pid": 123}]
        write_json(self.evidence, evidence)
        self.assertEqual(main(self.import_cmd), 2)
        self.assertFalse(self.out.exists())

    def test_budget_change_is_rejected_before_mutating_migration_contract(self):
        self.assertEqual(main(self.import_cmd), 0)
        before = (self.out / "creation-resume-contract.json").read_bytes()
        changed = copy.copy(self.options)
        changed[changed.index("32")] = "33"
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + changed), 2)
        self.assertEqual(before, (self.out / "creation-resume-contract.json").read_bytes())

    def _supplement_command(self):
        supplemental = self.base / "user-decision.md"
        supplemental.write_bytes("用户明确决定：保留旧规则，并补充工单未定义时输出待确认。\r\n".encode("utf-8"))
        return supplemental, self.import_cmd + ["--supplement", str(supplemental),
                                                "--supplement-source", "用户回答：decision-1"]

    def test_append_preserves_old_inputs_and_resumes_new_business_analysis(self):
        inputs = read_json(self.old / "inputs.json")
        inputs["requirements"] = [{"source": str(self.base / "original.md"), "content": "原有要求，不得变更。\n"}]
        write_json(self.old / "inputs.json", inputs)
        (self.old / "inputs/requirement-1.md").write_text(inputs["requirements"][0]["content"], encoding="utf-8")
        stage = StageManifest(self.old, "create")
        stage.complete("inputs")
        old_contract = read_json(self.old / "creation-resume-contract.json")
        old_contract["inputs_sha256"] = digest(inputs)
        write_json(self.old / "creation-resume-contract.json", old_contract)
        old_hashes = {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.old.rglob("*") if p.is_file()}
        supplement, cmd = self._supplement_command()
        self.assertEqual(main(cmd), 0)
        self.assertEqual(old_hashes, {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in self.old.rglob("*") if p.is_file()})
        record = read_json(self.out / "input-migration.json")
        self.assertEqual(record["schema_version"], "skill-refiner.checkpoint-migration/v2")
        self.assertEqual(record["mode"], "append-user-requirement")
        self.assertEqual(record["source_inputs_sha256"], digest(inputs))
        self.assertNotEqual(record["source_inputs_sha256"], record["inputs_sha256"])
        self.assertEqual((self.out / "provenance/source-inputs.json").read_bytes(), (self.old / "inputs.json").read_bytes())
        self.assertEqual((self.out / "inputs/requirement-1.md").read_bytes(), (self.old / "inputs/requirement-1.md").read_bytes())
        self.assertEqual((self.out / "inputs/requirement-2.md").read_bytes(), supplement.read_bytes())
        new_inputs = read_json(self.out / "inputs.json")
        self.assertEqual(new_inputs["requirements"][:1], inputs["requirements"])
        self.assertEqual(new_inputs["requirements"][1]["source"], str(supplement.resolve()))
        self.assertEqual(record["supplement"]["decision_source"], "用户回答：decision-1")
        self.assertEqual(record["inherited"], {"seconds": 346.671, "agent_calls": 4, "evaluations": 0})
        received = []
        def fake_ask(agent, role, instruction, payload, schema):
            received.append((role, agent.calls, payload))
            raise RefinerError("offline controlled stop")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(received[0][:2], ("analyze", 4))
        self.assertEqual([row["id"] for row in received[0][2]["business_sources"]],
                         ["business_description", "spec-1", "spec-2"])
        stages = read_json(self.out / "stages.json")["stages"]
        self.assertEqual(stages[0]["status"], "completed")
        self.assertNotEqual(stages[1]["status"], "completed")

    def test_supplement_requires_decision_source_and_rejects_unsafe_inputs(self):
        supplement, cmd = self._supplement_command()
        no_source = cmd[:cmd.index("--supplement-source")]
        self.assertEqual(main(no_source), 2)
        supplement.write_bytes(b"\xff\xfe")
        self.assertEqual(main(cmd), 2)
        supplement.write_text("  \n", encoding="utf-8")
        self.assertEqual(main(cmd), 2)
        self.assertFalse(self.out.exists())

    def test_derived_check_rejects_tampering_and_nonzero_prior_evaluations(self):
        supplement, cmd = self._supplement_command()
        with_eval = copy.copy(cmd)
        with_eval[with_eval.index("--prior-evaluations") + 1] = "1"
        self.assertEqual(main(with_eval), 2)
        state = read_json(self.old / "state.json")
        state["target_evaluations"] = 1
        write_json(self.old / "state.json", state)
        self.assertEqual(main(cmd), 2)
        state["target_evaluations"] = 0
        write_json(self.old / "state.json", state)
        self.assertEqual(main(cmd), 0)
        path = self.out / "inputs/requirement-1.md"
        path.write_text("篡改", encoding="utf-8")
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)

    def test_original_input_snapshot_tampering_is_rejected_before_execution(self):
        _, cmd = self._supplement_command()
        self.assertEqual(main(cmd), 0)
        path = self.out / "provenance/source-inputs.json"
        data = read_json(path)
        data["business_description"] = "篡改旧输入"
        write_json(path, data)
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)

    def test_rebinding_hashes_cannot_change_an_old_requirement(self):
        _, cmd = self._supplement_command()
        self.assertEqual(main(cmd), 0)
        inputs = read_json(self.out / "inputs.json")
        inputs["business_description"] = "替换原始业务描述"
        write_json(self.out / "inputs.json", inputs)
        record = read_json(self.out / "input-migration.json")
        record["inputs_sha256"] = digest(inputs)
        record["artifacts"]["inputs.json"] = hashlib.sha256((self.out / "inputs.json").read_bytes()).hexdigest()
        write_json(self.out / "input-migration.json", record)
        contract = read_json(self.out / "creation-resume-contract.json")
        contract["inputs_sha256"] = record["inputs_sha256"]
        contract["input_migration_sha256"] = digest(record)
        write_json(self.out / "creation-resume-contract.json", contract)
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)

    def test_chained_append_validates_prior_provenance_and_does_not_duplicate(self):
        first_supplement, first_cmd = self._supplement_command()
        self.assertEqual(main(first_cmd), 0)
        second = self.base / "second-continuation"
        evidence = self.base / "second-reconciliation.json"
        write_json(evidence, {"query": "processes for continuation", "exit_code": 0, "processes": [],
                              "checked_at": "2026-10-30T00:00:00Z", "historical_evidence": "fixture terminated",
                              "limitation": "fixture only"})
        cmd = copy.copy(first_cmd)
        cmd[cmd.index(str(self.old))] = str(self.out)
        cmd[cmd.index(str(self.evidence))] = str(evidence)
        cmd[cmd.index(str(self.out), cmd.index("--output"))] = str(second)
        self.assertEqual(main(cmd), 2)
        self.assertFalse(second.exists())
        another = self.base / "second-decision.md"
        another.write_text("用户的第二项独立业务补充。", encoding="utf-8")
        cmd[cmd.index(str(first_supplement))] = str(another)
        before = {p.relative_to(self.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.out.rglob("*") if p.is_file()}
        self.assertEqual(main(cmd), 0)
        after = {p.relative_to(self.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.out.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        new_inputs = read_json(second / "inputs.json")
        self.assertEqual(len(new_inputs["requirements"]), 2)
        self.assertEqual(new_inputs["requirements"][0]["source"], str(first_supplement.resolve()))
        self.assertEqual(new_inputs["requirements"][1]["source"], str(another.resolve()))
        self.assertEqual(read_json(second / "state.json")["agent_calls"], 4)

    def test_supplement_symlink_is_rejected(self):
        supplement, cmd = self._supplement_command()
        link = self.base / "decision-link.md"
        try:
            link.symlink_to(supplement)
        except (OSError, NotImplementedError):
            self.skipTest("File symlinks unavailable on this host")
        cmd[cmd.index(str(supplement))] = str(link)
        self.assertEqual(main(cmd), 2)
        self.assertFalse(self.out.exists())

    def test_migration_chain_rejects_tampered_old_provenance(self):
        _, first = self._supplement_command()
        self.assertEqual(main(first), 0)
        source_snapshot = self.out / "provenance/source-inputs.json"
        source_snapshot.write_text("{}", encoding="utf-8")
        second = self.base / "second"
        new_decision = self.base / "another.md"
        new_decision.write_text("另一条用户决定，测试夹具。", encoding="utf-8")
        evidence = self.base / "second-reconciliation.json"
        write_json(evidence, {"query": "processes for continuation", "exit_code": 0, "processes": [],
                              "checked_at": "2026-10-30T00:00:00Z", "historical_evidence": "fixture terminated",
                              "limitation": "fixture only"})
        cmd = copy.copy(first)
        cmd[cmd.index(str(self.old))] = str(self.out)
        cmd[cmd.index(str(self.evidence))] = str(evidence)
        cmd[cmd.index(str(self.out), cmd.index("--output"))] = str(second)
        cmd[cmd.index("--supplement") + 1] = str(new_decision)
        self.assertEqual(main(cmd), 2)
        self.assertFalse(second.exists())

    def test_invalid_source_and_resume_budget_values_are_rejected(self):
        _, cmd = self._supplement_command()
        original = read_json(self.old / "state.json")
        for field, value in (("elapsed_seconds", float("nan")), ("elapsed_seconds", float("inf")),
                             ("elapsed_seconds", -1), ("agent_calls", 1.5), ("agent_calls", True),
                             ("creation_agent_calls", 3), ("target_evaluations", -1), ("target_evaluations", 0.5)):
            with self.subTest(source_field=field, value=value):
                state = copy.deepcopy(original)
                state[field] = value
                write_json(self.old / "state.json", state)
                self.assertEqual(main(cmd), 2)
                self.assertFalse(self.out.exists())
        write_json(self.old / "state.json", original)
        self.assertEqual(main(cmd), 0)
        contract = read_json(self.out / "creation-resume-contract.json")
        state = read_json(self.out / "state.json")
        for field, value in (("elapsed_seconds", float("nan")), ("elapsed_seconds", float("inf")),
                             ("agent_calls", 4.5), ("creation_agent_calls", True), ("target_evaluations", -1)):
            with self.subTest(resume_field=field, value=value):
                invalid = copy.deepcopy(state)
                invalid[field] = value
                with self.assertRaisesRegex(RefinerError, "finite nonnegative"):
                    verify_migration(self.out, contract, invalid)

    def _rejected_analysis_fixture(self, invalid_unreviewed_latest=False, valid_unreviewed_latest=False):
        import test_analysis_contract_v3 as fixture
        inputs = read_json(self.old / "inputs.json")
        first = fixture.AnalysisV3Tests("runTest").analysis()
        first["name"] = inputs["requested_name"]
        first["requirements"][0]["source"] = "business_description: 业务需求"
        second = copy.deepcopy(first)
        second["goal"] += " Revision adds concrete detail."
        review1, review2 = fixture.AnalysisV3Tests("runTest").review(first), fixture.AnalysisV3Tests("runTest").review(second)
        review1.update(approved=False, issues=["First issue", "Second issue"])
        review2.update(approved=False, issues=["Third issue", "Fourth issue", "Fifth issue"])
        third = None
        responses = [first, review1, second, review2]
        if invalid_unreviewed_latest or valid_unreviewed_latest:
            third = copy.deepcopy(second)
            if invalid_unreviewed_latest:
                third["risk_assessments"][0]["scenario_ids"] = ["SC-UNKNOWN"]
            else:
                third["goal"] += " Unreviewed but schema-valid analysis."
            responses.append(third)
        responses = iter(responses)
        def fake_process(*args, **kwargs):
            return 0, json.dumps(next(responses), ensure_ascii=False), "", 0.01
        stage = StageManifest(self.old, "create", resume=True)
        stage.activate("analysis")
        agent = Agent(self.old, config=read_json(self.config))
        sources = [{"id": "business_description", "content": inputs["business_description"]}]
        with patch("refiner.agent.run_process", side_effect=fake_process):
            agent.ask("analyze", "first", {**inputs, "business_sources": sources}, ANALYSIS)
            agent.ask("analysis-review", "review", {"inputs": inputs, "analysis": first}, ANALYSIS_REVIEW)
            agent.ask("analyze", "second", {**inputs, "business_sources": sources}, ANALYSIS)
            agent.ask("analysis-review", "review", {"inputs": inputs, "analysis": second}, ANALYSIS_REVIEW)
            if third is not None:
                agent.ask("analyze", "unvalidated third", {**inputs, "business_sources": sources}, ANALYSIS)
        write_json(self.old / "analysis-attempt-1.json", first)
        write_json(self.old / "analysis-attempt-2.json", second)
        if third is not None:
            write_json(self.old / "analysis-attempt-3.json", third)
        write_json(self.old / "analysis-review.json", review2)
        (self.old / "agent-calls/003-analyze/process.stdout.log").write_text("recorded stream", encoding="utf-8")
        stage.finish("error")
        state = read_json(self.old / "state.json")
        call_count = 5 if third is not None else 4
        state.update(agent_calls=call_count, creation_agent_calls=call_count)
        write_json(self.old / "state.json", state)
        return first, review1, second, review2

    def test_invalid_unreviewed_latest_analysis_is_preserved_but_not_reused(self):
        first, review1, second, review2 = self._rejected_analysis_fixture(invalid_unreviewed_latest=True)
        from refiner.analysis_recovery import rejected_history
        from refiner.clarification_recovery import latest_clarification
        inputs = read_json(self.old / "inputs.json")
        stages = read_json(self.old / "stages.json")
        unvalidated_calls = []
        history = rejected_history(self.old, inputs, source_manifest=stages,
                                   allow_pending_clarification=True,
                                   unvalidated_calls=unvalidated_calls)
        self.assertEqual(len(history), 2)
        self.assertEqual(unvalidated_calls, ["005-analyze"])
        self.assertIsNone(latest_clarification(self.old, inputs, history, source_manifest=stages))
        original = {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.old.rglob("*") if p.is_file()}
        command = self.import_cmd.copy()
        command[command.index("--prior-agent-calls") + 1] = "5"
        self.assertEqual(main(command + ["--carry-analysis-feedback"]), 0)
        self.assertEqual(original, {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in self.old.rglob("*") if p.is_file()})
        recovery = read_json(self.out / "analysis-recovery.json")
        self.assertEqual(len(recovery["pairs"]), 2)
        self.assertTrue(all(not row["approved"] for row in recovery["pairs"]))
        invalid = self.old / "analysis-attempt-3.json"
        invalid_hash = hashlib.sha256(invalid.read_bytes()).hexdigest()
        copied_invalid = self.out.joinpath(*_source_artifact_path("analysis-attempt-3.json", invalid_hash).parts)
        self.assertEqual(read_json(copied_invalid), read_json(invalid))
        self.assertFalse((self.out / "agent-calls" / "005-analyze").exists())
        invalid_call_dir = self.old / "agent-calls" / "005-analyze"
        for path in invalid_call_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.old).as_posix()
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            archived = _source_artifact_path(relative, sha).as_posix()
            self.assertIn(archived, recovery["files"])
            self.assertEqual(hashlib.sha256((self.out / archived).read_bytes()).hexdigest(), sha)
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append((role, payload, agent.calls))
            raise RefinerError("offline stop after inspecting recovered history")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "analyze")
        self.assertEqual(seen[0][2], 5)
        self.assertEqual(seen[0][1]["previous_analysis"], second)
        self.assertEqual(seen[0][1]["previous_analysis_reviews"], [review1, review2])
        self.assertEqual(seen[0][1]["validation_feedback"], review1["issues"] + review2["issues"])

    def test_valid_unreviewed_latest_analysis_is_reused_for_its_independent_review(self):
        self._rejected_analysis_fixture(valid_unreviewed_latest=True)
        third = read_json(self.old / "analysis-attempt-3.json")
        from refiner.analysis_recovery import rejected_history
        inputs = read_json(self.old / "inputs.json")
        stages = read_json(self.old / "stages.json")
        pending = []
        history = rejected_history(self.old, inputs, source_manifest=stages, pending_analysis=pending)
        self.assertEqual(len(history), 2)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["analysis_call"], "005-analyze")
        self.assertEqual(pending[0]["analysis"], third)

    def test_invalid_unreviewed_latest_analysis_is_preserved_as_targeted_repair_context(self):
        self._rejected_analysis_fixture(invalid_unreviewed_latest=True)
        third = read_json(self.old / "analysis-attempt-3.json")
        from refiner.analysis_recovery import rejected_history
        inputs = read_json(self.old / "inputs.json")
        stages = read_json(self.old / "stages.json")
        invalid = []
        history = rejected_history(self.old, inputs, source_manifest=stages, invalid_analysis=invalid,
                                   unvalidated_calls=[])
        self.assertEqual(len(history), 2)
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["analysis_call"], "005-analyze")
        self.assertEqual(invalid[0]["analysis"], third)
        self.assertIn("scenario", invalid[0]["validation_error"].lower())

    def test_rejected_analysis_migration_passes_full_history_without_replaying(self):
        first, review1, second, review2 = self._rejected_analysis_fixture()
        old = {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in self.old.rglob("*") if p.is_file()}
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        self.assertEqual(old, {p.relative_to(self.old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in self.old.rglob("*") if p.is_file()})
        provenance = read_json(self.out / "analysis-recovery.json")
        self.assertEqual(len(provenance["pairs"]), 2)
        self.assertTrue(all(not row["approved"] for row in provenance["pairs"]))
        content_hash = hashlib.sha256((self.old / "analysis-attempt-2.json").read_bytes()).hexdigest()
        copied_analysis = self.out.joinpath(*_source_artifact_path("analysis-attempt-2.json", content_hash).parts)
        self.assertEqual(read_json(copied_analysis), second)
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append((role, payload, agent.calls))
            raise RefinerError("offline stop after inspecting revision context")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "analyze")
        self.assertEqual(seen[0][2], 4)
        self.assertEqual(seen[0][1]["previous_analysis"], second)
        self.assertEqual(seen[0][1]["previous_analysis_reviews"], [review1, review2])
        self.assertEqual(seen[0][1]["validation_feedback"], review1["issues"] + review2["issues"])
        stage = next(s for s in read_json(self.out / "stages.json")["stages"] if s["id"] == "analysis")
        self.assertNotEqual(stage["status"], "completed")

    def test_repeated_rejected_analysis_migrations_keep_artifacts_shallow(self):
        self._rejected_analysis_fixture()
        source = self.old
        expected_paths = None
        for generation in range(1, 9):
            if generation == 1:
                evidence = self.evidence
            else:
                stage = StageManifest(source, "create", resume=True)
                stage.activate("analysis")
                stage.finish("error")
                state = read_json(source / "state.json")
                state["status"] = "error"
                write_json(source / "state.json", state)
                evidence = self.base / f"reconciliation-{generation}.json"
                write_json(evidence, {"query": "processes for " + source.name, "exit_code": 0,
                                      "processes": [], "checked_at": "2026-10-31T00:00:00Z",
                                      "historical_evidence": "offline failed analysis fixture",
                                      "limitation": "current-process check only"})
            state = read_json(source / "state.json")
            destination = self.out if generation == 1 else self.base / f"continuation-{generation}"
            command = ["import-create-inputs", "--source-run", str(source), "--controller-archive", str(ROOT),
                       "--process-reconciliation", str(evidence), "--prior-seconds",
                       str(max(346.671, state["elapsed_seconds"])), "--prior-agent-calls",
                       str(state["agent_calls"]), "--prior-evaluations", "0", "--carry-analysis-feedback",
                       "--output", str(destination), "--config", str(self.config), "--max-seconds", "3600",
                       "--max-agent-calls", "32", "--max-evaluations", "80", "--max-rounds", "2",
                       "--planning-attempts", "2", "--max-cases", "12", "--acceptance-cases", "12",
            "--parallelism", "1", "--agent-parallelism", "2"]
            self.assertEqual(main(command), 0, f"migration generation {generation}")
            provenance = read_json(destination / "analysis-recovery.json")
            self.assertEqual(len(provenance["pairs"]), 2)
            artifact_root = destination / "analysis-previous" / "source-artifacts"
            artifact_files = sorted(p for p in artifact_root.rglob("*") if p.is_file())
            paths = [p.relative_to(destination).as_posix() for p in artifact_files]
            self.assertTrue(paths)
            self.assertFalse(any("source-artifacts/analysis-previous/source-artifacts/" in p for p in paths))
            self.assertFalse(any(p.rsplit("/", 1)[-1] in {
                "analysis-recovery.json", "technical-recovery.json", "clarification-recovery.json"
            } for p in paths), "replacement manifests must not be archived as historical source artifacts")
            self.assertTrue(all(len(str(p)) < 240 for p in artifact_files))
            if expected_paths is None:
                expected_paths = paths
            else:
                self.assertEqual(paths, expected_paths)
            source = destination

    def test_nontechnical_prior_recovery_rejects_unsealed_provenance(self):
        self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        source = self.out
        stage = StageManifest(source, 'create', resume=True)
        stage.activate('analysis')
        stage.finish('error')
        destination = self.base / 'nontechnical-second'
        options = [str(destination) if value == str(source) else value for value in self.options]
        evidence = self.base / 'nontechnical-reconciliation.json'
        write_json(evidence, {'query': 'processes for ' + source.name, 'exit_code': 0, 'processes': [],
                             'checked_at': '2026-10-31T00:00:00Z', 'historical_evidence': 'fixture terminated',
                             'limitation': 'offline fixture'})
        args = ['import-create-inputs', '--source-run', str(source), '--controller-archive', str(ROOT),
                '--process-reconciliation', str(evidence), '--prior-seconds', '350', '--prior-agent-calls', '4',
                '--prior-evaluations', '0', '--carry-analysis-feedback'] + options
        unknown = source / 'analysis-previous/unsealed-extra.json'
        unknown.write_text('{}',encoding='utf-8')
        self.assertEqual(main(args),2)
        self.assertFalse(destination.exists())
        unknown.unlink()
        self.assertEqual(main(args),0)

    def test_rejected_history_source_or_copied_response_tampering_is_rejected(self):
        self._rejected_analysis_fixture()
        original = self.old / "agent-calls/004-analysis-review/response.json"
        saved = original.read_bytes()
        original.write_text("{}", encoding="utf-8")
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 2)
        original.write_bytes(saved)
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        (self.out / "agent-calls/004-analysis-review/response.json").write_text("{}", encoding="utf-8")
        with patch("refiner.agent.Agent.ask", side_effect=AssertionError("backend must not run")):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)

    def test_forged_terminal_status_cannot_hide_later_rejected_feedback(self):
        self._rejected_analysis_fixture()
        manifest_path = self.old / "agent-calls/003-analyze/request-manifest.json"
        manifest = read_json(manifest_path)
        manifest["status"] = "invalid_response"
        write_json(manifest_path, manifest)
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 2)
        self.assertFalse(self.out.exists())

    def test_missing_previously_recorded_optional_call_evidence_is_rejected(self):
        self._rejected_analysis_fixture()
        (self.old / "agent-calls/003-analyze/process.stdout.log").unlink()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 2)
        self.assertFalse(self.out.exists())

    def test_review_must_be_bound_to_its_actual_preceding_analysis(self):
        first, _, _, _ = self._rejected_analysis_fixture()
        request_path = self.old / "agent-calls/004-analysis-review/request.json"
        request = read_json(request_path)
        request["input"]["analysis"] = first
        write_json(request_path, request)
        manifest_path = self.old / "agent-calls/004-analysis-review/request-manifest.json"
        manifest = read_json(manifest_path)
        manifest["request_sha256"] = hashlib.sha256(json.dumps(request, ensure_ascii=False).encode("utf-8")).hexdigest()
        write_json(manifest_path, manifest)
        stages_path = self.old / "stages.json"
        stages = read_json(stages_path)
        analysis = next(row for row in stages["stages"] if row["id"] == "analysis")
        for artifact in analysis["artifacts"]:
            path = self.old / artifact["path"]
            if path in (request_path, manifest_path):
                artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                artifact["bytes"] = path.stat().st_size
        write_json(stages_path, stages)
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 2)
        self.assertFalse(self.out.exists())

    def test_rejected_history_never_carries_across_changed_business_inputs(self):
        self._rejected_analysis_fixture()
        _, supplement = self._supplement_command()
        self.assertEqual(main(supplement + ["--carry-analysis-feedback"]), 2)
        self.assertFalse(self.out.exists())

    def test_recovered_identical_analysis_stops_without_review(self):
        _, _, second, _ = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        roles = []
        def fake_ask(agent, role, instruction, payload, schema):
            roles.append(role)
            return second
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(roles, ["analyze"])
        self.assertIn("no progress", read_json(self.out / "state.json")["error"])
        self.assertEqual(read_json(self.out / "analysis-attempt-3.json"), second)

    def test_recovered_earlier_rejected_analysis_stops_an_a_b_a_cycle(self):
        first, _, _, _ = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        roles = []
        def fake_ask(agent, role, instruction, payload, schema):
            roles.append(role)
            return first
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual(roles, ["analyze"])
        self.assertIn("no progress", read_json(self.out / "state.json")["error"])

    def test_revision_exposes_coverage_reduction_for_independent_justification(self):
        import test_analysis_contract_v3 as fixture
        _, _, previous, _ = self._rejected_analysis_fixture()
        revised = copy.deepcopy(previous)
        removed_id = revised["scenarios"].pop()["id"]
        revised["scenarios"][0]["applicable"] = False
        excluded_id = revised["scenarios"][0]["id"]
        revised["requirements"][0]["required"] = False
        delta = analysis_revision_delta(previous, revised)
        self.assertIn(removed_id, delta["scenarios_removed"])
        self.assertIn(excluded_id, delta["applicable_excluded"])
        self.assertIn("R1", delta["required_downgraded"])
        review = fixture.AnalysisV3Tests("runTest").review(revised)
        sources = [{"id": "business_description", "content": "业务需求"}]
        with self.assertRaisesRegex(RefinerError, "assess each changed"):
            validate_analysis_revision_review(review, delta, sources, revised)
        review["revision_assessments"] = [
            {"kind": row["kind"], "id": row["id"], "before_sha256": row["before_sha256"],
             "after_sha256": row["after_sha256"], "outcome": "excluded_by_source", "replacement_refs": [],
             "reason": "独立核查原始来源后确认此项是错误的范围假设。",
             "source_evidence": [{"source_id": "business_description", "quote": "业务需求"}]}
            for row in delta["changes"]]
        validate_analysis_revision_review(review, delta, sources, revised)

    def test_recovered_feedback_survives_clarification_revision(self):
        _, review1, second, review2 = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        candidate = copy.deepcopy(second)
        candidate["goal"] += " Further concrete revision."
        candidate["blocking_questions"] = [{"id": "BQ-EXTRA", "question": "是否需要未要求的额外能力？",
                                            "decision_owner": "业务负责人", "impact": "当前来源没有该额外业务能力的要求。"}]
        review = {"schema_version": "skill-refiner.clarification-review/v1", "question_assessments": [{
            "question_id": "BQ-EXTRA", "classification": "unsupported_extra",
            "evidence": [{"source_id": "business_description", "quote": "业务需求"}],
            "reason": "独立核对全部原始需求及适用组合后认为该能力未被要求。",
            "combination_risk_assessed": True, "required_requirement_ids_examined": ["R1"],
            "out_of_scope": "未要求的额外能力不属于这个示例的范围。"}]}
        corrected = copy.deepcopy(candidate)
        corrected["blocking_questions"] = []
        corrected["scope"]["out_of_scope"].append(review["question_assessments"][0]["out_of_scope"])
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append((role, payload))
            if role == "analyze":
                return candidate if len([r for r, _ in seen if r == "analyze"]) == 1 else corrected
            if schema.get("$id") == "skill-refiner.clarification-review":
                return review
            raise RefinerError("offline stop before independent review")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual([role for role, _ in seen], ["analyze", "analysis-review", "analyze", "analysis-review"])
        second_payload = seen[2][1]
        self.assertEqual(second_payload["previous_analysis"], candidate)
        self.assertEqual(second_payload["previous_analysis_reviews"], [review1, review2])
        for issue in review1["issues"] + review2["issues"]:
            self.assertIn(issue, second_payload["validation_feedback"])
        self.assertIn("Remove only", second_payload["validation_feedback"][-1])

    def test_new_independent_review_receives_runtime_context_and_is_added_to_history(self):
        import test_analysis_contract_v3 as fixture
        _, review1, second, review2 = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        revised = copy.deepcopy(second)
        revised["goal"] += " Additional distinct reviewable change."
        review3 = fixture.AnalysisV3Tests("runTest").review(revised)
        review3.update(schema_version="skill-refiner.analysis-review/v3", revision_assessments=[])
        review3.update(approved=False, issues=["New independent review issue"])
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append((role, instruction, payload))
            if role == "analysis-review":
                return review3
            if len([row for row in seen if row[0] == "analyze"]) == 1:
                return revised
            raise RefinerError("offline stop after reading accumulated review history")
        with patch("refiner.agent.Agent.ask", fake_ask):
            self.assertEqual(main(["create", "--resume-run", str(self.out)] + self.options), 2)
        self.assertEqual([row[0] for row in seen], ["analyze", "analysis-review", "analyze"])
        self.assertEqual(seen[1][2]["runtime_context"]["test_case_capacity"], {"development": 12, "acceptance": 12})
        self.assertEqual(seen[1][2]["runtime_context"]["validation_feedback"], review1["issues"] + review2["issues"])
        self.assertIn("not business requirements", seen[1][1])
        self.assertEqual(seen[2][2]["previous_analysis"], revised)
        self.assertEqual(seen[2][2]["previous_analysis_reviews"], [review1, review2, review3])
        self.assertIn("New independent review issue", seen[2][2]["validation_feedback"])
        self.assertEqual(read_json(self.out / "analysis-review-attempt-3.json"), review3)

    def test_same_id_semantic_change_cannot_be_approved_without_revision_assessment(self):
        import test_analysis_contract_v3 as fixture
        _, _, previous, _ = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        revised = copy.deepcopy(previous)
        revised['scenarios'][0]['situation'] += ' 同 ID 的覆盖条件发生变化。'
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append((role, payload, schema))
            if role == 'analyze':
                return revised
            if role == 'analysis-review':
                return fixture.AnalysisV3Tests.review(revised)  # Missing required v3 change assessments.
            raise AssertionError('Draft and target evaluation must not start')
        with patch('refiner.agent.Agent.ask', fake_ask):
            self.assertEqual(main(['create', '--resume-run', str(self.out)] + self.options), 2)
        self.assertEqual([row[0] for row in seen], ['analyze', 'analysis-review'])
        self.assertEqual(seen[1][2]['$id'], 'skill-refiner.analysis-review-v3')
        change = seen[1][1]['revision_delta']['changes'][0]
        self.assertEqual(change['id'], revised['scenarios'][0]['id'])
        self.assertIn('/situation', change['changed_paths'])
        self.assertEqual(change['before'], previous['scenarios'][0])
        self.assertEqual(change['after'], revised['scenarios'][0])
        self.assertNotEqual(next(s for s in read_json(self.out/'stages.json')['stages'] if s['id']=='analysis')['status'], 'completed')

    def test_source_grounded_same_id_wording_revision_reaches_draft(self):
        import test_analysis_contract_v3 as fixture
        _, _, previous, _ = self._rejected_analysis_fixture()
        self.assertEqual(main(self.import_cmd + ["--carry-analysis-feedback"]), 0)
        revised = copy.deepcopy(previous)
        revised['scenarios'][0]['situation'] += ' 补充等价的清晰说明。'
        seen = []
        def fake_ask(agent, role, instruction, payload, schema):
            seen.append(role)
            if role == 'analyze':
                return revised
            if role == 'analysis-review':
                review = fixture.AnalysisV3Tests.review(revised)
                review.update(schema_version='skill-refiner.analysis-review/v3', revision_assessments=[
                    {'kind': row['kind'], 'id': row['id'], 'before_sha256': row['before_sha256'],
                     'after_sha256': row['after_sha256'], 'outcome': 'equivalent',
                     'reason': '补充说明保持场景条件和预期结果的业务含义。',
                     'replacement_refs': [{'kind': 'scenario', 'id': revised['scenarios'][0]['id']}],
                     'source_evidence': [{'source_id': 'business_description', 'quote': '业务需求'}]}
                    for row in payload['revision_delta']['changes']])
                return review
            if role == 'draft':
                raise RefinerError('offline stop after approved revision')
            raise AssertionError('No target evaluation should occur')
        with patch('refiner.agent.Agent.ask', fake_ask):
            self.assertEqual(main(['create', '--resume-run', str(self.out)] + self.options), 2)
        self.assertEqual(seen[:3], ['analyze', 'analysis-review', 'draft'])
        stage = next(s for s in read_json(self.out/'stages.json')['stages'] if s['id']=='analysis')
        self.assertEqual(stage['status'], 'completed')


if __name__ == "__main__":
    unittest.main()

"""Deterministic checks for plan case contracts and markdown rendering."""
from __future__ import annotations

import json
import hashlib
import copy
import shlex
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import jsonschema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from refiner.checks import validate_plan  # noqa: E402
from refiner.agent import Agent as RefinerAgent  # noqa: E402
from refiner.common import (PlanGateError, RefinerError, digest, inventory, manifest, read_json,
                            PlanValidationError, run_process, write_json)  # noqa: E402
from refiner.creation import (ANALYSIS, ANALYSIS_V1, ANALYSIS_V2, ANALYSIS_V3, ANALYSIS_REVIEW,
                              ANALYSIS_REVIEW_V1, ANALYSIS_REVIEW_V2, analysis_markdown,
                              DRAFT_INSTRUCTION, validate_analysis, validate_analysis_review)  # noqa: E402
from refiner.plan import (parallel_agent_plan, _source_overview, write_test_scenarios,
                          write_work_plan)  # noqa: E402
from refiner.pipeline import (HOST_SUBAGENT_TEST_REVIEW, LANGUAGE_PLAN_REVIEW,
                              PLAN_REVIEW_CASE_BOOLS, PLANNING,
                              SELF_REVIEW_PLAN_REVIEW,
                              close_incomplete_plan_gate,
                              close_incomplete_planning_phases, initial_phase_results, make_plan,
                              make_plan_suites, require_plan_gate, set_phase_result, write_report,
                              validate_plan_review, _apply_refine_budget_cap_migration,
                              _criterion_coverage_repair_request, _uncovered_acceptance_criterion_ids)  # noqa: E402
from refiner.gate import (_verify_phase_history, _verify_plan_control_artifacts,
                          _verify_plan_review_artifacts)  # noqa: E402
from refiner.runtime import api_request, codex_output_schema, cursor_command, decode_codex_response  # noqa: E402
from refiner.stages import StageManifest, collect_artifacts, verify_stage_manifest  # noqa: E402
from refiner.artifact_validation import (all_schema_refs, schema_ref_compatible,
                                         schema_refs_compatible, validate_self_review_record,
                                         validate_skill_schema_package)  # noqa: E402
from refiner import schemas as refiner_schemas  # noqa: E402
from refiner.standards import (AUTHORING, HOST_SUBAGENT_TEST_RULES, SELF_REVIEW_STANDARD)  # noqa: E402


class SelfReviewProtocolTests(unittest.TestCase):
    def _record(self, *, overall="pass", round_number=0, result="pass"):
        check = {
            "check_id": "SR-01", "requirement_ids": ["R1"],
            "acceptance_criterion_ids": ["R1.A1"], "scenario_ids": ["SC-NORMAL-01"],
            "result": result, "evidence_refs": ["output/result.json#R1"],
        }
        if result in {"fail", "blocked"}:
            check["gap"] = "The required observable evidence is missing."
        record = {
            "schema_version": "agent-skill.self-review-record/v1", "stage_id": "FINAL-VERIFY",
            "overall_result": overall, "review_round": round_number, "checks": [check],
            "open_gaps": ([{"check_id": "SR-01", "description": check["gap"]}]
                          if result in {"fail", "blocked"} else []),
        }
        if overall in {"revise_once", "blocked"}:
            record["next_action"] = "Apply one targeted correction and recheck the frozen controls."
        return record

    def test_canonical_record_schema_accepts_pass_and_one_bounded_correction(self):
        validate_self_review_record(self._record())
        validate_self_review_record(self._record(overall="revise_once", result="fail"))
        validate_self_review_record(self._record(overall="blocked", round_number=1, result="fail"))
        with self.assertRaisesRegex(ValueError, "Self-review record fails"):
            validate_self_review_record(self._record(overall="revise_once", round_number=1, result="fail"))

    def test_record_requires_evidence_and_exact_open_gap_mapping(self):
        record = self._record()
        record["checks"][0]["evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "Self-review record fails"):
            validate_self_review_record(record)
        record = self._record(overall="revise_once", result="fail")
        record["open_gaps"] = [{"check_id": "UNKNOWN", "description": "The reported gap does not map to a frozen check."}]
        with self.assertRaisesRegex(ValueError, "open_gaps must identify"):
            validate_self_review_record(record)

    def test_creation_planning_repair_and_independent_review_share_bounded_protocol(self):
        for text in (AUTHORING, PLANNING, DRAFT_INSTRUCTION, SELF_REVIEW_STANDARD,
                     SELF_REVIEW_PLAN_REVIEW):
            with self.subTest(prompt=text[:40]):
                self.assertIn("one targeted", text.lower())
                self.assertIn("blocked", text.lower())
                self.assertIn("evidence", text.lower())


class SkillLanguagePolicyTests(unittest.TestCase):
    def test_new_skill_generation_defaults_human_readable_content_to_simplified_chinese(self):
        self.assertIn("Simplified Chinese", DRAFT_INSTRUCTION)
        self.assertIn("Simplified Chinese", AUTHORING)
        self.assertIn("Simplified Chinese", PLANNING)
        self.assertIn("explicit user language requirement", DRAFT_INSTRUCTION)
        self.assertIn("preserve its", AUTHORING)
        self.assertIn("Simplified Chinese", LANGUAGE_PLAN_REVIEW)


class HostSubagentTestPolicyTests(unittest.TestCase):
    def test_planning_and_review_prefer_bounded_case_level_subagents(self):
        policy = " ".join(HOST_SUBAGENT_TEST_RULES).lower()
        self.assertIn("prefer host subagents", policy)
        self.assertIn("one bounded assignment per", policy)
        self.assertIn("pass/fail/error/not_run", policy)
        self.assertIn("each assertion's expected and actual result", policy)
        self.assertIn("the main controller owns result synthesis", policy)
        self.assertIn("separate fixtures/workspaces/evidence paths", policy)
        self.assertIn("no shared runner or judge state", policy)
        self.assertIn("serially by default", policy)
        self.assertIn("whole suite", policy)
        self.assertIn("independent status and evidence record for every case", policy)
        self.assertIn("max_parallel_agents", policy)
        self.assertIn("host-subagent test execution policy", PLANNING.lower())
        self.assertEqual(PLANNING.lower().count("host-subagent test execution policy"), 1)
        self.assertIn("prefer host subagents", HOST_SUBAGENT_TEST_REVIEW.lower())



class CodexWireSchemaTests(unittest.TestCase):
    def test_json_schema_type_union_is_lowered_without_hashing_the_list(self):
        wire = codex_output_schema({"type": ["string", "null"], "minLength": 1})
        self.assertEqual(wire, {"anyOf": [{"type": "string"}, {"type": "null"}]})

    def test_current_frozen_plan_schema_can_be_lowered_for_codex(self):
        wire = codex_output_schema(refiner_schemas.PLAN)
        self.assertEqual(wire["type"], "object")

    def test_unconstrained_and_optional_fields_round_trip(self):
        schema = {
            "type": "object", "required": ["assertion_id", "value"],
            "properties": {"assertion_id": {"type": "string"}, "value": {},
                           "optional": {"type": "string"}},
        }
        wire_schema = codex_output_schema(schema)
        self.assertEqual(wire_schema["required"], ["assertion_id", "value_json", "optional"])
        self.assertEqual(wire_schema["properties"]["optional"]["anyOf"][1]["type"], "null")
        wire = {"assertion_id": "A-01", "value_json": json.dumps(["phrase one", "phrase two"]),
                "optional": None}
        self.assertEqual(decode_codex_response(wire, schema),
                         {"assertion_id": "A-01", "value": ["phrase one", "phrase two"]})

    def test_dynamic_object_round_trip(self):
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        lowered = codex_output_schema(schema)
        self.assertEqual(lowered["type"], "array")
        wire = [{"key": "status", "value": "passed"}, {"key": "stage", "value": "review"}]
        self.assertEqual(decode_codex_response(wire, schema), {"status": "passed", "stage": "review"})

    def test_cursor_executable_path_is_preserved_for_native_launcher(self):
        self.assertEqual(cursor_command("C:/tools/cursor-agent.exe"), (["C:/tools/cursor-agent.exe"], False))

    def test_process_output_is_decoded_as_utf8_on_windows(self):
        code, stdout, stderr, _ = run_process(
            [sys.executable, "-X", "utf8", "-c", "print('实际字段：已通过')"], timeout=10)
        self.assertEqual(code, 0, stderr)
        self.assertIn("实际字段：已通过", stdout)


class ControllerCallTimeoutTests(unittest.TestCase):
    def test_call_timeout_uses_the_smaller_profile_or_remaining_run_budget(self):
        with tempfile.TemporaryDirectory() as root:
            agent = RefinerAgent(root, timeout=240)
            agent.deadline = time.monotonic() + 10
            self.assertAlmostEqual(agent._call_timeout({"timeout": 3}), 3)
            remaining = agent._call_timeout({})
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 10)
            agent.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(RefinerError, "budget exhausted"):
                agent._call_timeout({})

    def test_timed_out_controller_call_persists_terminal_error_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            command = " ".join(shlex.quote(part) for part in (
                sys.executable, "-c", "import time; time.sleep(5)"))
            agent = RefinerAgent(root, command=command, timeout=30)
            agent.deadline = time.monotonic() + 0.2
            schema = {"type": "object", "required": ["ok"],
                      "properties": {"ok": {"type": "boolean"}}}
            with redirect_stdout(StringIO()), self.assertRaisesRegex(RefinerError, "timed out"):
                agent.ask("plan", "Return the required JSON.", {}, schema)

            call_dir = Path(root) / "agent-calls" / "001-plan"
            request = read_json(call_dir / "request-manifest.json")
            execution = read_json(call_dir / "execution.json")
            self.assertEqual(request["status"], "execution_error")
            self.assertEqual(execution["status"], "execution_error")
            self.assertLessEqual(execution["timeout_seconds"], 0.2)
            self.assertIn("timed out", execution["error"].lower())
            self.assertTrue((call_dir / "process.log").is_file())

    def test_managed_backend_receives_run_deadline_and_persists_stream_log_path(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"agents": {"planning": {"backend": "codex", "timeout": 2}}}
            agent = RefinerAgent(root, config=config, timeout=30)
            agent.deadline = time.monotonic() + 5
            schema = {"type": "object", "required": ["ok"],
                      "properties": {"ok": {"type": "boolean"}}}
            with patch("refiner.runtime.execute", return_value={
                    "final_message": json.dumps({"ok": True})}) as execute:
                agent.ask("plan", "Return the required JSON.", {}, schema)
            self.assertAlmostEqual(execute.call_args.args[3], 2)
            self.assertEqual(execute.call_args.kwargs["log_path"],
                             Path(root) / "agent-calls" / "001-plan" / "process.log")


class OpenAICompatibleRequestTimeoutTests(unittest.TestCase):
    @staticmethod
    def _server(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        return server, server_thread

    @staticmethod
    def _close_server(server, server_thread):
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    def test_http_worker_returns_a_valid_model_response(self):
        response_body = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":3}}'

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, *_args):
                pass

        server, server_thread = self._server(Handler)
        try:
            with tempfile.TemporaryDirectory() as root:
                log_path = Path(root) / "process.log"
                message, usage = api_request(
                    {"base_url": f"http://127.0.0.1:{server.server_port}", "model": "local-test"},
                    [{"role": "user", "content": "hello"}], [], 5, cwd=root, log_path=log_path)
                self.assertTrue(log_path.is_file())
        finally:
            self._close_server(server, server_thread)
        self.assertEqual(message, {"content": "ok"})
        self.assertEqual(usage, {"total_tokens": 3})

    def test_slow_drip_http_response_is_killed_at_the_call_deadline(self):
        request_seen = threading.Event()
        response_body = b'{"choices":[{"message":{"content":"ok"}}]}'

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request_seen.set()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                for byte in response_body:
                    try:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.1)

            def log_message(self, *_args):
                pass

        server, server_thread = self._server(Handler)
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(RefinerError, "Process timed out"):
                    api_request(
                        {"base_url": f"http://127.0.0.1:{server.server_port}", "model": "local-test"},
                        [{"role": "user", "content": "hello"}], [], 2, cwd=root)
            elapsed = time.monotonic() - started
            self.assertTrue(request_seen.is_set(), "the HTTP worker must reach the slow server before timing out")
            self.assertLess(elapsed, 3.5, f"the request exceeded its two-second hard deadline: {elapsed:.2f}s")
        finally:
            self._close_server(server, server_thread)


class AgentPlanningConcurrencyTests(unittest.TestCase):
    def test_independent_plan_review_overlaps_acceptance_planning_after_contract_freeze(self):
        development = _minimal_plan()
        acceptance = _minimal_plan()
        acceptance["requirements"] = json.loads(json.dumps(development["requirements"]))
        active, peak = 0, 0
        guard = threading.Lock()
        rendezvous = threading.Barrier(2)
        wave_events = []

        def fake_make_plan(_agent, context, _work, _max_cases, role="plan", _max_attempts=2, **kwargs):
            nonlocal active, peak
            if role == "plan":
                plan = development
                kwargs["on_self_check_passed"](plan)
            else:
                self.assertEqual(context["contract"], development["requirements"])
                plan = acceptance
            with guard:
                active += 1
                peak = max(peak, active)
            rendezvous.wait(timeout=3)
            with guard:
                active -= 1
            return plan

        with tempfile.TemporaryDirectory() as root, patch("refiner.pipeline.make_plan", side_effect=fake_make_plan):
            planned, accepted = make_plan_suites(
                object(), {"requirements": [{"source": "frozen.md", "content": "R1"}]}, root,
                4, 2, agent_parallelism=2,
                wave_callback=lambda status, **data: wave_events.append((status, data)))
        self.assertIs(planned, development)
        self.assertIs(accepted, acceptance)
        self.assertEqual(peak, 2, "both independent Agent branches must overlap")
        self.assertEqual([status for status, _ in wave_events],
                         ["running", "task_finished", "task_finished", "passed"])
        schedule = wave_events[0][1]
        self.assertEqual(schedule["wave_id"], "PW-PLAN-01")
        self.assertEqual(schedule["max_parallel_agents"], 2)
        self.assertEqual([task["id"] for task in schedule["tasks"]],
                         ["AG-DEV-REVIEW", "AG-ACCEPTANCE-PLAN"])
        self.assertEqual(set(schedule["tasks"][0]["write_scope"]) & set(schedule["tasks"][1]["write_scope"]), set())
        self.assertIn("opens the plan gate", schedule["join_gate"])
        self.assertEqual(wave_events[-1][1]["task_results"], [
            {"id": "AG-DEV-REVIEW", "status": "passed"},
            {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"},
        ])
        self.assertEqual([event[1]["task_id"] for event in wave_events[1:3]],
                         ["AG-DEV-REVIEW", "AG-ACCEPTANCE-PLAN"])

    def test_agent_parallelism_one_runs_plan_branches_sequentially(self):
        calls = []

        def fake_make_plan(_agent, context, _work, _max_cases, role="plan", _max_attempts=2, **_kwargs):
            calls.append(role)
            plan = _minimal_plan()
            if role == "acceptance-plan":
                self.assertEqual(context["contract"], plan["requirements"])
            return plan

        waves = []
        with tempfile.TemporaryDirectory() as root, patch("refiner.pipeline.make_plan", side_effect=fake_make_plan):
            make_plan_suites(object(), {}, root, 4, 2, agent_parallelism=1,
                             wave_callback=lambda status, **data: waves.append((status, data)))
        self.assertEqual(calls, ["plan", "acceptance-plan"])
        self.assertEqual(waves[0][0], "serial")
        self.assertEqual([status for status, _ in waves],
                         ["serial", "task_started", "task_finished", "task_started", "task_finished", "passed"])
        self.assertEqual(waves[0][1]["serial_reason"],
                         "--agent-parallelism=1; the tasks are logically independent but the configured concurrency cap is one")
        self.assertEqual([row["status"] for row in waves[-1][1]["task_results"]], ["passed", "passed"])

    def test_parallel_agent_plan_exposes_dependencies_scopes_and_join_gate(self):
        plan = parallel_agent_plan(2)
        self.assertEqual(plan["schedule"], "parallel")
        self.assertEqual(plan["starts_when"], "development plan is frozen and its complete local control set passes")
        self.assertTrue(all(task["depends_on"] and task["inputs"] and task["outputs"]
                            and task["write_scope"] for task in plan["tasks"]))
        self.assertEqual(set(plan["tasks"][0]["write_scope"]) & set(plan["tasks"][1]["write_scope"]),
                         set(), "parallel tasks must have disjoint write scopes")
        self.assertIn("plan gate", plan["join_gate"])

    def test_unsupported_agent_parallelism_is_rejected(self):
        for parallelism in (0, 3):
            with self.subTest(parallelism=parallelism):
                with self.assertRaisesRegex(RefinerError, "supports agent_parallelism 1 or 2"):
                    make_plan_suites(object(), {}, ".", 4, 2,
                                     agent_parallelism=parallelism)

    def test_parallel_plan_failure_joins_the_started_peer_and_records_failed_wave(self):
        peer_finished = threading.Event()
        waves = []

        def fake_make_plan(_agent, _context, _work, _max_cases, role="plan", _max_attempts=2, **kwargs):
            if role == "plan":
                kwargs["on_self_check_passed"](_minimal_plan())
                raise PlanGateError("development review rejected the plan")
            peer_finished.set()
            return _minimal_plan()

        with tempfile.TemporaryDirectory() as root, patch("refiner.pipeline.make_plan", side_effect=fake_make_plan):
            with self.assertRaisesRegex(PlanGateError, "development review rejected"):
                make_plan_suites(object(), {}, root, 4, 2, agent_parallelism=2,
                                 wave_callback=lambda status, **data: waves.append((status, data)))
        self.assertTrue(peer_finished.is_set(), "the already-started acceptance branch must be joined")
        self.assertEqual([status for status, _ in waves], ["running", "failed"])
        self.assertEqual(waves[-1][1]["task_results"], [
            {"id": "AG-DEV-REVIEW", "status": "failed", "error": "development review rejected the plan"},
            {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"},
        ])

    def test_agent_call_counter_and_artifacts_are_safe_under_parallel_requests(self):
        barrier = threading.Barrier(2)
        config = {"agents": {"default": {"backend": "openai_compatible", "model": "local-test",
                                          "base_url": "http://127.0.0.1:1"}}}
        schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}

        def backend(*_args, **_kwargs):
            barrier.wait(timeout=3)
            return {"final_message": json.dumps({"ok": True})}

        with tempfile.TemporaryDirectory() as root:
            agent = RefinerAgent(root, config=config, timeout=10)
            agent.max_calls = 2
            with patch("refiner.runtime.execute", side_effect=backend):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(agent.ask, role, "Return JSON.", {}, schema)
                               for role in ("plan", "acceptance-plan")]
                    self.assertEqual([future.result(timeout=5) for future in futures],
                                     [{"ok": True}, {"ok": True}])
            call_dirs = list((Path(root) / "agent-calls").iterdir())
            self.assertEqual(agent.calls, 2)
            self.assertEqual(len(call_dirs), 2)
            self.assertEqual({path.name.split("-", 1)[0] for path in call_dirs}, {"001", "002"})


class StageArtifactCollectionTests(unittest.TestCase):
    def test_original_glob_normalizes_windows_separators_and_collects_nested_files(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "original" / "references" / "guide.md"
            target.parent.mkdir(parents=True)
            target.write_text("frozen source", encoding="utf-8")
            artifacts, missing = collect_artifacts(Path(root), [
                {"path": r"original\**", "glob": True},
            ])
            self.assertEqual(missing, [])
            self.assertEqual([item["path"] for item in artifacts],
                             ["original/references/guide.md"])
            self.assertEqual(artifacts[0]["bytes"], len(b"frozen source"))

    def test_required_glob_with_no_files_is_reported_missing(self):
        with tempfile.TemporaryDirectory() as root:
            artifacts, missing = collect_artifacts(Path(root), [
                {"path": r"original\**", "glob": True},
            ])
            self.assertEqual(artifacts, [])
            self.assertEqual(missing, ["original/**"])


class StageSchemaContractTests(unittest.TestCase):
    def _complete_creation_inputs(self, root):
        stages = StageManifest(root, "create")
        stages.activate("inputs")
        (Path(root) / "inputs").mkdir(exist_ok=True)
        (Path(root) / "inputs.json").write_text('{"requested_name":"demo"}', encoding="utf-8")
        (Path(root) / "inputs" / "request.md").write_text("The user request.", encoding="utf-8")
        stages.complete("inputs")
        return stages

    def test_every_internal_stage_declares_hashed_schema_contracts(self):
        current = {row["id"]: row for row in all_schema_refs()}
        for mode in ("refine", "create"):
            for stage in StageManifest._bound_contract(mode):
                for field in ("inputs", "outputs"):
                    for declaration in stage[field]:
                        reference = declaration["schema_ref"]
                        self.assertEqual(reference, current[reference["id"]])
                        self.assertIn("validator_id", reference)

    def test_packaged_domain_schema_files_match_runtime_contracts(self):
        package_schemas = SCRIPTS.parent / "schemas"
        expected = {
            "v2/plan.schema.json": ("skill-refiner.plan-v2", "v2", refiner_schemas.PLAN_V2),
            "v3/plan.schema.json": ("skill-refiner.plan-v3", "v3", refiner_schemas.PLAN_V3),
            "v4/plan.schema.json": ("skill-refiner.plan-v4", "v4", refiner_schemas.PLAN_V4),
            "v5/plan.schema.json": ("skill-refiner.plan-v5", "v5", refiner_schemas.PLAN_V5),
            "v6/plan.schema.json": ("skill-refiner.plan-v6", "v6", refiner_schemas.PLAN_V6),
            "v1/execution-plan.schema.json": ("skill-refiner.execution-plan", "v1", refiner_schemas.EXECUTION_PLAN),
            "v2/plan-review.schema.json": ("skill-refiner.plan-review-v2", "v2", refiner_schemas.PLAN_REVIEW_V2),
            "v3/plan-review.schema.json": ("skill-refiner.plan-review-v3", "v3", refiner_schemas.PLAN_REVIEW_V3),
            "v4/plan-review.schema.json": ("skill-refiner.plan-review-v4", "v4", refiner_schemas.PLAN_REVIEW_V4),
            "v5/plan-review.schema.json": ("skill-refiner.plan-review-v5", "v5", refiner_schemas.PLAN_REVIEW),
            "v1/repair.schema.json": ("skill-refiner.repair", "v1", refiner_schemas.REPAIR),
            "v1/analysis.schema.json": ("skill-refiner.analysis", "v1", ANALYSIS_V1),
            "v1/analysis-review.schema.json": ("skill-refiner.analysis-review", "v1", ANALYSIS_REVIEW_V1),
            "v2/analysis.schema.json": ("skill-refiner.analysis-v2", "v2", ANALYSIS_V2),
            "v3/analysis.schema.json": ("skill-refiner.analysis-v3", "v3", ANALYSIS_V3),
            "v2/analysis-review.schema.json": ("skill-refiner.analysis-review-v2", "v2", ANALYSIS_REVIEW_V2),
            "v1/skill-stage-contracts.schema.json": (
                "skill-refiner.skill-stage-contracts", "v1",
                {**refiner_schemas.PLAN["properties"]["stage_contracts"],
                 "$defs": refiner_schemas.PLAN["$defs"]}),
        }
        for filename, (schema_id, version, source_schema) in expected.items():
            with self.subTest(filename=filename):
                packaged = read_json(package_schemas / filename)
                runtime = json.loads(json.dumps(source_schema))
                runtime.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                "$id": schema_id, "x-schema-version": version})
                self.assertEqual(packaged, runtime)

    def test_schema_registry_uses_version_directories_and_reads_legacy_paths(self):
        refs = all_schema_refs()
        self.assertTrue(all(row["path"].startswith("schemas/v") for row in refs))
        current = next(row for row in refs if row["id"] == "skill-refiner.plan-v5")
        legacy = {**current, "path": "schemas/plan-v5.schema.json"}
        self.assertTrue(schema_ref_compatible(legacy, current))
        self.assertTrue(schema_refs_compatible([legacy], [current]))
        changed = {**legacy, "sha256": "0" * 64}
        self.assertFalse(schema_ref_compatible(changed, current))
        self.assertFalse(schema_refs_compatible([changed], [current]))

    def test_resume_migrates_matching_legacy_flat_schema_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            StageManifest(root, "create")
            saved = read_json(root / "stages.json")

            def legacy_path(reference):
                filename = Path(reference["path"]).name
                stem = filename[:-len(".schema.json")]
                return f"schemas/{stem}-{reference['version']}.schema.json"

            old_paths = {}
            for reference in saved["schema_refs"]:
                old_paths[reference["id"]] = legacy_path(reference)
                reference["path"] = old_paths[reference["id"]]
            changed_declarations = 0
            for stage in saved["stages"]:
                for field in ("inputs", "outputs"):
                    for declaration in stage[field]:
                        reference = declaration.get("schema_ref")
                        if reference and reference["id"] in old_paths:
                            reference["path"] = old_paths[reference["id"]]
                            changed_declarations += 1
            self.assertGreater(changed_declarations, 0)
            write_json(root / "stages.json", saved)

            resumed = StageManifest(root, "create", resume=True)
            self.assertEqual(resumed.data["resume_count"], 1)
            self.assertTrue(all(row["path"].startswith("schemas/v")
                                for row in resumed.data["schema_refs"]))

    def test_invalid_structured_output_blocks_stage_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stages = StageManifest(root, "create")
            stages.activate("inputs")
            (root / "inputs").mkdir()
            (root / "inputs.json").write_text("[]", encoding="utf-8")
            (root / "inputs" / "request.md").write_text("The user request.", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "json-object"):
                stages.complete("inputs")
            self.assertEqual(next(row for row in stages.data["stages"] if row["id"] == "inputs")["status"],
                             "running")
            stages.finish("error")
            saved = read_json(root / "stages.json")
            self.assertEqual(saved["stages"][0]["status"], "blocked")

    def test_completed_stage_is_reused_and_handoff_inputs_are_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._complete_creation_inputs(root)
            resumed = StageManifest(root, "create", resume=True)
            self.assertTrue(resumed.is_completed("inputs"))
            self.assertEqual(resumed.data["resume_count"], 1)
            resumed.activate("analysis")
            analysis = next(row for row in resumed.data["stages"] if row["id"] == "analysis")
            self.assertTrue(analysis["input_artifacts"])
            self.assertTrue(all(row["validation"]["status"] == "passed"
                                for row in analysis["input_artifacts"]))

    def test_resume_rejects_changed_completed_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._complete_creation_inputs(root)
            (root / "inputs.json").write_text('{"requested_name":"changed"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "size or SHA-256 changed"):
                StageManifest(root, "create", resume=True)

    def test_manifest_schema_registry_hash_mismatch_rejects_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._complete_creation_inputs(root)
            saved = read_json(root / "stages.json")
            saved["schema_refs"][0]["sha256"] = "0" * 64
            write_json(root / "stages.json", saved)
            with self.assertRaisesRegex(ValueError, "schema registry or schema hash changed"):
                StageManifest(root, "create", resume=True)

    def test_final_stage_verifier_rejects_schema_registry_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            StageManifest(root, "create")
            saved = read_json(root / "stages.json")
            saved["schema_refs"][0]["path"] = "schemas/attacker.schema.json"
            write_json(root / "stages.json", saved)
            with self.assertRaisesRegex(ValueError, "schema registry or schema hash changed"):
                verify_stage_manifest(root, mode="create", run_status="running")

    def test_runtime_schemas_and_validators_must_be_shipped_in_target_skill(self):
        definition = {"id": "S-RESULT", "version": "v1", "path": "schemas/v1/result.schema.json",
                      "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                      "runtime_needed": True}
        contract = {"schemas": [definition]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "schemas/v1").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / definition["path"]).write_text(
                json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                            "$id": "S-RESULT", "x-schema-version": "v1", "type": "object"}),
                encoding="utf-8")
            (root / definition["validator_path"]).write_text("# packaged validator", encoding="utf-8")
            validate_skill_schema_package(root, contract)
            (root / definition["validator_path"]).unlink()
            with self.assertRaisesRegex(ValueError, "validator_path"):
                validate_skill_schema_package(root, contract)

    def test_delivered_contract_rejects_multiple_versions_for_one_schema_id(self):
        first = {"id": "S-RESULT", "version": "v1", "path": "schemas/v1/result.schema.json",
                 "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                 "runtime_needed": True}
        second = {**first, "version": "v2", "path": "schemas/v2/result.schema.json"}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "one active version per logical schema ID"):
                validate_skill_schema_package(Path(temp), {"schemas": [first, second]})

    def test_delivered_skill_omits_schemas_marked_not_runtime_needed(self):
        definition = {"id": "S-PLAN", "version": "v1", "path": "schemas/v1/plan.schema.json",
                      "validator_id": "jsonschema", "validator_path": "scripts/validate-plan.py",
                      "runtime_needed": False}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "schemas/v1").mkdir(parents=True)
            (root / "schemas/v1/plan.schema.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not runtime-needed"):
                validate_skill_schema_package(root, {"schemas": [definition]})

    def test_delivered_schema_directory_must_match_declared_version(self):
        definition = {"id": "S-RESULT", "version": "v1", "path": "schemas/v2/result.schema.json",
                      "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                      "runtime_needed": True}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "version directory must match"):
                validate_skill_schema_package(Path(temp), {"schemas": [definition]})

    def test_delivered_skill_rejects_unlisted_historical_schema_files(self):
        definition = {"id": "S-RESULT", "version": "v1", "path": "schemas/v1/result.schema.json",
                      "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                      "runtime_needed": True}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "SKILL.md").write_text("---\nname: sample-skill\n---\n", encoding="utf-8")
            (root / "schemas/v1").mkdir(parents=True)
            (root / "scripts").mkdir()
            schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "S-RESULT",
                      "x-schema-version": "v1", "type": "object"}
            (root / definition["path"]).write_text(json.dumps(schema), encoding="utf-8")
            (root / "schemas/v0").mkdir()
            (root / "schemas/v0/result.schema.json").write_text(
                json.dumps({**schema, "$id": "S-RESULT-OLD", "x-schema-version": "v0"}), encoding="utf-8")
            (root / definition["validator_path"]).write_text("# packaged validator", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "undeclared or historical schema file"):
                validate_skill_schema_package(root, {"schemas": [definition]})

    def test_refiner_keeps_private_schema_compatibility_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.mkdir(exist_ok=True)
            (root / "SKILL.md").write_text("---\nname: skill-refiner\n---\n", encoding="utf-8")
            (root / "schemas/v1").mkdir(parents=True)
            (root / "schemas/v2").mkdir()
            (root / "schemas/v1/legacy.schema.json").write_text("{}", encoding="utf-8")
            (root / "schemas/v2/current.schema.json").write_text(
                json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                            "$id": "S-CURRENT", "x-schema-version": "v2", "type": "object"}),
                encoding="utf-8")
            (root / "scripts").mkdir()
            validator = root / "scripts/validate.py"
            validator.write_text("# packaged validator", encoding="utf-8")
            definition = {"id": "S-CURRENT", "version": "v2", "path": "schemas/v2/current.schema.json",
                          "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                          "runtime_needed": True}
            validate_skill_schema_package(root, {"schemas": [definition]})


class BackupInventoryTests(unittest.TestCase):
    def test_before_backup_files_are_excluded_from_manifest_inventory_and_overview(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            scripts = root / "scripts" / "refiner"
            scripts.mkdir(parents=True)
            (scripts / "agent.py").write_text("current", encoding="utf-8")
            (scripts / "agent.py.before-old").write_text("stale", encoding="utf-8")
            self.assertEqual({Path(item).as_posix() for item in manifest(root)},
                             {"scripts/refiner/agent.py"})
            self.assertEqual({Path(item["path"]).as_posix() for item in inventory(root)},
                             {"scripts/refiner/agent.py"})
            _, overview = _source_overview(root)
            self.assertEqual([item for kind, item in overview if kind == "file"],
                             ["scripts/refiner/agent.py"])

    def test_agent_inventory_omits_installed_venv_but_keeps_skill_and_runtime_config(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            files = {
                "SKILL.md": "Skill instructions",
                ".runtime/backends.json": "{}",
                ".runtime/venv/Lib/site-packages/example.py": "third-party package source",
                "references/local-runtime.json": "{\"max_rounds\": 2}",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            observed = {Path(item["path"]).as_posix(): item for item in inventory(root)}

        self.assertEqual(set(observed), {
            "SKILL.md", ".runtime/backends.json", "references/local-runtime.json"})
        self.assertEqual(observed["SKILL.md"]["content"], "Skill instructions")
        self.assertEqual(observed["references/local-runtime.json"]["content"],
                         "{\"max_rounds\": 2}")

    def test_priority_paths_control_context_order_under_byte_budget(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name, content in (("alpha.txt", "a" * 50), ("beta.txt", "b" * 50),
                                  ("gamma.txt", "g" * 50)):
                (root / name).write_text(content, encoding="utf-8")

            observed = inventory(root, max_bytes=100, priority_paths=("gamma.txt", "alpha.txt"))

        self.assertEqual([item["path"] for item in observed], ["gamma.txt", "alpha.txt", "beta.txt"])
        self.assertEqual(observed[0]["content"], "g" * 50)
        self.assertEqual(observed[1]["content"], "a" * 50)
        self.assertEqual(observed[2]["omitted"], "context limit; unresolved dependency if required")


def _case(case_id, kind, scenario, expected_outcome, prompt, requirement_id="R1"):
    token = f"token-{case_id}"
    dimension = {"normal": "input_variation", "boundary": "workflow_state", "error": "dependency_failure",
                 "negative": "safety_boundary", "regression": "retry_recovery"}[kind]
    return {
        "id": case_id,
        "requirement_ids": [requirement_id],
        "acceptance_criteria_ids": [f"{requirement_id}.A1"],
        "scenario_id": f"S-{case_id}",
        "kind": kind,
        "scenario": scenario,
        "expected_outcome": expected_outcome,
        "scenario_detail": {
            "title": scenario, "preconditions": [f"The {case_id} fixture is available and isolated."],
            "stimulus": prompt, "actions": [f"Run the {case_id} scenario through the Skill."],
            "expected_observables": [expected_outcome],
            "forbidden_outcomes": ["The executor must not invent evidence or perform an unauthorized side effect."],
            "risk_dimensions": [dimension],
            "basis": f"{case_id} checks an independently observable business result.",
        },
        "prompt": prompt,
        "files": {},
        "helpers": [],
        "asset_audit": {
            "status": "not_required", "required_paths": [], "available_paths": [],
            "source_refs": [f"requirements.md#{requirement_id}"],
            "basis": "The case uses no files, helpers or external assets.",
        },
        "checks": [{"id": "A-RESPONSE", "type": "contains", "value": token}],
        "oracle_reason": f"Response must include {token} for observable success.",
        "good_response": f"Answer with {token} present.",
        "good_responses": [f"Alt wording includes {token}.", f"Another phrasing has {token}."],
        "bad_response": "No matching token in this answer.",
        "positive_controls": [
            {"id": "P-1", "response": f"Answer with {token} present."},
            {"id": "P-2", "response": f"Alt wording includes {token}."},
            {"id": "P-3", "response": f"Another phrasing has {token}."},
        ],
        "negative_controls": [{
            "id": "N-1", "assertion_id": "A-RESPONSE",
            "response": "No matching token in this answer.",
            "rationale": "The response omits the required observable result.",
        }],
    }


def _minimal_plan(**case_overrides):
    requirements = [{"id": "R1", "behavior": "Do the thing", "source": "requirements.md#R1", "required": True,
                     "acceptance_criteria": [{"id": "R1.A1", "statement": "Return the requested result.",
                                               "evidence": "The response includes the requested result token."}]}]
    normal = _case("dev-normal", "normal", "Operator starts a valid task.", "Task completes with visible proof.",
                   "Execute the documented valid task path with explicit executor wording.")
    boundary = _case("dev-boundary", "boundary", "Operator supplies incomplete input.", "Clarification without side effects.",
                     "Trigger the boundary path using incomplete operator input only.")
    for key, value in case_overrides.items():
        if key in normal:
            normal[key] = value
        elif key in boundary:
            boundary[key] = value
    return {
        "schema_version": "skill-refiner.plan/v5",
        "requirements": requirements,
        "cases": [normal, boundary],
        "coverage_gaps": [],
        "assumptions": [],
        "stage_contracts": {
            "schema_version": "skill-refiner.skill-stage-contracts/v1",
            "schemas": [{"id": "S-RESULT", "version": "v1", "path": "schemas/v1/result.schema.json",
                         "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
                         "runtime_needed": True,
                         "media_type": "application/json"}],
            "stages": [{
                "id": "result", "goal": "Produce the documented result artifact.", "requirement_ids": ["R1"],
                "inputs": [],
                "outputs": [{"path": "reports/result.json", "media_type": "application/json",
                             "schema_id": "S-RESULT", "schema_version": "v1", "required": True}],
                "validation_gate": "Validate the result against its packaged JSON Schema before handoff.",
                "validation_points": ["after_generation", "before_handoff", "on_resume", "final_verification"],
                "failure_states": ["schema_invalid", "validation_error"],
                "completion_check": "The output validates and its content hash is recorded.",
                "resume_policy": "reuse_if_contract_hashes_match",
            }],
        },
        "execution_plan": {
            "schema_version": "skill-refiner.execution-plan/v1",
            "objective": "Implement and verify the requested Skill behavior.",
            "scope": {"in_scope": ["Implement the mapped behavior."],
                      "out_of_scope": ["Unrequested integrations."], "constraints": []},
            "max_parallel_agents": 1,
            "steps": [
                {"id": "STEP-PLAN", "order": 1, "title": "Freeze the test plan",
                 "goal": "Freeze traceable cases before changing the Skill.",
                 "actions": ["Map the requirement and both cases to observable assertions."],
                 "inputs": ["The request and requirements.md#R1."],
                 "outputs": [{"path": "plans/test-plan.json", "schema_id": "S-RESULT",
                              "schema_version": "v1", "evidence": "The reviewed plan file and control summary."}],
                 "requirement_ids": ["R1"], "case_ids": ["dev-normal", "dev-boundary"],
                 "depends_on": [], "execution_mode": "serial", "parallel_wave": None,
                 "serial_reason": "Planning must freeze the requirements before implementation.",
                 "agent": {"role": "primary planner", "scope": "Freeze the complete requirement and case map.",
                           "write_scope": ["plans/"], "merge_owner": "primary agent",
                           "join_gate": "All requirements and cases have stable IDs and schema-valid fields."},
                 "exit_criteria": [{"id": "STEP-PLAN-OK", "condition": "Both cases map to the required behavior.",
                                    "evidence": "The plan JSON and successful full control results."}],
                 "failure_policy": {"action": "stop", "max_attempts": 0,
                                    "terminal_state": "blocked", "recovery_policy": "blocked"}},
                {"id": "STEP-IMPLEMENT", "order": 2, "title": "Implement the Skill",
                 "goal": "Implement the requested behavior without changing the frozen plan.",
                 "actions": ["Make the smallest implementation changes supported by the requirements."],
                 "inputs": ["The frozen plan and current Skill snapshot."],
                 "outputs": [{"path": "implementation/changes.json", "schema_id": "S-RESULT",
                              "schema_version": "v1", "evidence": "Changed files and requirement-linked rationale."}],
                 "requirement_ids": ["R1"], "case_ids": ["dev-normal", "dev-boundary"],
                 "depends_on": ["STEP-PLAN"], "execution_mode": "serial", "parallel_wave": None,
                 "serial_reason": "Implementation depends on the frozen requirements and test oracles.",
                 "agent": {"role": "primary implementer", "scope": "Edit the target Skill files only.",
                           "write_scope": ["implementation/"], "merge_owner": "primary agent",
                           "join_gate": "Changed files cite frozen requirements and preserve protected oracles."},
                 "exit_criteria": [{"id": "STEP-IMPLEMENT-OK", "condition": "Each required behavior has an implementation path.",
                                    "evidence": "The candidate Skill tree and static validation output."}],
                 "failure_policy": {"action": "stop", "max_attempts": 0,
                                    "terminal_state": "blocked", "recovery_policy": "blocked"}},
                {"id": "STEP-VERIFY", "order": 3, "title": "Verify and deliver",
                 "goal": "Run controls and independent acceptance against the final candidate.",
                 "actions": ["Run the frozen regression and acceptance cases, then verify delivered files."],
                 "inputs": ["The candidate Skill and frozen development and acceptance plans."],
                 "outputs": [{"path": "evidence/final.json", "schema_id": "S-RESULT",
                              "schema_version": "v1", "evidence": "Machine results and candidate hashes."}],
                 "requirement_ids": ["R1"], "case_ids": ["dev-normal", "dev-boundary"],
                 "depends_on": ["STEP-IMPLEMENT"], "execution_mode": "serial", "parallel_wave": None,
                 "serial_reason": "Final verification can only begin after the candidate and controls exist.",
                 "agent": {"role": "primary verifier", "scope": "Execute and reconcile all frozen controls.",
                           "write_scope": ["evidence/"], "merge_owner": "primary agent",
                           "join_gate": "All required cases pass and the delivered manifest matches evidence."},
                 "exit_criteria": [{"id": "STEP-VERIFY-OK", "condition": "Every required case and final hash check passes.",
                                    "evidence": "Acceptance summary and final evidence manifest."}],
                 "failure_policy": {"action": "stop", "max_attempts": 0,
                                    "terminal_state": "blocked", "recovery_policy": "blocked"}},
            ],
        },
    }


def _contract_entry(case_id, **flags):
    entry = {
        "case_id": case_id,
        "installed_source_unchanged": True,
        "fixture_contract_consistent": True,
        "response_contract_consistent": True,
        "trace_checks_observable": True,
        "scenario_consistent": True,
        "requirement_trace_consistent": True,
        "expected_outcome_consistent": True,
        "machine_checks_consistent": True,
        "matrix_complete": True,
        "asset_audit_consistent": True,
        "positive_controls_validated": True,
        "negative_controls_targeted": True,
        "acceptance_criteria_traceable": True,
        "scenario_detail_complete": True,
        "risk_coverage_complete": True,
        "basis": "Fixture and checks align with the frozen case contract.",
    }
    entry.update(flags)
    return entry


def _approved_review(plan):
    return {
        **({"revision_targets": []} if plan.get("schema_version") == "skill-refiner.plan/v7" else {}),
        "approved": True,
        "issues": [],
        "case_contracts": [_contract_entry(case["id"]) for case in plan["cases"]],
        "stage_contracts_assessed": True,
        "stage_contracts_complete": True,
        "schema_paths_safe": True,
        "validator_lifecycle_covered": True,
        "execution_plan_assessed": True,
        "execution_plan_complete": True,
        "dependency_graph_valid": True,
        "concurrency_safe": True,
        "failure_recovery_bounded": True,
        "step_assessments": [{"step_id": step["id"], "goal_actions_clear": True,
                              "inputs_outputs_evidenced": True, "requirement_case_traceable": True,
                              "dependencies_valid": True, "agent_scope_safe": True,
                              "exit_and_recovery_clear": True,
                              "basis": "The step has grounded goals, evidence, safe scope and bounded recovery."}
                             for step in plan["execution_plan"]["steps"]],
    }


class PlanCaseFieldValidationTests(unittest.TestCase):
    def test_plan_requires_versioned_skill_stage_artifact_contracts(self):
        plan = _minimal_plan()
        del plan["stage_contracts"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(jsonschema.ValidationError):
                validate_plan(plan, root)

    def test_plan_v5_requires_the_schema_constrained_execution_plan(self):
        plan = _minimal_plan()
        del plan["execution_plan"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(jsonschema.ValidationError):
                validate_plan(plan, root)

    def test_execution_plan_case_links_must_resolve_and_cover_the_matrix(self):
        plan = _minimal_plan()
        plan["execution_plan"]["steps"][0]["case_ids"] = ["unknown-case"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "unknown test case"):
                validate_plan(plan, root)
        plan = _minimal_plan()
        for step in plan["execution_plan"]["steps"]:
            step["case_ids"] = ["dev-normal"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "omits requirements or cases"):
                validate_plan(plan, root)

    def test_execution_plan_rejects_forward_or_cyclic_dependencies(self):
        plan = _minimal_plan()
        plan["execution_plan"]["steps"][0]["depends_on"] = ["STEP-VERIFY"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "cyclic or out of order"):
                validate_plan(plan, root)

    def test_parallel_execution_wave_rejects_overlapping_write_scopes(self):
        plan = _minimal_plan()
        work = plan["execution_plan"]
        work["max_parallel_agents"] = 2
        first, second = work["steps"][:2]
        for step in (first, second):
            step["execution_mode"] = "parallel"
            step["parallel_wave"] = "WAVE-1"
            step["serial_reason"] = ""
        second["depends_on"] = []
        first["agent"]["write_scope"] = ["shared/output/"]
        second["agent"]["write_scope"] = ["shared/output/step-two/"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "overlapping write scopes"):
                validate_plan(plan, root)

    def test_acceptance_must_preserve_frozen_execution_plan(self):
        plan = _minimal_plan()
        expected = json.loads(json.dumps(plan["execution_plan"]))
        expected["steps"][0]["goal"] = "A different plan must not replace the frozen work."
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "changed the frozen"):
                validate_plan(plan, root, expected_execution_plan=expected)

    def test_stage_outputs_must_reference_a_declared_schema(self):
        plan = _minimal_plan()
        plan["stage_contracts"]["stages"][0]["outputs"][0]["schema_id"] = "UNKNOWN"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "undeclared schema"):
                validate_plan(plan, root)

    def test_stage_contract_allows_only_one_active_version_per_schema_id(self):
        plan = _minimal_plan()
        plan["stage_contracts"]["schemas"].append({
            "id": "S-RESULT", "version": "v2", "path": "schemas/v2/result.schema.json",
            "validator_id": "jsonschema", "validator_path": "scripts/validate.py",
            "runtime_needed": True, "media_type": "application/json",
        })
        plan["execution_plan"]["steps"][0]["outputs"][0]["schema_version"] = "v2"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "one active version per logical schema ID"):
                validate_plan(plan, root)

    def test_stage_schema_path_must_stay_inside_the_skill_package(self):
        plan = _minimal_plan()
        plan["stage_contracts"]["schemas"][0]["path"] = "schemas/v1/../outside.schema.json"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "schema path is unsafe"):
                validate_plan(plan, root)

    def test_stage_schema_version_directory_must_match_declared_version(self):
        plan = _minimal_plan()
        plan["stage_contracts"]["schemas"][0]["path"] = "schemas/v2/result.schema.json"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RefinerError, "version directory must match"):
                validate_plan(plan, root)

    def test_missing_required_case_text_rejected(self):
        for field in ("scenario", "expected_outcome"):
            with self.subTest(field=field):
                plan = _minimal_plan()
                del plan["cases"][0][field]
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaises(jsonschema.ValidationError):
                        validate_plan(plan, root)

    def test_helpers_is_an_explicit_required_array(self):
        plan = _minimal_plan()
        del plan["cases"][0]["helpers"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(jsonschema.ValidationError):
                validate_plan(plan, root)

    def test_structured_scenario_detail_is_required(self):
        plan = _minimal_plan()
        del plan["cases"][0]["scenario_detail"]["preconditions"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(jsonschema.ValidationError):
                validate_plan(plan, root)

    def test_every_required_atomic_acceptance_criterion_needs_a_case(self):
        plan = _minimal_plan()
        plan["requirements"][0]["acceptance_criteria"].append(
            {"id": "R1.A2", "statement": "Respect the requested output shape.",
             "evidence": "The result follows the requested output shape."})
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlanValidationError) as ctx:
                validate_plan(plan, root)
        self.assertIn("R1.A2", str(ctx.exception))
        self.assertIn("acceptance_criteria", str(ctx.exception))

    def test_duplicate_scenario_detail_is_not_counted_as_new_coverage(self):
        plan = _minimal_plan()
        plan["cases"][1]["scenario_detail"] = json.loads(json.dumps(plan["cases"][0]["scenario_detail"]))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlanValidationError) as ctx:
                validate_plan(plan, root)
        self.assertIn("meaningfully distinct scenario", str(ctx.exception))

    def test_creation_scenario_matrix_must_be_preserved_by_executable_cases(self):
        plan = _minimal_plan()
        sources = [{"id": case["scenario_id"], "kind": case["kind"], "applicable": True,
                    "situation": case["scenario"], "requirement_ids": case["requirement_ids"],
                    "acceptance_criteria_ids": case["acceptance_criteria_ids"],
                    "detail": json.loads(json.dumps(case["scenario_detail"])),
                    "basis": "Frozen business scenario basis.", "exclusion_reason": ""}
                   for case in plan["cases"]]
        plan["cases"][1]["scenario_detail"]["expected_observables"] = [
            "An outcome that contradicts the frozen business scenario."
        ]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlanValidationError) as ctx:
                validate_plan(plan, root, expected_scenarios=sources)
        self.assertIn("preserve the frozen scenario ID, trace, expected outcomes", str(ctx.exception))

    def test_asset_audit_cannot_claim_a_missing_source_asset(self):
        plan = _minimal_plan()
        audit = plan["cases"][0]["asset_audit"]
        audit.update(status="ready", required_paths=["references/missing.md"],
                     available_paths=["references/missing.md"])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        self.assertIn("unavailable_claims", str(ctx.exception))
        self.assertEqual(ctx.exception.evidence["status"], "invalid_coverage")
        self.assertEqual(len(ctx.exception.evidence["case_control_results"]), 2)

    def test_every_positive_and_semantic_negative_is_checked_as_a_matrix(self):
        plan = _minimal_plan()
        case = plan["cases"][0]
        case["prompt"] += ' Return JSON only; status must be one of "complete" or "pending".'
        valid = [
            '{"status":"complete","message":"不需要补集数"}',
            '{"status":"complete","message":"无需补集数"}',
            '{"status":"complete","message":"不用补集数"}',
        ]
        case["checks"] = [
            {"id": "A-STATUS", "type": "json_path_equal", "pointer": "/status", "value": "complete"},
            {"id": "A-SEMANTIC", "type": "contains_any",
             "value": ["不需要补集数", "无需补集数", "不用补集数"]},
        ]
        case["positive_controls"] = [
            {"id": f"P-{i}", "response": response} for i, response in enumerate(valid, 1)
        ]
        case["good_response"] = valid[0]
        case["good_responses"] = valid[1:]
        semantic_failure = '{"status":"complete","message":"必须补集数"}'
        wrong_status_only = '{"status":"pending","message":"不需要补集数"}'
        case["negative_controls"] = [
            {"id": "N-STATUS", "assertion_id": "A-STATUS", "response": wrong_status_only,
             "rationale": "The semantic answer is correct but the status is incomplete."},
            {"id": "N-SEMANTIC", "assertion_id": "A-SEMANTIC", "response": semantic_failure,
             "rationale": "The status is correct but the business result is wrong."},
        ]
        case["bad_response"] = semantic_failure

        with tempfile.TemporaryDirectory() as root:
            result = validate_plan(plan, root)
        controls = result["case_control_results"][0]["controls"]
        self.assertEqual(len(controls), 5)
        self.assertTrue(all(row["passed"] for control in controls
                            for row in control["assertions"]
                            if control["polarity"] == "positive"))

        # A wrong status alone must not count as evidence that the semantic
        # business assertion detects an incorrect response.
        wrong_status_semantic_only = '{"status":"pending","message":"无需补集数"}'
        case["negative_controls"][1]["response"] = wrong_status_semantic_only
        case["bad_response"] = wrong_status_semantic_only
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        diagnostic = str(ctx.exception)
        self.assertIn("Frozen plan controls failed", diagnostic)
        self.assertIn("N-SEMANTIC", diagnostic)
        self.assertIn("A-STATUS", diagnostic)
        self.assertIn("A-SEMANTIC", diagnostic)
        self.assertIn("expected_condition", diagnostic)
        self.assertIn("actual_value", diagnostic)

    def test_positive_control_failure_names_assertion_and_actual_value(self):
        plan = _minimal_plan()
        case = plan["cases"][0]
        case["checks"].append({"id": "A-SECOND", "type": "contains", "value": "complete"})
        for control in case["negative_controls"]:
            control["assertion_id"] = "A-RESPONSE"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        message = str(ctx.exception)
        self.assertIn("A-SECOND", message)
        self.assertIn("field_path", message)
        self.assertIn("expected_condition", message)
        self.assertIn("actual_value", message)

    def test_blank_case_text_rejected(self):
        for field in ("scenario", "expected_outcome"):
            with self.subTest(field=field):
                plan = _minimal_plan()
                plan["cases"][0][field] = "   "
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaises(RefinerError) as ctx:
                        validate_plan(plan, root)
                self.assertIn(f"missing required {field}", str(ctx.exception))

    def test_prompt_duplicated_expected_outcome_rejected(self):
        plan = _minimal_plan()
        plan["cases"][0]["expected_outcome"] = plan["cases"][0]["prompt"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        self.assertIn("must be separate from the executor prompt", str(ctx.exception))


class PlanReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = _minimal_plan()
        self.plan["cases"].append(_case(
            "dev-error", "error", "Operator triggers an error case.",
            "The error is explicit and safe.", "Trigger the documented error path."))

    def test_incomplete_review_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"] = review["case_contracts"][:2]
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("every case exactly once", str(ctx.exception))

    def test_stage_schema_review_flags_are_mandatory_and_true(self):
        review = _approved_review(self.plan)
        review["validator_lifecycle_covered"] = False
        with self.assertRaisesRegex(RefinerError, "stage schema contracts"):
            validate_plan_review(review, self.plan)

    def test_missing_review_assessment_rejected(self):
        review = _approved_review(self.plan)
        del review["case_contracts"][0]["scenario_consistent"]
        with self.assertRaises(jsonschema.ValidationError):
            validate_plan_review(review, self.plan)

    def test_duplicate_review_assessment_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"].append(review["case_contracts"][0])
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("duplicate case assessments", str(ctx.exception))

    def test_unknown_case_review_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"][0]["case_id"] = "unknown-case"
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("unknown case", str(ctx.exception))

    def test_false_contract_blocks_approval(self):
        review = _approved_review(self.plan)
        review["case_contracts"][0]["machine_checks_consistent"] = False
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("conflicting execution contract", str(ctx.exception))

    def test_all_review_bools_required_for_approval(self):
        review = _approved_review(self.plan)
        for name in PLAN_REVIEW_CASE_BOOLS:
            entry = dict(review["case_contracts"][0])
            entry[name] = False
            bad = dict(review)
            bad["case_contracts"] = [entry, review["case_contracts"][1]]
            with self.assertRaises(RefinerError):
                validate_plan_review(bad, self.plan)


class PlanRevisionAndPhaseGateTests(unittest.TestCase):
    def test_upstream_planning_error_closes_a_running_plan_gate(self):
        state = {"phase_results": initial_phase_results(), "phase_history": []}
        set_phase_result(state, "plan_gate", "running")
        self.assertTrue(close_incomplete_plan_gate(state, "planner connection reset"))
        self.assertEqual(state["phase_results"]["plan_gate"]["status"], "not_passed")
        self.assertEqual(state["phase_history"][-1]["reason"], "upstream_error")
        self.assertEqual(state["phase_history"][-1]["error"], "planner connection reset")
        self.assertFalse(close_incomplete_plan_gate(state, "a later error"))

    def test_cancelled_planning_closes_running_phase_and_plan_gate(self):
        state = {"phase_results": initial_phase_results()}
        set_phase_result(state, "plan_generation", "running", suite="development")
        set_phase_result(state, "plan_generation", "running", suite="acceptance")
        self.assertTrue(close_incomplete_planning_phases(state, "controller interrupted"))
        close_incomplete_plan_gate(state, "controller interrupted", cause="cancelled")
        self.assertEqual(state["phase_results"]["plan_generation"]["status"], "error")
        self.assertEqual(state["phase_results"]["plan_generation"]["suites"],
                         {"development": "error", "acceptance": "error"})
        self.assertEqual(state["phase_results"]["plan_gate"]["status"], "not_passed")

    def test_accepted_plan_persists_full_controls_and_independent_review(self):
        plan = _minimal_plan()

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.roles = []

            def ask(self, role, instruction, payload, schema):
                self.roles.append(role)
                return plan if role == "plan" else _approved_review(plan)

        with tempfile.TemporaryDirectory() as root:
            agent = Agent(root)
            phases = []
            frozen_for_acceptance = []
            accepted = make_plan(agent, {}, root, 8, max_attempts=1,
                                 phase_callback=lambda name, status, **detail:
                                 phases.append((name, status, detail.get("suite"))),
                                 on_self_check_passed=frozen_for_acceptance.append)
            self.assertEqual(accepted, plan)
            controls = read_json(Path(root) / "plan-controls" / "development.json")
            review = read_json(Path(root) / "plan-reviews" / "development.json")
            attempt = read_json(Path(root) / "plan-controls" / "attempts" / "development-01.json")
        self.assertEqual(agent.roles, ["plan", "plan-review"])
        self.assertEqual(frozen_for_acceptance, [plan])
        self.assertEqual(controls["plan_sha256"], attempt["plan_sha256"])
        self.assertEqual(controls["case_control_results"], attempt["case_control_results"])
        self.assertEqual(review["review"]["approved"], True)
        self.assertIn(("plan_self_check", "passed", "development"), phases)
        self.assertIn(("independent_review", "passed", "development"), phases)
        self.assertLess(phases.index(("plan_self_check", "passed", "development")),
                        phases.index(("independent_review", "running", "development")))

    def test_plan_revision_cannot_drop_an_unrelated_case(self):
        first = _minimal_plan()
        replacement = _case("dev-added", "boundary", "Operator uses an added case.",
                            "The added case produces evidence.", "Run the added boundary prompt.")
        revised = {**first, "cases": [first["cases"][0], replacement]}
        for step in revised["execution_plan"]["steps"]:
            step["case_ids"] = ["dev-normal", "dev-added"]

        class Agent:
            def __init__(self):
                self.calls = 0
                self.reviews = 0

            def ask(self, role, instruction, payload, schema):
                if role == "plan":
                    self.calls += 1
                    return first if self.calls == 1 else revised
                self.reviews += 1
                review = _approved_review(first)
                review["approved"] = False
                review["issues"] = ["Revise dev-normal assertion while preserving the full matrix."]
                return review

        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlanGateError) as ctx:
                make_plan(Agent(), {}, root, 8, max_attempts=2)
            dropped_revision_controls = read_json(
                Path(root) / "plan-controls" / "attempts" / "development-02.json")
        self.assertIn("dropped_frozen_matrix_entries", str(ctx.exception))
        self.assertIn("after full controls", str(ctx.exception))
        self.assertIn("dev-boundary", str(ctx.exception))
        # Positive/negative controls passed; the revision still failed.
        self.assertEqual(dropped_revision_controls["status"], "failed")
        self.assertTrue(dropped_revision_controls["case_control_results"])
        self.assertEqual(dropped_revision_controls["control_failures"], [])
        self.assertEqual(dropped_revision_controls["coverage_failures"], [])
        self.assertTrue(dropped_revision_controls["revision_failures"])

    def test_skill_evaluation_is_blocked_until_all_plan_gates_pass(self):
        state = {"phase_results": initial_phase_results()}
        with self.assertRaises(PlanGateError):
            require_plan_gate(state)
        for name in ("plan_generation", "plan_self_check", "independent_review"):
            set_phase_result(state, name, "passed", suite="development")
            set_phase_result(state, name, "passed", suite="acceptance")
        set_phase_result(state, "plan_gate", "passed")
        require_plan_gate(state)

    def test_phase_status_enum_rejects_unknown_values(self):
        state = {"phase_results": initial_phase_results()}
        with self.assertRaises(RefinerError):
            set_phase_result(state, "plan_generation", "complete", suite="development")

    def test_agent_process_streams_events_and_waits_for_the_terminal_result(self):
        import sys

        events = []
        code, stdout, stderr, _ = run_process(
            [sys.executable, "-c", "import time; print('agent-start', flush=True); "
             "time.sleep(.05); print('agent-result', flush=True)"],
            timeout=None, on_output=lambda stream, chunk: events.append((stream, chunk)))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("agent-result", stdout)
        self.assertEqual("".join(chunk for stream, chunk in events if stream == "stdout"), stdout)

    def test_final_plan_verification_recomputes_controls_and_binds_review(self):
        plans = {"development": _minimal_plan(), "acceptance": _minimal_plan()}
        for suite in plans:
            plans[suite]["cases"][0]["id"] = f"{suite}-normal"
            plans[suite]["cases"][1]["id"] = f"{suite}-boundary"
            for step in plans[suite]["execution_plan"]["steps"]:
                step["case_ids"] = [f"{suite}-normal", f"{suite}-boundary"]
        with tempfile.TemporaryDirectory() as root:
            for suite, plan in plans.items():
                class Agent:
                    work = Path(root)

                    def ask(self, role, instruction, payload, schema):
                        return plan if role in {"plan", "acceptance-plan"} else _approved_review(plan)

                make_plan(Agent(), {}, root, 8,
                          role="acceptance-plan" if suite == "acceptance" else "plan",
                          max_attempts=1, suite=suite)
            _verify_plan_control_artifacts(Path(root), plans)
            _verify_plan_review_artifacts(Path(root), plans)

            summary = read_json(Path(root) / "plan-controls" / "development.json")
            summary["case_control_results"][0]["controls"][0]["assertions"][0]["actual_value"] = "tampered"
            write_json(Path(root) / "plan-controls" / "development.json", summary)
            with self.assertRaises(RefinerError):
                _verify_plan_control_artifacts(Path(root), plans)

    def test_final_gate_rejects_missing_or_out_of_order_plan_evidence(self):
        state = {"phase_results": initial_phase_results(), "phase_history": [
            {"sequence": 1, "phase": "plan_gate", "status": "passed"},
            {"sequence": 2, "phase": "skill_evaluation", "status": "running"},
        ]}
        for name in ("plan_generation", "plan_self_check", "independent_review"):
            state["phase_results"][name]["status"] = "passed"
            state["phase_results"][name]["suites"] = {"development": "passed", "acceptance": "passed"}
        state["phase_results"]["plan_gate"]["status"] = "passed"
        state["phase_results"]["skill_evaluation"] = {
            "status": "passed", "suites": {"baseline": "passed", "regression": "passed", "acceptance": "passed"}}
        state["phase_results"]["final_verification"]["status"] = "running"
        _verify_phase_history(state)
        state["phase_history"][0]["sequence"] = 3
        state["phase_history"][1]["sequence"] = 2
        with self.assertRaises(RefinerError):
            _verify_phase_history(state)


class PlanMarkdownRenderingTests(unittest.TestCase):
    def test_both_suites_render_with_distinct_requirement_sources(self):
        dev_plan = _minimal_plan()
        dev_plan["requirements"] = [
            {"id": "R1", "behavior": "Development behavior", "source": "dev-spec.md#R1", "required": True},
        ]
        acceptance = {
            "requirements": [
                {"id": "R1", "behavior": "Acceptance behavior", "source": "acceptance-spec.md#A1", "required": True},
            ],
            "cases": [
                _case("acc-normal", "normal", "Independent operator runs acceptance.", "Acceptance proof is visible.",
                      "Run the independent acceptance prompt with distinct executor wording."),
                _case("acc-error", "error", "Independent operator triggers failure.", "Failure is explicit and safe.",
                      "Run the independent error prompt with distinct executor wording."),
            ],
            "coverage_gaps": [],
            "assumptions": [],
        }
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            write_json(work / "test-plan.json", dev_plan)
            write_json(work / "acceptance-plan.json", acceptance)
            write_work_plan(work, plan=dev_plan, acceptance=acceptance)
            write_test_scenarios(work, plan=dev_plan, acceptance=acceptance)
            work_plan = (work / "work-plan.md").read_text(encoding="utf-8")
            scenarios = (work / "test-scenarios.md").read_text(encoding="utf-8")
            write_work_plan(work, plan=dev_plan, acceptance=acceptance, analysis=None,
                            agent_parallelism=1)
            serial_work_plan = (work / "work-plan.md").read_text(encoding="utf-8")
        self.assertIn("dev-spec.md#R1", work_plan)
        self.assertIn("acceptance-spec.md#A1", work_plan)
        self.assertIn("Scenario: Operator starts a valid task.", work_plan)
        self.assertIn("Expected outcome: Acceptance proof is visible.", work_plan)
        self.assertIn("Each case is a separate acceptance item", work_plan)
        self.assertIn("- [ ] **dev-normal (normal)**", work_plan)
        self.assertIn("- [ ] **acc-normal (normal)**", work_plan)
        self.assertIn("NOT_RUN, ERROR, missing evidence", work_plan)
        self.assertIn("contains; expected=token-acc-normal", work_plan)
        self.assertIn("Scenario: Operator starts a valid task.", scenarios)
        self.assertIn("Expected outcome: Acceptance proof is visible.", scenarios)
        self.assertIn("Acceptance rule: evaluate each case independently", scenarios)
        self.assertIn("acceptance-spec.md#A1", scenarios)
        self.assertIn("contains; expected=token-acc-normal", scenarios)
        self.assertIn("Oracle rationale:", work_plan)
        self.assertIn("## Skill phase and artifact contracts", work_plan)
        self.assertIn("S-RESULT@v1", work_plan)
        self.assertIn("## Schema-validated work breakdown", work_plan)
        self.assertIn("## Test delegation and case-level progress", work_plan)
        self.assertIn("one bounded assignment per frozen", work_plan)
        self.assertIn("large-task-tracking is active", work_plan)
        self.assertIn("max_parallel_agents is not proof", work_plan)
        self.assertIn("STEP-PLAN — Freeze the test plan", work_plan)
        self.assertIn("Outputs/evidence:", work_plan)
        self.assertIn("Concurrency limit: 1 Agent(s)", work_plan)
        self.assertIn("recovery: blocked", work_plan)
        self.assertIn("after generation", work_plan.lower())
        self.assertEqual(work_plan.count("- Conclusion required to advance:"), 8)
        self.assertIn("Step 1 — Inspect the request and map the existing Skill", work_plan)
        self.assertIn("Step 4 — Pass the plan gate before target evaluation", work_plan)
        self.assertIn("Step 8 — Verify and deliver", work_plan)
        self.assertIn("showing this plan is a visibility checkpoint, not an approval wait", work_plan)
        self.assertIn("exhausted configured budget", work_plan)
        self.assertIn("Bounded Agent concurrency", work_plan)
        self.assertIn("Bounded Agent concurrency and dependency-aware work plan", work_plan)
        self.assertIn("AG-DEV-REVIEW", work_plan)
        self.assertIn("AG-ACCEPTANCE-PLAN", work_plan)
        self.assertIn("Isolated scope:", work_plan)
        self.assertIn("join gate:", work_plan)
        self.assertIn("Other dependent phases remain serial", work_plan)
        self.assertIn("Cases within one skill-up executor remain sequential", work_plan)
        self.assertIn("Serial reason: --agent-parallelism=1", serial_work_plan)


class CaseByCaseReportingTests(unittest.TestCase):
    def test_report_records_each_case_verdict_instead_of_only_an_aggregate(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            (work / "baseline").mkdir()
            (work / "acceptance").mkdir()
            write_json(work / "baseline" / "summary.json", {
                "cases": [{"id": "C01", "status": "PASS"}, {"id": "C02", "status": "NOT_RUN"}],
            })
            write_json(work / "acceptance" / "summary.json", {
                "cases": [{"id": "A01", "status": "FAIL"}],
            })
            write_report(work, {"status": "blocked", "source": "fixture", "agent_calls": 1,
                               "target_evaluations": 0, "elapsed_seconds": 1, "rounds": [],
                               "limitations": [], "phase_results": {}})
            report = (work / "report.md").read_text(encoding="utf-8")
        self.assertIn("| C01 | PASS |", report)
        self.assertIn("| C02 | NOT_RUN |", report)
        self.assertIn("| A01 | FAIL |", report)


class CreationAnalysisConclusionTests(unittest.TestCase):
    @staticmethod
    def _scenario(scenario_id, kind, applicable, situation, stimulus, *, exclusion_reason=""):
        return {
            "id": scenario_id, "kind": kind, "applicable": applicable, "situation": situation,
            "requirement_ids": ["R1"], "acceptance_criteria_ids": ["R1.A1"],
            "detail": {"title": situation, "preconditions": ["The isolated request fixture is available."],
                       "stimulus": stimulus, "actions": ["Submit the fixture and inspect the response."],
                       "expected_observables": ["The response contains the documented result fields."],
                       "forbidden_outcomes": ["No unauthorized write or unsupported claim is produced."],
                       "risk_dimensions": ["input_variation" if kind == "normal" else
                                           "workflow_state" if kind == "boundary" else "dependency_failure"],
                       "basis": "This scenario is grounded in source requirement R1."},
            "basis": "This scenario exercises observable behavior from R1.",
            "exclusion_reason": exclusion_reason,
        }

    def _analysis(self):
        return {
            "schema_version": "skill-refiner.analysis/v2",
            "name": "demo-skill",
            "goal": "Complete the requested business workflow.",
            "context": {"user_intent": "Help the user complete the specified business workflow.",
                        "usage_context": "The Skill is invoked by a user with a concrete business request.",
                        "primary_user_ids": ["ACTOR-USER"]},
            "scope": {"in_scope": ["Complete the requested business workflow."],
                      "out_of_scope": ["No additional integrations or side effects beyond the request."],
                      "constraints": ["Use only facts supplied by the user or inspected sources."]},
            "actors": [{"id": "ACTOR-USER", "role": "Skill user",
                        "needs": ["Receive the requested result."],
                        "permission_boundary": "Only perform actions explicitly authorized by the user."}],
            "requirements": [
                {"id": "R1", "behavior": "Return a useful result.", "source": "spec-1: requested result", "required": True,
                 "acceptance_criteria": [{"id": "R1.A1", "statement": "Return the requested result fields.",
                                           "evidence": "Each requested field appears in the response."}]},
            ],
            "workflow": [
                {"id": "W1", "order": 1, "action": "Process the request.",
                 "actor_ids": ["ACTOR-USER"], "preconditions": ["A request is supplied."],
                 "inputs": "Request text.",
                 "outputs": "A result.", "completion_check": "The result contains the requested fields.",
                 "failure_behavior": "Report the missing field and stop.", "requirement_ids": ["R1"],
                 "dependency_ids": []},
            ],
            "scenarios": [
                self._scenario("SC-NORMAL-01", "normal", True, "Valid request with all required fields.",
                               "Submit a valid request containing every required field."),
                self._scenario("SC-NORMAL-02", "normal", True, "Valid request with a supported alternate input.",
                               "Submit an alternate supported input that exercises the same business outcome."),
                self._scenario("SC-BOUNDARY-01", "boundary", False, "Request omits an optional field.",
                               "Submit the request without the optional field.",
                               exclusion_reason="The source contract defines no behavior for this optional field."),
                self._scenario("SC-ERROR-01", "error", False, "A documented dependency is unavailable.",
                               "Make the declared dependency unavailable before submission.",
                               exclusion_reason="The requested Skill has no external dependency."),
            ],
            "risk_assessments": [
                {"dimension": dimension, "applicable": dimension == "input_variation",
                 "basis": "This risk was checked against the supplied scope and scenario matrix.",
                 "scenario_ids": (["SC-NORMAL-01", "SC-NORMAL-02"]
                                  if dimension == "input_variation" else [])}
                for dimension in refiner_schemas.RISK_DIMENSIONS
            ],
            "assumptions": [], "external_dependencies": [], "blocking_questions": [],
        }

    def test_business_workflow_step_requires_observable_completion_check(self):
        analysis = self._analysis()
        validate_analysis(analysis)
        self.assertIn("R1.A1: Return the requested result fields.", analysis_markdown(analysis))
        self.assertIn("SC-NORMAL-02", analysis_markdown(analysis))
        self.assertIn("Completion check: The result contains the requested fields.", analysis_markdown(analysis))
        del analysis["workflow"][0]["completion_check"]
        with self.assertRaises(jsonschema.ValidationError):
            validate_analysis(analysis)

    def test_v2_analysis_requires_explicit_context_scope_actors_and_full_risk_register(self):
        for field in ("context", "scope", "actors", "risk_assessments"):
            with self.subTest(field=field):
                analysis = self._analysis()
                del analysis[field]
                with self.assertRaises(jsonschema.ValidationError):
                    validate_analysis(analysis)

    def test_analysis_cross_references_actor_and_every_applicable_risk_scenario(self):
        analysis = self._analysis()
        analysis["workflow"][0]["actor_ids"] = ["UNKNOWN-ACTOR"]
        with self.assertRaisesRegex(RefinerError, "unknown actor"):
            validate_analysis(analysis)
        analysis = self._analysis()
        risk = next(row for row in analysis["risk_assessments"] if row["dimension"] == "input_variation")
        risk["scenario_ids"] = ["SC-NORMAL-01"]
        with self.assertRaisesRegex(RefinerError, "link every applicable matching scenario"):
            validate_analysis(analysis)

    def test_v2_review_assesses_every_workflow_and_risk_row(self):
        analysis = self._analysis()
        analysis["workflow"].append({**analysis["workflow"][0], "id": "W2", "order": 2})
        review = {
            "schema_version": "skill-refiner.analysis-review/v2", "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                                         "acceptance_criteria_atomic": True, "criteria_observable": True,
                                         "basis": "The source and criterion are traceable and observable."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": True,
                                      "risk_coverage_complete": True, "traceability_complete": True,
                                      "basis": "The actions and outcomes can be executed and observed."}
                                     for row in analysis["scenarios"]],
            "context_scope_assessment": {"intent_grounded": True, "actors_complete": True,
                "scope_clear": True, "constraints_complete": True,
                "basis": "User, scope and boundaries are all grounded in the request."},
            "workflow_assessments": [{"workflow_id": row["id"], "order_and_actors_clear": True,
                "inputs_outputs_complete": True, "completion_and_failure_testable": True,
                "requirement_traceable": True, "basis": "This step has ordered actions and observable completion."}
                for row in analysis["workflow"]],
            "risk_assessments": [{"dimension": row["dimension"], "applicability_justified": True,
                "scenario_links_complete": True,
                "basis": "The dimension's applicability and scenario links were checked."}
                for row in analysis["risk_assessments"]],
            "dependencies_assessed": True, "blocking_decisions_actionable": True,
        }
        validate_analysis_review(review, analysis)
        review["workflow_assessments"].pop()
        with self.assertRaisesRegex(RefinerError, "every workflow step"):
            validate_analysis_review(review, analysis)

    def test_legacy_v1_analysis_and_review_remain_readable_for_existing_runs(self):
        analysis = self._analysis()
        for field in ("schema_version", "context", "scope", "actors", "risk_assessments"):
            analysis.pop(field)
        for step in analysis["workflow"]:
            for field in ("order", "actor_ids", "preconditions", "dependency_ids"):
                step.pop(field)
        validate_analysis(analysis)
        review = {
            "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                                         "acceptance_criteria_atomic": True, "criteria_observable": True,
                                         "basis": "Legacy source and acceptance criterion remain traceable."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": True,
                                      "risk_coverage_complete": True, "traceability_complete": True,
                                      "basis": "The legacy scenario remains complete."}
                                     for row in analysis["scenarios"]],
        }
        validate_analysis_review(review, analysis)

    def test_every_required_atomic_criterion_must_appear_in_an_applicable_scenario(self):
        analysis = self._analysis()
        analysis["requirements"][0]["acceptance_criteria"].append(
            {"id": "R1.A2", "statement": "Keep the result within the requested scope.",
             "evidence": "The response contains no unrequested operation."})
        with self.assertRaisesRegex(RefinerError, "R1.A2"):
            validate_analysis(analysis)

    def test_excluded_scenario_requires_an_explicit_scope_reason(self):
        analysis = self._analysis()
        analysis["scenarios"][2]["exclusion_reason"] = ""
        with self.assertRaises(jsonschema.ValidationError):
            validate_analysis(analysis)

    def test_independent_analysis_review_must_assess_every_scenario_and_criterion(self):
        analysis = self._analysis()
        review = {
            "schema_version": "skill-refiner.analysis-review/v2",
            "approved": True, "issues": [],
            "requirement_assessments": [{"requirement_id": "R1", "grounded": True,
                                         "acceptance_criteria_atomic": True, "criteria_observable": True,
                                         "basis": "R1 and R1.A1 trace to the request."}],
            "scenario_assessments": [{"scenario_id": row["id"], "detail_executable": True,
                                      "risk_coverage_complete": True, "traceability_complete": True,
                                      "basis": "Inputs, actions and outcomes are concrete."}
                                     for row in analysis["scenarios"]],
            "context_scope_assessment": {"intent_grounded": True, "actors_complete": True,
                                         "scope_clear": True, "constraints_complete": True,
                                         "basis": "The request and all material boundaries are explicitly represented."},
            "workflow_assessments": [{"workflow_id": row["id"], "order_and_actors_clear": True,
                                      "inputs_outputs_complete": True, "completion_and_failure_testable": True,
                                      "requirement_traceable": True,
                                      "basis": "The ordered action has observed inputs, outputs, completion and failure."}
                                     for row in analysis["workflow"]],
            "risk_assessments": [{"dimension": row["dimension"], "applicability_justified": True,
                                  "scenario_links_complete": True,
                                  "basis": "Applicability and scenario links match the source and matrix."}
                                 for row in analysis["risk_assessments"]],
            "dependencies_assessed": True, "blocking_decisions_actionable": True,
        }
        validate_analysis_review(review, analysis)
        review["scenario_assessments"].pop()
        with self.assertRaisesRegex(RefinerError, "every scenario exactly once"):
            validate_analysis_review(review, analysis)
        review["scenario_assessments"] = [
            {"scenario_id": row["id"], "detail_executable": True,
             "risk_coverage_complete": row["kind"] != "normal", "traceability_complete": True,
             "basis": "A normal scenario is incorrectly marked incomplete."}
            for row in analysis["scenarios"]]
        with self.assertRaisesRegex(RefinerError, "contradicts"):
            validate_analysis_review(review, analysis)


class CriterionCoverageRepairTests(unittest.TestCase):
    @staticmethod
    def _plan_with_nine_existing_cases():
        plan = _minimal_plan()
        plan["requirements"].extend([
            {"id": "R3", "behavior": "Handle scoped impact", "source": "requirements.md#R3",
             "required": True, "acceptance_criteria": [
                 {"id": "R3.A2", "statement": "Record the scoped impact.",
                  "evidence": "The resulting record identifies the scoped impact."}]},
            {"id": "R5", "behavior": "Preserve workflow ownership", "source": "requirements.md#R5",
             "required": True, "acceptance_criteria": [
                 {"id": "R5.A3", "statement": "Keep the assigned owner.",
                  "evidence": "The output retains the assigned owner."},
                 {"id": "R5.A4", "statement": "Report the next action.",
                  "evidence": "The output contains the next action."}]},
        ])
        kinds = ["normal", "boundary", "error", "negative", "regression",
                 "normal", "boundary", "error", "regression"]
        plan["cases"] = [
            _case(f"c{index}", kind, f"Operator runs c{index}.",
                  f"c{index} produces its documented result.", f"Run c{index}.")
            for index, kind in enumerate(kinds, 1)
        ]
        for step in plan["execution_plan"]["steps"]:
            step["case_ids"] = [f"c{index}" for index in range(1, 10)]
        return plan

    @staticmethod
    def _coverage_error(ids):
        return PlanValidationError("acceptance criterion coverage is incomplete", {
            "schema_version": "skill-refiner.plan-controls/v1", "status": "invalid_coverage",
            "coverage_failures": [{
                "field_path": "requirements[].acceptance_criteria[].case_coverage",
                "expected_condition": "every required atomic criterion is linked to an executable case",
                "actual_value": {"uncovered_criterion_ids": list(ids)},
            }],
            "case_control_results": [], "control_failures": [],
        })

    @staticmethod
    def _corrected_plan(previous):
        plan = copy.deepcopy(previous)
        additions = [("c10", "R3", "R3.A2", "normal"),
                     ("c11", "R5", "R5.A3", "boundary"),
                     ("c12", "R5", "R5.A4", "error")]
        previous_step = plan["execution_plan"]["steps"][-1]["id"]
        for case_id, requirement_id, criterion_id, kind in additions:
            case = _case(case_id, kind, f"Operator runs {criterion_id}.",
                         f"The output proves {criterion_id}.", f"Run {case_id}.", requirement_id)
            case["acceptance_criteria_ids"] = [criterion_id]
            case["asset_audit"]["source_refs"] = [f"requirements.md#{requirement_id}"]
            plan["cases"].append(case)
            step = copy.deepcopy(plan["execution_plan"]["steps"][-1])
            step.update({
                "id": f"STEP-{case_id.upper()}", "order": len(plan["execution_plan"]["steps"]) + 1,
                "title": f"Run {criterion_id} control", "goal": f"Verify {criterion_id} with its new case.",
                "actions": [f"Execute {case_id} and inspect its observable result for {criterion_id}."],
                "outputs": [{"path": f"evidence/{case_id}.json", "schema_id": "S-RESULT",
                             "schema_version": "v1", "evidence": f"The executed {case_id} result."}],
                "requirement_ids": [requirement_id], "case_ids": [case_id],
                "depends_on": [previous_step],
            })
            step["agent"]["scope"] = f"Run and capture the {case_id} evidence only."
            plan["execution_plan"]["steps"].append(step)
            previous_step = step["id"]
        return plan

    def test_missing_atomic_ids_are_given_deterministic_case_repairs_and_corrected_matrix_passes(self):
        incomplete = self._plan_with_nine_existing_cases()
        corrected = self._corrected_plan(incomplete)
        expected_ids = ["R3.A2", "R5.A3", "R5.A4"]
        self.assertEqual(_uncovered_acceptance_criterion_ids(self._coverage_error(expected_ids).evidence), expected_ids)

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.plan_calls = 0
                self.repair_request = None

            def ask(self, role, instruction, payload, schema):
                if role == "plan":
                    self.plan_calls += 1
                    if self.plan_calls == 1:
                        return incomplete
                    self.repair_request = payload.get("required_criterion_coverage_repair")
                    return corrected
                review = _approved_review(corrected)
                review["recovery_assessments"] = [
                    {"change_id": f"execution-add:STEP-C{number}", "preserves_requirements": True,
                     "reason": "The new step and case directly cover the frozen criterion without changing prior requirements."}
                    for number in (10, 11, 12)
                ] + [
                    {"change_id": f"execution-add-case:c{number}", "preserves_requirements": True,
                     "reason": "The new case is directly linked to its frozen atomic criterion."}
                    for number in (10, 11, 12)
                ]
                return review

        phases = []
        with tempfile.TemporaryDirectory() as root, patch(
                "refiner.pipeline.validate_plan",
                side_effect=[self._coverage_error(expected_ids), {"case_control_results": []}]) as validator:
            agent = Agent(root)
            accepted = make_plan(agent, {}, root, 12, max_attempts=2,
                                 phase_callback=lambda name, status, **detail:
                                 phases.append((name, status, detail.get("reason"))))
            attempt = read_json(Path(root) / "plan-controls" / "attempts" / "development-01.json")

        self.assertEqual(accepted["cases"][-3:], corrected["cases"][-3:])
        self.assertEqual(agent.plan_calls, 2)
        self.assertEqual(validator.call_count, 2)
        self.assertEqual(agent.repair_request["uncovered_criterion_ids"], expected_ids)
        self.assertEqual([row["suggested_case_id"] for row in agent.repair_request["required_case_repairs"]],
                         ["c10", "c11", "c12"])
        self.assertEqual(attempt["criterion_coverage_ids"], expected_ids)
        self.assertFalse(attempt["criterion_coverage_repair_applied"])
        self.assertIn(("plan_self_check", "passed", None), phases)

    def test_repeated_uncovered_fingerprint_stops_before_third_plan_call(self):
        incomplete = self._plan_with_nine_existing_cases()
        ids = ["R3.A2", "R5.A3", "R5.A4"]

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.plan_calls = 0

            def ask(self, role, instruction, payload, schema):
                if role == "plan":
                    self.plan_calls += 1
                    return incomplete
                raise AssertionError("Repeated plan coverage must stop before independent review")

        phases = []
        with tempfile.TemporaryDirectory() as root, patch(
                "refiner.pipeline.validate_plan",
                side_effect=[self._coverage_error(ids), self._coverage_error(ids), self._coverage_error(ids)]):
            agent = Agent(root)
            with self.assertRaisesRegex(PlanGateError, "PLAN_NOT_PASSED.*R3.A2.*R5.A3.*R5.A4"):
                make_plan(agent, {}, root, 12, max_attempts=3,
                          phase_callback=lambda name, status, **detail:
                          phases.append((name, status, detail.get("reason"))))
            repeated_attempt = read_json(Path(root) / "plan-controls" / "attempts" / "development-02.json")

        self.assertEqual(agent.plan_calls, 2)
        self.assertEqual(repeated_attempt["criterion_coverage_ids"], ids)
        self.assertTrue(repeated_attempt["criterion_coverage_repair_applied"])
        self.assertIn(("plan_gate", "not_passed", "repeated_criterion_coverage_failure"), phases)

    def test_authenticated_resume_history_with_repeated_repair_stops_before_any_new_agent_call(self):
        plan = self._plan_with_nine_existing_cases()
        ids = ["R3.A2", "R5.A3", "R5.A4"]
        record = self._coverage_error(ids).evidence
        record.update({"attempt": 2, "plan_snapshot": plan, "plan_sha256": digest(plan),
                       "criterion_coverage_ids": ids, "criterion_coverage_repair_applied": True})

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.calls = 0

            def ask(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Authenticated repeated coverage history must stop before another call")

        phases = []
        recovery = {"control_records": [record]}
        recovered_material = {"feedback": [], "sources": [], "case_ids": [], "execution_revision": [],
                              "reference": {**recovery, "previous_plan": plan}}
        with tempfile.TemporaryDirectory() as root, \
                patch("refiner.pipeline.verified_response_sources", return_value=({}, [])), \
                patch("refiner.pipeline._recovery_feedback_material", return_value=recovered_material):
            agent = Agent(root)
            with self.assertRaisesRegex(PlanGateError, "authenticated history.*stopped before another plan-agent call"):
                make_plan(agent, {"_response_contract_v7": True}, root, 12, max_attempts=2,
                          phase_callback=lambda name, status, **detail:
                          phases.append((name, status, detail.get("reason"))), recovery=recovery)

        self.assertEqual(agent.calls, 0)
        self.assertIn(("plan_gate", "not_passed", "repeated_criterion_coverage_failure"), phases)

    def test_ordinary_resume_uses_local_control_to_make_one_repair_and_pass(self):
        incomplete = self._plan_with_nine_existing_cases()
        corrected = self._corrected_plan(incomplete)
        ids = ["R3.A2", "R5.A3", "R5.A4"]

        class FirstAgent:
            def __init__(self, work):
                self.work = Path(work)
                self.calls = 0

            def ask(self, role, instruction, payload, schema):
                self.calls += 1
                return incomplete

        class ResumedAgent:
            def __init__(self, work):
                self.work = Path(work)
                self.calls = 0
                self.repair = None

            def ask(self, role, instruction, payload, schema):
                if role == "plan":
                    self.calls += 1
                    self.repair = payload.get("required_criterion_coverage_repair")
                    return corrected
                review = _approved_review(corrected)
                review["recovery_assessments"] = [
                    {"change_id": f"execution-add:STEP-C{number}", "preserves_requirements": True,
                     "reason": "The new step and case directly cover the frozen criterion without changing prior requirements."}
                    for number in (10, 11, 12)
                ] + [
                    {"change_id": f"execution-add-case:c{number}", "preserves_requirements": True,
                     "reason": "The new case is directly linked to its frozen atomic criterion."}
                    for number in (10, 11, 12)
                ]
                return review

        with tempfile.TemporaryDirectory() as root:
            with patch("refiner.pipeline.validate_plan", side_effect=[self._coverage_error(ids)]):
                with self.assertRaises(PlanGateError):
                    make_plan(FirstAgent(root), {}, root, 12, max_attempts=1)
            with patch("refiner.pipeline.validate_plan", return_value={"case_control_results": []}) as validator:
                resumed = ResumedAgent(root)
                accepted = make_plan(resumed, {}, root, 12, max_attempts=2)

            self.assertEqual(resumed.calls, 1)
            self.assertEqual(validator.call_count, 1)
            self.assertEqual(resumed.repair["uncovered_criterion_ids"], ids)
            self.assertEqual(accepted["cases"][-3:], corrected["cases"][-3:])
            attempt_numbers = sorted(int(path.stem.rsplit("-", 1)[1])
                                     for path in (Path(root) / "plan-controls" / "attempts").glob("development-*.json"))
            self.assertEqual(attempt_numbers, [1, 2])

    def test_ordinary_resume_blocks_after_a_structured_repair_repeats_same_gap(self):
        incomplete = self._plan_with_nine_existing_cases()
        ids = ["R3.A2", "R5.A3", "R5.A4"]

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.calls = 0

            def ask(self, role, instruction, payload, schema):
                self.calls += 1
                return incomplete

        with tempfile.TemporaryDirectory() as root:
            with patch("refiner.pipeline.validate_plan",
                       side_effect=[self._coverage_error(ids), self._coverage_error(ids)]):
                with self.assertRaisesRegex(PlanGateError, "PLAN_NOT_PASSED"):
                    make_plan(Agent(root), {}, root, 12, max_attempts=2)
            resumed = Agent(root)
            with self.assertRaisesRegex(PlanGateError, "authenticated history.*stopped before another plan-agent call"):
                make_plan(resumed, {}, root, 12, max_attempts=2)

        self.assertEqual(resumed.calls, 0)


class RefineBudgetCapMigrationTests(unittest.TestCase):
    @staticmethod
    def _write_fixture(root, *, state_overrides=None, approval_overrides=None):
        root = Path(root) / "refine-continuation"
        root.mkdir(parents=True)
        state = {
            "status": "plan_not_passed", "workspace": str(root.resolve()),
            "phase_results": {"plan_gate": {"status": "not_passed"}},
            "target_evaluations": 0, "evaluation_history": [], "active_evaluation": None,
            "agent_calls": 27, "elapsed_seconds": 2453.82,
        }
        state.update(state_overrides or {})
        contract = {"schema_version": "skill-refiner.resume/v1",
                    "limits": {"max_agent_calls": 47, "max_seconds": 7200, "max_evaluations": 120},
                    "execution_sha256": "a" * 64}
        state_path, contract_path = root / "state.json", root / "resume-contract.json"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

        approval = {
            "schema_version": "goalexecutor.budget-approval/v1", "approval_id": "APR-GE-001",
            "decision": "approved_limited_extension",
            "scope": "Continue the existing create and refine run lineages without resetting or rerunning completed stages.",
            "caps": {"refine": {"cumulative_max_agent_calls": 47,
                                 "cumulative_max_seconds": 7200, "max_target_evaluations": 24}},
            "prior_usage": {"refine": {"agent_calls": 27, "seconds_approx": 2453.82,
                                        "target_evaluations": 0}},
            "remaining": {"refine": {"agent_calls": 20, "seconds_approx": 4746.18,
                                      "target_evaluations": 24}},
            "constraints": ["Do not reset counters or rewrite historical records."],
        }
        approval.update(approval_overrides or {})
        approval_path = Path(root).parent / ".tasks" / "artifacts" / "GE-test" / "art_067.json"
        approval_path.parent.mkdir(parents=True, exist_ok=True)
        approval_path.write_text(json.dumps(approval, ensure_ascii=False), encoding="utf-8")
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        migration = {
            "schema_version": "skill-refiner.refine-budget-cap-migration/v1",
            "approval_id": "APR-GE-001", "source_run": str(root.resolve()), "lineage_id": root.name,
            "source_state_sha256": sha(state_path), "source_resume_contract_sha256": sha(contract_path),
            "approved_limits": {"max_agent_calls": 47, "max_seconds": 7200, "max_evaluations": 24},
            "approval_artifact_path": str(approval_path.resolve()),
            "approval_artifact_sha256": sha(approval_path),
        }
        (root / "refine-budget-cap-migration.json").write_text(
            json.dumps(migration, ensure_ascii=False), encoding="utf-8")
        return root, state, contract, migration, approval_path

    def test_approved_zero_evaluation_migration_preserves_historical_limit_and_binds_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, state, contract, migration, approval_path = self._write_fixture(temporary)
            requested = {"max_agent_calls": 47, "max_seconds": 7200, "max_evaluations": 24}
            with patch("refiner.pipeline._evaluation_accounting",
                       return_value={"total_cost": 0, "unresolved_attempts": []}):
                changed = _apply_refine_budget_cap_migration(root, contract, state, requested)
            self.assertTrue(changed)
            self.assertEqual(contract["limits"]["max_evaluations"], 120)
            self.assertEqual(contract["effective_limits"], requested)
            self.assertEqual(hashlib.sha256(approval_path.read_bytes()).hexdigest(),
                             migration["approval_artifact_sha256"])
            self.assertEqual((root / "resume-history" / "budget-cap-source-state.json").read_bytes(),
                             (root / "state.json").read_bytes())
            self.assertEqual((root / "resume-history" / "budget-cap-source-resume-contract.json").read_bytes(),
                             (root / "resume-contract.json").read_bytes())
            (root / "resume-contract.json").write_text(json.dumps(contract, ensure_ascii=False),
                                                       encoding="utf-8")
            resumed_state = {**state, "status": "resuming", "target_evaluations": 1,
                             "evaluation_history": [{"status": "completed"}]}
            with patch("refiner.pipeline._evaluation_accounting",
                       return_value={"total_cost": 0, "unresolved_attempts": []}):
                self.assertTrue(_apply_refine_budget_cap_migration(root, contract, resumed_state, requested))
            self.assertEqual(contract["limits"]["max_evaluations"], 120)
            self.assertEqual(contract["effective_limits"], requested)

    def test_migration_rejects_missing_approval_or_any_started_evaluation(self):
        requested = {"max_agent_calls": 47, "max_seconds": 7200, "max_evaluations": 24}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "no-approval"
            root.mkdir()
            contract = {"limits": {"max_agent_calls": 47, "max_seconds": 7200, "max_evaluations": 120}}
            with self.assertRaisesRegex(RefinerError, "without a bound user-approved migration"):
                _apply_refine_budget_cap_migration(root, contract, {"target_evaluations": 0}, requested)

        for state_override in ({"target_evaluations": 1, "evaluation_history": [{"status": "completed"}]},
                               {"status": "complete", "target_evaluations": 0},
                               {"phase_results": {"plan_gate": {"status": "passed"}}}):
            with self.subTest(state_override=state_override), tempfile.TemporaryDirectory() as temporary:
                root, state, contract, _, _ = self._write_fixture(temporary, state_overrides=state_override)
                with patch("refiner.pipeline._evaluation_accounting",
                           return_value={"total_cost": 0, "unresolved_attempts": []}):
                    with self.assertRaisesRegex(RefinerError, "same plan_not_passed, zero-evaluation run"):
                        _apply_refine_budget_cap_migration(root, contract, state, requested)

    def test_migration_rejects_a_changed_hashed_old_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, state, contract, _, _ = self._write_fixture(temporary)
            contract_path = root / "resume-contract.json"
            contract_path.write_text(json.dumps({**contract, "limits": {**contract["limits"],
                                                                           "max_evaluations": 99}}),
                                     encoding="utf-8")
            with patch("refiner.pipeline._evaluation_accounting",
                       return_value={"total_cost": 0, "unresolved_attempts": []}):
                with self.assertRaisesRegex(RefinerError, "hashed source file|contract hash mismatch"):
                    _apply_refine_budget_cap_migration(root, contract, state,
                                                       {"max_agent_calls": 47, "max_seconds": 7200,
                                                        "max_evaluations": 24})


if __name__ == "__main__":
    unittest.main()

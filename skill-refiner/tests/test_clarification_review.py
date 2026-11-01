"""Offline source-grounded, bounded clarification review tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_analysis_contract_v3 as fixtures
from refiner.agent import Agent
from refiner.cli import main
from refiner.common import Cancelled, RefinerError, read_json, write_json
from refiner.creation import validate_clarification_review, validate_clarification_revision
from refiner.preflight import build_runtime_preflight


class ClarificationReviewTests(unittest.TestCase):
    source = "业务要求处理影响服务的工单；部分用户完全不可用时等级未明。每张工单返回一个结果。"

    def analysis(self):
        value = fixtures.AnalysisV3Tests("runTest").analysis()
        value["requirements"][0]["source"] = "business_description: 业务要求处理影响服务的工单"
        value["blocking_questions"] = [
            {"id": "BQ1", "question": "部分用户完全不可用时应如何分级？", "decision_owner": "业务负责人",
             "impact": "已有故障分类规则缺少此影响范围的明确处理。"},
            {"id": "BQ2", "question": "多项无关问题应如何合并？", "decision_owner": "业务负责人",
             "impact": "来源没有要求处理多个互不相关问题的聚合策略。"}]
        return value

    def row(self, question_id, classification):
        return {"question_id": question_id, "classification": classification,
                "evidence": [{"source_id": "business_description", "quote": "业务要求处理影响服务的工单"}],
                "reason": "基于原始业务范围和现有要求逐项核对，不推断额外业务规则。",
                "combination_risk_assessed": True, "required_requirement_ids_examined": ["R1"],
                "out_of_scope": "未要求聚合多个互不相关问题的优先级策略。" if classification == "unsupported_extra" else ""}

    def review(self, rows):
        return {"schema_version": "skill-refiner.clarification-review/v1", "question_assessments": rows}

    def test_source_quotes_and_all_required_scope_are_checked(self):
        analysis = self.analysis()
        required = self.row("BQ1", "required_in_scope")
        extra = self.row("BQ2", "unsupported_extra")
        review = self.review([required, extra])
        sources = [{"id": "business_description", "content": self.source}]
        validate_clarification_review(review, analysis, sources)
        for mutation in ("bad_quote", "punctuation_quote", "unknown_source", "unexamined_requirement", "missing_question", "unchecked_overlap"):
            candidate = copy.deepcopy(review)
            if mutation == "bad_quote":
                candidate["question_assessments"][1]["evidence"][0]["quote"] = "原文不存在"
            elif mutation == "punctuation_quote":
                candidate["question_assessments"][1]["evidence"][0]["quote"] = "。"
            elif mutation == "unknown_source":
                candidate["question_assessments"][1]["evidence"][0]["source_id"] = "unknown"
            elif mutation == "unexamined_requirement":
                candidate["question_assessments"][1]["required_requirement_ids_examined"] = []
            elif mutation == "missing_question":
                candidate["question_assessments"].pop()
            else:
                candidate["question_assessments"][1]["combination_risk_assessed"] = False
            with self.subTest(mutation=mutation), self.assertRaises(RefinerError):
                validate_clarification_review(candidate, analysis, sources)

    def test_correction_preserves_required_and_uncertain_questions_and_every_other_field(self):
        before = self.analysis()
        review = self.review([self.row("BQ1", "uncertain"), self.row("BQ2", "unsupported_extra")])
        after = copy.deepcopy(before)
        after["blocking_questions"] = [after["blocking_questions"][0]]
        after["scope"]["out_of_scope"].append(review["question_assessments"][1]["out_of_scope"])
        validate_clarification_revision(before, after, review)
        for mutation in ("requirement", "scenario", "retained_question", "scope"):
            altered = copy.deepcopy(after)
            if mutation == "requirement":
                altered["requirements"][0]["behavior"] = "偷偷变更已有业务要求。"
            elif mutation == "scenario":
                altered["scenarios"].pop()
            elif mutation == "retained_question":
                altered["blocking_questions"] = []
            else:
                altered["scope"]["out_of_scope"][-1] = "虚构的一条范围外说明。"
            with self.subTest(mutation=mutation), self.assertRaises(RefinerError):
                validate_clarification_revision(before, altered, review)

    def run_creation(self, *, speculative, break_revision=False, second_unsupported=False,
                     first_classification="required_in_scope", invalid_review=False, only_extra=False,
                     cancel_review=False, technical=False, stop_at_draft=False,
                     max_cases=12, acceptance_cases=12, scenario_count=None,
                     only_capacity_question=False, frozen_plan_overflow=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        config, work = base / "config.json", base / "run"
        write_json(config, {"schema_version": 1, "agents": {"default": {"backend": "command", "command": ["fixture"]}},
                            "executors": [{"id": "fake", "backend": "command", "command": ["fixture"]}]})
        before = self.analysis()
        if technical:
            before["blocking_questions"] = [copy.deepcopy(before["blocking_questions"][0])]
            before["blocking_questions"][0]["question"] = "实际执行装置能否独立观察临时写入？"
        if scenario_count is not None:
            template = next(row for row in before["scenarios"] if row["applicable"])
            for index in range(sum(bool(row["applicable"]) for row in before["scenarios"]), scenario_count):
                scenario = copy.deepcopy(template)
                scenario["id"] = f"SC-CAP-{index:02d}"
                scenario["situation"] = f"独立的容量控制场景 {index}，验证同一业务要求的具体变体。"
                scenario["detail"]["title"] = f"容量控制场景 {index}"
                scenario["detail"]["stimulus"] = f"提交容量控制用例 {index} 的不同业务输入。"
                scenario["detail"]["basis"] = f"该独立变体仍依据来源要求 R1，编号 {index}。"
                scenario["basis"] = f"该独立变体覆盖来源要求 R1，编号 {index}。"
                before["scenarios"].append(scenario)
            for risk in before["risk_assessments"]:
                if risk["dimension"] == "input_variation" and risk["applicable"]:
                    risk["scenario_ids"] = [row["id"] for row in before["scenarios"] if row["applicable"]]
            if scenario_count > 24:
                before["blocking_questions"].append({
                    "id": "BQ3", "question": "是否扩大开发与验收用例容量？",
                    "decision_owner": "Refiner 控制器容量配置", "impact": "场景数可能超过冻结的用例上限。"
                })
        if only_capacity_question:
            before["blocking_questions"] = [row for row in before["blocking_questions"] if row["id"] == "BQ3"]
        elif only_extra:
            before["blocking_questions"] = before["blocking_questions"][1:]
        elif not speculative:
            before["blocking_questions"] = [row for row in before["blocking_questions"]
                                            if row["id"] == "BQ1" or row["id"] == "BQ3"]
        rows = [] if only_extra else [self.row("BQ1", first_classification)]
        if speculative:
            rows.append(self.row("BQ2", "unsupported_extra"))
        if scenario_count is not None and scenario_count > 24:
            rows.append(self.row("BQ3", "uncertain"))
        if invalid_review:
            rows[-1]["evidence"][0]["quote"] = "并不存在于原文的证据"
        review = self.review(rows)
        corrected = copy.deepcopy(before)
        if speculative:
            corrected["blocking_questions"] = [] if only_extra else corrected["blocking_questions"][:1]
            corrected["scope"]["out_of_scope"].append(rows[-1]["out_of_scope"])
            if break_revision:
                corrected["requirements"][0]["behavior"] = "被错误修改的原始需求"
        roles = []
        def fake_ask(agent, role, instruction, payload, schema):
            roles.append(role)
            if role == "analyze":
                if "previous_analysis" in payload:
                    self.assertEqual(payload["previous_analysis"], before)
                    self.assertEqual(payload["clarification_review"], review)
                    return corrected
                return before
            if role == "analysis-review":
                if cancel_review:
                    raise Cancelled("用户取消")
                if schema.get("$id") == "skill-refiner.technical-triage":
                    assessment_rows = []
                    for row in payload["analysis"]["blocking_questions"]:
                        is_technical = technical and row["id"] == "BQ1"
                        is_capacity = row["id"] == "BQ3"
                        assessment_rows.append({
                            "question_id": row["id"],
                            "domain": ("technical_evidence" if is_technical else
                                       "uncertain" if is_capacity else "business_decision"),
                            "reason": ("核实运行器对实际副作用动作的独立观测范围。" if is_technical
                                       else "案例上限属于控制器约束，由容量预检按冻结计划判定。" if is_capacity
                                       else "该问题涉及尚未确定的业务行为及规则。"),
                            "requirement_ids": ["R1"] if is_technical else [],
                            "scenario_ids": [next(s["id"] for s in payload["analysis"]["scenarios"]
                                                   if s["applicable"] and "R1" in s["requirement_ids"])]
                                             if is_technical else []
                        })
                    return {"schema_version": "skill-refiner.technical-triage/v1", "assessments": assessment_rows}
                if schema.get("$id") != "skill-refiner.clarification-review":
                    return fixtures.AnalysisV3Tests("runTest").review(corrected)
                self.assertIn("never treat missing keywords alone", instruction)
                active_ids = {row["id"] for row in payload["analysis"]["blocking_questions"]}
                active_rows = [row for row in review["question_assessments"]
                               if row["question_id"] in active_ids]
                if len(roles) == 2:
                    return self.review(active_rows)
                return self.review([self.row("BQ1", "unsupported_extra" if second_unsupported else "required_in_scope")])
            if role == "draft" and frozen_plan_overflow:
                return {"implementation_notes": "按已批准分析生成用于离线容量门测试的最小文件。",
                        "files": [{"path": "SKILL.md",
                                   "content": "---\nname: demo-skill\ndescription: 用于离线容量门测试的 Skill。\n---\n\n# 演示\n\n处理已批准的业务要求。\n",
                                   "reason": "提供真实创建流程需要的 Skill 文件。",
                                   "requirement_ids": ["R1"]}]}
            if role == "draft" and (only_extra or stop_at_draft):
                raise RefinerError("offline controlled stop before draft")
            raise AssertionError("No draft or target evaluation should run while a required question is pending")
        evaluator_calls = []
        def fake_refine(child):
            report = build_runtime_preflight(
                mode="create", phase="after_frozen_plan",
                limits={"max_seconds": child.max_seconds, "max_agent_calls": child.max_agent_calls,
                        "max_evaluations": child.max_evaluations, "max_cases": child.max_cases,
                        "acceptance_cases": child.acceptance_cases},
                usage={"agent_calls": 0, "elapsed_seconds": 0, "target_evaluations": 0},
                applicable_scenario_count=25,
                planned_case_counts={"development": 13, "acceptance": 12})
            if report["status"] != "failed":
                evaluator_calls.append("target-evaluator")
            return {"status": "blocked" if report["status"] == "failed" else "passed",
                    "error": json.dumps(report["issues"], ensure_ascii=False),
                    "target_evaluations": len(evaluator_calls), "agent_calls": 0,
                    "evaluation_history": [], "rounds": []}

        arguments = ["create", self.source, "--name", "demo-skill", "--config", str(config),
                     "--output", str(work), "--max-cases", str(max_cases),
                     "--acceptance-cases", str(acceptance_cases),
                     "--max-agent-calls", "32", "--max-seconds", "3600"]
        with patch("refiner.agent.Agent.ask", fake_ask):
            if frozen_plan_overflow:
                with patch("refiner.creation.lint", return_value=[]), \
                     patch("refiner.creation.refine", side_effect=fake_refine):
                    result = main(arguments)
            else:
                result = main(arguments)
        self.evaluator_calls = evaluator_calls
        return result, work, before, corrected, roles

    def test_unrequested_extra_is_reviewed_and_revised_while_real_ambiguity_blocks(self):
        result, work, before, corrected, roles = self.run_creation(speculative=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analyze", "analysis-review", "analysis-review"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "analysis-attempt-2.json"), corrected)
        self.assertEqual(read_json(work / "analysis.json"), corrected)
        self.assertEqual(read_json(work / "state.json")["status"], "needs_clarification")
        self.assertEqual([q["id"] for q in corrected["blocking_questions"]], ["BQ1"])
        stage = next(s for s in read_json(work / "stages.json")["stages"] if s["id"] == "analysis")
        self.assertNotEqual(stage["status"], "completed")

    def test_explicit_or_implicit_required_ambiguity_is_not_discarded(self):
        result, work, before, _, roles = self.run_creation(speculative=False)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analysis-review"])
        self.assertEqual(read_json(work / "analysis.json"), before)
        self.assertEqual(read_json(work / "state.json")["status"], "needs_clarification")

    def test_uncertain_implicit_combination_is_retained(self):
        result, work, before, _, roles = self.run_creation(speculative=False, first_classification="uncertain")
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analysis-review"])
        state = read_json(work / "state.json")
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(state["status"], "error")
        self.assertIn("no user clarification was requested", state["error"])
        self.assertEqual(state.get("questions", []), [])

    def test_technical_observation_gap_is_recorded_without_user_question(self):
        result, work, _, _, roles = self.run_creation(
            speculative=False, technical=True, stop_at_draft=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analysis-review", "analysis-review", "draft"])
        state = read_json(work / "state.json")
        self.assertEqual(state["status"], "error")  # controlled stop before implementation
        self.assertNotEqual(state["status"], "needs_clarification")
        self.assertEqual(state.get("questions", []), [])
        coverage = read_json(work / "technical-coverage.json")
        self.assertEqual(coverage["status"], "coverage_gap")
        self.assertEqual([row["question_id"] for row in coverage["questions"]], ["BQ1"])

    def test_controller_capacity_question_is_routed_without_user_clarification(self):
        result, work, _, _, roles = self.run_creation(
            speculative=False, max_cases=12, acceptance_cases=12, scenario_count=25)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analysis-review"])
        state = read_json(work / "state.json")
        self.assertEqual(state["status"], "needs_clarification")
        self.assertEqual(state.get("questions", []), ["部分用户完全不可用时应如何分级？"])
        self.assertEqual([row["id"] for row in read_json(work / "analysis.json")["blocking_questions"]], ["BQ1"])
        routing = read_json(work / "controller-capacity-routing-attempt-1.json")
        self.assertEqual([row["id"] for row in routing["routed_questions"]], ["BQ3"])
        self.assertEqual(routing["decision"], "defer_to_frozen_plan_case_ids_and_coverage_links")
        self.assertFalse(routing["user_clarification_requested"])
        self.assertFalse(any(role == "draft" for role in roles))
        self.assertFalse((work / "technical-coverage.json").exists())

    def test_actual_create_flow_routes_capacity_then_blocks_13_case_plan_before_evaluator(self):
        result, work, _, _, roles = self.run_creation(
            speculative=False, only_capacity_question=True, frozen_plan_overflow=True,
            max_cases=12, acceptance_cases=12, scenario_count=25)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "draft"])
        state = read_json(work / "state.json")
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["target_evaluations"], 0)
        self.assertEqual(self.evaluator_calls, [])
        self.assertIn("PLANNED_CASES_EXCEED_LIMIT", state["error"])
        self.assertIn('"expected": 12', state["error"])
        self.assertIn('"actual": 13', state["error"])
        self.assertEqual(state.get("questions", []), [])

    def test_only_unsupported_extra_does_not_block_a_reviewed_analysis(self):
        result, work, before, corrected, roles = self.run_creation(speculative=True, only_extra=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analyze", "analysis-review", "draft"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "analysis.json"), corrected)
        self.assertEqual(corrected["blocking_questions"], [])
        self.assertEqual(read_json(work / "state.json").get("questions", []), [])
        stage = next(s for s in read_json(work / "stages.json")["stages"] if s["id"] == "analysis")
        self.assertEqual(stage["status"], "completed")
        self.assertEqual(read_json(work / "state.json")["target_evaluations"], 0)

    def test_invalid_review_evidence_cannot_trigger_unconstrained_analysis_retry(self):
        result, work, before, _, roles = self.run_creation(speculative=True, invalid_review=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "state.json")["status"], "error")
        self.assertFalse((work / "analysis.json").exists())

    def test_user_cancellation_is_not_misreported_or_retried(self):
        result, work, before, _, roles = self.run_creation(speculative=True, cancel_review=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "state.json")["status"], "cancelled")

    def test_review_call_budget_and_completed_cache_survive_resume(self):
        schema = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
        config = {"agents": {"default": {"backend": "command", "command": ["fixture"]}}}
        with tempfile.TemporaryDirectory() as directory:
            first = Agent(directory, config=config)
            first.max_calls = 1
            with patch("refiner.agent.run_process", return_value=(0, '{"status":"ok"}', "", 0.01)) as backend:
                self.assertEqual(first.ask("analysis-review", "review", {"question": "Q1"}, schema), {"status": "ok"})
                self.assertEqual(backend.call_count, 1)
            restored = Agent(directory, config=config)
            restored.max_calls = 1
            with patch("refiner.agent.run_process", side_effect=AssertionError("backend must not run")):
                self.assertEqual(restored.ask("analysis-review", "review", {"question": "Q1"}, schema), {"status": "ok"})
                with self.assertRaisesRegex(RefinerError, "budget exhausted"):
                    restored.ask("analysis-review", "review", {"question": "Q2"}, schema)
            self.assertEqual(restored.calls, 1)
            self.assertTrue((Path(directory) / "agent-calls/001-analysis-review/response.json").is_file())
            self.assertFalse((Path(directory) / "agent-calls/002-analysis-review").exists())

    def test_revision_that_changes_original_business_requirements_fails_closed(self):
        result, work, before, _, roles = self.run_creation(speculative=True, break_revision=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analyze"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "state.json")["status"], "error")
        self.assertFalse((work / "analysis.json").exists())

    def test_nonconvergent_clarification_review_stops_at_existing_budget(self):
        result, work, before, _, roles = self.run_creation(speculative=True, second_unsupported=True)
        self.assertEqual(result, 2)
        self.assertEqual(roles, ["analyze", "analysis-review", "analyze", "analysis-review"])
        self.assertEqual(read_json(work / "analysis-attempt-1.json"), before)
        self.assertEqual(read_json(work / "state.json")["status"], "error")

    def test_original_schema_and_meaningful_minimum_are_supplied_to_analyzer(self):
        schema = {"type": "object", "properties": {"situation": {"type": "string", "minLength": 8}},
                  "required": ["situation"]}
        seen = []
        def fake_process(*args, **kwargs):
            seen.append(json.loads(kwargs["input_text"]))
            return 0, '{"situation":"具有明确事实的工单问题"}', "", 0.01
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(directory, config={"agents": {"default": {"backend": "command", "command": ["fixture"]}}})
            with patch("refiner.agent.run_process", side_effect=fake_process):
                agent.ask("analyze", "analyze", {}, schema)
        self.assertEqual(seen[0]["input"]["canonical_response_schema"], schema)
        self.assertIn("at least eight meaningful characters", seen[0]["instruction"])
        self.assertIn("do not pad", seen[0]["instruction"])


if __name__ == "__main__":
    unittest.main()

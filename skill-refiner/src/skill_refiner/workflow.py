from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapters import AgentAdapter, CommandAdapter, CursorSdkAdapter, DeepSeekAdapter, FakeAdapter
from .contracts import ANALYSIS_SCHEMA, PLAN_SCHEMA, PROPOSAL_SCHEMA, REVIEW_SCHEMA
from .errors import EnvironmentError, NeedsUserDecision, VerificationError
from .kernel import RefinerControlKernel, STAGES
from .models import RunMode
from .security import tree_manifest


NodeHandler = Callable[[Any], Awaitable[Any]]


@dataclass
class PipelineRun:
    kernel: RefinerControlKernel
    fixture_root: Path
    plan_only: bool = False
    analysis: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    development_report: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Resume reconstructs only the small routing state that is not already
        # authoritative in the frozen plan or SQLite stage table.
        with self.kernel.store.connect() as db:
            rows = {row["stage_id"]: dict(row) for row in db.execute("SELECT * FROM stages")}
        for stage_id, attribute in (("analyze_requirements", "analysis"), ("review_plan", "review"),
                                    ("development_evaluation", "development_report")):
            row = rows[stage_id]
            if row["status"] == "passed" and row["attempt"]:
                try:
                    setattr(self, attribute, self.kernel.store.get_json(
                        f"stages/{stage_id}/{row['attempt']}/output.json"
                    ))
                except Exception:
                    pass

    def selected_stages(self) -> tuple[str, ...]:
        limit = 5 if self.plan_only else len(STAGES)
        candidates = STAGES[:limit]
        with self.kernel.store.connect() as db:
            statuses = {row["stage_id"]: row["status"] for row in db.execute("SELECT stage_id,status FROM stages")}
        for index, stage in enumerate(candidates):
            if statuses.get(stage) not in {"passed", "skipped"}:
                return candidates[index:]
        return ()

    def _adapter(self, role: str) -> AgentAdapter:
        profile = self.kernel.config.roles[role]
        if profile.kind == "fake":
            return FakeAdapter(profile, self.fixture_root, self.kernel.store)
        if profile.kind == "deepseek":
            return DeepSeekAdapter(profile, self.kernel.store)
        if profile.kind == "cursor_sdk":
            return CursorSdkAdapter(profile, self.kernel.store, self.kernel.config.allowed_environment)
        return CommandAdapter(profile, self.kernel.store, self.kernel.config.allowed_environment)

    def candidate_context(self) -> dict[str, Any]:
        """Return a bounded UTF-8 snapshot for author/repair proposals."""
        manifest = tree_manifest(self.kernel.candidate_dir)
        text_files: dict[str, str] = {}
        omitted_files: list[str] = []
        total = 0
        for relative in sorted(manifest):
            content = (self.kernel.candidate_dir / Path(relative)).read_bytes()
            if len(content) > 50_000 or total + len(content) > 200_000 or b"\x00" in content:
                omitted_files.append(relative)
                continue
            try:
                text_files[relative] = content.decode("utf-8")
                total += len(content)
            except UnicodeDecodeError:
                omitted_files.append(relative)
        return {"manifest": manifest, "text_files": text_files, "omitted_files": omitted_files}

    async def invoke(self, role: str, request: dict[str, Any], schema: dict[str, Any], stage_id: str) -> dict[str, Any]:
        self.kernel.check_budget(calls=1)
        response = await self._adapter(role).invoke(role, request, schema, stage_id=stage_id)
        return response.payload

    async def freeze_inputs(self, _node_input: Any = None) -> dict[str, Any]:
        return await self.kernel.stage("freeze_inputs", self.kernel.freeze_inputs)

    async def analyze_requirements(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            self.analysis = await self.invoke(
                "planner", {"task": "analyze requirements", "requirements": self.kernel.requirements_text()},
                ANALYSIS_SCHEMA, "analyze_requirements",
            )
            return self.analysis
        return await self.kernel.stage("analyze_requirements", action)

    async def build_plan(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            payload = await self.invoke(
                "planner",
                {"task": "build executable test plan", "requirements": self.kernel.requirements_text(),
                 "analysis": self.analysis, "fixed_stages": list(STAGES),
                 "source_context": {
                     "mode": self.kernel.run["mode"],
                     "baseline_manifest": (
                         tree_manifest(self.kernel.baseline_dir)
                         if self.kernel.run["mode"] == RunMode.REFINE.value
                         else {}
                     ),
                 },
                 "executor_contract": {
                     "first_argv_token_must_be_one_of": [
                         "{python}",
                         *(f"{{executor:{name}}}" for name in sorted(self.kernel.config.executors)),
                     ],
                     "python_example": ["{python}", "scripts/example.py", "argument"],
                     "paths_are_candidate_relative": True,
                 },
                 "assertion_contract": {
                     "stdout_contains_expected_is_a_portable_substring_without_cr_or_lf": True,
                     "model_generated_manifest_unchanged_is_forbidden": True,
                     "preservation_checks_use_file_exists_or_python_sha256_checks_against_source_context": True,
                 },
                 "review_contract": {
                     "every_requirement_has_a_direct_observable_assertion": True,
                     "refine_preservation_cases_name_unrelated_source_files_and_compare_hashes": True,
                     "documentation_cases_check_the_explicit_required_heading_and_behavior_text": True,
                  },
                  "work_item_contract": {
                      "one_leaf_is_one_independently_observable_capability_or_invariant": True,
                      "do_not_bundle_outputs_that_can_fail_independently": True,
                      "no_arbitrary_fixed_item_count": True,
                      "every_leaf_has_scope_dependencies_done_when_and_test_mappings": True,
                      "all_requirements_and_all_test_cases_are_assigned": True,
                 }},
                PLAN_SCHEMA, "build_plan",
            )
            return self.kernel.make_plan(payload).model_dump(mode="json")
        return await self.kernel.stage("build_plan", action)

    async def review_plan(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            self.review = await self.invoke(
                "reviewer", {
                    "task": "review frozen plan",
                    "plan": self.kernel.load_plan().model_dump(mode="json"),
                    "required_checks": {
                        "requirement_coverage": "every requirement is assigned to observable evidence",
                        "atomic_work_items": "each leaf is one capability or invariant and does not bundle independent failures",
                        "observable_done_when": "every done_when can be checked from artifacts or test evidence",
                        "evidence_mapping": "all development and held-out acceptance cases are assigned consistently",
                        "dependency_order": "dependencies are coherent and acyclic",
                        "progress_handoff": "a parent can mirror leaves without inventing status or completion rules",
                    },
                },
                REVIEW_SCHEMA, "review_plan",
            )
            return self.review
        return await self.kernel.stage("review_plan", action)

    async def plan_gate(self, _node_input: Any = None) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            checks = self.review.get("checks", {})
            if (
                not self.review.get("approved")
                or self.review.get("issues")
                or not checks
                or not all(checks.values())
            ):
                raise NeedsUserDecision("plan review did not approve execution", details=self.review)
            return {"approved": True, "plan_only": self.plan_only}
        return await self.kernel.stage("plan_gate", action)

    async def capture_baseline(self, _node_input: Any = None) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            if self.kernel.run["mode"] == RunMode.CREATE.value:
                return {"applicable": False}
            return {"applicable": True, "manifest": tree_manifest(self.kernel.baseline_dir)}
        return await self.kernel.stage("capture_baseline", action)

    async def generate_candidate(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            plan = self.kernel.load_plan()
            # Acceptance cases are deliberately withheld from the author.
            request = {
                "task": "propose candidate skill files",
                "requirements": list(plan.requirements),
                "work_items": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "scope": list(item.scope),
                        "requirement_ids": list(item.requirement_ids),
                        "depends_on": list(item.depends_on),
                        "done_when": list(item.done_when),
                        "development_case_ids": list(item.development_case_ids),
                    }
                    for item in plan.work_items
                ],
                "development_cases": [case.model_dump(mode="json") for case in plan.development_cases],
                "current_candidate": self.candidate_context(),
            }
            payload = await self.invoke("author", request, PROPOSAL_SCHEMA, "generate_candidate")
            return self.kernel.apply_proposal(payload)
        return await self.kernel.stage("generate_candidate", action)

    async def static_validation(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            try:
                return self.kernel.static_validate()
            except VerificationError as exc:
                self.kernel.check_budget(repairs=1)
                plan = self.kernel.load_plan()
                payload = await self.invoke(
                    "repair",
                    {
                        "task": "repair the current candidate so deterministic static validation passes",
                        "failure": {"code": exc.code, "message": str(exc), "details": exc.details},
                        "requirements": list(plan.requirements),
                        "development_cases": [case.model_dump(mode="json") for case in plan.development_cases],
                        "current_candidate": self.candidate_context(),
                    },
                    PROPOSAL_SCHEMA,
                    "static_validation",
                )
                self.kernel.apply_proposal(payload)
                return self.kernel.static_validate()
        return await self.kernel.stage("static_validation", action)

    async def development_evaluation(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            self.development_report = await self.kernel.evaluate(
                self.kernel.load_plan().development_cases, "development", raise_on_failure=False,
            )
            return self.development_report
        return await self.kernel.stage("development_evaluation", action)

    async def repair_candidate(self, _node_input: Any = None) -> dict[str, Any]:
        async def action() -> dict[str, Any]:
            if self.development_report.get("passed", True):
                return {"needed": False}
            self.kernel.check_budget(repairs=1)
            payload = await self.invoke(
                "repair",
                {"task": "repair candidate", "failure_report": self.development_report,
                 "development_cases": [case.model_dump(mode="json") for case in self.kernel.load_plan().development_cases]},
                PROPOSAL_SCHEMA, "repair_candidate",
            )
            result = self.kernel.apply_proposal(payload)
            return {"needed": True, **result}
        return await self.kernel.stage("repair_candidate", action)

    async def regression_evaluation(self, _node_input: Any = None) -> dict[str, Any]:
        return await self.kernel.stage(
            "regression_evaluation",
            lambda: self.kernel.evaluate(self.kernel.load_plan().development_cases, "regression"),
        )

    async def acceptance_evaluation(self, _node_input: Any = None) -> dict[str, Any]:
        return await self.kernel.stage(
            "acceptance_evaluation",
            lambda: self.kernel.evaluate(self.kernel.load_plan().acceptance_cases, "acceptance"),
        )

    async def final_verify(self, _node_input: Any = None) -> dict[str, Any]:
        return await self.kernel.stage("final_verify", self.kernel.final_verify)

    async def prepare_delivery(self, _node_input: Any = None) -> dict[str, Any]:
        return await self.kernel.stage("prepare_delivery", self.kernel.prepare_delivery)

    def handlers(self) -> dict[str, NodeHandler]:
        return {stage: getattr(self, stage) for stage in STAGES}


def build_adk_workflow(
    handlers: dict[str, NodeHandler], *, stop_after_plan: bool = False,
    selected_stages: tuple[str, ...] | None = None,
) -> Any:
    """Build and construction-validate the real Google ADK 2.x Workflow graph."""
    try:
        from google.adk.workflow import FunctionNode, START, Workflow
    except Exception as exc:
        raise EnvironmentError("google-adk 2.x is required for the ADK runtime") from exc
    selected = list(selected_stages if selected_stages is not None else (STAGES[:5] if stop_after_plan else STAGES))
    if not selected:
        raise EnvironmentError("ADK workflow has no pending stages")
    nodes = []
    for name in selected:
        handler = handlers[name]

        # Keep the ADK edge contract intentionally untyped. The Kernel performs
        # schema validation; node outputs are only sequencing tokens and must
        # not become an accidental data-coupling contract between stages.
        async def dispatch(node_input, _handler=handler):
            return await _handler(node_input)

        nodes.append(FunctionNode(func=dispatch, name=name))
    workflow = Workflow(name="skill_refiner", edges=[(START, *nodes)], max_concurrency=1)
    graph_names = {node.name for node in workflow.graph.nodes} if workflow.graph else set()
    missing = set(selected) - graph_names
    if missing:
        raise EnvironmentError(f"ADK graph validation lost nodes: {sorted(missing)}")
    return workflow


async def run_adk_workflow(pipeline: PipelineRun) -> dict[str, Any]:
    """Execute the graph through ADK's Runner; no custom scheduler is used."""
    selected = pipeline.selected_stages()
    workflow = build_adk_workflow(pipeline.handlers(), selected_stages=selected)
    try:
        from google.adk.runners import InMemoryRunner
        from google.genai import types
    except Exception as exc:
        raise EnvironmentError("google-adk runtime dependencies are incomplete") from exc
    runner = InMemoryRunner(node=workflow, app_name="skill_refiner")
    terminal: Any = None
    session_id = pipeline.kernel.run["run_id"]
    try:
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id="skill-refiner", session_id=session_id,
        )
        async for event in runner.run_async(
            user_id="skill-refiner", session_id=session_id,
            new_message=types.UserContent(parts=[types.Part(text=json.dumps({"run_dir": str(pipeline.kernel.run_dir)}))]),
        ):
            if getattr(event, "output", None) is not None:
                terminal = event.output
    finally:
        await runner.close()
    return {"driver": "google-adk", "terminal": terminal}


async def run_builtin_workflow(pipeline: PipelineRun) -> dict[str, Any]:
    """Deterministic test driver. Production config must select ``adk``."""
    selected = pipeline.selected_stages()
    output: Any = None
    for stage in selected:
        output = await pipeline.handlers()[stage](output)
    return {"driver": "builtin_test", "terminal": output}

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .contracts import PLAN_SCHEMA, REVIEW_SCHEMA
from .hashing import digest_file, digest_json
from .models import (
    AnalysisOutput,
    BaselineOutput,
    CandidateOutput,
    DeliveryOutput,
    EvaluationReport,
    FinalVerificationOutput,
    FreezeInputsOutput,
    FrozenPlan,
    PlanGateOutput,
    RepairOutput,
    ReviewOutput,
    StageContractRecord,
    StageContractRegistry,
    StaticValidationOutput,
)


STAGES = (
    "freeze_inputs",
    "analyze_requirements",
    "build_plan",
    "review_plan",
    "plan_gate",
    "capture_baseline",
    "generate_candidate",
    "static_validation",
    "development_evaluation",
    "repair_candidate",
    "regression_evaluation",
    "acceptance_evaluation",
    "final_verify",
    "prepare_delivery",
)

STAGE_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "freeze_inputs": FreezeInputsOutput,
    "analyze_requirements": AnalysisOutput,
    "build_plan": FrozenPlan,
    "review_plan": ReviewOutput,
    "plan_gate": PlanGateOutput,
    "capture_baseline": BaselineOutput,
    "generate_candidate": CandidateOutput,
    "static_validation": StaticValidationOutput,
    "development_evaluation": EvaluationReport,
    "repair_candidate": RepairOutput,
    "regression_evaluation": EvaluationReport,
    "acceptance_evaluation": EvaluationReport,
    "final_verify": FinalVerificationOutput,
    "prepare_delivery": DeliveryOutput,
}

STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "freeze_inputs": ("inputs/requirements.md", "inputs/config.json", "contracts/stages.json"),
    "analyze_requirements": ("freeze_inputs",),
    "build_plan": ("analyze_requirements",),
    "review_plan": ("build_plan",),
    "plan_gate": ("review_plan",),
    "capture_baseline": ("plan_gate",),
    "generate_candidate": ("capture_baseline", "plan/frozen.json"),
    "static_validation": ("generate_candidate",),
    "development_evaluation": ("static_validation", "plan/frozen.json"),
    "repair_candidate": ("development_evaluation",),
    "regression_evaluation": ("repair_candidate", "plan/frozen.json"),
    "acceptance_evaluation": ("regression_evaluation", "plan/frozen.json"),
    "final_verify": ("static_validation", "regression_evaluation", "acceptance_evaluation", "plan/frozen.json"),
    "prepare_delivery": ("final_verify", "verification/final.json"),
}

SCHEMA_PATH = "schemas/v1/stage-artifacts.schema.json"
ATTEMPT_SCHEMA_PATH = "schemas/v1/stage-attempt.schema.json"
CONTRACT_SCHEMA_PATH = "schemas/v1/stage-contracts.schema.json"
STAGES_SCHEMA_PATH = "schemas/v1/stages.schema.json"


def registry() -> StageContractRegistry:
    records = []
    for ordinal, stage_id in enumerate(STAGES, 1):
        model = STAGE_OUTPUT_MODELS[stage_id]
        records.append(StageContractRecord(
            stage_id=stage_id,
            ordinal=ordinal,
            input_schema_id=f"skill-refiner.stage.{stage_id}.input/v1",
            output_schema_id=f"skill-refiner.stage.{stage_id}.output/v1",
            schema_version=1,
            schema_path=SCHEMA_PATH,
            validator_id=f"pydantic:{model.__module__}.{model.__name__}",
            validation_points=("generation", "handoff", "resume", "final_verify"),
            failure_states=("validation_failed", "tamper_detected", "execution_error"),
            resume_policy="reuse_if_valid_else_block",
        ))
    return StageContractRegistry(schema_version="skill-refiner.stage-contracts/v1", stages=tuple(records))


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def schema_file(relative: str) -> Path:
    path = (package_root() / relative).resolve()
    if not path.is_file() or not path.is_relative_to(package_root()):
        raise FileNotFoundError(relative)
    return path


def schema_hash(relative: str) -> str:
    return digest_file(schema_file(relative))


def controller_contract_hash() -> str:
    return digest_json({
        "registry": registry().model_dump(mode="json"),
        "stage_output_models": {
            stage_id: model.model_json_schema()
            for stage_id, model in sorted(STAGE_OUTPUT_MODELS.items())
        },
        "adapter_response_schemas": {
            "plan": PLAN_SCHEMA,
            "review": REVIEW_SCHEMA,
        },
        "schemas": {
            relative: schema_hash(relative)
            for relative in (SCHEMA_PATH, ATTEMPT_SCHEMA_PATH, CONTRACT_SCHEMA_PATH, STAGES_SCHEMA_PATH)
        },
    })


def validate_stage_output(stage_id: str, value: Any) -> dict[str, Any]:
    model = STAGE_OUTPUT_MODELS[stage_id]
    return model.model_validate(value).model_dump(mode="json")

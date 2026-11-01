# Skill Refiner schema bundle

Schema definitions are grouped by format version. Keep the logical filename stable and put the version in the
directory, for example `schemas/v1/analysis.schema.json` and `schemas/v2/analysis.schema.json`. A run records the
schema ID, version, package-relative path, validator ID and SHA-256 used. Runtime instances and validation evidence
live in the run directory.

```text
schemas/
├── v1/
│   ├── analysis.schema.json
│   ├── analysis-review.schema.json
│   ├── analysis-recovery.schema.json
│   ├── analysis-revision-delta.schema.json
│   ├── artifact-record.schema.json
│   ├── binary-content.schema.json
│   ├── checkpoint-migration.schema.json
│   ├── clarification-review.schema.json
│   ├── execution-plan.schema.json
│   ├── evaluation-cost.schema.json
│   ├── json-object.schema.json
│   ├── markdown.schema.json
│   ├── process-execution.schema.json
│   ├── repair.schema.json
│   ├── runtime-preflight.schema.json
│   ├── skill-stage-contracts.schema.json
│   ├── stage-artifact-contract.schema.json
│   ├── unified-diff.schema.json
│   ├── user-approval-record.schema.json
│   └── utf8-text.schema.json
├── v2/
│   ├── analysis.schema.json
│   ├── analysis-review.schema.json
│   ├── checkpoint-migration.schema.json
│   ├── plan.schema.json
│   ├── plan-review.schema.json
│   └── stages.schema.json
├── v3/
│   ├── analysis.schema.json
│   ├── plan.schema.json
│   └── plan-review.schema.json
├── v4/
│   ├── plan.schema.json
│   └── plan-review.schema.json
├── v5/
│   ├── plan.schema.json
│   └── plan-review.schema.json
├── v6/
│   └── plan.schema.json
└── README.md
```

## Schema roles

- `v2/stages.schema.json` validates each Refiner `stages.json` manifest.
- The artifact-record and content schemas validate persisted artifact indexes and byte-level formats.
- `v2/plan.schema.json` through `v5/plan.schema.json` retain the historical plan contracts needed to resume old
  Refiner runs; v4 added reusable cross-Skill `stage_contracts`, and v5 added the executable work breakdown.
- `v6/plan.schema.json` retains the v5 execution and review gates and accepts concise Chinese labels in
  scenario detail; creation from analysis v3 selects v6 for both development and acceptance.
- `v1/execution-plan.schema.json` constrains the structured execution plan.
- The plan-review schemas validate independent plan review evidence at each supported plan generation.
- v1/user-approval-record.schema.json constrains workflow-start and problem-recovery approval decisions recorded in each run’s approvals.jsonl.
- The v1 and v2 analysis schemas remain available to read legacy runs. New creation uses v3 analysis and v2
  review schemas for context, actors, scope, traceable requirements, workflow,
  risk applicability, assumptions, dependencies, blocking decisions and scenario links.
- `v1/skill-stage-contracts.schema.json` defines the reusable cross-Skill stage and artifact contract.

The registry and content validators live in `scripts/refiner/artifact_validation.py`. It now emits the versioned
directory path for new runs and accepts an old flat path only when the saved schema identity, version, validator
and hash still match. A changed schema hash still blocks resume. The package tests compare Python runtime
contracts with the corresponding JSON Schema files.

For generated Skills, the frozen stage contract declares one active version per logical Schema ID. The final
package gate requires every target `schemas/**/*.schema.json` file to be declared as runtime-needed. Refiner's
private historical schemas stay inside this controller to preserve old-run compatibility and are not copied to
generated Skills.

## 运行器执行证据

`v1/process-execution.schema.json` 约束实时进程身份、输出计数和终态记录。
`v1/runtime-preflight.schema.json` 约束声明配置来源、预算下限、场景/用例容量及失败诊断。
`v1/evaluation-cost.schema.json` 约束评测预留额度、实际消耗和恢复时的累计核算。
`v1/checkpoint-migration.schema.json` 约束输入检查点迁移的来源、文件摘要、运行配置和继承预算。
`v2/checkpoint-migration.schema.json` 另约束用户明确补充的来源、原始快照及派生输入的完整溯源；旧记录仍按 v1 校验。
`v3/checkpoint-migration.schema.json` 记录原控制器归档缺失时的受限迁移：当前控制器必须重新校验已完成的 `inputs` 阶段，其他阶段都作为未验证历史保留。
`v1/clarification-review.schema.json` 约束对每个候选澄清问题的独立分类和原始业务来源引用；不确定的问题继续阻断。
`v1/analysis-recovery.schema.json` 绑定未通过分析与独立复核的来源、配对和原始证据哈希；`v1/analysis-revision-delta.schema.json` 记录修订前后需求与场景变化，变化本身不代表审核通过。
`v2/analysis-revision-delta.schema.json` 对每个被删除或变更的需求、场景保留前后完整内容、哈希及变更路径，包括同 ID 换义；修订轮使用 `v3/analysis-review.schema.json` 逐项审查变更、原始来源摘录和覆盖映射。等价措辞可通过，未解决的覆盖缺口不可批准；映射与摘录的真实性由程序核验，业务语义是否等价由独立复核判断。旧 v1/v2 合同仍保留。
这些定义属于 Skill 包；每次调用和预检的数据保存在 run 中，不能把运行记录当作 Schema。

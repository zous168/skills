# Stage contracts and handoff artifacts

The controller runs a requirement-driven sequence of small stages. A stage receives
only the contract and source material it needs, emits a typed artifact, and hands the
next stage file references with SHA-256 identities instead of replaying the full chat
history. Tests and standards findings stay frozen while repair proceeds. If one stage
still needs more context than its configured budget, the run stops with the full
request and a diagnostic manifest; it never silently truncates requirements or tests.

## Artifact formats

Every run writes `stages.json` using the package-owned
[`skill-refiner.stages/v2` JSON Schema](../schemas/v2/stages.schema.json). The package also ships
the [artifact inventory schema](../schemas/v1/artifact-record.schema.json), per-format schemas,
and the executable validator in `scripts/refiner/artifact_validation.py`. `stages.json` records
each schema ID, version, package-relative path, validator ID and SHA-256. It is an index and audit
record; the referenced files remain the canonical stage data.

Each stage declares schema-bound input and output paths, media types, a `validation_gate`, failure
states, status, and discovered artifacts. JSON, Markdown, UTF-8 text and unified-diff outputs use
versioned content schemas; JSON plans, reviews, analyses and repair responses use their dedicated
packaged schemas and also pass their domain-specific validators. Heterogeneous glob outputs resolve
to a content schema per file and keep the declaration's artifact-inventory contract. Artifact rows
record byte count, content SHA-256 and validation result. Status is one of `pending`, `running`,
`completed`, `blocked`, or `skipped`.

On write capture, outputs are checked before a stage can complete; inputs and outputs are checked at
the completion gate and again before handoff. Resume reloads the package schemas, compares their
hashes and the complete stage contract, then revalidates every completed input/output before reuse.
Final verification repeats those checks. Missing or changed schemas, invalid content, missing files
or changed hashes block completion and reuse. A controller/schema change requires either a new run
or an explicitly supported, evidence-validated migration, with the old evidence left intact.
Optional reused plans, drafts and call transcripts may be absent in a new run, but any recorded
evidence is still validated and hash-checked.

An authenticated legacy, unfinished plan can be migrated only through the supported
`refine_plan_recovery` path: retain the original run and validated historical call/input
evidence, carry forward elapsed time, agent calls and evaluations, and record the new version
selection in `response-contract-migration.json` under
[response-contract-migration.schema.json](../schemas/v7/response-contract-migration.schema.json).
Only a proven one-to-one case-ID normalization is accepted; independent review assesses
each recorded semantic change. An unfinished legacy run without authenticated recovery
evidence blocks. A migrated v7 plan cannot currently be reused in a different run without
portable authenticated migration provenance. Frozen v2–v6 runs remain on their old versions.
Migration does not erase prior consumption or waive source, control or review gates.

For refinement runs, `run --resume-from RUN_DIRECTORY` reopens the same run directory.
Before continuing, it checks the target and frozen-input manifests, controller/backend
contract, every completed stage handoff, and cached result hashes. Completed agent
responses and evaluations are keyed by their exact request or by the target manifest,
frozen cases, executor settings and grader hashes. They are loaded without invoking
the agent or evaluator again. The active incomplete operation uses a new attempt
directory when its prior output is partial; engine `ERROR` results are retried. If a
required checkpoint is damaged or its contract changed, resume stops and asks for a
new run rather than trusting or overwriting that evidence. `--reuse-plan` remains a
new-run path and must not be combined with `--resume-from`.

Use `run --plan-only` for the visibility checkpoint before tests. It freezes and
validates both plans, writes `work-plan.md` and `test-scenarios.md`, then returns `status=planned` without
executing baseline, regression or acceptance. Read the plan and continue the same
workspace with `run --resume-from RUN_DIRECTORY`; plan calls are reused. Creation uses
the same checkpoint through `create --plan-only` and resumes with `create --resume-run`.
`work-plan.md` is the frozen, human-readable design and test checklist. Execution
state belongs in `state.json`, `stages.json` and immutable stage evidence. The CLI
emits structured JSON stage/phase/evaluation events, controller Agent stream events,
and periodic heartbeats. Each controller call is bounded by the remaining run-wide
wall-clock budget and, when configured, the smaller role-profile timeout. CLI process
timeouts terminate the process tree; OpenAI-compatible requests use a killable worker
so slow-drip HTTP responses are also bounded. Timeout/cancellation evidence is saved
and the phase becomes an error rather than a pass. Completed stages remain reusable;
resume retries only incomplete work under the validated run contract. Per-case executor
timeouts and round/evaluation limits also bound target testing. When the development
plan has passed local controls, dependency-aware wave `PW-PLAN-01` runs `AG-DEV-REVIEW`
beside `AG-ACCEPTANCE-PLAN` under `--agent-parallelism 2`. The work plan and each
`planning_waves[]` record identify task roles, prerequisites, inputs, outputs, isolated
write scopes, merge owner and join gate. Wave records retain per-task terminal status
and errors; the controller joins all started branches before deciding the plan gate.
`--agent-parallelism=1` runs the same independent tasks in sequence and records the
configured serial reason. Same-executor skill-up cases remain serialized because the
judge uses shared temporary directories. When `large-task-tracking` owns the task, its
parent snapshot/runtime is the sole user-visible progress record; it reads these internal
files and events without copying long logs.
Final verification remains bound to stage artifacts and hashes.

Creation runs use `create --resume-run RUN_DIRECTORY` to continue the same workspace.
The creation contract freezes business inputs, backends, controller code and case
limits. Completed analysis and draft stages are validated and reused. If refinement
was interrupted, its nested `refinement/` run resumes from its own checkpoint. A
different contract or changed completed artifact is rejected; higher time, call,
evaluation, planning or round budgets can be supplied when continuing.

The human-readable `work-plan.md` presents the design/requirement map, acceptance
criteria, dependency-aware Agent task graph and frozen case checklist before execution.
Each case shows its scenario,
expected outcome, linked requirement sources, machine checks and expected values,
and oracle rationale. `test-scenarios.md` contains the corresponding executable
development and acceptance scenario inventory. These files have no live statuses
and are written when the relevant plan is frozen. The parent task manager reports progress
from structured events and internal checkpoints. One full regression run per repair
round includes affected and previously passing development cases, so there is no extra
focused pass before it.


Controller calls use `skill-refiner.agent-request/v1` JSON with `role`, `instruction`,
`input`, and `response_schema`. Each call directory also contains
`request-manifest.json` (`skill-refiner.agent-request-manifest/v1`) with the request
hash, exact transmitted UTF-8 byte count and hash, configured limit, outcome, and
response hash when one was returned, plus relative paths to its `request.json`,
`transmission.txt` and (on success) `response.json`. `transmission.txt` preserves the exact bytes
sent to the backend (the JSON request for command adapters, the rendered prompt for
managed model adapters). Structured outputs are validated against the role's JSON
schema before the next stage can consume them. CLI Agent events are forwarded as
structured `agent_stream` records while the call remains active; a terminal JSON
result is saved before the next role or stage starts. When a backend does not expose
partial output, it emits elapsed-time heartbeats and returns its final response. The
controller does not infer completion from a short wait or heartbeat count. Human-readable
reports use Markdown; patches use unified diff; logs and backend-native evidence retain
their source format.

The default maximum serialized prompt is 350,000 UTF-8 bytes. A controller role's
backend profile may set `max_request_bytes` from 16,384 to 10,000,000, based on a
verified limit for that backend/model. This is a byte guard, not a token counter or a
claim about a provider's exact tokenizer. A request over the limit is saved and
rejected before transmission. Reduce the stage handoff or set a tested limit; do not
remove required inputs, tests, or evidence just to fit. Full source and execution
evidence remain in the run directory even when a role receives a bounded excerpt.

## Response contracts and evidence boundaries

New plans use the packaged [`plan/v7` Schema](../schemas/v7/plan.schema.json). Each case
declares `response_contract`: `null` for a case without an applicable JSON final-response
requirement, or `schema_json`, `source_refs` and `allow_markdown_fence`. A source reference
identifies content in the authenticated frozen inputs; its hash binds the content, not an
unverified path supplied by the model. An existing target schema is evidence to inspect and
must not override the original business requirements. Clearly text-only responses are not
forced into JSON.

The same frozen contract is applied to every positive control and actual executor result.
A contract-negative control is checked against the contract separately; each semantic negative
must first satisfy that contract and then fail its declared semantic assertion. Full controls,
source and asset checks, and independent plan review all precede target evaluation. V7 review
uses [plan-review.schema.json](../schemas/v7/plan-review.schema.json) or
[plan-review-recovery.schema.json](../schemas/v7/plan-review-recovery.schema.json) to map
each issue to affected cases, requirements or scenarios. Each newly generated revision attempt persists
[plan-revision-context.schema.json](../schemas/v7/plan-revision-context.schema.json) data:
its baseline hash, complete feedback, sources, allowed case IDs and before/after differences.
The plan gate and final verifier recompute that history; unresolved scope blocks the gate.
The review validators currently use the matching in-memory definitions in `scripts/refiner/schemas.py`;
the two packaged review Schema files mirror those definitions. The review and revision-context
Schema files are not yet entries in the stage Schema registry, so do not claim those three files
are individually hash-pinned by `stages.json`. The revision-context Schema is read for validation.

When JSON applicability is unresolved, the controller uses two independent blind role
contexts under a shared, atomic call-budget reservation. Their per-case assessments cite
verbatim frozen source content and use
[response-applicability-analysis.schema.json](../schemas/v7/response-applicability-analysis.schema.json);
the recorded decision is checked with
[response-applicability-decision.schema.json](../schemas/v7/response-applicability-decision.schema.json)
and bound to input, plan, schema and call evidence. Review disagreement, uncertain scope or
conflicting response requirements cannot open the gate. These analyses share the configured
backend and do not prove interpretation correctness. Applicability detection is conservative,
not a complete natural-language interpreter. The current response validator rejects external
or recursive references and schema keywords `pattern`, `patternProperties` and `format`;
unsupported contracts fail closed.

Tool traces must cover every relevant entry point before an absence assertion can pass. The
current executor cannot independently establish complete absence coverage and therefore blocks
`tool_command_absent` and `tool_call_absent` cases. An incomplete transcript is not evidence
of no external call or no side effect.

## Human-readable phase plan and progress

The generated work-plan.md is the user-facing execution map, not only a list of test
cases. It must contain a numbered ordered phase sequence. Each step states its goal,
actions, required inputs, outputs/evidence, conclusion required to advance, and the
failure or stop action. Creation plans include business analysis and first-draft gates
before the shared refinement sequence; refinement plans begin with source and
requirement mapping. Both then show plan freezing, plan self-check and independent
review, the plan gate, baseline, bounded repair with full regression, independent
acceptance, and final verification/delivery.

New creation business analysis uses the packaged `analysis/v4` contract. It makes the intent and usage
context, users/actors, in-scope and out-of-scope behavior, constraints, source-linked atomic
requirements, ordered workflow, complete risk applicability, assumptions, dependencies and
blocking decisions explicit. Every workflow step has an observable completion check beside
its actors, preconditions, inputs, outputs, failure behavior and requirement/dependency links.
The independent analysis review assesses every requirement, workflow step, scenario and risk
dimension. A missing or inconsistent element blocks draft generation.

New creation and independent refinement use `plan/v7` and `execution-plan/v1`; the selected
version is frozen in the inputs. Previously frozen `plan/v2`–`plan/v6` plans keep their version
on resume or reuse. Target standard O obligations remain separately sourced and hash-bound in
analysis/v4, join the effective R+O test contract, and require actual per-executor judge evidence
at final verification. The structured execution plan is the source for its human-readable work-plan section; each step states its goal, actions,
inputs, schema-bound outputs/evidence, linked requirements and cases, dependencies, serial or
parallel schedule, Agent write scope, merge owner, join gate, exit evidence and bounded failure/
recovery policy. Structural validation checks complete traceability, safe unique outputs, an
acyclic ordered dependency graph, concurrency limits and nonoverlapping parallel write scopes.
The held-out acceptance plan must retain the same execution plan and independently assess every
step before the plan gate can pass.

Target Skills also include the [bounded self-review protocol](self-review-protocol.md). Planning freezes each
stage's self-check IDs against requirements and cases; normal and missing-evidence controls verify both pass and
stop behavior. One targeted correction is allowed before a still-failing check becomes blocked.

Show the executable controller Agent DAG with stable task IDs, roles, prerequisites, inputs,
output artifacts, write scopes, concurrency limit, merge owner and join gate. For any host-
subagent tasks, include their own `done_when` and evidence path. Label each task as planned,
running or terminal in persisted state; explain why dependent or shared-scope tasks remain
serial. Never describe a candidate graph as a running task or treat one completed branch as
a passed wave.

Presenting the plan is a visibility checkpoint within an approved workflow. Follow
[user confirmation gates](user-confirmation.md): execute planned stages and cases autonomously,
including bounded repair/regression for normal case failures. Diagnose and repair recoverable
operational errors and failed gates within the authorized scope and cumulative budget; rerun
the affected full controls and resume validated work. Ask for an unresolved material user
decision, unavailable authority or an exhausted approved hard budget. Do not reset the budget,
repeatedly run unchanged tests, or abandon a resumable stage. Resume the same run and reuse
completed evidence while hashes and contracts match; otherwise preserve old evidence and use a
new run or an explicitly supported, evidence-validated migration.

At each meaningful transition, report the step number/name, live status, concise
conclusion, evidence path, and next step. Populate these from structured events and
state.json/stages.json. The static plan states expected conclusions; it must not be
presented as evidence that execution already passed.

When planning, self-check or independent review ends in an error or cancellation, close
any pending/running plan gate as not_passed and persist the cause. Keep target
skill_evaluation at not_started. A terminal planning error must never leave the gate
displayed as running.

## Existing creation and refinement stages

| Stage | Required input artifacts | Output artifacts and formats | Gate before handoff |
|---|---|---|---|
| Creation: inputs | User description, supplied requirements, auto-discovered project standards, and optional additional standards | `inputs.json` (JSON), `inputs/*.md` (UTF-8 Markdown/text) | Inputs frozen and hashed in the business contract |
| Creation: analysis | Frozen business inputs | `analysis.json`, `analysis-review.json` (JSON), `business-analysis.md` (Markdown), `business.lock.json` (JSON) | `analysis/v4` validates context, users/actors, scope, constraints, sourced atomic requirements, ordered workflow, full risk applicability, assumptions, dependencies and blocking decisions; review assesses every workflow/risk row; unresolved decisions pause creation |
| Creation: first draft | Approved analysis and business lock | `drafts/` (Skill source files), `draft-manifest.json` (path-to-SHA-256 JSON) | Draft paths and static authoring checks pass |
| Refinement: planning | Immutable Skill snapshot, requirements, auto-discovered project standards, additional standards, analysis contract and the actual plan schema | `test-plan.json`, `acceptance-plan.json`, `plan-controls/{development,acceptance}.json`, `plan-reviews/{development,acceptance}.json`, `oracle.lock.json` (JSON) | `plan/v7` validates atomic criteria, detailed scenario/control matrix, stage contracts and an execution plan with ordered steps, evidence-bound outputs, requirement/case links, dependency DAG, bounded concurrency, Agent scopes, exit criteria and failure/recovery rules; development and acceptance preserve the same work plan; full controls, asset/source audits and independent reviews must pass before the plan gate |
| Refinement: baseline | Locked development plan and original Skill snapshot | `baseline/summary.json`, `baseline/standards.json` (JSON), backend logs/evidence | All configured engines return observed results; environment errors stop repair |
| Repair | Failed-case results (including immutable per-attempt acceptance summaries after held-out failure), material standards findings, frozen requirements, prioritized source excerpts | `iterations/round-N/changes.json` (JSON), candidate Skill files, call request/response manifests | Each changed file cites a failed case or finding; protected tests and oracles cannot be edited |
| Regression | Candidate, same frozen development suite, original passing-case set | `iterations/round-N/regression/summary.json`, `standards.json`, engine comparisons (JSON/Markdown) | No previously passing case regresses; each engine is judged separately |
| Acceptance | Qualified candidate and separately frozen acceptance plan | `acceptance/target.json` (candidate path and hashes), `acceptance/summary.json`, immutable `acceptance/attempts/<attempt>/evaluation/summary.json` records and raw engine evidence | Every configured executor passes the independent cases against the delivered candidate |
| Delivery | Passing regression, acceptance, locked plans and source manifests | `final/<skill>/`, `final/evidence.json` (JSON), `final/changes.patch` (unified diff) | Candidate matches the acceptance target and evidence |
| Verification | Final evidence (plus the accepted child run for creation) | `verification.json` (JSON), final `report.md` (Markdown) | Hashes, frozen plans, all engines, stage contracts and final artifacts satisfy the completion gate |

The creation pipeline invokes the same refinement stages for the first draft, so a
new Skill receives the same executable planning, baseline, repair, regression and
independent acceptance gates as an existing Skill.

The frozen development plan carries `stage_contracts` using
`skill-refiner.skill-stage-contracts/v1`. It maps the target Skill's persisted/handoff artifacts to
its own schema IDs, one active version per ID, files and validator implementations. A schema version changes
only when its persisted format changes incompatibly. Historical controller formats are compatibility inputs to
the Refiner and must not be copied into a target Skill. Independent acceptance must reuse
the same frozen stage/schema map. The delivered candidate must contain every schema and validator
marked as runtime-required; final verification parses those files as JSON Schemas and verifies their
IDs and versions, and rejects undeclared or historical schema files left in its `schemas/` directory.
Refiner's private compatibility registry remains inside the controller so old runs can resume, but is not
packaged into other Skills. The authoring policy applies to every generated or refined Skill and to Refiner's
own stages; see [schema-backed Skill artifact standard](skill-stage-artifact-standard.md).

## Context and extension rules

Add a new stage only when it has a distinct responsibility or independent gate.
Define its stable ID, accepted input artifact types, output schema/media type,
validation rule, failure states, and provenance fields before adding the backend call.
Keep machine-readable contracts in JSON so validators and new engines can consume
them; keep Markdown as a generated view for people. Preserve raw evidence separately
from summaries so future graders can be added without rerunning old stages.

Do not pass prior conversation transcripts as implicit state. Pass a concise current
contract, source artifact references with hashes, and the minimum relevant excerpts.
For repair, the frozen requirements and cited findings are the governing contract;
large requirement/standard documents are recorded by source, size and hash rather
than copied wholesale into every repair request. The bounded source inventory
prioritizes `SKILL.md`, cited findings and local Markdown references. If an omitted
file is necessary, split the work into a smaller explicit stage or raise the budget
only after checking the configured engine's context limits. Omit acceptance cases
from repair until a qualified candidate fails acceptance; then expose only the failed
cases and their raw evidence, and rerun the complete suite after regression.

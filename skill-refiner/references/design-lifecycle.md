# General Skill design and interaction lifecycle

Use this lifecycle for both a new Skill and a substantial change to an existing one.
The purpose is a Skill that can carry out the requested work reliably, not a polished
`SKILL.md` that merely describes an imagined workflow.

## 1. Understand the request

Read the user's request and any supplied examples, existing Skill files, standards,
and available tools. For a new Skill, identify its intended user, goal, invocation
triggers, expected inputs and outputs, and the boundary of its responsibility. For an
existing Skill, inspect its entrypoint, linked references, scripts, configuration,
and real execution traces before proposing changes.

Separate confirmed requirements, evidence, reasonable low-impact assumptions, and
unknowns. Trace each material behavior to the user's request or a supplied source.
Do not turn implementation preferences or authoring standards into business rules.
Load project conventions automatically from `PROJECT-STANDARDS.md` and
`.skill-refiner/standards/*.md` in the current project and target's project ancestry.
Freeze the discovered file paths and contents into the run inputs; use `--standard`
only for additional standards.

## 2. Map the workflow and risks

Describe the work in its actual order: inputs, decisions, transformations, outputs,
side effects, permissions, external services, configuration, and failure behavior.
Identify normal, boundary, and error situations. Include missing or invalid inputs,
partial results, unavailable dependencies, denied permissions, retries, and recovery
when those situations apply to the requested work.

Check which parts need instructions, references, deterministic scripts, or both.
Scripts should perform repeatable work that benefits from exact behavior; instructions
should explain judgment, policy, tool selection, and decisions that cannot be reduced
to a deterministic operation. Keep integrations and permissions explicit.

## 3. Ask only for decisions that block correctness or safe execution

Analyze the available evidence first. Do not make the user diagnose a failure that can
be explained from logs, exit codes, files, configuration, or a reproducible check.
Ask when a missing business rule, target, permission, provider choice, model endpoint,
or local configuration would materially change the result and cannot be inferred safely.
Do not interrupt for low-impact details that can be handled as a clearly recorded
assumption.

Before asking, state what is known, what remains unknown, why it changes the workflow
or result, and the smallest safe action or decision needed. Group related questions
into one concise request. Offer a recommended default where appropriate, and explain
the consequence of choosing it. Ask for the model/provider name, endpoint, or config
location when needed; never ask the user to paste passwords, API keys, tokens, or other
secrets into chat. Give safe local setup instructions for credentials instead.

Use the host's native structured question/input mechanism when available; otherwise
ask clearly in conversation and accept a free-form answer. Do not assume a specific
tool name or imply that a Skill can force a host to expose a question API.

When the user answers, preserve the answer in the requirements or local run record,
update the affected workflow and acceptance criteria, regenerate any affected frozen
test plan, and execute fresh verification. Changed business requirements need a new
creation run; do not reuse approval or verdicts for a changed contract. If the answer
is unavailable, leave the work explicitly waiting for that decision, preserve the
diagnosis and evidence, and do not publish or report an unverified result as complete.

## 4. Design the Skill

Write a concise entrypoint with a precise name, useful trigger description, and clear
instructions for the common path. Put substantial conditional detail in linked
references. State the tool and configuration prerequisites, invocation contract,
expected outputs, failure handling, and recovery path. For a Skill that may need user
decisions or local setup, include the clarification and safe-configuration behavior
from section 3. Ask only when blocked; do not add a mandatory question ritual to a
deterministic task.

Keep the user's public contract stable. Avoid placeholder integrations, fabricated
success, unrequested side effects, unnecessary dependencies, and instructions that
claim support for a host feature that has not been verified.

Divide work into stages when a single context would accumulate unrelated sources,
large fixtures or repeated history. Before implementation, define each stage's stable
ID, purpose, required inputs, output artifacts and media/JSON schema, validation gate,
failure state, and provenance. Pass content only when the next stage needs it; otherwise
pass a file reference with a hash and a concise summary. Preserve complete artifacts
on disk. See [stage contracts and handoff artifacts](stage-contracts.md) for the
controller's concrete versioned contracts and context budget.

## 5. Plan independent tests before repair

The plan makes the work visible. Follow [user-confirmation.md](user-confirmation.md):
confirm once before each complete create/run workflow, then execute its planned stages and
cases autonomously. Ask again for execution problems or material scope changes.

Make the plan a numbered sequence. For each step show its goal, actions, inputs,
outputs/evidence, observable exit conclusion, and the stop or repair action when that
conclusion is not met. Report each meaningful transition with the step, live status,
conclusion, evidence path, and next step. The plan describes expected gates; execution
status must come from persisted phase state and structured events.

Map every required behavior to at least one executable check. Plan concrete normal,
boundary, and error cases where applicable, including configuration and help-request
paths when they affect behavior. Define observable inputs, expected outputs, and
independent checks. Split each material requirement into atomic acceptance criteria:
one observable outcome per criterion, a stable ID, a source link, and the evidence that
would prove it. Do not let one broad requirement row hide separate decisions, permissions,
outputs or side effects.

Build a scenario inventory before freezing executable cases. Give each scenario a stable
ID, linked requirement and criterion IDs, preconditions, concrete stimulus, ordered
actions, observable expected outcomes, forbidden outcomes, relevant risk dimensions and
a source-based rationale. Consider distinct inputs, actors and permissions, workflow
states, dependency failures, cross-requirement interactions, side effects, retries,
idempotency, interruption and recovery when they apply. Use separate scenarios when the
correct result or evidence changes; paraphrases and cosmetic fixture changes do not add
coverage. Assess normal, boundary and error applicability, and explain exclusions from
the scope. Map every applicable scenario to a distinct executable case in development
and in the fresh acceptance suite. If configured case or evaluation limits cannot fit
the justified matrix, report the exact uncovered criteria/scenarios and keep the plan
gate closed; do not compress them into vague catch-all cases.

Before execution, also turn the work itself into a dependency-aware Agent task graph. Give each task a stable ID, role, requirement references, prerequisites, input hashes, exclusive read/write scope, output/evidence path, completion check, merge owner and join gate. A task can enter a parallel wave only after its prerequisites pass and its write scope is disjoint from every other running task.

Good candidates include read-only source audits over separate file groups, scenario drafting over non-overlapping requirement clusters after the requirement contract is frozen, and independent plan review beside acceptance-plan generation. Keep shared contract decisions, cross-requirement interaction coverage, shared-file edits, repair/regression dependencies and unisolated executors serial. Limit concurrency to configured host/controller capacity; record every started branch terminal status and merge only after all required branches finish. If the host cannot dispatch subagents, state that capability limit and keep the documented controller Agent wave as the only claimed parallel execution.

For each executable case, include its scenario ID, concrete setup, inputs, steps,
expected observable results, forbidden outcomes, fixtures/assets, and independent
assertions. A reviewer checks both the trace links and whether the scenario actually
distinguishes the behavior it claims to test. Keep the source requirements and test plan frozen before repair.
Before running the target, render and show a concise work plan with the design outline,
stable requirement IDs, acceptance criteria, case IDs/types, what each case checks,
and the total time/round/evaluation limits. Save the complete plan and checklist in the
run workspace. Show progress as the phase and case results change; do not leave users
to infer status from a long-running process.
Do not show acceptance cases to the implementation author before the candidate passes
development; if the candidate fails final acceptance, expose only failed cases and
their evidence to repair, then rerun the complete frozen acceptance suite.

## 6. Run, diagnose, repair, and regress

Execute the same frozen cases against the baseline and candidate with each configured
engine. Preserve raw outputs, exit statuses, logs, and per-case judgments. Diagnose
failures from that evidence and classify them as a Skill defect, a test/contract gap,
or an environment/configuration problem. Repair only from cited failures or standards
findings. Ask the user only when an evidence-based safe next step depends on their
choice or local access.

Each model call receives a stage-specific request envelope and structured response
schema. Keep the serialized handoff within the configured byte budget; if it does not
fit, the controller saves and rejects the request with evidence instead of truncating
it. Split oversized work along requirement or component boundaries while preserving
the original frozen test contract and keeping acceptance inputs out of pre-acceptance
repair. After a failed acceptance run, only its failed cases become repair evidence.

After each repair, run one complete regression suite; it includes affected and
previously passing cases, so do not immediately repeat those cases in a separate
focused suite. Then run fresh independent acceptance and compare engines on the same
case set. If acceptance fails, route its failing cases into another evidence-bound
repair and repeat regression and the complete acceptance suite. An unavailable engine
or failed execution is an error, not a pass. Enforce the configured hard caps on rounds,
wall-clock time, calls, evaluations and cases. Never extend a budget or start another
test pass automatically after it is exhausted. Preserve evidence and report exactly
what remains unverified.

## 7. Deliver with evidence

Report the Skill location, behavior changed, test and engine results, relevant
configuration, consumed limits, limitations, and any remaining user decision. Keep
the original, candidate, diff, visible work plan, live progress, raw run evidence, and
final gate result available for review.
Only claim completion when the required verification gate passes.

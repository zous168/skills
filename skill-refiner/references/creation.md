# Business request to verified Skill

`create` accepts a natural-language description, repeatable independent `--spec`
files, optional `--standard` files and optional `--name`. A name is otherwise chosen
during business analysis. From a source checkout:

```sh
.venv/bin/skill-refiner create 'Create a local business workflow with these rules ...' \
  --config /absolute/path/to/backends.json --output /absolute/path/to/new-workspace --plan-only
.venv/bin/skill-refiner create --resume-run /absolute/path/to/new-workspace \
  --config /absolute/path/to/backends.json
```

In an installed Skill, use `scripts/launch.py create` to select its saved runtime
and matrix. Save prose containing shell syntax as a UTF-8 file and pass `--spec`
instead of interpolating it into executable shell text.

## Actual stages

1. Save original business inputs and standards. Analyze inputs, outputs, workflow
   order, decisions, failure behavior, requirements, dependencies and assumptions.
   Assess normal, boundary and error scenarios and map required behavior to them.
2. Independently review grounding against the original request. Reject invented
   material rules, omissions and unjustified category exclusions. Allow one bounded
   analysis revision. Material unanswered real-business decisions stop with questions;
   handle reversible gaps in explicitly synthetic examples as documented assumptions.
3. Freeze the approved analysis and original inputs by hash. Create actual Skill
   instructions, references and working implementations. Reject unsafe/protected
   paths. Static errors allow one bounded regeneration. No author sees held-out tests.
4. Freeze and display `work-plan.md` before target execution. `create --plan-only`
stops after the first draft and reviewed development/acceptance plans are ready;
inspect `business-analysis.md`, `work-plan.md` and `test-scenarios.md`, then continue
the same workspace with `create --resume-run RUN_DIRECTORY`. The parent
`large-task-tracking` tracks visible progress from this run’s state and events. Follow
[user confirmation gates](user-confirmation.md): confirm once before the complete create workflow
unless it is already explicitly authorized.
Plan-only and its same-run continuation share that approval; stages and cases then run
autonomously within the approved plan and budget. Diagnose recoverable problems and ask only
when a material user decision, unavailable authorization or unapproved change requires it.

5. Reuse the real refinement pipeline: independent test planning/review, original
   baseline, standards review, failure-driven repair, one full regression suite per
   repair round, and independent acceptance. Each full regression includes every
   failed and previously passing development case. Both frozen suites must preserve
   approved requirement IDs/properties and cover every applicable normal/boundary/error
   category.
6. Publish only after all configured executors pass, optionally to a new
   `--destination`. Preserve drafts, rejected attempts, original inputs and results.

Creation and refinement share the same Schema contract and final package gate. Every
versioned schema path must use `schemas/vN/<logical-name>.schema.json`, where `N` matches
the declared `version`. A plan with a mismatched version directory or a delivered Skill
with undeclared/extra schema files cannot pass acceptance or be published.

New Skills use Simplified Chinese for user-facing instructions and human-readable package
text by default, including `SKILL.md`, references, examples and script messages. Preserve
machine identifiers, protocol literals, source quotations and the workflow's required
business-output language. Honor a user-specified language when it differs. Refining an
existing Skill preserves its established language unless translation is requested.

Created Skills also include the [bounded self-review protocol](self-review-protocol.md):
freeze requirement-linked checks, cite evidence, permit one targeted correction, and stop
with a concrete blocker if the check still fails. Creation acceptance verifies both the
passing path and the missing-evidence/failure path.

`agents.planning` handles analysis and tests, `agents.review` handles business/test/
standards reviews, `agents.creation` writes the first implementation (falling back
to `agents.repair`, then `agents.default`), and `agents.repair` fixes observed failures.
Every role must resolve to a configured backend; the controller does not choose a
model vendor implicitly.
They are independently configurable. Role-call and wall-clock budgets cover the
entire creation plus refinement workflow; no budget resets between stages.
Create defaults allow two repair rounds, four plan/review attempts per suite, eight
development cases, six acceptance cases, 30 minutes and 80 case-executor evaluations.
Hard caps are three repair rounds, four plan/review attempts, 30 cases per suite, 60
minutes, 128 agent calls and 200 case-executor evaluations. Budgets cover the whole
creation, including nested refinement. `--planning-attempts`, `--max-rounds`,
`--max-seconds`, `--max-agent-calls` and `--max-evaluations` can lower or raise defaults
within those caps. Revisions receive the prior plan and accumulated concrete review
issues; rejected versions are preserved.

## Clarification and help requests

The command-line pipeline records material business unknowns as
`state.json.questions`, emits `needs_clarification`, and saves them in
`business-analysis.md` and `report.md`. It does not itself open a chat prompt. The
Skill host/orchestrator first applies [user confirmation gates](user-confirmation.md).
For an explicitly synthetic test or demo, select reasonable reversible example assumptions,
record their source and rationale separately, and validate them as part of the frozen test
contract; do not present them as user decisions. For consequential real-business unknowns
or missing authorization, relay the fewest answerable questions through the host's native
structured input facility when available, or ordinary conversation otherwise. Explain why
each answer changes behavior and offer a recommended default when safe.

Before asking for operational help, inspect `doctor` output, the run status, exit
code, stderr/stdout, and relevant backend/configuration files. Classify the blocker
as a business decision, missing local setup, permission, unavailable integration,
or an implementation/test failure. Tell the user exactly which local action or
choice unblocks it. Ask for endpoint/provider/model names or a config file path only
when needed. Never request credentials in chat; direct the user to configure them
locally using the host's secret store or environment.

After receiving an answer, or recording a permitted synthetic-example assumption,
preserve it in a separately identified UTF-8 requirements file. Keep the original
inputs unchanged. If the run qualifies for `import-create-inputs --supplement`, use
its versioned migration to create and validate derived inputs while inheriting the
consumed budget; otherwise start a new creation with both sources and explicitly
account for prior consumption. Do not pass changed business inputs to `--resume-from`;
resume is only for an unchanged approved contract and validated draft. Regenerate
affected development and acceptance plans and execute fresh verification. If the
user cannot answer or perform the local action, leave the run incomplete, preserve
the diagnostics, and report what remains blocked. Existing Skills follow the same
evidence-first and question policy before and during `run`; configuration changes
belong in a per-run config passed with `--config`, not a silent global-default edit.

## Outputs and stops

- `analysis.json`, `business-analysis.md`, `business.lock.json`: business contract,
  workflow, scenario intents and input hash. Questions are saved before approval.
- `work-plan.md`: design outline, requirement-to-case mapping, acceptance criteria
  and development/acceptance test checklist, written before target tests run.
- `state.json`, `stages.json`: internal stage status, resumable checkpoints and
  artifact hashes. Structured console events include stage transitions and bounded
evaluation heartbeats for the parent task manager.

- `drafts/` and `draft-manifest.json`: preserved first implementations and findings.
- `refinement/`: frozen suites, per-engine results, repairs, traces and evidence.
  Use this subdirectory as a future `compare --suite` source.
- `test-scenarios.md`: development/acceptance scenarios and expected outcomes,
  actual prompts, input fixtures, linked requirement sources, every independent
  check and expected value, and oracle rationales.
- `final/<name>/`, `final/creation.patch`, `final/changes.patch`, `final/evidence.json`:
  accepted Skill, complete creation diff, draft-to-final repair diff and evidence.
  The repair diff can be empty if the first implementation already passes.

Exit 0 with `created` means the full workflow passed. `refinement_status` accurately
records `passed` or `unchanged`. Exit 2 means clarification, rejection, coverage gap,
execution error, exhausted budget or nonconvergence. Incomplete runs retain drafts
but do not publish a final Skill or populate the destination. Never relabel them.
Exit 0 with `planned` means `--plan-only` prepared the reviewed plan but did not run
baseline, regression or acceptance. Resume it with `create --resume-run` to execute.

`create --resume-from PREVIOUS_CREATION --output NEW_WORKSPACE` validates and reuses
its frozen approved business contract and original validated draft. If complete
frozen suites exist, their source/requirement hashes are checked before reuse.
Every baseline and candidate verdict is executed again. Source changes, changed
business inputs, missing approvals or tampered artifacts are rejected. Reports
record original artifact provenance and, when replaying after a controller upgrade,
the original checker hash alongside the new run's checker hash. Old verdicts and
locks remain untouched. A resumed invocation has its own explicit budget.

If a controller upgrade prevents `--resume-run` and only the old `inputs` stage
completed, `import-create-inputs` is an offline, explicit migration into a new,
isolated workspace. Pass `--source-run`, `--controller-archive`, `--output`,
the new `--config` and full execution options, and `--prior-seconds`,
`--prior-agent-calls`, `--prior-evaluations`. Prior calls also require
`--process-reconciliation` with a source-specific process check, historical
termination evidence and its limitations. With an exact source archive, the command
validates the old controller and schemas, copies verified inputs, records provenance
and preserves prior consumption. It never marks analysis or later stages complete.
When that exact archive is unavailable, omit `--controller-archive` and explicitly
pass `--revalidate-source-checkpoint`; the current controller then revalidates the
completed inputs stage and its file/schema hashes. This fallback records a v3
migration attestation, treats every other stage as untrusted, and never reuses it as
completed work. It currently supports only zero prior target evaluations and no
original destination.
Resume with `create --resume-run NEW_WORKSPACE` using the same backend, options
and limits that were frozen during import; a different budget is rejected.
The external process check can have gaps, such as unknown historical child PIDs;
retain and report those limits instead of claiming stronger proof.

When the unchanged input checkpoint has independently rejected analyses,
`--carry-analysis-feedback` verifies and copies their original call evidence,
including each request, response and analysis-to-review relationship. The new
run keeps analysis incomplete, inherits all consumption and supplies the latest
analysis plus complete prior reviews to the next revision. Each attempt and review
has a new evidence path. Schema, semantic and independent-review gates still apply;
the controller records a schema-validated revision delta and requires an independent
explanation before approving coverage reductions. An identical previously rejected
analysis stops as no progress. This option cannot be combined with `--supplement`:
changed inputs require fresh analysis. A completed analysis without its independent
review currently requires reconciliation rather than silently rerunning it.

When the user has explicitly supplied a new business decision, append it during
offline import with `--supplement PATH --supplement-source ORIGIN`. The file
must contain the user's actual decision; never manufacture one or use a test
fixture as a business answer. The v2 migration record binds the old input hash,
the original input snapshot and artifact inventory, the supplement's original
path and bytes, its decision source, and the newly derived input hash and full
inventory. Existing requirements stay unchanged and in order. The derived inputs
are a new input stage, not an assertion that the old checkpoint is unchanged;
analysis and all downstream stages execute against the supplemented inputs.
Neither this nor the v1 import supports a nonzero historical evaluation count;
stop for explicit reconciliation instead of discarding that consumption.

Category coverage is not exhaustive scenario coverage. Current executable checks
cover deterministic text/JSON, files and command traces. External services, visual
output and long-running stateful systems need real accessible task material or
additional judging support. An assumption does not verify a service, and fake
provider tests do not establish a real integration pass.

For Python scripts, the planner can request the controller-supplied frozen
`python_probe` helper. It actually executes the installed script and exports
stdout, business exit status, tracked input hashes and Python audit events.
Optional flags record and block tracked-input writes, sockets and subprocesses.
`file_json_path_equal` grades these evidence fields; `file_text_equal` verifies
exact UTF-8 content. This is Python-level observation, not OS isolation or a
guarantee about arbitrary native code. The observer's transport exit code is not
the business script's exit code. Its code hash is frozen with the suite.
For batches, `json_sequence_equal` compares independent expected JSON values with
the actual output stream while ignoring member order and whitespace. Separate
stdout checks still require forwarding actual complete outputs unchanged.

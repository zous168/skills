---
name: skill-refiner
description: Create or refine Agent Skills through a Google ADK 2.x graph harness and a deterministic, evidence-producing control kernel. Use when a skill must be independently runnable, schema-bound, budgeted, tested with held-out acceptance cases, resumable, and explicitly verified before adoption.
---

# Skill Refiner

Use the CLI as the only mutation entry point. The Google ADK `Workflow` orders
the fixed graph. The Control Kernel, not an LLM response, owns paths, budgets,
state, evidence, test execution, and the final verdict.

## Workflow

On this machine, read `references/local-runtime.json` and use its absolute
`python` path with `scripts/refiner.py`. The launcher imports the bundled
`src/` tree, so the installed Skill does not depend on an editable package
path. Run `doctor` before starting a workflow.

1. Write requirements to a UTF-8 Markdown file.
2. Copy `examples/config.adk.json` and replace the fake role profiles with
   command adapters that emit JSON matching `schemas/`.
3. Run `python scripts/refiner.py doctor --config <config>`.
4. Run `create` for a new skill or `refine --source <skill-dir>` for an
   existing skill. Use `--plan-only` to freeze and review the plan without
   generating files.
5. Inspect `state.json`, `events.jsonl`, `plan.json`, `report.md`, and the
   content-addressed artifacts in the run directory.
6. Run `verify <run-dir>`. Only then run `adopt <run-dir> <empty-destination>`.

Never edit frozen inputs, plan evidence, or test evidence inside a run. Resume
with `resume <run-dir>` after repairing an external adapter or environment.

See [references/architecture.md](references/architecture.md) for trust
boundaries and [references/adapters.md](references/adapters.md) for the
adapter envelope. See [references/persistence.md](references/persistence.md)
for the versioned per-stage persistence and recovery contract. For an
independent external model backend, see
[references/deepseek-adapter.md](references/deepseek-adapter.md).
For the optional current-host Python SDK backend, see
[references/adapters.md](references/adapters.md) and
`examples/config.cursor-sdk.json`; do not claim live availability unless its
doctor and a real inference have succeeded on the current platform.

# skill-refiner

Create a business Skill from natural-language requirements or improve an existing
Skill using real **Alibaba skill-up** runs.
This project includes an installable Skill and a working Python CLI. Planning,
test review, authoring review and repairs use independently configured backends;
skill-up executes target Skills and scores their actual results. Version 0.2 adds
Codex, Cursor Agent, native Codex subagents, OpenAI-compatible custom models and
multi-engine regression. Intended for macOS and Linux.

Version 0.3 adds business analysis, independent contract review, complete first
implementation and explicit normal/boundary/error scenario coverage, followed by
the same real refinement and multi-engine acceptance pipeline.

The public repository includes source, tests, examples and a
[portable verification summary](verification/README.md). Complete local run
archives under `.refiner-runs/`, downloaded tools and virtual environments are
excluded from Git. Evidence links into `.refiner-runs/` in the detailed historical
reports resolve only in the original verification workspace.

The separate [CSP Media project](csp-media-skill/README.md) keeps its Skill source,
project-level fake-gateway tests and reproducible build together. Its installable
Skill artifact is `csp-media-skill/dist/csp-media/`.

The historical 0.2 integrated real self-audit ended `unchanged` with complete Codex/native-child workflows. It includes a native Agent-generated repair and explicit engineering integration, with frozen suites and raw evidence. See [SELF-REFINEMENT.md](SELF-REFINEMENT.md) for results, earlier failed attempts and limits.

## Install

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/skill-refiner bootstrap
.venv/bin/skill-refiner doctor
```

Bootstrap downloads the pinned skill-up v0.13.0 release, verifies the archive against
upstream checksums and installs it under `.tools/`. It does not change global tools.
This default CLI profile uses Codex and therefore needs an authenticated Codex CLI.
For other agents and models, supply an explicit backend config as described below.

### Install into another agent

`skill-refiner/SKILL.md` follows the Agent Skills folder format, so the host agent
does not have to be Codex. Install it into the skill directory your agent reads;
for example, Cursor supports `.cursor/skills` and `.agents/skills`, Claude Code
supports `.claude/skills`, and Codex supports `~/.codex/skills`.

See the
[Cursor skill locations](https://cursor.com/help/customization/skills),
[Claude Code skill locations](https://code.claude.com/docs/en/skills), or
[Codex skill locations](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex)
for the host-specific paths and discovery behavior.

For the normal Skill-only installation, use the [multi-agent skills installer](https://github.com/vercel-labs/skills):

```sh
npx skills add zous168/skill-refiner --skill skill-refiner --global --agent cursor
```

Replace `cursor` with your agent name, such as `claude-code` or `codex`. This is
the usual one-command Skill install; it copies the Skill into that agent's global
skills directory. Omit `--global` to install it in the current project instead.
Then ask the agent in natural language to create a Skill or improve an existing one.

### Optional: provision the complete runtime

The Skill-only install above is enough to load its instructions. To run its full
automated planning, repair and regression pipeline, also provision Python,
skill-up and your chosen model backends once. The generic installer places the
Skill and its runtime together; choose the destination for your host and provide
an explicit backend config. It does not install or assume Codex:

```sh
git clone https://github.com/zous168/skill-refiner.git
cd skill-refiner
python3 scripts/install_skill.py \
  --skill-dir "$HOME/.cursor/skills/skill-refiner" \
  --config /absolute/path/to/backends.json
```

Use your host's path in `--skill-dir`, such as `~/.cursor/skills/skill-refiner`,
`~/.claude/skills/skill-refiner`, or `${CODEX_HOME:-$HOME/.codex}/skills/skill-refiner`.
For project-level sharing, use a project skills directory such as
`/path/to/project/.cursor/skills/skill-refiner`. The installer provisions an
isolated Python environment, checksum-verifies and pins skill-up v0.13.0, and
writes local runtime settings. It refuses to overwrite an existing Skill or
runtime, so choose this setup instead of the Skill-only command if you want the
full runtime provisioned in one go. Configure model and CLI backends first;
available choices and examples are in [backend configuration](skill-refiner/references/backends.md).

## Natural-language use in Codex

The local installation lives at `~/.agents/skills/skill-refiner`, with a compatibility
link under `~/.codex/skills/`. Its Python dependencies, pinned skill-up binary and
default backend configuration live separately under `~/.codex/skill-refiner-runtime`.
It runs from other projects without this source checkout. Automatic skill selection
is enabled. For example, tell Codex:

> 帮我优化 `/absolute/path/to/my-skill`，要求是……。自主规划测试、按规范修复，
> 使用 Codex 和原生子 Agent 做多引擎回归，保留修改前后对比与原始证据。

For a new business workflow, describe it directly:

> 帮我创建一个订单备货业务流 Skill：……业务规则……。自主分析流程、编写实现，
> 规划正常、边界、异常测试，修复优化并做多引擎回归，保留证据。

Prose requirements are saved as run inputs; project standards are auto-discovered
from `PROJECT-STANDARDS.md` and `.skill-refiner/standards/*.md` in the project and
target paths. Use `--standard` only for additional standards. Requirements and
discovered standards are frozen into each run. The installed launcher defaults to Codex planning/review,
native-child repair and both execution modes for regression/acceptance. Defaults
are bounded to 3 repair rounds, 24 role calls, 100 evaluations and 3600 seconds;
explicit user budgets override them. The original Skill is preserved, with a
separate candidate and patch returned for adoption. Cursor/custom-model profiles
still require available credentials/endpoints and live verification.

Installation and isolated-working-directory checks are recorded under
`.refiner-runs/codex-install-20261018/`. These are installation checks, separate
from the earlier complete model-backed self-audit.

## Create a new business Skill

```sh
.venv/bin/skill-refiner create 'Describe the business workflow and expected rules here' \
  --config examples/backends/live-local.json \
  --output .refiner-runs/new-business-skill
```

Use `--spec FILE` instead of the description for a requirements file, and add
`--standard FILE` for supplied authoring standards. `--name` is optional; otherwise
analysis chooses a name. `--destination NEW_PATH` copies the Skill there only after
all configured engines pass. Existing destinations are never replaced.

The real stages are business analysis → independent contract review → frozen
requirements → working first implementation → independent test planning/review →
baseline → failure-driven repair/regression → fresh acceptance. Each applicable
normal/boundary/error category must occur in development and acceptance suites.
The analysis cannot silently invent material business rules; unanswered questions
stop with `needs_clarification`. Role-call/time budgets span the entire workflow.
Every run records a versioned `stages.json` with stage inputs/outputs, validation
gates, statuses and artifact hashes. `verification.json` is written only after the
machine completion gate passes. Model requests also retain their exact transmitted
payload and byte/hash manifest; oversized requests stop before backend transmission.
See [stage contracts](skill-refiner/references/stage-contracts.md) and [backend
configuration](skill-refiner/references/backends.md).

Inspect `business-analysis.md`, `test-scenarios.md`, `report.md` and the full
`refinement/` evidence. `created` is successful; failed/incomplete runs retain drafts
but withhold final delivery. `final/creation.patch` contains the complete new Skill;
`final/changes.patch` shows repairs relative to its preserved first draft and can
be empty when that draft already passes. Read [creation details](skill-refiner/references/creation.md).

## Run the real demonstration

The example has two intentional implementation bugs: it truncates monetary cents
and returns an error for an empty list. Its requirements are supplied independently
of the erroneous implementation.

```sh
.venv/bin/skill-refiner run examples/decimal-total \
  --spec examples/decimal-total/references/requirements.md \
  --output .refiner-runs/my-demo
```

The output path must not already exist. This run makes real model calls. Defaults:
up to 4 development cases, 2 acceptance cases, 3 repair rounds, 16 planning/review/
repair calls, 50 target evaluations, 240 seconds per operation and 2400 seconds overall. Evaluation model
calls are additional to the planning call count; case/round limits bound their number.
The local Agent's configured model is preserved. Progress is emitted as JSON lines.

For your own Skill:

```sh
.venv/bin/skill-refiner run /absolute/path/to/my-skill \
  --spec /absolute/path/to/requirements.md \
  --standard /absolute/path/to/authoring-standard.md
```

Without `--output`, a uniquely named sibling workspace is created. The original
Skill is never overwritten. A successful run writes `final/<skill-name>/`,
`final/changes.patch`, `final/evidence.json`, `verification.json`, `stages.json` and
`report.md`. `state.json` records
non-successful stops too. Exit 0 means `passed` or `unchanged`; exit 2 means a
reported failure, unresolved coverage, budget exhaustion or unavailable execution.
The successful status is reached only when the completion gate also reports `verified`.

## What runs

1. Snapshot source and gather actual instructions, references and scripts.
2. Ask the planner for a requirement-to-case coverage map, fixtures and oracles.
3. Validate schemas and positive/negative grader controls; independently review tests.
4. Generate fresh acceptance inputs and freeze all requirements/oracles by hash.
5. Run the original through skill-up and review applicable authoring standards.
6. Ask for evidence-linked file repairs, apply them to a candidate copy, run focused
   tests, then full regression including any existing native `evals/eval.yaml` suite.
7. Retain improvements only if no baseline or previously repaired case regresses.
8. Run independent acceptance cases, then deliver a candidate and reproducible evidence.

Unsupported existing Anthropic eval files stop the run with an import instruction.
They are not dropped. Changes to `evals/`, `tests/`, known test files or identity are
rejected. Environment errors stop repair; they are not treated as Skill defects.

## Independent backends and multi-engine regression

```sh
.venv/bin/skill-refiner doctor --config examples/backends/live-local.json
.venv/bin/skill-refiner run examples/decimal-total \
  --config examples/backends/live-local.json \
  --max-evaluations 100 --output .refiner-runs/my-matrix
```

The local example uses Codex for planning/review, a native subagent for repair,
and both Codex and a native subagent for every evaluation. It pins the model used
in local verification; change its model fields to models your account can access.
`examples/backends/all-engines.json` also includes Cursor and a configurable local
API model. Set the API model to your installed model and authenticate Cursor before
expecting those profiles to pass. No backend is silently skipped.

To compare another candidate using exactly the same locked development and
acceptance cases, without replanning or repair:

```sh
.venv/bin/skill-refiner compare /absolute/path/to/candidate-skill \
  --suite /absolute/path/to/previous-run \
  --config /absolute/path/to/backends.json \
  --output /absolute/path/to/new-comparison
```

Every phase writes per-engine logs, a JSON result and a Markdown case-by-engine
table. Regression protects passing `(engine, case)` pairs; delivery requires all
configured engines to pass. Budgets count evaluation cases across the entire
matrix. For the pinned upstream version, local script-judge runs are serialized
to avoid an observed temporary-directory collision; requested and effective case
parallelism are recorded. Native subagent
mode verifies actual spawning and child command records. Custom API execution
contains a working bounded shell/file tool loop, not just a transport stub.

Read [configuration and execution details](skill-refiner/references/backends.md).
Do not mix `--config` with legacy `--engine`, `--engine-config`, `--agent-command`.
Availability checks and provider doubles are not live-model validation; see
[the verification record](VERIFICATION.md) for observed results and missing access.

## Verify

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/skill-refiner check skill-refiner
```

The integration test invokes the **real skill-up binary**, executes the example's
actual script and deliberately requires two separate repair rounds. Its planner and
execution adapter are deterministic test doubles, so this test establishes pipeline
correctness, not LLM quality. Live-run evidence is separate under `.refiner-runs/`.

See [supported scope](skill-refiner/references/scope.md) and
[adapter protocol](skill-refiner/references/protocol.md). The first version supports
local deterministic text/JSON/file validation and command traces. It does not claim
visual evaluation, Docker isolation, semantic output grading, or natural trigger
precision/recall. It does not call this a statistically proven optimizer.

After an interrupted or failed run you can reuse the autonomously generated plans:

```sh
.venv/bin/skill-refiner run /absolute/path/to/my-skill \
  --spec /absolute/path/to/requirements.md \
  --reuse-plan /absolute/path/to/previous-workspace \
  --output /absolute/path/to/new-workspace
```

This verifies frozen plan hashes and identical source/requirements, then reruns
the baseline and all candidate checks. It does not reuse previous PASS verdicts.

## Reuse

- Direct dependency: [Alibaba skill-up](https://github.com/alibaba/skill-up), external
  pinned binary for execution, judges and reports.
- Method reference: [Anthropic skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator),
  especially evidence-based grading and critique of weak assertions.
- Format baseline: [Agent Skills specification](https://agentskills.io/specification).
- Developed here: capability planning protocol, oracle controls/locking, standards
  checks, evidence-linked patches, candidate selection, regression/acceptance gates.


## Latest 0.3 verification

45 automated checks pass. A real business Skill was autonomously analyzed,
authored and test-planned; final recovery replay passed 5/5
development and 6/6 acceptance cases on each of Codex and
native child execution. A separate declared fault was repaired by a real native
Agent and regressed on the complete same frozen matrix. Earlier failures remain
preserved; see [verification details](VERIFICATION.md) for evidence, recovery
provenance and limits.

# Backend configuration and matrix regression

Use `create 'BUSINESS REQUEST' --config /absolute/path/backends.json` for business
analysis, first implementation and the full refinement pipeline. `agents.creation`
independently selects the first author and defaults to repair when omitted.
Use `run TARGET --config /absolute/path/backends.json` for autonomous planning,
repair and regression. Use `compare TARGET --suite /absolute/path/previous-run
--config /absolute/path/backends.json` to execute that run's frozen development
and acceptance suites on another candidate without planning or modifying it.
The compare command validates the suite lock, but deliberately allows a changed
target: its results describe the old contract, not coverage of new capabilities.
No provider is selected implicitly. A managed workflow must receive a complete
`--config`; the legacy interface requires an explicit controller command and
evaluation engine. This lets each host use adapters it actually supports.

```json
{
  "schema_version": 1,
  "agents": {
    "planning": {"backend": "codex", "model": "your-codex-model"},
    "repair": {"backend": "cursor", "model": "your-cursor-model"},
    "review": {
      "backend": "openai_compatible",
      "base_url": "https://your-provider.example/v1",
      "model": "your-review-model",
      "api_key_env": "REVIEW_API_KEY"
    }
  },
  "executors": [
    {"id": "codex", "backend": "codex"},
    {"id": "cursor", "backend": "cursor"},
    {"id": "child", "backend": "codex_subagent"},
    {
      "id": "custom",
      "backend": "openai_compatible",
      "base_url": "http://localhost:11434/v1",
      "model": "your-installed-local-model",
      "max_turns": 12
    }
  ]
}
```

`agents.default` supplies a fallback for omitted roles; without a matching role or
default profile, the workflow stops with a configuration error rather than choosing a vendor. `planning`
handles development and independent acceptance planning; `review` handles plan
and standards review. Each call starts a new context. All controller roles can use any
managed CLI or OpenAI-compatible backend, including `codex_subagent`. Model names
are sent exactly as configured, with no automatic substitution. An omitted CLI
model uses that tool's local default. Configuration selects backends; it does not
install a CLI, authenticate it, or grant access to a model.

Profiles support `backend`, `model`, optional `executable`, `timeout`, `max_turns` and
`max_request_bytes`. The latter bounds the exact serialized request or model prompt (default
350,000 UTF-8 bytes; configurable from 16,384 to 10,000,000) and is recorded per call.
It is a byte guard, not a provider-specific token count. Oversized requests are saved
and rejected before transmission instead of being silently truncated. Set a larger
limit only after verifying that role's actual model context window.
For controller roles, `timeout` caps one Agent call; every call is also capped by the
remaining run-wide wall-clock budget. If no profile timeout is set, the remaining run
budget still bounds the call. OpenAI-compatible HTTP calls run in a killable worker,
so a response that keeps sending bytes cannot extend the deadline. A timeout is an
execution error with saved evidence, not a pass; resume only through the same validated
run workspace. The CLI `--timeout` continues to set the per-case executor limit.
`--agent-parallelism` is separate from executor `--parallelism` and accepts 1 or 2.
At 2, development-plan independent review overlaps with acceptance-plan generation only
after the development plan passes full local controls and freezes its requirement
contract. Both branches have separate call evidence and must join successfully before
the plan gate. At 1 they run sequentially. Cases in the same skill-up executor remain
serial because of the shared judge-directory collision described below.
Codex supports `reasoning_effort`; native delegation additionally supports
`child_model` via Codex's default-subagent-model setting. Model access and that
setting depend on the installed CLI/account; verify the resulting child records.
Codex profiles may set `codex_sandbox` to `read-only`, `workspace-write` or
`danger-full-access`; executor profiles default to `workspace-write`, while planning
calls always use `read-only`. Managed Codex target evaluation rejects
`danger-full-access` because it bypasses the command network sandbox. If a test needs
localhost or external access, it cannot use that strict managed Codex evaluation path.
Use a separately reviewed runner with an explicit network allowlist instead.
Managed target evaluation also requires `workspace-write`: the verified deny-all
network proxy setting applies to that sandbox. A `read-only` target evaluation is
rejected because its loopback behavior has not been verified in this environment.
Profiles do not contain literal credentials. `api_key_env` names an existing
environment variable. API URLs must not contain passwords, query strings or keys.
Use absolute executable/script paths; role working directories are empty.

Every managed target evaluation using the `codex` or `codex_subagent` executor preserves
user config and exec-policy rules, then adds one-shot Codex CLI network/tool overrides:
it enables the workspace-write network proxy with an empty domain allowlist, blocks local/private
destinations, disables upstream proxy chaining, sets `web_search="disabled"`, empties
`mcp_servers`, and disables Apps, Browser Use and Computer Use. These controls apply
to sandboxed shell commands and their subprocesses. Codex still needs its client-side
model and authentication connection; the policy does not mean the whole CLI process
is offline. Unknown Codex config fields fail under `--strict-config`.

## Actual execution paths

- `codex`: real `codex exec --ephemeral --json`; execution evidence comes from
  completed command events. Roles use read-only mode; target evaluations use the
  configured `codex_sandbox` (workspace-write by default) in skill-up's temporary task workspace.
- `cursor`: real Cursor Agent headless CLI with `--output-format stream-json`.
  Searches PATH and `~/.local/bin/cursor-agent`, or uses `executable`. Roles use
  ask mode; evaluation uses `--force` in the temporary task workspace. Requires
  existing Cursor authentication. Shell provenance is only accepted for parsed
  completed events with an exit code and output; unknown event shapes fail closed.
- `codex_subagent`: real native Codex delegation, not another name for a CLI
  process. A coordinator spawns a fresh-context child and waits for its answer.
  The adapter checks the persisted spawn and parent-linked completed child
  rollouts; command evidence is read from actual child CommandExecution events.
  No rollout, no child or incomplete child means ERROR. This mode persists Codex
  sessions under the user's configured CODEX_HOME, and archives only sanitized
  child command records. It supports the observed current CLI event format;
  other versions need verification. The standard output alone can omit spawn
  and child events, so it is insufficient proof of delegation.
- `openai_compatible`: actual HTTP Chat Completions requests, with configurable
  base URL, model and credential environment variable. Evaluation implements a
  bounded model/tool loop (`shell`, `read_file`, `write_file`), records real tool
  outputs, and returns them to the model. Roles request schema-conforming JSON
  without tools. This is not a proxy of Codex and does not borrow its login.
  Providers must support Chat Completions, and evaluation requires function tools.
  `request_options` accepts temperature, token limits, reasoning_effort, seed and
  top_p; it cannot replace messages, tools, model or authentication.
- `claude_code`: real Claude Code JSON event stream. Kept for compatibility;
  validation status is recorded separately from the requested four modes.

Every managed executor runs through skill-up's **custom local transport**; skill-up
still installs the target, materializes fixtures, invokes judges and writes reports.
Existing native eval suites are run on every executor. `skillup` profiles can
instead pass a native `engine` object directly. A `command` profile provides an
argv list: controller roles use the JSON stdin/stdout protocol; executors use
`{input_file}` / `{output_file}` placeholders and the skill-up SessionResult file
protocol. Commands run directly without an implicit shell.

## Matrix invariants and artifacts

Executors run sequentially. The pinned skill-up v0.13.0 uses clock-derived local
script-judge directories; concurrent cases were observed reading each other's
judge script/transcript. Therefore this version forces effective case parallelism
to one and uses a per-user process lock around local skill-up runs. The requested
`--parallelism` and effective value are both recorded. Cross-case judge identities
are checked as an additional fail-closed guard. This reduces throughput until an
upstream version with verified collision-free judging is adopted.
Each executor gets a separate target copy and the same frozen case definitions,
fixtures and judges. `suite_sha256` in every engine result proves the case set is
identical. No engine's successful result is reused as another engine's verdict.
Managed adapters hash the installed candidate's source files before and after
execution. An executor that edits the Skill while doing a test produces ERROR;
repair belongs to the separately configured repair role. Task output files may
still be created or edited as the case requires.

The controller retains `executor-id::case-id` as the regression identity. A patch
cannot trade away one engine's passing case for another engine's improvement.
Full regression, existing suites and independent acceptance must pass on **every
configured engine** before a final candidate is delivered. ERROR stops repair
after collecting the other engines' evidence; missing credentials are environment
problems, not Skill defects. No engine is silently skipped or removed from gates.

Each phase writes `summary.json`, `comparison.md`, and `engines/<id>/` containing
logs and upstream evidence. Comparisons show per-case PASS/FAIL/ERROR, counts,
elapsed time and configured model. Native child records and model API usage are
retained as adapter artifacts. Token/cost accounting across all CLIs is not yet
normalized. Reports demonstrate observed results, not statistical model rankings.

`doctor --config FILE` checks binaries/authentication and credential presence
without a model call. Its availability result is not evidence of live execution.
Use `run` or `compare` and inspect the resulting artifacts for that evidence.

This version uses local environment.type=none. Empty role directories and fresh
contexts are not OS isolation. API shell tools and Cursor force mode can execute
host commands; use this on authorized local task material. It does not claim a
filesystem read barrier around frozen oracles.

Parameter and event references: [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Cursor headless CLI](https://cursor.com/docs/cli/headless),
[Cursor output events](https://cursor.com/docs/cli/reference/output-format),
[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Installed CLI help and retained execution artifacts determine the locally verified behavior.

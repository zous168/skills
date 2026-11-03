# Architecture

The runtime has two layers:

- Google ADK 2.x `Workflow`, `START`, `FunctionNode`, and `Runner` provide the
  graph scheduler and its construction-time graph validation.
- `RefinerControlKernel` is the deterministic authority. It stores state in
  SQLite, freezes inputs by SHA-256, enforces cumulative budgets, confines
  candidate mutations, launches direct-argv processes, records evidence, and
  decides verified/adopted status.

Model or command adapters can only propose JSON. They cannot mark a test as
passed: the Kernel independently runs the plan's commands and evaluates every
assertion from exit code, stdout, files, JSON schema, or a manifest.

Acceptance cases are present in the frozen plan but withheld from author and
repair requests. `final_verify` requires the acceptance stage to have passed
and binds the candidate manifest to immutable verification evidence.

`runtime_driver=adk` is the production path. `builtin_test` exists only for
fast deterministic tests of the same node handlers and is rejected as a
production recommendation.

Role adapters are replaceable without changing the Kernel. `command` launches
a direct-argv local process, `deepseek` calls the schema-constrained DeepSeek
Responses API, and `fake` is limited to deterministic fixtures. Provider
responses remain proposals; only Kernel-owned process evidence and assertions
can produce a verified verdict.

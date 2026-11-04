# Stage persistence contract

The package owns fourteen ordered stage contracts in
`src/skill_refiner/stage_contracts.py`. Each contract has a stable stage
ID, ordinal, unique input and output schema IDs, schema version, validator ID,
validation points, failure states, and a fail-closed resume policy.

Every attempt is stored under:

```text
stages/<stage-id>/<attempt>/
├─ input.json
├─ output.json       # present only after a schema-valid output
└─ envelope.json     # terminal passed or failed record
```

`input.json` binds the stage to exact dependency artifact hashes.
`output.json` is validated by the stage-specific Pydantic model before the
stage can pass. `envelope.json` binds both artifacts to their schema hashes,
records cumulative budgets before and after execution, timestamps, terminal
status, and structured error evidence.

The live `stages.json` index is validated by
`schemas/v1/stages.schema.json` and must agree with SQLite. The immutable
`contracts/stages.json` snapshot is validated by
`schemas/v1/stage-contracts.schema.json`. Artifact and attempt structures are
defined by `stage-artifacts.schema.json` and `stage-attempt.schema.json`.

The live `progress.json` projection uses `skill-refiner.progress/v1` and is
rebuilt whenever state is exported. It maps frozen plan work items to
Kernel-owned development, regression, and acceptance evidence. It is a status
projection, not a second authority: SQLite and immutable artifacts remain the
source of truth.

All JSON artifacts are materialized at their logical paths and copied into the
SHA-256 object store. Resume re-reads both copies, reloads packaged schemas,
checks the frozen contract and controller contract hash, validates every
completed stage and every historical attempt, and retries only the first
non-passing stage. A previously running attempt without a terminal envelope is
closed as recoverable `INTERRUPTED`; a new attempt directory is then used.

Changing the packaged stage contract or schema changes the controller contract
hash. Existing runs fail closed and require a new run instead of silently
migrating evidence.

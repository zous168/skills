# Planning and progress contract

## Plan leaves

`plan.json` uses `skill-refiner.plan/v2`. Its required `work_items` array is the
execution plan exposed to a parent orchestrator. One item represents one
independently observable capability or invariant. If two outputs can fail or
be accepted independently, they are separate items. The contract does not
require an arbitrary number of items.

Every work item has:

- `id`: stable `W-*` identifier;
- `title`: short user-facing name;
- `scope`: files, components, or deliverables it owns;
- `requirement_ids`: frozen requirements it serves;
- `depends_on`: other work-item IDs;
- `done_when`: observable completion statements;
- `development_case_ids` and `acceptance_case_ids`: Kernel-run evidence.

The Kernel rejects duplicate or unknown IDs, self-dependencies, cycles,
uncovered requirements, unassigned cases, and case mappings that do not share
a requirement with the work item. The Reviewer separately decides whether
each item is semantically atomic and whether `done_when` can be verified.

The plan gate opens only when the Reviewer approves and all six structured
checks pass: requirement coverage, atomic work items, observable completion,
evidence mapping, dependency order, and progress handoff.

## Live projection

`progress.json` is a versioned live projection rebuilt from SQLite and
content-addressed evaluation artifacts. It is also embedded in `state.json` as
`progress`. It contains the current Harness stage, the first unresolved leaf
whose dependencies are verified, counts by status, and every work item's
scope, requirements, dependencies, completion criteria, test mappings, status,
and evidence references.

Allowed item states are `pending`, `in_progress`, `verified`, and `blocked`.
Planning alone leaves an item pending. Candidate generation can make it in
progress. A failed mapped test makes it blocked. Verification requires passing
results for every mapped case in development, regression, and held-out
acceptance evidence. Files existing on disk or a model claiming completion do
not verify an item.

## Parent orchestrator handoff

GoalExecutor or another parent mirrors every work item as one executable leaf.
Phase nodes are display-only groups. Preserve `requirement_ids` as goal
references and preserve `done_when` without weakening it. Use
`current_work_item_id` for the current leaf when present, and attach the
projection's evidence references when changing status.

Do not infer a live process from `state.json`. A stage recorded as `running`
describes the last durable transition. Claim that execution is active only
when the host can observe the process or session. Otherwise report the last
recorded stage and state that no live process is observed.

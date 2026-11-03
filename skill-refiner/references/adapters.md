# Adapter contract

A command adapter is launched with `create_subprocess_exec`; no shell command
string is evaluated. It receives one JSON object on stdin:

```json
{"role":"planner","request":{},"response_schema":{}}
```

It must write exactly one JSON object matching `response_schema` to stdout and
exit zero. Stderr is diagnostic only. The Kernel passes an allowlisted
environment plus explicitly named credential variables, applies time/output
limits, redacts common secret forms, and records request/response hashes.

The optional `CandidateProposer` protocol is the only Harness-squared extension
surface. Implementations return a `CandidateProposal`; they receive no Kernel
handle and cannot write run files directly.

## Local host SDK

`kind: cursor_sdk` uses the Python `cursor-sdk` package through a killable
worker process. It uses `Agent.prompt`, an isolated per-call working directory,
`setting_sources=[]`, and `tools=[]`. The worker receives the credential only
through a configured environment variable. The parent adapter accepts only a
terminal `finished` result containing one JSON object that passes the frozen
response Schema.

Use `examples/config.cursor-sdk.json` and install the optional `host` extra.
At the time of the bundled verification, `cursor-sdk 1.2.0` on Windows/Python
3.12 failed while launching its bridge with `WinError 10038`; this is recorded
as an external runtime limitation, not a passing live inference. DeepSeek is
the currently live-verified production backend.

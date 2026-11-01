# Verification evidence

The source project keeps its test harness at `tests/verify_contract.py`, outside the installable Skill. After building, run `python3 tests/verify_contract.py` from the `csp-media` project root. It starts a local loopback fake gateway and launches the packaged `dist/csp-media/scripts/csp_media.py` in subprocesses with a random synthetic key. It does not contact CSP, access public reference URLs or incur generation charges.

The test reports `evidence=local_mock_only` and `live_csp_verified=false`. It checks actual request routes and payloads, alias/capability discovery, local artifact bytes, task-ID reuse during polling, missing-input and missing-key behavior, API errors, and that the synthetic key does not appear in captured output or saved files. The test is a focused contract check; its pass does not prove live gateway availability, account access, upstream provider behavior, real media quality, or every model-specific limit.

Report three kinds of evidence separately:

- **Local mock:** verifier exit code and its JSON stdout.
- **Agent workflow:** actual test prompts, engine(s), observed client calls/results and the test runner's checks.
- **Live CSP:** only a real authorized media request with its resulting task ID and status. Model discovery alone is not a generation test.

Do not claim a test was run if it was only planned. Do not include API keys in logs, screenshots, test fixtures or evidence files.

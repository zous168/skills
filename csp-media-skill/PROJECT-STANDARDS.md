# CSP Media Project Standards

These rules apply to every change in this project and are automatically supplied
to `skill-refiner` when the command runs from this project or targets its built
Skill.

## Project structure and release artifact

- Keep the canonical Agent Skill package in `skill-src/`.
- Treat `dist/csp-media/` as generated output. Change `skill-src/`, rebuild with
  `python3 build.py`, and verify with `python3 build.py --check`; do not hand-edit
  the built package.
- The distribution contains only files needed by an Agent Skill. Keep project
  documentation, build tools and test harnesses outside `dist/csp-media/`.
- Preserve the public Skill name `csp-media` and the request/response contract
  documented in `skill-src/references/client-contract.md` unless a user requirement
  explicitly changes them.

## Testing and evidence

- Keep durable project tests in `tests/`. The fake-gateway contract test must run
  against `dist/csp-media/scripts/csp_media.py`, not a separate reimplementation.
- For changes to the client or its contract, run `python3 build.py`,
  `python3 build.py --check`, and `python3 tests/verify_contract.py`.
- Default to local fake-gateway tests. Do not make live CSP requests or incur
  generation charges without explicit user authorization and configured access.
- Report local mock evidence separately from Agent workflow runs and live CSP
  generation. Never describe mock results as proof of live gateway availability,
  account access, provider behavior or media quality.

## Credentials and safe behavior

- Read the API credential only from `CSP_API_KEY`; never place it in source,
  request files, logs, test fixtures, or user-facing output.
- Keep production requests on the configured HTTPS `/v1` gateway. Permit HTTP only
  for loopback fake-gateway tests.
- Require explicit user confirmation before voice cloning. Do not retry an
  ambiguous media-submission result automatically.

# CSP Media Skill Project

This project develops and tests an Agent Skill for the CSP Gateway. The authored
Skill source lives in `skill-src/`; `dist/csp-media/` is the installable package
assembled from that source. Keep project tests and build tooling outside the
distribution so an Agent receives only the Skill and its runtime resources.

## Build and verify

```sh
python3 build.py
python3 build.py --check
python3 tests/verify_contract.py
```

The contract test runs the built client against a local fake gateway. It reports
`live_csp_verified=false`; it does not call the production service or prove account
access, provider behavior, or media quality. Preserve raw JSON output when recording
test evidence.

Edit the Skill in `skill-src/`, then rebuild `dist/csp-media/` and rerun both checks.
The `dist/` package is the reviewable installable artifact; `build.py --check`
detects stale output.

`PROJECT-STANDARDS.md` defines the shared source, test, build and release rules.
`skill-refiner` discovers it automatically when run from this project or against
`dist/csp-media`; you do not need to pass it with `--standard`.

## Install

Install the built Skill from GitHub with the Agent Skills CLI, replacing `codex`
with the agent you use:

```sh
npx skills add https://github.com/zous168/skills/tree/main/csp-media-skill/dist/csp-media \
  --global --agent codex
```

The CSP credential must be configured as `CSP_API_KEY` in the environment where
the Agent runs. Never put it in chat or request files.

---
name: csp-media
description: Use CSP Gateway to generate images and videos, clone an authorized voice, or synthesize speech with that voice. Applies to requests for CSP media creation through its documented API.
---

# CSP Media

Use the bundled Python client for CSP image, video and audio requests. The client uses only Python 3.9+ standard-library modules, so the complete `csp-media` folder can be installed in any agent's Skill directory. In commands below, replace `$SKILL_ROOT` with the installed directory containing this `SKILL.md` if the host does not define it. Read [the API contract](references/client-contract.md) when choosing request fields or interpreting task responses; use [verification guidance](references/verification.md) when testing the client.

The production API base defaults to `https://llm.zerohalu.com/v1`. Read the bearer credential only from `CSP_API_KEY`; use `CSP_BASE_URL` only when the user configures another compatible `/v1` gateway. Never ask the user to paste a key into chat, a request file or a command. The gateway model field must be an alias returned by `GET /v1/models`, not an upstream provider model name.

## Workflow

1. Map the user's request to `t2i`, `i2i`, `t2v`, `i2v`, `r2v`, `voice` or `speech`. Keep any planning, scriptwriting or prompt improvement within the requested media task. Do not claim unsupported video modes or invent model abilities.
2. When the alias or its operation capability is unknown, run `python3 "$SKILL_ROOT/scripts/csp_media.py" models` and inspect both aliases and capability metadata. The supplied guide shows display-name strings under capability keys, for example `capabilities.t2v`; do not assume capabilities are boolean-only. If the selected alias is unavailable or its exact capability is absent, report what discovery returned and ask the user to choose. Do not probe by submitting a generation.
3. Resolve inputs that affect validity or billable scope before submission. This includes missing video duration or size, an unclear number of outputs, and missing public reference URLs. A singular request can use quantity 1. References must already be publicly reachable by CSP; do not upload them to another host. For voice cloning, obtain explicit confirmation that the user owns the voice or has permission to clone it. If discovery identifies the Xuanling/Xuanmap voice path, also obtain confirmation that the sample is public PCM WAV, 16 kHz, mono; the client does not transcode it.
4. Create a JSON request without credentials, then run `python3 "$SKILL_ROOT/scripts/csp_media.py" run --request REQUEST.json`. The supported request fields and operation-to-API mappings are in [the API contract](references/client-contract.md). The client validates the selected alias and capability before sending a media-creation request.
5. Read the JSON result and exit status. If an asynchronous task remains pending, preserve its returned `task_id` byte-for-byte and resume using `poll`; do not create a replacement task. Do not automatically retry an ambiguous network failure. A video is complete only when the gateway reports completion and supplies a video URL.
6. Report the model alias, operation, status, task ID or `voice_id`, and only the paths actually listed in `saved_paths`. A remote URL or pending task is not a downloaded deliverable. If the client returns an error after already saving an artifact, preserve and report that path.

For example, an image request file can be:

```json
{"operation":"t2i","model":"DISCOVERED_GATEWAY_ALIAS","quantity":1,"prompt":"a red paper crane on a white table","output_dir":"./csp-media-output"}
```

The source project maintains a local fake-gateway contract test at `tests/verify_contract.py`; project maintainers can run it from the repository root after building the Skill. It is local mock evidence only and does not prove live CSP generation. State that live generation is unverified unless an authorized real request was actually submitted.

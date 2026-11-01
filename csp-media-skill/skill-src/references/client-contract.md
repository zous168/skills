# CSP Gateway API contract

Source: [CSP Gateway API Guide](https://csp.zerohalu.com/docs/gateway-api-guide), checked for this Skill on 2026-10-18. The endpoint host is `https://llm.zerohalu.com`; use `/v1` for the OpenAI-compatible runtime and the host-root `/model/info` route for capability metadata.

## Discovery and authentication

The client reads `CSP_API_KEY` from its process environment and sends `Authorization: Bearer …` to the configured API origin only. The default base is `https://llm.zerohalu.com/v1`; a custom `CSP_BASE_URL` must be an HTTPS `/v1` base. HTTP is accepted only for loopback-based local tests. Do not send the key on artifact downloads.

`GET /v1/models` returns gateway alias IDs in `data[].id`. `GET /model/info` may return model records under `data[]`, with `model_name` and a nested `model_info`. The guide's example uses `model_info.mode="video_generation"` and string-valued capability entries such as `capabilities.t2v="文生视频"`. Treat a recognized, non-empty capability key as support for that exact mode. Do not infer `i2v` or `r2v` from a generic video category. A missing or unfamiliar capability remains unconfirmed.

## Request operations

Every `run` request is a UTF-8 JSON object with `operation`, discovered `model` alias, positive integer `quantity`, and `output_dir`. The client accepts no unknown fields. Image `quantity` maps to `n`; all other operations run one item per request. If the user explicitly asks for multiple videos, clones or speech outputs, make separate requests and report them separately.

| Operation | Required request fields | Gateway request |
|---|---|---|
| `t2i` | `prompt` | `POST /v1/images/generations`, body `model`, `prompt`, `n`; optional `size` |
| `i2i` | `prompt`, `images` (array of public URLs) | Same image endpoint; `images` is placed in `extra_body.images` |
| `t2v` | `prompt`, `seconds`, `size` | `POST /v1/videos`; `seconds` and `size` stay at top level |
| `i2v` | `prompt`, `seconds`, `size`, `img_url` | Video endpoint; first-frame URL is `extra_body.img_url` |
| `r2v` | `prompt`, `seconds`, `size`, exactly one of `reference_urls` or `media` | Video endpoint; `extra_body.gen_mode="r2v"` and reference fields are in `extra_body` |
| `voice` | `reference_audio`, `consent:true` | `POST /v1/audio/voices`; body `media:[{"type":"reference_audio","url":"…"}]`; returns `voice_id`; optional `name`, `prefix`, and discovered alias `target_model` pass through |
| `speech` | `text`, `voice_id`, `voice_authorized:true` | `POST /v1/audio/speech`; body uses `input` and `voice`; saves audio response bytes |

Typed `r2v` media entries have exactly `type` and `url`, and `type` must be `reference_image`, `reference_video` or `reference_audio`. The client does not construct other video modes or administrative requests.

Voice `consent:true` must be based on explicit user confirmation that they own the voice or have permission to clone it. For a model alias or model record identifying the Xuanling/Xuanmap voice path, also set `public_pcm_wav_16k_mono:true` only after the user confirms that the public reference is PCM WAV, 16 kHz, mono. The client does not fetch, inspect or transcode the source audio.

The client checks public URL syntax and rejects URL credentials, fragments, localhost and literal private, reserved or link-local IP addresses. This syntax validation cannot prove DNS resolution or public reachability; the Agent must rely on a user-confirmed URL that CSP can access.

## Results and recovery

Image creation can return an image synchronously or an async task ID. The client accepts image URLs or `b64_json`, and polls `GET /v1/images/generations/{id}` when only a task ID is returned. Video creation returns an ID for `GET /v1/videos/{id}`; preserve the returned encoded ID unchanged. A video is considered complete only for a completion status plus a video URL, normally `usage.video_url`. Task states such as `in_progress`, `completed` and `failed` are surfaced; a pending result includes the original ID for later polling.

`poll` accepts `operation` (`image` or `video`), `model`, original `task_id`, `output_dir`, and optional `poll_timeout` / `poll_interval`. Its JSON result has `status`, `model`, `task_id`, `saved_paths`, and `remote_urls`; errors add a short `error` object and clarifications add `missing`. Exit codes are 0 for completion/discovery, 2 clarification or invalid input before submission, 3 missing key, 4 API/task failure, 5 network result potentially unknown, 6 download/local/protocol failure, and 7 pending poll deadline.

Artifact downloads use a separate unauthenticated request and reject redirects. A pending task, remote URL, or download error is not reported as a saved file. Failed downloads retain any known task ID, remote URL and successfully saved earlier paths. The client does not automatically retry or resubmit after ambiguous failures. API error messages and exception text are omitted from output to prevent credential leakage.

## Request examples

Reference-based video:

```json
{"operation":"r2v","model":"DISCOVERED_ALIAS","quantity":1,"prompt":"a slow orbit around the sculpture","seconds":5,"size":"720x1280","reference_urls":["https://media.example/approved-reference.png"],"output_dir":"./csp-media-output"}
```

Voice enrollment using the required typed payload:

```json
{"operation":"voice","model":"DISCOVERED_ENROLLMENT_ALIAS","quantity":1,"reference_audio":"https://media.example/approved-voice.wav","consent":true,"output_dir":"./csp-media-output"}
```

If the selected alias is the Xuanling/Xuanmap path, add `"public_pcm_wav_16k_mono":true` only after confirming that exact source format and public access. Never put the API key in these files.

# DeepSeek adapter

`kind: deepseek` calls the OpenAI-compatible DeepSeek Responses API at
`https://api.deepseek.com/responses`. The adapter sends the role request as
input and the frozen stage response contract as
`text.format.type=json_schema`. It accepts only a completed response containing
one JSON object that passes the same local JSON Schema.

The API key is read from the first configured, non-empty environment variable
(normally `DEEPSEEK_API_KEY`). It is never written to requests, diagnostics,
SQLite, events, or artifacts. Configure the variable outside the Skill:

```powershell
$env:DEEPSEEK_API_KEY = "<set outside chat>"
```

Run the non-billing configuration check:

```powershell
skill-refiner doctor --config examples/config.deepseek.json
```

HTTP 408, 429, 500, 502, 503, 504 and network/timeout failures are retried only
within `max_retries` (0–3). Other HTTP failures, malformed bodies, incomplete
responses, blank text and schema violations fail closed. Each invocation
records the model, response ID, token usage, HTTP attempt outcomes, hashes and
redacted diagnostics.

Official protocol references:

- <https://api-docs.deepseek.com/guides/responses_api/>
- <https://api-docs.deepseek.com/api/create-response/>
- <https://api-docs.deepseek.com/guides/json_mode/>

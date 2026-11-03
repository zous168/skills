# Skill Refiner

Google ADK 2.x Graph 负责编排，Refiner Control Kernel 负责权威状态、预算、哈希链、路径安全、验证与采用。

```bash
python -m pip install -e ".[test]"
skill-refiner doctor --config examples/config.adk.json
skill-refiner create --requirements examples/requirements.md --config examples/config.adk.json --output-root .runs
skill-refiner status .runs/run-xxxxxxxxxxxx
skill-refiner verify .runs/run-xxxxxxxxxxxx
skill-refiner adopt .runs/run-xxxxxxxxxxxx ../my-skill
```

生产配置使用 `runtime_driver: adk`。离线测试显式使用 `builtin_test`，两者调用同一 Control Kernel 和节点实现。

每个阶段持久化 `input.json`、`output.json` 和 `envelope.json`。顶层
`stages.json` 是实时索引，`contracts/stages.json` 是冻结契约；恢复和最终
验证会重新检查 Schema 哈希、内容哈希、SQLite 状态和全部历史 attempt。
详细格式见 `references/persistence.md`。

## DeepSeek backend

`examples/config.deepseek.json` uses the native DeepSeek Responses API adapter.
Set `DEEPSEEK_API_KEY` outside the repository, run `doctor`, then pass that
configuration to `create` or `refine`. The key is never persisted. Responses
must satisfy each stage's frozen JSON Schema before the Kernel consumes them.
See `references/deepseek-adapter.md`.

## Current host SDK backend

Install `.[host]` and use `examples/config.cursor-sdk.json` to route roles
through the local Python `cursor-sdk` adapter. The adapter isolates each call,
disables tools and ambient setting sources, and revalidates the returned JSON.
See `references/adapters.md` for the current Windows bridge limitation and the
difference between implemented/tested support and a live-verified backend.


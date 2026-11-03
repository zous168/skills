from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def main() -> int:
    value = json.load(sys.stdin)
    credential_env = value["credential_env"]
    api_key = os.environ.get(credential_env)
    if not api_key:
        print("configured cursor_sdk credential is unavailable", file=sys.stderr)
        return 2
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    result = Agent.prompt(
        value["prompt"],
        AgentOptions(
            api_key=api_key,
            model=value["model"],
            local=LocalAgentOptions(cwd=value["cwd"], setting_sources=[]),
            tools=[],
        ),
    )
    print(json.dumps({
        "status": result.status,
        "result": result.result,
        "run_id": result.id,
        "agent_id": result.agent_id,
        "model": _plain(result.model),
        "duration_ms": result.duration_ms,
        "usage": _plain(result.usage),
        "error_code": result.error_code,
        "error_message": result.error_message,
    }, ensure_ascii=False))
    return 0 if result.status == "finished" else 3


if __name__ == "__main__":
    raise SystemExit(main())

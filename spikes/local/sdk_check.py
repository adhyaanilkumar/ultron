"""Owner-run check: does the Agent SDK work on the Claude Code Pro login for each model?

Makes 3 tiny real calls. Refuses to run if ANTHROPIC_API_KEY is set.
Usage: uv run --group spikes python spikes/local/sdk_check.py
"""

import asyncio
import os
import sys
import time
from typing import Any

MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
EFFORT = "low"
PROMPT = "Reply with the word ok"


async def run_check(model: str, effort: str = EFFORT) -> dict[str, Any]:
    """One tiny query; returns a result row. Never raises for model errors."""
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        query,
    )

    row: dict[str, Any] = {
        "requested_model": model,
        "requested_effort": effort,
        "reported_model": "not reported",
        "effort_applied": "not reported",
        "ok": False,
        "detail": "",
        "seconds": 0.0,
        "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
    }
    start = time.monotonic()
    try:
        options = ClaudeAgentOptions(
            model=model,
            effort=effort,  # type: ignore[arg-type]
            max_turns=1,
            allowed_tools=[],
            tools=[],
        )
        async for msg in query(prompt=PROMPT, options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                data = msg.data or {}
                row["reported_model"] = str(data.get("model", "not reported"))
                if "effort" in data:
                    row["effort_applied"] = str(data["effort"])
            elif isinstance(msg, ResultMessage):
                usage = msg.model_usage or {}
                if usage and row["reported_model"] == "not reported":
                    row["reported_model"] = ",".join(usage)
                row["ok"] = not msg.is_error
                row["detail"] = (
                    (msg.result or "")[:60] if not msg.is_error else str(msg.errors)
                )
    except Exception as exc:  # noqa: BLE001 - the error text is the finding
        row["detail"] = f"{type(exc).__name__}: {exc}"[:200]
    row["seconds"] = round(time.monotonic() - start, 1)
    return row


def render(rows: list[dict[str, Any]]) -> str:
    head = (
        "| requested model | effort | reported model | effort applied | result | secs | key set |\n"
        "|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for r in rows:
        result = "ok" if r["ok"] else f"FAIL {r['detail']}".replace("|", "/")
        lines.append(
            f"| {r['requested_model']} | {r['requested_effort']} | {r['reported_model']} "
            f"| {r['effort_applied']} | {result} | {r['seconds']} "
            f"| {'present' if r['api_key_present'] else 'absent'} |"
        )
    return "\n".join(lines)


async def run_all() -> list[dict[str, Any]]:
    return [await run_check(m) for m in MODELS]


def main() -> int:
    if "ANTHROPIC_API_KEY" in os.environ:
        print(
            "Refusing: ANTHROPIC_API_KEY is set. Unset it so only the Pro login is used."
        )
        return 2
    print(render(asyncio.run(run_all())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

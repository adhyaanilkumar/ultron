"""Throwaway PreToolUse hook: log every event, never block, print nothing."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    try:
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        tool = data.get("tool_name", "?") if isinstance(data, dict) else "?"
        tin = data.get("tool_input", {}) if isinstance(data, dict) else {}
        cmd = tin.get("command", "") if isinstance(tin, dict) else ""
        log = Path(os.environ.get("SPIKE_HOOK_LOG") or Path(__file__).with_name("hook.log"))
        stamp = datetime.now(timezone.utc).isoformat()
        one = raw.replace("\r", " ").replace("\n", " ")
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{tool}\t{cmd!r}\t{one}\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)

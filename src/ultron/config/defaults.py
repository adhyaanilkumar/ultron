"""Spec constants: roles (D16), rung ladder (11.4), gate presets (section 8), paths."""

from __future__ import annotations

import sys
from pathlib import Path

LOCKED_GATES = ("merge", "deploy", "rollback")
CHANGEABLE_GATES = (
    "intent",
    "spec",
    "tasks",
    "plan",
    "result",
    "escalation",
    "maintain",
)
DEFAULT_HUMAN_GATES = ("intent", "spec", "plan", "result")
DEFAULT_GATES: dict[str, str] = {
    "intent": "human",
    "spec": "human",
    "tasks": "auto",
    "plan": "human",
    "result": "human",
    "escalation": "auto",
    "maintain": "auto",
}
PRESETS: dict[str, dict[str, str]] = {
    "strict": dict.fromkeys(CHANGEABLE_GATES, "human"),
    "default": dict(DEFAULT_GATES),
    "unattended": dict.fromkeys(CHANGEABLE_GATES, "auto"),
}

ADAPTER = "claude-agent-sdk"
# role -> (model, effort)
DEFAULT_AGENTS: dict[str, tuple[str, str]] = {
    "intake_scout": ("haiku-5.5", "medium"),
    "intake": ("sonnet-5.5", "medium"),
    "spec": ("opus-5.5", "high"),
    "decomposition": ("opus-5.5", "high"),
    "maintenance": ("sonnet-5.5", "medium"),
}
DEFAULT_LADDER: dict[str, tuple[str, str]] = {
    "R0": ("haiku-5.5", "medium"),
    "R1": ("sonnet-5.5", "medium"),
    "R2": ("sonnet-5.5", "high"),
    "R3": ("opus-5.5", "high"),
    "R4": ("opus-5.5", "xhigh"),
    "R5": ("opus-5.5", "max"),
}
RUNGS = tuple(DEFAULT_LADDER)

MAX_ATTEMPTS_DEFAULT = 3
MAX_PARALLEL_DEFAULT = 2
# The spec gives no cap amounts (FR18); these are proposed defaults, USD API-equivalent.
CAP_PER_REQUIREMENT_USD = 5.0
CAP_PER_EVALUATION_RUN_USD = 50.0
DENIED_ACTIONS_LIMIT = 5
NO_PROGRESS_MINUTES = 10
SCOUT_TOOL_CALLS = 15
SCOUT_TOKEN_CAP = 50_000
BROKER_ASK_LIMIT = 3
TAG_FORMAT = "v{version}"
ROLLBACK_REHEARSAL_DAYS = 30
MAINTAIN_INTERVAL_MINUTES = 30


def is_windows() -> bool:
    return sys.platform == "win32"


def default_work_folder(windows: bool | None = None, home: Path | None = None) -> str:
    if windows if windows is not None else is_windows():
        return r"C:\git\.ultron-work"
    return str((home or Path.home()) / ".ultron-work")


def default_ultron_home(windows: bool | None = None, home: Path | None = None) -> str:
    if windows if windows is not None else is_windows():
        return r"C:\git\.ultron"
    return str((home or Path.home()) / ".ultron")

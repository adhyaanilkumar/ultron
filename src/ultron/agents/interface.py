"""The swappable agent interface (spec section 11.1): types and the `AgentRunner` protocol.

This module imports only the standard library, so adapters (the Agent SDK, `claude -p`) and the fake all depend on
it and it depends on none of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "ActivityEvent",
    "ActivityKind",
    "AgentResult",
    "AgentRunner",
    "RunLimits",
    "StopReason",
    "Usage",
]


class StopReason(str, Enum):
    DONE = "done"
    TURN_LIMIT = "turn_limit"
    BUDGET = "budget"
    USAGE_LIMIT = "usage_limit"
    OVERLOAD = "overload"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class ActivityKind(str, Enum):
    STARTED = "started"
    FINISHED = "finished"
    READ = "read"
    EDIT = "edit"
    RUN = "run"
    ASK = "ask"
    ANSWER = "answer"
    THINKING = "thinking"
    USAGE = "usage"


@dataclass(frozen=True)
class ActivityEvent:
    """One line of what an agent is doing, the same for every adapter."""

    kind: ActivityKind
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "text": self.text, "data": dict(self.data)}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> ActivityEvent:
        return ActivityEvent(
            ActivityKind(raw["kind"]), str(raw["text"]), dict(raw.get("data", {}))
        )


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def as_tokens(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> Usage:
        return Usage(
            input=int(raw.get("input", 0)),
            output=int(raw.get("output", 0)),
            cache_read=int(raw.get("cache_read", 0)),
            cache_write=int(raw.get("cache_write", 0)),
        )


@dataclass(frozen=True)
class RunLimits:
    """What the engine asks for. `model` and `effort` are the requested setting (the result says what ran)."""

    model: str
    effort: str
    max_turns: int | None = None
    timeout_s: float | None = None
    max_budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "max_turns": self.max_turns,
            "timeout_s": self.timeout_s,
            "max_budget_usd": self.max_budget_usd,
        }


@dataclass(frozen=True)
class AgentResult:
    output: str
    data: Mapping[str, Any] | None
    usage: Usage
    cost_usd: float
    """The adapter's own estimate. The call log recomputes the figure it records."""
    duration_ms: int
    transcript_path: str | None
    stop_reason: StopReason
    model: str
    """The model that actually ran."""
    effort: str
    """The effort that actually ran (the SDK can quietly lower it)."""
    activity: Sequence[ActivityEvent] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "data": None if self.data is None else dict(self.data),
            "usage": self.usage.as_tokens(),
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "transcript_path": self.transcript_path,
            "stop_reason": self.stop_reason.value,
            "model": self.model,
            "effort": self.effort,
            "activity": [a.to_dict() for a in self.activity],
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> AgentResult:
        data = raw["data"]
        return AgentResult(
            output=str(raw["output"]),
            data=None if data is None else dict(data),
            usage=Usage.from_dict(raw["usage"]),
            cost_usd=float(raw["cost_usd"]),
            duration_ms=int(raw["duration_ms"]),
            transcript_path=raw["transcript_path"],
            stop_reason=StopReason(raw["stop_reason"]),
            model=str(raw["model"]),
            effort=str(raw["effort"]),
            activity=tuple(ActivityEvent.from_dict(a) for a in raw["activity"]),
        )


class AgentRunner(Protocol):
    def run(
        self,
        role: str,
        prompt: str,
        workdir: Path,
        allowed_tools: Sequence[str],
        output_schema: Mapping[str, Any] | None,
        limits: RunLimits,
        *,
        on_activity: Callable[[ActivityEvent], None] | None = None,
    ) -> AgentResult: ...

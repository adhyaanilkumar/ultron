"""A scriptable `AgentRunner` for tests: replays scripted results, activity, diffs and failures. No model calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ultron.agents.interface import (
    ActivityEvent,
    AgentResult,
    RunLimits,
    StopReason,
    Usage,
)

__all__ = ["FakeAgentRunner", "FakeCall", "FakeStep", "ScriptExhaustedError"]


class ScriptExhaustedError(AssertionError):
    """The fake was called more times than its script has steps."""


@dataclass(frozen=True)
class FakeStep:
    output: str = ""
    data: Mapping[str, Any] | None = None
    activity: Sequence[ActivityEvent] = ()
    usage: Usage = field(default_factory=lambda: Usage(input=100, output=50))
    stop_reason: StopReason = StopReason.DONE
    duration_ms: int = 10
    diff: Mapping[str, str] = field(default_factory=dict)
    """Files (relative to the workdir) written on the call: the scripted edit."""
    model: str | None = None
    effort: str | None = None
    """What actually ran; defaults to the requested setting."""
    raises: BaseException | None = None


@dataclass(frozen=True)
class FakeCall:
    role: str
    prompt: str
    workdir: Path
    allowed_tools: tuple[str, ...]
    output_schema: Mapping[str, Any] | None
    limits: RunLimits


class FakeAgentRunner:
    def __init__(self, script: Sequence[FakeStep]) -> None:
        self._script = list(script)
        self.calls: list[FakeCall] = []

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
    ) -> AgentResult:
        index = len(self.calls)
        self.calls.append(
            FakeCall(role, prompt, workdir, tuple(allowed_tools), output_schema, limits)
        )
        if index >= len(self._script):
            raise ScriptExhaustedError(f"no scripted step for call {index + 1}")
        step = self._script[index]
        if step.raises is not None:
            raise step.raises
        for event in step.activity:
            if on_activity is not None:
                on_activity(event)
        for rel, content in step.diff.items():
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return AgentResult(
            output=step.output,
            data=step.data,
            usage=step.usage,
            cost_usd=0.0,
            duration_ms=step.duration_ms,
            transcript_path=None,
            stop_reason=step.stop_reason,
            model=step.model or limits.model,
            effort=step.effort or limits.effort,
            activity=tuple(step.activity),
        )

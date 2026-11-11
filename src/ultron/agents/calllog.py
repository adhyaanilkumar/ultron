"""The logging wrapper: cap check, transcript and the `agent_call` event for every call (FR17, FR18)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ultron.agents.interface import (
    ActivityEvent,
    AgentResult,
    AgentRunner,
    RunLimits,
)
from ultron.engine.costs import (
    CapDecision,
    CapLimits,
    Scope,
    UsageLedger,
    UsageRecord,
    check_cap,
)
from ultron.store import events as ev
from ultron.store.eventlog import EventLog

__all__ = ["CallContext", "CapRefusedError", "LoggingRunner"]


class CapRefusedError(Exception):
    """The spending cap was already reached, so the call was not started."""

    def __init__(self, decision: CapDecision) -> None:
        super().__init__(
            f"spending cap reached for {decision.scope.kind} {decision.scope.id}: "
            f"{decision.total} of {decision.cap}"
        )
        self.decision = decision


@dataclass(frozen=True)
class CallContext:
    requirement: str
    task: str | None = None
    attempt: int | None = None
    run: str | None = None
    """The evaluation run this requirement belongs to, if any."""
    prompt_version: str | None = None
    """The prompt template version (T18), written into the `agent_call` event when set."""
    context_files: tuple[Mapping[str, str | bool], ...] = ()
    """Path, SHA-256, kind and `truncated` of each file in the prompt (T18), written into the event when not empty.

    A path that is not valid Unicode also has `path_b64` (see `ultron.agents.context.record_path`)."""
    context_omitted: tuple[str, ...] = ()
    """Paths of repo instruction files left out past the cap (T18), written into the event when not empty."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utf8(value: Any) -> Any:
    """`value` with every string made valid UTF-8: a lone surrogate (from a non-Unicode path) becomes `\\udcXX` text.

    Valid text is unchanged. Lists, tuples and mappings are walked; other values are returned as they are.
    """
    if isinstance(value, str):
        return value.encode("utf-8", errors="backslashreplace").decode("utf-8")
    if isinstance(value, Mapping):
        return {_utf8(k): _utf8(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_utf8(v) for v in value]
    return value


def _context_data(context: CallContext) -> dict[str, Any]:
    """The T18 fields of the `agent_call` event: only the ones set, every string valid UTF-8."""
    data: dict[str, Any] = {}
    if context.prompt_version is not None:
        data["prompt_version"] = _utf8(context.prompt_version)
    if context.context_files:
        data["context_files"] = [_utf8(f) for f in context.context_files]
    if context.context_omitted:
        data["context_omitted"] = _utf8(context.context_omitted)
    return data


def _preflight(
    context: CallContext, role: str, prompt: str, data: dict[str, Any]
) -> None:
    """Raise `InvalidEventError` now, before the agent runs and is charged, if its `agent_call` event could not be
    written: a role, requirement, task or attempt the event refuses, or text that is not valid UTF-8."""
    ev.NewEvent(
        actor=ev.agent_actor(role),
        type=ev.AGENT_CALL,
        requirement=context.requirement,
        task=context.task,
        attempt=context.attempt,
        data=data,
    )
    for name, text in (
        ("prompt", prompt),
        ("requirement", context.requirement),
        ("task", context.task or ""),
    ):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ev.InvalidEventError(f"{name} is not valid UTF-8: {exc}") from None
    try:
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ev.InvalidEventError(f"context fields cannot be logged: {exc}") from exc


class LoggingRunner:
    """Wraps any `AgentRunner`. Writes under `ultron_dir`: `transcripts/` and `activity.jsonl`."""

    def __init__(
        self,
        inner: AgentRunner,
        *,
        ultron_dir: Path,
        log: EventLog,
        ledger: UsageLedger,
        caps: CapLimits,
        estimate: Decimal = Decimal(0),
    ) -> None:
        self._inner = inner
        self._dir = Path(ultron_dir)
        self._log = log
        self._ledger = ledger
        self._caps = caps
        self._estimate = estimate

    def run(
        self,
        context: CallContext,
        role: str,
        prompt: str,
        workdir: Path,
        allowed_tools: Sequence[str],
        output_schema: Mapping[str, Any] | None,
        limits: RunLimits,
        *,
        on_activity: Callable[[ActivityEvent], None] | None = None,
    ) -> AgentResult:
        extra = _context_data(context)
        _preflight(context, role, prompt, extra)
        scopes = [Scope.requirement(context.requirement)]
        if context.run is not None:
            scopes.append(Scope.evaluation_run(context.run))
        for scope in scopes:
            decision = check_cap(self._ledger, scope, self._estimate, self._caps)
            if not decision.allowed:
                raise CapRefusedError(decision)

        seen: list[ActivityEvent] = []

        def tee(event: ActivityEvent) -> None:
            seen.append(event)
            if on_activity is not None:
                on_activity(event)

        result = self._inner.run(
            role,
            prompt,
            workdir,
            allowed_tools,
            output_schema,
            limits,
            on_activity=tee,
        )
        activity = tuple(result.activity) or tuple(seen)

        record = UsageRecord(result.model, result.usage.as_tokens())
        cost = float(self._ledger.add(context.requirement, record, context.run).cost)

        rel = self._write_transcript(
            context, role, prompt, limits, result, activity, cost
        )
        self._append_activity(context, role, activity)
        event = ev.agent_call(
            context.requirement,
            role=role,
            model=result.model,
            effort=result.effort,
            duration_ms=result.duration_ms,
            tokens=result.usage.as_tokens(),
            cost_usd=cost,
            transcript=rel,
            prompt_sha256=_sha(prompt),
            result_sha256=_sha(
                json.dumps(
                    {"output": result.output, "data": result.data},
                    sort_keys=True,
                    default=str,
                )
            ),
            task=context.task,
            attempt=context.attempt,
            stop_reason=result.stop_reason.value,
        )
        event.data.update(extra)
        self._log.append(event)
        return result

    def _write_transcript(
        self,
        context: CallContext,
        role: str,
        prompt: str,
        limits: RunLimits,
        result: AgentResult,
        activity: Sequence[ActivityEvent],
        cost: float,
    ) -> str:
        folder = self._dir / "transcripts" / context.requirement
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{context.task or 'req'}-a{context.attempt or 0}-{role}"
        n = 1
        while (folder / f"{stem}-{n}.json").exists():
            n += 1
        path = folder / f"{stem}-{n}.json"
        body = {
            "role": role,
            "requirement": context.requirement,
            "task": context.task,
            "attempt": context.attempt,
            "requested": limits.to_dict(),
            "ran": {"model": result.model, "effort": result.effort},
            "prompt": prompt,
            "activity": [a.to_dict() for a in activity],
            "result": result.to_dict(),
            "cost_usd": cost,
        }
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        return path.relative_to(self._dir).as_posix()

    def _append_activity(
        self, context: CallContext, role: str, activity: Sequence[ActivityEvent]
    ) -> None:
        if not activity:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        with (self._dir / "activity.jsonl").open("a", encoding="utf-8") as fh:
            for event in activity:
                row = {
                    "role": role,
                    "requirement": context.requirement,
                    "task": context.task,
                    "attempt": context.attempt,
                    **event.to_dict(),
                }
                fh.write(json.dumps(row) + "\n")

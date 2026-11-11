"""Shared builders for the monitor tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ultron.store import events as ev
from ultron.store.eventlog import EventLog, events_path

REQ = "R-20261109-a3f2"


def n(
    req: str,
    type_: str,
    task: str | None = None,
    attempt: int | None = None,
    **data: object,
) -> ev.NewEvent:
    return ev.NewEvent(
        actor="engine",
        type=type_,
        requirement=req,
        task=task,
        attempt=attempt,
        data=dict(data),
    )


def gate(req: str, name: str, task: str | None = None) -> ev.NewEvent:
    return ev.gate_decision(
        req, actor="auto", gate=name, decision="approve", channel="auto", task=task
    )


def in_progress(root: Path, req: str = REQ) -> EventLog:
    log = EventLog(events_path(root, req))
    log.append(
        [
            n(req, "spec_started"),
            n(req, "spec_drafted"),
            gate(req, "spec"),
            n(req, "decomposition_started"),
            n(req, "task_created", "T1", depends_on=[], max_attempts=3),
            n(req, "task_created", "T2", depends_on=["T1"], max_attempts=3),
            n(req, "tasks_drafted"),
            gate(req, "tasks"),
            n(req, "task_started", "T1"),
        ]
    )
    return log


def finished(root: Path, req: str, when: datetime) -> None:
    """A requirement with one task, approved, so it is `complete`; every event is stamped `when`."""
    log = EventLog(events_path(root, req), clock=lambda: when)
    log.append(
        [
            n(req, "spec_started"),
            n(req, "spec_drafted"),
            gate(req, "spec"),
            n(req, "decomposition_started"),
            n(req, "task_created", "T1", depends_on=[], max_attempts=3),
            n(req, "tasks_drafted"),
            gate(req, "tasks"),
            n(req, "task_started", "T1"),
            n(req, "plan_drafted", "T1"),
            gate(req, "plan", "T1"),
            n(req, "attempt_started", "T1", 1),
            n(req, "coding_finished", "T1", 1),
            n(req, "attempt_finished", "T1", 1, outcome="passed"),
            n(req, "result_requested", "T1"),
            gate(req, "result", "T1"),
        ]
    )

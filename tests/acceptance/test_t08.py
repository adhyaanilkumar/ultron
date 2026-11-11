"""Acceptance tests for T08 (state machines and requirement ids), by the testing agent.

Written from the acceptance criteria and spec §3 (state diagrams), §7.2, §8, §9.1, FR9, FR10 and FR18, with expected
transitions written by hand here, not copied from the developer's tests.

Each criterion has a `verify_with` of `pytest`:
- AC1 `test_ac1_*`: every legal transition from the §3 diagrams, and every other (state, trigger) pair raises.
- AC2 `test_ac2_*`: fold twice, prefix stability, no shared state between folds.
- AC3 `test_ac3_*`: rejected and failed lead to escalated, escalation cascades to dependents as blocked.
- AC4 `test_ac4_*`: one case per requirement outcome, plus unfinished and attempt counting (FR9, FR10).
- AC5 `test_ac5_*`: id format, local date, forced collisions.
No criterion has a `ci` half; CI only reruns these on Windows and Linux.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ultron.engine import ids
from ultron.engine.states import (
    IllegalTransition,
    RequirementState,
    Snapshot,
    TaskState,
    fold,
    next_requirement_state,
    next_task_state,
)
from ultron.store.events import Event

R = RequirementState
T = TaskState

REQ = "R-20261111-a3f2"
USER = "user:Tester <tester@example.com>"

# --- building event lists -----------------------------------------------------------------------------------------


class Log:
    """Builds a requirement's event list with gap-free seq numbers."""

    def __init__(self, requirement: str = REQ) -> None:
        self.requirement = requirement
        self.events: list[Event] = []

    def add(
        self,
        type_: str,
        *,
        task: str | None = None,
        attempt: int | None = None,
        actor: str = "engine",
        **data: Any,
    ) -> Event:
        seq = len(self.events) + 1
        event = Event(
            schema=1,
            seq=seq,
            time=f"2026-11-11T10:{seq // 60:02d}:{seq % 60:02d}+05:30",
            actor=actor,
            type=type_,
            requirement=self.requirement,
            task=task,
            attempt=attempt,
            data=dict(data),
            ultron_version="0.0.0",
        )
        self.events.append(event)
        return event

    def gate(
        self,
        gate: str,
        decision: str,
        *,
        task: str | None = None,
        auto: bool = False,
    ) -> Event:
        return self.add(
            "gate_decision",
            task=task,
            actor="auto" if auto else USER,
            gate=gate,
            decision=decision,
            channel="auto" if auto else "terminal",
            comment="",
        )

    # requirement steps
    def to_decomposing(self) -> None:
        self.add("intake_started")
        self.add("intent_drafted")
        self.gate("intent", "approve")
        self.add("spec_drafted")
        self.gate("spec", "approve")
        self.add("decomposition_started")

    def to_in_progress(self, tasks: dict[str, dict[str, Any]]) -> None:
        self.to_decomposing()
        for name, spec in tasks.items():
            self.add("task_created", task=name, **spec)
        self.add("tasks_drafted")
        self.gate("tasks", "approve", auto=True)

    # task steps
    def begin(self, name: str) -> None:
        """pending -> planning -> awaiting_plan -> plan_approved"""
        self.add("task_started", task=name)
        self.add("plan_drafted", task=name)
        self.gate("plan", "approve", task=name)

    def replan(self, name: str) -> None:
        """After a failed attempt (state planning): a revised plan goes through the plan gate again."""
        self.add("plan_drafted", task=name)
        self.gate("plan", "approve", task=name)

    def start_attempt(self, name: str, n: int) -> None:
        self.add("attempt_started", task=name, attempt=n)

    def code(self, name: str) -> None:
        self.add("coding_finished", task=name)

    def finish(self, name: str, n: int, outcome: str) -> None:
        self.add("attempt_finished", task=name, attempt=n, outcome=outcome)

    def result(self, name: str, decision: str) -> None:
        self.add("result_requested", task=name)
        self.gate("result", decision, task=name)

    def approve_task(self, name: str) -> None:
        self.begin(name)
        self.start_attempt(name, 1)
        self.code(name)
        self.finish(name, 1, "passed")
        self.result(name, "approve")

    def escalate(self, name: str, **data: Any) -> None:
        self.add("escalation", task=name, **data)

    def fail_all_attempts(self, name: str, attempts: int) -> None:
        """Take a started (plan approved) task through `attempts` failing attempts, ending in `failed`."""
        for n in range(1, attempts + 1):
            if n > 1:
                self.replan(name)
            self.start_attempt(name, n)
            self.code(name)
            self.finish(name, n, "failed")


def states(snapshot: Snapshot) -> dict[str, TaskState]:
    return {name: status.state for name, status in snapshot.tasks.items()}


def one(
    max_attempts: int | None = None,
) -> tuple[Log, dict[str, dict[str, Any]]]:
    spec: dict[str, Any] = {}
    if max_attempts is not None:
        spec["max_attempts"] = max_attempts
    return Log(), {"T1": spec}


# --- AC1: every legal transition from spec §3, written by hand ------------------------------------------------

# §3: submitted -> [intake_drafting -> awaiting_intent] -> spec_drafting -> awaiting_spec -> (changes ->
# spec_drafting)* -> spec_approved -> decomposing -> [awaiting_tasks] -> in_progress -> complete|partial|escalated
LEGAL_REQ: dict[tuple[RequirementState, str], RequirementState] = {
    (R.SUBMITTED, "intake_started"): R.INTAKE_DRAFTING,
    (R.SUBMITTED, "spec_started"): R.SPEC_DRAFTING,
    (R.INTAKE_DRAFTING, "intent_drafted"): R.AWAITING_INTENT,
    (R.AWAITING_INTENT, "intent.approve"): R.SPEC_DRAFTING,
    (R.SPEC_DRAFTING, "spec_drafted"): R.AWAITING_SPEC,
    (R.AWAITING_SPEC, "spec.changes"): R.SPEC_DRAFTING,
    (R.AWAITING_SPEC, "spec.approve"): R.SPEC_APPROVED,
    (R.SPEC_APPROVED, "decomposition_started"): R.DECOMPOSING,
    (R.DECOMPOSING, "tasks_drafted"): R.AWAITING_TASKS,
    (R.DECOMPOSING, "tasks.approve"): R.IN_PROGRESS,
    (R.AWAITING_TASKS, "tasks.approve"): R.IN_PROGRESS,
    (R.IN_PROGRESS, "all_approved"): R.COMPLETE,
    (R.IN_PROGRESS, "partly_approved"): R.PARTIAL,
    (R.IN_PROGRESS, "none_approved"): R.ESCALATED,
}

# §3: pending -> planning -> awaiting_plan -> (changes -> planning)* -> plan_approved -> attempt k (coding ->
# testing) -> passed -> awaiting_result -> approved|rejected; failed after max_attempts; rejected and failed lead to
# escalated; a stopped agent leads to held, the next attempt, or escalated; a task behind an escalated one is blocked.
LEGAL_TASK: dict[tuple[TaskState, str], TaskState] = {
    (T.PENDING, "task_started"): T.PLANNING,
    (T.PENDING, "dependency_escalated"): T.BLOCKED,
    (T.PLANNING, "plan_drafted"): T.AWAITING_PLAN,
    (T.AWAITING_PLAN, "plan.changes"): T.PLANNING,
    (T.AWAITING_PLAN, "plan.approve"): T.PLAN_APPROVED,
    (T.PLAN_APPROVED, "attempt_started"): T.CODING,
    (T.CODING, "coding_finished"): T.TESTING,
    (T.TESTING, "attempt_passed"): T.PASSED,
    (T.CODING, "attempt_failed"): T.PLANNING,
    (T.TESTING, "attempt_failed"): T.PLANNING,
    (T.CODING, "attempts_exhausted"): T.FAILED,
    (T.TESTING, "attempts_exhausted"): T.FAILED,
    (T.PASSED, "result_requested"): T.AWAITING_RESULT,
    (T.AWAITING_RESULT, "result.approve"): T.APPROVED,
    (T.AWAITING_RESULT, "result.reject"): T.REJECTED,
    (T.PLANNING, "agent_held"): T.HELD,
    (T.CODING, "agent_held"): T.HELD,
    (T.TESTING, "agent_held"): T.HELD,
}
# escalation: from rejected and failed (AC3), from held and from every unfinished state (stop with then=escalate
# §9.1, spending cap FR18). Not from approved. Whether blocked or escalated may "escalate again" is spec silent.
ESCALATE_FROM = (
    T.PENDING,
    T.PLANNING,
    T.AWAITING_PLAN,
    T.PLAN_APPROVED,
    T.CODING,
    T.TESTING,
    T.PASSED,
    T.AWAITING_RESULT,
    T.HELD,
    T.REJECTED,
    T.FAILED,
)
for _state in ESCALATE_FROM:
    LEGAL_TASK[(_state, "escalation")] = T.ESCALATED
SPEC_SILENT_TASK = {(T.BLOCKED, "escalation"), (T.ESCALATED, "escalation")}

GATES = ("intent", "spec", "tasks", "plan", "result", "merge", "deploy", "escalation")
DECISIONS = ("approve", "changes", "reject")
EVENT_TRIGGERS = (
    "intake_started",
    "intent_drafted",
    "spec_started",
    "spec_drafted",
    "decomposition_started",
    "tasks_drafted",
    "task_created",
    "task_started",
    "plan_drafted",
    "coding_finished",
    "result_requested",
    "attempt_started",
    "attempt_finished",
    "attempt_passed",
    "attempt_failed",
    "attempts_exhausted",
    "agent_held",
    "agent.stopped",
    "dependency_escalated",
    "escalation",
    "all_approved",
    "partly_approved",
    "none_approved",
    "bogus",
    "",
)
UNIVERSE = sorted(
    set(EVENT_TRIGGERS)
    | {f"{g}.{d}" for g in GATES for d in DECISIONS}
    | {t for _, t in LEGAL_REQ}
    | {t for _, t in LEGAL_TASK}
)


def test_ac1_every_legal_requirement_transition_succeeds() -> None:
    for (state, trigger), want in LEGAL_REQ.items():
        assert next_requirement_state(state, trigger) is want, (state, trigger)


def test_ac1_every_other_requirement_pair_raises() -> None:
    wrongly_allowed = []
    for state in RequirementState:
        for trigger in UNIVERSE:
            if (state, trigger) in LEGAL_REQ:
                continue
            try:
                got = next_requirement_state(state, trigger)
            except IllegalTransition:
                continue
            wrongly_allowed.append((state.value, trigger, got.value))
    assert not wrongly_allowed


def test_ac1_every_legal_task_transition_succeeds() -> None:
    for (state, trigger), want in LEGAL_TASK.items():
        assert next_task_state(state, trigger) is want, (state, trigger)


def test_ac1_every_other_task_pair_raises() -> None:
    wrongly_allowed = []
    for state in TaskState:
        for trigger in UNIVERSE:
            if trigger == "task_resumed" or (state, trigger) in LEGAL_TASK:
                continue
            if (state, trigger) in SPEC_SILENT_TASK:
                continue
            try:
                got = next_task_state(state, trigger)
            except IllegalTransition:
                continue
            wrongly_allowed.append((state.value, trigger, got.value))
    assert not wrongly_allowed


def test_ac1_nothing_leaves_a_finished_requirement_or_task() -> None:
    for state in (R.COMPLETE, R.PARTIAL, R.ESCALATED):
        for trigger in UNIVERSE:
            with pytest.raises(IllegalTransition):
                next_requirement_state(state, trigger)
    for state in (T.APPROVED, T.ESCALATED):
        for trigger in UNIVERSE:
            if (state, trigger) in SPEC_SILENT_TASK:
                continue
            with pytest.raises(IllegalTransition):
                next_task_state(state, trigger)


def test_ac1_blocked_task_cannot_start_plan_or_code() -> None:
    for trigger in ("task_started", "plan_drafted", "attempt_started", "plan.approve"):
        with pytest.raises(IllegalTransition):
            next_task_state(T.BLOCKED, trigger)


@pytest.mark.parametrize("held_from", [T.PLANNING, T.CODING, T.TESTING])
def test_ac1_resume_returns_to_where_the_task_was_held(held_from: TaskState) -> None:
    assert next_task_state(T.HELD, "task_resumed", held_from=held_from) is held_from


@pytest.mark.parametrize("state", [s for s in TaskState if s is not T.HELD])
def test_ac1_resume_only_from_held(state: TaskState) -> None:
    for held_from in (None, T.PLANNING, T.CODING):
        with pytest.raises(IllegalTransition):
            next_task_state(state, "task_resumed", held_from=held_from)


def test_ac1_strict_s3_gate_decisions_are_illegal() -> None:
    """§3 draws a `changes` loop only for spec and plan, and no `reject` edge before result. Spec §8 lists three
    form buttons, so for intent/tasks `changes` and the `reject`s the spec is silent; the developer chose strict §3."""
    for state, trigger in [
        (R.AWAITING_INTENT, "intent.changes"),
        (R.AWAITING_INTENT, "intent.reject"),
        (R.AWAITING_SPEC, "spec.reject"),
        (R.AWAITING_TASKS, "tasks.changes"),
        (R.AWAITING_TASKS, "tasks.reject"),
    ]:
        with pytest.raises(IllegalTransition):
            next_requirement_state(state, trigger)
    for state, trigger in [
        (T.AWAITING_PLAN, "plan.reject"),
        (T.AWAITING_RESULT, "result.changes"),
    ]:
        with pytest.raises(IllegalTransition):
            next_task_state(state, trigger)


# AC1 through fold: the same diagrams as event lists, checked after every event.


def test_ac1_fold_walks_every_requirement_and_task_state() -> None:
    log = Log()
    trace: list[tuple[RequirementState, dict[str, TaskState]]] = []

    def step(
        req: RequirementState, tasks: dict[str, TaskState], count_before: int
    ) -> None:
        assert len(log.events) == count_before + 1, "one event per step"
        trace.append((req, dict(tasks)))

    n = 0

    def ev(
        event_fn: Any,
        req: RequirementState,
        tasks: dict[str, TaskState] | None = None,
    ) -> None:
        nonlocal n
        event_fn()
        step(req, tasks or {}, n)
        n += 1

    ev(lambda: log.add("intake_started"), R.INTAKE_DRAFTING)
    ev(lambda: log.add("intent_drafted"), R.AWAITING_INTENT)
    ev(lambda: log.gate("intent", "approve"), R.SPEC_DRAFTING)
    ev(lambda: log.add("spec_drafted"), R.AWAITING_SPEC)
    ev(lambda: log.gate("spec", "changes"), R.SPEC_DRAFTING)
    ev(lambda: log.add("spec_drafted"), R.AWAITING_SPEC)
    ev(lambda: log.gate("spec", "approve"), R.SPEC_APPROVED)
    ev(lambda: log.add("decomposition_started"), R.DECOMPOSING)
    ev(lambda: log.add("task_created", task="T1"), R.DECOMPOSING, {"T1": T.PENDING})
    ev(lambda: log.add("tasks_drafted"), R.AWAITING_TASKS, {"T1": T.PENDING})
    ev(lambda: log.gate("tasks", "approve"), R.IN_PROGRESS, {"T1": T.PENDING})
    ev(lambda: log.add("task_started", task="T1"), R.IN_PROGRESS, {"T1": T.PLANNING})
    ev(
        lambda: log.add("plan_drafted", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.AWAITING_PLAN},
    )
    ev(
        lambda: log.gate("plan", "changes", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.PLANNING},
    )
    ev(
        lambda: log.add("plan_drafted", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.AWAITING_PLAN},
    )
    ev(
        lambda: log.gate("plan", "approve", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.PLAN_APPROVED},
    )
    ev(lambda: log.start_attempt("T1", 1), R.IN_PROGRESS, {"T1": T.CODING})
    ev(lambda: log.code("T1"), R.IN_PROGRESS, {"T1": T.TESTING})
    ev(lambda: log.finish("T1", 1, "failed"), R.IN_PROGRESS, {"T1": T.PLANNING})
    ev(
        lambda: log.add("plan_drafted", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.AWAITING_PLAN},
    )
    ev(
        lambda: log.gate("plan", "approve", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.PLAN_APPROVED},
    )
    ev(lambda: log.start_attempt("T1", 2), R.IN_PROGRESS, {"T1": T.CODING})
    ev(lambda: log.code("T1"), R.IN_PROGRESS, {"T1": T.TESTING})
    ev(lambda: log.finish("T1", 2, "passed"), R.IN_PROGRESS, {"T1": T.PASSED})
    ev(
        lambda: log.add("result_requested", task="T1"),
        R.IN_PROGRESS,
        {"T1": T.AWAITING_RESULT},
    )
    ev(lambda: log.gate("result", "approve", task="T1"), R.COMPLETE, {"T1": T.APPROVED})

    for i, (req, tasks) in enumerate(trace, start=1):
        snap = fold(log.events[:i])
        assert snap.requirement is req, (i, log.events[i - 1].type)
        assert states(snap) == tasks, (i, log.events[i - 1].type)


def test_ac1_fold_template_intent_skips_intake() -> None:
    log = Log()
    log.add("spec_started")
    assert fold(log.events).requirement is R.SPEC_DRAFTING
    log.add("spec_drafted")
    log.gate("spec", "approve")
    assert fold(log.events).requirement is R.SPEC_APPROVED


def test_ac1_fold_auto_tasks_gate_may_skip_awaiting_tasks() -> None:
    """§3 brackets awaiting_tasks: an auto tasks gate need not stop there."""
    log = Log()
    log.to_decomposing()
    log.add("task_created", task="T1")
    log.gate("tasks", "approve", auto=True)
    assert fold(log.events).requirement is R.IN_PROGRESS


def test_ac1_fold_auto_gates_are_recorded_and_state_is_the_same() -> None:
    """An auto plan/result decision (actor `auto`) moves the task exactly like a human one."""
    human, auto = Log(), Log()
    for log, is_auto in ((human, False), (auto, True)):
        log.to_in_progress({"T1": {}})
        log.add("task_started", task="T1")
        log.add("plan_drafted", task="T1")
        log.gate("plan", "approve", task="T1", auto=is_auto)
        log.start_attempt("T1", 1)
        log.code("T1")
        log.finish("T1", 1, "passed")
        log.add("result_requested", task="T1")
        log.gate("result", "approve", task="T1", auto=is_auto)
    assert fold(human.events) == fold(auto.events)
    assert fold(auto.events).requirement is R.COMPLETE


@pytest.mark.parametrize(
    "build",
    [
        lambda log: log.add(
            "spec_drafted"
        ),  # submitted -> awaiting_spec skips spec_drafting
        lambda log: (log.add("intake_started"), log.gate("intent", "approve")),
        lambda log: (log.add("spec_started"), log.gate("spec", "approve")),
        lambda log: (log.add("spec_started"), log.add("decomposition_started")),
        lambda log: (log.add("intake_started"), log.add("intake_started")),
        lambda log: log.add("task_created", task="T1"),  # before decomposing
        lambda log: log.add("task_started", task="T1"),
    ],
)
def test_ac1_fold_illegal_requirement_sequences_raise(build: Any) -> None:
    log = Log()
    build(log)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def _in_progress_one(max_attempts: int | None = None) -> Log:
    log, spec = one(max_attempts)
    log.to_in_progress(spec)
    return log


@pytest.mark.parametrize(
    "build",
    [
        lambda log: log.add("plan_drafted", task="T1"),  # pending, not started
        lambda log: log.add("coding_finished", task="T1"),
        lambda log: log.add("attempt_started", task="T1", attempt=1),
        lambda log: log.result("T1", "approve"),
        lambda log: (
            log.add("task_started", task="T1"),
            log.add("task_started", task="T1"),
        ),
        lambda log: (
            log.add("task_started", task="T1"),
            log.gate("plan", "approve", task="T1"),
        ),
        lambda log: (
            log.add("task_started", task="T1"),
            log.add("plan_drafted", task="T1"),
            log.add("attempt_started", task="T1", attempt=1),  # before plan approved
        ),
        lambda log: (
            log.begin("T1"),
            log.code("T1"),
        ),  # coding_finished before an attempt
        lambda log: (
            log.begin("T1"),
            log.start_attempt("T1", 1),
            log.finish("T1", 1, "passed"),  # passed without coding_finished
        ),
        lambda log: (
            log.begin("T1"),
            log.start_attempt("T1", 1),
            log.code("T1"),
            log.finish("T1", 1, "passed"),
            log.gate(
                "result", "approve", task="T1"
            ),  # result gate before it was requested
        ),
        lambda log: log.add("coding_finished", task="NOPE"),  # unknown task
    ],
)
def test_ac1_fold_illegal_task_sequences_raise(build: Any) -> None:
    log = _in_progress_one()
    build(log)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ac1_task_events_need_an_in_progress_requirement() -> None:
    log = Log()
    log.to_in_progress({"T1": {}})
    log.approve_task("T1")  # requirement is now complete
    assert fold(log.events).requirement is R.COMPLETE
    log.add("task_started", task="T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def _after_dependency(a_progress: str) -> Log:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}})
    if a_progress == "planning":
        log.add("task_started", task="A")
    elif a_progress == "awaiting_result":
        log.begin("A")
        log.start_attempt("A", 1)
        log.code("A")
        log.finish("A", 1, "passed")
        log.add("result_requested", task="A")
    elif a_progress == "approved":
        log.approve_task("A")
    log.add("task_started", task="B")
    return log


@pytest.mark.parametrize("a_progress", ["pending", "planning", "awaiting_result"])
def test_ac1_task_started_is_illegal_while_a_dependency_is_not_approved(
    a_progress: str,
) -> None:
    with pytest.raises(IllegalTransition):
        fold(_after_dependency(a_progress).events)


def test_ac1_task_started_is_legal_once_the_dependency_is_approved() -> None:
    assert fold(_after_dependency("approved").events).tasks["B"].state is T.PLANNING


def test_ac1_task_started_waits_for_all_of_several_dependencies() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}, "C": {"depends_on": ["A", "B"]}})
    log.approve_task("A")
    log.add("task_started", task="C")  # B is still pending
    with pytest.raises(IllegalTransition):
        fold(log.events)
    log2 = Log()
    log2.to_in_progress({"A": {}, "B": {}, "C": {"depends_on": ["A", "B"]}})
    log2.approve_task("A")
    log2.approve_task("B")
    log2.add("task_started", task="C")
    assert fold(log2.events).tasks["C"].state is T.PLANNING


def test_ac1_independent_tasks_can_run_in_parallel() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.add("task_started", task="A")
    log.add("task_started", task="B")
    log.add("plan_drafted", task="B")
    log.add("plan_drafted", task="A")
    assert states(fold(log.events)) == {"A": T.AWAITING_PLAN, "B": T.AWAITING_PLAN}


# --- AC2: replay is deterministic ----------------------------------------------------------------------------


def _busy_log() -> Log:
    log = Log()
    log.to_in_progress(
        {
            "A": {"max_attempts": 2},
            "B": {"depends_on": ["A"]},
            "C": {},
            "D": {"depends_on": ["C"]},
        }
    )
    log.begin("A")
    log.start_attempt("A", 1)
    log.add("agent_call", task="A", role="developer")  # not part of the vocabulary
    log.code("A")
    log.finish("A", 1, "failed")
    log.add("config_snapshot")
    log.replan("A")
    log.start_attempt("A", 2)
    log.code("A")
    log.finish("A", 2, "passed")
    log.result("A", "approve")
    log.begin("B")
    log.begin("C")
    log.start_attempt("C", 1)
    log.add("agent.stopped", task="C", attempt=1, then="hold")
    log.add("task_resumed", task="C")
    log.code("C")
    log.finish("C", 1, "passed")
    log.result("C", "reject")
    log.escalate("C", reason="rejected")
    return log


def test_ac2_folding_the_same_events_twice_gives_identical_state() -> None:
    log = _busy_log()
    first = fold(log.events)
    second = fold(log.events)
    assert first == second
    assert first.requirement is second.requirement
    assert states(first) == states(second)
    assert states(first) == {
        "A": T.APPROVED,
        "B": T.PLAN_APPROVED,
        "C": T.ESCALATED,
        "D": T.BLOCKED,
    }
    assert first.requirement is R.IN_PROGRESS


def test_ac2_fold_accepts_any_iterable_and_leaves_the_events_alone() -> None:
    log = _busy_log()
    before = list(log.events)
    from_list = fold(log.events)
    from_tuple = fold(tuple(log.events))
    from_generator = fold(e for e in log.events)
    assert from_list == from_tuple == from_generator
    assert log.events == before


def test_ac2_a_fold_does_not_leak_state_into_the_next() -> None:
    log = _busy_log()
    first = fold(log.events)
    first.tasks.clear()  # vandalise the returned snapshot
    first.tasks["ZZ"] = None  # type: ignore[assignment]
    again = fold(log.events)
    assert "ZZ" not in again.tasks
    assert len(again.tasks) == 4
    # a different requirement folded in between changes nothing
    other = Log("R-20261111-0000")
    other.to_in_progress({"X": {}})
    assert states(fold(other.events)) == {"X": T.PENDING}
    assert fold(log.events) == again


def test_ac2_prefix_stability() -> None:
    """The state after k events is the same however many events follow, and every prefix replays identically."""
    log = _busy_log()
    full = log.events
    prefix_snapshots = [fold(full[:k]) for k in range(len(full) + 1)]
    for k in range(len(full) + 1):
        assert fold(full[:k]) == prefix_snapshots[k]
    # folding the prefix of a longer list is the same as folding exactly that prefix
    longer = Log()
    longer.events = list(full)
    longer.add("agent_call", role="spec")  # an ignored event on the end
    for k in range(len(full) + 1):
        assert fold(longer.events[:k]) == prefix_snapshots[k]
    assert fold(longer.events) == prefix_snapshots[-1]
    assert prefix_snapshots[0] == Snapshot(R.SUBMITTED, {})


def test_ac2_state_ignores_times_and_extra_events() -> None:
    log = _busy_log()
    shifted = Log()
    for e in log.events:
        shifted.events.append(
            Event(
                schema=e.schema,
                seq=e.seq,
                time="2030-01-01T00:00:00Z",
                actor=e.actor,
                type=e.type,
                requirement=e.requirement,
                task=e.task,
                attempt=e.attempt,
                data=e.data,
                ultron_version="9.9.9",
            )
        )
    assert fold(shifted.events) == fold(log.events)


def test_ac2_ignored_event_types_change_nothing() -> None:
    log = Log()
    log.add("intake", text="hello")
    assert fold(log.events) == Snapshot(R.SUBMITTED, {})
    log.add("intake_started")
    log.add("config_snapshot")
    log.add("agent_call", role="intake")
    log.add("intent_drafted")
    assert fold(log.events).requirement is R.AWAITING_INTENT
    log.gate("merge", "approve", task=None)
    log.gate("deploy", "approve", task=None)
    assert fold(log.events).requirement is R.AWAITING_INTENT


def test_ac2_events_of_two_requirements_are_refused() -> None:
    a, b = Log(), Log("R-20261111-ffff")
    a.add("intake_started")
    b.add("intake_started")
    with pytest.raises(ValueError):
        fold([a.events[0], b.events[0]])


def test_ac2_empty_log_is_a_submitted_requirement() -> None:
    assert fold([]) == Snapshot(R.SUBMITTED, {})


# --- AC3: rejected and failed lead to escalated; dependents of an escalated task are blocked -------------------


def test_ac3_rejected_leads_to_escalated() -> None:
    log, spec = one()
    log.to_in_progress(spec)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "passed")
    log.result("T1", "reject")
    assert fold(log.events).tasks["T1"].state is T.REJECTED
    log.escalate("T1", reason="rejected at the result gate")
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.ESCALATED
    assert snap.requirement is R.ESCALATED  # no task approved


def test_ac3_failed_leads_to_escalated() -> None:
    log, spec = one(max_attempts=2)
    log.to_in_progress(spec)
    log.begin("T1")
    log.fail_all_attempts("T1", 2)
    assert fold(log.events).tasks["T1"].state is T.FAILED
    log.escalate("T1")
    assert fold(log.events).tasks["T1"].state is T.ESCALATED


def test_ac3_a_repeated_escalation_record_is_harmless() -> None:
    """A later `escalation` event can carry the Jira key (spec §12); the state stays escalated."""
    log, spec = one(max_attempts=1)
    log.to_in_progress(spec)
    log.begin("T1")
    log.fail_all_attempts("T1", 1)
    log.escalate("T1")
    log.escalate("T1", jira="AD-999")
    assert fold(log.events).tasks["T1"].state is T.ESCALATED


def test_ac3_failed_and_rejected_are_not_yet_escalated_until_recorded() -> None:
    log, spec = one(max_attempts=1)
    log.to_in_progress(spec)
    log.begin("T1")
    log.fail_all_attempts("T1", 1)
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.FAILED
    assert snap.requirement is R.IN_PROGRESS  # failed is unfinished until escalated


def test_ac3_dependents_of_an_escalated_task_become_blocked() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}, "C": {}})
    log.begin("A")
    log.add("escalation", task="A")
    snap = fold(log.events)
    assert states(snap) == {"A": T.ESCALATED, "B": T.BLOCKED, "C": T.PENDING}
    assert snap.requirement is R.IN_PROGRESS  # C is unfinished


def test_ac3_blocked_applies_the_moment_the_dependency_escalates() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}})
    log.begin("A")
    before = fold(log.events)
    assert before.tasks["B"].state is T.PENDING
    log.escalate("A")
    assert fold(log.events).tasks["B"].state is T.BLOCKED


def test_ac3_blocking_is_transitive() -> None:
    log = Log()
    log.to_in_progress(
        {
            "A": {},
            "B": {"depends_on": ["A"]},
            "C": {"depends_on": ["B"]},
            "D": {"depends_on": ["B", "C"]},
        }
    )
    log.begin("A")
    log.escalate("A")
    snap = fold(log.events)
    assert states(snap) == {
        "A": T.ESCALATED,
        "B": T.BLOCKED,
        "C": T.BLOCKED,
        "D": T.BLOCKED,
    }
    assert snap.requirement is R.ESCALATED


def test_ac3_one_escalated_dependency_blocks_even_if_the_other_is_approved() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}, "C": {"depends_on": ["A", "B"]}})
    log.approve_task("A")
    log.begin("B")
    log.escalate("B")
    snap = fold(log.events)
    assert states(snap) == {"A": T.APPROVED, "B": T.ESCALATED, "C": T.BLOCKED}
    assert snap.requirement is R.PARTIAL


def test_ac3_independent_task_is_not_blocked() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}, "C": {"depends_on": ["B"]}})
    log.begin("A")
    log.escalate("A")
    snap = fold(log.events)
    assert snap.tasks["B"].state is T.PENDING
    assert snap.tasks["C"].state is T.PENDING


def test_ac3_a_blocked_task_cannot_be_started() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}})
    log.begin("A")
    log.escalate("A")
    log.add("task_started", task="B")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ac3_escalation_of_an_approved_task_is_illegal() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.approve_task("A")
    log.escalate("A")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ac3_dependency_fully_escalated_after_a_failed_attempt_run() -> None:
    log = Log()
    log.to_in_progress({"A": {"max_attempts": 1}, "B": {"depends_on": ["A"]}})
    log.begin("A")
    log.fail_all_attempts("A", 1)
    assert fold(log.events).tasks["B"].state is T.PENDING  # A is only failed
    log.escalate("A")
    assert fold(log.events).tasks["B"].state is T.BLOCKED


# agent.stopped (§9.1)


def _coding_log(max_attempts: int | None = None) -> Log:
    log = _in_progress_one(max_attempts)
    log.begin("T1")
    log.start_attempt("T1", 1)
    return log


def test_ac3_stop_then_hold_pauses_without_counting_an_attempt() -> None:
    log = _coding_log()
    log.add("agent.stopped", task="T1", attempt=1, then="hold", reason="no progress")
    held = fold(log.events).tasks["T1"]
    assert held.state is T.HELD
    assert held.attempt == 1  # attempt 1 was started, the stop is not an extra attempt
    log.add("task_resumed", task="T1")
    resumed = fold(log.events).tasks["T1"]
    assert resumed.state is T.CODING
    assert resumed.attempt == 1


def test_ac3_stop_default_is_hold() -> None:
    """§9.1: `hold` is the default."""
    log = _coding_log()
    log.add("agent.stopped", task="T1", attempt=1)
    assert fold(log.events).tasks["T1"].state is T.HELD


def test_ac3_held_task_can_be_escalated_but_not_driven_on() -> None:
    log = _coding_log()
    log.add("agent.stopped", task="T1", attempt=1, then="hold")
    log.code("T1")  # a held task cannot move until it is resumed
    with pytest.raises(IllegalTransition):
        fold(log.events)
    log2 = _coding_log()
    log2.add("agent.stopped", task="T1", attempt=1, then="hold")
    log2.escalate("T1")
    assert fold(log2.events).tasks["T1"].state is T.ESCALATED


def test_ac3_stop_then_escalate_goes_to_escalated() -> None:
    log = _coding_log()
    log.add("agent.stopped", task="T1", attempt=1, then="escalate")
    assert fold(log.events).tasks["T1"].state is T.ESCALATED


def test_ac3_stop_then_retry_counts_as_a_failed_attempt() -> None:
    log = _coding_log(max_attempts=3)
    log.add("agent.stopped", task="T1", attempt=1, then="retry")
    snap = fold(log.events).tasks["T1"]
    assert snap.state is T.PLANNING  # next attempt needs the plan gate again (FR9)
    assert snap.attempt == 1
    log.replan("T1")
    log.start_attempt("T1", 2)
    assert fold(log.events).tasks["T1"].attempt == 2


def test_ac3_stop_then_retry_with_no_attempts_left_ends_the_task() -> None:
    """§9.1: no attempts left means escalated; §3 puts `failed` before `escalated`. Either end state is accepted
    here, and it must not be able to start another attempt."""
    log = _coding_log(max_attempts=1)
    log.add("agent.stopped", task="T1", attempt=1, then="retry")
    state = fold(log.events).tasks["T1"].state
    assert state in (T.FAILED, T.ESCALATED)
    log.replan("T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ac3_stop_while_testing_and_planning_can_hold() -> None:
    log = _in_progress_one()
    log.add("task_started", task="T1")
    log.add("agent.stopped", task="T1", then="hold")  # developer drafting the plan
    assert fold(log.events).tasks["T1"].state is T.HELD
    log.add("task_resumed", task="T1")
    assert fold(log.events).tasks["T1"].state is T.PLANNING

    log = _coding_log()
    log.code("T1")
    log.add("agent.stopped", task="T1", then="hold")  # testing agent
    assert fold(log.events).tasks["T1"].state is T.HELD
    log.add("task_resumed", task="T1")
    assert fold(log.events).tasks["T1"].state is T.TESTING


def test_ac3_resume_when_not_held_is_illegal() -> None:
    log = _coding_log()
    log.add("task_resumed", task="T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize(
    "where",
    [
        "pending",
        "planning",
        "awaiting_plan",
        "plan_approved",
        "coding",
        "testing",
        "passed",
        "awaiting_result",
        "held",
        "rejected",
        "failed",
    ],
)
def test_ac3_an_unfinished_task_can_be_escalated_from_every_state(where: str) -> None:
    """FR18: when a requirement hits its cap, its unfinished tasks are escalated."""
    log = _in_progress_one(max_attempts=1)
    if where != "pending":
        log.add("task_started", task="T1")
    if where in {
        "awaiting_plan",
        "plan_approved",
        "coding",
        "testing",
        "passed",
        "awaiting_result",
        "held",
        "rejected",
        "failed",
    }:
        log.add("plan_drafted", task="T1")
    if where in {
        "plan_approved",
        "coding",
        "testing",
        "passed",
        "awaiting_result",
        "held",
        "rejected",
        "failed",
    }:
        log.gate("plan", "approve", task="T1")
    if where in {
        "coding",
        "testing",
        "passed",
        "awaiting_result",
        "held",
        "rejected",
        "failed",
    }:
        log.start_attempt("T1", 1)
    if where in {"testing", "passed", "awaiting_result", "rejected"}:
        log.code("T1")
    if where in {"passed", "awaiting_result", "rejected"}:
        log.finish("T1", 1, "passed")
    if where in {"awaiting_result", "rejected"}:
        log.add("result_requested", task="T1")
    if where == "rejected":
        log.gate("result", "reject", task="T1")
    if where == "held":
        log.add("agent.stopped", task="T1", then="hold")
    if where == "failed":
        log.code("T1")
        log.finish("T1", 1, "failed")
    expected = {"held": T.HELD, "rejected": T.REJECTED, "failed": T.FAILED}.get(
        where, T(where)
    )
    assert fold(log.events).tasks["T1"].state is expected
    log.escalate("T1", reason="spending cap")
    assert fold(log.events).tasks["T1"].state is T.ESCALATED


# --- AC4: requirement outcomes ---------------------------------------------------------------------------------


def test_ac4_complete_when_all_tasks_are_approved() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}, "C": {}})
    log.approve_task("A")
    assert fold(log.events).requirement is R.IN_PROGRESS
    log.approve_task("B")
    assert fold(log.events).requirement is R.IN_PROGRESS  # C still unfinished
    log.approve_task("C")
    snap = fold(log.events)
    assert snap.requirement is R.COMPLETE
    assert set(states(snap).values()) == {T.APPROVED}


def test_ac4_complete_with_a_single_task() -> None:
    log = Log()
    log.to_in_progress({"T1": {}})
    log.approve_task("T1")
    assert fold(log.events).requirement is R.COMPLETE


def test_ac4_partial_when_some_approved_and_some_escalated() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.approve_task("A")
    log.begin("B")
    log.escalate("B")
    snap = fold(log.events)
    assert states(snap) == {"A": T.APPROVED, "B": T.ESCALATED}
    assert snap.requirement is R.PARTIAL


def test_ac4_partial_when_some_approved_and_some_blocked() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}, "C": {"depends_on": ["B"]}})
    log.begin("B")
    log.escalate("B")
    assert fold(log.events).requirement is R.IN_PROGRESS  # A not done yet
    log.approve_task("A")
    snap = fold(log.events)
    assert states(snap) == {"A": T.APPROVED, "B": T.ESCALATED, "C": T.BLOCKED}
    assert snap.requirement is R.PARTIAL


def test_ac4_escalated_when_no_task_is_approved() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.begin("A")
    log.escalate("A")
    assert fold(log.events).requirement is R.IN_PROGRESS  # B still unfinished
    log.begin("B")
    log.escalate("B")
    snap = fold(log.events)
    assert states(snap) == {"A": T.ESCALATED, "B": T.ESCALATED}
    assert snap.requirement is R.ESCALATED


def test_ac4_escalated_when_the_only_other_tasks_are_blocked() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}})
    log.begin("A")
    log.escalate("A")
    snap = fold(log.events)
    assert states(snap) == {"A": T.ESCALATED, "B": T.BLOCKED}
    assert snap.requirement is R.ESCALATED


def test_ac4_requirement_stays_in_progress_while_any_task_is_unfinished() -> None:
    for unfinished in (
        "pending",
        "planning",
        "awaiting_plan",
        "coding",
        "awaiting_result",
        "rejected",
        "failed",
        "held",
    ):
        log = Log()
        log.to_in_progress({"A": {}, "B": {"max_attempts": 1}})
        log.approve_task("A")
        if unfinished != "pending":
            log.add("task_started", task="B")
        if unfinished == "awaiting_plan":
            log.add("plan_drafted", task="B")
        if unfinished in ("coding", "awaiting_result", "rejected", "failed", "held"):
            log.add("plan_drafted", task="B")
            log.gate("plan", "approve", task="B")
            log.start_attempt("B", 1)
        if unfinished == "held":
            log.add("agent.stopped", task="B", then="hold")
        if unfinished in ("awaiting_result", "rejected"):
            log.code("B")
            log.finish("B", 1, "passed")
            log.add("result_requested", task="B")
        if unfinished == "rejected":
            log.gate("result", "reject", task="B")
        if unfinished == "failed":
            log.code("B")
            log.finish("B", 1, "failed")
        snap = fold(log.events)
        assert snap.requirement is R.IN_PROGRESS, unfinished
        assert snap.tasks["B"].state.value == unfinished


def test_ac4_outcome_is_independent_of_task_order() -> None:
    a = Log()
    a.to_in_progress({"A": {}, "B": {}})
    a.approve_task("A")
    a.begin("B")
    a.escalate("B")
    b = Log()
    b.to_in_progress({"A": {}, "B": {}})
    b.begin("B")
    b.escalate("B")
    b.approve_task("A")
    assert fold(a.events).requirement is fold(b.events).requirement is R.PARTIAL


def test_ac4_a_finished_requirement_takes_no_new_task_work() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.approve_task("A")
    log.begin("B")
    log.escalate("B")
    assert fold(log.events).requirement is R.PARTIAL
    log.add("plan_drafted", task="B")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ac4_escalation_record_with_the_jira_ticket_after_the_requirement_settled() -> (
    None
):
    """T05's `escalation` event carries `ticket` ("the Jira key once filed", spec §12: placement is recorded in the
    log). The ticket is filed after the task escalates, which is often after the requirement has settled, and the
    log must still replay."""
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.approve_task("A")
    log.begin("B")
    log.escalate("B", reason="rejected", ticket=None)
    assert fold(log.events).requirement is R.PARTIAL
    log.escalate("B", reason="rejected", ticket="AD-1234")
    snap = fold(log.events)
    assert snap.requirement is R.PARTIAL
    assert snap.tasks["B"].state is T.ESCALATED


# --- FR9 / FR10: attempts -------------------------------------------------------------------------------------


def test_fr9_default_max_attempts_is_three_and_the_third_failure_fails_the_task() -> (
    None
):
    log = _in_progress_one()
    log.begin("T1")
    assert fold(log.events).tasks["T1"].max_attempts == 3
    expected_attempt_after = [(1, T.PLANNING), (2, T.PLANNING), (3, T.FAILED)]
    for n, expected_state in expected_attempt_after:
        if n > 1:
            log.replan("T1")
        log.start_attempt("T1", n)
        assert fold(log.events).tasks["T1"].attempt == n
        assert fold(log.events).tasks["T1"].state is T.CODING
        log.code("T1")
        log.finish("T1", n, "failed")
        status = fold(log.events).tasks["T1"]
        assert status.state is expected_state, n
        assert status.attempt == n


@pytest.mark.parametrize("max_attempts", [1, 2, 5, 10])
def test_fr10_max_attempts_is_respected(max_attempts: int) -> None:
    log = _in_progress_one(max_attempts)
    log.begin("T1")
    for n in range(1, max_attempts):
        log.start_attempt("T1", n) if n == 1 else (
            log.replan("T1"),
            log.start_attempt("T1", n),
        )
        log.code("T1")
        log.finish("T1", n, "failed")
        assert fold(log.events).tasks["T1"].state is T.PLANNING, n
    if max_attempts > 1:
        log.replan("T1")
    log.start_attempt("T1", max_attempts)
    log.code("T1")
    log.finish("T1", max_attempts, "failed")
    status = fold(log.events).tasks["T1"]
    assert status.state is T.FAILED
    assert status.attempt == max_attempts
    # no attempt max_attempts + 1
    log.replan("T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_fr10_a_failed_coding_step_counts_as_a_failed_attempt() -> None:
    """FR11: a developer diff touching the testing agent's tests fails the attempt, before testing runs."""
    log = _in_progress_one(2)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.finish("T1", 1, "failed")  # still coding
    assert fold(log.events).tasks["T1"].state is T.PLANNING
    log.replan("T1")
    log.start_attempt("T1", 2)
    log.finish("T1", 2, "failed")
    assert fold(log.events).tasks["T1"].state is T.FAILED


@pytest.mark.parametrize("bad", [0, 11, -1])
def test_fr10_max_attempts_range_is_one_to_ten(bad: int) -> None:
    log = Log()
    log.to_decomposing()
    log.add("task_created", task="T1", max_attempts=bad)
    with pytest.raises(ValueError):
        fold(log.events)


def test_fr10_max_attempts_is_fixed_per_task() -> None:
    log = Log()
    log.to_in_progress({"A": {"max_attempts": 1}, "B": {"max_attempts": 3}})
    for name in ("A", "B"):
        log.begin(name)
        log.start_attempt(name, 1)
        log.code(name)
        log.finish(name, 1, "failed")
    snap = fold(log.events)
    assert snap.tasks["A"].state is T.FAILED
    assert snap.tasks["B"].state is T.PLANNING
    assert (snap.tasks["A"].max_attempts, snap.tasks["B"].max_attempts) == (1, 3)


def test_fr10_waiting_at_a_gate_is_not_an_attempt() -> None:
    log = _in_progress_one(3)
    log.add("task_started", task="T1")
    for _ in range(6):  # the plan gate asks for changes again and again
        log.add("plan_drafted", task="T1")
        assert fold(log.events).tasks["T1"].state is T.AWAITING_PLAN
        log.gate("plan", "changes", task="T1")
        assert fold(log.events).tasks["T1"].attempt == 0
    log.add("plan_drafted", task="T1")
    log.gate("plan", "approve", task="T1")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.PLAN_APPROVED, 0)
    # three real attempts are still available
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "failed")
    for _ in range(4):  # more waiting at the plan gate between attempts
        log.add("plan_drafted", task="T1")
        log.gate("plan", "changes", task="T1")
        status = fold(log.events).tasks["T1"]
        assert (status.state, status.attempt) == (T.PLANNING, 1)
    log.replan("T1")
    log.start_attempt("T1", 2)
    assert fold(log.events).tasks["T1"].attempt == 2
    log.code("T1")
    log.finish("T1", 2, "failed")
    log.replan("T1")
    log.start_attempt("T1", 3)
    assert fold(log.events).tasks["T1"].state is T.CODING


def test_fr10_waiting_at_the_result_gate_is_not_an_attempt() -> None:
    log = _in_progress_one(1)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "passed")
    log.add("result_requested", task="T1")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.AWAITING_RESULT, 1)
    log.gate("result", "approve", task="T1")
    assert fold(log.events).tasks["T1"].state is T.APPROVED


def test_fr9_attempt_count_is_kept_through_a_revised_plan() -> None:
    log = _in_progress_one(3)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "failed")
    log.add("plan_drafted", task="T1")
    assert fold(log.events).tasks["T1"].attempt == 1
    log.gate("plan", "approve", task="T1")
    assert fold(log.events).tasks["T1"].attempt == 1
    log.start_attempt("T1", 2)
    assert fold(log.events).tasks["T1"].attempt == 2


def test_fr9_a_revised_plan_must_pass_the_plan_gate_again() -> None:
    log = _in_progress_one(3)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "failed")
    log.start_attempt("T1", 2)  # straight back to coding without a new approved plan
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_fr9_a_passing_attempt_after_failures_leads_to_the_result_gate() -> None:
    log = _in_progress_one(3)
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "failed")
    log.replan("T1")
    log.start_attempt("T1", 2)
    log.code("T1")
    log.finish("T1", 2, "passed")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.PASSED, 2)
    log.result("T1", "approve")
    assert fold(log.events).requirement is R.COMPLETE


def test_fr9_attempt_finished_needs_a_known_outcome() -> None:
    log = _in_progress_one()
    log.begin("T1")
    log.start_attempt("T1", 1)
    log.code("T1")
    log.finish("T1", 1, "maybe")
    with pytest.raises(IllegalTransition):
        fold(log.events)


# --- AC5: requirement ids ------------------------------------------------------------------------------------

ID_RE = re.compile(r"R-[0-9]{8}-[0-9a-f]{4}")
AC_RE = re.compile(r"^R-\d{8}-[0-9a-f]{4}$")


def test_ac5_ids_match_the_pattern(tmp_path: Path) -> None:
    for _ in range(300):
        rid = ids.new_requirement_id(tmp_path)
        assert AC_RE.match(rid), rid
        assert ID_RE.fullmatch(rid), rid
        assert rid.isascii() and "\n" not in rid
        assert len(rid) == len("R-20261109-a3f2")
        (tmp_path / rid).mkdir()


def test_ac5_the_spec_example_has_the_same_shape() -> None:
    assert ID_RE.fullmatch("R-20261109-a3f2")
    day = ids.new_requirement_id(Path("."), now=datetime(2026, 11, 9)).split("-")[1]
    assert day == "20261109"


def test_ac5_date_is_the_local_date(tmp_path: Path) -> None:
    before = datetime.now().strftime("%Y%m%d")
    rid = ids.new_requirement_id(tmp_path)
    after = datetime.now().strftime("%Y%m%d")
    assert rid.split("-")[1] in {before, after}


@pytest.mark.parametrize(
    ("moment", "day"),
    [
        (datetime(2026, 1, 2, 0, 0, 1), "20260102"),
        (datetime(2026, 12, 31, 23, 59, 59), "20261231"),
        (datetime(2028, 2, 29, 12, 0), "20280229"),
        (datetime(2026, 11, 9, 7, 5, 3), "20261109"),
    ],
)
def test_ac5_date_follows_the_given_moment_zero_padded(
    tmp_path: Path, moment: datetime, day: str
) -> None:
    assert ids.new_requirement_id(tmp_path, now=moment).startswith(f"R-{day}-")


def test_ac5_the_default_date_is_local_not_utc() -> None:
    """Run the generator under two fixed time zones 26 hours apart: their local dates differ, and each id carries the
    date of its own zone. (`TZ` in POSIX form: `UTC-14` is UTC+14.)"""
    code = (
        "import sys, tempfile, time\n"
        "from pathlib import Path\n"
        "from datetime import datetime\n"
        "from ultron.engine.ids import new_requirement_id\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        "    rid = new_requirement_id(Path(d))\n"
        "print(rid.split('-')[1], datetime.now().strftime('%Y%m%d'), time.timezone)\n"
    )
    out = {}
    for tz in ("UTC-14", "UTC+12"):
        env = {**__import__("os").environ, "TZ": tz}
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        out[tz] = result.stdout.split()
    for tz, (rid_day, local_day, _offset) in out.items():
        assert rid_day == local_day, (tz, out)
    # Both zones' wall clocks must really be 26 hours apart, otherwise this platform ignores TZ and the check above
    # only proved "id equals datetime.now()", which the previous test already proves.
    if out["UTC-14"][2] != out["UTC+12"][2]:
        east = datetime.now(timezone(timedelta(hours=14))).strftime("%Y%m%d")
        west = datetime.now(timezone(timedelta(hours=-12))).strftime("%Y%m%d")
        assert out["UTC-14"][0] == east
        assert out["UTC+12"][0] == west
        assert east != west


def test_ac5_forced_collision_gives_a_different_id(tmp_path: Path) -> None:
    day = datetime(2026, 11, 9)
    (tmp_path / "R-20261109-a3f2").mkdir()
    tokens = iter(["a3f2", "a3f2", "9e01"])
    rid = ids.new_requirement_id(tmp_path, now=day, token=lambda: next(tokens))
    assert rid == "R-20261109-9e01"
    assert rid != "R-20261109-a3f2"


def test_ac5_forced_collision_through_the_random_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the default random source to repeat a taken id, without using the `token` parameter."""
    draws = iter(["abcd", "abcd", "ef01"])
    monkeypatch.setattr(ids.secrets, "token_hex", lambda nbytes=None: next(draws))
    (tmp_path / "R-20261109-abcd").mkdir()
    rid = ids.new_requirement_id(tmp_path, now=datetime(2026, 11, 9))
    assert rid == "R-20261109-ef01"


def test_ac5_two_ids_in_a_row_in_the_same_folder_differ(tmp_path: Path) -> None:
    seen: set[str] = set()
    for _ in range(500):
        rid = ids.new_requirement_id(tmp_path, now=datetime(2026, 11, 9))
        assert rid not in seen
        seen.add(rid)
        (tmp_path / rid).mkdir()
    assert len(seen) == 500


def test_ac5_collisions_are_checked_against_the_other_days_too(tmp_path: Path) -> None:
    """The same suffix on another day is a different id and is fine; only the full id counts."""
    (tmp_path / "R-20261108-a3f2").mkdir()
    rid = ids.new_requirement_id(
        tmp_path, now=datetime(2026, 11, 9), token=lambda: "a3f2"
    )
    assert rid == "R-20261109-a3f2"


def test_ac5_a_full_day_of_taken_ids_raises_rather_than_repeating(
    tmp_path: Path,
) -> None:
    (tmp_path / "R-20261109-0001").mkdir()
    with pytest.raises(RuntimeError):
        ids.new_requirement_id(
            tmp_path, now=datetime(2026, 11, 9), token=lambda: "0001", tries=5
        )


def test_ac5_a_missing_requirements_folder_is_not_an_error(tmp_path: Path) -> None:
    rid = ids.new_requirement_id(tmp_path / "sdlc" / "requirements")
    assert ID_RE.fullmatch(rid)


@pytest.mark.parametrize("bad", ["ABCD", "abc", "abcde", "abcg", "٠٠٠٠", "", "a b!"])
def test_ac5_a_token_that_is_not_four_lowercase_hex_digits_is_refused(
    tmp_path: Path, bad: str
) -> None:
    with pytest.raises(ValueError):
        ids.new_requirement_id(tmp_path, token=lambda: bad)


# --- attempt 2: the Jira-ticket escalation record after the requirement settled (spec §12) --------------------
# The fix: an `escalation` record for a task that is already `escalated` is a no-op whatever the requirement's
# state. A first escalation on a settled requirement (approved, blocked or unknown task) is still illegal, and no
# other task event becomes legal after settling.


def _settled(outcome: str) -> tuple[Log, dict[str, TaskState]]:
    """A requirement that has just settled as `outcome`, and the state of each of its tasks."""
    log = Log()
    if outcome == "complete":
        log.to_in_progress({"A": {}, "B": {"depends_on": ["A"]}})
        log.approve_task("A")
        log.approve_task("B")
        want = {"A": T.APPROVED, "B": T.APPROVED}
    elif outcome == "partial":
        log.to_in_progress(
            {"A": {}, "B": {"max_attempts": 2}, "C": {"depends_on": ["B"]}}
        )
        log.approve_task("A")
        log.begin("B")
        log.fail_all_attempts("B", 2)
        log.escalate("B", reason="all attempts failed", ticket=None)
        want = {"A": T.APPROVED, "B": T.ESCALATED, "C": T.BLOCKED}
    elif outcome == "escalated":
        log.to_in_progress(
            {"A": {"max_attempts": 1}, "B": {"depends_on": ["A"]}, "C": {}}
        )
        log.begin("C")
        log.start_attempt("C", 1)
        log.code("C")
        log.finish("C", 1, "passed")
        log.result("C", "reject")
        log.escalate("C", reason="rejected", ticket=None)
        log.begin("A")
        log.fail_all_attempts("A", 1)
        log.escalate("A", reason="all attempts failed", ticket=None)
        want = {"A": T.ESCALATED, "B": T.BLOCKED, "C": T.ESCALATED}
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(outcome)
    snap = fold(log.events)
    assert snap.requirement is R(outcome)
    assert states(snap) == want
    return log, want


SETTLED_OUTCOMES = ("complete", "partial", "escalated")


@pytest.mark.parametrize(
    ("outcome", "repeats"),
    [(o, n) for o in ("partial", "escalated") for n in (1, 2, 5)],
)
def test_a2_ticket_record_after_settling_is_a_no_op(outcome: str, repeats: int) -> None:
    """The ticket (and its sprint placement, and retried filings) arrives after the requirement settled: the log
    must replay, and the snapshot is exactly what it was before the records."""
    log, _ = _settled(outcome)
    before = fold(log.events)
    escalated = [name for name, s in before.tasks.items() if s.state is T.ESCALATED]
    assert escalated
    for i in range(repeats):
        for name in escalated:
            log.escalate(
                name,
                reason="all attempts failed",
                ticket=None if i == 0 and repeats > 1 else f"AD-{1000 + i}",
                sprint="active" if i % 2 else "backlog",
            )
    after = fold(log.events)
    assert after == before
    assert after.requirement is R(outcome)


def test_a2_ticket_record_for_each_escalated_task_in_any_order() -> None:
    log, _ = _settled("escalated")
    before = fold(log.events)
    for name in ("A", "C", "A", "C", "C"):
        log.escalate(name, reason="filed", ticket=f"AD-{name}")
        assert fold(log.events) == before


def test_a2_ticket_record_with_a_task_level_attempt_field_is_a_no_op() -> None:
    """The real `escalation(...)` builder can carry `attempt`; the repeat is still a no-op."""
    log, _ = _settled("partial")
    before = fold(log.events)
    log.add(
        "escalation", task="B", attempt=2, reason="all attempts failed", ticket="AD-7"
    )
    log.add("escalation", task="B", attempt=1, reason="pending", ticket=None)
    assert fold(log.events) == before


def test_a2_ticket_record_interleaved_with_other_tasks_events() -> None:
    """A escalates while B and C still run; ticket records for A land between their events; nothing of B or C
    changes because of them, and the requirement settles as partial only when B and C finish."""
    log = Log()
    log.to_in_progress(
        {
            "A": {"max_attempts": 1},
            "B": {},
            "C": {"max_attempts": 2},
            "D": {"depends_on": ["A"]},
        }
    )
    log.begin("A")
    log.fail_all_attempts("A", 1)
    log.escalate("A", reason="all attempts failed", ticket=None)
    log.add("task_started", task="B")
    log.escalate(
        "A", reason="all attempts failed", ticket=None
    )  # filing pending, retried
    log.add("plan_drafted", task="B")
    log.begin("C")
    log.escalate("A", reason="all attempts failed", ticket="AD-500")
    log.gate("plan", "approve", task="B")
    log.start_attempt("B", 1)
    log.start_attempt("C", 1)
    log.add("agent.stopped", task="C", attempt=1, then="hold")
    log.escalate("A", reason="all attempts failed", ticket="AD-500", sprint="active")
    snap = fold(log.events)
    assert snap.requirement is R.IN_PROGRESS
    assert states(snap) == {
        "A": T.ESCALATED,
        "B": T.CODING,
        "C": T.HELD,
        "D": T.BLOCKED,
    }
    assert (snap.tasks["B"].attempt, snap.tasks["C"].attempt) == (1, 1)
    assert snap.tasks["C"].held_from is T.CODING
    log.add("task_resumed", task="C")
    log.code("B")
    log.escalate("A", reason="all attempts failed", ticket="AD-500")
    log.code("C")
    log.finish("B", 1, "passed")
    log.finish("C", 1, "failed")
    log.escalate("A", reason="all attempts failed", ticket="AD-500")
    log.result("B", "approve")
    assert fold(log.events).requirement is R.IN_PROGRESS  # C is planning again
    log.replan("C")
    log.start_attempt("C", 2)
    log.code("C")
    log.finish("C", 2, "failed")
    log.escalate("C", reason="all attempts failed", ticket=None)
    snap = fold(log.events)
    assert snap.requirement is R.PARTIAL
    assert states(snap) == {
        "A": T.ESCALATED,
        "B": T.APPROVED,
        "C": T.ESCALATED,
        "D": T.BLOCKED,
    }
    # both tickets arrive after settling, interleaved
    log.escalate("C", reason="all attempts failed", ticket="AD-501")
    log.escalate("A", reason="all attempts failed", ticket="AD-500")
    log.escalate("C", reason="all attempts failed", ticket="AD-501", sprint="next")
    assert fold(log.events) == snap


def test_a2_interleaved_ticket_records_equal_the_log_without_them() -> None:
    """Dropping every repeated escalation record from a log gives the same state at every point."""
    log = Log()
    log.to_in_progress({"A": {"max_attempts": 1}, "B": {}})
    log.begin("A")
    log.fail_all_attempts("A", 1)
    log.escalate("A", reason="failed", ticket=None)
    repeat_seqs: set[int] = set()
    log.add("task_started", task="B")
    repeat_seqs.add(log.add("escalation", task="A", reason="failed", ticket=None).seq)
    for step in (
        lambda: log.add("plan_drafted", task="B"),
        lambda: log.gate("plan", "approve", task="B"),
        lambda: log.start_attempt("B", 1),
        lambda: log.code("B"),
        lambda: log.finish("B", 1, "passed"),
        lambda: log.result("B", "approve"),
    ):
        step()
        repeat_seqs.add(
            log.add("escalation", task="A", reason="failed", ticket="AD-9").seq
        )
    with_repeats = log.events
    without = [e for e in with_repeats if e.seq not in repeat_seqs]
    assert fold(with_repeats) == fold(without)
    assert fold(with_repeats).requirement is R.PARTIAL
    for k in range(len(with_repeats) + 1):
        prefix = with_repeats[:k]
        assert fold(prefix) == fold([e for e in prefix if e.seq not in repeat_seqs])


@pytest.mark.parametrize("outcome", SETTLED_OUTCOMES)
def test_a2_first_escalation_after_settling_is_still_illegal(outcome: str) -> None:
    """Every task that is not escalated (approved or blocked), and an unknown task, cannot be escalated once the
    requirement has settled."""
    _, want = _settled(outcome)
    targets = [name for name, s in want.items() if s is not T.ESCALATED] + ["NOPE"]
    assert targets
    for name in targets:
        log, _ = _settled(outcome)
        log.escalate(name, reason="spending cap", ticket=None)
        with pytest.raises(IllegalTransition):
            fold(log.events)


def test_a2_first_escalation_of_a_blocked_task_after_a_ticket_record_is_still_illegal() -> (
    None
):
    log, _ = _settled("partial")
    log.escalate("B", reason="filed", ticket="AD-1")  # fine: B is escalated
    assert fold(log.events).requirement is R.PARTIAL
    log.escalate("C", reason="blocked", ticket=None)  # C is blocked, not escalated
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_a2_escalation_of_an_approved_task_is_illegal_in_progress_too() -> None:
    log = Log()
    log.to_in_progress({"A": {}, "B": {}})
    log.approve_task("A")
    log.escalate("A", reason="x", ticket="AD-1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_a2_requirement_level_escalation_record_after_settling_is_still_a_no_op() -> (
    None
):
    log, _ = _settled("escalated")
    before = fold(log.events)
    log.add("escalation", reason="spending cap", ticket="AD-77")  # no task
    assert fold(log.events) == before


# The sweep through fold: every task event, for every task of every settled requirement. Only the repeated
# `escalation` record of an escalated task is accepted.

TaskEventFn = Any


def _task_events() -> dict[str, TaskEventFn]:
    out: dict[str, TaskEventFn] = {
        "task_created": lambda log, n: log.add("task_created", task=n),
        "task_started": lambda log, n: log.add("task_started", task=n),
        "plan_drafted": lambda log, n: log.add("plan_drafted", task=n),
        "coding_finished": lambda log, n: log.add("coding_finished", task=n),
        "result_requested": lambda log, n: log.add("result_requested", task=n),
        "task_resumed": lambda log, n: log.add("task_resumed", task=n),
        "attempt_started": lambda log, n: log.add("attempt_started", task=n),
        "escalation": lambda log, n: log.add(
            "escalation", task=n, reason="r", ticket="AD-1"
        ),
        "agent.stopped(default)": lambda log, n: log.add("agent.stopped", task=n),
    }
    for then in ("hold", "retry", "escalate"):
        out[f"agent.stopped({then})"] = lambda log, n, t=then: log.add(
            "agent.stopped", task=n, then=t
        )
    for outcome in ("passed", "failed", "stopped"):
        out[f"attempt_finished({outcome})"] = lambda log, n, o=outcome: log.add(
            "attempt_finished", task=n, outcome=o
        )
    for gate in ("plan", "result"):
        for decision in DECISIONS:
            out[f"{gate}.{decision}"] = lambda log, n, g=gate, d=decision: log.gate(
                g, d, task=n
            )
    return out


TASK_EVENTS = _task_events()


@pytest.mark.parametrize("outcome", SETTLED_OUTCOMES)
def test_a2_sweep_every_task_event_after_settling(outcome: str) -> None:
    _, want = _settled(outcome)
    wrongly_allowed = []
    wrongly_refused = []
    for name in [*want, "NOPE"]:
        for label, add in TASK_EVENTS.items():
            log, _ = _settled(outcome)
            before = fold(log.events)
            add(log, name)
            expected_ok = label == "escalation" and want.get(name) is T.ESCALATED
            try:
                after = fold(log.events)
            except IllegalTransition:
                if expected_ok:
                    wrongly_refused.append((name, label))
                continue
            if not expected_ok:
                wrongly_allowed.append((name, want.get(name), label))
            elif after != before:
                wrongly_allowed.append((name, "changed state", label))
    assert not wrongly_refused
    assert not wrongly_allowed


def test_a2_sweep_every_task_event_on_an_escalated_task_in_progress() -> None:
    """While the requirement is still in progress, an escalated task takes only the repeated record (a no-op).

    `attempt_finished` with outcome `stopped` is left out: it is a no-op for every task by design (the
    `agent.stopped` event decides), unchanged by attempt 2."""
    wrongly_allowed = []
    for label, add in TASK_EVENTS.items():
        if label == "attempt_finished(stopped)":
            continue
        log = Log()
        log.to_in_progress({"A": {}, "B": {}})
        log.begin("A")
        log.escalate("A", reason="cap", ticket=None)
        before = fold(log.events)
        assert before.requirement is R.IN_PROGRESS
        add(log, "A")
        try:
            after = fold(log.events)
        except IllegalTransition:
            if label == "escalation":
                wrongly_allowed.append(("refused", label))
            continue
        if label != "escalation" or after != before:
            wrongly_allowed.append(("allowed", label))
    assert not wrongly_allowed


def test_a2_table_sweeps_are_unchanged_by_the_fix() -> None:
    """The fix is in the fold, not the tables: the pure tables still match the hand-written §3 tables exactly."""
    for (state, trigger), want in LEGAL_REQ.items():
        assert next_requirement_state(state, trigger) is want
    for state in RequirementState:
        for trigger in UNIVERSE:
            if (state, trigger) not in LEGAL_REQ:
                with pytest.raises(IllegalTransition):
                    next_requirement_state(state, trigger)
    for state in TaskState:
        for trigger in UNIVERSE:
            if trigger == "task_resumed":
                continue
            if (state, trigger) in LEGAL_TASK:
                assert next_task_state(state, trigger) is LEGAL_TASK[(state, trigger)]
            else:
                # including (escalated, escalation) and (blocked, escalation): the no-op lives in the fold only
                with pytest.raises(IllegalTransition):
                    next_task_state(state, trigger)


def test_a2_new_tasks_cannot_be_created_after_settling() -> None:
    for outcome in SETTLED_OUTCOMES:
        log, _ = _settled(outcome)
        log.add("task_created", task="Z")
        with pytest.raises(IllegalTransition):
            fold(log.events)


def test_a2_a_requirement_gate_after_settling_is_still_illegal() -> None:
    for outcome in SETTLED_OUTCOMES:
        for gate in ("intent", "spec", "tasks"):
            log, _ = _settled(outcome)
            log.gate(gate, "approve")
            with pytest.raises(IllegalTransition):
                fold(log.events)


# A realistic full log written and read back through the real event log (T05), then replayed.


def _realistic_log(tmp_path: Path) -> list[Event]:
    from ultron.store import events as ev
    from ultron.store.eventlog import EventLog, events_path

    req = REQ
    user = ev.user_actor("Tester", "tester@example.com")

    def new(
        type_: str, task: str | None = None, attempt: int | None = None, **data: Any
    ) -> ev.NewEvent:
        return ev.NewEvent(
            actor="engine",
            type=type_,
            requirement=req,
            task=task,
            attempt=attempt,
            data=data,
        )

    def gate(
        gate_: str, decision: str, task: str | None = None, auto: bool = False
    ) -> ev.NewEvent:
        return ev.gate_decision(
            req,
            actor="auto" if auto else user,
            gate=gate_,
            decision=decision,
            channel="auto" if auto else "terminal",
            task=task,
        )

    stream: list[ev.NewEvent] = [
        ev.intake(req, actor=user, source="text", request_path="request.md"),
        new("intake_started"),
        new("intent_drafted"),
        gate("intent", "approve"),
        new("spec_drafted"),
        gate("spec", "approve"),
        new("decomposition_started"),
        new("task_created", task="T1", depends_on=[], max_attempts=2),
        new("task_created", task="T2", depends_on=["T1"]),
        new("task_created", task="T3", depends_on=[]),
        new("tasks_drafted"),
        gate("tasks", "approve", auto=True),
        # T3 is approved
        new("task_started", task="T3"),
        new("plan_drafted", task="T3"),
        gate("plan", "approve", task="T3", auto=True),
        ev.attempt_started(req, "T3", 1, rung="R1"),
        new("coding_finished", task="T3", attempt=1),
        ev.attempt_finished(req, "T3", 1, outcome="passed"),
        new("result_requested", task="T3"),
        gate("result", "approve", task="T3"),
        # T1 fails both attempts, then escalates; T2 is blocked behind it
        new("task_started", task="T1"),
        new("plan_drafted", task="T1"),
        gate("plan", "approve", task="T1"),
        ev.attempt_started(req, "T1", 1, rung="R1"),
        new("coding_finished", task="T1", attempt=1),
        ev.attempt_finished(req, "T1", 1, outcome="failed"),
        new("plan_drafted", task="T1"),
        gate("plan", "changes", task="T1"),
        new("plan_drafted", task="T1"),
        gate("plan", "approve", task="T1"),
        ev.attempt_started(req, "T1", 2, rung="R2"),
        ev.attempt_finished(req, "T1", 2, outcome="stopped"),
        new("agent.stopped", task="T1", attempt=2, then="retry", reason="no progress"),
        gate("escalation", "approve", task="T1", auto=True),
        ev.escalation(
            req, task="T1", attempt=2, reason="all attempts failed", ticket=None
        ),
        # Jira unreachable: filing is pending, retried, then the key and sprint placement are recorded
        ev.escalation(
            req, task="T1", attempt=2, reason="all attempts failed", ticket=None
        ),
        ev.escalation(
            req, task="T1", attempt=2, reason="all attempts failed", ticket="AD-4242"
        ),
        gate("merge", "approve", task="T3"),
        gate("deploy", "approve"),
    ]
    log = EventLog(events_path(tmp_path, req))
    for item in stream:
        log.append(item)
    return log.read(strict=True).events


def test_a2_replay_of_a_realistic_full_log_is_deterministic(tmp_path: Path) -> None:
    events = _realistic_log(tmp_path)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    first = fold(events)
    assert first.requirement is R.PARTIAL
    assert states(first) == {"T1": T.ESCALATED, "T2": T.BLOCKED, "T3": T.APPROVED}
    assert first.tasks["T1"].attempt == 2
    assert first.tasks["T3"].attempt == 1
    # the same log, folded again, read again from disk, and round-tripped through JSON, gives the same snapshot
    assert fold(events) == first
    from ultron.store import events as ev
    from ultron.store.eventlog import EventLog, events_path

    reread = EventLog(events_path(tmp_path, REQ)).read(strict=True).events
    assert reread == events
    assert fold(reread) == first
    assert fold([ev.from_json(ev.to_json_line(e)) for e in events]) == first
    # every prefix replays without error, and the requirement settles exactly at the first escalation record
    settled_at = None
    for k in range(len(events) + 1):
        snap = fold(events[:k])
        assert fold(events[:k]) == snap
        if snap.requirement is R.PARTIAL and settled_at is None:
            settled_at = k
    assert settled_at is not None
    assert events[settled_at - 1].type == "escalation"
    assert events[settled_at - 1].data["ticket"] is None
    # the later ticket records came after settling and changed nothing
    later = [e for e in events[settled_at:] if e.type == "escalation"]
    assert [e.data["ticket"] for e in later] == [None, "AD-4242"]
    for k in range(settled_at, len(events) + 1):
        assert fold(events[:k]) == first


# --- housekeeping ----------------------------------------------------------------------------------------------


def test_no_t08_acceptance_test_is_skipped_or_expected_to_fail() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    markers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"skip", "skipif", "xfail"}:
            markers.append(node.attr)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {
            "skip",
            "xfail",
            "importorskip",
        }:
            markers.append(node.func.attr)  # type: ignore[attr-defined]
    assert not markers

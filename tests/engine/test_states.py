"""T08: requirement and task state machines, and folding events into state (spec §3, FR9, FR13, FR18, NFR1)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from ultron.engine.states import (
    REQUIREMENT_TRANSITIONS,
    TASK_TRANSITIONS,
    IllegalTransition,
    RequirementState,
    TaskState,
    fold,
    next_requirement_state,
    next_task_state,
    settle,
)
from ultron.store.events import Event

R = RequirementState
T = TaskState

REQ = "R-20261111-a3f2"

# --- AC1: every legal transition, written out by hand from spec §3 --------------------------------------------

LEGAL_REQUIREMENT: dict[tuple[RequirementState, str], RequirementState] = {
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

_UNFINISHED = (
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
    **{(state, "escalation"): T.ESCALATED for state in _UNFINISHED},
}

_EXTRA_TRIGGERS = (
    "bogus",
    "merge.approve",
    "plan.reject",
    "result.changes",
    "deploy.approve",
)


def test_requirement_table_is_exactly_the_legal_list() -> None:
    assert dict(REQUIREMENT_TRANSITIONS) == LEGAL_REQUIREMENT


def test_task_table_is_exactly_the_legal_list() -> None:
    assert dict(TASK_TRANSITIONS) == LEGAL_TASK


_REQ_TRIGGERS = sorted({t for _, t in LEGAL_REQUIREMENT} | set(_EXTRA_TRIGGERS))
_TASK_TRIGGERS = sorted({t for _, t in LEGAL_TASK} | set(_EXTRA_TRIGGERS))


@pytest.mark.parametrize(
    ("state", "trigger"),
    list(itertools.product(RequirementState, _REQ_TRIGGERS)),
)
def test_every_requirement_pair(state: RequirementState, trigger: str) -> None:
    expected = LEGAL_REQUIREMENT.get((state, trigger))
    if expected is None:
        with pytest.raises(IllegalTransition):
            next_requirement_state(state, trigger)
    else:
        assert next_requirement_state(state, trigger) is expected


@pytest.mark.parametrize(
    ("state", "trigger"),
    list(itertools.product(TaskState, _TASK_TRIGGERS)),
)
def test_every_task_pair(state: TaskState, trigger: str) -> None:
    expected = LEGAL_TASK.get((state, trigger))
    if expected is None:
        with pytest.raises(IllegalTransition):
            next_task_state(state, trigger)
    else:
        assert next_task_state(state, trigger) is expected


@pytest.mark.parametrize("origin", [T.PLANNING, T.CODING, T.TESTING])
def test_resume_returns_to_where_the_task_was_held(origin: TaskState) -> None:
    assert next_task_state(T.HELD, "task_resumed", held_from=origin) is origin


@pytest.mark.parametrize("state", list(TaskState))
def test_resume_is_illegal_unless_held_with_an_origin(state: TaskState) -> None:
    with pytest.raises(IllegalTransition):
        next_task_state(state, "task_resumed")  # no origin: never legal
    if state is not T.HELD:
        with pytest.raises(IllegalTransition):
            next_task_state(state, "task_resumed", held_from=T.CODING)


def test_illegal_transition_names_state_and_trigger() -> None:
    with pytest.raises(IllegalTransition, match="awaiting_plan.*result.approve"):
        next_task_state(T.AWAITING_PLAN, "result.approve")


# --- event helpers ----------------------------------------------------------------------------------------------


class Log:
    """Builds a requirement's event list with increasing `seq`."""

    def __init__(self, requirement: str = REQ) -> None:
        self.requirement = requirement
        self.events: list[Event] = []

    def add(
        self,
        type_: str,
        task: str | None = None,
        attempt: int | None = None,
        **data: Any,
    ) -> Log:
        self.events.append(
            Event(
                schema=1,
                seq=len(self.events) + 1,
                time="2026-11-11T00:00:00Z",
                actor="engine",
                type=type_,
                requirement=self.requirement,
                task=task,
                attempt=attempt,
                data=data,
                ultron_version="0.0.0",
            )
        )
        return self

    def gate(self, gate: str, decision: str, task: str | None = None) -> Log:
        return self.add("gate_decision", task, gate=gate, decision=decision)

    def to_in_progress(self, tasks: dict[str, list[str]], max_attempts: int = 3) -> Log:
        self.add("intake_started").add("intent_drafted").gate("intent", "approve")
        self.add("spec_drafted").gate("spec", "approve")
        self.add("decomposition_started")
        for task, deps in tasks.items():
            self.add("task_created", task, depends_on=deps, max_attempts=max_attempts)
        return self.add("tasks_drafted").gate("tasks", "approve")

    def plan_approved(self, task: str) -> Log:
        self.add("task_started", task).add("plan_drafted", task)
        return self.gate("plan", "approve", task)

    def attempt(self, task: str, n: int, outcome: str) -> Log:
        self.add("attempt_started", task, n).add("coding_finished", task, n)
        return self.add("attempt_finished", task, n, outcome=outcome)

    def approved(self, task: str) -> Log:
        self.plan_approved(task).attempt(task, 1, "passed")
        self.add("result_requested", task)
        return self.gate("result", "approve", task)

    def escalated_after_reject(self, task: str) -> Log:
        self.plan_approved(task).attempt(task, 1, "passed")
        self.add("result_requested", task).gate("result", "reject", task)
        return self.add("escalation", task, reason="rejected")


def state_of(log: Log, task: str) -> TaskState:
    return fold(log.events).tasks[task].state


# --- folding ------------------------------------------------------------------------------------------------------


def test_fold_empty_log_is_submitted() -> None:
    snap = fold([])
    assert snap.requirement is R.SUBMITTED
    assert snap.tasks == {}


def test_fold_requirement_path_with_intake_and_changes() -> None:
    log = Log().add("intake_started").add("intent_drafted")
    assert fold(log.events).requirement is R.AWAITING_INTENT
    log.gate("intent", "approve").add("spec_drafted")
    assert fold(log.events).requirement is R.AWAITING_SPEC
    log.gate("spec", "changes")
    assert fold(log.events).requirement is R.SPEC_DRAFTING
    log.add("spec_drafted").gate("spec", "approve")
    assert fold(log.events).requirement is R.SPEC_APPROVED
    log.add("decomposition_started")
    assert fold(log.events).requirement is R.DECOMPOSING


def test_fold_template_intent_and_auto_tasks_gate_skip_the_bracketed_steps() -> None:
    log = Log().add("spec_started").add("spec_drafted").gate("spec", "approve")
    log.add("decomposition_started").add("task_created", "T1", depends_on=[])
    log.gate("tasks", "approve")
    snap = fold(log.events)
    assert snap.requirement is R.IN_PROGRESS
    assert snap.tasks["T1"].state is T.PENDING


def test_fold_ignores_events_outside_the_state_vocabulary() -> None:
    log = Log().add("intake", source="text").add("config_snapshot", max_attempts=3)
    log.add("agent_call", role="spec")
    log.gate("merge", "approve").gate("escalation", "approve")
    assert fold(log.events).requirement is R.SUBMITTED


def test_fold_rejects_a_gate_decision_without_gate_or_decision() -> None:
    with pytest.raises(IllegalTransition):
        fold(Log().add("gate_decision").events)


def test_fold_rejects_illegal_order() -> None:
    with pytest.raises(IllegalTransition):
        fold(Log().gate("spec", "approve").events)


def test_fold_rejects_events_of_another_requirement() -> None:
    events = [
        *Log().add("spec_started").events,
        *Log("R-20261111-ffff").add("spec_drafted").events,
    ]
    with pytest.raises(ValueError, match="requirement"):
        fold(events)


def test_task_events_need_an_in_progress_requirement() -> None:
    log = Log().add("spec_started").add("task_started", "T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_task_event_needs_a_task_and_a_known_one() -> None:
    with pytest.raises(IllegalTransition):
        fold(Log().to_in_progress({"T1": []}).add("task_started").events)
    with pytest.raises(IllegalTransition):
        fold(Log().to_in_progress({"T1": []}).add("task_started", "T9").events)


def test_task_created_twice_is_illegal() -> None:
    log = Log().add("spec_started").add("spec_drafted").gate("spec", "approve")
    log.add("decomposition_started")
    log.add("task_created", "T1", depends_on=[]).add(
        "task_created", "T1", depends_on=[]
    )
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_task_walks_the_full_path_to_approved() -> None:
    log = Log().to_in_progress({"T1": []})
    seen: list[tuple[str, str]] = []

    def check(expected: str) -> None:
        seen.append((expected, state_of(log, "T1").value))

    log.add("task_started", "T1")
    check("planning")
    log.add("plan_drafted", "T1")
    check("awaiting_plan")
    log.gate("plan", "changes", "T1")
    check("planning")
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    check("plan_approved")
    log.add("attempt_started", "T1", 1)
    check("coding")
    log.add("coding_finished", "T1", 1)
    check("testing")
    log.add("attempt_finished", "T1", 1, outcome="passed")
    check("passed")
    log.add("result_requested", "T1")
    check("awaiting_result")
    log.gate("result", "approve", "T1")
    check("approved")
    assert all(expected == actual for expected, actual in seen)


def test_failed_attempt_with_attempts_left_goes_back_through_the_plan_gate() -> None:
    log = Log().to_in_progress({"T1": []}, max_attempts=3)
    log.plan_approved("T1").attempt("T1", 1, "failed")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.PLANNING, 1)
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    log.add("attempt_started", "T1", 2)
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.CODING, 2)


def test_last_failed_attempt_fails_the_task_then_escalation_escalates_it() -> None:
    log = Log().to_in_progress({"T1": []}, max_attempts=2)
    log.plan_approved("T1").attempt("T1", 1, "failed")
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    log.attempt("T1", 2, "failed")
    assert state_of(log, "T1") is T.FAILED
    log.add("escalation", "T1", 2, reason="max_attempts reached")
    assert state_of(log, "T1") is T.ESCALATED


def test_attempt_number_must_follow_the_last() -> None:
    log = Log().to_in_progress({"T1": []}).plan_approved("T1")
    log.add("attempt_started", "T1", 2)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_hold_resume_and_stop_variants() -> None:
    log = Log().to_in_progress({"T1": []}).plan_approved("T1")
    log.add("attempt_started", "T1", 1).add("agent.stopped", "T1", 1, then="hold")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.HELD, 1)
    log.add("task_resumed", "T1")
    assert state_of(log, "T1") is T.CODING
    log.add("agent.stopped", "T1", 1)  # the default for a stop is hold
    assert state_of(log, "T1") is T.HELD
    log.add("escalation", "T1", reason="user escalated")
    assert state_of(log, "T1") is T.ESCALATED


def test_stop_with_retry_counts_as_a_failed_attempt() -> None:
    log = Log().to_in_progress({"T1": []}, max_attempts=2).plan_approved("T1")
    log.add("attempt_started", "T1", 1).add("agent.stopped", "T1", 1, then="retry")
    assert state_of(log, "T1") is T.PLANNING
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    log.add("attempt_started", "T1", 2).add("agent.stopped", "T1", 2, then="retry")
    assert state_of(log, "T1") is T.FAILED


def test_stop_with_escalate_escalates() -> None:
    log = Log().to_in_progress({"T1": []}).plan_approved("T1")
    log.add("attempt_started", "T1", 1).add("agent.stopped", "T1", 1, then="escalate")
    assert state_of(log, "T1") is T.ESCALATED


def test_stop_with_an_unknown_then_is_illegal() -> None:
    log = Log().to_in_progress({"T1": []}).plan_approved("T1")
    log.add("attempt_started", "T1", 1).add("agent.stopped", "T1", 1, then="explode")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_attempt_finished_outcomes() -> None:
    log = Log().to_in_progress({"T1": []}).plan_approved("T1")
    log.add("attempt_started", "T1", 1).add("coding_finished", "T1", 1)
    log.add("attempt_finished", "T1", 1, outcome="stopped")  # agent.stopped drives this
    assert state_of(log, "T1") is T.TESTING
    log.add("attempt_finished", "T1", 1, outcome="weird")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_a_task_cannot_start_before_its_dependencies_are_approved() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}).add("task_started", "T2")
    with pytest.raises(IllegalTransition, match="T1"):
        fold(log.events)


def test_a_task_with_an_unknown_dependency_cannot_start() -> None:
    log = Log().to_in_progress({"T1": ["T9"]}).add("task_started", "T1")
    with pytest.raises(IllegalTransition, match="T9"):
        fold(log.events)


def test_dependent_starts_after_its_dependency_is_approved() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}).approved("T1")
    log.add("task_started", "T2")
    assert state_of(log, "T2") is T.PLANNING


def test_repeat_escalation_is_a_noop_but_an_approved_task_cannot_escalate() -> None:
    log = Log().to_in_progress({"T1": [], "T2": [], "T3": []})
    log.escalated_after_reject("T1").approved("T2")
    log.add("escalation", "T1", reason="ticket filed", ticket="AD-400")
    assert state_of(log, "T1") is T.ESCALATED
    log.add("escalation", "T2", reason="oops")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_ticket_record_after_a_single_task_requirement_escalated() -> None:
    """Spec §12: the Jira key is recorded after the task escalates, here after the requirement settled."""
    log = Log().to_in_progress({"T1": []}, max_attempts=1)
    log.plan_approved("T1").attempt("T1", 1, "failed")
    log.add("escalation", "T1", 1, reason="max_attempts", ticket=None)
    assert fold(log.events).requirement is R.ESCALATED
    log.add("escalation", "T1", 1, reason="max_attempts", ticket="AD-401")
    snap = fold(log.events)
    assert snap.requirement is R.ESCALATED
    assert snap.tasks["T1"].state is T.ESCALATED


def test_ticket_record_after_the_last_task_escalated_to_partial() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).approved("T1")
    log.escalated_after_reject("T2")
    before = fold(log.events)
    assert before.requirement is R.PARTIAL
    log.add("escalation", "T2", reason="rejected", ticket="AD-402")
    log.add("escalation", "T2", reason="rejected", ticket="AD-402", sprint="active")
    assert fold(log.events) == before


def test_first_escalation_after_the_requirement_settled_is_illegal() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).approved("T1")
    log.escalated_after_reject("T2")
    log.add("escalation", "T1", reason="too late")
    with pytest.raises(IllegalTransition, match="partial"):
        fold(log.events)


def test_repeat_escalation_of_an_unknown_task_is_illegal() -> None:
    log = Log().to_in_progress({"T1": []}).escalated_after_reject("T1")
    log.add("escalation", "T9", reason="rejected", ticket="AD-403")
    with pytest.raises(IllegalTransition):
        fold(log.events)


# --- AC2: folding twice gives the same state ---------------------------------------------------------------------


def _full_run() -> list[Event]:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"], "T3": []}, max_attempts=2)
    log.plan_approved("T1").attempt("T1", 1, "failed")
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    log.attempt("T1", 2, "failed").add("escalation", "T1", 2, reason="max_attempts")
    log.approved("T3")
    return log.events


def test_fold_twice_gives_identical_state() -> None:
    events = _full_run()
    first, second = fold(events), fold(list(events))
    assert first == second
    assert first.requirement is R.PARTIAL
    assert {k: v.state for k, v in first.tasks.items()} == {
        "T1": T.ESCALATED,
        "T2": T.BLOCKED,
        "T3": T.APPROVED,
    }


def test_fold_takes_any_iterable_and_every_prefix_is_stable() -> None:
    events = _full_run()
    assert fold(iter(events)) == fold(events)
    for n in range(len(events) + 1):
        assert fold(events[:n]) == fold(events[:n])


# --- AC3: rejected and failed lead to escalated; an escalated dependency blocks -----------------------------------


def test_rejected_leads_to_escalated() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []})
    log.plan_approved("T1").attempt("T1", 1, "passed")
    log.add("result_requested", "T1").gate("result", "reject", "T1")
    assert state_of(log, "T1") is T.REJECTED
    log.add("escalation", "T1", reason="rejected at the result gate")
    assert state_of(log, "T1") is T.ESCALATED


def test_failed_leads_to_escalated() -> None:
    assert next_task_state(T.FAILED, "escalation") is T.ESCALATED
    assert next_task_state(T.REJECTED, "escalation") is T.ESCALATED


def test_a_task_whose_dependency_is_escalated_becomes_blocked_transitively() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"], "T3": ["T2"], "T4": []})
    log.plan_approved("T1").attempt("T1", 1, "passed")
    log.add("result_requested", "T1").gate("result", "reject", "T1")
    assert state_of(log, "T2") is T.PENDING  # rejected is not yet escalated
    log.add("escalation", "T1", reason="rejected")
    states = {k: v.state for k, v in fold(log.events).tasks.items()}
    assert states == {
        "T1": T.ESCALATED,
        "T2": T.BLOCKED,
        "T3": T.BLOCKED,
        "T4": T.PENDING,
    }


def test_a_blocked_task_cannot_start() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"], "T3": []})
    log.escalated_after_reject("T1").add("task_started", "T2")
    with pytest.raises(IllegalTransition, match="blocked"):
        fold(log.events)


def test_requirement_cap_escalates_unfinished_tasks() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).approved("T1")
    log.add("escalation", "T2", reason="requirement spending cap reached")
    snap = fold(log.events)
    assert snap.requirement is R.PARTIAL
    assert snap.tasks["T2"].state is T.ESCALATED


# --- AC4: how a requirement ends --------------------------------------------------------------------------------


def test_requirement_complete_when_all_tasks_approved() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}).approved("T1")
    assert fold(log.events).requirement is R.IN_PROGRESS
    log.approved("T2")
    assert fold(log.events).requirement is R.COMPLETE


def test_requirement_partial_when_some_approved_and_some_escalated_or_blocked() -> None:
    log = Log().to_in_progress({"T1": [], "T2": [], "T3": ["T2"]}).approved("T1")
    log.escalated_after_reject("T2")
    snap = fold(log.events)
    assert snap.tasks["T3"].state is T.BLOCKED
    assert snap.requirement is R.PARTIAL


def test_requirement_escalated_when_no_task_is_approved() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}).escalated_after_reject("T1")
    snap = fold(log.events)
    assert snap.tasks["T2"].state is T.BLOCKED
    assert snap.requirement is R.ESCALATED


def test_requirement_stays_in_progress_while_a_task_is_unfinished() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).approved("T1")
    log.add("task_started", "T2")
    assert fold(log.events).requirement is R.IN_PROGRESS


def test_settled_requirement_takes_no_more_task_events() -> None:
    log = Log().to_in_progress({"T1": []}).approved("T1")
    assert fold(log.events).requirement is R.COMPLETE
    log.add("task_started", "T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([T.APPROVED, T.APPROVED], R.COMPLETE),
        ([T.APPROVED, T.ESCALATED], R.PARTIAL),
        ([T.APPROVED, T.BLOCKED], R.PARTIAL),
        ([T.APPROVED, T.ESCALATED, T.BLOCKED], R.PARTIAL),
        ([T.ESCALATED, T.BLOCKED], R.ESCALATED),
        ([T.ESCALATED], R.ESCALATED),
        ([T.APPROVED, T.HELD], None),
        ([T.APPROVED, T.PENDING], None),
        ([T.ESCALATED, T.FAILED], None),
        ([], None),
    ],
)
def test_settle(states: list[TaskState], expected: RequirementState | None) -> None:
    assert settle(states) is expected

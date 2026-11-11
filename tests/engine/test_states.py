"""T08: requirement and task state machines, and folding events into state (spec §3, FR9, FR13, FR18, NFR1)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from ultron.engine.states import (
    EDIT_ROUNDS_MAX,
    EDIT_STOPPED,
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
from ultron.store.events import (
    ATTACHMENT_MISSING,
    EDIT_DONE,
    EDIT_REQUESTED,
    FILED_REASON_CAP,
    FILED_REASON_EDIT_ROUNDS,
    FILED_WITH_MISSING,
    Event,
)

R = RequirementState
T = TaskState

REQ = "R-20261111-a3f2"

# --- AC1: every legal transition, written out by hand from spec §3 --------------------------------------------

LEGAL_REQUIREMENT: dict[tuple[RequirementState, str], RequirementState] = {
    (R.SUBMITTED, "intake_started"): R.INTAKE_DRAFTING,
    (R.SUBMITTED, "spec_started"): R.SPEC_DRAFTING,
    (R.INTAKE_DRAFTING, "intent_drafted"): R.AWAITING_INTENT,
    (R.AWAITING_INTENT, "intent.approve"): R.SPEC_DRAFTING,
    (R.AWAITING_INTENT, "intent.changes"): R.INTAKE_DRAFTING,  # D28
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
    # D27: a missing attachment before filing, and the user's question after filing (spec §3, §12)
    # D29: the check runs before every escalation, so from every escalatable state
    **{(state, "attachment_missing"): T.CALL_FOR_EDIT for state in _UNFINISHED},
    (T.ESCALATED, "edit_requested"): T.CALL_FOR_EDIT,
    (T.CALL_FOR_EDIT, "edit_done"): T.ESCALATED,
    # D29: still missing after the edit starts another round; at the cap or after the last round, file anyway
    (T.CALL_FOR_EDIT, "attachment_missing"): T.CALL_FOR_EDIT,
    (T.CALL_FOR_EDIT, "filed_with_missing"): T.ESCALATED,
}

_EXTRA_TRIGGERS = (
    "bogus",
    "merge.approve",
    "plan.reject",
    "result.changes",
    "deploy.approve",
    "edit_stopped",  # resolved with an origin only (D29)
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


def test_fold_intent_changes_go_back_to_intake_and_redraft_returns_to_the_gate() -> (
    None
):
    """D28: request changes at the intent gate sends the draft back to the intake agent."""
    log = Log().add("intake_started").add("intent_drafted")
    log.add(
        "gate_decision", gate="intent", decision="changes", comment="name the users"
    )
    assert fold(log.events).requirement is R.INTAKE_DRAFTING
    log.add("intent_drafted")
    assert fold(log.events).requirement is R.AWAITING_INTENT
    log.gate("intent", "changes").add("intent_drafted").gate("intent", "approve")
    assert fold(log.events).requirement is R.SPEC_DRAFTING


@pytest.mark.parametrize("decision", ["changes", "approve"])
def test_fold_intent_decision_without_a_draft_is_illegal(decision: str) -> None:
    log = Log().add("intake_started").gate("intent", decision)
    with pytest.raises(IllegalTransition):
        fold(log.events)


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
        (
            [T.APPROVED, T.CALL_FOR_EDIT],
            None,
        ),  # settle sees states only; the fold maps a filed one
        ([], None),
    ],
)
def test_settle(states: list[TaskState], expected: RequirementState | None) -> None:
    assert settle(states) is expected


# --- AC6 (D27): call_for_edit -----------------------------------------------------------------------------------


def test_call_for_edit_event_names() -> None:
    assert (ATTACHMENT_MISSING, EDIT_REQUESTED, EDIT_DONE) == (
        "attachment_missing",
        "edit_requested",
        "edit_done",
    )
    assert T.CALL_FOR_EDIT.value == "call_for_edit"
    # D29
    assert FILED_WITH_MISSING == "filed_with_missing"
    assert (FILED_REASON_CAP, FILED_REASON_EDIT_ROUNDS) == ("cap", "edit_rounds")
    assert EDIT_STOPPED == "edit_stopped"
    assert EDIT_ROUNDS_MAX == 2


def test_what_leaves_or_stays_in_call_for_edit() -> None:
    allowed = {t for (s, t) in TASK_TRANSITIONS if s is T.CALL_FOR_EDIT}
    assert allowed == {"edit_done", "attachment_missing", "filed_with_missing"}
    for trigger in ("escalation", "attempt_started", "agent_held", "attempt_failed"):
        with pytest.raises(IllegalTransition):
            next_task_state(T.CALL_FOR_EDIT, trigger)


@pytest.mark.parametrize(
    "state", [s for s in TaskState if s not in (*_UNFINISHED, T.CALL_FOR_EDIT)]
)
def test_attachment_missing_only_from_escalatable_states_or_another_round(
    state: TaskState,
) -> None:
    with pytest.raises(IllegalTransition):
        next_task_state(state, "attachment_missing")


@pytest.mark.parametrize("origin", [*_UNFINISHED, T.ESCALATED])
def test_edit_stopped_returns_to_where_call_for_edit_was_entered_from(
    origin: TaskState,
) -> None:
    assert next_task_state(T.CALL_FOR_EDIT, EDIT_STOPPED, edit_from=origin) is origin


@pytest.mark.parametrize("state", list(TaskState))
def test_edit_stopped_is_illegal_unless_in_call_for_edit_with_an_origin(
    state: TaskState,
) -> None:
    with pytest.raises(IllegalTransition):
        next_task_state(state, EDIT_STOPPED)  # no origin: never legal
    for origin in (T.APPROVED, T.BLOCKED, T.CALL_FOR_EDIT):
        with pytest.raises(IllegalTransition):
            next_task_state(state, EDIT_STOPPED, edit_from=origin)
    if state is not T.CALL_FOR_EDIT:
        with pytest.raises(IllegalTransition):
            next_task_state(state, EDIT_STOPPED, edit_from=T.FAILED)


@pytest.mark.parametrize("state", [s for s in TaskState if s is not T.ESCALATED])
def test_edit_requested_only_from_escalated(state: TaskState) -> None:
    with pytest.raises(IllegalTransition):
        next_task_state(state, "edit_requested")


def _failed(log: Log, task: str, max_attempts: int) -> Log:
    log.plan_approved(task)
    for n in range(1, max_attempts + 1):
        if n > 1:
            log.add("plan_drafted", task).gate("plan", "approve", task)
        log.attempt(task, n, "failed")
    return log


def _rejected(log: Log, task: str) -> Log:
    log.plan_approved(task).attempt(task, 1, "passed")
    return log.add("result_requested", task).gate("result", "reject", task)


def test_failed_with_a_missing_attachment_passes_through_call_for_edit() -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}, max_attempts=2)
    _failed(log, "T1", 2).add(ATTACHMENT_MISSING, "T1", missing=["plan.md"])
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.CALL_FOR_EDIT
    assert snap.tasks["T1"].attempt == 2
    assert (
        snap.tasks["T2"].state is T.PENDING
    )  # not escalated yet, so nothing is blocked
    assert snap.requirement is R.IN_PROGRESS
    log.add(EDIT_DONE, "T1")
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.ESCALATED
    assert snap.tasks["T1"].attempt == 2
    assert snap.tasks["T2"].state is T.BLOCKED
    assert snap.requirement is R.ESCALATED
    log.add("escalation", "T1", reason="max_attempts", ticket="AD-500")  # the filing
    assert fold(log.events) == snap


def test_rejected_with_a_missing_attachment_passes_through_call_for_edit() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1")
    assert state_of(log, "T1") is T.CALL_FOR_EDIT
    log.add(EDIT_DONE, "T1")
    assert state_of(log, "T1") is T.ESCALATED
    assert fold(log.events).tasks["T1"].attempt == 1


def test_all_attachments_present_escalates_directly() -> None:
    log = _rejected(Log().to_in_progress({"T1": []}), "T1")
    log.add("escalation", "T1", reason="rejected")
    assert state_of(log, "T1") is T.ESCALATED


def test_a_first_escalation_cannot_skip_the_pending_edit() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1").add("escalation", "T1", reason="rejected")
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_call_for_edit_is_not_an_attempt() -> None:
    log = _failed(Log().to_in_progress({"T1": []}, max_attempts=3), "T1", 3)
    log.add(ATTACHMENT_MISSING, "T1").add(EDIT_DONE, "T1")
    log.add("escalation", "T1", reason="max_attempts", ticket="AD-501")
    for _ in range(3):
        log.add(EDIT_REQUESTED, "T1", question="why?").add(EDIT_DONE, "T1")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt, status.max_attempts) == (T.ESCALATED, 3, 3)
    log.add(EDIT_REQUESTED, "T1", question="again?").add("attempt_started", "T1", 4)
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize(
    ("type_", "data"),
    [
        ("attempt_started", {}),
        ("coding_finished", {}),
        ("attempt_finished", {"outcome": "failed"}),
        ("agent.stopped", {"then": "bogus"}),
        ("task_resumed", {}),
        ("escalation", {}),
        (EDIT_REQUESTED, {}),
        (FILED_WITH_MISSING, {}),
        (FILED_WITH_MISSING, {"reason": "bogus"}),
        (
            FILED_WITH_MISSING,
            {"reason": FILED_REASON_EDIT_ROUNDS},
        ),  # only after the last round
    ],
)
def test_call_for_edit_before_filing_takes_no_attempt_events(
    type_: str, data: dict[str, Any]
) -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1").add(type_, "T1", **data)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_escalated_to_call_for_edit_and_back_while_in_progress() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    log.add(EDIT_REQUESTED, "T1", question="which config?")
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.CALL_FOR_EDIT
    assert snap.requirement is R.IN_PROGRESS
    log.add(EDIT_DONE, "T1", answer="answer.md")
    assert state_of(log, "T1") is T.ESCALATED


def test_edit_done_without_a_request_and_a_double_request_are_illegal() -> None:
    log = Log().to_in_progress({"T1": []}).escalated_after_reject("T1")
    log.add(EDIT_DONE, "T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)
    log = Log().to_in_progress({"T1": []}).escalated_after_reject("T1")
    log.add(EDIT_REQUESTED, "T1").add(EDIT_REQUESTED, "T1")
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize("task", ["T1", "T3", "T9"])
def test_call_for_edit_of_an_approved_blocked_or_unknown_task_is_illegal(
    task: str,
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": [], "T3": ["T2"]}).approved("T1")
    log.escalated_after_reject("T2").add(EDIT_REQUESTED, task)
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize("outcome", [R.PARTIAL, R.ESCALATED])
def test_call_for_edit_round_trips_after_the_requirement_settled(
    outcome: RequirementState,
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": []})
    if outcome is R.PARTIAL:
        log.approved("T1")
    else:
        log.escalated_after_reject("T1")
    log.escalated_after_reject("T2")
    settled = fold(log.events)
    assert settled.requirement is outcome
    log.add("escalation", "T2", reason="rejected", ticket="AD-502")
    log.add(EDIT_REQUESTED, "T2", question="what failed?")
    middle = fold(log.events)
    assert middle.requirement is outcome
    assert middle.tasks["T2"].state is T.CALL_FOR_EDIT
    # a retried filing record arriving during the edit is a no-op
    log.add("escalation", "T2", reason="rejected", ticket="AD-502", sprint="active")
    assert fold(log.events) == middle
    log.add(EDIT_DONE, "T2", answer="answer.md")
    log.add(EDIT_REQUESTED, "T2", question="and the logs?").add(EDIT_DONE, "T2")
    after = fold(log.events)
    assert after == settled
    assert fold(list(log.events)) == after
    for n in range(len(log.events) + 1):
        assert fold(log.events[:n]) == fold(log.events[:n])  # every prefix replays


@pytest.mark.parametrize(
    ("type_", "data"),
    [
        ("task_started", {}),
        ("attempt_started", {}),
        ("agent.stopped", {"then": "bogus"}),
        (ATTACHMENT_MISSING, {}),
        (EDIT_REQUESTED, {}),
        (FILED_WITH_MISSING, {"reason": FILED_REASON_CAP}),
        (FILED_WITH_MISSING, {"reason": FILED_REASON_EDIT_ROUNDS}),
    ],
)
def test_other_events_on_a_filed_task_in_call_for_edit_are_illegal(
    type_: str, data: dict[str, Any]
) -> None:
    log = Log().to_in_progress({"T1": []}).escalated_after_reject("T1")
    assert fold(log.events).requirement is R.ESCALATED
    log.add(EDIT_REQUESTED, "T1").add(type_, "T1", **data)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_a_filed_task_in_call_for_edit_counts_as_escalated_for_settling() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    log.add(EDIT_REQUESTED, "T1", question="?")
    log.approved("T2")
    settled = fold(log.events)
    assert settled.requirement is R.PARTIAL
    assert settled.tasks["T1"].state is T.CALL_FOR_EDIT
    log.add(EDIT_DONE, "T1")
    snap = fold(log.events)
    assert snap.requirement is R.PARTIAL
    assert snap.tasks["T1"].state is T.ESCALATED


def test_settling_does_not_depend_on_when_the_question_was_asked() -> None:
    early = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    early.add(EDIT_REQUESTED, "T1").approved("T2").add(EDIT_DONE, "T1")
    late = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    late.approved("T2").add(EDIT_REQUESTED, "T1").add(EDIT_DONE, "T1")
    assert fold(early.events) == fold(late.events)


def test_a_pre_filing_call_for_edit_task_is_unfinished_for_settling() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1").approved("T2")
    assert fold(log.events).requirement is R.IN_PROGRESS
    log.add(EDIT_DONE, "T1")
    assert fold(log.events).requirement is R.PARTIAL


# --- AC7 (D29): call_for_edit edge cases ------------------------------------------------------------------------


def _replays(log: Log) -> None:
    """Every prefix folds, and folds the same way twice."""
    for n in range(len(log.events) + 1):
        assert fold(log.events[:n]) == fold(list(log.events[:n]))


def _to_state(log: Log, task: str, state: TaskState) -> None:
    """Drive `task` (max_attempts 1) to `state`."""
    if state is T.PENDING:
        return
    log.add("task_started", task)
    if state is T.PLANNING:
        return
    log.add("plan_drafted", task)
    if state is T.AWAITING_PLAN:
        return
    log.gate("plan", "approve", task)
    if state is T.PLAN_APPROVED:
        return
    log.add("attempt_started", task, 1)
    if state is T.CODING:
        return
    if state is T.HELD:
        log.add("agent.stopped", task, then="hold")
        return
    log.add("coding_finished", task, 1)
    if state is T.TESTING:
        return
    if state is T.FAILED:
        log.add("attempt_finished", task, 1, outcome="failed")
        return
    log.add("attempt_finished", task, 1, outcome="passed")
    if state is T.PASSED:
        return
    log.add("result_requested", task)
    if state is T.AWAITING_RESULT:
        return
    assert state is T.REJECTED
    log.gate("result", "reject", task)


@pytest.mark.parametrize("then", [None, "hold", "retry", "escalate"])
@pytest.mark.parametrize("origin", [T.FAILED, T.REJECTED])
def test_a_stop_in_call_for_edit_before_filing_returns_to_where_it_came_from(
    then: str | None, origin: TaskState
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}, max_attempts=2)
    if origin is T.FAILED:
        _failed(log, "T1", 2)
    else:
        _rejected(log, "T1")
    before = fold(log.events)
    log.add(ATTACHMENT_MISSING, "T1", missing=["report.md"])
    data = {} if then is None else {"then": then}
    log.add("agent.stopped", "T1", **data)
    snap = fold(log.events)
    assert snap == before  # back where it was: same state and attempt count, no round
    assert snap.tasks["T1"].state is origin
    assert snap.tasks["T1"].edit_round == 0
    # the engine runs the check again: a fresh first round
    log.add(ATTACHMENT_MISSING, "T1").add(EDIT_DONE, "T1")
    assert state_of(log, "T1") is T.ESCALATED
    _replays(log)


@pytest.mark.parametrize("settled", [False, True])
def test_a_stop_in_call_for_edit_after_filing_returns_to_escalated(
    settled: bool,
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    if settled:
        log.approved("T2")
    before = fold(log.events)
    log.add("escalation", "T1", reason="rejected", ticket="AD-600")
    log.add(EDIT_REQUESTED, "T1", question="which config?")
    log.add("agent.stopped", "T1", then="hold")
    after = fold(log.events)
    assert after == before
    assert after.requirement is (R.PARTIAL if settled else R.IN_PROGRESS)
    log.add(EDIT_REQUESTED, "T1", question="again?").add(EDIT_DONE, "T1")
    assert fold(log.events) == before
    _replays(log)


def test_a_stop_in_call_for_edit_never_counts_an_attempt() -> None:
    log = Log().to_in_progress({"T1": []}, max_attempts=3)
    log.plan_approved("T1").attempt("T1", 1, "failed")
    log.add("plan_drafted", "T1").gate("plan", "approve", "T1")
    log.add("attempt_started", "T1", 2)
    log.add(ATTACHMENT_MISSING, "T1").add("agent.stopped", "T1", then="retry")
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.attempt) == (T.CODING, 2)


@pytest.mark.parametrize("origin", list(_UNFINISHED))
def test_the_attachment_check_runs_before_every_escalation(origin: TaskState) -> None:
    """D29 (2): failed, rejected, held, any unfinished state at the cap, a stop with then=escalate."""
    log = Log().to_in_progress({"T1": [], "T2": [], "T3": ["T1"]}, max_attempts=1)
    _to_state(log, "T1", origin)
    assert state_of(log, "T1") is origin
    status_before = fold(log.events).tasks["T1"]
    log.add(ATTACHMENT_MISSING, "T1", missing=["attempt-1.log"])
    snap = fold(log.events)
    status = snap.tasks["T1"]
    assert (status.state, status.edit_from, status.edit_round) == (
        T.CALL_FOR_EDIT,
        origin,
        1,
    )
    assert status.attempt == status_before.attempt
    assert snap.tasks["T3"].state is T.PENDING  # not filed yet: nothing blocked
    assert snap.requirement is R.IN_PROGRESS
    log.add(EDIT_DONE, "T1")
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.ESCALATED
    assert snap.tasks["T1"].attempt == status_before.attempt
    assert snap.tasks["T3"].state is T.BLOCKED
    _replays(log)


def test_a_stop_in_call_for_edit_from_held_can_still_resume() -> None:
    log = Log().to_in_progress({"T1": [], "T2": []})
    log.plan_approved("T1").add("attempt_started", "T1", 1)
    log.add("agent.stopped", "T1", then="hold")
    held = fold(log.events)
    assert held.tasks["T1"].held_from is T.CODING
    # the user escalates the held task: the check finds a file missing, then the edit agent is stopped
    log.add(ATTACHMENT_MISSING, "T1").add("agent.stopped", "T1")
    assert fold(log.events) == held
    log.add("task_resumed", "T1")
    assert state_of(log, "T1") is T.CODING
    _replays(log)


def test_still_missing_after_the_edit_starts_a_second_round_then_files_anyway() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1", missing=["report.md"])
    assert fold(log.events).tasks["T1"].edit_round == 1
    log.add(
        ATTACHMENT_MISSING, "T1", missing=["report.md"]
    )  # still missing after the edit
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.edit_from, status.edit_round) == (
        T.CALL_FOR_EDIT,
        T.REJECTED,
        EDIT_ROUNDS_MAX,
    )
    log.add(
        FILED_WITH_MISSING, "T1", reason=FILED_REASON_EDIT_ROUNDS, missing=["report.md"]
    )
    status = fold(log.events).tasks["T1"]
    assert (status.state, status.edit_from, status.edit_round, status.attempt) == (
        T.ESCALATED,
        None,
        0,
        1,
    )
    log.approved("T2")
    assert fold(log.events).requirement is R.PARTIAL
    log.add("escalation", "T1", reason="rejected", ticket="AD-601")  # the ticket record
    assert fold(log.events).requirement is R.PARTIAL
    _replays(log)


def test_the_second_round_can_end_with_the_file_added() -> None:
    log = _rejected(Log().to_in_progress({"T1": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1").add(ATTACHMENT_MISSING, "T1").add(EDIT_DONE, "T1")
    snap = fold(log.events)
    assert (snap.tasks["T1"].state, snap.requirement) == (T.ESCALATED, R.ESCALATED)


def test_a_third_round_is_illegal() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    for _ in range(EDIT_ROUNDS_MAX + 1):
        log.add(ATTACHMENT_MISSING, "T1")
    with pytest.raises(IllegalTransition, match="round"):
        fold(log.events)


def test_filing_for_rounds_before_the_last_round_is_illegal() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(ATTACHMENT_MISSING, "T1")
    log.add(FILED_WITH_MISSING, "T1", reason=FILED_REASON_EDIT_ROUNDS)
    with pytest.raises(IllegalTransition, match="round"):
        fold(log.events)


@pytest.mark.parametrize("rounds", [1, 2])
def test_the_cap_during_call_for_edit_files_noting_the_missing_file(
    rounds: int,
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": ["T1"]}, max_attempts=2)
    _failed(log, "T1", 2)
    for _ in range(rounds):
        log.add(ATTACHMENT_MISSING, "T1", missing=["attempt-2.log"])
    log.add(
        FILED_WITH_MISSING, "T1", reason=FILED_REASON_CAP, missing=["attempt-2.log"]
    )
    snap = fold(log.events)
    assert snap.tasks["T1"].state is T.ESCALATED
    assert snap.tasks["T1"].attempt == 2
    assert snap.tasks["T2"].state is T.BLOCKED
    assert snap.requirement is R.ESCALATED
    log.add("escalation", "T1", reason="max_attempts", ticket="AD-602")  # repeat: no-op
    log.add(EDIT_REQUESTED, "T1", question="why?").add(EDIT_DONE, "T1")
    assert fold(log.events) == snap
    _replays(log)


def test_filing_anyway_needs_a_pending_edit() -> None:
    log = _rejected(Log().to_in_progress({"T1": [], "T2": []}), "T1")
    log.add(FILED_WITH_MISSING, "T1", reason=FILED_REASON_CAP)
    with pytest.raises(IllegalTransition):
        fold(log.events)


@pytest.mark.parametrize(
    ("type_", "data"),
    [
        (ATTACHMENT_MISSING, {}),
        (FILED_WITH_MISSING, {"reason": FILED_REASON_CAP}),
    ],
)
def test_a_filed_task_in_call_for_edit_takes_no_attachment_round_or_filing(
    type_: str, data: dict[str, Any]
) -> None:
    log = Log().to_in_progress({"T1": [], "T2": []}).escalated_after_reject("T1")
    assert fold(log.events).requirement is R.IN_PROGRESS
    log.add(EDIT_REQUESTED, "T1").add(type_, "T1", **data)
    with pytest.raises(IllegalTransition):
        fold(log.events)


def test_d29_full_run_replays_identically_after_settling() -> None:
    log = Log().to_in_progress({"A": [], "B": [], "C": ["A"]}, max_attempts=1)
    _rejected(log, "A")
    # stopped in call_for_edit: back to rejected
    log.add(ATTACHMENT_MISSING, "A").add("agent.stopped", "A")
    log.add(ATTACHMENT_MISSING, "A").add(ATTACHMENT_MISSING, "A")
    log.add(FILED_WITH_MISSING, "A", reason=FILED_REASON_EDIT_ROUNDS, missing=["x"])
    log.approved("B")
    settled = fold(log.events)
    assert settled.requirement is R.PARTIAL
    assert {n: s.state for n, s in settled.tasks.items()} == {
        "A": T.ESCALATED,
        "B": T.APPROVED,
        "C": T.BLOCKED,
    }
    log.add("escalation", "A", ticket="AD-603")
    log.add(EDIT_REQUESTED, "A").add("agent.stopped", "A", then="escalate")
    log.add(EDIT_REQUESTED, "A").add(EDIT_DONE, "A")
    assert fold(log.events) == settled
    assert fold(iter(log.events)) == fold(log.events)
    _replays(log)

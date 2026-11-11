"""Requirement and task state machines, and folding a requirement's events into state (spec §3, §8, §9.1).

Two pure tables map `(state, trigger)` to the next state. Anything not in a table raises `IllegalTransition`
(NFR1: the engine enforces the rules, never an agent). `fold` reads a requirement's events in order and returns
the same `Snapshot` every time for the same list.

A **trigger** is a string. Most are the event type (`plan_drafted`). A gate decision is `<gate>.<decision>`
(`plan.approve`). Three are resolved from event data and context, never written by hand:

- `attempt_failed` / `attempts_exhausted`: a failed attempt (`attempt_finished` with outcome `failed`, or an
  `agent.stopped` with `then=retry`) goes back to `planning` while attempts are left (FR9), else to `failed`;
- `agent_held`: `agent.stopped` with `then=hold` (the default). `task_resumed` returns to where the task was held;
- `dependency_escalated`: derived, a `pending` task behind an `escalated` or `blocked` task becomes `blocked`.

`call_for_edit` (D27, spec §3, §12) is never an attempt and never changes the attempt count:

- `attachment_missing`: the engine's check before filing found an attachment missing, `failed`/`rejected` →
  `call_for_edit`; then `edit_done` (the developer agent added it) → `escalated`;
- `edit_requested`: after filing, the user's question, `escalated` → `call_for_edit`; then `edit_done` (the answer is
  attached) → `escalated`.

A task is **filed** when it is `escalated`, or in `call_for_edit` entered from `escalated`. A filed task counts as
`escalated` for blocking and settling, so the requirement's outcome does not depend on when a question is asked.

Requirements settle by themselves: `all_approved`, `partly_approved` or `none_approved`. After that, task events
are illegal, except on a filed task: a repeated `escalation` record (the Jira key, a retried filing; spec §12) is a
no-op, and `edit_requested` / `edit_done` round trips are applied without unsettling the requirement.
Event types outside this vocabulary (`intake`, `agent_call`, `config_snapshot`, merge and deploy gates) are ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from ultron.config.defaults import MAX_ATTEMPTS_DEFAULT
from ultron.store.events import (
    ATTACHMENT_MISSING,
    ATTEMPT_FINISHED,
    ATTEMPT_STARTED,
    EDIT_DONE,
    EDIT_REQUESTED,
    ESCALATION,
    GATE_DECISION,
    Event,
)


class IllegalTransition(ValueError):
    """A trigger is not allowed in the current state."""


class RequirementState(StrEnum):
    SUBMITTED = "submitted"
    INTAKE_DRAFTING = "intake_drafting"
    AWAITING_INTENT = "awaiting_intent"
    SPEC_DRAFTING = "spec_drafting"
    AWAITING_SPEC = "awaiting_spec"
    SPEC_APPROVED = "spec_approved"
    DECOMPOSING = "decomposing"
    AWAITING_TASKS = "awaiting_tasks"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ESCALATED = "escalated"


class TaskState(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_PLAN = "awaiting_plan"
    PLAN_APPROVED = "plan_approved"
    CODING = "coding"
    TESTING = "testing"
    PASSED = "passed"
    AWAITING_RESULT = "awaiting_result"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    HELD = "held"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    CALL_FOR_EDIT = "call_for_edit"


R = RequirementState
T = TaskState

# --- the tables ---------------------------------------------------------------------------------------------------

REQUIREMENT_TRANSITIONS: Mapping[tuple[RequirementState, str], RequirementState] = (
    MappingProxyType(
        {
            (R.SUBMITTED, "intake_started"): R.INTAKE_DRAFTING,
            (
                R.SUBMITTED,
                "spec_started",
            ): R.SPEC_DRAFTING,  # template intent: no intake
            (R.INTAKE_DRAFTING, "intent_drafted"): R.AWAITING_INTENT,
            (R.AWAITING_INTENT, "intent.approve"): R.SPEC_DRAFTING,
            (
                R.AWAITING_INTENT,
                "intent.changes",
            ): R.INTAKE_DRAFTING,  # D28: the comment goes to the intake agent
            (R.SPEC_DRAFTING, "spec_drafted"): R.AWAITING_SPEC,
            (R.AWAITING_SPEC, "spec.changes"): R.SPEC_DRAFTING,
            (R.AWAITING_SPEC, "spec.approve"): R.SPEC_APPROVED,
            (R.SPEC_APPROVED, "decomposition_started"): R.DECOMPOSING,
            (R.DECOMPOSING, "tasks_drafted"): R.AWAITING_TASKS,
            (
                R.DECOMPOSING,
                "tasks.approve",
            ): R.IN_PROGRESS,  # auto gate: no awaiting_tasks
            (R.AWAITING_TASKS, "tasks.approve"): R.IN_PROGRESS,
            (R.IN_PROGRESS, "all_approved"): R.COMPLETE,
            (R.IN_PROGRESS, "partly_approved"): R.PARTIAL,
            (R.IN_PROGRESS, "none_approved"): R.ESCALATED,
        }
    )
)

_ESCALATABLE = (
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

TASK_TRANSITIONS: Mapping[tuple[TaskState, str], TaskState] = MappingProxyType(
    {
        (T.PENDING, "task_started"): T.PLANNING,
        (T.PENDING, "dependency_escalated"): T.BLOCKED,
        (T.PLANNING, "plan_drafted"): T.AWAITING_PLAN,
        (T.AWAITING_PLAN, "plan.changes"): T.PLANNING,
        (T.AWAITING_PLAN, "plan.approve"): T.PLAN_APPROVED,
        (T.PLAN_APPROVED, "attempt_started"): T.CODING,
        (T.CODING, "coding_finished"): T.TESTING,
        (T.TESTING, "attempt_passed"): T.PASSED,
        (
            T.CODING,
            "attempt_failed",
        ): T.PLANNING,  # a revised plan goes back through the plan gate
        (T.TESTING, "attempt_failed"): T.PLANNING,
        (T.CODING, "attempts_exhausted"): T.FAILED,
        (T.TESTING, "attempts_exhausted"): T.FAILED,
        (T.PASSED, "result_requested"): T.AWAITING_RESULT,
        (T.AWAITING_RESULT, "result.approve"): T.APPROVED,
        (T.AWAITING_RESULT, "result.reject"): T.REJECTED,
        (T.PLANNING, "agent_held"): T.HELD,
        (T.CODING, "agent_held"): T.HELD,
        (T.TESTING, "agent_held"): T.HELD,
        # rejected and failed lead to escalated; so do a stop with then=escalate (§9.1) and the cap (FR18)
        **{(state, "escalation"): T.ESCALATED for state in _ESCALATABLE},
        # D27: an attachment missing before filing, or the user's question after it; never an attempt
        (T.FAILED, ATTACHMENT_MISSING): T.CALL_FOR_EDIT,
        (T.REJECTED, ATTACHMENT_MISSING): T.CALL_FOR_EDIT,
        (T.ESCALATED, EDIT_REQUESTED): T.CALL_FOR_EDIT,
        (T.CALL_FOR_EDIT, EDIT_DONE): T.ESCALATED,
    }
)

_RESUMABLE = (T.PLANNING, T.CODING, T.TESTING)
_SETTLED_TASKS = frozenset({T.APPROVED, T.ESCALATED, T.BLOCKED})


def next_requirement_state(state: RequirementState, trigger: str) -> RequirementState:
    """The requirement state after `trigger`, or `IllegalTransition`."""
    try:
        return REQUIREMENT_TRANSITIONS[(state, trigger)]
    except KeyError:
        raise IllegalTransition(
            f"illegal transition: requirement in {state.value} cannot take {trigger!r}"
        ) from None


def next_task_state(
    state: TaskState, trigger: str, *, held_from: TaskState | None = None
) -> TaskState:
    """The task state after `trigger`, or `IllegalTransition`.

    `task_resumed` is legal only from `held`, and returns the task to `held_from` (where it stopped).
    """
    if trigger == "task_resumed":
        if state is T.HELD and held_from in _RESUMABLE:
            return held_from
        raise IllegalTransition(
            f"illegal transition: task in {state.value} cannot take 'task_resumed'"
        )
    try:
        return TASK_TRANSITIONS[(state, trigger)]
    except KeyError:
        raise IllegalTransition(
            f"illegal transition: task in {state.value} cannot take {trigger!r}"
        ) from None


def settle(task_states: Iterable[TaskState]) -> RequirementState | None:
    """How a requirement ends, or `None` while any task is unfinished (or there are none).

    `complete` when all tasks are approved, `partial` when some are approved and the rest escalated or blocked,
    `escalated` when none is approved.
    """
    states = list(task_states)
    if not states or any(s not in _SETTLED_TASKS for s in states):
        return None
    approved = sum(1 for s in states if s is T.APPROVED)
    if approved == len(states):
        trigger = "all_approved"
    elif approved:
        trigger = "partly_approved"
    else:
        trigger = "none_approved"
    return next_requirement_state(R.IN_PROGRESS, trigger)


# --- folding ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStatus:
    """One task as folded from its events."""

    state: TaskState
    attempt: int
    """The number of the last attempt started (0 before the first)."""
    max_attempts: int
    depends_on: tuple[str, ...] = ()
    held_from: TaskState | None = None
    edit_from: TaskState | None = None
    """In `call_for_edit`: the state it was entered from (`failed`, `rejected` or `escalated`)."""

    @property
    def filed(self) -> bool:
        """Escalated, or in `call_for_edit` after filing (the user's question, D27)."""
        return self.state is T.ESCALATED or (
            self.state is T.CALL_FOR_EDIT and self.edit_from is T.ESCALATED
        )

    @property
    def settled_state(self) -> TaskState:
        """The state blocking and settling see: a filed task counts as `escalated`."""
        return T.ESCALATED if self.filed else self.state


@dataclass(frozen=True)
class Snapshot:
    """A requirement's state and each of its tasks' state at the end of its events."""

    requirement: RequirementState
    tasks: dict[str, TaskStatus] = field(default_factory=dict)


_REQUIREMENT_TYPES = frozenset(
    {
        "intake_started",
        "intent_drafted",
        "spec_started",
        "spec_drafted",
        "decomposition_started",
        "tasks_drafted",
    }
)
_REQUIREMENT_GATES = frozenset({"intent", "spec", "tasks"})
_TASK_TYPES = frozenset(
    {
        "task_created",
        "task_started",
        "plan_drafted",
        "coding_finished",
        "result_requested",
        "task_resumed",
        "agent.stopped",
        ATTEMPT_STARTED,
        ATTEMPT_FINISHED,
        ESCALATION,
        ATTACHMENT_MISSING,
        EDIT_REQUESTED,
        EDIT_DONE,
    }
)
_TASK_GATES = frozenset({"plan", "result"})
_AFTER_FILING_TYPES = frozenset({ESCALATION, EDIT_REQUESTED, EDIT_DONE})
"""Events a filed task takes whatever the requirement's state (spec §12, D27)."""


def fold(events: Iterable[Event]) -> Snapshot:
    """Fold a requirement's events, in order, into its state. Raises `IllegalTransition` on a bad sequence.

    Pure: the same events always give an equal `Snapshot`. Raises `ValueError` if events of more than one
    requirement are mixed.
    """
    requirement = R.SUBMITTED
    tasks: dict[str, TaskStatus] = {}
    requirement_id: str | None = None
    for event in events:
        if requirement_id is None:
            requirement_id = event.requirement
        elif event.requirement != requirement_id:
            raise ValueError(
                f"events of two requirements: {requirement_id} and {event.requirement}"
            )
        requirement = _apply(event, requirement, tasks)
    return Snapshot(requirement, tasks)


def _apply(
    event: Event, requirement: RequirementState, tasks: dict[str, TaskStatus]
) -> RequirementState:
    gate_trigger: str | None = None
    if event.type == GATE_DECISION:
        gate, decision = event.data.get("gate"), event.data.get("decision")
        if not isinstance(gate, str) or not isinstance(decision, str):
            raise IllegalTransition("a gate_decision needs a gate and a decision")
        gate_trigger = f"{gate}.{decision}"
        if gate in _REQUIREMENT_GATES:
            return _settle(next_requirement_state(requirement, gate_trigger), tasks)
        if gate not in _TASK_GATES:
            return requirement  # merge, deploy, escalation and the like do not move state here
    elif event.type in _REQUIREMENT_TYPES:
        return _settle(next_requirement_state(requirement, event.type), tasks)
    elif event.type not in _TASK_TYPES:
        return requirement
    _apply_task(event, gate_trigger, requirement, tasks)
    return _settle(requirement, tasks)


def _settle(
    requirement: RequirementState, tasks: dict[str, TaskStatus]
) -> RequirementState:
    """Block tasks behind an escalated or blocked task, then settle the requirement if every task is done."""
    changed = True
    while changed:
        changed = False
        for name, status in tasks.items():
            if status.state is T.PENDING and any(
                dep in tasks and tasks[dep].settled_state in (T.ESCALATED, T.BLOCKED)
                for dep in status.depends_on
            ):
                tasks[name] = replace(
                    status,
                    state=next_task_state(status.state, "dependency_escalated"),
                )
                changed = True
    if requirement is R.IN_PROGRESS:
        outcome = settle(s.settled_state for s in tasks.values())
        if outcome is not None:
            return outcome
    return requirement


def _apply_task(
    event: Event,
    gate_trigger: str | None,
    requirement: RequirementState,
    tasks: dict[str, TaskStatus],
) -> None:
    name = event.task
    if name is None:
        if event.type == ESCALATION:
            return  # a requirement-level escalation record changes no task
        raise IllegalTransition(f"{event.type} needs a task")
    if event.type == "task_created":
        _create_task(event, name, requirement, tasks)
        return
    status = tasks.get(name)
    if status is not None and status.filed and event.type in _AFTER_FILING_TYPES:
        # These usually arrive after the requirement settled, so they apply whatever the requirement's state.
        if event.type == ESCALATION:
            # A later record of the same escalation (the Jira key, its sprint placement, a retried filing; spec
            # §12): a no-op, also while the task is in call_for_edit.
            return
        new_state = next_task_state(status.state, event.type)
        tasks[name] = replace(
            status, state=new_state, edit_from=_edit_from(status, new_state)
        )
        return
    if requirement is not R.IN_PROGRESS:
        raise IllegalTransition(
            f"illegal transition: {event.type} for {name} while the requirement is {requirement.value}"
        )
    if status is None:
        raise IllegalTransition(f"{event.type} for unknown task {name}")

    trigger = gate_trigger or event.type
    attempt = status.attempt
    held_from = status.held_from
    if event.type == ESCALATION:
        trigger = "escalation"
    elif event.type == ATTEMPT_FINISHED:
        outcome = event.data.get("outcome")
        if outcome == "stopped":
            return  # the agent.stopped event decides what happens
        if outcome == "passed":
            trigger = "attempt_passed"
        elif outcome == "failed":
            trigger = _failure(status)
        else:
            raise IllegalTransition(
                f"attempt_finished outcome {outcome!r} is not passed or failed"
            )
    elif event.type == "agent.stopped":
        then = event.data.get("then", "hold")
        if then == "hold":
            trigger, held_from = "agent_held", status.state
        elif then == "retry":
            trigger = _failure(status)
        elif then == "escalate":
            trigger = "escalation"
        else:
            raise IllegalTransition(
                f"agent.stopped then {then!r} is not retry, hold or escalate"
            )
    elif event.type == ATTEMPT_STARTED:
        attempt += 1
        if event.attempt is not None and event.attempt != attempt:
            raise IllegalTransition(
                f"{name} attempt {event.attempt} started after attempt {status.attempt}"
            )

    new_state = next_task_state(status.state, trigger, held_from=status.held_from)
    if trigger == "task_started":
        _check_dependencies(name, status, tasks)
    if new_state is not T.HELD:
        held_from = None
    tasks[name] = replace(
        status,
        state=new_state,
        attempt=attempt,
        held_from=held_from,
        edit_from=_edit_from(status, new_state),
    )


def _edit_from(status: TaskStatus, new_state: TaskState) -> TaskState | None:
    """Where a task entered `call_for_edit` from; kept while it stays there, cleared when it leaves."""
    if new_state is not T.CALL_FOR_EDIT:
        return None
    return status.edit_from if status.state is T.CALL_FOR_EDIT else status.state


def _failure(status: TaskStatus) -> str:
    return (
        "attempts_exhausted"
        if status.attempt >= status.max_attempts
        else "attempt_failed"
    )


def _check_dependencies(
    name: str, status: TaskStatus, tasks: Mapping[str, TaskStatus]
) -> None:
    for dep in status.depends_on:
        other = tasks.get(dep)
        if other is None:
            raise IllegalTransition(f"{name} depends on unknown task {dep}")
        if other.state is not T.APPROVED:
            raise IllegalTransition(
                f"{name} cannot start: {dep} is {other.state.value}, not approved"
            )


def _create_task(
    event: Event, name: str, requirement: RequirementState, tasks: dict[str, TaskStatus]
) -> None:
    if requirement not in (R.DECOMPOSING, R.AWAITING_TASKS):
        raise IllegalTransition(
            f"illegal transition: task_created for {name} while the requirement is {requirement.value}"
        )
    if name in tasks:
        raise IllegalTransition(f"task {name} is created twice")
    depends_on = event.data.get("depends_on", [])
    max_attempts = event.data.get("max_attempts", MAX_ATTEMPTS_DEFAULT)
    if not isinstance(depends_on, list) or not all(
        isinstance(d, str) for d in depends_on
    ):
        raise IllegalTransition(f"task {name}: depends_on must be a list of task ids")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 10
    ):
        raise IllegalTransition(f"task {name}: max_attempts must be 1 to 10")
    tasks[name] = TaskStatus(
        state=T.PENDING,
        attempt=0,
        max_attempts=max_attempts,
        depends_on=tuple(depends_on),
    )

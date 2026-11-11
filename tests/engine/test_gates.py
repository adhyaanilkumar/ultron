"""T10: gates — settings, snapshots, auto decisions and `decisions.md` (spec §8, FR4, FR16, D7, D8, NFR1)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ultron.config import build_config
from ultron.config.defaults import CHANGEABLE_GATES, LOCKED_GATES
from ultron.engine import states as t08
from ultron.engine.gates import (
    CONFIG_CHANGE,
    CONFIG_WARNING,
    GATES,
    GateChange,
    GateError,
    Gatekeeper,
    GateSettings,
    gate_reached,
    render_decisions,
)
from ultron.engine.states import (
    IllegalTransition,
    RequirementState,
    TaskState,
    fold,
)
from ultron.store.eventlog import EventLog
from ultron.store.events import (
    ATTACHMENT_MISSING,
    CONFIG_SNAPSHOT,
    EDIT_DONE,
    EDIT_REQUESTED,
    GATE_DECISION,
    NewEvent,
    attempt_finished,
    attempt_started,
    config_snapshot,
    escalation,
    gate_decision,
    user_actor,
)

R = RequirementState
T = TaskState
REQ = "R-20261111-ab12"
HUMAN = user_actor("Anil", "anil@example.com")
MIN: dict[str, Any] = {"commands": {"test": "make test"}}

# Spec §8, written by hand (not read from the code under test).
SPEC_TABLE: dict[str, dict[str, str]] = {
    "strict": {
        "intent": "human",
        "spec": "human",
        "tasks": "human",
        "plan": "human",
        "result": "human",
        "escalation": "human",
        "maintain": "human",
    },
    "default": {
        "intent": "human",
        "spec": "human",
        "tasks": "auto",
        "plan": "human",
        "result": "human",
        "escalation": "auto",
        "maintain": "auto",
    },
    "unattended": {
        "intent": "auto",
        "spec": "auto",
        "tasks": "auto",
        "plan": "auto",
        "result": "auto",
        "escalation": "auto",
        "maintain": "auto",
    },
}
ALL_GATES = (
    "intent",
    "spec",
    "tasks",
    "plan",
    "result",
    "escalation",
    "maintain",
    "merge",
    "deploy",
    "rollback",
)


def _clock() -> Iterator[datetime]:
    start = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    n = 0
    while True:
        yield start + timedelta(seconds=n)
        n += 1


def make_log(tmp_path: Path) -> EventLog:
    ticks = _clock()
    return EventLog(
        tmp_path / "events.jsonl",
        tmp_path / "events.lock",
        clock=lambda: next(ticks),
    )


def keeper(
    tmp_path: Path, gates: dict[str, Any] | None = None, **over: Any
) -> Gatekeeper:
    raw = {**MIN, **over}
    if gates is not None:
        raw["gates"] = gates
    gk = Gatekeeper(make_log(tmp_path), REQ)
    gk.record_snapshot(build_config(raw))
    return gk


def add(gk: Gatekeeper, type_: str, task: str | None = None, **data: Any) -> None:
    gk.log.append(
        NewEvent(actor="engine", type=type_, requirement=REQ, task=task, data=data)
    )


def events(gk: Gatekeeper) -> list[Any]:
    return gk.log.read(strict=True).events


def state(gk: Gatekeeper) -> RequirementState:
    return fold(events(gk)).requirement


def task_state(gk: Gatekeeper, task: str) -> TaskState:
    return fold(events(gk)).tasks[task].state


def to_awaiting_intent(gk: Gatekeeper) -> None:
    add(gk, "intake_started")
    add(gk, "intent_drafted")


def to_awaiting_spec(gk: Gatekeeper) -> None:
    to_awaiting_intent(gk)
    gk.decide("intent", REQ, "approve", "ok", HUMAN, "form")
    add(gk, "spec_drafted")


def to_in_progress(
    gk: Gatekeeper, tasks: dict[str, list[str]], max_attempts: int = 3
) -> None:
    to_awaiting_spec(gk)
    gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0")
    add(gk, "decomposition_started")
    for name, deps in tasks.items():
        add(gk, "task_created", name, depends_on=deps, max_attempts=max_attempts)
    add(gk, "tasks_drafted")
    gk.decide("tasks", REQ, "approve", "", HUMAN, "terminal")


def to_awaiting_plan(gk: Gatekeeper, task: str) -> None:
    add(gk, "task_started", task)
    add(gk, "plan_drafted", task)


def to_coding(gk: Gatekeeper, task: str, attempt: int = 1) -> None:
    if attempt == 1:
        to_awaiting_plan(gk, task)
    else:
        add(gk, "plan_drafted", task)
    gk.decide("plan", task, "approve", "", HUMAN, "form")
    gk.log.append(attempt_started(REQ, task, attempt))


def to_awaiting_result(gk: Gatekeeper, task: str) -> None:
    to_coding(gk, task)
    add(gk, "coding_finished", task)
    gk.log.append(attempt_finished(REQ, task, 1, outcome="passed"))
    add(gk, "result_requested", task)


def task_at(gk: Gatekeeper, target: TaskState) -> str:
    """Bring a task to `target` and return its id. T1 has one attempt; T2 depends on T1 (blocked once T1 escalates)."""
    to_in_progress(gk, {"T1": [], "T2": ["T1"]}, max_attempts=1)
    if target is T.PENDING:
        return "T1"
    if target is T.PLANNING:
        add(gk, "task_started", "T1")
    elif target is T.AWAITING_PLAN:
        to_awaiting_plan(gk, "T1")
    elif target is T.PLAN_APPROVED:
        to_awaiting_plan(gk, "T1")
        gk.decide("plan", "T1", "approve", "", HUMAN, "form")
    elif target is T.CODING:
        to_coding(gk, "T1")
    elif target is T.TESTING:
        to_coding(gk, "T1")
        add(gk, "coding_finished", "T1")
    elif target is T.PASSED:
        to_coding(gk, "T1")
        add(gk, "coding_finished", "T1")
        gk.log.append(attempt_finished(REQ, "T1", 1, outcome="passed"))
    elif target is T.AWAITING_RESULT:
        to_awaiting_result(gk, "T1")
    elif target is T.APPROVED:
        to_awaiting_result(gk, "T1")
        gk.decide("result", "T1", "approve", "", HUMAN, "form")
    elif target is T.REJECTED:
        to_awaiting_result(gk, "T1")
        gk.decide("result", "T1", "reject", "wrong approach", HUMAN, "form")
    elif target is T.HELD:
        to_coding(gk, "T1")
        add(gk, "agent.stopped", "T1", then="hold")
    else:  # failed, and what follows a failure
        to_coding(gk, "T1")
        add(gk, "coding_finished", "T1")
        gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
        if target is T.CALL_FOR_EDIT:
            add(gk, ATTACHMENT_MISSING, "T1", missing=["plan.md"])
        elif target in (T.ESCALATED, T.BLOCKED):
            gk.log.append(escalation(REQ, reason="all attempts failed", task="T1"))
        if target is T.BLOCKED:
            return "T2"
    return "T1"


AT_ESCALATION = (T.FAILED, T.REJECTED, T.CALL_FOR_EDIT, T.ESCALATED)
"""The states in which a task has reached the escalation gate (spec §3, §12, D27, D29)."""
NOT_AT_ESCALATION = (
    T.PENDING,
    T.PLANNING,
    T.AWAITING_PLAN,
    T.PLAN_APPROVED,
    T.CODING,
    T.TESTING,
    T.PASSED,
    T.AWAITING_RESULT,
    T.APPROVED,
    T.HELD,
    T.BLOCKED,
)


def test_task_states_split_into_reached_and_not_reached_escalation() -> None:
    assert set(AT_ESCALATION) | set(NOT_AT_ESCALATION) == set(TaskState)
    assert not set(AT_ESCALATION) & set(NOT_AT_ESCALATION)


# --- the gate table -----------------------------------------------------------------------------------------------


def test_gate_table_names_every_gate_and_matches_config() -> None:
    assert tuple(GATES) == ALL_GATES
    assert {g for g, info in GATES.items() if info.locked} == set(LOCKED_GATES)
    assert set(GATES) == set(CHANGEABLE_GATES) | set(LOCKED_GATES)


def test_gate_table_levels_and_drafting_states() -> None:
    assert GATES["intent"].drafting is R.INTAKE_DRAFTING
    assert GATES["spec"].drafting is R.SPEC_DRAFTING
    assert GATES["plan"].drafting is T.PLANNING
    assert {g for g, info in GATES.items() if info.level == "task"} == {
        "plan",
        "result",
        "escalation",
    }


# --- AC1: presets × gates; the snapshot is written at submit ------------------------------------------------------


@pytest.mark.parametrize("preset", sorted(SPEC_TABLE))
@pytest.mark.parametrize("gate", ALL_GATES)
def test_ac1_preset_gate_resolves_to_spec_table(
    tmp_path: Path, preset: str, gate: str
) -> None:
    gk = keeper(tmp_path, {"preset": preset})
    expected = SPEC_TABLE[preset].get(
        gate, "human"
    )  # merge, deploy, rollback: always human
    assert gk.settings().mode(gate) == expected
    snap = [e for e in events(gk) if e.type == CONFIG_SNAPSHOT]
    assert len(snap) == 1
    assert snap[0].data["gates"][gate] == expected


def test_ac1_snapshot_is_written_at_submit_with_max_attempts(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    gk = Gatekeeper(log, REQ)
    assert events(gk) == []
    written = gk.record_snapshot(build_config({**MIN, "max_attempts": 5}))
    assert written[0].type == CONFIG_SNAPSHOT
    assert written[0].seq == 1
    assert written[0].data["max_attempts"] == 5
    assert set(written[0].data["gates"]) == set(ALL_GATES)
    assert gk.settings().max_attempts == 5


def test_ac1_snapshot_applies_the_per_requirement_override(tmp_path: Path) -> None:
    gk = Gatekeeper(make_log(tmp_path), REQ)
    gk.record_snapshot(build_config(MIN, {"gates": {"plan": "auto"}}))
    assert gk.settings().mode("plan") == "auto"
    assert gk.settings().mode("spec") == "human"


def test_ac1_warnings_are_recorded_with_the_snapshot(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    warned = [e.data["gate"] for e in events(gk) if e.type == CONFIG_WARNING]
    assert warned == ["intent", "spec", "plan", "result"]


def test_ac1_snapshot_is_written_once(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    with pytest.raises(GateError):
        gk.record_snapshot(build_config(MIN))
    assert len(events(gk)) == 1


def test_ac1_settings_need_a_snapshot(tmp_path: Path) -> None:
    gk = Gatekeeper(make_log(tmp_path), REQ)
    with pytest.raises(GateError):
        gk.settings()


# --- AC2: mid-run changes -----------------------------------------------------------------------------------------


def test_ac2_change_before_the_gate_applies_and_is_recorded(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_intent(gk)
    event = gk.change_gate("spec", "auto", who=HUMAN)
    assert event.type == CONFIG_CHANGE
    assert event.actor == HUMAN
    assert event.data["gate"] == "spec"
    assert event.data["from"] == "human"
    assert event.data["to"] == "auto"
    assert event.data["reached"] is False
    assert event.data["warning"]  # a default-human gate went auto
    assert gk.settings().mode("spec") == "auto"


def test_ac2_change_after_the_gate_was_passed_does_not_reopen_it(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0")
    before = fold(events(gk))
    event = gk.change_gate("spec", "auto", who=HUMAN)
    assert event.data["reached"] is True
    assert fold(events(gk)) == before
    assert gk.settings().mode("spec") == "human"
    with pytest.raises((IllegalTransition, GateError)):
        gk.decide(
            "spec", REQ, "approve", "", "auto", "auto", "1.0.0", checks_passed=True
        )
    with pytest.raises(IllegalTransition):
        gk.decide("spec", REQ, "changes", "again", HUMAN, "form")


def test_ac2_change_at_a_waiting_gate_does_not_apply_to_it(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    gk.change_gate("spec", "auto", who=HUMAN)
    assert gk.settings().mode("spec") == "human"
    with pytest.raises(GateError):
        gk.decide(
            "spec", REQ, "approve", "", "auto", "auto", "1.0.0", checks_passed=True
        )


def test_ac2_task_gate_change_applies_only_to_tasks_before_it(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": [], "T2": []})
    to_awaiting_plan(gk, "T1")
    event = gk.change_gate("plan", "auto", who=HUMAN)
    assert event.data["reached_tasks"] == ["T1"]
    settings = gk.settings()
    assert settings.mode("plan", "T1") == "human"
    assert settings.mode("plan", "T2") == "auto"
    with pytest.raises(GateError):
        gk.decide("plan", "T1", "approve", "", "auto", "auto", checks_passed=True)
    to_awaiting_plan(gk, "T2")
    gk.decide("plan", "T2", "approve", "", "auto", "auto", checks_passed=True)
    assert task_state(gk, "T2") is T.PLAN_APPROVED


def test_ac2_change_to_human_applies_to_unreached_gates(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    gk.change_gate("tasks", "human", who=HUMAN)
    assert gk.settings().mode("tasks") == "human"
    change = next(e for e in events(gk) if e.type == CONFIG_CHANGE)
    assert change.data["warning"] is None


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac2_locked_gate_cannot_be_changed_to_auto(tmp_path: Path, gate: str) -> None:
    gk = keeper(tmp_path)
    count = len(events(gk))
    with pytest.raises(GateError):
        gk.change_gate(gate, "auto", who=HUMAN)
    assert len(events(gk)) == count


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac2_a_config_change_in_the_log_cannot_make_a_locked_gate_auto(
    tmp_path: Path, gate: str
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    # change_gate refuses this; written straight to the log, it must still not count
    gk.log.append(
        NewEvent(
            actor=HUMAN,
            type=CONFIG_CHANGE,
            requirement=REQ,
            data={"gate": gate, "from": "human", "to": "auto", "reached": False},
        )
    )
    settings = gk.settings()
    assert settings.mode(gate) == "human"
    assert settings.modes[gate] == "human"
    assert [c for c in settings.changes if c.gate == gate] == []
    before = gk.log.path.read_bytes()
    with pytest.raises(GateError):
        gk.decide(gate, REQ, "approve", "", "auto", "auto", checks_passed=True)
    assert gk.log.path.read_bytes() == before


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac2_gate_settings_keep_a_locked_gate_human_whatever_they_hold(
    gate: str,
) -> None:
    settings = GateSettings(
        max_attempts=3,
        modes=dict.fromkeys(ALL_GATES, "auto"),
        changes=(GateChange(gate, "auto", reached=False),),
    )
    assert settings.mode(gate) == "human"
    assert settings.mode("plan") == "auto"


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac1_a_snapshot_in_the_log_cannot_make_a_locked_gate_auto(
    tmp_path: Path, gate: str
) -> None:
    log = make_log(tmp_path)
    log.append(
        config_snapshot(REQ, max_attempts=3, gates=dict.fromkeys(ALL_GATES, "auto"))
    )
    settings = Gatekeeper(log, REQ).settings()
    assert settings.mode(gate) == "human"
    assert settings.modes[gate] == "human"


def test_ac1_a_snapshot_mode_that_is_not_human_or_auto_reads_human(
    tmp_path: Path,
) -> None:
    log = make_log(tmp_path)
    log.append(  # the snapshot builder refuses this; written raw, it must still read safe
        NewEvent(
            actor="engine",
            type=CONFIG_SNAPSHOT,
            requirement=REQ,
            data={
                "max_attempts": 3,
                "gates": {**dict.fromkeys(ALL_GATES, "auto"), "plan": "sometimes"},
            },
        )
    )
    assert Gatekeeper(log, REQ).settings().mode("plan") == "human"


@pytest.mark.parametrize(
    ("actor", "gate", "to"),
    [
        ("engine", "plan", "auto"),
        ("agent:developer", "plan", "auto"),
        ("auto", "plan", "auto"),
        (HUMAN, "plan", "sometimes"),
        (HUMAN, "bogus", "auto"),
    ],
)
def test_ac2_settings_ignore_a_change_change_gate_could_not_write(
    tmp_path: Path, actor: str, gate: str, to: str
) -> None:
    gk = keeper(tmp_path)  # plan is human
    gk.log.append(
        NewEvent(
            actor=actor,
            type=CONFIG_CHANGE,
            requirement=REQ,
            data={"gate": gate, "from": "human", "to": to, "reached": False},
        )
    )
    settings = gk.settings()
    assert settings.mode("plan") == "human"
    assert settings.changes == ()


@pytest.mark.parametrize("who", ["auto", "engine", "agent:developer"])
def test_ac2_only_a_human_changes_settings(tmp_path: Path, who: str) -> None:
    gk = keeper(tmp_path)
    with pytest.raises(GateError):
        gk.change_gate("plan", "auto", who=who)


def test_ac2_unknown_gate_or_mode(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    with pytest.raises(GateError):
        gk.change_gate("bogus", "auto", who=HUMAN)
    with pytest.raises(GateError):
        gk.change_gate("plan", "sometimes", who=HUMAN)


# --- AC3: auto refused on locked gates and on failing tasks; agents never decide ----------------------------------


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac3_auto_on_locked_gate_raises(tmp_path: Path, gate: str) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []})
    count = len(events(gk))
    with pytest.raises(GateError):
        gk.decide(gate, REQ, "approve", "", "auto", "auto", checks_passed=True)
    assert len(events(gk)) == count


@pytest.mark.parametrize("gate", LOCKED_GATES)
@pytest.mark.parametrize("who", ["engine", "agent:developer", "agent:maintenance"])
def test_ac3_locked_gate_needs_a_human_identity(
    tmp_path: Path, gate: str, who: str
) -> None:
    gk = keeper(tmp_path)
    with pytest.raises(GateError):
        gk.decide(gate, REQ, "approve", "", who, "terminal")


@pytest.mark.parametrize("gate", LOCKED_GATES)
def test_ac3_locked_gate_takes_a_human_decision(tmp_path: Path, gate: str) -> None:
    gk = keeper(tmp_path)
    event = gk.decide(gate, REQ, "approve", "ship it", HUMAN, "terminal")
    assert event.actor == HUMAN
    assert event.data["gate"] == gate


@pytest.mark.parametrize("who", ["engine", "agent:developer", "agent:testing"])
def test_ac3_agents_never_pass_any_gate(tmp_path: Path, who: str) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_spec(gk)
    with pytest.raises(GateError):
        gk.decide("spec", REQ, "approve", "", who, "form", "1.0.0")
    assert state(gk) is R.AWAITING_SPEC


def test_ac3_auto_on_a_task_whose_attempt_failed_raises(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []})
    add(gk, "task_started", "T1")
    add(gk, "plan_drafted", "T1")
    gk.decide("plan", "T1", "approve", "", "auto", "auto", checks_passed=True)
    gk.log.append(attempt_started(REQ, "T1", 1))
    gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
    count = len(events(gk))
    with pytest.raises(GateError):
        gk.decide("result", "T1", "approve", "", "auto", "auto", checks_passed=True)
    assert len(events(gk)) == count


def test_ac3_auto_on_a_failed_task_raises(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []}, max_attempts=1)
    to_coding(gk, "T1")
    add(gk, "coding_finished", "T1")
    gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
    assert task_state(gk, "T1") is T.FAILED
    for gate in ("plan", "result"):
        with pytest.raises((GateError, IllegalTransition)):
            gk.decide(gate, "T1", "approve", "", "auto", "auto", checks_passed=True)


def test_ac3_auto_needs_the_stage_checks_to_have_passed(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_intent(gk)
    with pytest.raises(GateError):
        gk.decide("intent", REQ, "approve", "", "auto", "auto")
    gk.decide("intent", REQ, "approve", "", "auto", "auto", checks_passed=True)
    assert state(gk) is R.SPEC_DRAFTING


def test_ac3_auto_needs_the_gate_set_to_auto(tmp_path: Path) -> None:
    gk = keeper(tmp_path)  # default: intent is human
    to_awaiting_intent(gk)
    with pytest.raises(GateError):
        gk.decide("intent", REQ, "approve", "", "auto", "auto", checks_passed=True)


@pytest.mark.parametrize("decision", ["changes", "reject"])
def test_ac3_auto_only_approves(tmp_path: Path, decision: str) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_spec(gk)
    with pytest.raises(GateError):
        gk.decide("spec", REQ, decision, "x", "auto", "auto", checks_passed=True)


def test_ac3_auto_result_passes_a_passing_task(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []})
    to_awaiting_result(gk, "T1")
    gk.decide("result", "T1", "approve", "", "auto", "auto", checks_passed=True)
    assert task_state(gk, "T1") is T.APPROVED


def test_ac3_auto_escalation_is_allowed_for_a_rejected_task(tmp_path: Path) -> None:
    gk = keeper(tmp_path)  # escalation is auto by default
    to_in_progress(gk, {"T1": []})
    to_awaiting_result(gk, "T1")
    gk.decide("result", "T1", "reject", "wrong approach", HUMAN, "form")
    event = gk.decide(
        "escalation", "T1", "approve", "", "auto", "auto", checks_passed=True
    )
    assert event.actor == "auto"


@pytest.mark.parametrize("target", NOT_AT_ESCALATION)
@pytest.mark.parametrize(
    ("who", "decision"),
    [("auto", "approve"), (HUMAN, "approve"), (HUMAN, "reject")],
)
def test_ac3_escalation_decision_is_refused_before_the_task_reaches_the_gate(
    tmp_path: Path, target: TaskState, who: str, decision: str
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})  # escalation is auto
    task = task_at(gk, target)
    assert task_state(gk, task) is target
    assert gate_reached(events(gk), "escalation", task) is False
    channel = "auto" if who == "auto" else "form"
    before = gk.log.path.read_bytes()
    with pytest.raises(GateError):
        gk.decide("escalation", task, decision, "why", who, channel, checks_passed=True)
    assert gk.log.path.read_bytes() == before


@pytest.mark.parametrize("target", AT_ESCALATION)
@pytest.mark.parametrize("who", ["auto", HUMAN])
def test_ac3_escalation_decision_is_taken_once_the_task_reaches_the_gate(
    tmp_path: Path, target: TaskState, who: str
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    task = task_at(gk, target)
    assert task_state(gk, task) is target
    assert gate_reached(events(gk), "escalation", task) is True
    channel = "auto" if who == "auto" else "terminal"
    event = gk.decide(
        "escalation", task, "approve", "", who, channel, checks_passed=True
    )
    assert event.actor == who
    assert event.task == task
    assert event.data["gate"] == "escalation"


def test_ac2_an_escalation_decision_on_an_unreached_gate_does_not_count_as_reached(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)  # escalation is auto by default
    to_in_progress(gk, {"T1": []}, max_attempts=1)
    to_awaiting_plan(gk, "T1")
    # decide refuses this; written straight to the log, it must not freeze T1's mode
    gk.log.append(
        gate_decision(
            REQ,
            actor="auto",
            gate="escalation",
            decision="approve",
            channel="auto",
            task="T1",
        )
    )
    assert gate_reached(events(gk), "escalation", "T1") is False
    change = gk.change_gate("escalation", "human", who=HUMAN)
    assert change.data["reached_tasks"] == []
    assert gk.settings().mode("escalation", "T1") == "human"
    # T1 now fails: it reaches the gate under the new (human) mode
    gk.decide("plan", "T1", "approve", "", HUMAN, "form")
    gk.log.append(attempt_started(REQ, "T1", 1))
    gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
    assert task_state(gk, "T1") is T.FAILED
    with pytest.raises(GateError):
        gk.decide("escalation", "T1", "approve", "", "auto", "auto", checks_passed=True)
    gk.decide("escalation", "T1", "approve", "file it", HUMAN, "form")


@pytest.mark.parametrize(
    ("who", "channel"),
    [("auto", "form"), (HUMAN, "auto"), (HUMAN, "chat"), (HUMAN, "")],
)
def test_ac3_channel_must_match_who(tmp_path: Path, who: str, channel: str) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_intent(gk)
    with pytest.raises(GateError):
        gk.decide("intent", REQ, "approve", "", who, channel, checks_passed=True)


def test_ac3_bad_gate_decision_and_item(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": []})
    to_awaiting_plan(gk, "T1")
    with pytest.raises(GateError):
        gk.decide("bogus", REQ, "approve", "", HUMAN, "form")
    with pytest.raises(GateError):
        gk.decide("plan", "T1", "maybe", "", HUMAN, "form")
    with pytest.raises(GateError):
        gk.decide("plan", "T9", "approve", "", HUMAN, "form")  # unknown task
    with pytest.raises(GateError):
        gk.decide("plan", REQ, "approve", "", HUMAN, "form")  # a task gate needs a task
    with pytest.raises(GateError):
        gk.decide("merge", "T1", "approve", "", HUMAN, "form")  # a requirement gate
    with pytest.raises(GateError):
        gk.decide(
            "plan", "T1", "approve", "", HUMAN, "form", "1.0.0"
        )  # version: spec only


def test_ac3_waiting_at_a_gate_is_not_an_attempt(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": []})
    to_awaiting_plan(gk, "T1")
    for n in range(3):
        gk.decide("plan", "T1", "changes", f"round {n}", HUMAN, "form")
        add(gk, "plan_drafted", "T1")
    gk.decide("plan", "T1", "approve", "", HUMAN, "form")
    assert fold(events(gk)).tasks["T1"].attempt == 0


# --- AC4: the fields of every decision event ----------------------------------------------------------------------


def test_ac4_every_decision_event_has_its_fields(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": []})
    to_awaiting_result(gk, "T1")
    gk.decide("result", "T1", "approve", "looks right", HUMAN, "terminal")
    gk.decide("merge", REQ, "approve", "merge it", HUMAN, "form")
    decisions = [e for e in events(gk) if e.type == GATE_DECISION]
    assert [d.data["gate"] for d in decisions] == [
        "intent",
        "spec",
        "tasks",
        "plan",
        "result",
        "merge",
    ]
    for d in decisions:
        assert d.actor in ("auto", HUMAN)
        assert d.time.endswith("Z")
        assert d.data["channel"] in ("form", "terminal", "auto")
        assert d.data["decision"] in ("approve", "changes", "reject")
        assert isinstance(d.data["comments"], str)
    by_gate = {d.data["gate"]: d for d in decisions}
    assert by_gate["intent"].data["comments"] == "ok"
    assert by_gate["result"].data["comments"] == "looks right"
    assert by_gate["result"].data["channel"] == "terminal"
    assert by_gate["result"].task == "T1"
    assert by_gate["result"].attempt == 1
    assert by_gate["spec"].data["version"] == "1.0.0"
    assert by_gate["tasks"].data["version"] is None


def test_ac4_auto_decision_is_recorded_as_auto(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_intent(gk)
    event = gk.decide("intent", REQ, "approve", "", "auto", "auto", checks_passed=True)
    assert event.actor == "auto"
    assert event.data["channel"] == "auto"


def test_ac4_spec_approve_needs_a_valid_version(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    with pytest.raises(GateError):
        gk.decide("spec", REQ, "approve", "", HUMAN, "form")
    with pytest.raises(GateError):
        gk.decide("spec", REQ, "approve", "", HUMAN, "form", "v1.2")
    event = gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.2.0")
    assert event.data["version"] == "1.2.0"


# --- AC5: decisions.md --------------------------------------------------------------------------------------------


def test_ac5_decisions_md_lists_every_decision_in_order_and_is_stable(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": []})
    to_awaiting_plan(gk, "T1")
    gk.decide("plan", "T1", "changes", "split | this\nand that", HUMAN, "form")
    add(gk, "plan_drafted", "T1")
    gk.decide("plan", "T1", "approve", "", HUMAN, "terminal")
    out = tmp_path / "decisions.md"
    gk.write_decisions(out)
    first = out.read_bytes()
    gk.write_decisions(out)
    assert out.read_bytes() == first
    assert b"\r\n" not in first
    text = first.decode("utf-8")
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("| ") and "---" not in line
    ][1:]
    decisions = [e for e in events(gk) if e.type == GATE_DECISION]
    assert len(rows) == len(decisions) == 5
    for row, d in zip(rows, decisions, strict=True):
        assert row.startswith(f"| {d.seq} |")
        assert f"| {d.data['gate']} |" in row
        assert f"| {d.data['decision']} |" in row
    assert "split \\| this<br>and that" in text
    assert "1.0.0" in text
    assert render_decisions(events(gk), REQ) == text


def test_ac5_decisions_md_with_no_decisions(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    text = render_decisions(events(gk), REQ)
    assert REQ in text
    assert "No decisions yet." in text
    assert text.endswith("\n")


def test_ac5_regenerating_from_a_reread_log_is_byte_identical(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    a = render_decisions(events(gk), REQ).encode("utf-8")
    again = EventLog(gk.log.path, gk.log.lock_path).read(strict=True).events
    assert render_decisions(again, REQ).encode("utf-8") == a


# --- AC6: request_changes returns the item to its drafting state with the comment ---------------------------------


def test_ac6_spec_changes_returns_to_spec_drafting(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    new_state = gk.request_changes("spec", REQ, "cover Linux too", HUMAN, "form")
    assert new_state is R.SPEC_DRAFTING
    assert state(gk) is R.SPEC_DRAFTING
    assert gk.feedback("spec", REQ) == "cover Linux too"
    add(gk, "spec_drafted")
    assert gk.feedback("spec", REQ) is None  # consumed by the next draft


def test_ac6_plan_changes_returns_to_planning(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": [], "T2": []})
    to_awaiting_plan(gk, "T1")
    to_awaiting_plan(gk, "T2")
    new_state = gk.request_changes(
        "plan", "T1", "add a rollback step", HUMAN, "terminal"
    )
    assert new_state is T.PLANNING
    assert task_state(gk, "T1") is T.PLANNING
    assert task_state(gk, "T2") is T.AWAITING_PLAN
    assert gk.feedback("plan", "T1") == "add a rollback step"
    assert gk.feedback("plan", "T2") is None
    add(gk, "plan_drafted", "T1")
    assert gk.feedback("plan", "T1") is None


def test_ac6_intent_changes_returns_to_intake_drafting(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_intent(gk)
    new_state = gk.request_changes("intent", REQ, "it's about Windows", HUMAN, "form")
    assert new_state is R.INTAKE_DRAFTING
    assert gk.feedback("intent", REQ) == "it's about Windows"
    add(gk, "intent_drafted")
    assert gk.feedback("intent", REQ) is None  # consumed by the next draft


def test_ac6_changes_needs_a_comment(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    with pytest.raises(GateError):
        gk.request_changes("spec", REQ, "  ", HUMAN, "form")


def test_ac6_changes_on_a_gate_without_drafting_state_raises(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_in_progress(gk, {"T1": []})
    to_awaiting_result(gk, "T1")
    with pytest.raises((GateError, IllegalTransition)):
        gk.request_changes("result", "T1", "redo", HUMAN, "form")


# ==================================================================================================================
# Attempt 3: a value of the wrong type anywhere in the log (or from a caller) never crashes a read
# ==================================================================================================================

NOT_STRINGS: list[Any] = [
    ["auto"],
    [],
    {"mode": "auto"},
    {},
    0,
    1,
    2.5,
    True,
    False,
    None,
]
"""JSON values that are not strings: lists, objects, numbers, bools and null."""
NOT_BOOLS: list[Any] = [
    ["x"],
    [],
    {"reached": True},
    {},
    0,
    1,
    0.0,
    "false",
    "true",
    "",
    None,
]
NOT_TASK_LISTS: list[Any] = [
    {"T1": True},
    {},
    0,
    1,
    2.5,
    True,
    False,
    None,
    "T1",
    "",
    [1],
    [None],
    [True],
    [["T1"]],
    [{"task": "T1"}],
    [""],
    ["T1", 2],
]
NOT_COUNTS: list[Any] = [
    ["3"],
    [3],
    {"n": 3},
    {},
    2.5,
    3.0,
    True,
    False,
    None,
    "3",
    "",
    0,
    -1,
]


class _Absent:
    def __repr__(self) -> str:
        return "absent"


ABSENT = _Absent()
"""Marks a field left out of a forged event."""
GOOD_CHANGE: dict[str, Any] = {
    "gate": "plan",
    "from": "human",
    "to": "auto",
    "reached": False,
    "reached_tasks": [],
    "warning": None,
}
"""A change `change_gate` could have written: plan to auto, nothing reached."""
CHANGE_FIELD_VALUES: list[tuple[str, Any]] = [
    *(("gate", v) for v in NOT_STRINGS),
    *(("to", v) for v in NOT_STRINGS),
    *(("reached", v) for v in NOT_BOOLS),
    *(("reached_tasks", v) for v in NOT_TASK_LISTS),
]


def raw_keeper(tmp_path: Path, gates: object, max_attempts: object = 3) -> Gatekeeper:
    """A requirement whose submit snapshot was written without T05's builder checks (any value in any field)."""
    data: dict[str, Any] = {"gates": gates}
    if max_attempts is not ABSENT:
        data["max_attempts"] = max_attempts
    gk = Gatekeeper(make_log(tmp_path), REQ)
    gk.log.append(
        NewEvent(actor="engine", type=CONFIG_SNAPSHOT, requirement=REQ, data=data)
    )
    return gk


def raw_change(gk: Gatekeeper, actor: str = HUMAN, **data: Any) -> None:
    """A `config_change` written straight to the log, as a hand edit would be."""
    gk.log.append(NewEvent(actor=actor, type=CONFIG_CHANGE, requirement=REQ, data=data))


def refused(
    gk: Gatekeeper,
    call: Callable[[], object],
    error: type[Exception] = GateError,
    match: str | None = None,
) -> None:
    """`call` raises `error` (a TypeError fails the test) and the log's bytes do not change."""
    before = gk.log.path.read_bytes()
    with pytest.raises(error, match=match):
        call()
    assert gk.log.path.read_bytes() == before


def auto_plan(gk: Gatekeeper, task: str) -> Any:
    return gk.decide("plan", task, "approve", "", "auto", "auto", checks_passed=True)


# --- the snapshot -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", NOT_STRINGS, ids=repr)
@pytest.mark.parametrize("gate", ALL_GATES)
def test_a3_a_snapshot_mode_that_is_not_a_string_reads_human(
    tmp_path: Path, gate: str, value: Any
) -> None:
    gk = raw_keeper(tmp_path, {**dict.fromkeys(ALL_GATES, "auto"), gate: value})
    settings = gk.settings()  # no TypeError
    assert settings.modes[gate] == "human"
    assert settings.mode(gate) == "human"
    assert settings.mode(gate, "T1") == "human"
    for other in CHANGEABLE_GATES:  # the odd value does not spread to the other gates
        if other != gate:
            assert settings.mode(other) == "auto", other


@pytest.mark.parametrize(
    "value", [*NOT_STRINGS, "auto", "human", "plan=auto"], ids=repr
)
def test_a3_a_snapshot_gates_value_of_another_shape_reads_every_gate_human(
    tmp_path: Path, value: Any
) -> None:
    settings = raw_keeper(tmp_path, value).settings()
    assert {g: settings.mode(g) for g in ALL_GATES} == dict.fromkeys(ALL_GATES, "human")


@pytest.mark.parametrize("value", [*NOT_COUNTS, ABSENT], ids=repr)
def test_a3_a_max_attempts_that_is_not_a_positive_integer_reads_the_default(
    tmp_path: Path, value: Any
) -> None:
    gk = raw_keeper(tmp_path, dict.fromkeys(ALL_GATES, "auto"), max_attempts=value)
    settings = gk.settings()  # no KeyError, TypeError or ValueError
    assert settings.max_attempts == 3  # spec D7: the default
    assert settings.mode("plan") == "auto"  # the rest of the snapshot still reads
    to_in_progress(gk, {"T1": []})
    to_awaiting_plan(gk, "T1")
    assert auto_plan(gk, "T1").actor == "auto"  # auto decisions read the settings too


@pytest.mark.parametrize("value", [1, 2, 10, 11])
def test_a3_a_positive_integer_max_attempts_is_kept(tmp_path: Path, value: int) -> None:
    assert raw_keeper(tmp_path, {}, max_attempts=value).settings().max_attempts == value


@pytest.mark.parametrize("value", NOT_STRINGS, ids=repr)
def test_a3_auto_decisions_and_changes_work_beside_an_odd_snapshot_mode(
    tmp_path: Path, value: Any
) -> None:
    gk = raw_keeper(tmp_path, {**dict.fromkeys(ALL_GATES, "auto"), "intent": value})
    to_awaiting_intent(gk)
    refused(
        gk,
        lambda: gk.decide(
            "intent", REQ, "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    change = gk.change_gate("intent", "auto", who=HUMAN)
    assert change.data["from"] == "human"
    assert change.data["reached"] is True  # waiting at the gate: it keeps human
    gk.decide("intent", REQ, "approve", "", HUMAN, "form")
    add(gk, "spec_drafted")
    gk.decide("spec", REQ, "approve", "", "auto", "auto", "1.0.0", checks_passed=True)
    assert state(gk) is R.SPEC_APPROVED


# --- changes --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), CHANGE_FIELD_VALUES, ids=repr)
def test_a3_a_change_with_a_field_of_another_type_is_skipped(
    tmp_path: Path, field: str, value: Any
) -> None:
    gk = keeper(tmp_path, {"preset": "strict"})  # every changeable gate human
    to_in_progress(gk, {"T1": []})
    raw_change(gk, **{**GOOD_CHANGE, field: value})
    settings = gk.settings()  # no TypeError
    assert settings.changes == ()
    assert settings.mode("plan") == "human"
    assert settings.mode("plan", "T1") == "human"
    assert gk.change_gate("result", "auto", who=HUMAN).data["to"] == "auto"
    to_awaiting_plan(gk, "T1")
    refused(gk, lambda: auto_plan(gk, "T1"))
    gk.decide("plan", "T1", "approve", "", HUMAN, "form")


@pytest.mark.parametrize(
    ("field", "value"),
    [(f, v) for f, v in CHANGE_FIELD_VALUES if f in ("reached", "reached_tasks")],
    ids=repr,
)
def test_a3_a_skipped_change_to_human_leaves_an_auto_gate_auto(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Skipped means the change never counts, not that it reads human."""
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []})
    raw_change(gk, **{**GOOD_CHANGE, "from": "auto", "to": "human", field: value})
    assert gk.settings().changes == ()
    assert gk.settings().mode("plan", "T1") == "auto"
    to_awaiting_plan(gk, "T1")
    assert auto_plan(gk, "T1").actor == "auto"


@pytest.mark.parametrize("left_out", ["reached", "reached_tasks", "from", "warning"])
def test_a3_a_change_without_an_optional_field_still_applies(
    tmp_path: Path, left_out: str
) -> None:
    gk = keeper(tmp_path, {"preset": "strict"})
    to_in_progress(gk, {"T1": []})
    raw_change(gk, **{k: v for k, v in GOOD_CHANGE.items() if k != left_out})
    assert gk.settings().mode("plan", "T1") == "auto"
    to_awaiting_plan(gk, "T1")
    assert auto_plan(gk, "T1").actor == "auto"


def test_a3_settings_read_past_every_malformed_change(tmp_path: Path) -> None:
    gk = keeper(tmp_path, {"preset": "strict"})
    to_in_progress(gk, {"T1": []})
    for field, value in CHANGE_FIELD_VALUES:
        raw_change(gk, **{**GOOD_CHANGE, field: value})
    gk.change_gate("plan", "auto", who=HUMAN)  # the one change change_gate wrote
    settings = gk.settings()
    assert [(c.gate, c.to, c.reached, c.reached_tasks) for c in settings.changes] == [
        ("plan", "auto", False, ())
    ]
    to_awaiting_plan(gk, "T1")
    assert auto_plan(gk, "T1").actor == "auto"


# --- decision events ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", NOT_STRINGS, ids=repr)
def test_a3_a_decision_event_with_odd_fields_never_crashes_a_read(
    tmp_path: Path, value: Any
) -> None:
    gk = keeper(tmp_path)
    to_awaiting_intent(gk)
    gk.log.append(
        NewEvent(
            actor=HUMAN,
            type=GATE_DECISION,
            requirement=REQ,
            data={
                "gate": "intent",
                "decision": "changes",
                "channel": value,
                "comments": value,
                "version": value,
            },
        )
    )
    assert state(gk) is R.INTAKE_DRAFTING  # T08 takes it
    assert (
        gk.feedback("intent", REQ) == ""
    )  # changes were asked; the comment is not text
    assert gk.settings().mode("intent") == "human"
    assert gate_reached(events(gk), "intent")
    assert "| intent | " in gk.render_decisions()
    add(gk, "intent_drafted")
    assert gk.feedback("intent", REQ) is None


@pytest.mark.parametrize("field", ["gate", "decision"])
@pytest.mark.parametrize("value", NOT_STRINGS, ids=repr)
def test_a3_a_decision_t08_cannot_fold_leaves_settings_readable_and_decide_closed(
    tmp_path: Path, field: str, value: Any
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_intent(gk)
    data = {
        "gate": "intent",
        "decision": "approve",
        "channel": "form",
        "comments": "",
        "version": None,
        field: value,
    }
    gk.log.append(NewEvent(actor=HUMAN, type=GATE_DECISION, requirement=REQ, data=data))
    assert gk.settings().mode("intent") == "auto"  # settings never fold
    assert gk.feedback("intent", REQ) is None
    assert REQ in gk.render_decisions()
    refused(
        gk,
        lambda: gk.decide(
            "intent", REQ, "approve", "", "auto", "auto", checks_passed=True
        ),
        IllegalTransition,
    )
    refused(gk, lambda: gk.change_gate("spec", "human", who=HUMAN), IllegalTransition)


# --- values from callers --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", NOT_STRINGS, ids=repr)
def test_a3_decide_refuses_a_value_of_the_wrong_type_with_a_gate_error(
    tmp_path: Path, bad: Any
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_in_progress(gk, {"T1": []})
    to_awaiting_plan(gk, "T1")
    refused(gk, lambda: gk.decide(bad, "T1", "approve", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("plan", bad, "approve", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("plan", "T1", bad, "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("plan", "T1", "approve", bad, HUMAN, "form"))
    refused(gk, lambda: gk.decide("plan", "T1", "approve", "", bad, "form"))
    refused(gk, lambda: gk.decide("plan", "T1", "approve", "", HUMAN, bad))
    refused(gk, lambda: gk.decide("plan", "T1", "approve", "", bad, "auto"))
    refused(gk, lambda: gk.decide("intent", bad, "approve", "", HUMAN, "form"))
    assert auto_plan(gk, "T1").actor == "auto"  # control


@pytest.mark.parametrize(
    "checks", [1, 2.5, "yes", "false", ["x"], {"ok": True}, None, 0]
)
def test_a3_auto_needs_checks_passed_to_be_exactly_true(
    tmp_path: Path, checks: Any
) -> None:
    gk = keeper(tmp_path, {"preset": "unattended"})
    to_awaiting_intent(gk)
    refused(
        gk,
        lambda: gk.decide(
            "intent", REQ, "approve", "", "auto", "auto", checks_passed=checks
        ),
    )
    gk.decide("intent", REQ, "approve", "", "auto", "auto", checks_passed=True)


@pytest.mark.parametrize("bad", [["1.0.0"], {"v": "1.0.0"}, 1, 1.0, True])
def test_a3_a_version_that_is_not_a_string_is_refused(tmp_path: Path, bad: Any) -> None:
    gk = keeper(tmp_path)
    to_awaiting_spec(gk)
    refused(gk, lambda: gk.decide("spec", REQ, "approve", "", HUMAN, "form", bad))
    refused(gk, lambda: gk.decide("spec", REQ, "changes", "more", HUMAN, "form", bad))
    refused(gk, lambda: gk.decide("spec", REQ, "reject", "no", HUMAN, "form", bad))
    event = gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0")
    assert event.data["version"] == "1.0.0"


@pytest.mark.parametrize("bad", NOT_STRINGS, ids=repr)
def test_a3_change_gate_refuses_a_value_of_the_wrong_type(
    tmp_path: Path, bad: Any
) -> None:
    gk = keeper(tmp_path)
    refused(gk, lambda: gk.change_gate("plan", bad, who=HUMAN))
    refused(gk, lambda: gk.change_gate(bad, "auto", who=HUMAN))
    refused(gk, lambda: gk.change_gate("plan", "auto", who=bad))
    assert gk.change_gate("plan", "auto", who=HUMAN).data["to"] == "auto"  # control


@pytest.mark.parametrize("bad", NOT_STRINGS, ids=repr)
def test_a3_reads_take_an_item_of_any_type(tmp_path: Path, bad: Any) -> None:
    gk = keeper(tmp_path)
    task = task_at(gk, T.FAILED)
    log = events(gk)
    assert gate_reached(log, "escalation", bad) is False
    assert gate_reached(log, "plan", bad) is False
    assert gk.feedback("plan", bad) is None
    assert gate_reached(log, "escalation", task) is True  # control


# --- settings built by hand -----------------------------------------------------------------------------------------


def test_a3_hand_built_settings_read_any_other_type_as_human() -> None:
    modes: dict[str, Any] = {
        "intent": ["auto"],
        "spec": {"mode": "auto"},
        "tasks": None,
        "plan": 1,
        "result": True,
        "escalation": "auto",
    }
    odd = GateSettings(
        max_attempts=3,
        modes=modes,
        changes=(
            GateChange("maintain", ["auto"], False),  # type: ignore[arg-type]
            GateChange("escalation", {"to": "auto"}, False),  # type: ignore[arg-type]
        ),
    )
    for gate in ("intent", "spec", "tasks", "plan", "result", "maintain", "escalation"):
        assert odd.mode(gate) == "human", gate
        assert odd.mode(gate, "T1") == "human", gate


def test_a3_hand_built_changes_with_odd_reached_fields_are_skipped() -> None:
    settings = GateSettings(
        max_attempts=3,
        modes={"plan": "auto", "spec": "auto"},
        changes=(
            GateChange("plan", "human", False, None),  # type: ignore[arg-type]
            GateChange("plan", "human", False, "T1"),  # type: ignore[arg-type]
            GateChange("plan", "human", False, (1,)),  # type: ignore[arg-type]
            GateChange("plan", "human", False, ["T2"]),  # type: ignore[arg-type]
            GateChange("spec", "human", "false"),  # type: ignore[arg-type]
            GateChange("spec", "human", None),  # type: ignore[arg-type]
            GateChange("spec", "human", 0),  # type: ignore[arg-type]
        ),
    )
    assert settings.mode("plan") == "auto"
    assert settings.mode("plan", "T1") == "auto"
    assert settings.mode("plan", "T2") == "auto"
    assert settings.mode("spec") == "auto"


# ==================================================================================================================
# D30 (owner, 2026-11-11): one decision per escalation gate
# ==================================================================================================================

ESCALATION_DECISIONS: list[tuple[str, str, str]] = [
    ("auto", "auto", "approve"),
    (HUMAN, "form", "approve"),
    (HUMAN, "terminal", "reject"),
    (HUMAN, "form", "changes"),
]
KIND_IDS = ["auto-approve", "human-approve", "human-reject", "human-changes"]


def decide_escalation(gk: Gatekeeper, task: str, kind: tuple[str, str, str]) -> Any:
    who, channel, decision = kind
    return gk.decide(
        "escalation",
        task,
        decision,
        "file it",
        who,
        channel,
        checks_passed=who == "auto",
    )


@pytest.mark.parametrize("target", AT_ESCALATION)
@pytest.mark.parametrize("first", ESCALATION_DECISIONS, ids=KIND_IDS)
@pytest.mark.parametrize("second", ESCALATION_DECISIONS, ids=KIND_IDS)
def test_d30_an_escalation_gate_takes_one_decision(
    tmp_path: Path,
    target: TaskState,
    first: tuple[str, str, str],
    second: tuple[str, str, str],
) -> None:
    """Second approve, approve then reject, auto then human, human then auto: all refused, nothing written."""
    gk = keeper(tmp_path, {"preset": "unattended"})  # escalation is auto
    task = task_at(gk, target)
    assert decide_escalation(gk, task, first).data["gate"] == "escalation"
    settled = fold(events(gk))
    refused(gk, lambda: decide_escalation(gk, task, second), match="D30")
    assert fold(events(gk)) == settled


def test_d30_each_task_has_its_own_escalation_gate(tmp_path: Path) -> None:
    gk = keeper(tmp_path)  # escalation is auto by default
    to_in_progress(gk, {"T1": [], "T2": []}, max_attempts=1)
    for task in ("T1", "T2"):
        to_coding(gk, task)
        add(gk, "coding_finished", task)
        gk.log.append(attempt_finished(REQ, task, 1, outcome="failed"))
    decide_escalation(gk, "T1", ("auto", "auto", "approve"))
    decide_escalation(
        gk, "T2", (HUMAN, "form", "approve")
    )  # T1's decision leaves T2 open
    for task in ("T1", "T2"):
        for kind in ESCALATION_DECISIONS:
            refused(gk, lambda t=task, k=kind: decide_escalation(gk, t, k), match="D30")


def test_d30_filing_retries_and_questions_are_events_not_gate_decisions(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)
    task = task_at(gk, T.FAILED)
    decide_escalation(gk, task, ("auto", "auto", "approve"))
    gk.log.append(escalation(REQ, reason="all attempts failed", task=task))
    gk.log.append(escalation(REQ, reason="retry", task=task, ticket="AD-500"))
    add(gk, EDIT_REQUESTED, task, question="which test fails first?")
    add(gk, EDIT_DONE, task)
    gk.log.append(escalation(REQ, reason="retry", task=task, ticket="AD-500"))
    assert task_state(gk, task) is T.ESCALATED
    for kind in ESCALATION_DECISIONS:
        refused(gk, lambda k=kind: decide_escalation(gk, task, k), match="D30")


def test_d30_a_decision_recorded_before_the_task_reached_the_gate_never_counts(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)  # escalation is auto
    to_in_progress(gk, {"T1": []}, max_attempts=1)
    to_awaiting_plan(gk, "T1")
    for actor, channel in (("auto", "auto"), (HUMAN, "form")):
        gk.log.append(
            gate_decision(
                REQ,
                actor=actor,
                gate="escalation",
                decision="approve",
                channel=channel,
                task="T1",
            )
        )
    gk.decide("plan", "T1", "approve", "", HUMAN, "form")
    gk.log.append(attempt_started(REQ, "T1", 1))
    gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
    assert task_state(gk, "T1") is T.FAILED
    decide_escalation(
        gk, "T1", (HUMAN, "terminal", "approve")
    )  # the gate's one decision
    refused(
        gk,
        lambda: decide_escalation(gk, "T1", ("auto", "auto", "approve")),
        match="D30",
    )


FORGED_SHAPES: dict[str, tuple[str, dict[str, Any]]] = {
    "engine": ("engine", {"channel": "form"}),
    "agent": ("agent:developer", {"channel": "terminal"}),
    "auto-on-a-form": ("auto", {"channel": "form"}),
    "human-on-auto": (HUMAN, {"channel": "auto"}),
    "auto-reject": ("auto", {"channel": "auto", "decision": "reject"}),
    "auto-changes": (
        "auto",
        {"channel": "auto", "decision": "changes", "comments": "x"},
    ),
    "decision-spelled": (HUMAN, {"decision": "Approve"}),
    "channel-spelled": (HUMAN, {"channel": "chat"}),
    "channel-list": (HUMAN, {"channel": ["form"]}),
    "channel-null": (HUMAN, {"channel": None}),
    "comments-null": (HUMAN, {"comments": None}),
    "comments-list": (HUMAN, {"comments": ["x"]}),
    "changes-blank": (HUMAN, {"decision": "changes", "comments": "  "}),
    "version": (HUMAN, {"version": "1.0.0"}),
    "version-list": (HUMAN, {"version": ["1.0.0"]}),
}


@pytest.mark.parametrize("shape", sorted(FORGED_SHAPES))
def test_d30_a_decision_decide_could_not_have_written_never_counts(
    tmp_path: Path, shape: str
) -> None:
    gk = keeper(tmp_path)  # escalation is auto
    task = task_at(gk, T.FAILED)
    actor, fields = FORGED_SHAPES[shape]
    data = {
        "gate": "escalation",
        "decision": "approve",
        "channel": "form",
        "comments": "",
        "version": None,
        **fields,
    }
    gk.log.append(
        NewEvent(actor=actor, type=GATE_DECISION, requirement=REQ, task=task, data=data)
    )
    decide_escalation(gk, task, (HUMAN, "form", "approve"))  # still open
    refused(
        gk, lambda: decide_escalation(gk, task, (HUMAN, "form", "reject")), match="D30"
    )


def test_d30_an_auto_decision_while_the_gate_was_human_never_counts(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path, {"preset": "strict"})  # escalation is human
    task = task_at(gk, T.FAILED)
    gk.log.append(
        gate_decision(
            REQ,
            actor="auto",
            gate="escalation",
            decision="approve",
            channel="auto",
            task=task,
        )
    )
    refused(gk, lambda: decide_escalation(gk, task, ("auto", "auto", "approve")))
    decide_escalation(gk, task, (HUMAN, "form", "approve"))
    refused(
        gk, lambda: decide_escalation(gk, task, (HUMAN, "form", "approve")), match="D30"
    )


@pytest.mark.skipif(
    not hasattr(t08, "EDIT_STOPPED"),
    reason="needs T08's D29 (a stop in call_for_edit returns the task), on v0-design",
)
def test_d30_a_task_that_left_the_gate_takes_a_new_decision_when_it_returns(
    tmp_path: Path,
) -> None:
    gk = keeper(tmp_path)  # escalation is auto
    to_in_progress(gk, {"T1": []}, max_attempts=1)
    to_coding(gk, "T1")
    add(gk, ATTACHMENT_MISSING, "T1", missing=["plan.md"])  # at the cap (D29)
    assert task_state(gk, "T1") is T.CALL_FOR_EDIT
    decide_escalation(gk, "T1", (HUMAN, "form", "approve"))
    refused(
        gk,
        lambda: decide_escalation(gk, "T1", ("auto", "auto", "approve")),
        match="D30",
    )
    add(gk, "agent.stopped", "T1", then="hold")  # D29: back to where it came from
    assert task_state(gk, "T1") is T.CODING
    refused(gk, lambda: decide_escalation(gk, "T1", (HUMAN, "form", "approve")))
    add(gk, "coding_finished", "T1")
    gk.log.append(attempt_finished(REQ, "T1", 1, outcome="failed"))
    assert task_state(gk, "T1") is T.FAILED
    decide_escalation(gk, "T1", ("auto", "auto", "approve"))  # a new stay at the gate
    refused(
        gk, lambda: decide_escalation(gk, "T1", (HUMAN, "form", "reject")), match="D30"
    )

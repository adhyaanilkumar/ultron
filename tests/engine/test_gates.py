"""T10: gates — settings, snapshots, auto decisions and `decisions.md` (spec §8, FR4, FR16, D7, D8, NFR1)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ultron.config import build_config
from ultron.config.defaults import CHANGEABLE_GATES, LOCKED_GATES
from ultron.engine.gates import (
    CONFIG_CHANGE,
    CONFIG_WARNING,
    GATES,
    GateError,
    Gatekeeper,
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
    CONFIG_SNAPSHOT,
    GATE_DECISION,
    NewEvent,
    attempt_finished,
    attempt_started,
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


@pytest.mark.xfail(
    strict=True,
    raises=IllegalTransition,
    reason="T08 pins intent.changes illegal (strict §3); needs a T08 change, reported",
)
def test_ac6_intent_changes_returns_to_intake_drafting(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_intent(gk)
    new_state = gk.request_changes("intent", REQ, "it's about Windows", HUMAN, "form")
    assert new_state is R.INTAKE_DRAFTING
    assert gk.feedback("intent", REQ) == "it's about Windows"


def test_ac6_intent_changes_today_writes_nothing(tmp_path: Path) -> None:
    gk = keeper(tmp_path)
    to_awaiting_intent(gk)
    count = len(events(gk))
    with pytest.raises(IllegalTransition):
        gk.request_changes("intent", REQ, "it's about Windows", HUMAN, "form")
    assert len(events(gk)) == count


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

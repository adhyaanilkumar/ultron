"""Acceptance tests for T10 (gates: settings, snapshots, auto decisions and `decisions.md`), by the testing agent.

Written from the T10 acceptance criteria and spec §3 (lifecycle), §6 (no tool takes a decision), §8 (gates, presets,
snapshot, warning, what `auto` means, the record), D7, D8, D27 and D28. The §8 preset table is written by hand here
from the spec text, not read from the code under test. Setup events are written straight to a real `EventLog` (T05)
with T05's builders, so the setup never goes through `Gatekeeper.decide`; only the decision under test does.

- AC1 `test_ac1_*`: presets × gates; the snapshot is written at submit; locked gates are human whatever the input.
- AC2 `test_ac2_*`: mid-run changes apply only to gates not reached (per task for task gates) and are recorded.
- AC3 `test_ac3_*`: `auto` never on merge/deploy/rollback, never on a failing task, only approves, only with checks
  passed and the gate set to auto; `who` is never an agent or the engine; a refused decision writes nothing.
- AC4 `test_ac4_*`: every decision records who, when, channel, decision and comment; `spec` records the version.
- AC5 `test_ac5_*`: `decisions.md` from the events, in order, byte-identical across regenerations and processes.
- AC6 `test_ac6_*`: request changes at `intent` (D28), `spec` and `plan` returns to drafting with the comment fed back
  until the next draft.

Attempt 2 (`test_a2_*`, after 8f73f70): the two attempt-1 failures re-checked for the right reason; locked gates
attacked through every input (snapshot, change event, events forged with odd casing, whitespace, unknown fields and
non-string values, hand-built settings, presets, overrides, `change_gate`, odd spellings in `decide`); the escalation
gate from every task state (spec §3, §12, D27) with both decision kinds, the log byte-identical on refusal; a repeat
escalation decision; and the `maintain` gate's invariants that hold whatever T33 defines as reaching it.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ultron.config import build_config
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
    settings_from,
)
from ultron.engine.states import RequirementState, TaskState, fold
from ultron.store import events as ev
from ultron.store.eventlog import EventLog

R = RequirementState
T = TaskState
REQ = "R-20261111-c0de"
OTHER = "R-20261111-beef"
HUMAN = ev.user_actor("Anil Kumar", "anil@example.com")
HUMAN2 = ev.user_actor("अनिल", "anil2@example.com")
MIN: dict[str, Any] = {"commands": {"test": "make test"}}

# --- spec §8, by hand ---------------------------------------------------------------------------------------------
# "default (the table above)": intent human, spec human, tasks auto, plan human, result human, escalation auto,
# maintain auto; merge, deploy, rollback human and locked. "strict (every changeable gate human)",
# "unattended (every changeable gate auto)".
H, A = "human", "auto"
SPEC_8: dict[str, dict[str, str]] = {
    "default": {
        "intent": H,
        "spec": H,
        "tasks": A,
        "plan": H,
        "result": H,
        "escalation": A,
        "maintain": A,
        "merge": H,
        "deploy": H,
        "rollback": H,
    },
    "strict": {
        "intent": H,
        "spec": H,
        "tasks": H,
        "plan": H,
        "result": H,
        "escalation": H,
        "maintain": H,
        "merge": H,
        "deploy": H,
        "rollback": H,
    },
    "unattended": {
        "intent": A,
        "spec": A,
        "tasks": A,
        "plan": A,
        "result": A,
        "escalation": A,
        "maintain": A,
        "merge": H,
        "deploy": H,
        "rollback": H,
    },
}
GATE_NAMES = (
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
LOCKED = ("merge", "deploy", "rollback")
CHANGEABLE = tuple(g for g in GATE_NAMES if g not in LOCKED)
TASK_GATES = ("plan", "result", "escalation")
DEFAULT_HUMAN = ("intent", "spec", "plan", "result")  # §8 "default-human" gates
NOT_HUMAN = [
    "engine",
    "agent:developer",
    "agent:testing",
    "agent:spec",
    "agent:maintenance",
    "agent:intake",
    "",
    "user:",
    "user:   ",
    "Anil",
    "AUTO",
    "Auto",
    " auto",
]


# --- setup: real logs, events written with T05 builders (never through decide) -------------------------------------


def gk_for(
    tmp_path: Path,
    gates: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
    max_attempts: int | None = None,
    sub: str = "log",
) -> Gatekeeper:
    raw: dict[str, Any] = dict(MIN)
    if gates is not None:
        raw["gates"] = gates
    if max_attempts is not None:
        raw["max_attempts"] = max_attempts
    folder = tmp_path / sub
    folder.mkdir(parents=True, exist_ok=True)
    gk = Gatekeeper(EventLog(folder / "events.jsonl"), REQ)
    gk.record_snapshot(build_config(raw, override))
    return gk


def put(gk: Gatekeeper, type_: str, task: str | None = None, **data: Any) -> None:
    gk.log.append(
        ev.NewEvent(actor="engine", type=type_, requirement=REQ, task=task, data=data)
    )


def rec(
    gk: Gatekeeper,
    gate: str,
    decision: str = "approve",
    task: str | None = None,
    *,
    actor: str = HUMAN,
    channel: str = "form",
    version: str | None = None,
    comments: str = "",
) -> None:
    """A setup decision written straight to the log (not through decide)."""
    gk.log.append(
        ev.gate_decision(
            REQ,
            actor=actor,
            gate=gate,
            decision=decision,
            channel=channel,
            comments=comments,
            task=task,
            version=version,
        )
    )


def evs(gk: Gatekeeper) -> list[ev.Event]:
    return gk.log.read(strict=True).events


def req_state(gk: Gatekeeper) -> RequirementState:
    return fold(evs(gk)).requirement


def task_state(gk: Gatekeeper, task: str) -> TaskState:
    return fold(evs(gk)).tasks[task].state


def at_intent(gk: Gatekeeper) -> None:
    put(gk, "intake_started")
    put(gk, "intent_drafted")


def at_spec(gk: Gatekeeper) -> None:
    put(gk, "spec_started")  # a template intent skips intake (§7.1)
    put(gk, "spec_drafted")


def at_decomposing(gk: Gatekeeper, tasks: dict[str, list[str]], ma: int = 3) -> None:
    at_spec(gk)
    rec(gk, "spec", version="1.0.0")
    put(gk, "decomposition_started")
    for name, deps in tasks.items():
        put(gk, "task_created", name, depends_on=deps, max_attempts=ma)


def at_tasks(gk: Gatekeeper, tasks: dict[str, list[str]], ma: int = 3) -> None:
    at_decomposing(gk, tasks, ma)
    put(gk, "tasks_drafted")


def in_progress(gk: Gatekeeper, tasks: dict[str, list[str]], ma: int = 3) -> None:
    at_tasks(gk, tasks, ma)
    rec(gk, "tasks")


def at_plan(gk: Gatekeeper, task: str) -> None:
    put(gk, "task_started", task)
    put(gk, "plan_drafted", task)


def run_attempt(gk: Gatekeeper, task: str, k: int, outcome: str) -> None:
    """Plan approved (by a human, in setup), then attempt k ends with `outcome`."""
    rec(gk, "plan", task=task)
    gk.log.append(ev.attempt_started(REQ, task, k))
    put(gk, "coding_finished", task)
    gk.log.append(ev.attempt_finished(REQ, task, k, outcome=outcome))


def at_result(gk: Gatekeeper, task: str) -> None:
    at_plan(gk, task)
    run_attempt(gk, task, 1, "passed")
    put(gk, "result_requested", task)


def failed_task(gk: Gatekeeper, task: str) -> None:
    """`task` (created with max_attempts=1) fails its only attempt: state `failed`."""
    at_plan(gk, task)
    run_attempt(gk, task, 1, "failed")


def complete(gk: Gatekeeper) -> None:
    in_progress(gk, {"T1": []})
    at_result(gk, "T1")
    rec(gk, "result", task="T1")


def ready(gk: Gatekeeper, gate: str) -> str:
    """Bring the log to where `gate` is legally waiting for a decision; return the item to decide."""
    if gate == "intent":
        at_intent(gk)
        return REQ
    if gate == "spec":
        at_spec(gk)
        return REQ
    if gate == "tasks":
        at_tasks(gk, {"T1": []})
        return REQ
    if gate == "plan":
        in_progress(gk, {"T1": []})
        at_plan(gk, "T1")
        return "T1"
    if gate == "result":
        in_progress(gk, {"T1": []})
        at_result(gk, "T1")
        return "T1"
    if gate == "escalation":
        in_progress(gk, {"T1": []}, ma=1)
        failed_task(gk, "T1")
        assert task_state(gk, "T1") is T.FAILED
        return "T1"
    complete(gk)  # maintain, merge, deploy, rollback: after the work is done
    assert req_state(gk) is R.COMPLETE
    return REQ


def version_for(gate: str) -> str | None:
    return "1.0.0" if gate == "spec" else None


def refused(gk: Gatekeeper, call: Callable[[], object]) -> None:
    """`call` raises a ValueError (GateError / IllegalTransition) and the log's bytes do not change."""
    before = gk.log.path.read_bytes()
    with pytest.raises(ValueError):
        call()
    assert gk.log.path.read_bytes() == before


def decisions(gk: Gatekeeper) -> list[ev.Event]:
    return [e for e in evs(gk) if e.type == ev.GATE_DECISION]


# --- AC1: presets × gates; the snapshot at submit -----------------------------------------------------------------


@pytest.mark.parametrize("preset", sorted(SPEC_8))
@pytest.mark.parametrize("gate", GATE_NAMES)
def test_ac1_preset_x_gate_matches_spec_8(
    tmp_path: Path, preset: str, gate: str
) -> None:
    gk = gk_for(tmp_path, {"preset": preset})
    want = SPEC_8[preset][gate]
    settings = gk.settings()
    assert settings.mode(gate) == want
    if gate in TASK_GATES:
        assert settings.mode(gate, "T1") == want
    snap = [e for e in evs(gk) if e.type == ev.CONFIG_SNAPSHOT]
    assert len(snap) == 1
    assert snap[0].data["gates"][gate] == want


def test_ac1_no_preset_means_default(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    assert {g: gk.settings().mode(g) for g in GATE_NAMES} == SPEC_8["default"]


def test_ac1_snapshot_is_the_first_event_written_at_submit(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    gk = Gatekeeper(log, REQ)
    assert not log.path.exists() or evs(gk) == []
    written = gk.record_snapshot(build_config({**MIN, "max_attempts": 4}))
    first = evs(gk)[0]
    assert first.seq == 1
    assert first.type == ev.CONFIG_SNAPSHOT
    assert first.requirement == REQ
    assert first == written[0]
    assert set(first.data["gates"]) == set(GATE_NAMES)
    assert first.data["max_attempts"] == 4
    # the settings are read back from the snapshot on disk, by a fresh reader
    again = Gatekeeper(EventLog(log.path), REQ).settings()
    assert again.max_attempts == 4
    assert {g: again.mode(g) for g in GATE_NAMES} == SPEC_8["default"]


@pytest.mark.parametrize(
    ("gates", "override", "want"),
    [
        ({"preset": "strict"}, {"gates": {"tasks": "auto"}}, {"tasks": A}),
        ({"preset": "unattended"}, {"gates": {"result": "human"}}, {"result": H}),
        ({"preset": "default", "plan": "auto"}, None, {"plan": A}),
        ({"preset": "default", "plan": "auto"}, {"gates": {"plan": "human"}}, {}),
    ],
)
def test_ac1_per_repo_and_per_requirement_settings_on_top_of_a_preset(
    tmp_path: Path,
    gates: dict[str, Any],
    override: dict[str, Any] | None,
    want: dict[str, str],
) -> None:
    gk = gk_for(tmp_path, gates, override)
    expected = {**SPEC_8[gates["preset"]], **want}
    assert {g: gk.settings().mode(g) for g in GATE_NAMES} == expected


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("mode", ["auto", "AUTO", "off", ""])
def test_ac1_a_locked_gate_cannot_be_configured_other_than_human(
    tmp_path: Path, gate: str, mode: str
) -> None:
    with pytest.raises(ValueError):
        gk_for(tmp_path, {"preset": "unattended", gate: mode})
    with pytest.raises(ValueError):
        gk_for(tmp_path, None, {"gates": {gate: mode}}, sub="override")


@pytest.mark.parametrize("gate", LOCKED)
def test_ac1_a_forged_snapshot_cannot_make_a_locked_gate_auto(
    tmp_path: Path, gate: str
) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        ev.config_snapshot(REQ, max_attempts=3, gates=dict.fromkeys(GATE_NAMES, "auto"))
    )
    gk = Gatekeeper(log, REQ)
    assert gk.settings().mode(gate) == "human"
    complete(gk)
    refused(
        gk,
        lambda: gk.decide(gate, REQ, "approve", "", "auto", "auto", checks_passed=True),
    )


def test_ac1_switching_a_default_human_gate_to_auto_warns_and_is_recorded(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path, None, {"gates": {"result": "auto", "tasks": "human"}})
    warned = [e for e in evs(gk) if e.type == CONFIG_WARNING]
    assert [w.data["gate"] for w in warned] == ["result"]
    assert warned[0].data["from"] == "human"
    assert warned[0].data["to"] == "auto"
    assert warned[0].seq == 2  # recorded at submit, beside the snapshot
    strict = gk_for(tmp_path, {"preset": "strict"}, sub="strict")
    assert [e for e in evs(strict) if e.type == CONFIG_WARNING] == []
    unattended = gk_for(tmp_path, {"preset": "unattended"}, sub="unattended")
    assert sorted(
        e.data["gate"] for e in evs(unattended) if e.type == CONFIG_WARNING
    ) == sorted(DEFAULT_HUMAN)


def test_ac1_a_second_snapshot_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    refused(gk, lambda: gk.record_snapshot(build_config({**MIN, "max_attempts": 9})))
    assert gk.settings().max_attempts == 3


# --- AC2: mid-run changes -----------------------------------------------------------------------------------------


def changes_of(gk: Gatekeeper) -> list[ev.Event]:
    return [e for e in evs(gk) if e.type == CONFIG_CHANGE]


def test_ac2_change_to_an_unreached_gate_applies_and_is_recorded(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    gk.change_gate("spec", "auto", who=HUMAN)
    [change] = changes_of(gk)
    assert change.actor == HUMAN
    assert change.data["gate"] == "spec"
    assert change.data["from"] == "human"
    assert change.data["to"] == "auto"
    assert change.data["warning"]  # §8: a default-human gate going auto warns
    assert gk.settings().mode("spec") == "auto"
    rec(gk, "intent")
    put(gk, "spec_drafted")
    gk.decide("spec", REQ, "approve", "", "auto", "auto", "1.1.0", checks_passed=True)
    assert req_state(gk) is R.SPEC_APPROVED


def test_ac2_a_gate_waiting_for_a_decision_is_reached_and_keeps_its_mode(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path)
    at_spec(gk)
    gk.change_gate("spec", "auto", who=HUMAN)
    assert len(changes_of(gk)) == 1  # recorded even though it does not apply
    assert gk.settings().mode("spec") == "human"
    refused(
        gk,
        lambda: gk.decide(
            "spec", REQ, "approve", "", "auto", "auto", "1.0.0", checks_passed=True
        ),
    )
    gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0")
    assert req_state(gk) is R.SPEC_APPROVED


@pytest.mark.parametrize("to", ["auto", "human"])
def test_ac2_a_passed_gate_is_not_reopened(tmp_path: Path, to: str) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended" if to == "human" else "default"})
    at_intent(gk)
    rec(
        gk,
        "intent",
        actor="auto" if to == "human" else HUMAN,
        channel="auto" if to == "human" else "form",
    )
    before = fold(evs(gk))
    old_mode = gk.settings().mode("intent")
    gk.change_gate("intent", to, who=HUMAN)
    assert fold(evs(gk)) == before
    assert gk.settings().mode("intent") == old_mode
    for decision in ("approve", "changes", "reject"):
        refused(
            gk, lambda d=decision: gk.decide("intent", REQ, d, "again", HUMAN, "form")
        )


def test_ac2_tasks_gate_set_human_before_decomposition_stops_the_auto_pass(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path)  # tasks is auto by default
    at_decomposing(gk, {"T1": []})
    gk.change_gate("tasks", "human", who=HUMAN)
    assert changes_of(gk)[0].data["warning"] in (None, "")  # auto -> human: no warning
    refused(
        gk,
        lambda: gk.decide(
            "tasks", REQ, "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    put(gk, "tasks_drafted")
    refused(
        gk,
        lambda: gk.decide(
            "tasks", REQ, "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    gk.decide("tasks", REQ, "approve", "split ok", HUMAN, "terminal")
    assert req_state(gk) is R.IN_PROGRESS


def test_ac2_tasks_gate_already_passed_by_auto_is_not_reopened(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_decomposing(gk, {"T1": []})
    rec(
        gk, "tasks", actor="auto", channel="auto"
    )  # auto passes straight from decomposing
    assert req_state(gk) is R.IN_PROGRESS
    gk.change_gate("tasks", "human", who=HUMAN)
    assert gk.settings().mode("tasks") == "auto"
    assert req_state(gk) is R.IN_PROGRESS


def test_ac2_plan_gate_change_is_per_task(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)  # plan is human
    in_progress(gk, {"T1": [], "T2": [], "T3": []})
    at_plan(gk, "T1")  # T1 reached the plan gate; T2 and T3 have not
    gk.change_gate("plan", "auto", who=HUMAN)
    settings = gk.settings()
    assert settings.mode("plan", "T1") == "human"
    assert settings.mode("plan", "T2") == "auto"
    assert settings.mode("plan", "T3") == "auto"
    refused(
        gk,
        lambda: gk.decide(
            "plan", "T1", "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    at_plan(gk, "T2")
    gk.decide("plan", "T2", "approve", "", "auto", "auto", checks_passed=True)
    assert task_state(gk, "T2") is T.PLAN_APPROVED
    # T1's revised plan after a failed attempt keeps the mode T1 had when it reached the gate
    rec(gk, "plan", task="T1")
    gk.log.append(ev.attempt_started(REQ, "T1", 1))
    gk.log.append(ev.attempt_finished(REQ, "T1", 1, outcome="failed"))
    put(gk, "plan_drafted", "T1")
    assert task_state(gk, "T1") is T.AWAITING_PLAN
    refused(
        gk,
        lambda: gk.decide(
            "plan", "T1", "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    # a change made later does not move T2 (it already passed) but does reach T3
    gk.change_gate("plan", "human", who=HUMAN)
    assert gk.settings().mode("plan", "T2") == "auto"
    assert gk.settings().mode("plan", "T3") == "human"


def test_ac2_escalation_gate_change_is_per_task(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)  # escalation is auto
    in_progress(gk, {"T1": [], "T2": []}, ma=1)
    failed_task(gk, "T1")  # T1 reached the escalation gate
    gk.change_gate("escalation", "human", who=HUMAN)
    assert gk.settings().mode("escalation", "T1") == "auto"
    assert gk.settings().mode("escalation", "T2") == "human"
    gk.decide("escalation", "T1", "approve", "", "auto", "auto", checks_passed=True)
    failed_task(gk, "T2")
    refused(
        gk,
        lambda: gk.decide(
            "escalation", "T2", "approve", "", "auto", "auto", checks_passed=True
        ),
    )


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("stage", ["submitted", "in_progress", "complete"])
def test_ac2_a_locked_gate_cannot_be_changed_to_auto_mid_run(
    tmp_path: Path, gate: str, stage: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    if stage == "in_progress":
        in_progress(gk, {"T1": []})
    elif stage == "complete":
        complete(gk)
    refused(gk, lambda: gk.change_gate(gate, "auto", who=HUMAN))
    assert gk.settings().mode(gate) == "human"


@pytest.mark.parametrize("gate", LOCKED)
def test_ac2_a_forged_config_change_cannot_make_a_locked_gate_auto(
    tmp_path: Path, gate: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    gk.log.append(
        ev.NewEvent(
            actor=HUMAN,
            type=CONFIG_CHANGE,
            requirement=REQ,
            data={"gate": gate, "from": "human", "to": "auto", "reached": False},
        )
    )
    refused(
        gk,
        lambda: gk.decide(gate, REQ, "approve", "", "auto", "auto", checks_passed=True),
    )
    assert (
        gk.settings().mode(gate) == "human"
    )  # ultron_settings must never show it auto


@pytest.mark.parametrize("who", ["auto", *NOT_HUMAN])
def test_ac2_only_a_human_changes_a_gate(tmp_path: Path, who: str) -> None:
    gk = gk_for(tmp_path)
    refused(gk, lambda: gk.change_gate("plan", "auto", who=who))


# --- AC3: auto and who rules; refused decisions write nothing -----------------------------------------------------


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize(
    ("who", "channel"),
    [("auto", "auto"), ("auto", "form"), ("auto", "terminal"), (HUMAN, "auto")],
)
def test_ac3_auto_on_merge_deploy_rollback_raises(
    tmp_path: Path, gate: str, who: str, channel: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    for checks in (True, False):
        refused(
            gk,
            lambda c=checks: gk.decide(
                gate, item, "approve", "", who, channel, checks_passed=c
            ),
        )


@pytest.mark.parametrize("gate", GATE_NAMES)
@pytest.mark.parametrize("who", NOT_HUMAN)
def test_ac3_who_is_never_an_agent_or_the_engine(
    tmp_path: Path, gate: str, who: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    for channel in ("form", "terminal", "auto"):
        refused(
            gk,
            lambda ch=channel: gk.decide(
                gate,
                item,
                "approve",
                "",
                who,
                ch,
                version_for(gate),
                checks_passed=True,
            ),
        )


@pytest.mark.parametrize("gate", GATE_NAMES)
def test_ac3_a_human_can_decide_every_ready_gate(tmp_path: Path, gate: str) -> None:
    gk = gk_for(tmp_path)
    item = ready(gk, gate)
    event = gk.decide(gate, item, "approve", "ok", HUMAN, "terminal", version_for(gate))
    assert event.actor == HUMAN
    assert evs(gk)[-1] == event


@pytest.mark.parametrize("gate", CHANGEABLE)
def test_ac3_auto_passes_a_changeable_gate_set_auto_when_checks_passed(
    tmp_path: Path, gate: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    event = gk.decide(
        gate, item, "approve", "", "auto", "auto", version_for(gate), checks_passed=True
    )
    assert event.actor == "auto"
    assert event.data["channel"] == "auto"
    assert event.data["decision"] == "approve"


@pytest.mark.parametrize("gate", CHANGEABLE)
def test_ac3_auto_needs_the_stage_checks_to_have_passed(
    tmp_path: Path, gate: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    refused(
        gk,
        lambda: gk.decide(gate, item, "approve", "", "auto", "auto", version_for(gate)),
    )
    refused(
        gk,
        lambda: gk.decide(
            gate,
            item,
            "approve",
            "",
            "auto",
            "auto",
            version_for(gate),
            checks_passed=False,
        ),
    )


@pytest.mark.parametrize("gate", CHANGEABLE)
@pytest.mark.parametrize("decision", ["changes", "reject"])
def test_ac3_auto_only_approves(tmp_path: Path, gate: str, decision: str) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    refused(
        gk,
        lambda: gk.decide(
            gate, item, decision, "why", "auto", "auto", checks_passed=True
        ),
    )


@pytest.mark.parametrize("gate", CHANGEABLE)
def test_ac3_auto_is_refused_where_the_gate_is_human(tmp_path: Path, gate: str) -> None:
    gk = gk_for(tmp_path, {"preset": "strict"})
    item = ready(gk, gate)
    refused(
        gk,
        lambda: gk.decide(
            gate,
            item,
            "approve",
            "",
            "auto",
            "auto",
            version_for(gate),
            checks_passed=True,
        ),
    )


def test_ac3_auto_on_a_failed_task_raises(tmp_path: Path) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []}, ma=1)
    failed_task(gk, "T1")
    assert task_state(gk, "T1") is T.FAILED
    for gate in ("plan", "result"):
        refused(
            gk,
            lambda g=gate: gk.decide(
                g, "T1", "approve", "", "auto", "auto", checks_passed=True
            ),
        )


def test_ac3_auto_result_after_the_attempt_failed_raises(tmp_path: Path) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []})
    at_plan(gk, "T1")
    run_attempt(gk, "T1", 1, "failed")  # attempts left: back to planning
    assert task_state(gk, "T1") is T.PLANNING
    refused(
        gk,
        lambda: gk.decide(
            "result", "T1", "approve", "", "auto", "auto", checks_passed=True
        ),
    )


def test_ac3_auto_on_a_rejected_task_raises_except_at_escalation(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []})
    at_result(gk, "T1")
    rec(gk, "result", "reject", task="T1", comments="wrong approach")
    assert task_state(gk, "T1") is T.REJECTED
    for gate in ("plan", "result"):
        refused(
            gk,
            lambda g=gate: gk.decide(
                g, "T1", "approve", "", "auto", "auto", checks_passed=True
            ),
        )
    # escalation exists for failed and rejected tasks (§8: confirm before a Jira ticket); default auto
    gk.decide("escalation", "T1", "approve", "", "auto", "auto", checks_passed=True)


def test_ac3_auto_escalation_is_refused_for_a_task_that_is_not_failing(
    tmp_path: Path,
) -> None:
    """Probe: the escalation gate is reached only by a failed or rejected task (§3, §8). An auto decision on a
    healthy task's escalation gate passes a gate that was never reached, so it must write nothing."""
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []})
    at_plan(gk, "T1")
    refused(
        gk,
        lambda: gk.decide(
            "escalation", "T1", "approve", "", "auto", "auto", checks_passed=True
        ),
    )


def test_ac3_auto_retry_plan_and_passing_result_after_a_failed_attempt(
    tmp_path: Path,
) -> None:
    """Developer choice (agreed): a revised plan after a counted failure may pass an auto plan gate, and a task whose
    latest attempt passed is not failing, so `unattended` evaluation can retry (§8, §16)."""
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []})
    at_plan(gk, "T1")
    run_attempt(gk, "T1", 1, "failed")
    put(gk, "plan_drafted", "T1")
    gk.decide("plan", "T1", "approve", "", "auto", "auto", checks_passed=True)
    gk.log.append(ev.attempt_started(REQ, "T1", 2))
    put(gk, "coding_finished", "T1")
    gk.log.append(ev.attempt_finished(REQ, "T1", 2, outcome="passed"))
    put(gk, "result_requested", "T1")
    event = gk.decide("result", "T1", "approve", "", "auto", "auto", checks_passed=True)
    assert event.attempt == 2
    assert task_state(gk, "T1") is T.APPROVED


def test_ac3_a_human_may_decide_an_auto_gate(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)  # tasks is auto by default
    at_tasks(gk, {"T1": []})
    event = gk.decide("tasks", REQ, "approve", "checked by hand", HUMAN, "form")
    assert event.actor == HUMAN
    assert req_state(gk) is R.IN_PROGRESS


def test_ac3_illegal_decisions_write_nothing(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    refused(gk, lambda: gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0"))
    refused(gk, lambda: gk.decide("tasks", REQ, "approve", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("intent", REQ, "maybe", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("intent", REQ, "approve", "", HUMAN, "chat"))
    refused(gk, lambda: gk.decide("intent", OTHER, "approve", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("nope", REQ, "approve", "", HUMAN, "form"))
    refused(gk, lambda: gk.decide("intent", REQ, "approve", "", HUMAN, "form", "1.0.0"))
    gk2 = gk_for(tmp_path, sub="two")
    in_progress(gk2, {"T1": [], "T2": ["T1"]})
    at_plan(gk2, "T1")
    refused(gk2, lambda: gk2.decide("plan", "T2", "approve", "", HUMAN, "form"))
    refused(gk2, lambda: gk2.decide("plan", "T9", "approve", "", HUMAN, "form"))
    refused(gk2, lambda: gk2.decide("plan", REQ, "approve", "", HUMAN, "form"))
    refused(gk2, lambda: gk2.decide("result", "T1", "approve", "", HUMAN, "form"))
    refused(gk2, lambda: gk2.decide("merge", "T1", "approve", "", HUMAN, "form"))
    gk2.decide("plan", "T1", "approve", "", HUMAN, "form")
    refused(gk2, lambda: gk2.decide("plan", "T1", "approve", "", HUMAN, "form"))


def test_ac3_waiting_at_a_gate_is_not_an_attempt(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    in_progress(gk, {"T1": []})
    at_plan(gk, "T1")
    for n in range(4):
        gk.request_changes("plan", "T1", f"round {n}", HUMAN, "form")
        put(gk, "plan_drafted", "T1")
    gk.decide("plan", "T1", "approve", "", HUMAN, "form")
    assert fold(evs(gk)).tasks["T1"].attempt == 0


# --- AC4: the record of every decision ----------------------------------------------------------------------------


def test_ac4_every_decision_records_who_when_channel_decision_and_comment(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path, None, {"gates": {"maintain": "human"}})
    made: list[tuple[str, str, str, str, str]] = []

    def decide(
        gate: str, item: str, decision: str, comment: str, who: str, ch: str
    ) -> None:
        version = "2.1.0" if gate == "spec" and decision == "approve" else None
        gk.decide(
            gate, item, decision, comment, who, ch, version, checks_passed=who == "auto"
        )
        made.append((gate, decision, comment, who, ch))

    at_intent(gk)
    decide("intent", REQ, "changes", "mention Windows | Linux\nboth", HUMAN, "form")
    put(gk, "intent_drafted")
    decide("intent", REQ, "approve", "", HUMAN2, "terminal")
    put(gk, "spec_drafted")
    decide("spec", REQ, "changes", "split the tool table", HUMAN, "form")
    put(gk, "spec_drafted")
    decide("spec", REQ, "approve", "मंज़ूर", HUMAN, "form")
    put(gk, "decomposition_started")
    put(gk, "task_created", "T1", depends_on=[], max_attempts=3)
    put(gk, "tasks_drafted")
    decide("tasks", REQ, "approve", "", "auto", "auto")
    at_plan(gk, "T1")
    decide("plan", "T1", "approve", "go", HUMAN, "terminal")
    gk.log.append(ev.attempt_started(REQ, "T1", 1))
    put(gk, "coding_finished", "T1")
    gk.log.append(ev.attempt_finished(REQ, "T1", 1, outcome="passed"))
    put(gk, "result_requested", "T1")
    decide("result", "T1", "approve", "evidence ok", HUMAN, "form")
    assert req_state(gk) is R.COMPLETE
    decide("maintain", REQ, "approve", "", HUMAN, "form")
    decide("merge", REQ, "approve", "merge in order", HUMAN, "form")
    decide("deploy", REQ, "approve", "", HUMAN, "terminal")
    decide("rollback", REQ, "approve", "bad release", HUMAN, "form")

    got = decisions(gk)
    assert len(got) == len(made)
    for event, (gate, decision, comment, who, ch) in zip(got, made, strict=True):
        assert event.actor == who  # who
        assert event.time.endswith("Z")  # when, in UTC
        when = datetime.fromisoformat(event.time)
        assert when.utcoffset() == timedelta(0)
        assert event.data["gate"] == gate
        assert event.data["channel"] == ch  # channel
        assert event.data["channel"] in ("form", "terminal", "auto")
        assert event.data["decision"] == decision
        assert event.data["comments"] == comment  # stored exactly
        if gate == "spec" and decision == "approve":
            assert event.data["version"] == "2.1.0"
        elif gate != "spec":
            assert event.data.get("version") is None
        if gate in TASK_GATES:
            assert event.task == "T1"
        else:
            assert event.task is None
    assert [e.seq for e in got] == sorted(e.seq for e in got)
    plan_event = next(e for e in got if e.data["gate"] == "plan")
    result_event = next(e for e in got if e.data["gate"] == "result")
    assert plan_event.attempt is None  # waiting at a gate is not an attempt
    assert result_event.attempt == 1


@pytest.mark.parametrize("bad", [None, "", "v1.0.0", "1.0", "1.0.0.0", "one", " 1.0.0"])
def test_ac4_spec_approve_needs_a_confirmed_version(
    tmp_path: Path, bad: str | None
) -> None:
    gk = gk_for(tmp_path)
    at_spec(gk)
    refused(gk, lambda: gk.decide("spec", REQ, "approve", "", HUMAN, "form", bad))
    event = gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.3.2")
    assert event.data["version"] == "1.3.2"


@pytest.mark.parametrize("gate", [g for g in GATE_NAMES if g != "spec"])
def test_ac4_only_the_spec_gate_records_a_version(tmp_path: Path, gate: str) -> None:
    gk = gk_for(tmp_path)
    item = ready(gk, gate)
    refused(gk, lambda: gk.decide(gate, item, "approve", "", HUMAN, "form", "1.0.0"))


def test_ac4_auto_spec_records_the_version_too(tmp_path: Path) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    at_spec(gk)
    refused(
        gk,
        lambda: gk.decide(
            "spec", REQ, "approve", "", "auto", "auto", checks_passed=True
        ),
    )
    event = gk.decide(
        "spec", REQ, "approve", "", "auto", "auto", "0.1.0", checks_passed=True
    )
    assert event.data["version"] == "0.1.0"


# --- AC5: decisions.md --------------------------------------------------------------------------------------------


def _history(tmp_path: Path) -> Gatekeeper:
    gk = gk_for(tmp_path)
    at_intent(gk)
    gk.request_changes(
        "intent", REQ, "it's about Windows | PowerShell\r\nand Linux", HUMAN, "form"
    )
    put(gk, "intent_drafted")
    gk.decide("intent", REQ, "approve", "", HUMAN2, "terminal")
    gk.change_gate("plan", "auto", who=HUMAN)  # not a decision: no row
    put(gk, "spec_drafted")
    gk.decide("spec", REQ, "approve", "हाँ, ठीक है", HUMAN, "form", "1.0.0")
    put(gk, "decomposition_started")
    put(gk, "task_created", "T1", depends_on=[], max_attempts=3)
    gk.decide("tasks", REQ, "approve", "", "auto", "auto", checks_passed=True)
    at_plan(gk, "T1")
    gk.decide("plan", "T1", "approve", "", "auto", "auto", checks_passed=True)
    return gk


def _rows(text: str) -> list[str]:
    lines = text.split("\n")
    table = [ln for ln in lines if ln.startswith("|")]
    return [ln for ln in table[2:]]  # skip header and separator


def test_ac5_decisions_md_lists_every_decision_in_order(tmp_path: Path) -> None:
    gk = _history(tmp_path)
    text = render_decisions(evs(gk), REQ)
    rows = _rows(text)
    made = decisions(gk)
    assert len(rows) == len(made) == 5
    for row, event in zip(rows, made, strict=True):
        assert str(event.seq) in row.split("|")[1]
        assert f" {event.data['gate']} " in row
        assert f" {event.data['decision']} " in row
        assert f" {event.data['channel']} " in row
        assert event.time in row
    assert "1.0.0" in rows[2]
    assert "हाँ, ठीक है" in rows[2]
    assert "auto" in rows[3] and "auto" in rows[4]
    assert "Anil Kumar" in rows[0]
    assert "अनिल" in rows[1]
    # the comment's pipe and line break cannot break the table: every row has the header's column count
    header = next(ln for ln in text.split("\n") if ln.startswith("|"))
    cols = header.count("|") - header.count("\\|")
    for row in rows:
        assert row.count("|") - row.count("\\|") == cols


def test_ac5_regenerating_is_byte_identical(tmp_path: Path) -> None:
    gk = _history(tmp_path)
    out = tmp_path / "out" / "decisions.md"
    gk.write_decisions(out)
    first = out.read_bytes()
    gk.write_decisions(out)
    assert out.read_bytes() == first
    fresh = Gatekeeper(EventLog(gk.log.path), REQ)
    fresh.write_decisions(out)
    assert out.read_bytes() == first
    assert render_decisions(evs(gk), REQ).encode("utf-8") == first
    assert b"\r" not in first
    assert first.endswith(b"\n")
    first.decode("utf-8")  # valid UTF-8


def test_ac5_regenerating_in_other_processes_is_byte_identical(tmp_path: Path) -> None:
    gk = _history(tmp_path)
    want = render_decisions(evs(gk), REQ).encode("utf-8")
    script = (
        "import sys\n"
        "from ultron.engine.gates import render_decisions\n"
        "from ultron.store.eventlog import EventLog\n"
        "events = EventLog(sys.argv[1]).read(strict=True).events\n"
        "sys.stdout.buffer.write(render_decisions(events, sys.argv[2]).encode('utf-8'))\n"
    )
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "TZ": "Asia/Kolkata"}
        done = subprocess.run(
            [sys.executable, "-c", script, str(gk.log.path), REQ],
            capture_output=True,
            env=env,
            check=True,
            timeout=60,
        )
        assert done.stdout == want


def test_ac5_decisions_md_is_derived_only_from_the_events(tmp_path: Path) -> None:
    gk = _history(tmp_path)
    out = tmp_path / "decisions.md"
    gk.write_decisions(out)
    want = out.read_bytes()
    out.write_text(
        "| 1 | hand edited | approve |\r\n", encoding="utf-8"
    )  # a hand edit is overwritten
    gk.write_decisions(out)
    assert out.read_bytes() == want
    out.unlink()
    gk.write_decisions(out)
    assert out.read_bytes() == want
    # order comes from seq, not from the order the events are passed in; other requirements' events are ignored
    shuffled = list(evs(gk))
    random.Random(7).shuffle(shuffled)
    other_log = EventLog(tmp_path / "other" / "events.jsonl")
    other_log.append(
        ev.gate_decision(
            OTHER, actor=HUMAN, gate="intent", decision="approve", channel="form"
        )
    )
    mixed = [*shuffled, *other_log.read(strict=True).events]
    assert render_decisions(mixed, REQ).encode("utf-8") == want


def test_ac5_a_new_decision_appends_a_row_and_leaves_the_earlier_ones(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    empty = render_decisions(evs(gk), REQ)
    assert _rows(empty) == []
    assert REQ in empty
    gk.decide("intent", REQ, "approve", "", HUMAN, "form")
    one = _rows(render_decisions(evs(gk), REQ))
    put(gk, "spec_drafted")
    gk.decide("spec", REQ, "changes", "more detail", HUMAN, "terminal")
    two = _rows(render_decisions(evs(gk), REQ))
    assert len(one) == 1
    assert two[:1] == one
    assert len(two) == 2


# --- AC6: request changes at intent (D28), spec and plan ----------------------------------------------------------


def test_ac6_intent_changes_go_back_to_the_intake_agent_d28(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    state = gk.request_changes(
        "intent", REQ, "it's for Hindi speakers too", HUMAN, "form"
    )
    assert state is R.INTAKE_DRAFTING
    assert req_state(gk) is R.INTAKE_DRAFTING
    assert decisions(gk)[-1].data["decision"] == "changes"
    assert decisions(gk)[-1].data["comments"] == "it's for Hindi speakers too"
    assert gk.feedback("intent", REQ) == "it's for Hindi speakers too"
    put(gk, "agent_call_note")  # unrelated events do not consume the comment
    assert gk.feedback("intent", REQ) == "it's for Hindi speakers too"
    put(gk, "intent_drafted")
    assert gk.feedback("intent", REQ) is None
    assert req_state(gk) is R.AWAITING_INTENT
    gk.decide("intent", REQ, "approve", "", HUMAN, "form")
    assert req_state(gk) is R.SPEC_DRAFTING


def test_ac6_spec_changes_go_back_to_spec_drafting(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_spec(gk)
    assert (
        gk.request_changes("spec", REQ, "first", HUMAN, "terminal") is R.SPEC_DRAFTING
    )
    assert gk.feedback("spec", REQ) == "first"
    put(gk, "spec_drafted")
    assert gk.feedback("spec", REQ) is None
    assert gk.request_changes("spec", REQ, "second", HUMAN, "form") is R.SPEC_DRAFTING
    assert gk.feedback("spec", REQ) == "second"  # the latest comment, not the first
    gk.change_gate("plan", "auto", who=HUMAN)
    assert gk.feedback("spec", REQ) == "second"
    put(gk, "spec_drafted")
    assert gk.feedback("spec", REQ) is None
    gk.decide("spec", REQ, "approve", "", HUMAN, "form", "1.0.0")
    assert req_state(gk) is R.SPEC_APPROVED


def test_ac6_plan_changes_go_back_to_planning_per_task(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    in_progress(gk, {"T1": [], "T2": []})
    at_plan(gk, "T1")
    at_plan(gk, "T2")
    assert (
        gk.request_changes("plan", "T1", "add a rollback step", HUMAN, "form")
        is T.PLANNING
    )
    assert task_state(gk, "T1") is T.PLANNING
    assert task_state(gk, "T2") is T.AWAITING_PLAN
    assert gk.feedback("plan", "T1") == "add a rollback step"
    assert gk.feedback("plan", "T2") is None
    gk.request_changes("plan", "T2", "name the files", HUMAN, "terminal")
    put(gk, "plan_drafted", "T2")  # T2's next draft does not consume T1's comment
    assert gk.feedback("plan", "T1") == "add a rollback step"
    assert gk.feedback("plan", "T2") is None
    put(gk, "plan_drafted", "T1")
    assert gk.feedback("plan", "T1") is None
    assert task_state(gk, "T1") is T.AWAITING_PLAN


@pytest.mark.parametrize("gate", ["intent", "spec", "plan"])
@pytest.mark.parametrize("comment", ["", "   ", "\n\t"])
def test_ac6_changes_need_a_comment_for_the_agent(
    tmp_path: Path, gate: str, comment: str
) -> None:
    gk = gk_for(tmp_path)
    item = ready(gk, gate)
    refused(gk, lambda: gk.request_changes(gate, item, comment, HUMAN, "form"))
    refused(gk, lambda: gk.decide(gate, item, "changes", comment, HUMAN, "form"))


@pytest.mark.parametrize("gate", ["intent", "spec", "plan"])
def test_ac6_agents_and_auto_cannot_request_changes(tmp_path: Path, gate: str) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    item = ready(gk, gate)
    refused(gk, lambda: gk.request_changes(gate, item, "redo", "auto", "auto"))
    for who in ("engine", "agent:spec", "agent:developer"):
        refused(gk, lambda w=who: gk.request_changes(gate, item, "redo", w, "form"))


def test_ac6_feedback_is_none_before_any_changes(tmp_path: Path) -> None:
    gk = gk_for(tmp_path)
    at_intent(gk)
    assert gk.feedback("intent", REQ) is None
    assert gk.feedback("spec", REQ) is None


def test_gate_table_has_the_ten_gates_of_spec_8(tmp_path: Path) -> None:
    assert set(GATES) == set(GATE_NAMES)
    assert {g for g, info in GATES.items() if info.locked} == set(LOCKED)


# ==================================================================================================================
# Attempt 2
# ==================================================================================================================


def forge(
    gk: Gatekeeper,
    type_: str,
    data: dict[str, Any],
    *,
    actor: str = HUMAN,
    task: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write one line straight to `events.jsonl`, as a hand edit or another writer would (unknown fields kept)."""
    event = ev.Event(
        schema=ev.SCHEMA_VERSION,
        seq=len(evs(gk)) + 1,
        time="2026-11-11T05:00:00Z",
        actor=actor,
        type=type_,
        requirement=REQ,
        task=task,
        attempt=None,
        data=data,
        ultron_version="0.0.0",
        extra=extra or {},
    )
    with gk.log.path.open("ab") as fh:
        fh.write(ev.to_json_line(event))


def forged_snapshot(tmp_path: Path, gates: object, sub: str = "forged") -> Gatekeeper:
    """A log whose submit snapshot was written without T05's builder checks (any `gates` value)."""
    folder = tmp_path / sub
    folder.mkdir(parents=True, exist_ok=True)
    gk = Gatekeeper(EventLog(folder / "events.jsonl"), REQ)
    gk.log.append(
        ev.NewEvent(
            actor="engine",
            type=ev.CONFIG_SNAPSHOT,
            requirement=REQ,
            data={"max_attempts": 3, "gates": gates},
        )
    )
    return gk


def refused_any(gk: Gatekeeper, call: Callable[[], object]) -> None:
    """`call` raises (a gate error, or a TypeError on a malformed log: it fails closed) and the log is unchanged."""
    before = gk.log.path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        call()
    assert gk.log.path.read_bytes() == before


def auto_decide(gk: Gatekeeper, gate: str, item: str = REQ) -> ev.Event:
    return gk.decide(gate, item, "approve", "", "auto", "auto", checks_passed=True)


# --- the attempt-1 failures, re-checked for the right reason -------------------------------------------------------


@pytest.mark.parametrize("gate", LOCKED)
def test_a2_attempt1_forged_change_is_skipped_not_only_masked(
    tmp_path: Path, gate: str
) -> None:
    """`test_ac2_a_forged_config_change_cannot_make_a_locked_gate_auto` passes because the change is never folded
    (not only hidden by the last read), the snapshot layer stays human, and auto is refused as locked."""
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    gk.log.append(
        ev.NewEvent(
            actor=HUMAN,
            type=CONFIG_CHANGE,
            requirement=REQ,
            data={"gate": gate, "from": "human", "to": "auto", "reached": False},
        )
    )
    settings = settings_from(evs(gk))
    assert settings.changes == ()
    assert settings.modes[gate] == "human"
    assert settings.mode(gate) == "human"
    before = gk.log.path.read_bytes()
    with pytest.raises(GateError, match="locked"):
        auto_decide(gk, gate)
    assert gk.log.path.read_bytes() == before
    gk.decide(
        gate, REQ, "approve", "", HUMAN, "form"
    )  # the gate itself still works for a human


def test_a2_attempt1_escalation_refusal_is_about_reaching_the_gate(
    tmp_path: Path,
) -> None:
    """`test_ac3_auto_escalation_is_refused_for_a_task_that_is_not_failing` passes because of the reached rule: a
    gate error (not a crash or a fold error), and the very same call is taken once the task fails."""
    gk = gk_for(tmp_path, {"preset": "unattended"})
    in_progress(gk, {"T1": []}, ma=1)
    at_plan(gk, "T1")
    before = gk.log.path.read_bytes()
    with pytest.raises(GateError, match="awaiting_plan"):
        auto_decide(gk, "escalation", "T1")
    assert gk.log.path.read_bytes() == before
    run_attempt(gk, "T1", 1, "failed")
    assert task_state(gk, "T1") is T.FAILED
    event = auto_decide(gk, "escalation", "T1")
    assert event.task == "T1"
    assert event.actor == "auto"


# --- locked gates through every input -----------------------------------------------------------------------------

ODD_MODES: list[Any] = [
    "AUTO",
    "Auto",
    " auto",
    "auto ",
    "auto\n",
    "",
    "human ",
    None,
    0,
    1,
    True,
    False,
    ["auto"],
    {"mode": "auto"},
]


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("value", ["auto", *ODD_MODES], ids=repr)
def test_a2_a_locked_gate_reads_human_whatever_the_snapshot_holds(
    tmp_path: Path, gate: str, value: Any
) -> None:
    """8f73f70: "a snapshot mode outside human/auto reads human" and locked gates read human at every layer, so
    `ultron_settings` shows them human. JSON lists and objects are values outside human/auto too."""
    gk = forged_snapshot(tmp_path, dict.fromkeys(GATE_NAMES, value))
    settings = gk.settings()
    assert settings.modes[gate] == "human"
    assert settings.mode(gate) == "human"
    assert settings.mode(gate, "T1") == "human"


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("value", ["auto", *ODD_MODES], ids=repr)
def test_a2_auto_never_passes_a_locked_gate_whatever_the_snapshot_holds(
    tmp_path: Path, gate: str, value: Any
) -> None:
    gk = forged_snapshot(tmp_path, dict.fromkeys(GATE_NAMES, value))
    complete(gk)
    refused_any(gk, lambda: auto_decide(gk, gate))
    gk.decide(gate, REQ, "approve", "", HUMAN, "form")  # a human still can


@pytest.mark.parametrize("value", ODD_MODES, ids=repr)
def test_a2_an_odd_snapshot_mode_reads_human_for_a_changeable_gate(
    tmp_path: Path, value: Any
) -> None:
    gk = forged_snapshot(tmp_path, dict.fromkeys(GATE_NAMES, value))
    settings = gk.settings()
    for gate in CHANGEABLE:
        assert settings.mode(gate) == "human", gate
    at_tasks(gk, {"T1": []})
    refused(gk, lambda: auto_decide(gk, "tasks"))


@pytest.mark.parametrize(
    "gates",
    [
        {
            "MERGE": "auto",
            "Merge": "auto",
            " merge": "auto",
            "merge ": "auto",
            "DEPLOY": "auto",
            "Rollback": "auto",
            "rollback\n": "auto",
        },
        ["merge", "auto"],
        "merge=auto",
        None,
        {},
    ],
    ids=["spelled-keys", "list", "string", "null", "empty"],
)
def test_a2_odd_snapshot_shapes_leave_locked_gates_human(
    tmp_path: Path, gates: object
) -> None:
    gk = forged_snapshot(tmp_path, gates)
    for gate in LOCKED:
        assert gk.settings().mode(gate) == "human"
    complete(gk)
    for gate in LOCKED:
        refused(gk, lambda g=gate: auto_decide(gk, g))


def test_a2_a_later_snapshot_cannot_make_a_locked_gate_auto(tmp_path: Path) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    every_auto = {"max_attempts": 9, "gates": dict.fromkeys(GATE_NAMES, "auto")}
    gk.log.append(
        ev.NewEvent(
            actor="engine", type=ev.CONFIG_SNAPSHOT, requirement=REQ, data=every_auto
        )
    )
    forge(gk, ev.CONFIG_SNAPSHOT, every_auto, actor="engine", extra={"replaces": 1})
    settings = gk.settings()
    assert settings.max_attempts == 3  # the submit snapshot is the one in force
    for gate in LOCKED:
        assert settings.mode(gate) == "human"
        refused(gk, lambda g=gate: auto_decide(gk, g))


def _change(gate: object, to: object = "auto", **more: Any) -> dict[str, Any]:
    return {
        "gate": gate,
        "from": "human",
        "to": to,
        "reached": False,
        "reached_tasks": [],
        "warning": None,
        **more,
    }


def forged_change(
    gate: str, variant: str
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    """(actor, type, data, unknown top-level fields) of a config change written by hand to the log."""
    plain = (HUMAN, CONFIG_CHANGE)
    table: dict[str, tuple[str, str, dict[str, Any], dict[str, Any] | None]] = {
        "human": (*plain, _change(gate), None),
        "unknown-fields": (
            *plain,
            _change(gate, locked=False, force=True, override="auto"),
            {"locked": False, "override": True},
        ),
        "engine": ("engine", CONFIG_CHANGE, _change(gate), None),
        "auto": ("auto", CONFIG_CHANGE, _change(gate), None),
        "agent": ("agent:developer", CONFIG_CHANGE, _change(gate), None),
        "user-agent": ("user:agent:developer", CONFIG_CHANGE, _change(gate), None),
        "gate-upper": (*plain, _change(gate.upper()), None),
        "gate-title": (*plain, _change(gate.title()), None),
        "gate-lead-space": (*plain, _change(f" {gate}"), None),
        "gate-trail-space": (*plain, _change(f"{gate} "), None),
        "gate-list": (*plain, _change([gate]), None),
        "to-upper": (*plain, _change(gate, "AUTO"), None),
        "to-space": (*plain, _change(gate, " auto"), None),
        "to-list": (*plain, _change(gate, ["auto"]), None),
        "to-object": (*plain, _change(gate, {"to": "auto"}), None),
        "to-null": (*plain, _change(gate, None), None),
        "to-true": (*plain, _change(gate, True), None),
        "type-upper": (HUMAN, "CONFIG_CHANGE", _change(gate), None),
        "type-space": (HUMAN, "config_change ", _change(gate), None),
        "reached-tasks": (*plain, _change(gate, reached_tasks=["T1"]), None),
        "reached-text": (*plain, _change(gate, reached="false"), None),
    }
    return table[variant]


LOCKED_CHANGE_VARIANTS = [
    "human",
    "unknown-fields",
    "engine",
    "auto",
    "agent",
    "user-agent",
    "gate-upper",
    "gate-title",
    "gate-lead-space",
    "gate-trail-space",
    "gate-list",
    "to-upper",
    "to-space",
    "to-list",
    "to-object",
    "type-upper",
    "type-space",
    "reached-tasks",
    "reached-text",
]


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("variant", LOCKED_CHANGE_VARIANTS)
def test_a2_no_config_change_in_the_log_makes_a_locked_gate_auto(
    tmp_path: Path, gate: str, variant: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    actor, type_, data, extra = forged_change(gate, variant)
    forge(gk, type_, data, actor=actor, extra=extra)
    settings = gk.settings()
    assert settings.mode(gate) == "human"
    assert settings.mode(gate, "T1") == "human"
    assert all(change.gate not in LOCKED for change in settings.changes)
    refused(gk, lambda: auto_decide(gk, gate))


CHANGEABLE_CHANGE_VARIANTS = [
    "engine",
    "auto",
    "agent",
    "gate-upper",
    "gate-lead-space",
    "gate-list",
    "to-upper",
    "to-space",
    "to-list",
    "to-object",
    "to-null",
    "to-true",
    "type-upper",
    "type-space",
]


@pytest.mark.parametrize("variant", CHANGEABLE_CHANGE_VARIANTS)
def test_a2_a_change_change_gate_could_not_write_is_skipped(
    tmp_path: Path, variant: str
) -> None:
    """8f73f70: `settings_from` "ignores a config_change that change_gate could not have written (not by a human
    identity, an unknown gate, a mode outside human/auto)". Skipped means the settings still read, unchanged."""
    gk = gk_for(tmp_path, {"preset": "strict"})  # plan is human
    in_progress(gk, {"T1": []})
    actor, type_, data, extra = forged_change("plan", variant)
    forge(gk, type_, data, actor=actor, extra=extra)
    settings = gk.settings()
    assert settings.mode("plan") == "human"
    assert settings.mode("plan", "T1") == "human"
    at_plan(gk, "T1")
    refused_any(gk, lambda: auto_decide(gk, "plan", "T1"))


@pytest.mark.parametrize("variant", ["human", "unknown-fields"])
def test_a2_a_well_formed_change_still_applies(tmp_path: Path, variant: str) -> None:
    """Control for the test above: a change `change_gate` could have written (unknown extra fields are kept, not
    fatal: T05) does apply to a task that has not reached the gate."""
    gk = gk_for(tmp_path, {"preset": "strict"})
    in_progress(gk, {"T1": []})
    actor, type_, data, extra = forged_change("plan", variant)
    forge(gk, type_, data, actor=actor, extra=extra)
    assert gk.settings().mode("plan", "T1") == "auto"
    at_plan(gk, "T1")
    assert auto_decide(gk, "plan", "T1").actor == "auto"


def test_a2_hand_built_settings_keep_locked_gates_human() -> None:
    every_auto = GateSettings(
        max_attempts=3,
        modes=dict.fromkeys(GATE_NAMES, "auto"),
        changes=(
            *(GateChange(g, "auto", False, ("T1",)) for g in LOCKED),
            *(GateChange(g, "auto", True) for g in LOCKED),
            *(GateChange(g, "auto", False) for g in LOCKED),
        ),
    )
    for gate in LOCKED:
        assert every_auto.mode(gate) == "human"
        assert every_auto.mode(gate, "T1") == "human"
        assert every_auto.mode(gate, "T2") == "human"
    odd = GateSettings(
        max_attempts=3,
        modes={"plan": "AUTO", "spec": " auto", "tasks": "Auto"},
        changes=(GateChange("intent", "AUTO", False), GateChange("result", "", False)),
    )
    for gate in ("plan", "spec", "tasks", "intent", "result", "escalation"):
        assert odd.mode(gate) == "human", gate
    for spelled in ("MERGE", " merge", "merge ", "Deploy"):
        with pytest.raises(GateError):
            every_auto.mode(spelled)


@pytest.mark.parametrize("preset", sorted(SPEC_8))
@pytest.mark.parametrize(
    "override",
    [
        None,
        {"gates": {"preset": "unattended"}},
        {"gates": {"preset": "strict"}},
        {"gates": dict.fromkeys(CHANGEABLE, "auto")},
    ],
    ids=["none", "to-unattended", "to-strict", "every-changeable-auto"],
)
def test_a2_no_preset_or_override_makes_a_locked_gate_auto(
    tmp_path: Path, preset: str, override: dict[str, Any] | None
) -> None:
    gk = gk_for(tmp_path, {"preset": preset}, override)
    [snap] = [e for e in evs(gk) if e.type == ev.CONFIG_SNAPSHOT]
    complete(gk)
    for gate in LOCKED:
        assert snap.data["gates"][gate] == "human"
        assert gk.settings().mode(gate) == "human"
        refused(gk, lambda g=gate: auto_decide(gk, g))


def _odd_gates(gate: str, spelling: str) -> dict[str, Any]:
    return {
        "upper": {gate.upper(): "auto"},
        "title": {gate.title(): "auto"},
        "lead-space": {f" {gate}": "auto"},
        "value-Auto": {gate: "Auto"},
        "value-AUTO": {gate: "AUTO"},
        "value-space": {gate: " auto"},
        "value-human-space": {gate: "human "},
        "value-null": {gate: None},
        "value-true": {gate: True},
        "value-list": {gate: ["auto"]},
        "with-unattended": {"preset": "unattended", gate: "auto"},
    }[spelling]


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("source", ["repo", "override"])
@pytest.mark.parametrize(
    "spelling",
    [
        "upper",
        "title",
        "lead-space",
        "value-Auto",
        "value-AUTO",
        "value-space",
        "value-human-space",
        "value-null",
        "value-true",
        "value-list",
        "with-unattended",
    ],
)
def test_a2_an_odd_setting_for_a_locked_gate_is_refused_or_stays_human(
    tmp_path: Path, gate: str, source: str, spelling: str
) -> None:
    odd = _odd_gates(gate, spelling)
    try:
        if source == "repo":
            gk = gk_for(tmp_path, odd)
        else:
            gk = gk_for(tmp_path, {"preset": "unattended"}, {"gates": odd})
    except ValueError:
        return  # refused at submit (T04)
    [snap] = [e for e in evs(gk) if e.type == ev.CONFIG_SNAPSHOT]
    assert snap.data["gates"][gate] == "human"
    assert gk.settings().mode(gate) == "human"
    complete(gk)
    refused(gk, lambda: auto_decide(gk, gate))


@pytest.mark.parametrize("gate", LOCKED)
def test_a2_change_gate_keeps_a_locked_gate_human(tmp_path: Path, gate: str) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    for mode in ("auto", "AUTO", "Auto", " auto", "auto ", "off", ""):
        refused(gk, lambda m=mode: gk.change_gate(gate, m, who=HUMAN))
    for spelled in (gate.upper(), gate.title(), f" {gate}", f"{gate} "):
        refused(gk, lambda s=spelled: gk.change_gate(s, "auto", who=HUMAN))
    try:
        gk.change_gate(gate, "human", who=HUMAN)  # may be recorded; it changes nothing
    except ValueError:
        pass
    assert gk.settings().mode(gate) == "human"
    refused(gk, lambda: auto_decide(gk, gate))


@pytest.mark.parametrize("gate", LOCKED)
def test_a2_odd_spellings_in_decide_cannot_pass_a_locked_gate(
    tmp_path: Path, gate: str
) -> None:
    gk = gk_for(tmp_path, {"preset": "unattended"})
    complete(gk)
    for spelled in (gate.upper(), gate.title(), f" {gate}", f"{gate} ", f"{gate}\n"):
        for who, ch in (("auto", "auto"), (HUMAN, "form")):
            refused(
                gk,
                lambda s=spelled, w=who, c=ch: gk.decide(
                    s, REQ, "approve", "", w, c, checks_passed=True
                ),
            )
    for who, ch in (
        ("auto", "Auto"),
        ("auto", "AUTO"),
        ("auto", " auto"),
        ("AUTO", "auto"),
        ("Auto", "auto"),
        ("auto ", "auto"),
        (HUMAN, "Form"),
        (HUMAN, "FORM"),
        (HUMAN, " form"),
        (HUMAN, "Terminal"),
        (HUMAN, "auto"),
    ):
        refused(
            gk,
            lambda w=who, c=ch: gk.decide(
                gate, REQ, "approve", "", w, c, checks_passed=True
            ),
        )
    for decision in ("Approve", "APPROVE", " approve", "approve "):
        refused(gk, lambda d=decision: gk.decide(gate, REQ, d, "", HUMAN, "form"))
    assert gk.decide(gate, REQ, "approve", "", HUMAN, "form").actor == HUMAN  # control


# --- the escalation gate from every task state (spec §3, §8, §12, D27) --------------------------------------------

UNREACHED = (
    "pending",
    "planning",
    "awaiting_plan",
    "plan_approved",
    "coding",
    "testing",
    "passed",
    "awaiting_result",
    "approved",
    "held",
    "blocked",
    "planning_after_a_failed_attempt",
)
REACHED = (
    "failed",
    "rejected",
    "escalated_after_failing",
    "escalated_after_rejection",
    "escalated_by_a_stop",
    "escalated_at_the_cap",
    "call_for_edit_before_filing_failed",
    "call_for_edit_before_filing_rejected",
    "call_for_edit_after_filing",
    "escalated_after_an_edit",
)
_TWO_FAILURES = (
    "planning_after_a_failed_attempt",
    "failed",
    "escalated_after_failing",
    "call_for_edit_before_filing_failed",
    "blocked",
    "call_for_edit_after_filing",
    "escalated_after_an_edit",
)


def drive(gk: Gatekeeper, target: str) -> tuple[str, TaskState]:
    """Bring a task to `target` with T08's events (setup; never through decide). Return the task and its state.

    T1 (2 attempts) is the one driven; T2 depends on T1 (it is the `blocked` one); T9 stays pending, so the
    requirement stays in progress.
    """
    in_progress(gk, {"T1": [], "T2": ["T1"], "T9": []}, ma=2)
    if target == "pending":
        return "T1", T.PENDING
    put(gk, "task_started", "T1")
    if target == "planning":
        return "T1", T.PLANNING
    put(gk, "plan_drafted", "T1")
    if target == "awaiting_plan":
        return "T1", T.AWAITING_PLAN
    if (
        target == "escalated_at_the_cap"
    ):  # the spending cap escalates from any unfinished state (FR18)
        gk.log.append(ev.escalation(REQ, reason="cap reached", task="T1"))
        return "T1", T.ESCALATED
    rec(gk, "plan", task="T1")
    if target == "plan_approved":
        return "T1", T.PLAN_APPROVED
    gk.log.append(ev.attempt_started(REQ, "T1", 1))
    if target == "coding":
        return "T1", T.CODING
    if target == "held":
        put(gk, "agent.stopped", "T1", then="hold", reason="no progress")
        return "T1", T.HELD
    if target == "escalated_by_a_stop":
        put(gk, "agent.stopped", "T1", then="escalate", reason="looping")
        return "T1", T.ESCALATED
    put(gk, "coding_finished", "T1")
    if target == "testing":
        return "T1", T.TESTING
    if target in _TWO_FAILURES:
        gk.log.append(ev.attempt_finished(REQ, "T1", 1, outcome="failed"))
        if target == "planning_after_a_failed_attempt":
            return "T1", T.PLANNING
        put(gk, "plan_drafted", "T1")
        rec(gk, "plan", task="T1")
        gk.log.append(ev.attempt_started(REQ, "T1", 2))
        put(gk, "coding_finished", "T1")
        gk.log.append(ev.attempt_finished(REQ, "T1", 2, outcome="failed"))
        if target == "failed":
            return "T1", T.FAILED
        if target == "call_for_edit_before_filing_failed":
            put(gk, ev.ATTACHMENT_MISSING, "T1", missing=["report.md"])
            return "T1", T.CALL_FOR_EDIT
        gk.log.append(ev.escalation(REQ, reason="all attempts failed", task="T1"))
        if target == "escalated_after_failing":
            return "T1", T.ESCALATED
        if target == "blocked":
            return "T2", T.BLOCKED
        put(gk, ev.EDIT_REQUESTED, "T1", question="which test fails first?")
        if target == "call_for_edit_after_filing":
            return "T1", T.CALL_FOR_EDIT
        put(gk, ev.EDIT_DONE, "T1")
        return "T1", T.ESCALATED  # escalated_after_an_edit
    gk.log.append(ev.attempt_finished(REQ, "T1", 1, outcome="passed"))
    if target == "passed":
        return "T1", T.PASSED
    put(gk, "result_requested", "T1")
    if target == "awaiting_result":
        return "T1", T.AWAITING_RESULT
    if target == "approved":
        rec(gk, "result", task="T1")
        return "T1", T.APPROVED
    rec(gk, "result", "reject", task="T1", comments="wrong approach")
    if target == "rejected":
        return "T1", T.REJECTED
    if target == "call_for_edit_before_filing_rejected":
        put(gk, ev.ATTACHMENT_MISSING, "T1", missing=["plan.md"])
        return "T1", T.CALL_FOR_EDIT
    gk.log.append(ev.escalation(REQ, reason="rejected", task="T1"))
    return "T1", T.ESCALATED  # escalated_after_rejection


def test_a2_drive_covers_every_task_state(tmp_path: Path) -> None:
    seen = set()
    for i, target in enumerate((*UNREACHED, *REACHED)):
        gk = gk_for(tmp_path, sub=f"s{i}")
        task, state = drive(gk, target)
        assert task_state(gk, task) is state, target
        seen.add(state)
    assert seen == set(TaskState)


ESCALATION_KINDS = [
    ("auto", "auto", "approve"),
    (HUMAN, "form", "approve"),
    (HUMAN, "terminal", "approve"),
    (HUMAN, "form", "reject"),
    (HUMAN, "terminal", "changes"),
]


@pytest.mark.parametrize("target", UNREACHED)
@pytest.mark.parametrize(("who", "channel", "decision"), ESCALATION_KINDS)
def test_a2_escalation_is_refused_before_the_task_reaches_it(
    tmp_path: Path, target: str, who: str, channel: str, decision: str
) -> None:
    gk = gk_for(tmp_path)  # escalation is auto by default
    task, state = drive(gk, target)
    assert task_state(gk, task) is state
    assert not gate_reached(evs(gk), "escalation", task)
    refused(
        gk,
        lambda: gk.decide(
            "escalation", task, decision, "why", who, channel, checks_passed=True
        ),
    )


@pytest.mark.parametrize("target", REACHED)
@pytest.mark.parametrize(
    ("who", "channel"), [("auto", "auto"), (HUMAN, "form"), (HUMAN, "terminal")]
)
def test_a2_escalation_is_taken_once_the_task_reaches_it(
    tmp_path: Path, target: str, who: str, channel: str
) -> None:
    gk = gk_for(tmp_path)
    task, state = drive(gk, target)
    assert task_state(gk, task) is state
    assert gate_reached(evs(gk), "escalation", task)
    before = fold(evs(gk))
    event = gk.decide(
        "escalation", task, "approve", "", who, channel, checks_passed=who == "auto"
    )
    assert event.task == task
    assert event.actor == who
    assert event.data["gate"] == "escalation"
    assert (
        fold(evs(gk)) == before
    )  # §3: escalating is the engine's move, not the gate's


def test_a2_a_forged_escalation_decision_never_counts_as_reached(
    tmp_path: Path,
) -> None:
    gk = gk_for(tmp_path)  # escalation is auto
    in_progress(gk, {"T1": [], "T9": []}, ma=1)
    at_plan(gk, "T1")
    rec(gk, "escalation", task="T1", actor="auto", channel="auto")
    forge(
        gk,
        ev.GATE_DECISION,
        {
            "gate": "escalation",
            "decision": "approve",
            "channel": "form",
            "comments": "",
            "version": None,
        },
        task="T1",
        extra={"reached": True},
    )
    assert not gate_reached(evs(gk), "escalation", "T1")
    for who, ch in (("auto", "auto"), (HUMAN, "form")):
        refused(
            gk,
            lambda w=who, c=ch: gk.decide(
                "escalation", "T1", "approve", "", w, c, checks_passed=True
            ),
        )
    change = gk.change_gate("escalation", "human", who=HUMAN)
    assert "T1" not in change.data["reached_tasks"]
    assert gk.settings().mode("escalation", "T1") == "human"
    run_attempt(gk, "T1", 1, "failed")
    assert task_state(gk, "T1") is T.FAILED
    refused(gk, lambda: auto_decide(gk, "escalation", "T1"))
    assert (
        gk.decide("escalation", "T1", "approve", "file it", HUMAN, "form").task == "T1"
    )


@pytest.mark.parametrize(
    "target",
    ["failed", "rejected", "escalated_after_failing", "call_for_edit_after_filing"],
)
def test_a2_a_repeat_escalation_decision_never_changes_state(
    tmp_path: Path, target: str
) -> None:
    """§8: the escalation gate confirms before a Jira ticket is created; D27/§12 make a repeat `escalation` record
    (the Jira key, a retried filing) a no-op. So a second decision on the same task's gate may at most be recorded:
    it never moves the task or the requirement, never counts as an attempt, and the gate keeps the mode the task had
    when it reached it. (Whether a repeat should be refused outright is for the owner; see the report.)"""
    gk = gk_for(tmp_path)
    task, _ = drive(gk, target)
    auto_decide(gk, "escalation", task)
    settled = fold(evs(gk))
    for who, ch, decision in (
        ("auto", "auto", "approve"),
        (HUMAN, "form", "approve"),
        (HUMAN, "terminal", "reject"),
        (HUMAN, "form", "changes"),
    ):
        before = gk.log.path.read_bytes()
        try:
            gk.decide(
                "escalation",
                task,
                decision,
                "again",
                who,
                ch,
                checks_passed=who == "auto",
            )
        except ValueError:
            assert gk.log.path.read_bytes() == before
        assert fold(evs(gk)) == settled
    gk.change_gate("escalation", "human", who=HUMAN)
    assert gk.settings().mode("escalation", task) == "auto"
    assert fold(evs(gk)) == settled


# --- the maintain gate: what holds whatever T33 defines as reaching it --------------------------------------------


@pytest.mark.parametrize(
    "stage",
    ["submitted", "awaiting_intent", "spec_drafting", "in_progress", "complete"],
)
def test_a2_a_maintain_decision_never_moves_the_pipeline(
    tmp_path: Path, stage: str
) -> None:
    gk = gk_for(tmp_path)  # maintain is auto by default
    if stage == "awaiting_intent":
        at_intent(gk)
    elif stage == "spec_drafting":
        put(gk, "spec_started")
    elif stage == "in_progress":
        in_progress(gk, {"T1": []})
        at_plan(gk, "T1")
    elif stage == "complete":
        complete(gk)
    before = fold(evs(gk))
    for who, ch, decision in (
        ("auto", "auto", "approve"),
        (HUMAN, "form", "approve"),
        (HUMAN, "form", "reject"),
        (HUMAN, "terminal", "changes"),
    ):
        raw = gk.log.path.read_bytes()
        try:
            gk.decide(
                "maintain", REQ, decision, "c", who, ch, checks_passed=who == "auto"
            )
        except ValueError:
            assert gk.log.path.read_bytes() == raw
        assert fold(evs(gk)) == before


def test_a2_auto_maintain_follows_the_requirements_mode(tmp_path: Path) -> None:
    strict = gk_for(tmp_path, {"preset": "strict"}, sub="strict")
    refused(strict, lambda: auto_decide(strict, "maintain"))
    changed = gk_for(
        tmp_path, sub="changed"
    )  # auto by default, set human before any maintain decision
    changed.change_gate("maintain", "human", who=HUMAN)
    refused(changed, lambda: auto_decide(changed, "maintain"))
    assert changed.decide("maintain", REQ, "approve", "", HUMAN, "form").actor == HUMAN
    default = gk_for(tmp_path, sub="default")
    refused(
        default,
        lambda: default.decide("maintain", REQ, "approve", "", "auto", "auto"),
    )
    for who in NOT_HUMAN:
        refused(
            default,
            lambda w=who: default.decide(
                "maintain", REQ, "approve", "", w, "form", checks_passed=True
            ),
        )
    assert auto_decide(default, "maintain").actor == "auto"

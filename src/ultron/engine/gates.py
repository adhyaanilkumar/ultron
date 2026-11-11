"""Gates: the gate table, the settings snapshot, mid-run changes, decisions and `decisions.md` (spec §8, FR4, FR16).

Every gate decision goes through `Gatekeeper.decide`, the single function that records one (NFR1):

- `who` is `auto` or a human git identity (`user:<name> <email>`). The engine and ultron agents never pass a gate;
- `merge`, `deploy` and `rollback` are locked: they refuse `auto` and any `who` that is not a human;
- `auto` only approves, only when the requirement's settings make that gate `auto` for that item, only when the
  engine says the stage's own checks passed, and never on a failing task;
- the decision is folded with the state machine (T08) before it is written, so an illegal one writes nothing.

Settings are snapshotted when a requirement is submitted (`config_snapshot`, T05). A mid-run change is a
`config_change` event that applies only to gates (or, for task gates, tasks) that have not reached the gate yet, so
a gate already reached or passed keeps its mode and is never re-opened. `decisions.md` is rendered from the
`gate_decision` events and is never edited by hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from ultron.config.defaults import DEFAULT_HUMAN_GATES, LOCKED_GATES
from ultron.config.loader import LoadedConfig
from ultron.engine.states import (
    IllegalTransition,
    RequirementState,
    Snapshot,
    TaskState,
    fold,
)
from ultron.engine.version import parse_version
from ultron.store.eventlog import EventLog
from ultron.store.events import (
    ATTEMPT_FINISHED,
    CONFIG_SNAPSHOT,
    GATE_CHANNELS,
    GATE_DECISION,
    GATE_DECISIONS,
    GATE_MODES,
    SCHEMA_VERSION,
    Event,
    EventError,
    NewEvent,
    config_snapshot,
    gate_decision,
    validate_actor,
)

__all__ = [
    "CONFIG_CHANGE",
    "CONFIG_WARNING",
    "GATES",
    "GateChange",
    "GateError",
    "GateInfo",
    "GateSettings",
    "Gatekeeper",
    "feedback",
    "gate_reached",
    "render_decisions",
    "resolve_gates",
    "settings_from",
]

CONFIG_CHANGE = "config_change"
"""A mid-run change to one gate's mode (spec §8 "Snapshot"): who, gate, from, to and what it no longer applies to."""
CONFIG_WARNING = "config_warning"
"""A default-human gate set to `auto` at submit (spec §8 "Warning"), recorded beside the snapshot."""

R = RequirementState
T = TaskState
Level = Literal["requirement", "task"]
DraftState = RequirementState | TaskState


class GateError(ValueError):
    """A gate decision or a settings change breaks a gate rule."""


@dataclass(frozen=True)
class GateInfo:
    """One gate: where it sits and what `changes` does."""

    name: str
    level: Level
    """`requirement`: the item is the requirement id. `task`: the item is a task id."""
    awaiting: DraftState | None
    """The state the item waits in at this gate; `None` for gates outside the state machine."""
    drafting: DraftState | None
    """Where `changes` sends the item; `None` where the gate has no `changes` loop."""
    drafted_event: str | None
    """The event that means the gate was reached (a new draft for it to judge)."""
    locked: bool = False


GATES: Mapping[str, GateInfo] = MappingProxyType(
    {
        "intent": GateInfo(
            "intent",
            "requirement",
            R.AWAITING_INTENT,
            R.INTAKE_DRAFTING,
            "intent_drafted",
        ),
        "spec": GateInfo(
            "spec", "requirement", R.AWAITING_SPEC, R.SPEC_DRAFTING, "spec_drafted"
        ),
        "tasks": GateInfo(
            "tasks", "requirement", R.AWAITING_TASKS, R.DECOMPOSING, "tasks_drafted"
        ),
        "plan": GateInfo("plan", "task", T.AWAITING_PLAN, T.PLANNING, "plan_drafted"),
        "result": GateInfo(
            "result", "task", T.AWAITING_RESULT, None, "result_requested"
        ),
        "escalation": GateInfo("escalation", "task", None, None, None),
        "maintain": GateInfo("maintain", "requirement", None, None, None),
        **{
            name: GateInfo(name, "requirement", None, None, None, locked=True)
            for name in LOCKED_GATES
        },
    }
)
"""The ten gates of spec §8, in the order of the table."""

_ESCALATION_REACHED = frozenset(
    {T.FAILED, T.REJECTED, T.ESCALATED, T.CALL_FOR_EDIT, T.BLOCKED}
)
_FAILING = frozenset({T.FAILED, T.REJECTED, T.ESCALATED, T.CALL_FOR_EDIT, T.BLOCKED})
"""A task in one of these states is never auto-approved (except at the escalation gate, which exists for them)."""


def _gate(name: object) -> GateInfo:
    if not isinstance(name, str) or name not in GATES:
        raise GateError(f"unknown gate {name!r}; gates are {', '.join(GATES)}")
    return GATES[name]


def _is_human(who: object) -> bool:
    if not isinstance(who, str) or not who.startswith("user:"):
        return False
    try:
        validate_actor(who)
    except EventError:
        return False
    return True


# --- settings ---------------------------------------------------------------------------------------------------


def resolve_gates(loaded: LoadedConfig) -> dict[str, str]:
    """Each gate's mode (`human` or `auto`) from a validated config; locked gates are always `human`."""
    gates = loaded.config.gates
    return {
        name: ("human" if info.locked else gates.value(name))
        for name, info in GATES.items()
    }


@dataclass(frozen=True)
class GateChange:
    """One recorded mid-run change."""

    gate: str
    to: str
    reached: bool
    """Requirement-level gate: it had already been reached, so the change does not apply to it."""
    reached_tasks: tuple[str, ...] = ()
    """Task-level gate: tasks that had already reached it keep their mode."""


@dataclass(frozen=True)
class GateSettings:
    """A requirement's settings: the snapshot plus the mid-run changes, in order."""

    max_attempts: int
    modes: Mapping[str, str]
    changes: tuple[GateChange, ...] = ()

    def mode(self, gate: str, task: str | None = None) -> str:
        """The mode of `gate` for `task` (task gates) or for the requirement.

        For a task gate with `task=None`, the mode a task that has not reached the gate yet would get.
        """
        info = _gate(gate)
        mode = self.modes[gate]
        for change in self.changes:
            if change.gate != gate:
                continue
            if info.level == "requirement":
                if not change.reached:
                    mode = change.to
            elif task is None or task not in change.reached_tasks:
                mode = change.to
        return mode


def settings_from(events: Iterable[Event]) -> GateSettings:
    """Fold the snapshot and the changes. Raises `GateError` if there is no snapshot."""
    snapshot: Event | None = None
    changes: list[GateChange] = []
    for event in events:
        if event.type == CONFIG_SNAPSHOT and snapshot is None:
            snapshot = event
        elif event.type == CONFIG_CHANGE:
            data = event.data
            changes.append(
                GateChange(
                    gate=str(data["gate"]),
                    to=str(data["to"]),
                    reached=bool(data.get("reached", False)),
                    reached_tasks=tuple(str(t) for t in data.get("reached_tasks", [])),
                )
            )
    if snapshot is None:
        raise GateError(
            "no settings snapshot for this requirement; it is written at submit"
        )
    modes: dict[str, str] = {name: "human" for name in GATES}
    modes.update({str(k): str(v) for k, v in snapshot.data.get("gates", {}).items()})
    for name in LOCKED_GATES:
        modes[name] = "human"
    return GateSettings(
        max_attempts=int(snapshot.data["max_attempts"]),
        modes=MappingProxyType(modes),
        changes=tuple(changes),
    )


def gate_reached(events: Sequence[Event], gate: str, task: str | None = None) -> bool:
    """Whether the requirement (or `task`, for task gates) has reached `gate`: a draft or a decision for it exists."""
    info = _gate(gate)
    for event in events:
        if info.level == "task" and event.task != task:
            continue
        if event.type == GATE_DECISION and event.data.get("gate") == gate:
            return True
        if info.drafted_event is not None and event.type == info.drafted_event:
            return True
    if gate == "tasks":
        # an auto `tasks` gate can pass straight from `decomposing` (T08)
        return fold(events).requirement not in _BEFORE_TASKS
    if gate == "escalation" and task is not None:
        status = fold(events).tasks.get(task)
        return status is not None and status.state in _ESCALATION_REACHED
    return False


_BEFORE_TASKS = frozenset(
    {
        R.SUBMITTED,
        R.INTAKE_DRAFTING,
        R.AWAITING_INTENT,
        R.SPEC_DRAFTING,
        R.AWAITING_SPEC,
        R.SPEC_APPROVED,
        R.DECOMPOSING,
    }
)


def feedback(events: Iterable[Event], gate: str, item: str) -> str | None:
    """The comment of the latest `changes` decision on `item` at `gate`, until the item's next draft.

    This is what the drafting agent gets on its next run (spec, plan, intent).
    """
    info = _gate(gate)
    task = item if info.level == "task" else None
    pending: str | None = None
    for event in events:
        if info.level == "task" and event.task != task:
            continue
        if (
            event.type == GATE_DECISION
            and event.data.get("gate") == gate
            and event.data.get("decision") == "changes"
        ):
            pending = str(event.data.get("comments", ""))
        elif info.drafted_event is not None and event.type == info.drafted_event:
            pending = None
    return pending


# --- decisions.md -----------------------------------------------------------------------------------------------


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>").strip()


def render_decisions(events: Iterable[Event], requirement: str) -> str:
    """`decisions.md` for a requirement, from its `gate_decision` events in `seq` order. Deterministic."""
    rows = sorted(
        (e for e in events if e.type == GATE_DECISION and e.requirement == requirement),
        key=lambda e: e.seq,
    )
    lines = [
        f"# Decisions: {requirement}",
        "",
        "Generated from `events.jsonl` by ultron. Do not edit by hand: it is rewritten from the events.",
        "",
    ]
    if not rows:
        lines.append("No decisions yet.")
    else:
        lines.append(
            "| # | Time | Gate | Item | Decision | Who | Channel | Version | Comment |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for e in rows:
            who = e.actor.removeprefix("user:")
            cells = [
                e.seq,
                e.time,
                e.data.get("gate"),
                e.task or e.requirement,
                e.data.get("decision"),
                who,
                e.data.get("channel"),
                e.data.get("version"),
                e.data.get("comments"),
            ]
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


# --- the gatekeeper ---------------------------------------------------------------------------------------------


class Gatekeeper:
    """Records one requirement's settings and gate decisions in its event log."""

    def __init__(self, log: EventLog, requirement: str) -> None:
        self.log = log
        self.requirement = requirement

    def _events(self) -> list[Event]:
        return self.log.read(strict=True).events

    # settings

    def record_snapshot(
        self, loaded: LoadedConfig, *, actor: str = "engine"
    ) -> list[Event]:
        """Write the settings snapshot at submit, with one `config_warning` per default-human gate set to `auto`."""
        if any(e.type == CONFIG_SNAPSHOT for e in self._events()):
            raise GateError(
                f"{self.requirement} already has a settings snapshot; use change_gate"
            )
        batch = [
            config_snapshot(
                self.requirement,
                max_attempts=loaded.config.max_attempts,
                gates=resolve_gates(loaded),
                actor=actor,
            )
        ]
        for warning in loaded.warnings:
            batch.append(
                NewEvent(
                    actor=actor,
                    type=CONFIG_WARNING,
                    requirement=self.requirement,
                    data={
                        "gate": warning.gate,
                        "from": warning.from_value,
                        "to": warning.to_value,
                        "message": warning.message,
                    },
                )
            )
        return self.log.append(batch)

    def settings(self) -> GateSettings:
        return settings_from(self._events())

    def change_gate(self, gate: str, mode: str, *, who: str) -> Event:
        """Change a gate's mode mid-run. It applies only to what has not reached the gate yet, and is recorded."""
        info = _gate(gate)
        if mode not in GATE_MODES:
            raise GateError(f"mode must be one of {sorted(GATE_MODES)}, got {mode!r}")
        if not _is_human(who):
            raise GateError(f"only a human git identity can change a gate, not {who!r}")
        if info.locked and mode != "human":
            raise GateError(
                f"gate '{gate}' is locked to human and cannot be set to '{mode}'"
            )
        events = self._events()
        settings = settings_from(events)
        from_mode = settings.mode(gate)
        reached = False
        reached_tasks: list[str] = []
        if info.level == "requirement":
            reached = gate_reached(events, gate)
        else:
            reached_tasks = sorted(
                t for t in fold(events).tasks if gate_reached(events, gate, t)
            )
        warning = (
            f"gate '{gate}' is human by default and is now auto"
            if gate in DEFAULT_HUMAN_GATES and mode == "auto"
            else None
        )
        event = NewEvent(
            actor=who,
            type=CONFIG_CHANGE,
            requirement=self.requirement,
            data={
                "gate": gate,
                "from": from_mode,
                "to": mode,
                "reached": reached,
                "reached_tasks": reached_tasks,
                "warning": warning,
            },
        )
        return self.log.append(event)[0]

    # decisions

    def decide(
        self,
        gate: str,
        item: str,
        decision: str,
        comment: str,
        who: str,
        channel: str,
        version: str | None = None,
        *,
        checks_passed: bool = False,
    ) -> Event:
        """Record one gate decision, or raise and write nothing.

        `item` is the requirement id for requirement gates and the task id for task gates. `checks_passed` is the
        engine's word that the stage's own checks passed; an `auto` decision needs it.
        """
        info = _gate(gate)
        if decision not in GATE_DECISIONS:
            raise GateError(
                f"decision must be one of {sorted(GATE_DECISIONS)}, got {decision!r}"
            )
        if not isinstance(comment, str):
            raise GateError("comment must be a string")
        is_auto = who == "auto"
        if not is_auto and not _is_human(who):
            raise GateError(
                f"{who!r} cannot decide a gate: only a human git identity or the engine's 'auto' can"
            )
        if is_auto != (channel == "auto"):
            raise GateError(
                "an auto decision has who 'auto' and channel 'auto', and only then"
            )
        if channel not in GATE_CHANNELS:
            raise GateError(
                f"channel must be one of {sorted(GATE_CHANNELS)}, got {channel!r}"
            )
        if info.locked and is_auto:
            raise GateError(f"gate '{gate}' is locked: only a human can decide it")
        if decision == "changes" and not comment.strip():
            raise GateError(
                "a 'changes' decision needs a comment for the agent's next run"
            )
        if version is not None and gate != "spec":
            raise GateError("only the spec gate records a version")
        if gate == "spec" and decision == "approve":
            if version is None:
                raise GateError("approving the spec gate needs the confirmed version")
            try:
                parse_version(version)
            except ValueError as exc:
                raise GateError(str(exc)) from exc

        events = self._events()
        snapshot = fold(events)
        task = self._task_for(info, item, snapshot)
        if is_auto:
            self._check_auto(info, task, decision, checks_passed, events, snapshot)

        attempt = None
        if task is not None:
            attempt = snapshot.tasks[task].attempt or None
        new = gate_decision(
            self.requirement,
            actor=who,
            gate=gate,
            decision=decision,
            channel=channel,
            comments=comment,
            task=task,
            attempt=attempt,
            version=version,
        )
        self._check_fold(events, new)
        return self.log.append(new)[0]

    def request_changes(
        self, gate: str, item: str, comment: str, who: str, channel: str
    ) -> DraftState:
        """Send `item` back to its drafting state with `comment` for the agent's next run; return that state."""
        info = _gate(gate)
        if info.drafting is None:
            raise GateError(f"gate '{gate}' has no changes loop")
        self.decide(gate, item, "changes", comment, who, channel)
        snapshot = fold(self._events())
        if info.level == "task":
            return snapshot.tasks[item].state
        return snapshot.requirement

    def feedback(self, gate: str, item: str) -> str | None:
        return feedback(self._events(), gate, item)

    def render_decisions(self) -> str:
        return render_decisions(self._events(), self.requirement)

    def write_decisions(self, path: Path | str) -> Path:
        """Regenerate `decisions.md` at `path` (UTF-8, `\\n` line endings)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.render_decisions().encode("utf-8"))
        return out

    # checks

    def _task_for(self, info: GateInfo, item: str, snapshot: Snapshot) -> str | None:
        if info.level == "requirement":
            if item != self.requirement:
                raise GateError(
                    f"gate '{info.name}' decides the requirement {self.requirement}, not {item!r}"
                )
            return None
        if item not in snapshot.tasks:
            raise GateError(
                f"gate '{info.name}' decides a task; {item!r} is not a task of {self.requirement}"
            )
        return item

    def _check_auto(
        self,
        info: GateInfo,
        task: str | None,
        decision: str,
        checks_passed: bool,
        events: Sequence[Event],
        snapshot: Snapshot,
    ) -> None:
        if decision != "approve":
            raise GateError("auto only approves; changes and reject come from a human")
        if not checks_passed:
            raise GateError(
                f"auto cannot pass '{info.name}': the stage's own checks have not passed"
            )
        mode = settings_from(events).mode(info.name, task)
        if mode != "auto":
            raise GateError(
                f"gate '{info.name}' is {mode} for this item; auto cannot decide it"
            )
        if task is None or info.name == "escalation":
            return
        state = snapshot.tasks[task].state
        if state in _FAILING:
            raise GateError(
                f"{task} is {state.value}: a failing task is never auto-approved"
            )
        if info.name == "result" and _last_outcome(events, task) != "passed":
            raise GateError(
                f"{task}'s last attempt did not pass: a failing task is never auto-approved"
            )

    def _check_fold(self, events: list[Event], new: NewEvent) -> None:
        candidate = Event(
            schema=SCHEMA_VERSION,
            seq=len(events) + 1,
            time="candidate",
            actor=new.actor,
            type=new.type,
            requirement=new.requirement,
            task=new.task,
            attempt=new.attempt,
            data=new.data,
            ultron_version="candidate",
        )
        try:
            fold([*events, candidate])
        except IllegalTransition as exc:
            raise IllegalTransition(f"{new.data['gate']} gate: {exc}") from exc


def _last_outcome(events: Sequence[Event], task: str) -> Any:
    outcome: Any = None
    for event in events:
        if event.type == ATTEMPT_FINISHED and event.task == task:
            outcome = event.data.get("outcome")
    return outcome

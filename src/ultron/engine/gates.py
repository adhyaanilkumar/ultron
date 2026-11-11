"""Gates: the gate table, the settings snapshot, mid-run changes, decisions and `decisions.md` (spec §8, FR4, FR16).

Every gate decision goes through `Gatekeeper.decide`, the single function that records one (NFR1):

- `who` is `auto` or a human git identity (`user:<name> <email>`). The engine and ultron agents never pass a gate;
- `merge`, `deploy` and `rollback` are locked: they refuse `auto` and any `who` that is not a human;
- `auto` only approves, only when the requirement's settings make that gate `auto` for that item, only when the
  engine says the stage's own checks passed, and never on a failing task;
- the `escalation` gate takes a decision only for a task that reached it (failed, rejected or escalating), and only
  one while the task stays there (D30);
- the decision is folded with the state machine (T08) before it is written, so an illegal one writes nothing.

Settings are snapshotted when a requirement is submitted (`config_snapshot`, T05). A mid-run change is a
`config_change` event that applies only to gates (or, for task gates, tasks) that have not reached the gate yet, so
a gate already reached or passed keeps its mode and is never re-opened. Locked gates read `human` whatever any event
says. `decisions.md` is rendered from the `gate_decision` events and is never edited by hand.

Every value read from the log or given by a caller is type-checked before it is compared or converted, so a JSON
list, object, number, bool or null where a string belongs never crashes a read: a snapshot mode other than the string
`human` or `auto` reads `human`, and an event that `change_gate` or `decide` could not have written never counts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeGuard

from ultron.config.defaults import (
    DEFAULT_HUMAN_GATES,
    LOCKED_GATES,
    MAX_ATTEMPTS_DEFAULT,
)
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

_ESCALATION_REACHED = frozenset({T.FAILED, T.REJECTED, T.CALL_FOR_EDIT, T.ESCALATED})
"""A task in one of these states has reached the escalation gate (spec §3, §12, D27, D29): failed and rejected tasks,
`call_for_edit` on the way to filing (any escalating state) or after it, and `escalated` (also a stop with
`then=escalate` and the spending cap). A `blocked` task is never escalated (T08), so it never reaches this gate."""
_FAILING = frozenset({T.FAILED, T.REJECTED, T.ESCALATED, T.CALL_FOR_EDIT, T.BLOCKED})
"""A task in one of these states is never auto-approved (except at the escalation gate, which exists for them)."""


def _one_of(value: object, choices: frozenset[str]) -> TypeGuard[str]:
    """Whether `value` is one of the strings in `choices`.

    The type is checked before the set lookup, so a JSON list or object (which can't be hashed) is simply not one.
    """
    return isinstance(value, str) and value in choices


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

        For a task gate with `task=None`, the mode a task that has not reached the gate yet would get. A locked gate
        is `human` whatever the modes and changes hold. A mode other than the string `human` or `auto`, of any type,
        reads `human`. A change whose `reached` is not a bool, or whose `reached_tasks` is not a tuple of strings,
        never counts (settings built by hand can hold one; `settings_from` never builds one).
        """
        info = _gate(gate)
        if info.locked:
            return "human"
        mode: object = self.modes.get(gate, "human")
        for change in self.changes:
            if change.gate != gate or not _well_formed(change):
                continue
            if info.level == "requirement":
                if not change.reached:
                    mode = change.to
            elif task is None or task not in change.reached_tasks:
                mode = change.to
        if _one_of(mode, GATE_MODES):
            return mode
        return "human"


def _well_formed(change: GateChange) -> bool:
    """`reached` is a bool and `reached_tasks` a tuple of strings, as `settings_from` builds them."""
    return (
        isinstance(change.reached, bool)
        and isinstance(change.reached_tasks, tuple)
        and all(isinstance(t, str) for t in change.reached_tasks)
    )


def _change_from(event: Event) -> GateChange | None:
    """A recorded change, or `None` for one `change_gate` could not have written (it never counts).

    Each field settings read is type-checked before it is compared: the actor is a human identity, `gate` names a
    changeable gate, `to` is the string `human` or `auto`, `reached` is a bool and `reached_tasks` a list of task ids
    (non-empty strings). Any other value there, null included, means `change_gate` did not write the event. An
    absent `reached` or `reached_tasks` reads as nothing reached; fields settings don't read are ignored.
    """
    data = event.data
    gate, to = data.get("gate"), data.get("to")
    reached = data.get("reached", False)
    reached_tasks = data.get("reached_tasks", [])
    if not (
        _is_human(event.actor)
        and isinstance(gate, str)
        and gate in GATES
        and not GATES[gate].locked
        and _one_of(to, GATE_MODES)
        and isinstance(reached, bool)
        and isinstance(reached_tasks, list | tuple)
        and all(isinstance(t, str) and t for t in reached_tasks)
    ):
        return None
    return GateChange(
        gate=gate, to=to, reached=reached, reached_tasks=tuple(reached_tasks)
    )


def settings_from(events: Iterable[Event]) -> GateSettings:
    """Fold the snapshot and the changes. Raises `GateError` if there is no snapshot, and for nothing else.

    Locked gates are `human` whatever the events say: a snapshot value for them is overridden and a change to them
    is skipped. A snapshot mode other than the string `human` or `auto` (a list, object, number, bool, null or any
    other text) reads `human`, and a `max_attempts` that is not a positive integer reads as the default. A change
    `change_gate` could not have written is skipped (see `_change_from`). Only the first snapshot counts.
    """
    snapshot: Event | None = None
    changes: list[GateChange] = []
    for event in events:
        if event.type == CONFIG_SNAPSHOT and snapshot is None:
            snapshot = event
        elif event.type == CONFIG_CHANGE:
            change = _change_from(event)
            if change is not None:
                changes.append(change)
    if snapshot is None:
        raise GateError(
            "no settings snapshot for this requirement; it is written at submit"
        )
    modes: dict[str, str] = dict.fromkeys(GATES, "human")
    recorded = snapshot.data.get("gates")
    if isinstance(recorded, Mapping):
        for name, value in recorded.items():
            if (
                isinstance(name, str)
                and name in GATES
                and not GATES[name].locked
                and _one_of(value, GATE_MODES)
            ):
                modes[name] = value
    return GateSettings(
        max_attempts=_max_attempts(snapshot.data.get("max_attempts")),
        modes=MappingProxyType(modes),
        changes=tuple(changes),
    )


def _max_attempts(value: object) -> int:
    """A snapshot's `max_attempts` when it is a positive integer (T05's builder rule), else the default (3)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return MAX_ATTEMPTS_DEFAULT
    return value


def gate_reached(events: Sequence[Event], gate: str, task: str | None = None) -> bool:
    """Whether the requirement (or `task`, for task gates) has reached `gate`: a draft or a decision for it exists.

    The escalation gate is the exception: it is reached only by a task in an escalating state, never by a decision
    event, so a decision recorded on an unreached escalation gate does not count. A task that is not a string never
    reaches it.
    """
    return _reached(events, None, _gate(gate), task)


def _reached(
    events: Sequence[Event], snapshot: Snapshot | None, info: GateInfo, task: str | None
) -> bool:
    """`gate_reached`, with the folded `snapshot` of `events` when the caller already has it."""
    if info.name == "escalation":
        if not isinstance(task, str):
            return False
        if snapshot is None:
            snapshot = fold(events)
        status = snapshot.tasks.get(task)
        return status is not None and status.state in _ESCALATION_REACHED
    for event in events:
        if info.level == "task" and event.task != task:
            continue
        if event.type == GATE_DECISION and event.data.get("gate") == info.name:
            return True
        if info.drafted_event is not None and event.type == info.drafted_event:
            return True
    if info.name == "tasks":
        # an auto `tasks` gate can pass straight from `decomposing` (T08)
        if snapshot is None:
            snapshot = fold(events)
        return snapshot.requirement not in _BEFORE_TASKS
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

    This is what the drafting agent gets on its next run (spec, plan, intent). A comment that is not a string (one
    `decide` never writes) gives `""`: changes were asked, with no comment that can be read.
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
            comments = event.data.get("comments", "")
            pending = comments if isinstance(comments, str) else ""
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
        if not _one_of(mode, GATE_MODES):
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
        snapshot = fold(events)
        reached = False
        reached_tasks: list[str] = []
        if info.level == "requirement":
            reached = _reached(events, snapshot, info, None)
        else:
            reached_tasks = sorted(
                t for t in snapshot.tasks if _reached(events, snapshot, info, t)
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
        engine's word that the stage's own checks passed; an `auto` decision needs it to be exactly `True`. A gate,
        item, decision, channel, comment or version that is not a string raises `GateError`.
        """
        info = _gate(gate)
        if not _one_of(decision, GATE_DECISIONS):
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
        if not _one_of(channel, GATE_CHANNELS):
            raise GateError(
                f"channel must be one of {sorted(GATE_CHANNELS)}, got {channel!r}"
            )
        if info.locked and is_auto:
            raise GateError(f"gate '{gate}' is locked: only a human can decide it")
        if decision == "changes" and not comment.strip():
            raise GateError(
                "a 'changes' decision needs a comment for the agent's next run"
            )
        if version is not None and not isinstance(version, str):
            raise GateError(f"version must be a string, got {version!r}")
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
        if info.name == "escalation":
            self._check_escalation(events, snapshot, item)
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
        if not isinstance(item, str) or item not in snapshot.tasks:
            raise GateError(
                f"gate '{info.name}' decides a task; {item!r} is not a task of {self.requirement}"
            )
        return item

    def _check_escalation(
        self, events: Sequence[Event], snapshot: Snapshot, task: str
    ) -> None:
        """The escalation gate takes a decision only for a task that reached it, and only one per stay (D30).

        T08's fold ignores escalation decisions, so the gate's own rules are checked here, for auto and humans.
        """
        if not _reached(events, snapshot, GATES["escalation"], task):
            state = snapshot.tasks[task].state.value
            raise GateError(
                f"{task} is {state}: only a failed, rejected or escalating task reaches the escalation gate"
            )
        taken = _escalation_decision(events, task)
        if taken is not None:
            raise GateError(
                f"{task}'s escalation gate is already decided ({taken.data['decision']} by {taken.actor}, "
                f"event {taken.seq}): a gate takes one decision (D30)"
            )

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
        if checks_passed is not True:
            raise GateError(
                f"auto cannot pass '{info.name}': the stage's own checks have not passed"
            )
        mode = settings_from(events).mode(info.name, task)
        if mode != "auto":
            raise GateError(
                f"gate '{info.name}' is {mode} for this item; auto cannot decide it"
            )
        if task is None or info.name == "escalation":
            return  # the escalation gate exists for failing tasks; `decide` checked the task reached it
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


def _escalation_decision(events: Sequence[Event], task: str) -> Event | None:
    """The escalation decision already taken in `task`'s current stay at the gate, or `None` (D30).

    A stay starts when the task reaches the gate and lasts while it stays in `_ESCALATION_REACHED`. A task that leaves
    (D29: a stop in `call_for_edit` back to an unfinished state) starts a new stay when it comes back, as a new draft
    re-opens the other gates. Only a decision `decide` could have written counts (see `_could_decide`), so one
    recorded before the task reached the gate, or by the engine or an agent, never closes the gate.
    """
    taken: Event | None = None
    for i, event in enumerate(events):
        if event.task != task:
            continue
        candidate = (
            taken is None
            and event.type == GATE_DECISION
            and event.data.get("gate") == "escalation"
        )
        if taken is None and not candidate:
            continue  # nothing to follow until a decision
        status = fold(events[: i + 1]).tasks.get(task)
        if status is None or status.state not in _ESCALATION_REACHED:
            taken = None  # not at the gate: not yet, or it left (its next arrival is a new stay)
        elif candidate and _could_decide(events[:i], event, task):
            taken = event
    return taken


def _could_decide(prior: Sequence[Event], event: Event, task: str) -> bool:
    """Whether `decide` could have written `event`, an escalation decision on `task` after the events in `prior`.

    The decision, channel and comment are strings of the kinds `decide` writes and there is no version. It is by a
    human identity on `form` or `terminal`, or by `auto` approving while the gate was `auto` for the task then.
    (`checks_passed` is not recorded, so it can't be checked.)
    """
    data = event.data
    decision, channel = data.get("decision"), data.get("channel")
    comments = data.get("comments")
    if not (
        _one_of(decision, GATE_DECISIONS)
        and _one_of(channel, GATE_CHANNELS)
        and isinstance(comments, str)
        and data.get("version") is None
    ):
        return False
    if decision == "changes" and not comments.strip():
        return False
    if event.actor != "auto":
        return _is_human(event.actor) and channel != "auto"
    if channel != "auto" or decision != "approve":
        return False
    try:
        return settings_from(prior).mode("escalation", task) == "auto"
    except GateError:  # no snapshot before it
        return False

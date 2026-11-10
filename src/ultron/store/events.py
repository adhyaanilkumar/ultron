"""The event model for a requirement's append-only `events.jsonl` (spec §10, FR16, FR17).

Each line is one JSON object, UTF-8, compact, keys in a fixed order, ending in a single `\\n`. Two versions are
stored on every line: `ultron_version` (the package that wrote it, kept for audit, spec §5.3 "Updating") and
`schema` (an integer). Readers only ever compare `schema`; `ultron_version` is never compared, so a log written by
a newer patch release whose schema is unchanged reads without trouble.

Unknown top-level fields are kept in `Event.extra` and written back unchanged. Known renames from older schemas
are applied through `UPGRADES`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
"""The newest event schema this ultron reads and writes."""

KNOWN_FIELDS: tuple[str, ...] = (
    "schema",
    "seq",
    "time",
    "actor",
    "type",
    "requirement",
    "task",
    "attempt",
    "data",
    "ultron_version",
)
"""Top-level keys, in the order they are written. Anything else goes to `Event.extra`."""

UPGRADES: dict[int, dict[str, str]] = {}
"""Renames that take a line from schema `n` to `n + 1`: `{n: {old_key: new_key}}`.

A key is a top-level field name, or `data.<name>` for a field inside `data`. Empty today: schema 1 is the first.
"""

# Event types (spec §10). `type` stays an open string so later tasks can add their own.
INTAKE = "intake"
GATE_DECISION = "gate_decision"
ATTEMPT_STARTED = "attempt_started"
ATTEMPT_FINISHED = "attempt_finished"
ESCALATION = "escalation"
AGENT_CALL = "agent_call"
CONFIG_SNAPSHOT = "config_snapshot"

GATE_DECISIONS = frozenset({"approve", "changes", "reject"})
GATE_CHANNELS = frozenset({"form", "terminal", "auto"})
GATE_MODES = frozenset({"human", "auto"})

_ROLE_RE = re.compile(r"[a-z][a-z0-9_-]*")


class EventError(ValueError):
    """An event or event line is not valid."""


class InvalidEventError(EventError):
    """A field is missing or has the wrong type or value."""


class NewerSchemaError(EventError):
    """The line was written with a newer event schema than this ultron understands."""

    def __init__(self, schema: int) -> None:
        super().__init__(
            f"event schema {schema} is newer than this ultron reads ({SCHEMA_VERSION}); update ultron"
        )
        self.schema = schema


# --- actors ---------------------------------------------------------------------------------------------------


def agent_actor(role: str) -> str:
    """The actor string for an ultron agent role, e.g. `agent:developer`."""
    actor = f"agent:{role}"
    validate_actor(actor)
    return actor


def user_actor(name: str, email: str) -> str:
    """The actor string for a git user: `user:<name> <email>`."""
    actor = f"user:{name.strip()} <{email.strip()}>"
    validate_actor(actor)
    return actor


def validate_actor(actor: object) -> str:
    """Check an actor is `auto`, `engine`, `agent:<role>` or `user:<git name> <email>` and return it."""
    if not isinstance(actor, str):
        raise InvalidEventError(f"actor must be a string, got {type(actor).__name__}")
    if actor in ("auto", "engine"):
        return actor
    if actor.startswith("agent:") and _ROLE_RE.fullmatch(actor.removeprefix("agent:")):
        return actor
    if (
        actor.startswith("user:")
        and actor.removeprefix("user:").strip()
        and "\n" not in actor
    ):
        return actor
    raise InvalidEventError(
        f"actor {actor!r} is not auto, engine, agent:<role> or user:<git name> <email>"
    )


# --- the model ------------------------------------------------------------------------------------------------


def _check_common(
    actor: object, type_: object, requirement: object, task: object, attempt: object
) -> None:
    validate_actor(actor)
    if not isinstance(type_, str) or not type_:
        raise InvalidEventError("type must be a non-empty string")
    if not isinstance(requirement, str) or not requirement:
        raise InvalidEventError("requirement must be a non-empty string")
    if task is not None and (not isinstance(task, str) or not task):
        raise InvalidEventError("task must be a non-empty string or null")
    if attempt is not None and (
        isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
    ):
        raise InvalidEventError("attempt must be a positive integer or null")


@dataclass(frozen=True)
class NewEvent:
    """An event before the log stamps it with `seq`, `time`, `schema` and `ultron_version`."""

    actor: str
    type: str
    requirement: str
    task: str | None = None
    attempt: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_common(self.actor, self.type, self.requirement, self.task, self.attempt)
        if not isinstance(self.data, Mapping):
            raise InvalidEventError("data must be an object")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True)
class Event:
    """One line of `events.jsonl`."""

    schema: int
    seq: int
    time: str
    actor: str
    type: str
    requirement: str
    task: str | None
    attempt: int | None
    data: dict[str, Any]
    ultron_version: str
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_common(self.actor, self.type, self.requirement, self.task, self.attempt)
        if (
            isinstance(self.schema, bool)
            or not isinstance(self.schema, int)
            or self.schema < 1
        ):
            raise InvalidEventError("schema must be a positive integer")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise InvalidEventError("seq must be a positive integer")
        if not isinstance(self.time, str) or not self.time:
            raise InvalidEventError("time must be a non-empty string")
        if not isinstance(self.data, dict):
            raise InvalidEventError("data must be an object")
        if not isinstance(self.ultron_version, str) or not self.ultron_version:
            raise InvalidEventError("ultron_version must be a non-empty string")
        clash = set(self.extra) & set(KNOWN_FIELDS)
        if clash:
            raise InvalidEventError(f"extra repeats known fields: {sorted(clash)}")

    def to_dict(self) -> dict[str, Any]:
        """The JSON object for this event: known fields in order, then the unknown ones as read."""
        out: dict[str, Any] = {
            "schema": self.schema,
            "seq": self.seq,
            "time": self.time,
            "actor": self.actor,
            "type": self.type,
            "requirement": self.requirement,
            "task": self.task,
            "attempt": self.attempt,
            "data": self.data,
            "ultron_version": self.ultron_version,
        }
        out.update(self.extra)
        return out


def to_json_line(event: Event) -> bytes:
    """Serialise an event to one UTF-8 line ending in `\\n` (never `\\r\\n`)."""
    try:
        text = json.dumps(
            event.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(f"event is not JSON-serialisable: {exc}") from exc
    return text.encode("utf-8") + b"\n"


def _rename(obj: dict[str, Any], old: str, new: str) -> None:
    if old.startswith("data.") or new.startswith("data."):
        data = obj.get("data")
        if not isinstance(data, dict):
            return
        old_key, new_key = old.removeprefix("data."), new.removeprefix("data.")
        if old_key in data and new_key not in data:
            data[new_key] = data.pop(old_key)
        return
    if old in obj and new not in obj:
        obj[new] = obj.pop(old)


def upgrade(obj: dict[str, Any]) -> dict[str, Any]:
    """Bring a parsed line up to `SCHEMA_VERSION` by applying `UPGRADES` in order. Mutates and returns `obj`.

    A line with no `schema` key is schema 1, the first. A newer schema raises `NewerSchemaError`.
    """
    schema = obj.get("schema", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise InvalidEventError("schema must be a positive integer")
    if schema > SCHEMA_VERSION:
        raise NewerSchemaError(schema)
    while schema < SCHEMA_VERSION:
        for old, new in UPGRADES.get(schema, {}).items():
            _rename(obj, old, new)
        schema += 1
    obj["schema"] = schema
    return obj


def from_dict(obj: Mapping[str, Any]) -> Event:
    """Build an `Event` from a parsed line, upgrading older schemas and keeping unknown fields in `extra`."""
    if not isinstance(obj, Mapping):
        raise InvalidEventError("an event line must be a JSON object")
    raw = upgrade(dict(obj))
    for key in ("seq", "time", "actor", "type", "requirement", "ultron_version"):
        if key not in raw:
            raise InvalidEventError(f"missing field {key!r}")
    data = raw.get("data", {})
    if not isinstance(data, dict):
        raise InvalidEventError("data must be an object")
    return Event(
        schema=raw["schema"],
        seq=raw["seq"],
        time=raw["time"],
        actor=raw["actor"],
        type=raw["type"],
        requirement=raw["requirement"],
        task=raw.get("task"),
        attempt=raw.get("attempt"),
        data=data,
        ultron_version=raw["ultron_version"],
        extra={k: v for k, v in raw.items() if k not in KNOWN_FIELDS},
    )


def from_json(line: str | bytes) -> Event:
    """Parse one line (with or without its trailing `\\n`)."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidEventError(f"line is not UTF-8: {exc}") from exc
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InvalidEventError(f"line is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise InvalidEventError("an event line must be a JSON object")
    return from_dict(obj)


# --- typed constructors (AC5) ---------------------------------------------------------------------------------


def _non_empty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidEventError(f"{name} must be a non-empty string")
    return value


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidEventError(f"{name} must be a non-negative integer")
    return value


def intake(
    requirement: str,
    *,
    actor: str,
    source: str,
    request_path: str,
    title: str | None = None,
) -> NewEvent:
    """A requirement was submitted (`source`: e.g. `file` or `text`), its request saved at `request_path`."""
    return NewEvent(
        actor=actor,
        type=INTAKE,
        requirement=requirement,
        data={
            "source": _non_empty("source", source),
            "request_path": _non_empty("request_path", request_path),
            "title": title,
        },
    )


def gate_decision(
    requirement: str,
    *,
    actor: str,
    gate: str,
    decision: str,
    channel: str,
    comments: str = "",
    task: str | None = None,
    attempt: int | None = None,
    version: str | None = None,
) -> NewEvent:
    """A gate decision (FR16): who (`actor`), the gate, the decision, the channel and the comments.

    `when` is the event's `time`. An `auto` decision has actor `auto` and channel `auto`.
    """
    if decision not in GATE_DECISIONS:
        raise InvalidEventError(
            f"decision must be one of {sorted(GATE_DECISIONS)}, got {decision!r}"
        )
    if channel not in GATE_CHANNELS:
        raise InvalidEventError(
            f"channel must be one of {sorted(GATE_CHANNELS)}, got {channel!r}"
        )
    if (actor == "auto") != (channel == "auto"):
        raise InvalidEventError(
            "an auto decision has actor 'auto' and channel 'auto', and only then"
        )
    if not isinstance(comments, str):
        raise InvalidEventError("comments must be a string")
    return NewEvent(
        actor=actor,
        type=GATE_DECISION,
        requirement=requirement,
        task=task,
        attempt=attempt,
        data={
            "gate": _non_empty("gate", gate),
            "decision": decision,
            "channel": channel,
            "comments": comments,
            "version": version,
        },
    )


def attempt_started(
    requirement: str,
    task: str,
    attempt: int,
    *,
    rung: str | None = None,
    actor: str = "engine",
) -> NewEvent:
    """Attempt `attempt` of `task` started, optionally on a model/effort `rung`."""
    return NewEvent(
        actor=actor,
        type=ATTEMPT_STARTED,
        requirement=requirement,
        task=_non_empty("task", task),
        attempt=attempt,
        data={"rung": rung},
    )


def attempt_finished(
    requirement: str,
    task: str,
    attempt: int,
    *,
    outcome: str,
    report_path: str | None = None,
    counted: bool = True,
    actor: str = "engine",
) -> NewEvent:
    """Attempt `attempt` of `task` ended with `outcome` (e.g. `passed`, `failed`, `stopped`)."""
    return NewEvent(
        actor=actor,
        type=ATTEMPT_FINISHED,
        requirement=requirement,
        task=_non_empty("task", task),
        attempt=attempt,
        data={
            "outcome": _non_empty("outcome", outcome),
            "report_path": report_path,
            "counted": bool(counted),
        },
    )


def escalation(
    requirement: str,
    *,
    reason: str,
    task: str | None = None,
    attempt: int | None = None,
    ticket: str | None = None,
    actor: str = "engine",
) -> NewEvent:
    """A task (or the requirement) was escalated for `reason`; `ticket` is the Jira key once filed."""
    return NewEvent(
        actor=actor,
        type=ESCALATION,
        requirement=requirement,
        task=task,
        attempt=attempt,
        data={"reason": _non_empty("reason", reason), "ticket": ticket},
    )


def agent_call(
    requirement: str,
    *,
    role: str,
    model: str,
    effort: str,
    duration_ms: int,
    tokens: Mapping[str, int],
    cost_usd: float,
    transcript: str,
    prompt_sha256: str,
    result_sha256: str,
    task: str | None = None,
    attempt: int | None = None,
    stop_reason: str | None = None,
) -> NewEvent:
    """The summary of one ultron agent call (FR17). The full transcript stays local at `transcript`."""
    token_counts = {
        _non_empty("token kind", k): _non_negative_int(f"tokens[{k}]", v)
        for k, v in tokens.items()
    }
    if (
        isinstance(cost_usd, bool)
        or not isinstance(cost_usd, (int, float))
        or cost_usd < 0
    ):
        raise InvalidEventError("cost_usd must be a non-negative number")
    return NewEvent(
        actor=agent_actor(role),
        type=AGENT_CALL,
        requirement=requirement,
        task=task,
        attempt=attempt,
        data={
            "role": role,
            "model": _non_empty("model", model),
            "effort": _non_empty("effort", effort),
            "duration_ms": _non_negative_int("duration_ms", duration_ms),
            "tokens": token_counts,
            "cost_usd": float(cost_usd),
            "transcript": _non_empty("transcript", transcript),
            "prompt_sha256": _non_empty("prompt_sha256", prompt_sha256),
            "result_sha256": _non_empty("result_sha256", result_sha256),
            "stop_reason": stop_reason,
        },
    )


def config_snapshot(
    requirement: str,
    *,
    max_attempts: int,
    gates: Mapping[str, str],
    actor: str = "engine",
) -> NewEvent:
    """The settings a requirement runs with: `max_attempts` and each gate's mode (`human` or `auto`)."""
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise InvalidEventError("max_attempts must be a positive integer")
    modes: dict[str, str] = {}
    for gate, mode in gates.items():
        if mode not in GATE_MODES:
            raise InvalidEventError(
                f"gate {gate!r} mode must be one of {sorted(GATE_MODES)}, got {mode!r}"
            )
        modes[_non_empty("gate", gate)] = mode
    return NewEvent(
        actor=actor,
        type=CONFIG_SNAPSHOT,
        requirement=requirement,
        data={"max_attempts": max_attempts, "gates": modes},
    )

"""The event model: actors, the JSON line format, unknown fields, schema checks and the typed constructors (AC5)."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ultron.store import events as ev
from ultron.store.eventlog import EventLog

CONSTRUCTORS: dict[str, Callable[[], ev.NewEvent]] = {
    "intake": lambda: ev.intake(
        "R1",
        actor=ev.user_actor("Ada Lovelace", "ada@example.com"),
        source="text",
        request_path="sdlc/requirements/R1/request.md",
        title="Add a health check",
    ),
    "gate_decision": lambda: ev.gate_decision(
        "R1",
        actor=ev.user_actor("Ada Lovelace", "ada@example.com"),
        gate="plan",
        decision="changes",
        channel="form",
        comments="Split step 3 — and keep ünïcode",
        task="T2",
        attempt=1,
    ),
    "attempt_started": lambda: ev.attempt_started("R1", "T2", 1, rung="sonnet-high"),
    "attempt_finished": lambda: ev.attempt_finished(
        "R1",
        "T2",
        1,
        outcome="failed",
        report_path="sdlc/requirements/R1/tasks/T2/attempt-1.md",
    ),
    "escalation": lambda: ev.escalation(
        "R1", task="T2", attempt=3, reason="max_attempts reached", ticket="AD-400"
    ),
    "agent_call": lambda: ev.agent_call(
        "R1",
        role="developer",
        model="claude-sonnet-5-5",
        effort="high",
        duration_ms=12345,
        tokens={"input": 1000, "output": 250, "cache_read": 4000},
        cost_usd=0.0425,
        transcript=".ultron/transcripts/R1/T2/1/developer.jsonl",
        prompt_sha256="a" * 64,
        result_sha256="b" * 64,
        task="T2",
        attempt=1,
        stop_reason="end_turn",
    ),
    "config_snapshot": lambda: ev.config_snapshot(
        "R1",
        max_attempts=3,
        gates={"intent": "human", "spec": "human", "plan": "auto", "merge": "human"},
    ),
}


@pytest.mark.parametrize("name", sorted(CONSTRUCTORS))
def test_constructor_round_trips(name: str, tmp_path: Path) -> None:
    new = CONSTRUCTORS[name]()
    assert new.type == name
    log = EventLog(tmp_path / "events.jsonl")
    written = log.append(new)
    result = log.read()
    assert result.truncated_tail is None
    assert result.events == written
    (event,) = result.events
    assert (
        event.actor,
        event.type,
        event.requirement,
        event.task,
        event.attempt,
        event.data,
    ) == (
        new.actor,
        new.type,
        new.requirement,
        new.task,
        new.attempt,
        new.data,
    )


def test_all_spec_event_types_have_constructors() -> None:
    assert set(CONSTRUCTORS) == {
        ev.INTAKE,
        ev.GATE_DECISION,
        ev.ATTEMPT_STARTED,
        ev.ATTEMPT_FINISHED,
        ev.ESCALATION,
        ev.AGENT_CALL,
        ev.CONFIG_SNAPSHOT,
    }


def test_gate_decision_records_fr16_fields() -> None:
    data = CONSTRUCTORS["gate_decision"]().data
    assert {"gate", "decision", "channel", "comments"} <= set(data)


def test_agent_call_records_fr17_summary() -> None:
    new = CONSTRUCTORS["agent_call"]()
    assert new.actor == "agent:developer"
    for key in ("role", "model", "duration_ms", "tokens", "cost_usd", "transcript"):
        assert key in new.data


@pytest.mark.parametrize(
    "actor",
    ["auto", "engine", "agent:developer", "agent:spec_agent", "user:Ada <a@b.c>"],
)
def test_valid_actors(actor: str) -> None:
    assert ev.validate_actor(actor) == actor


@pytest.mark.parametrize(
    "actor", ["", "Auto", "agent:", "agent:Dev", "user:", "user:  ", "ada", 7]
)
def test_invalid_actors(actor: object) -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.validate_actor(actor)


def test_git_user_called_engine_is_not_the_engine() -> None:
    assert ev.user_actor("engine", "e@x.y") == "user:engine <e@x.y>"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"decision": "maybe"}, "decision"),
        ({"channel": "email"}, "channel"),
        ({"actor": "auto", "channel": "form"}, "auto"),
        ({"channel": "auto"}, "auto"),
    ],
)
def test_gate_decision_rejects_bad_values(kwargs: dict[str, str], message: str) -> None:
    args = {
        "actor": "user:Ada <a@b.c>",
        "gate": "plan",
        "decision": "approve",
        "channel": "form",
    }
    args.update(kwargs)
    with pytest.raises(ev.InvalidEventError, match=message):
        ev.gate_decision("R1", **args)


def test_config_snapshot_rejects_unknown_mode() -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.config_snapshot("R1", max_attempts=3, gates={"plan": "sometimes"})
    with pytest.raises(ev.InvalidEventError):
        ev.config_snapshot("R1", max_attempts=0, gates={})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requirement": ""},
        {"type": ""},
        {"task": ""},
        {"attempt": 0},
        {"attempt": True},
        {"actor": "nobody"},
    ],
)
def test_new_event_validates(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"actor": "engine", "type": "x", "requirement": "R1"}
    args.update(kwargs)
    with pytest.raises(ev.InvalidEventError):
        ev.NewEvent(**args)  # type: ignore[arg-type]


def _event(**overrides: object) -> ev.Event:
    args: dict[str, object] = {
        "schema": 1,
        "seq": 1,
        "time": "2026-11-10T09:00:00.000Z",
        "actor": "engine",
        "type": "x",
        "requirement": "R1",
        "task": None,
        "attempt": None,
        "data": {"k": "v"},
        "ultron_version": "0.0.0",
    }
    args.update(overrides)
    return ev.Event(**args)  # type: ignore[arg-type]


def test_json_line_format() -> None:
    line = ev.to_json_line(_event(data={"text": "naïve\r\nline"}))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert b"\r" not in line
    assert b": " not in line and b", " not in line  # compact separators
    obj = json.loads(line)
    assert list(obj) == list(ev.KNOWN_FIELDS)


def test_unknown_fields_are_kept_and_written_back() -> None:
    line = (
        b'{"schema":1,"seq":4,"time":"t","actor":"engine","type":"x","requirement":"R1",'
        b'"task":null,"attempt":null,"data":{"new":1},"ultron_version":"9.9.9",'
        b'"host":"codex","nested":{"a":[1,2]}}\n'
    )
    event = ev.from_json(line)
    assert event.extra == {"host": "codex", "nested": {"a": [1, 2]}}
    assert event.data == {"new": 1}
    assert ev.to_json_line(event) == line


def test_missing_optional_fields_default() -> None:
    event = ev.from_json(
        '{"seq":1,"time":"t","actor":"engine","type":"x","requirement":"R1","ultron_version":"0.0.0"}'
    )
    assert event.schema == 1
    assert (event.task, event.attempt, event.data) == (None, None, {})


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        "not json",
        '{"seq":1}',
        '{"schema":1,"seq":0,"time":"t","actor":"engine","type":"x","requirement":"R1","ultron_version":"v"}',
        '{"schema":1,"seq":1,"time":"t","actor":"engine","type":"x","requirement":"R1"}',
        '{"schema":"1","seq":1,"time":"t","actor":"engine","type":"x","requirement":"R1","ultron_version":"v"}',
    ],
)
def test_invalid_lines(line: str) -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.from_json(line)


def test_newer_schema_is_refused() -> None:
    line = (
        '{"schema":2,"seq":1,"time":"t","actor":"engine","type":"x",'
        '"requirement":"R1","ultron_version":"0.0.0"}'
    )
    with pytest.raises(ev.NewerSchemaError) as exc:
        ev.from_json(line)
    assert exc.value.schema == 2


def test_non_json_data_is_refused() -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.to_json_line(_event(data={"x": object()}))
    with pytest.raises(ev.InvalidEventError):
        ev.to_json_line(_event(data={"x": float("nan")}))


def _deep(depth: int) -> dict[str, object]:
    root: dict[str, object] = {}
    node = root
    for _ in range(depth):
        child: dict[str, object] = {}
        node["a"] = child
        node = child
    return root


@pytest.mark.parametrize(
    "overrides",
    [
        {"data": {"path": "C:\\tmp\\\udcff.txt"}},  # lone surrogate in a value
        {"data": {"\ud800": 1}},  # ... in a key
        {"type": "probe\udcff"},  # ... in a top-level field
        {"actor": "user:Ann\udcff <a@b.c>"},
        {"extra": {"host": "\udcff"}},
        {"data": _deep(100_000)},  # nesting too deep: RecursionError
        {"data": {"n": 10**5000}},  # integer too long for str()
    ],
    ids=["value", "key", "type", "actor", "extra", "deep", "big-int"],
)
def test_unserialisable_event_raises_invalid_event_error(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.to_json_line(_event(**overrides))


@pytest.mark.parametrize(
    "line",
    [
        "[" * 100_000 + "]" * 100_000,  # nesting too deep: RecursionError
        '{"seq":' + "1" * 5000 + "}",  # integer too long: plain ValueError
    ],
    ids=["deep", "big-int"],
)
def test_unparseable_line_raises_invalid_event_error(line: str) -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.from_json(line)


def test_upgrade_renames_top_level_and_data_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ev, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(
        ev,
        "UPGRADES",
        {1: {"req": "requirement"}, 2: {"data.verdict": "data.decision"}},
    )
    event = ev.from_json(
        '{"schema":1,"seq":1,"time":"t","actor":"auto","type":"gate_decision",'
        '"req":"R1","data":{"verdict":"approve"},"ultron_version":"0.0.0"}'
    )
    assert event.schema == 3
    assert event.requirement == "R1"
    assert event.data == {"decision": "approve"}
    assert event.extra == {}

"""The append-only writer and the versioned reader (AC1, AC3, AC4)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ultron import __version__
from ultron.store import events as ev
from ultron.store.eventlog import (
    CorruptLogError,
    EventLog,
    NewerSchemaError,
    TruncatedLogError,
    events_path,
    format_time,
)

FIXTURE = Path(__file__).parent / "fixtures" / "events_v0_0_1.jsonl"


def _new(i: int = 0) -> ev.NewEvent:
    return ev.NewEvent(actor="engine", type="probe", requirement="R1", data={"i": i})


def _good_line(seq: int) -> bytes:
    obj = {
        "schema": 1,
        "seq": seq,
        "time": "2026-11-10T09:00:00.000Z",
        "actor": "engine",
        "type": "probe",
        "requirement": "R1",
        "task": None,
        "attempt": None,
        "data": {},
        "ultron_version": "0.0.0",
    }
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


# --- AC1 ------------------------------------------------------------------------------------------------------


def test_seq_gap_free(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    seqs: list[int] = []
    for size in (1, 5, 1, 10, 8):
        seqs += [e.seq for e in log.append([_new(i) for i in range(size)])]
    assert seqs == list(range(1, 26))
    assert [e.seq for e in log.read().events] == list(range(1, 26))
    assert [e.data["i"] for e in log.read().events[1:6]] == [0, 1, 2, 3, 4]


def test_prefix_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append([_new(i) for i in range(5)])
    before = path.read_bytes()
    log.append([_new(i) for i in range(5)])
    log.append(_new())
    after = path.read_bytes()
    assert len(after) > len(before)
    assert (
        hashlib.sha256(after[: len(before)]).digest() == hashlib.sha256(before).digest()
    )


def test_file_has_lf_only(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    EventLog(path).append([_new(i) for i in range(3)])
    data = path.read_bytes()
    assert b"\r" not in data
    assert data.count(b"\n") == 3


def test_append_empty_batch_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    assert EventLog(path).append([]) == []
    assert not path.exists()


def test_invalid_event_in_batch_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(_new())
    before = path.read_bytes()
    bad = ev.NewEvent(
        actor="engine", type="probe", requirement="R1", data={"x": {1, 2}}
    )
    with pytest.raises(ev.InvalidEventError):
        log.append([_new(), bad])
    assert path.read_bytes() == before
    assert log.append(_new())[0].seq == 2


def test_append_rejects_non_events(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        EventLog(tmp_path / "events.jsonl").append([{"type": "x"}])  # type: ignore[list-item]


def test_long_last_line_is_found(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(_new())
    big = ev.NewEvent(
        actor="engine", type="probe", requirement="R1", data={"blob": "x" * 200_000}
    )
    assert log.append(big)[0].seq == 2
    assert log.append(_new())[0].seq == 3


def test_missing_log_reads_empty(tmp_path: Path) -> None:
    result = EventLog(tmp_path / "nope" / "events.jsonl").read()
    assert result.events == []
    assert result.truncated_tail is None
    assert not result.truncated


def test_append_creates_parent_dirs_and_default_lock(tmp_path: Path) -> None:
    path = events_path(tmp_path, "R-20261109-a3f2")
    assert (
        path == tmp_path / "sdlc" / "requirements" / "R-20261109-a3f2" / "events.jsonl"
    )
    log = EventLog(path)
    assert log.lock_path == path.with_name("events.jsonl.lock")
    log.append(_new())
    assert path.is_file()


def test_custom_lock_path(tmp_path: Path) -> None:
    lock = tmp_path / ".ultron" / "locks" / "R1.events.lock"
    log = EventLog(tmp_path / "events.jsonl", lock)
    log.append(_new())
    assert lock.exists()
    assert not (tmp_path / "events.jsonl.lock").exists()


@pytest.mark.parametrize("bad", ["", "..", "../R1", "R1/x", "R1\\x", ".hidden"])
def test_events_path_rejects_bad_ids(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        events_path(tmp_path, bad)


def test_time_is_utc_millis_z(tmp_path: Path) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 11, 10, 15, 0, 0, 123456, tzinfo=ist)
    log = EventLog(tmp_path / "events.jsonl", clock=lambda: moment)
    (event,) = log.append(_new())
    assert event.time == "2026-11-10T09:30:00.123Z"
    with pytest.raises(ValueError):
        format_time(datetime(2026, 1, 1))
    assert format_time(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000Z"


# --- AC3 ------------------------------------------------------------------------------------------------------


def test_every_event_records_version(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append([_new(i) for i in range(3)])
    for raw in (tmp_path / "events.jsonl").read_bytes().splitlines():
        obj = json.loads(raw)
        assert obj["ultron_version"] == __version__
        assert obj["schema"] == ev.SCHEMA_VERSION


def test_reads_old_version_fixture(tmp_path: Path) -> None:
    result = EventLog(FIXTURE).read(strict=True)
    assert [e.seq for e in result.events] == [1, 2, 3]
    assert {e.ultron_version for e in result.events} == {"0.0.1"}
    first, second, third = result.events
    assert first.extra == {"host": "claude-code"}
    assert second.data["future_flag"] is True
    assert third.extra == {"host": "claude-code", "trace": {"id": "abc"}}
    # Re-serialising keeps every key and value of the original line.
    for event, raw in zip(
        result.events, FIXTURE.read_bytes().splitlines(), strict=True
    ):
        assert json.loads(ev.to_json_line(event)) == json.loads(raw)


def test_appending_after_old_version_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(FIXTURE.read_bytes().replace(b"\r\n", b"\n"))
    (event,) = EventLog(path).append(_new())
    assert event.seq == 4
    assert event.ultron_version == __version__
    versions = [e.ultron_version for e in EventLog(path).read().events]
    assert versions == ["0.0.1", "0.0.1", "0.0.1", __version__]


def test_rename_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"schema":1,"seq":1,"time":"t","actor":"engine","type":"x","req":"R1",'
        b'"data":{},"ultron_version":"0.0.0"}\n'
    )
    monkeypatch.setattr(ev, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(ev, "UPGRADES", {1: {"req": "requirement"}})
    (event,) = EventLog(path).read().events
    assert event.requirement == "R1"
    assert event.schema == 2


def test_newer_schema_on_read(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        _good_line(1) + _good_line(2).replace(b'"schema":1', b'"schema":99')
    )
    with pytest.raises(NewerSchemaError):
        EventLog(path).read()
    with pytest.raises(NewerSchemaError):
        EventLog(path).append(_new())


# --- AC4 ------------------------------------------------------------------------------------------------------


def test_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append([_new(i) for i in range(3)])
    good = path.read_bytes()
    partial = _good_line(4)[:37]
    with path.open("ab") as f:
        f.write(partial)

    result = log.read()
    assert result.truncated
    assert result.truncated_tail == partial
    assert [e.seq for e in result.events] == [1, 2, 3]
    with pytest.raises(TruncatedLogError):
        log.read(strict=True)
    with pytest.raises(TruncatedLogError):
        log.append(_new())
    assert path.read_bytes() == good + partial  # the refused append wrote nothing

    assert log.repair() == partial
    assert path.read_bytes() == good
    assert log.repair() == b""
    assert log.append(_new())[0].seq == 4
    assert [e.seq for e in log.read(strict=True).events] == [1, 2, 3, 4]


def test_truncated_only_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"schema":1,"se')
    log = EventLog(path)
    assert log.read().events == []
    assert log.repair() == b'{"schema":1,"se'
    assert path.read_bytes() == b""
    assert log.append(_new())[0].seq == 1


def test_repair_missing_or_empty(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    assert EventLog(path).repair() == b""
    path.write_bytes(b"")
    assert EventLog(path).repair() == b""
    assert EventLog(path).append(_new())[0].seq == 1


def test_corrupt_middle_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_good_line(1) + b"{oops}\n" + _good_line(3))
    with pytest.raises(CorruptLogError) as exc:
        EventLog(path).read()
    assert exc.value.line_no == 2


def test_blank_line_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_good_line(1) + b"\n" + _good_line(2))
    with pytest.raises(CorruptLogError) as exc:
        EventLog(path).read()
    assert exc.value.line_no == 2


@pytest.mark.parametrize("seqs", [[1, 3], [2], [1, 1], [1, 2, 2]])
def test_seq_out_of_order_is_corrupt(seqs: list[int], tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"".join(_good_line(s) for s in seqs))
    with pytest.raises(CorruptLogError, match="seq"):
        EventLog(path).read()


def test_corrupt_last_line_blocks_append(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_good_line(1) + b"not json\n")
    with pytest.raises(CorruptLogError):
        EventLog(path).append(_new())
    assert path.read_bytes() == _good_line(1) + b"not json\n"

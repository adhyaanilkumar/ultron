"""Acceptance tests for T05 (event log), written by the testing agent from the ACs and the diff only.

Focus: what could lose or corrupt the audit log. Real concurrent writer processes with a reader running at the
same time, crashes at many byte offsets, append-only guarantees, CRLF, very large events, and older logs.
"""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ultron import __version__
from ultron.store import events as ev
from ultron.store.eventlog import (
    CorruptLogError,
    EventLog,
    LockTimeout,
    NewerSchemaError,
    TruncatedLogError,
)

DEADLINE_S = 45


def _new(i: int = 0, **data: Any) -> ev.NewEvent:
    return ev.NewEvent(
        actor="engine", type="probe", requirement="R1", data={"i": i, **data}
    )


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "sdlc" / "requirements" / "R1" / "events.jsonl")


def _line(seq: int, **over: Any) -> bytes:
    obj: dict[str, Any] = {
        "schema": 1,
        "seq": seq,
        "time": "2026-11-10T09:00:00.000Z",
        "actor": "engine",
        "type": "probe",
        "requirement": "R1",
        "task": None,
        "attempt": None,
        "data": {"n": seq},
        "ultron_version": "0.0.1",
    }
    obj.update(over)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def _seqs(log: EventLog) -> list[int]:
    return [e.seq for e in log.read(strict=True).events]


def _spawn(code: str, *args: str) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait(cond: Any, deadline: float, what: str) -> None:
    while not cond():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


# --- AC1: gap-free seq, append-only -------------------------------------------------------------------------


def test_ac1_seq_gap_free_across_instances_and_batches(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    seqs: list[int] = []
    for k, size in enumerate((1, 3, 1, 7, 2, 1)):
        # a fresh EventLog object each time: seq must come from the file, not from memory
        seqs += [e.seq for e in EventLog(path).append([_new(k)] * size)]
    assert seqs == list(range(1, 16))
    assert _seqs(EventLog(path)) == list(range(1, 16))


def test_ac1_earlier_bytes_never_change(tmp_path: Path) -> None:
    log = _log(tmp_path)
    snapshots: list[bytes] = []
    for i in range(30):
        log.append([_new(i)] * (1 + i % 3))
        snapshots.append(log.path.read_bytes())
    final = log.path.read_bytes()
    for snap in snapshots:
        assert final.startswith(snap)
    log.read()  # reading never writes
    assert log.path.read_bytes() == final


def test_ac1_failed_batch_writes_nothing_and_seq_continues(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append([_new(1), _new(2)])
    before = log.path.read_bytes()
    with pytest.raises(ev.EventError):
        log.append([_new(3), _new(4, bad=float("nan"))])
    with pytest.raises(ev.EventError):
        log.append([_new(3), _new(4, bad=object())])
    assert log.path.read_bytes() == before
    assert [e.seq for e in log.append(_new(5))] == [3]


def test_ac1_unencodable_text_writes_nothing_and_raises_event_error(
    tmp_path: Path,
) -> None:
    """A lone surrogate (e.g. from a surrogate-escaped Windows path) must be refused as an invalid event."""
    log = _log(tmp_path)
    log.append(_new(1))
    before = log.path.read_bytes()
    with pytest.raises(ev.EventError):
        log.append(_new(2, path="C:\\tmp\\\udcff.txt"))
    assert log.path.read_bytes() == before


def test_ac1_writes_lf_only_and_one_line_per_event(tmp_path: Path) -> None:
    log = _log(tmp_path)
    tricky = 'line1\nline2\r\nsep\u2028para\u2029emoji \U0001f600 nul\x00 quote" back\\'
    log.append([_new(1, text=tricky), _new(2, text=tricky)])
    raw = log.path.read_bytes()
    assert b"\r" not in raw
    assert raw.count(b"\n") == 2
    assert [e.data["text"] for e in log.read(strict=True).events] == [tricky, tricky]


# --- AC2: concurrent writers across processes, with a reader running -----------------------------------------

WRITER = r"""
import sys, time
from pathlib import Path
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

path, ready, go, w, n, big = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], int(sys.argv[5]), int(sys.argv[6])
log = EventLog(path)
ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
i = 0
while i < n:
    pad = "x" * big if big and i % 5 == 0 else ""
    if i % 7 == 3 and i + 3 <= n:
        log.append([NewEvent(actor="engine", type="probe", requirement="R1",
                             data={"w": w, "i": i + k, "pad": pad}) for k in range(3)])
        i += 3
    else:
        log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"w": w, "i": i, "pad": pad}))
        i += 1
"""

READER = r"""
import json, sys, time
from pathlib import Path
from ultron.store.eventlog import EventLog, TruncatedLogError

path, ready, stop = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
log = EventLog(path)
stats = {"reads": 0, "truncated_seen": 0, "errors": [], "shrank": 0, "prefix_changed": 0, "max": 0}
prev = []
ready.touch()
end = time.monotonic() + 50
while not stop.exists() and time.monotonic() < end:
    try:
        r = log.read()
    except Exception as exc:  # any CorruptLogError here means a reader saw a corrupt log mid-write
        stats["errors"].append(f"{type(exc).__name__}: {exc}"[:300])
        continue
    stats["reads"] += 1
    if r.truncated:
        stats["truncated_seen"] += 1
    cur = [(e.seq, e.data.get("w"), e.data.get("i")) for e in r.events]
    if len(cur) < len(prev):
        stats["shrank"] += 1
    elif cur[: len(prev)] != prev:
        stats["prefix_changed"] += 1
    prev = cur
    stats["max"] = max(stats["max"], len(cur))
print(json.dumps(stats))
"""


def _run_writers_with_reader(
    tmp_path: Path, workers: int, per_worker: int, big: int
) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / "sdlc" / "requirements" / "R1" / "events.jsonl"
    go, stop = tmp_path / "go", tmp_path / "stop"
    ready = [tmp_path / f"ready-{w}" for w in range(workers)]
    reader_ready = tmp_path / "reader-ready"
    deadline = time.monotonic() + DEADLINE_S
    reader = _spawn(READER, str(path), str(reader_ready), str(stop))
    procs = [
        _spawn(
            WRITER,
            str(path),
            str(ready[w]),
            str(go),
            str(w),
            str(per_worker),
            str(big),
        )
        for w in range(workers)
    ]
    try:
        _wait(
            lambda: reader_ready.exists() and all(p.exists() for p in ready),
            deadline,
            "processes to start",
        )
        go.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
        time.sleep(0.2)  # let the reader see the final state at least once
        stop.touch()
        out, err = reader.communicate(timeout=max(1.0, deadline - time.monotonic()))
        assert reader.returncode == 0, err.decode(errors="replace")
    finally:
        for proc in [*procs, reader]:
            if proc.poll() is None:
                proc.kill()
    return path, json.loads(out)


def _check_log(path: Path, workers: int, per_worker: int) -> None:
    data = path.read_bytes()
    assert data.endswith(b"\n")
    lines = data.split(b"\n")[:-1]
    assert len(lines) == workers * per_worker
    objs = [json.loads(line) for line in lines]  # every line valid JSON
    assert [o["seq"] for o in objs] == list(range(1, workers * per_worker + 1))
    for w in range(workers):
        mine = [o["data"]["i"] for o in objs if o["data"]["w"] == str(w)]
        assert mine == list(range(per_worker)), f"worker {w} lost or reordered events"
    assert len(EventLog(path).read(strict=True).events) == workers * per_worker


def test_ac2_four_processes_fifty_each_with_reader(tmp_path: Path) -> None:
    path, stats = _run_writers_with_reader(tmp_path, workers=4, per_worker=50, big=0)
    _check_log(path, 4, 50)
    assert stats["errors"] == []
    assert stats["shrank"] == 0 and stats["prefix_changed"] == 0
    assert stats["reads"] > 0 and stats["max"] == 200


def test_ac2_eight_processes_large_events_with_reader(tmp_path: Path) -> None:
    path, stats = _run_writers_with_reader(
        tmp_path, workers=8, per_worker=25, big=300_000
    )
    _check_log(path, 8, 25)
    assert stats["errors"] == [], stats["errors"][:3]
    assert stats["shrank"] == 0 and stats["prefix_changed"] == 0
    assert stats["max"] == 200


def test_ac2_threads_in_one_process(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    errors: list[BaseException] = []

    def work(w: int) -> None:
        log = EventLog(path)
        try:
            for i in range(25):
                log.append(_new(i, w=w))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=DEADLINE_S)
    assert errors == []
    assert _seqs(EventLog(path)) == list(range(1, 201))


HOLDER = r"""
import sys, time
from ultron.store._filelock import FileLock
with FileLock(sys.argv[1]):
    print("locked", flush=True)
    time.sleep(30)
"""


def test_ac2_lock_timeout_writes_nothing(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(_new(1))
    before = log.path.read_bytes()
    holder = _spawn(HOLDER, str(log.lock_path))
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == b"locked"
        slow = EventLog(log.path, lock_timeout=0.3)
        with pytest.raises(LockTimeout):
            slow.append(_new(2))
        with pytest.raises(LockTimeout):
            slow.repair()
        assert log.path.read_bytes() == before
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # the lock dies with its process
    assert [e.seq for e in EventLog(log.path, lock_timeout=10).append(_new(2))] == [2]


CRASHER = r"""
import os, sys
from ultron.store._filelock import FileLock
path, lock, partial = sys.argv[1], sys.argv[2], bytes.fromhex(sys.argv[3])
with FileLock(lock):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
    os.write(fd, partial)
    os._exit(9)  # die holding the lock, mid-line
"""


def test_ac2_ac4_process_dies_mid_write_holding_lock(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append([_new(1), _new(2)])
    good = log.path.read_bytes()
    partial = _line(3)[:40]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASHER,
            str(log.path),
            str(log.lock_path),
            partial.hex(),
        ],
        check=False,
        capture_output=True,
        timeout=DEADLINE_S,
    )
    assert proc.returncode == 9, proc.stderr.decode(errors="replace")
    other = EventLog(log.path, lock_timeout=10)
    with pytest.raises(TruncatedLogError):
        other.append(_new(3))
    assert log.path.read_bytes() == good + partial
    assert other.repair() == partial
    assert log.path.read_bytes() == good
    assert [e.seq for e in other.append(_new(3))] == [3]
    assert log.path.read_bytes().startswith(good)


KILLED_WRITER = r"""
import sys
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent
log = EventLog(sys.argv[1])
i = 0
while True:
    log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"i": i, "pad": "y" * 2_000_000}))
    i += 1
"""


def test_ac2_ac4_writer_killed_mid_stream_recovers(tmp_path: Path) -> None:
    log = _log(tmp_path)
    proc = _spawn(KILLED_WRITER, str(log.path))
    deadline = time.monotonic() + DEADLINE_S
    try:
        _wait(
            lambda: log.path.exists() and log.path.stat().st_size > 5_000_000,
            deadline,
            "the writer to write",
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)
    snapshot = log.path.read_bytes()
    after = EventLog(log.path, lock_timeout=10)
    result = after.read()
    n = len(result.events)
    assert n >= 2
    assert [e.seq for e in result.events] == list(range(1, n + 1))
    if result.truncated:
        with pytest.raises(TruncatedLogError):
            after.append(_new(0))
        assert after.repair() == result.truncated_tail
    assert [e.seq for e in after.append(_new(0))] == [n + 1]
    complete = snapshot[: snapshot.rfind(b"\n") + 1]
    assert log.path.read_bytes().startswith(complete)


# --- AC3: writer version and older logs ---------------------------------------------------------------------

OLD_LOG = (
    b'{"schema":1,"seq":1,"time":"2026-01-01T00:00:00.000Z","actor":"user:Old Timer <old@example.com>",'
    b'"type":"intake","requirement":"R1","task":null,"attempt":null,'
    b'"data":{"source":"file","request_path":"r.md","title":null,"legacy":[1,2]},'
    b'"ultron_version":"0.0.1","origin":{"host":"claude-code","pid":7}}\n'
    # no "schema" key at all, and no task/attempt keys: an early hand-written style line
    b'{"seq":2,"time":"2026-01-01T00:00:01.000Z","actor":"engine","type":"mystery_event",'
    b'"requirement":"R1","data":{"x":1},"ultron_version":"0.0.1","zz_unknown":"keep me"}\n'
)


def test_ac3_every_event_records_writer_version(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append([_new(1), _new(2)])
    for raw in log.path.read_bytes().splitlines():
        assert json.loads(raw)["ultron_version"] == __version__
    v = EventLog(log.path, ultron_version="9.9.9")
    v.append(_new(3))
    assert (
        json.loads(log.path.read_bytes().splitlines()[-1])["ultron_version"] == "9.9.9"
    )


def test_ac3_old_version_log_reads_and_keeps_unknown_fields(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.path.parent.mkdir(parents=True)
    log.path.write_bytes(OLD_LOG)
    events = log.read(strict=True).events
    assert [e.ultron_version for e in events] == ["0.0.1", "0.0.1"]
    assert events[0].extra == {"origin": {"host": "claude-code", "pid": 7}}
    assert events[0].data["legacy"] == [1, 2]
    assert events[1].extra == {"zz_unknown": "keep me"}
    assert events[1].type == "mystery_event"
    assert events[1].task is None and events[1].attempt is None
    # the first (complete, ordered) line round-trips byte for byte
    assert ev.to_json_line(events[0]) == OLD_LOG.splitlines(keepends=True)[0]
    assert log.path.read_bytes() == OLD_LOG  # reading never rewrites


def test_ac3_append_onto_old_log_keeps_old_bytes(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.path.parent.mkdir(parents=True)
    log.path.write_bytes(OLD_LOG)
    written = log.append(_new(3))
    assert [e.seq for e in written] == [3]
    raw = log.path.read_bytes()
    assert raw.startswith(OLD_LOG)
    events = log.read(strict=True).events
    assert [e.ultron_version for e in events] == ["0.0.1", "0.0.1", __version__]
    assert events[1].extra == {"zz_unknown": "keep me"}


def test_ac3_known_rename_is_upgraded_on_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ev, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        ev, "UPGRADES", {1: {"who": "actor", "data.ticket_key": "data.ticket"}}
    )
    raw = (
        b'{"schema":1,"seq":1,"time":"t","who":"engine","type":"escalation","requirement":"R1",'
        b'"data":{"reason":"r","ticket_key":"AD-1"},"ultron_version":"0.0.1"}\n'
    )
    path = tmp_path / "events.jsonl"
    path.write_bytes(raw)
    (event,) = EventLog(path).read(strict=True).events
    assert event.actor == "engine" and event.data == {"reason": "r", "ticket": "AD-1"}
    assert event.schema == 2 and "who" not in event.extra
    assert path.read_bytes() == raw


def test_ac3_newer_schema_is_refused_not_misread(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(1) + _line(2, schema=99))
    log = EventLog(path)
    with pytest.raises(NewerSchemaError):
        log.read()
    before = path.read_bytes()
    with pytest.raises(NewerSchemaError):
        log.append(_new(3))  # never write old-schema lines after a newer one
    assert path.read_bytes() == before


def test_ac3_newer_patch_with_same_schema_reads(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(1, ultron_version="99.0.0") + _line(2))
    assert _seqs(EventLog(path)) == [1, 2]


# --- AC4: crash / truncation at many byte offsets ---------------------------------------------------------


def _three_line_log(tmp_path: Path) -> tuple[EventLog, bytes, bytes]:
    log = _log(tmp_path)
    log.append(_new(1))
    log.append(_new(2, text="Ünïcødé ✓ 日本語 \U0001f600"))
    complete = log.path.read_bytes()
    log.append(_new(3, text="ā日\U0001f600z" * 20))
    last = log.path.read_bytes()[len(complete) :]
    return log, complete, last


def _cut_points(last: bytes) -> list[int]:
    """1, 2, every 37th byte, every byte inside a multi-byte character near the end, and the last two."""
    multi = [i for i in range(len(last) - 60, len(last) - 1) if last[i] >= 0x80][:12]
    return sorted(
        {1, 2, *range(3, len(last) - 1, 37), *multi, len(last) - 2, len(last) - 1}
    )


def test_ac4_truncation_at_every_kind_of_offset(tmp_path: Path) -> None:
    log, complete, last = _three_line_log(tmp_path)
    for cut in _cut_points(last):
        _check_truncation(log, complete, last, cut)


def _check_truncation(log: EventLog, complete: bytes, last: bytes, cut: int) -> None:
    partial = last[:cut]
    assert partial and not partial.endswith(b"\n")
    log.path.write_bytes(complete + partial)

    result = log.read()
    assert result.truncated and result.truncated_tail == partial
    assert [e.seq for e in result.events] == [1, 2]
    with pytest.raises(TruncatedLogError):
        log.read(strict=True)
    with pytest.raises(TruncatedLogError):
        log.append(_new(9))
    assert log.path.read_bytes() == complete + partial  # refused append wrote nothing

    assert log.repair() == partial
    assert log.path.read_bytes() == complete  # earlier lines intact
    assert log.repair() == b""
    assert [e.seq for e in log.append(_new(3))] == [3]
    assert log.path.read_bytes().startswith(complete)


def test_ac4_truncation_inside_a_batch_keeps_whole_lines(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(_new(0))
    base = log.path.read_bytes()
    log.append([_new(1), _new(2), _new(3)])
    batch = log.path.read_bytes()[len(base) :]
    first_nl = batch.index(b"\n") + 1
    log.path.write_bytes(base + batch[: first_nl + 5])
    r = log.read()
    assert [e.seq for e in r.events] == [1, 2] and r.truncated_tail == batch[
        first_nl : first_nl + 5
    ]
    log.repair()
    assert [e.seq for e in log.append(_new(9))] == [3]
    assert _seqs(log) == [1, 2, 3]


def test_ac4_nul_filled_tail_after_power_loss(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append([_new(1), _new(2)])
    good = log.path.read_bytes()
    log.path.write_bytes(good + b"\x00" * 300)
    assert log.read().truncated
    with pytest.raises(TruncatedLogError):
        log.append(_new(3))
    assert log.repair() == b"\x00" * 300
    assert log.path.read_bytes() == good


def test_ac4_only_line_truncated(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(1)[:-5])
    log = EventLog(path)
    r = log.read()
    assert r.events == [] and r.truncated
    log.repair()
    assert path.read_bytes() == b""
    assert [e.seq for e in log.append(_new(1))] == [1]


def test_ac4_large_truncated_tail(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(_new(1))
    good = log.path.read_bytes()
    big = ev.to_json_line(
        ev.Event(
            1,
            2,
            "t",
            "engine",
            "probe",
            "R1",
            None,
            None,
            {"p": "z" * 3_000_000},
            "0.0.1",
        )
    )
    log.path.write_bytes(good + big[:2_500_000])
    assert log.read().truncated_tail == big[:2_500_000]
    with pytest.raises(TruncatedLogError):
        log.append(_new(2))
    assert len(log.repair()) == 2_500_000
    assert log.path.read_bytes() == good


def test_ac4_corrupt_complete_line_is_reported_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(_line(1) + b'{"seq":2,"oops"\n' + _line(3))
    with pytest.raises(CorruptLogError) as info:
        EventLog(path).read()
    assert info.value.line_no == 2


# --- CRLF (e.g. a git checkout with core.autocrlf=true) -----------------------------------------------------


def test_crlf_log_reads_and_appends_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    crlf = (_line(1) + _line(2) + _line(3)).replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    log = EventLog(path)
    assert _seqs(log) == [1, 2, 3]
    assert [e.seq for e in log.append(_new(4))] == [4]
    raw = path.read_bytes()
    assert raw.startswith(crlf) and raw.endswith(b"\n") and not raw.endswith(b"\r\n")
    assert _seqs(log) == [1, 2, 3, 4]


def test_crlf_truncated_after_cr(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    good = (_line(1) + _line(2)).replace(b"\n", b"\r\n")
    path.write_bytes(good + _line(3).replace(b"\n", b"\r"))
    log = EventLog(path)
    assert log.read().truncated
    with pytest.raises(TruncatedLogError):
        log.append(_new(3))
    log.repair()
    assert path.read_bytes() == good


# --- very large events -------------------------------------------------------------------------------------


def test_very_large_event_round_trips_and_seq_continues(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(_new(1))
    blob = "€" * 3_000_000  # ~9 MB of UTF-8
    log.append(_new(2, blob=blob))
    assert [e.seq for e in log.append(_new(3))] == [3]
    events = log.read(strict=True).events
    assert events[1].data["blob"] == blob and [e.seq for e in events] == [1, 2, 3]


@pytest.mark.parametrize("delta", [-2, -1, 0, 1, 2])
@pytest.mark.parametrize("chunks", [1, 2])
def test_last_line_length_at_tail_chunk_boundaries(
    tmp_path: Path, chunks: int, delta: int
) -> None:
    """The writer finds the last seq by reading back in 64 KiB chunks; lines that straddle a boundary must work."""
    path = tmp_path / "events.jsonl"
    head = _line(1)
    stub = _line(2, data={"p": ""})
    target = chunks * 64 * 1024 + delta  # length of the last line without its "\n"
    pad = target - (len(stub) - 1)
    last = _line(2, data={"p": "q" * pad})
    assert len(last) - 1 == target
    path.write_bytes(head + last)
    assert [e.seq for e in EventLog(path).append(_new(3))] == [3]
    # and when the long line is the only line
    path.write_bytes(last.replace(b'"seq":2', b'"seq":1'))
    assert [e.seq for e in EventLog(path).append(_new(2))] == [2]


# --- AC5: typed constructors round-trip ----------------------------------------------------------------------

USER = ev.user_actor("Adhya Anil", "adhya@example.com")

CONSTRUCTED: dict[str, ev.NewEvent] = {
    "intake": ev.intake(
        "R1",
        actor=USER,
        source="text",
        request_path="sdlc/requirements/R1/request.md",
        title="Add ✓",
    ),
    "gate_human": ev.gate_decision(
        "R1",
        actor=USER,
        gate="architect",
        decision="changes",
        channel="form",
        comments="split T3\nplease",
        version="v2",
    ),
    "gate_terminal": ev.gate_decision(
        "R1",
        actor=USER,
        gate="result",
        decision="reject",
        channel="terminal",
        task="T2",
        attempt=3,
    ),
    "gate_auto": ev.gate_decision(
        "R1", actor="auto", gate="plan", decision="approve", channel="auto", task="T1"
    ),
    "attempt_started": ev.attempt_started("R1", "T1", 1, rung="R2"),
    "attempt_finished": ev.attempt_finished(
        "R1", "T1", 1, outcome="failed", report_path="r.md", counted=False
    ),
    "escalation": ev.escalation(
        "R1", reason="3 attempts failed", task="T1", attempt=3, ticket="AD-400"
    ),
    "agent_call": ev.agent_call(
        "R1",
        role="developer",
        model="claude-sonnet",
        effort="high",
        duration_ms=1234,
        tokens={"input": 10, "output": 20, "cache_read": 0},
        cost_usd=0.0125,
        transcript=".ultron/transcripts/x.jsonl",
        prompt_sha256="a" * 64,
        result_sha256="b" * 64,
        task="T1",
        attempt=1,
        stop_reason="end_turn",
    ),
    "config_snapshot": ev.config_snapshot(
        "R1",
        max_attempts=3,
        gates={"architect": "human", "plan": "auto", "tester": "human"},
    ),
}


def test_ac5_every_spec_event_type_has_a_constructor() -> None:
    types = {e.type for e in CONSTRUCTED.values()}
    assert {
        ev.INTAKE,
        ev.GATE_DECISION,
        ev.ATTEMPT_STARTED,
        ev.ATTEMPT_FINISHED,
        ev.ESCALATION,
        ev.AGENT_CALL,
        ev.CONFIG_SNAPSHOT,
    } <= types


def test_ac5_constructors_round_trip_through_write_and_read(tmp_path: Path) -> None:
    log = _log(tmp_path)
    written = log.append(list(CONSTRUCTED.values()))
    again = EventLog(log.path).read(strict=True).events
    assert again == written
    for new, got in zip(CONSTRUCTED.values(), again, strict=True):
        assert (
            got.actor,
            got.type,
            got.requirement,
            got.task,
            got.attempt,
            got.data,
        ) == (
            new.actor,
            new.type,
            new.requirement,
            new.task,
            new.attempt,
            new.data,
        )
        assert ev.from_json(ev.to_json_line(got)) == got


def test_ac5_gate_decision_records_who_when_channel_decision_comments(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    (got,) = log.append(CONSTRUCTED["gate_human"])
    obj = json.loads(log.path.read_bytes())
    assert obj["actor"] == "user:Adhya Anil <adhya@example.com>"
    assert obj["time"] == got.time and obj["time"].endswith("Z")
    assert obj["data"]["channel"] == "form" and obj["data"]["decision"] == "changes"
    assert obj["data"]["comments"] == "split T3\nplease"
    with pytest.raises(ev.EventError):
        ev.gate_decision(
            "R1", actor=USER, gate="plan", decision="approve", channel="auto"
        )
    with pytest.raises(ev.EventError):
        ev.gate_decision(
            "R1", actor="auto", gate="plan", decision="approve", channel="form"
        )


def test_ac5_actor_kinds() -> None:
    assert ev.validate_actor("engine") == "engine"
    assert ev.validate_actor("auto") == "auto"
    assert ev.agent_actor("tester") == "agent:tester"
    assert USER.startswith("user:")
    for bad in ("", "Engine", "agent:", "user:", "user:a\nb", 5):
        with pytest.raises(ev.EventError):
            ev.validate_actor(bad)


# --- attempt 2: every way an event can fail to serialise or parse -------------------------------------------
# Writing: the failure is an `InvalidEventError` (an `EventError`), never a raw TypeError, ValueError,
# UnicodeEncodeError or RecursionError, and the log's bytes don't change. Reading: a bad line is an
# `InvalidEventError` from `from_json` and a `CorruptLogError` from the log, never a raw exception.


class _Opaque:
    pass


def _nested(depth: int) -> dict[str, Any]:
    root: dict[str, Any] = {}
    node = root
    for _ in range(depth):
        child: dict[str, Any] = {}
        node["a"] = child
        node = child
    return root


def _nested_list(depth: int) -> list[Any]:
    root: list[Any] = []
    node = root
    for _ in range(depth):
        child: list[Any] = []
        node.append(child)
        node = child
    return root


def _cyclic_dict() -> dict[str, Any]:
    d: dict[str, Any] = {}
    d["self"] = d
    return d


def _cyclic_list() -> list[Any]:
    lst: list[Any] = []
    lst.append(lst)
    return lst


def _raw(data: dict[Any, Any], **over: Any) -> ev.NewEvent:
    fields: dict[str, Any] = {"actor": "engine", "type": "probe", "requirement": "R1"}
    fields.update(over)
    return ev.NewEvent(data=data, **fields)


UNSERIALISABLE: dict[str, Callable[[], ev.NewEvent]] = {
    "surrogate-value": lambda: _new(9, path="C:\\tmp\\\udcff.txt"),
    "surrogate-key": lambda: _raw({"\ud800": 1}),
    "surrogate-nested-list": lambda: _new(9, x=["ok", ["\udfff"]]),
    "surrogate-nested-key": lambda: _new(9, x={"\udc80": 1}),
    "surrogate-pair-as-two-code-points": lambda: _new(9, x="\ud83d\ude00"),
    "surrogates-reversed": lambda: _new(9, x="\udc00\ud800"),
    "surrogate-in-type": lambda: _raw({}, type="probe\udcff"),
    "surrogate-in-actor": lambda: _raw({}, actor="user:Ann\udcff <a@b.c>"),
    "surrogate-in-requirement": lambda: _raw({}, requirement="R\udcff"),
    "surrogate-in-task": lambda: _raw({}, task="T\udcff", attempt=1),
    "surrogate-in-gate-comments": lambda: ev.gate_decision(
        "R1",
        actor=USER,
        gate="plan",
        decision="changes",
        channel="form",
        comments="see \udcff",
    ),
    "nan": lambda: _new(9, x=float("nan")),
    "inf": lambda: _new(9, x=float("-inf")),
    "nan-key": lambda: _raw({float("nan"): 1}),
    "big-int": lambda: _new(9, n=10**5000),
    "big-int-key": lambda: _raw({10**5000: 1}),
    "tuple-key": lambda: _raw({(1, 2): 1}),
    "bytes": lambda: _new(9, x=b"raw"),
    "set": lambda: _new(9, x={1, 2}),
    "object": lambda: _new(9, x=_Opaque()),
    "cycle-dict": lambda: _new(9, x=_cyclic_dict()),
    "cycle-list": lambda: _new(9, x=_cyclic_list()),
    "deep-dict": lambda: _new(9, x=_nested(100_000)),
    "deep-list": lambda: _new(9, x=_nested_list(100_000)),
}


def _stamp(new: ev.NewEvent) -> ev.Event:
    return ev.Event(
        1,
        1,
        "2026-11-10T09:00:00.000Z",
        new.actor,
        new.type,
        new.requirement,
        new.task,
        new.attempt,
        new.data,
        "0.0.1",
    )


@pytest.mark.parametrize("make", UNSERIALISABLE.values(), ids=UNSERIALISABLE.keys())
def test_a2_unserialisable_event_raises_invalid_event_error(
    make: Callable[[], ev.NewEvent],
) -> None:
    with pytest.raises(ev.InvalidEventError):
        ev.to_json_line(_stamp(make()))


@pytest.mark.parametrize("make", UNSERIALISABLE.values(), ids=UNSERIALISABLE.keys())
def test_a2_unserialisable_event_anywhere_in_a_batch_writes_nothing(
    tmp_path: Path, make: Callable[[], ev.NewEvent]
) -> None:
    log = _log(tmp_path)
    log.append(_new(1))
    before = log.path.read_bytes()
    for batch in ([make(), _new(2)], [_new(2), make(), _new(3)], [_new(2), make()]):
        with pytest.raises(ev.InvalidEventError):
            log.append(batch)
        assert log.path.read_bytes() == before
    assert [e.seq for e in log.append(_new(2))] == [2]
    assert _seqs(log) == [1, 2]


@pytest.mark.parametrize(
    "case", ["surrogate-value", "nan", "big-int", "cycle-dict", "deep-dict"]
)
def test_a2_failed_first_append_leaves_no_bytes(tmp_path: Path, case: str) -> None:
    log = _log(tmp_path)
    with pytest.raises(ev.InvalidEventError):
        log.append(UNSERIALISABLE[case]())
    assert not log.path.exists() or log.path.read_bytes() == b""
    assert [e.seq for e in log.append(_new(1))] == [1]


def _with_data(seq: int, data: bytes) -> bytes:
    """`_line(seq)` without its newline, with `data` spliced in as raw JSON text."""
    line = _line(seq)[:-1]
    old = b'"data":{"n":%d}' % seq
    assert old in line
    return line.replace(old, b'"data":' + data)


UNPARSEABLE: dict[str, bytes] = {
    "utf8-bom": b"\xef\xbb\xbf" + _line(2)[:-1],
    "utf-16": _line(2)[:-1].decode().encode("utf-16"),
    "invalid-utf8": _line(2)[:-1].replace(b'"probe"', b'"pr\xffbe"'),
    "overlong-utf8": _line(2)[:-1].replace(b'"probe"', b'"pr\xc0\xafbe"'),
    "utf8-encoded-surrogate": _line(2)[:-1].replace(b'"probe"', b'"pr\xed\xa0\x80"'),
    "deep-object": _with_data(2, b'{"a":' * 100_000 + b"1" + b"}" * 100_000),
    "deep-array": _with_data(2, b"[" * 100_000 + b"]" * 100_000),
    "big-int-seq": _line(2)[:-1].replace(b'"seq":2', b'"seq":' + b"2" * 5000),
    "big-int-in-data": _with_data(2, b'{"n":' + b"9" * 5000 + b"}"),
    "nul": b"\x00",
    "cut-json": _line(2)[:-12],
    "trailing-garbage": _line(2)[:-1] + b"x",
    "two-objects": _line(2)[:-1] + _line(3)[:-1],
    "array": b"[]",
    "null": b"null",
    "string": b'"seq"',
    "number": b"2",
}


@pytest.mark.parametrize("raw", UNPARSEABLE.values(), ids=UNPARSEABLE.keys())
def test_a2_unparseable_line_raises_invalid_event_error(raw: bytes) -> None:
    assert b"\n" not in raw
    with pytest.raises(ev.InvalidEventError):
        ev.from_json(raw)
    with pytest.raises(ev.InvalidEventError):
        ev.from_json(raw + b"\n")


@pytest.mark.parametrize("raw", UNPARSEABLE.values(), ids=UNPARSEABLE.keys())
def test_a2_unparseable_middle_line_is_corrupt_and_left_alone(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "events.jsonl"
    content = _line(1) + raw + b"\n" + _line(3)
    path.write_bytes(content)
    log = EventLog(path)
    for strict in (False, True):
        with pytest.raises(CorruptLogError) as info:
            log.read(strict=strict)
        assert info.value.line_no == 2
    assert path.read_bytes() == content


@pytest.mark.parametrize("raw", UNPARSEABLE.values(), ids=UNPARSEABLE.keys())
def test_a2_unparseable_last_line_blocks_append_and_writes_nothing(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "events.jsonl"
    content = _line(1) + raw + b"\n"
    path.write_bytes(content)
    log = EventLog(path)
    with pytest.raises(CorruptLogError):
        log.append(_new(3))
    with pytest.raises(CorruptLogError):
        log.read()
    assert log.repair() == b""  # a complete line is not a crash tail; repair leaves it
    assert path.read_bytes() == content


def test_a2_reasonable_nesting_still_round_trips_after_a_recursion_failure(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    with pytest.raises(ev.InvalidEventError):
        log.append(_new(1, x=_nested(100_000)))
    nested = {"d": _nested(200), "l": _nested_list(200)}
    (written,) = log.append(_new(1, **nested))
    (read,) = log.read(strict=True).events
    assert read == written and read.data["d"] == nested["d"]


def test_a2_ascii_escaped_text_from_another_writer_reads_and_rewrites_as_utf8() -> None:
    """A line from a writer using `\\uXXXX` escapes (valid surrogate pairs included) is not refused."""
    text = "\U0001f600 é"
    line = _line(1, data={"t": text}).replace(text.encode(), b"\\ud83d\\ude00 \\u00e9")
    assert line.isascii()
    event = ev.from_json(line)
    assert event.data == {"t": text}
    assert text.encode() in ev.to_json_line(event)

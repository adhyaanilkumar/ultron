"""Acceptance tests for T05 (event log), written by the testing agent from the ACs and the diff only.

Focus: what could lose or corrupt the audit log. Real concurrent writer processes with a reader running at the
same time, crashes at many byte offsets, append-only guarantees, CRLF, very large events, and older logs.

Rerun after the tasks.md revision (each criterion has a `verify_with`):
- AC1 `pytest`: `test_ac1_*` (gap-free seq, byte prefix unchanged after appends).
- AC2 `pytest`: `test_ac2_*` (4 and 8 real processes, a reader running, non-ASCII paths and payloads, the engine's
  `.ultron/locks/` lock path). Each writer reports the lock it used, so a CI run names the lock it proved
  (msvcrt on Windows, fcntl on Linux). The `ci` half of AC2, and AC6, need a real GitHub Actions run.
- AC3 `pytest`: `test_ac3_*`, including the developer's `tests/store/fixtures/events_v0_0_1.jsonl` read and
  round-tripped byte for byte, and future schemas refused rather than misread.
- AC4 `pytest`: `test_ac4_*`, truncation at many byte offsets, in-file and by a real process dying mid-write, and
  a repair that must wait for a write in progress instead of cutting it.
- AC5 `pytest`: `test_ac5_*`, one round-trip per event type, with non-ASCII in every free-text field.
- No T05 test may be skipped or expected to fail: `test_no_t05_test_is_skipped_*`.

Reopen for lock starvation (R4): `test_reopen_*`. The once-flaky AC2 test 30 times unloaded and 30 times with a
busy process per CPU, under a duration bound; mutual exclusion with threads and processes together (no torn or
interleaved line, strict seq, no lost update); holders and waiters killed while holding the lock or the turn lock;
the kept read handle against other writers, git, resets under the lock and in-place rewrites; `os.replace` as the
plan's risk 1 says; no handle leaks; the Linux branch still on `fcntl.flock`.
"""

import ast
import contextlib
import gc
import importlib.util
import json
import os
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import types
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

import pytest

from ultron import __version__
from ultron.store import events as ev
from ultron.store._filelock import LOCK_BACKEND, FileLock
from ultron.store.eventlog import (
    CorruptLogError,
    EventLog,
    LockTimeout,
    NewerSchemaError,
    TruncatedLogError,
    events_path,
)

DEADLINE_S = 45
EXPECTED_BACKEND = "msvcrt" if sys.platform == "win32" else "fcntl"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "store" / "fixtures" / "events_v0_0_1.jsonl"


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
from ultron.store._filelock import LOCK_BACKEND
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
print(LOCK_BACKEND)
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
            used, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
            # the lock this run proved (AC2 `ci`, AC6): msvcrt on Windows, fcntl on Linux
            assert used.decode().strip() == EXPECTED_BACKEND
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
    assert {o["ultron_version"] for o in objs} == {__version__}
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


# --- rerun: probes added after the tasks.md revision --------------------------------------------------------

# 2-, 3- and 4-byte UTF-8, plus U+2028 and U+0085, which str.splitlines() treats as line breaks.
TEXT_U = "Ä日本😀\u2028\x85é ✓"
NAME_U = "Ädhya Ånil 日本 😀"
SPEC_FIELDS = frozenset(
    {
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
    }
)


def test_ac2_lock_backend_is_this_os_lock() -> None:
    assert LOCK_BACKEND == EXPECTED_BACKEND


# AC2 in the engine's own setup: the lock under `.ultron/locks/` (spec §10), a work folder whose path has spaces
# and non-ASCII, and payloads of many lengths in multi-byte characters, so a torn or interleaved write would
# split a character or mix two workers' text.
WRITER_U = r"""
import sys, time
from pathlib import Path
from ultron.store._filelock import LOCK_BACKEND
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent, user_actor

path, lock, ready, go, w, n = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], int(sys.argv[6])
text = bytes.fromhex(sys.argv[7]).decode("utf-8")
log = EventLog(path, lock)
actor = user_actor("Wörker " + w + " 日本", "w" + w + "@example.com")
ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
for i in range(n):
    log.append(NewEvent(actor=actor, type="probe", requirement="R1", task="T" + w,
                        data={"w": w, "i": i, "text": text * (1 + (7 * i + int(w)) % 40)}))
print(LOCK_BACKEND)
"""


def test_ac2_engine_lock_path_non_ascii_paths_and_payloads(tmp_path: Path) -> None:
    workers, per_worker = 4, 50
    base = tmp_path / "wörk dír 日本 😀"
    path = base / "sdlc" / "requirements" / "R1" / "events.jsonl"
    lock = base / ".ultron" / "locks" / "R1.events.lock"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{w}" for w in range(workers)]
    deadline = time.monotonic() + DEADLINE_S
    procs = [
        _spawn(
            WRITER_U,
            str(path),
            str(lock),
            str(ready[w]),
            str(go),
            str(w),
            str(per_worker),
            TEXT_U.encode().hex(),
        )
        for w in range(workers)
    ]
    try:
        _wait(lambda: all(p.exists() for p in ready), deadline, "writers to start")
        go.touch()
        for proc in procs:
            used, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
            assert used.decode().strip() == EXPECTED_BACKEND
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    data = path.read_bytes()
    text = data.decode("utf-8")  # strict: a torn multi-byte character fails here
    assert text.endswith("\n") and "\r" not in text
    lines = data.split(b"\n")[:-1]
    assert len(lines) == workers * per_worker
    objs = [json.loads(line) for line in lines]
    assert [o["seq"] for o in objs] == list(range(1, workers * per_worker + 1))
    for w in range(workers):
        mine = [o for o in objs if o["data"]["w"] == str(w)]
        assert [o["data"]["i"] for o in mine] == list(range(per_worker))
        for o in mine:
            assert o["data"]["text"] == TEXT_U * (1 + (7 * o["data"]["i"] + w) % 40)
            assert o["actor"] == f"user:Wörker {w} 日本 <w{w}@example.com>"
            assert o["task"] == f"T{w}"
    assert TEXT_U.encode() in data  # written as UTF-8, not \u escapes (NFR2: readable)
    assert lock.exists() and not path.with_name(path.name + ".lock").exists()
    events = EventLog(path, lock).read(strict=True).events
    assert [e.seq for e in events] == list(range(1, workers * per_worker + 1))


# --- AC3: the developer's fixture (named by verify_with), future and malformed schemas -----------------------


def test_ac3_fixture_v0_0_1_reads_and_round_trips(tmp_path: Path) -> None:
    raw = FIXTURE.read_bytes()
    # A Windows checkout with core.autocrlf=true (the default on GitHub's windows runner) gives CRLF here.
    lines = [line.rstrip(b"\r") + b"\n" for line in raw.split(b"\n")[:-1]]
    assert raw.endswith(b"\n") and len(lines) >= 2
    path = tmp_path / "events.jsonl"
    path.write_bytes(raw)

    events = EventLog(path).read(strict=True).events
    assert [e.seq for e in events] == list(range(1, len(lines) + 1))
    assert {e.ultron_version for e in events} == {"0.0.1"}
    assert any(e.extra for e in events), "the fixture must carry an unknown field"
    for line, event in zip(lines, events, strict=True):
        obj = json.loads(line)
        assert event.extra == {k: v for k, v in obj.items() if k not in SPEC_FIELDS}
        assert event.data == obj["data"]  # unknown keys inside data are kept too
        assert ev.to_json_line(event) == line  # round trip, byte for byte
        assert ev.from_json(ev.to_json_line(event)) == event
    assert path.read_bytes() == raw  # reading never rewrites

    (new,) = EventLog(path).append(_new(9))
    assert new.seq == len(lines) + 1 and new.ultron_version == __version__
    assert path.read_bytes().startswith(raw)
    again = EventLog(path).read(strict=True).events
    assert again[:-1] == events and again[-1] == new


@pytest.mark.parametrize("where", ["middle", "last"])
@pytest.mark.parametrize("schema", [2, 99, 10**30])
def test_ac3_future_schema_is_refused_not_misread(
    tmp_path: Path, schema: int, where: str
) -> None:
    future = _line(2, schema=schema)
    content = _line(1) + future + (_line(3) if where == "middle" else b"")
    path = tmp_path / "events.jsonl"
    path.write_bytes(content)
    log = EventLog(path)
    for strict in (False, True):
        with pytest.raises(NewerSchemaError) as info:
            log.read(strict=strict)
        assert info.value.schema == schema
    with pytest.raises(NewerSchemaError):
        ev.from_json(future)
    if where == "last":
        with pytest.raises(NewerSchemaError):
            log.append(_new(9))
        assert log.repair() == b""
    assert path.read_bytes() == content


def test_ac3_future_schema_with_a_new_shape_is_newer_not_corrupt(
    tmp_path: Path,
) -> None:
    """A future line may rename or drop today's fields; it must still be reported as newer, not as corrupt."""
    path = tmp_path / "events.jsonl"
    content = _line(1) + b'{"schema":2,"seq":2,"who":"x","kind":"y","payload":[1]}\n'
    path.write_bytes(content)
    with pytest.raises(NewerSchemaError):
        EventLog(path).read()
    with pytest.raises(NewerSchemaError):
        EventLog(path).append(_new(3))
    assert path.read_bytes() == content


@pytest.mark.parametrize(
    "schema",
    ["2", "1", 0, -1, True, 1.5],
    ids=["str-2", "str-1", "0", "-1", "true", "1.5"],
)
def test_ac3_malformed_schema_is_never_read_as_an_event(
    tmp_path: Path, schema: Any
) -> None:
    path = tmp_path / "events.jsonl"
    content = _line(1) + _line(2, schema=schema) + _line(3)
    path.write_bytes(content)
    with pytest.raises((CorruptLogError, NewerSchemaError)):
        EventLog(path).read()
    assert path.read_bytes() == content


# --- the reader validates order ------------------------------------------------------------------------------

ORDERS: dict[str, tuple[list[int], int]] = {
    "gap": ([1, 3], 2),
    "duplicate": ([1, 1], 2),
    "reversed": ([2, 1], 1),
    "starts-at-0": ([0, 1], 1),
    "starts-at-2": ([2, 3], 1),
    "repeat-later": ([1, 2, 2], 3),
    "jumps-back": ([1, 2, 3, 2], 4),
}


@pytest.mark.parametrize(("seqs", "bad_line"), ORDERS.values(), ids=ORDERS.keys())
def test_reader_rejects_seq_out_of_order(
    tmp_path: Path, seqs: list[int], bad_line: int
) -> None:
    path = tmp_path / "events.jsonl"
    content = b"".join(_line(s) for s in seqs)
    path.write_bytes(content)
    for strict in (False, True):
        with pytest.raises(CorruptLogError) as info:
            EventLog(path).read(strict=strict)
        assert info.value.line_no == bad_line
    assert path.read_bytes() == content


def test_events_path_is_the_spec_location(tmp_path: Path) -> None:
    assert (
        events_path(tmp_path, "R12")
        == tmp_path / "sdlc" / "requirements" / "R12" / "events.jsonl"
    )
    for bad in ["../R1", "R1/..", "..", "", "a/b", "a\\b", "R1\n", "/abs", "R 1"]:
        with pytest.raises(ValueError):
            events_path(tmp_path, bad)


# --- AC4: a real process dying mid-line at several offsets; repair racing a write in progress -----------------

THIRD = _line(3, data={"t": TEXT_U * 3})
_FIRST_MULTI = next(i for i, b in enumerate(THIRD) if b >= 0x80)
CRASH_CUTS: dict[str, int] = {
    "1-byte": 1,
    "inside-a-key": 12,
    "inside-a-multibyte-char": _FIRST_MULTI + 1,
    "mid-line": len(THIRD) // 2,
    "before-closing-brace": len(THIRD) - 2,
    "all-but-newline": len(THIRD) - 1,
}


@pytest.mark.parametrize("cut", CRASH_CUTS.values(), ids=CRASH_CUTS.keys())
def test_ac4_real_process_dies_mid_line(tmp_path: Path, cut: int) -> None:
    log = _log(tmp_path)
    log.append([_new(1), _new(2, text=TEXT_U)])
    good = log.path.read_bytes()
    partial = THIRD[:cut]
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

    after = EventLog(log.path, lock_timeout=10)  # the dead writer's lock must be free
    result = after.read()
    assert result.truncated_tail == partial
    assert [e.seq for e in result.events] == [1, 2]
    with pytest.raises(TruncatedLogError):
        after.read(strict=True)
    with pytest.raises(TruncatedLogError):
        after.append(_new(3))
    assert log.path.read_bytes() == good + partial
    assert after.repair() == partial
    assert log.path.read_bytes() == good
    assert [e.seq for e in after.append(_new(3, text=TEXT_U))] == [3]
    assert log.path.read_bytes().startswith(good)
    assert _seqs(log) == [1, 2, 3]


SLOW_WRITER = r"""
import os, sys, time
from pathlib import Path
from ultron.store._filelock import FileLock

path, lock, line, half, go, cut = sys.argv[1], sys.argv[2], bytes.fromhex(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]), int(sys.argv[6])
with FileLock(lock):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
    os.write(fd, line[:cut])
    half.touch()
    end = time.monotonic() + 40
    while not go.exists():
        if time.monotonic() > end:
            sys.exit(3)
        time.sleep(0.005)
    os.write(fd, line[cut:])
    os.close(fd)
"""


def test_ac4_repair_waits_for_a_write_in_progress(tmp_path: Path) -> None:
    """A lock-free reader can see a half-written line. Repairing then must wait for the writer, not cut its line."""
    log = _log(tmp_path)
    log.append([_new(1), _new(2)])
    good = log.path.read_bytes()
    cut = _FIRST_MULTI + 1
    half, go = tmp_path / "half", tmp_path / "go"
    proc = _spawn(
        SLOW_WRITER,
        str(log.path),
        str(log.lock_path),
        THIRD.hex(),
        str(half),
        str(go),
        str(cut),
    )
    results: dict[str, Any] = {}

    def run(name: str, action: Callable[[], Any]) -> None:
        try:
            results[name] = action()
        except BaseException as exc:  # noqa: BLE001 - checked below
            results[name] = exc

    threads = [
        threading.Thread(
            target=run, args=("repair", EventLog(log.path, lock_timeout=30).repair)
        ),
        threading.Thread(
            target=run,
            args=(
                "append",
                lambda: EventLog(log.path, lock_timeout=30).append(_new(4)),
            ),
        ),
    ]
    try:
        _wait(half.exists, time.monotonic() + DEADLINE_S, "the writer to start a line")
        seen = log.read()
        assert seen.truncated_tail == THIRD[:cut]  # what a lock-free reader sees
        for t in threads:
            t.start()
        time.sleep(0.3)
        assert all(t.is_alive() for t in threads), "repair/append did not wait"
        assert log.path.read_bytes() == good + THIRD[:cut]
        go.touch()
        for t in threads:
            t.join(timeout=DEADLINE_S)
        _, err = proc.communicate(timeout=DEADLINE_S)
        assert proc.returncode == 0, err.decode(errors="replace")
    finally:
        go.touch()
        if proc.poll() is None:
            proc.kill()
    assert results["repair"] == b"", results["repair"]
    assert [e.seq for e in results["append"]] == [4], results["append"]
    assert log.path.read_bytes().startswith(good + THIRD)
    assert _seqs(log) == [1, 2, 3, 4]


def test_ac4_append_and_repair_with_the_log_open_elsewhere(tmp_path: Path) -> None:
    """A viewer holding the file open (common on Windows) must not block appends or a repair."""
    log = _log(tmp_path)
    log.append(_new(1))
    with log.path.open("rb") as viewer:
        log.append(_new(2))
        with log.path.open("ab") as f:
            f.write(b'{"seq":3,"time')
        assert log.repair() == b'{"seq":3,"time'
        assert [e.seq for e in log.append(_new(3))] == [3]
        viewer.seek(0)
        assert viewer.read() == log.path.read_bytes()
    assert _seqs(log) == [1, 2, 3]


@pytest.mark.parametrize("shift", [0, 1, 2, 3])
def test_ac1_multibyte_char_across_the_tail_scan_boundary(
    tmp_path: Path, shift: int
) -> None:
    """The writer reads the last line back in 64 KiB chunks; a 4-byte character split by a chunk must not matter."""
    path = tmp_path / "events.jsonl"
    last = _line(2, data={"p": "a" * shift + "😀" * 17_000})
    assert len(last) > 64 * 1024
    path.write_bytes(_line(1) + last)
    assert [e.seq for e in EventLog(path).append(_new(3))] == [3]
    assert _seqs(EventLog(path)) == [1, 2, 3]


# --- AC5: one round trip per event type, with non-ASCII in every free-text field ------------------------------

CONSTRUCTED_U: dict[str, tuple[str, Callable[[], ev.NewEvent]]] = {
    "intake": (
        ev.INTAKE,
        lambda: ev.intake(
            "R1",
            actor=ev.user_actor(NAME_U, "ädhya@例え.jp"),
            source="text",
            request_path="sdlc/requirements/R1/réquest 日本.md",
            title=TEXT_U,
        ),
    ),
    "gate_decision-human": (
        ev.GATE_DECISION,
        lambda: ev.gate_decision(
            "R1",
            actor=ev.user_actor(NAME_U, "a@b.c"),
            gate="architect",
            decision="changes",
            channel="terminal",
            comments=TEXT_U + "\nzweite Zeile ✓\ttab",
            task="T1",
            attempt=2,
            version="v2 ✓",
        ),
    ),
    "gate_decision-auto": (
        ev.GATE_DECISION,
        lambda: ev.gate_decision(
            "R1", actor="auto", gate="tester", decision="approve", channel="auto"
        ),
    ),
    "attempt_started": (
        ev.ATTEMPT_STARTED,
        lambda: ev.attempt_started("R1", "T1", 1, rung="R2 ✓ 日本"),
    ),
    "attempt_finished": (
        ev.ATTEMPT_FINISHED,
        lambda: ev.attempt_finished(
            "R1",
            "T1",
            3,
            outcome="failed ✗",
            report_path="sdlc/requirements/R1/tasks/T1/attempt-3 日本.md",
            counted=True,
        ),
    ),
    "escalation": (
        ev.ESCALATION,
        lambda: ev.escalation(
            "R1", reason=TEXT_U, task="T1", attempt=3, ticket="AD-400"
        ),
    ),
    "agent_call": (
        ev.AGENT_CALL,
        lambda: ev.agent_call(
            "R1",
            role="developer",
            model="claude-opus-5-5",
            effort="high",
            duration_ms=0,
            tokens={"input": 0, "output": 2**40, "cache_read": 7},
            cost_usd=0,
            transcript=".ultron/transcripts/日本 😀.jsonl",
            prompt_sha256="0" * 64,
            result_sha256="f" * 64,
            task="T1",
            attempt=1,
            stop_reason=TEXT_U,
        ),
    ),
    "config_snapshot": (
        ev.CONFIG_SNAPSHOT,
        lambda: ev.config_snapshot(
            "R1",
            max_attempts=5,
            gates={
                "architect": "human",
                "plan": "auto",
                "tester": "auto",
                "merge": "human",
                "deploy": "human",
            },
        ),
    ),
}


def test_ac5_constructors_cover_every_spec_event_type() -> None:
    assert {t for t, _ in CONSTRUCTED_U.values()} == {
        ev.INTAKE,
        ev.GATE_DECISION,
        ev.ATTEMPT_STARTED,
        ev.ATTEMPT_FINISHED,
        ev.ESCALATION,
        ev.AGENT_CALL,
        ev.CONFIG_SNAPSHOT,
    }


@pytest.mark.parametrize("name", CONSTRUCTED_U)
def test_ac5_each_event_type_round_trips(tmp_path: Path, name: str) -> None:
    kind, make = CONSTRUCTED_U[name]
    new = make()
    assert new.type == kind
    log = _log(tmp_path)
    log.append(_new(0))  # in the middle of a log, not only as its first line
    (written,) = log.append(new)
    log.append(_new(2))

    raw = log.path.read_bytes()
    raw.decode("utf-8")  # strict
    line = raw.split(b"\n")[1] + b"\n"
    assert b"\\u" not in line  # non-ASCII is stored as UTF-8 text, readable (NFR2)
    got = EventLog(log.path).read(strict=True).events[1]
    assert got == written
    assert (got.actor, got.type, got.requirement, got.task, got.attempt, got.data) == (
        new.actor,
        new.type,
        new.requirement,
        new.task,
        new.attempt,
        new.data,
    )
    assert got.seq == 2 and got.ultron_version == __version__ and got.extra == {}
    assert ev.to_json_line(got) == line
    assert json.loads(line)["data"] == new.data


UNENCODABLE_U: dict[str, Callable[[], ev.NewEvent]] = {
    "intake-title": lambda: ev.intake(
        "R1", actor="engine", source="text", request_path="r.md", title="x\udcff"
    ),
    "intake-actor": lambda: ev.intake(
        "R1",
        actor=ev.user_actor("Ann\ud800", "a@b.c"),
        source="text",
        request_path="r.md",
    ),
    "intake-path": lambda: ev.intake(
        "R1", actor="engine", source="file", request_path="C:\\r\udcff.md"
    ),
    "gate-name": lambda: ev.gate_decision(
        "R1", actor="auto", gate="pl\udfffan", decision="approve", channel="auto"
    ),
    "gate-version": lambda: ev.gate_decision(
        "R1",
        actor="auto",
        gate="plan",
        decision="approve",
        channel="auto",
        version="\udcff",
    ),
    "attempt-rung": lambda: ev.attempt_started("R1", "T1", 1, rung="R\udcff"),
    "attempt-report": lambda: ev.attempt_finished(
        "R1", "T1", 1, outcome="failed", report_path="C:\\r\udcff.md"
    ),
    "escalation-reason": lambda: ev.escalation("R1", reason="r\udcff"),
    "agent-transcript": lambda: ev.agent_call(
        "R1",
        role="tester",
        model="m",
        effort="low",
        duration_ms=1,
        tokens={"input": 1},
        cost_usd=0.1,
        transcript="C:\\t\udcff.jsonl",
        prompt_sha256="a",
        result_sha256="b",
    ),
    "agent-token-kind": lambda: ev.agent_call(
        "R1",
        role="tester",
        model="m",
        effort="low",
        duration_ms=1,
        tokens={"in\udcff": 1},
        cost_usd=0.1,
        transcript="t",
        prompt_sha256="a",
        result_sha256="b",
    ),
    "config-gate-name": lambda: ev.config_snapshot(
        "R1", max_attempts=3, gates={"pl\udcffan": "auto"}
    ),
}


@pytest.mark.parametrize("make", UNENCODABLE_U.values(), ids=UNENCODABLE_U.keys())
def test_ac5_unencodable_text_in_any_constructor_is_an_event_error(
    tmp_path: Path, make: Callable[[], ev.NewEvent]
) -> None:
    """Attempt 1 let a raw UnicodeEncodeError escape; every constructor's text must fail as an EventError."""
    log = _log(tmp_path)
    log.append(_new(1))
    before = log.path.read_bytes()
    with pytest.raises(ev.EventError):
        log.append(make())
    assert log.path.read_bytes() == before
    assert [e.seq for e in log.append(_new(2))] == [2]


def test_unencodable_unknown_field_is_an_event_error() -> None:
    event = ev.Event(
        1, 1, "t", "engine", "probe", "R1", None, None, {}, "0.0.1", {"note": "\udcff"}
    )
    with pytest.raises(ev.InvalidEventError):
        ev.to_json_line(event)


def test_escaped_lone_surrogate_from_another_writer_never_raises_a_raw_unicode_error(
    tmp_path: Path,
) -> None:
    """`"\\udcff"` is legal JSON text but can't be UTF-8. Refusing it or reading it are both fine; a raw
    UnicodeError (attempt 1's failure) is not, on read, on re-serialise or on the next append."""
    path = tmp_path / "events.jsonl"
    content = _line(1) + _line(2, data={"p": "X"}).replace(b'"X"', b'"\\udcff"')
    path.write_bytes(content)
    log = EventLog(path)
    try:
        events = log.read(strict=True).events
    except CorruptLogError:
        with pytest.raises(CorruptLogError):
            log.append(_new(3))
        assert path.read_bytes() == content
        return
    assert [e.seq for e in events] == [1, 2]
    try:
        ev.to_json_line(events[1])
    except ev.InvalidEventError:
        pass
    assert [e.seq for e in log.append(_new(3))] == [3]
    assert path.read_bytes().startswith(content)


def test_line_separators_inside_text_never_split_a_line(tmp_path: Path) -> None:
    weird = "a\x85b\u2028c\u2029d\x1ce\x1df\x1eg\x0bh\x0ci\rj\x00k\nl"
    new = ev.NewEvent(
        actor="user:Ädhya 日本\x85 <a@b.c>",
        type="probe\u2028x",
        requirement="R1",
        task="T\u2029",
        attempt=1,
        data={weird: weird, "nested": [weird, {weird: [weird]}]},
    )
    log = _log(tmp_path)
    log.append([new, _new(2)])
    log.append(
        _new(3, **{weird: weird})
    )  # the last line holds them too: the tail scan must cope
    assert [e.seq for e in log.append(_new(4))] == [4]
    raw = log.path.read_bytes()
    assert raw.count(b"\n") == 4 and b"\r" not in raw
    events = log.read(strict=True).events
    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert (events[0].actor, events[0].type, events[0].task, events[0].data) == (
        new.actor,
        new.type,
        new.task,
        new.data,
    )
    assert events[2].data[weird] == weird


# --- no T05 test may be skipped or expected to fail (a skipped test is green in CI but proves nothing) -------

_NO_RUN = frozenset({"skip", "skipif", "xfail", "importorskip", "SkipTest"})


def _t05_test_files() -> list[Path]:
    files = sorted((ROOT / "tests" / "store").glob("*.py"))
    files.append(ROOT / "tests" / "acceptance" / "test_t05.py")
    return files


def test_no_t05_test_is_skipped_in_source() -> None:
    found: list[str] = []
    for path in _t05_test_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else node.name
                if isinstance(node, ast.alias)
                else None
            )
            if name in _NO_RUN:
                found.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}")
    # a conftest hook or pytest config could skip or deselect tests without touching the files
    for conftest in [ROOT / "conftest.py", *(ROOT / "tests").rglob("conftest.py")]:
        if conftest.exists():
            tree = ast.parse(conftest.read_text(encoding="utf-8"))
            hooks = [
                n.name
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name.startswith("pytest_")
            ]
            assert hooks == [], f"{conftest}: {hooks}"
    with (ROOT / "pyproject.toml").open("rb") as f:
        ini = tomllib.load(f).get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = ini.get("addopts", "")
    tokens = addopts if isinstance(addopts, list) else shlex.split(addopts)
    narrowing = ("--deselect", "--ignore", "-k", "-m", "--runxfail")
    assert [t for t in tokens if t.startswith(narrowing)] == []
    assert len(_t05_test_files()) >= 5
    assert found == []


def test_no_t05_store_test_is_skipped_when_run(tmp_path: Path) -> None:
    """Run the developer's T05 tests on this OS and count: nothing skipped, nothing xfailed."""
    report = tmp_path / "store.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/store",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            f"--basetemp={tmp_path / 'basetemp'}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-3000:]
    root = ET.parse(report).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    counts = {
        k: int(suite.get(k, "0")) for k in ("tests", "skipped", "failures", "errors")
    }
    assert counts["skipped"] == 0 and counts["failures"] == 0 and counts["errors"] == 0
    assert counts["tests"] >= 30, counts


# --- reopen (R4): lock starvation ----------------------------------------------------------------------------
# The defect: `test_ac2_engine_lock_path_non_ascii_paths_and_payloads` took ~40 s and failed about 1 run in 11 on
# Windows under load. The fix keeps one read handle per `EventLog` (opened outside the lock, revalidated under it)
# and adds a turn lock (`*.turn.lock`) so a waiter goes before the releaser. These tests check the symptom is gone
# and that neither change costs exclusion, liveness or freshness.

FLAKY_RUNS = 30
# Bounds per run of the once-flaky test (its own deadline is 45 s). Measured by the testing agent (24 CPUs): with
# the fix, mean 0.50 s and max 0.80 s unloaded, mean 2.79 s and max 5.31 s with 24 busy processes. The pre-fix code
# (40b76f2) through the same loop: mean 8.2 s unloaded with runs of 39, 41 and 45 s (the deadline), and up to 26 s
# loaded. The developer measured up to 9.5 s loaded with the fix (15.5 s with 36 busy processes).
FLAKY_UNLOADED_MAX_S = 10.0
FLAKY_UNLOADED_MEAN_S = 3.0
FLAKY_LOADED_MAX_S = 25.0
FLAKY_LOADED_MEAN_S = 10.0

BUSY = """
import sys, time
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    pass
"""


@contextlib.contextmanager
def _cpu_load(seconds: float) -> Iterator[int]:
    """One busy-loop process per logical CPU for the block (killed at the end)."""
    procs = [_spawn(BUSY, str(seconds)) for _ in range(os.cpu_count() or 2)]
    try:
        yield len(procs)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


def _run_flaky_test(tmp_path: Path, runs: int) -> list[float]:
    times: list[float] = []
    for n in range(runs):
        run_dir = tmp_path / f"run-{n:02d}"
        run_dir.mkdir()
        started = time.monotonic()
        test_ac2_engine_lock_path_non_ascii_paths_and_payloads(run_dir)
        times.append(time.monotonic() - started)
    return times


def _report(label: str, times: list[float]) -> str:
    return (
        f"{label}: {len(times)} runs, mean {statistics.mean(times):.2f} s, "
        f"median {statistics.median(times):.2f} s, max {max(times):.2f} s, "
        f"min {min(times):.2f} s; all {[round(t, 2) for t in times]}"
    )


def test_reopen_flaky_ac2_test_30_runs_unloaded(tmp_path: Path) -> None:
    times = _run_flaky_test(tmp_path, FLAKY_RUNS)
    print(_report("unloaded", times), flush=True)
    assert max(times) <= FLAKY_UNLOADED_MAX_S, _report("unloaded", times)
    assert statistics.mean(times) <= FLAKY_UNLOADED_MEAN_S, _report("unloaded", times)


def test_reopen_flaky_ac2_test_30_runs_with_every_cpu_busy(tmp_path: Path) -> None:
    with _cpu_load(FLAKY_RUNS * DEADLINE_S) as busy:
        time.sleep(0.5)  # let the busy processes start
        times = _run_flaky_test(tmp_path, FLAKY_RUNS)
    label = f"{busy} busy processes"
    print(_report(label, times), flush=True)
    assert max(times) <= FLAKY_LOADED_MAX_S, _report(label, times)
    assert statistics.mean(times) <= FLAKY_LOADED_MEAN_S, _report(label, times)


# --- reopen: probing a lock from outside, with the backend's own call --------------------------------------------


def _held(path: Path) -> bool:
    """Whether another handle holds the OS lock on `path` now (takes and drops it if free)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _turn(lock: Path) -> Path:
    return FileLock(lock).turn_path


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=30)


def _assert_free(lock: Path) -> None:
    deadline = time.monotonic() + 5
    while _held(lock) or _held(_turn(lock)):
        assert time.monotonic() < deadline, (
            f"left held: lock {_held(lock)}, turn {_held(_turn(lock))}"
        )
        time.sleep(0.01)


def test_reopen_probe_sees_a_held_lock_and_a_held_turn(tmp_path: Path) -> None:
    """The probe the tests below rely on: it sees `FileLock` holding either lock, in this process too."""
    lock = tmp_path / "x.lock"
    assert not _held(lock)
    with FileLock(lock):
        assert _held(lock) and not _held(_turn(lock))
    with FileLock(_turn(lock)):
        assert _held(_turn(lock)) and not _held(lock)
    _assert_free(lock)


# --- reopen: mutual exclusion is still airtight ------------------------------------------------------------------

# Threads in several processes. Even threads share one `EventLog` (and its kept reader), odd threads have their
# own. Payloads in multi-byte text of many lengths, every 10th past the 64 KiB tail chunk, and batches of 3.
MIXED_WRITER = r"""
import json, sys, threading, time
from pathlib import Path
from ultron.store._filelock import LOCK_BACKEND
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

path, lock, ready, go, p, threads, n = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], int(sys.argv[6]), int(sys.argv[7])
text = bytes.fromhex(sys.argv[8]).decode("utf-8")
SIZE = lambda p, t, i: 3000 if i % 10 == 9 else 1 + (7 * i + 3 * t + len(p)) % 40
shared = EventLog(path, lock, lock_timeout=60)
errors = []

def work(t):
    log = shared if t % 2 == 0 else EventLog(path, lock, lock_timeout=60)
    def new(i):
        return NewEvent(actor="engine", type="probe", requirement="R1", task=p,
                        data={"p": p, "t": t, "i": i, "text": text * SIZE(p, t, i)})
    try:
        i = 0
        while i < n:
            if i % 7 == 3 and i + 3 <= n:
                log.append([new(i + k) for k in range(3)])
                i += 3
            else:
                log.append(new(i))
                i += 1
    except BaseException as exc:
        errors.append(repr(exc))

ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print(json.dumps({"backend": LOCK_BACKEND, "errors": errors}))
"""


def _mixed_size(p: str, t: int, i: int) -> int:
    return 3000 if i % 10 == 9 else 1 + (7 * i + 3 * t + len(p)) % 40


def test_reopen_exclusion_threads_and_processes_together(tmp_path: Path) -> None:
    procs_n, threads_n, per_thread = 4, 4, 50
    base = tmp_path / "wörk 日本"
    path = base / "sdlc" / "requirements" / "R1" / "events.jsonl"
    lock = base / ".ultron" / "locks" / "R1.events.lock"
    go, stop, reader_ready = tmp_path / "go", tmp_path / "stop", tmp_path / "rr"
    ready = [tmp_path / f"ready-{k}" for k in range(procs_n)]
    deadline = time.monotonic() + 120
    reader = _spawn(READER, str(path), str(reader_ready), str(stop))
    procs = [
        _spawn(
            MIXED_WRITER,
            str(path),
            str(lock),
            str(ready[k]),
            str(go),
            f"P{k}",
            str(threads_n),
            str(per_thread),
            TEXT_U.encode().hex(),
        )
        for k in range(procs_n)
    ]
    # threads in this process too, through their own objects and one shared object
    errors: list[BaseException] = []
    shared = EventLog(path, lock, lock_timeout=60)

    def work(t: int) -> None:
        log = shared if t % 2 == 0 else EventLog(path, lock, lock_timeout=60)
        try:
            i = 0
            while i < per_thread:
                size = 3 if i % 7 == 3 and i + 3 <= per_thread else 1
                log.append(
                    [
                        ev.NewEvent(
                            actor="engine",
                            type="probe",
                            requirement="R1",
                            task="main",
                            data={
                                "p": "main",
                                "t": t,
                                "i": i + k,
                                "text": TEXT_U * _mixed_size("main", t, i + k),
                            },
                        )
                        for k in range(size)
                    ]
                )
                i += size
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(threads_n)]
    try:
        _wait(
            lambda: reader_ready.exists() and all(r.exists() for r in ready),
            deadline,
            "writers to start",
        )
        go.touch()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max(1.0, deadline - time.monotonic()))
        for proc in procs:
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
            report = json.loads(out)
            assert report == {"backend": EXPECTED_BACKEND, "errors": []}
        time.sleep(0.2)
        stop.touch()
        out, err = reader.communicate(timeout=max(1.0, deadline - time.monotonic()))
        assert reader.returncode == 0, err.decode(errors="replace")
        seen = json.loads(out)
    finally:
        for proc in [*procs, reader]:
            if proc.poll() is None:
                proc.kill()
    assert errors == []
    assert not any(t.is_alive() for t in threads)

    total = (procs_n + 1) * threads_n * per_thread
    data = path.read_bytes()
    text = data.decode("utf-8")  # strict: a torn multi-byte character fails here
    assert text.endswith("\n") and "\r" not in text
    lines = data.split(b"\n")[:-1]
    assert len(lines) == total
    objs = [json.loads(line) for line in lines]  # no torn or interleaved line
    assert [o["seq"] for o in objs] == list(range(1, total + 1))
    writers = {(f"P{k}", t) for k in range(procs_n) for t in range(threads_n)}
    writers |= {("main", t) for t in range(threads_n)}
    for p, t in writers:
        mine = [o for o in objs if (o["data"]["p"], o["data"]["t"]) == (p, t)]
        assert [o["data"]["i"] for o in mine] == list(range(per_thread)), (p, t)
        for o in mine:
            assert o["data"]["text"] == TEXT_U * _mixed_size(p, t, o["data"]["i"])
            assert o["task"] == p
    # a batch is written whole: its 3 events are consecutive lines with one time stamp
    for n, o in enumerate(objs):
        i = o["data"]["i"]
        if i % 7 == 3 and i + 3 <= per_thread:
            batch = objs[n : n + 3]
            key = (o["data"]["p"], o["data"]["t"])
            assert [(b["data"]["p"], b["data"]["t"]) for b in batch] == [key] * 3
            assert [b["data"]["i"] for b in batch] == [i, i + 1, i + 2]
            assert len({b["time"] for b in batch}) == 1
    # a lock-free reader running throughout saw only whole prefixes of the final log
    assert seen["errors"] == [] and seen["shrank"] == 0
    assert seen["prefix_changed"] == 0 and seen["max"] == total
    assert len(EventLog(path, lock).read(strict=True).events) == total
    _assert_free(lock)


# The lock itself, across processes and threads: a counter in shared memory, read and written back inside the
# lock with a pause between (a lost update means two holders), plus an "inside" flag. Thread 0 of each process
# uses a 4 ms timeout, so `LockTimeout` paths (dropping the turn lock while waiting) run all the time too.
LOCKER = r"""
import json, sys, threading, time
from multiprocessing import shared_memory
from pathlib import Path
from ultron.store._filelock import FileLock, LockTimeout

lock, name, ready, go, threads, rounds = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
shm = shared_memory.SharedMemory(name=name, track=False)
buf = shm.buf
totals = {"acquired": 0, "timeouts": 0, "violations": 0}
mutex = threading.Lock()

def work(t):
    timeout = 0.004 if t == 0 else 60.0
    got = timeouts = bad = 0
    for _ in range(rounds):
        try:
            with FileLock(lock, timeout=timeout):
                if buf[0]:
                    bad += 1
                buf[0] = 1
                n = int.from_bytes(buf[1:9], "little")
                time.sleep(0.0002)
                buf[1:9] = (n + 1).to_bytes(8, "little")
                buf[0] = 0
                got += 1
        except LockTimeout:
            timeouts += 1
    with mutex:
        totals["acquired"] += got
        totals["timeouts"] += timeouts
        totals["violations"] += bad

ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
ts = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()
del buf
shm.close()
print(json.dumps(totals))
"""


def test_reopen_lock_exclusion_no_lost_update_with_timeouts_mixed_in(
    tmp_path: Path,
) -> None:
    procs_n, threads_n, rounds = 6, 4, 40
    lock = tmp_path / "locks" / "R1.events.lock"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{k}" for k in range(procs_n)]
    shm = shared_memory.SharedMemory(create=True, size=16)
    try:
        shm.buf[:16] = bytes(16)
        procs = [
            _spawn(
                LOCKER,
                str(lock),
                shm.name,
                str(ready[k]),
                str(go),
                str(threads_n),
                str(rounds),
            )
            for k in range(procs_n)
        ]
        deadline = time.monotonic() + 120
        totals = {"acquired": 0, "timeouts": 0, "violations": 0}
        try:
            _wait(lambda: all(r.exists() for r in ready), deadline, "lockers to start")
            go.touch()
            for proc in procs:
                out, err = proc.communicate(
                    timeout=max(1.0, deadline - time.monotonic())
                )
                assert proc.returncode == 0, err.decode(errors="replace")
                for k, v in json.loads(out).items():
                    totals[k] += v
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
        counter = int.from_bytes(shm.buf[1:9], "little")
    finally:
        shm.close()
        shm.unlink()
    print(f"lock exclusion: {totals}, counter {counter}", flush=True)
    assert totals["violations"] == 0
    assert counter == totals["acquired"], "a lost update: two holders at once"
    assert totals["acquired"] + totals["timeouts"] == procs_n * threads_n * rounds
    waiters = procs_n * (threads_n - 1) * rounds  # threads with a 60 s timeout
    assert totals["acquired"] >= waiters, "a 60 s waiter timed out"
    _assert_free(lock)


# --- reopen: a holder or a waiter killed while holding the lock or the turn lock ---------------------------------

# Appends once, then hangs inside the lock on its second append: after the line is written but before `fsync`
# ("fsync"), or after half the line ("write"). Killed there by the test.
HANGING_WRITER = r"""
import os, sys, time
from pathlib import Path
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

path, lock, where, inside, text = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), bytes.fromhex(sys.argv[5]).decode("utf-8")
log = EventLog(path, lock)
log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"i": 1}))

def hang(*args):
    inside.touch()
    time.sleep(120)

if where == "fsync":
    os.fsync = hang
else:
    real_write = os.write
    def half(fd, data):
        real_write(fd, bytes(data[: len(data) // 2]))
        hang()
    os.write = half
log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"i": 2, "text": text}))
"""

QUEUED_APPENDER = r"""
import sys
from pathlib import Path
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent
path, lock, ready = sys.argv[1], sys.argv[2], Path(sys.argv[3])
log = EventLog(path, lock, lock_timeout=60)
ready.touch()
log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"i": 99}))
"""


@pytest.mark.parametrize("where", ["fsync", "write"])
def test_reopen_holder_killed_mid_append_frees_the_lock_and_the_turn(
    tmp_path: Path, where: str
) -> None:
    """The holder hangs inside `append`, a waiter process holds the turn lock, and a thread waits behind it. The
    turn holder is killed, then the holder. The thread must go on promptly, and nothing may stay held. (The turn
    holder dies first so it can't take the lock between the two kills.)"""
    path = tmp_path / "wörk" / "events.jsonl"
    lock = tmp_path / ".ultron" / "locks" / "R1.events.lock"
    inside, queued = tmp_path / "inside", tmp_path / "queued"
    text = TEXT_U * 50
    hanger = _spawn(
        HANGING_WRITER, str(path), str(lock), where, str(inside), text.encode().hex()
    )
    waiter: subprocess.Popen[bytes] | None = None
    result: dict[str, Any] = {}

    def append_third() -> None:
        try:
            result["append"] = EventLog(path, lock, lock_timeout=30).append(_new(3))
        except BaseException as exc:  # noqa: BLE001 - checked below
            result["append"] = exc

    thread = threading.Thread(target=append_third)
    deadline = time.monotonic() + DEADLINE_S
    try:
        _wait(inside.exists, deadline, "the writer to hang inside the lock")
        assert _held(lock)
        waiter = _spawn(QUEUED_APPENDER, str(path), str(lock), str(queued))
        _wait(queued.exists, deadline, "the queued writer to start")
        _wait(lambda: _held(_turn(lock)), deadline, "the queued writer's turn lock")
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive() and "append" not in result
        _kill(waiter)  # it held the turn lock
        time.sleep(0.3)
        assert thread.is_alive() and "append" not in result  # the lock is still held
        killed = time.monotonic()
        hanger.kill()
        thread.join(timeout=DEADLINE_S)
        took = time.monotonic() - killed
    finally:
        _kill(hanger)
        if waiter is not None:
            _kill(waiter)
        thread.join(timeout=DEADLINE_S)
    assert not thread.is_alive()
    assert took < 5.0, f"the thread waited {took:.1f} s after the holder died"
    if where == "fsync":
        # the dead holder's line was written whole: the thread's event follows it
        assert [e.seq for e in result["append"]] == [3], result["append"]
        events = EventLog(path, lock).read(strict=True).events
        assert [(e.seq, e.data["i"]) for e in events] == [(1, 1), (2, 2), (3, 3)]
        assert events[1].data["text"] == text
    else:
        assert isinstance(result["append"], TruncatedLogError), result["append"]
        cut = EventLog(path, lock, lock_timeout=5).repair()
        assert cut and not cut.endswith(b"\n")
        assert [e.seq for e in EventLog(path, lock).append(_new(3))] == [2]
        assert [e.data["i"] for e in EventLog(path).read(strict=True).events] == [1, 3]
    _assert_free(lock)


TURN_WAITER = r"""
import sys, time
from pathlib import Path
from ultron.store._filelock import FileLock
lock, record, ready, tag = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
ready.touch()
with FileLock(lock, timeout=60):
    with record.open("a", encoding="utf-8") as f:
        f.write(tag + "\n")
    time.sleep(0.01)
"""


def test_reopen_waiters_killed_while_holding_the_turn_lock_do_not_block_the_rest(
    tmp_path: Path,
) -> None:
    """While this process holds the lock: w1 queues alone (so it holds the turn lock) and is killed; w2 then holds
    it alone and is killed with 8 more queued behind it. The 8 must all get the lock once it is released."""
    lock = tmp_path / "locks" / "R1.events.lock"
    turn = _turn(lock)
    record = tmp_path / "record"
    procs: dict[str, subprocess.Popen[bytes]] = {}
    deadline = time.monotonic() + 90

    def start(tag: str) -> None:
        ready = tmp_path / f"ready-{tag}"
        procs[tag] = _spawn(TURN_WAITER, str(lock), str(record), str(ready), tag)
        _wait(ready.exists, deadline, f"{tag} to start")

    holder = FileLock(lock)
    holder.acquire()
    try:
        start("w1")
        _wait(lambda: _held(turn), deadline, "w1 to hold the turn lock")
        start("w2")
        time.sleep(0.3)
        _kill(procs.pop("w1"))
        _wait(lambda: _held(turn), deadline, "w2 to hold the turn lock")
        for k in range(3, 11):
            start(f"w{k}")
        time.sleep(0.3)
        _kill(procs.pop("w2"))  # the turn holder, with 8 waiters behind it
        _wait(lambda: _held(turn), deadline, "another waiter to take the turn lock")
        assert not record.exists(), "a waiter got the lock while it was held"
        released = time.monotonic()
        holder.release()
        for tag, proc in procs.items():
            _, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, (tag, err.decode(errors="replace"))
        took = time.monotonic() - released
    finally:
        holder.release()
        for proc in procs.values():
            _kill(proc)
    assert sorted(record.read_text(encoding="utf-8").split()) == sorted(procs)
    assert took < 15.0, f"8 waiters took {took:.1f} s after the release"
    _assert_free(lock)


CHURNER = r"""
import sys, time
from pathlib import Path
from ultron.store._filelock import FileLock
lock, ready, go, rounds = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
for _ in range(rounds):
    with FileLock(lock, timeout=60):
        time.sleep(0.002)
print("done")
"""


def test_reopen_many_waiters_with_random_kills_never_deadlock(tmp_path: Path) -> None:
    """12 processes take the lock 40 times each; 4 are killed at random moments (holding the lock, the turn lock,
    or neither). The other 8 must finish, and both locks must end free."""
    n, rounds = 12, 40
    lock = tmp_path / "locks" / "R1.events.lock"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{k}" for k in range(n)]
    procs = [
        _spawn(CHURNER, str(lock), str(ready[k]), str(go), str(rounds))
        for k in range(n)
    ]
    rng = random.Random(0x7505)
    victims = rng.sample(range(n), 4)
    deadline = time.monotonic() + 120
    try:
        _wait(lambda: all(r.exists() for r in ready), deadline, "churners to start")
        go.touch()
        for k in victims:
            time.sleep(rng.uniform(0.05, 0.4))
            procs[k].kill()
        for k, proc in enumerate(procs):
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            if k not in victims:
                assert proc.returncode == 0, err.decode(errors="replace")
                assert out.strip() == b"done"
    finally:
        for proc in procs:
            _kill(proc)
    _assert_free(lock)
    with FileLock(lock, timeout=1.0):
        pass


# --- reopen: the kept read handle -----------------------------------------------------------------------------

APPENDER = r"""
import sys
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent
path, lock = sys.argv[1], sys.argv[2]
log = EventLog(path, lock)
for line in sys.stdin:
    k, size = map(int, line.split())
    out = log.append([NewEvent(actor="engine", type="probe", requirement="R1",
                               data={"by": "child", "pad": "é" * size}) for _ in range(k)])
    print(out[-1].seq, flush=True)
"""


def test_reopen_kept_reader_sees_every_append_by_another_process(
    tmp_path: Path,
) -> None:
    """Two long-lived objects in two processes, alternating; some lines are longer than the 64 KiB tail chunk."""
    path, lock = tmp_path / "events.jsonl", tmp_path / "locks" / "e.lock"
    mine = EventLog(path, lock)
    child = subprocess.Popen(
        [sys.executable, "-c", APPENDER, str(path), str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert child.stdin is not None and child.stdout is not None
    rng = random.Random(5)
    total = 0
    try:
        for r in range(40):
            (mine_event,) = mine.append(_new(r))
            total += 1
            assert mine_event.seq == total, f"round {r}: stale tail"
            k, size = rng.randint(1, 4), rng.choice([0, 10, 40_000, 70_000])
            child.stdin.write(f"{k} {size}\n")
            child.stdin.flush()
            line = child.stdout.readline()
            assert line, child.stderr.read() if child.stderr else ""
            total += k
            assert int(line) == total, f"round {r}: the child's tail was stale"
    finally:
        if child.stdin:
            child.stdin.close()
        child.wait(timeout=30)
        for stream in (child.stdout, child.stderr):
            if stream:
                stream.close()
    assert [e.seq for e in mine.read(strict=True).events] == list(range(1, total + 1))


REWRITE = r"""
import sys
from pathlib import Path
path, how, data = Path(sys.argv[1]), sys.argv[2], bytes.fromhex(sys.argv[3])
if how == "truncate":  # in place: the same file, made shorter
    with path.open("wb") as f:
        f.write(data)
elif how == "overwrite":  # in place: the same file and size, other bytes
    with path.open("r+b") as f:
        f.write(data)
else:  # delete and write a new file, as git does
    path.unlink()
    path.write_bytes(data)
"""


def _rewrite(path: Path, how: str, data: bytes) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", REWRITE, str(path), how, data.hex()],
        check=False,
        capture_output=True,
        timeout=DEADLINE_S,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")


def test_reopen_kept_reader_never_returns_stale_data(tmp_path: Path) -> None:
    """Another process changes the log between appends in every way it can; each next `seq` follows the file as it
    is now, never what the kept handle saw before."""
    path, lock = tmp_path / "events.jsonl", tmp_path / "locks" / "e.lock"
    log = EventLog(path, lock)
    log.append([_new(i) for i in range(1, 6)])
    assert [e.seq for e in log.append(_new(6))] == [6]  # the reader is open now
    ino = os.stat(path).st_ino

    _rewrite(path, "truncate", _line(1) + _line(2))  # shorter, same file
    assert os.stat(path).st_ino == ino
    assert [e.seq for e in log.append(_new(7))] == [3]

    now = path.read_bytes()
    lines = now.split(b"\n")[:-1]
    swapped = b"".join(x + b"\n" for x in lines[:-1]) + lines[-1].replace(
        b'"seq":3,', b'"seq":8,', 1
    )
    assert len(swapped) + 1 == len(now)
    _rewrite(path, "overwrite", swapped + b"\n")  # same size, last seq 3 -> 8
    assert os.stat(path).st_ino == ino and path.stat().st_size == len(now)
    assert [e.seq for e in log.append(_new(9))] == [9]

    _rewrite(path, "recreate", _line(1) + _line(2) + _line(30))  # a new file
    assert [e.seq for e in log.append(_new(31))] == [31]
    _rewrite(path, "recreate", _line(1))  # and again, shorter than the old one
    assert [e.seq for e in log.append(_new(2))] == [2]
    assert path.read_bytes().startswith(_line(1))
    path.unlink()  # gone: a new log
    assert [e.seq for e in log.append(_new(1))] == [1]


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=T05",
            "-c",
            "user.email=t05@example.com",
            "-c",
            "core.autocrlf=false",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=60,
    )
    err = proc.stderr.decode(errors="replace")
    assert proc.returncode == 0, f"git {args}: {err}"
    assert "unlink" not in err.lower() and "unable" not in err.lower(), err


def test_reopen_kept_reader_follows_git_rewriting_the_log(tmp_path: Path) -> None:
    """Real git replaces the log (checkout, stash, reset, branch switch, rm) while an `EventLog` keeps its reader
    open; git must not be refused, and every next `seq` must follow the file git left."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    path = repo / "sdlc" / "requirements" / "R1" / "events.jsonl"
    log = EventLog(path, tmp_path / "locks" / "R1.events.lock")
    log.append([_new(1), _new(2)])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    two = path.read_bytes()

    # the reader is open on this file from here on
    assert [e.seq for e in log.append(_new(3))] == [3]
    _git(repo, "checkout", "--", ".")
    assert path.read_bytes() == two
    assert [e.seq for e in log.append(_new(4))] == [3]
    _git(repo, "stash", "-q")
    assert path.read_bytes() == two
    assert [e.seq for e in log.append(_new(5))] == [3]
    _git(repo, "reset", "-q", "--hard")
    assert path.read_bytes() == two

    _git(repo, "checkout", "-q", "-b", "other")
    assert [e.seq for e in log.append([_new(6), _new(7)])] == [3, 4]
    _git(repo, "commit", "-q", "-am", "four")
    four = path.read_bytes()
    _git(repo, "checkout", "-q", "main")
    assert path.read_bytes() == two
    assert [e.seq for e in log.append(_new(8))] == [3]
    _git(repo, "checkout", "-q", "-f", "other")
    assert path.read_bytes() == four
    assert [e.seq for e in log.append(_new(9))] == [5]
    assert [e.data["i"] for e in log.read(strict=True).events] == [1, 2, 6, 7, 9]

    _git(repo, "rm", "-q", "-r", "-f", "sdlc")
    assert not path.exists()
    assert [e.seq for e in log.append(_new(10))] == [1]  # a new log
    assert [e.seq for e in log.append(_new(11))] == [2]  # the reader is open on it
    shutil.rmtree(repo / "sdlc")  # a worktree cleaned up while the reader is open
    assert not (repo / "sdlc").exists()
    assert [e.seq for e in log.append(_new(12))] == [1]


RESET_WRITER = r"""
import sys, time
from pathlib import Path
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent
path, lock, ready, stop, w = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
log = EventLog(path, lock, lock_timeout=60)
ready.touch()
i = 0
while not stop.exists():
    log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"w": w, "i": i}))
    i += 1
print(i)
"""


def _remove_with_retry(path: Path, retries: list[int]) -> None:
    """Like git on Windows: retry a delete refused for a moment (an antivirus scan holding the new file)."""
    for attempt in range(200):
        try:
            path.unlink()
            return
        except PermissionError:
            retries[0] += 1
            if attempt == 199:
                raise
            time.sleep(0.01)


def test_reopen_kept_readers_follow_resets_under_the_lock(tmp_path: Path) -> None:
    """3 writer processes append in a loop, each with its kept reader, while this process takes the lock 25 times
    and replaces the log with its first half (delete, then write a new file). A writer reading a stale handle
    would continue the old file's `seq`, leaving a gap or a repeat the strict read below would reject."""
    path = tmp_path / "sdlc" / "requirements" / "R1" / "events.jsonl"
    lock = tmp_path / ".ultron" / "locks" / "R1.events.lock"
    stop = tmp_path / "stop"
    ready = [tmp_path / f"ready-{w}" for w in range(3)]
    procs = [
        _spawn(RESET_WRITER, str(path), str(lock), str(ready[w]), str(stop), str(w))
        for w in range(3)
    ]
    rng = random.Random(25)
    retries = [0]
    kept: list[int] = []
    deadline = time.monotonic() + 120
    try:
        _wait(lambda: all(r.exists() for r in ready), deadline, "writers to start")
        _wait(
            lambda: path.exists() and path.stat().st_size > 2000,
            deadline,
            "a first few appends",
        )
        for _ in range(25):
            time.sleep(rng.uniform(0.02, 0.08))
            with FileLock(lock, timeout=30):
                lines = path.read_bytes().split(b"\n")[:-1]
                # every append since the last reset followed the file as reset, not a stale handle
                seqs = [json.loads(line)["seq"] for line in lines]
                assert seqs == list(range(1, len(lines) + 1)), seqs[-20:]
                keep = lines[: len(lines) // 2]
                _remove_with_retry(path, retries)
                with path.open("xb") as f:
                    f.write(b"".join(line + b"\n" for line in keep))
                kept.append(len(keep))
        time.sleep(0.1)
        stop.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
    finally:
        stop.touch()
        for proc in procs:
            _kill(proc)
    print(f"resets kept {kept} lines; delete retries {retries[0]}", flush=True)
    events = EventLog(path, lock).read(strict=True).events  # seq 1..N, no gap
    assert len(events) > kept[-1]
    for w in range(3):
        mine = [e.data["i"] for e in events if e.data["w"] == str(w)]
        assert mine == sorted(mine) and len(set(mine)) == len(mine)


def test_reopen_os_replace_matches_the_documented_risk(tmp_path: Path) -> None:
    """Plan risk 1 (revised): on Windows an atomic `os.replace` onto the log is refused while a live `EventLog`
    has appended to it; delete-then-write still works; once the object is gone, so is its handle. On Linux the
    replace works and the next append follows the new file."""
    path, lock = tmp_path / "events.jsonl", tmp_path / "locks" / "e.lock"
    older = tmp_path / "older.jsonl"
    log = EventLog(path, lock)
    idle = EventLog(path, lock)  # never appends: holds nothing
    log.append([_new(1), _new(2)])
    assert [e.seq for e in log.append(_new(3))] == [3]  # the reader is open now
    before = path.read_bytes()
    older.write_bytes(_line(1))
    if sys.platform == "win32":
        with pytest.raises(PermissionError):
            os.replace(older, path)
        assert path.read_bytes() == before and older.exists()
        path.unlink()  # delete, then write: works with the handle open
        path.write_bytes(_line(1))
        assert [e.seq for e in log.append(_new(4))] == [2]
        # refused again: the reader now follows the new file
        with pytest.raises(PermissionError):
            os.replace(older, path)
        del log
        gc.collect()
        os.replace(older, path)  # no live object holds a handle: `idle` never appended
        assert [e.seq for e in idle.append(_new(5))] == [2]
    else:
        os.replace(older, path)
        assert [e.seq for e in log.append(_new(4))] == [2]
        assert [e.seq for e in idle.append(_new(5))] == [3]


LEAK_PROBE = r"""
import gc, json, os, subprocess, sys
from pathlib import Path
from ultron.store._filelock import FileLock, LockTimeout
from ultron.store.eventlog import EventLog, TruncatedLogError
from ultron.store.events import NewEvent

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    def handles():
        n = wintypes.DWORD()
        assert k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
        return n.value
else:
    def handles():
        return len(os.listdir("/proc/self/fd"))

def new(i):
    return NewEvent(actor="engine", type="probe", requirement="R1", data={"i": i})

path, lock, bad = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
for _ in range(3):
    EventLog(path, lock).append(new(0))  # warm up imports and caches
gc.collect()
out = {"base": handles()}
for i in range(500):
    EventLog(path, lock).append(new(i))
gc.collect()
out["after_500_dropped"] = handles()
kept = [EventLog(path, lock) for _ in range(200)]
for i, log in enumerate(kept):
    log.append(new(i))
    log.append(new(i))
out["with_200_alive"] = handles()
del kept, log
gc.collect()
out["after_200_released"] = handles()
for i in range(100):
    log = EventLog(path, lock)
    log.me = log  # a reference cycle: only the garbage collector frees it
    log.append(new(i))
del log
gc.collect()
out["after_100_cycles"] = handles()
bad.write_bytes(b'{"seq":1')
for i in range(100):
    try:
        EventLog(bad, lock).append(new(i))
    except TruncatedLogError:
        pass
gc.collect()
out["after_100_truncated"] = handles()
held = FileLock(lock)
held.acquire()
for i in range(20):
    try:
        EventLog(path, lock, lock_timeout=0.01).append(new(i))
    except LockTimeout:
        pass
held.release()
gc.collect()
out["after_20_timeouts"] = handles()
# a child started while an object holds a reader must not inherit it
log = EventLog(path, lock)
log.append(new(1))
log.append(new(2))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], close_fds=False)
del log
gc.collect()
spare = path.with_name("spare.jsonl")
spare.write_bytes(path.read_bytes())
os.replace(spare, path)  # refused on Windows if any handle on the log were left, here or in the child
out["replace_ok"] = True
if sys.platform != "win32":
    out["child_fds_on_log"] = sum(
        1 for fd in os.listdir(f"/proc/{child.pid}/fd")
        if os.path.realpath(f"/proc/{child.pid}/fd/{fd}").endswith("events.jsonl"))
child.kill()
child.wait()
print(json.dumps(out))
"""


def test_reopen_no_handle_leaks_after_many_event_logs(tmp_path: Path) -> None:
    path, lock = tmp_path / "events.jsonl", tmp_path / "locks" / "e.lock"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            LEAK_PROBE,
            str(path),
            str(lock),
            str(tmp_path / "bad.jsonl"),
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    out = json.loads(proc.stdout)
    print(f"handle counts: {out}", flush=True)
    base, slack = out["base"], 10
    # each live object that has appended holds exactly one handle: the count is real and per object
    assert base + 200 - slack <= out["with_200_alive"] <= base + 200 + slack
    for key in (
        "after_500_dropped",
        "after_200_released",
        "after_100_cycles",
        "after_100_truncated",
        "after_20_timeouts",
    ):
        assert out[key] <= base + slack, (key, out)
    assert out["replace_ok"] is True
    assert out.get("child_fds_on_log", 0) == 0


# --- reopen: the Linux branch still uses `fcntl.flock` (checked statically, and run against a stand-in) ---------

FILELOCK_SRC = ROOT / "src" / "ultron" / "store" / "_filelock.py"
EVENTLOG_SRC = ROOT / "src" / "ultron" / "store" / "eventlog.py"


def _platform_branches(src: Path) -> tuple[str, str]:
    tree = ast.parse(src.read_text(encoding="utf-8"))
    ifs = [
        n
        for n in tree.body
        if isinstance(n, ast.If) and ast.unparse(n.test) == "sys.platform == 'win32'"
    ]
    assert len(ifs) == 1, f"{src.name}: one module-level platform switch"
    return (
        "\n".join(ast.unparse(n) for n in ifs[0].body),
        "\n".join(ast.unparse(n) for n in ifs[0].orelse),
    )


def test_reopen_linux_branch_still_uses_fcntl_flock_statically() -> None:
    win, linux = _platform_branches(FILELOCK_SRC)
    assert "import msvcrt" in win and "msvcrt.LK_NBLCK" in win
    assert "LOCK_BACKEND: Final[str] = 'fcntl'" in linux
    assert "import fcntl" in linux
    assert "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" in linux
    assert "fcntl.flock(fd, fcntl.LOCK_UN)" in linux
    assert "msvcrt" not in linux and "_winapi" not in linux
    # both the lock and the turn lock go through the backend's two functions only
    tree = ast.parse(FILELOCK_SRC.read_text(encoding="utf-8"))
    (cls,) = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FileLock"
    ]
    body = ast.unparse(cls)
    assert "msvcrt" not in body and "fcntl" not in body
    assert "_try_lock(" in body and "_unlock(turn)" in body and "_unlock(fd)" in body
    # the kept reader: Windows opens it with delete sharing, Linux with a plain os.open
    win_e, linux_e = _platform_branches(EVENTLOG_SRC)
    assert "_winapi.CreateFile" in win_e and "_SHARE_ALL = 1 | 2 | 4" in win_e
    assert "os.open(path, os.O_RDONLY)" in linux_e and "_winapi" not in linux_e
    module = ast.parse(EVENTLOG_SRC.read_text(encoding="utf-8"))
    top_imports = [
        alias.name
        for n in module.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    ]
    assert "_winapi" not in top_imports and "msvcrt" not in top_imports


class _FakeFcntl(types.ModuleType):
    """`fcntl.flock` semantics in one process: one holder per file, `LOCK_NB` raises `BlockingIOError`."""

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self) -> None:
        super().__init__("fcntl")
        self.ops: list[int] = []
        self.owner: dict[tuple[int, int], int] = {}
        self.mutex = threading.Lock()

    def flock(self, fd: int, op: int) -> None:
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        with self.mutex:
            self.ops.append(op)
            if op == self.LOCK_EX | self.LOCK_NB:
                if self.owner.get(key, fd) != fd:
                    raise BlockingIOError
                self.owner[key] = fd
            elif op == self.LOCK_UN:
                assert self.owner.pop(key) == fd, "unlocked by a handle not holding it"
            else:
                raise AssertionError(f"unexpected flock op {op}")


def test_reopen_linux_branch_runs_with_flock_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module's Linux branch, executed here against a stand-in `fcntl`: threads contend through the lock and
    the turn lock; only `LOCK_EX | LOCK_NB` and `LOCK_UN` are used, nothing is left held, and no update is lost."""
    fake = _FakeFcntl()
    spec = importlib.util.spec_from_file_location("_filelock_linux", FILELOCK_SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patched:  # only while the module is imported
        patched.setattr(sys, "platform", "linux")
        patched.setitem(sys.modules, "fcntl", fake)
        spec.loader.exec_module(module)
    assert module.LOCK_BACKEND == "fcntl" and module.fcntl is fake
    linux_lock = module.FileLock
    lock = tmp_path / "locks" / "R1.events.lock"
    state = {"n": 0, "inside": 0, "bad": 0, "timeouts": 0}

    def work(t: int) -> None:
        for _ in range(40):
            try:
                with linux_lock(lock, timeout=0.003 if t == 0 else 30.0):
                    state["inside"] += 1
                    if state["inside"] != 1:
                        state["bad"] += 1
                    n = state["n"]
                    time.sleep(0.0002)
                    state["n"] = n + 1
                    state["inside"] -= 1
            except module.LockTimeout:
                state["timeouts"] += 1

    threads = [threading.Thread(target=work, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert state["bad"] == 0
    assert state["n"] + state["timeouts"] == 6 * 40
    assert set(fake.ops) == {fake.LOCK_EX | fake.LOCK_NB, fake.LOCK_UN}
    assert fake.owner == {}, "a lock or turn lock was left held"
    assert (lock.parent / "R1.events.turn.lock").exists()


def test_reopen_mypy_checks_the_linux_branch(tmp_path: Path) -> None:
    """Windows mypy only checks the win32 branch; ask for Linux explicitly (strict, from pyproject.toml)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--platform",
            "linux",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            "src/ultron/store/_filelock.py",
            "src/ultron/store/eventlog.py",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=300,
    )
    out = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, out
    assert "Success: no issues found" in out

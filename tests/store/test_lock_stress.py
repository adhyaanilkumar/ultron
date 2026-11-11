"""Reopen (lock starvation): concurrent writers must share the event log's lock fairly and keep its hold short.

Found on Windows under load: 4 writers × 50 appends sometimes took 27-50 s instead of ~3 s. Two causes, one test each:
- Inside the lock, `append` reopened the log to read its last line. On Windows the first read-open after another
  process's write is scanned by the antivirus (measured 9-60 ms, ~350 ms on a busy machine), so every append paid
  it while every other writer waited. Now each `EventLog` opens one reader before the lock and keeps it:
  `test_append_never_opens_the_log_for_reading_inside_the_lock`, and the tests after it for a log that is
  created, deleted or replaced meanwhile.
- The polling lock let the writer that just released take it straight back, before the waiting writers polled
  again, so one writer could hold it 37 times in a row while another waited ~30 s.
  `test_writers_take_turns_under_load` runs 6 writers × 100 appends in tight loops with every CPU busy. It bounds
  each writer's longest single wait in `FileLock.acquire` (starvation) and checks, from the log's own order, how
  often the lock changed hands while all six were asking for it (barging). Calibrated on the reopen machine (24
  CPUs, 24 busy processes): with the fix, waits of 0.05-0.6 s and a hand-off on 95-100% of appends; before it,
  waits of 1.1-10 s and hand-offs on 11-42%. Barging is rarer with the fix, not impossible, hence a ratio.
  Stabilised after the tester saw it fail 2 of 14 runs: a writer that started before the log existed skipped the
  (scanned) read-open and did all its appends while the others were still opening the log. Now the log exists
  first, each writer warms up (one append, which opens and keeps its reader) before the start, and hand-offs are
  counted only from when the last writer first asked for the lock.
"""

import builtins
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ultron.store import eventlog
from ultron.store._filelock import FileLock
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

DEADLINE_S = 110  # a guard against hangs, not a speed limit: the run takes 1-2 s with the fix, ~10 s without
WRITERS = 6
PER_WRITER = 100
MAX_LOCK_WAIT_S = 2.0  # the longest single wait in `FileLock.acquire`, for any writer
# of consecutive appends while all six are asking for the lock, the share made by different writers
MIN_HANDOFF_RATIO = 0.6
WORST_APPEND_S = 15.0  # the slowest `append`, far below the 30 s lock timeout


def handoff_ratio(writers: list[str], start: int = 0) -> float:
    """How often consecutive appends come from different writers, from index `start` (all writers are asking
    for the lock) up to the first writer's last append. 0.0 if that window has no pair."""
    last = {w: i for i, w in enumerate(writers)}
    cut = min(last.values()) + 1
    pairs = range(max(start, 0) + 1, cut)
    if not pairs:
        return 0.0
    return sum(writers[i] != writers[i - 1] for i in pairs) / len(pairs)


def test_handoff_ratio_counts_only_while_all_write() -> None:
    assert handoff_ratio(list("ababab") + ["b"] * 20) == 1.0
    # `a` finished first, before any hand-off
    assert handoff_ratio(list("aaabbb")) == 0.0
    assert handoff_ratio(list("aabab") + ["b"] * 9) == pytest.approx(2 / 3)
    assert handoff_ratio(list("a")) == 0.0
    # from `start` on: the run of `a` before `b` asked for the lock is not counted
    assert handoff_ratio(list("aaaabab") + ["b"] * 5, start=3) == 1.0
    assert handoff_ratio(list("aaab"), start=3) == 0.0  # `a` was done before `b` asked


def _new(i: int, **data: Any) -> NewEvent:
    return NewEvent(
        actor="engine", type="probe", requirement="R1", data={"i": i, **data}
    )


def _same_file(a: object, b: Path) -> bool:
    if not isinstance(a, (str, os.PathLike)):
        return False  # an fd or similar
    path = os.fspath(a)
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(b))


def test_append_never_opens_the_log_for_reading_inside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, lock = tmp_path / "events.jsonl", tmp_path / "locks" / "e.lock"
    EventLog(path, lock).append(
        _new(0)
    )  # the log exists: each append must find its last seq
    log = EventLog(path, lock)  # a fresh object, holding no handle yet
    held = [False]
    inside: list[str] = []
    readers: list[bool] = []  # one entry per reader opened: was the lock held?

    real_open_reader = eventlog._open_reader

    def open_reader(p: Path) -> int | None:
        readers.append(held[0])
        return real_open_reader(p)

    real_acquire, real_release = FileLock.acquire, FileLock.release

    def acquire(self: FileLock) -> None:
        real_acquire(self)
        held[0] = True

    def release(self: FileLock) -> None:
        held[0] = False
        real_release(self)

    real_os_open = os.open

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        access = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if held[0] and access != os.O_WRONLY and _same_file(path, log.path):
            inside.append(f"os.open(flags={flags:#x})")
        return real_os_open(path, flags, *args, **kwargs)

    def wrap_open(real: Callable[..., Any], name: str) -> Callable[..., Any]:
        def opener(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if held[0] and ("r" in mode or "+" in mode) and _same_file(file, log.path):
                inside.append(f"{name}(mode={mode!r})")
            return real(file, mode, *args, **kwargs)

        return opener

    monkeypatch.setattr(FileLock, "acquire", acquire)
    monkeypatch.setattr(FileLock, "release", release)
    monkeypatch.setattr(eventlog, "_open_reader", open_reader)
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(io, "open", wrap_open(io.open, "io.open"))
    monkeypatch.setattr(builtins, "open", wrap_open(builtins.open, "open"))

    for i in range(1, 6):
        assert [e.seq for e in log.append(_new(i))] == [i + 1]
    log.append([_new(6), _new(7)])
    monkeypatch.undo()

    assert inside == [], "the log was opened for reading while the lock was held"
    assert readers == [False], "one reader, opened before the lock and kept"
    assert [e.seq for e in log.read(strict=True).events] == list(range(1, 9))


def test_the_kept_reader_follows_a_log_deleted_and_recreated(tmp_path: Path) -> None:
    """Git replaces a file by deleting it and writing a new one; a kept handle must neither block that nor be
    read instead of the new file."""
    path, lock = tmp_path / "events.jsonl", tmp_path / "e.lock"
    log = EventLog(path, lock)
    log.append([_new(1), _new(2), _new(3)])  # `log` now holds a reader on this file
    first_line = path.read_bytes().split(b"\n", 1)[0] + b"\n"
    path.unlink()  # on Windows this needs the reader's delete sharing
    path.write_bytes(first_line)
    assert [e.seq for e in log.append(_new(4))] == [2]
    assert [e.data["i"] for e in log.read(strict=True).events] == [1, 4]
    path.unlink()
    assert [e.seq for e in log.append(_new(5))] == [
        1
    ]  # deleted and not recreated: a new log


def test_append_reads_a_log_created_after_it_started_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log is missing when `append` starts, and another writer creates it before this one gets the lock."""
    path, lock = tmp_path / "events.jsonl", tmp_path / "e.lock"
    real_acquire = FileLock.acquire
    first = [True]

    def acquire(self: FileLock) -> None:
        if first[0]:
            first[0] = False
            EventLog(path, lock).append([_new(1), _new(2)])  # sneaks in before us
        real_acquire(self)

    monkeypatch.setattr(FileLock, "acquire", acquire)
    assert [e.seq for e in EventLog(path, lock).append(_new(3))] == [3]
    monkeypatch.undo()
    assert [e.data["i"] for e in EventLog(path).read(strict=True).events] == [1, 2, 3]


def test_append_rereads_a_log_replaced_after_it_started_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log is replaced (as a reset to an older copy would) while `append` waits: seq follows the new file.

    On Windows an atomic replace onto a file that is open fails, even with delete sharing, so the kept read handle
    makes it fail (plan risk 1), and the append continues the file it has.
    """
    path, lock = tmp_path / "events.jsonl", tmp_path / "e.lock"
    log = EventLog(path, lock)
    log.append([_new(1), _new(2), _new(3)])
    older = tmp_path / "older.jsonl"
    older.write_bytes(path.read_bytes().split(b"\n", 1)[0] + b"\n")  # just seq 1
    real_acquire = FileLock.acquire
    replaced: list[str] = []

    def acquire(self: FileLock) -> None:
        if not replaced:
            try:
                os.replace(older, path)
                replaced.append("replaced")
            except PermissionError:
                replaced.append("refused")
        real_acquire(self)

    monkeypatch.setattr(FileLock, "acquire", acquire)
    (written,) = log.append(_new(4))
    monkeypatch.undo()
    events = log.read(strict=True).events
    if replaced == ["replaced"]:
        assert written.seq == 2 and [e.data["i"] for e in events] == [1, 4]
    else:
        assert replaced == ["refused"] and sys.platform == "win32"
        assert written.seq == 4 and [e.data["i"] for e in events] == [1, 2, 3, 4]


# --- stress: many writers in tight loops, with the CPU busy ----------------------------------------------------

# Each writer reports its longest wait inside `FileLock.acquire` (the lock's fairness, without the time spent
# opening the log) and its slowest `append` overall.
STRESS_WRITER = r"""
import json, sys, time
from pathlib import Path
from ultron.store._filelock import FileLock
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

path, lock, ready, go, w, n = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], int(sys.argv[6])
waits = []
acquire = FileLock.acquire
def timed_acquire(self):
    started = time.monotonic()
    try:
        acquire(self)
    finally:
        waits.append(time.monotonic() - started)
FileLock.acquire = timed_acquire
log = EventLog(path, lock, lock_timeout=float(sys.argv[7]))
# Warm-up before the start: this object opens (and keeps) its read handle now, so no writer pays that scanned
# open after the start while another, which skipped it, races ahead.
log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"w": w, "i": -1, "t": 0.0}))
waits.clear()
ready.touch()
end = time.monotonic() + 40
while not go.exists():
    if time.monotonic() > end:
        sys.exit(3)
    time.sleep(0.001)
worst = 0.0
for i in range(n):
    started = time.monotonic()  # when this writer asks for the lock; the clock is system-wide
    log.append(NewEvent(actor="engine", type="probe", requirement="R1", data={"w": w, "i": i, "t": started}))
    worst = max(worst, time.monotonic() - started)
print(json.dumps({"worst_s": worst, "wait_s": max(waits)}))
"""

BURNER = """
import sys, time
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    pass
"""


def _spawn(code: str, *args: str) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_writers_take_turns_under_load(tmp_path: Path) -> None:
    base = tmp_path / "wörk dír 日本"
    path = base / "sdlc" / "requirements" / "R1" / "events.jsonl"
    lock = base / ".ultron" / "locks" / "R1.events.lock"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{w}" for w in range(WRITERS)]
    deadline = time.monotonic() + DEADLINE_S
    # The log exists before any writer starts, so every writer's warm-up opens (and keeps) a reader.
    EventLog(path, lock).append(
        NewEvent(
            actor="engine", type="probe", requirement="R1", data={"w": "seed", "i": -1}
        )
    )
    burners = [_spawn(BURNER, str(DEADLINE_S)) for _ in range(os.cpu_count() or 2)]
    procs = [
        _spawn(
            STRESS_WRITER,
            str(path),
            str(lock),
            str(ready[w]),
            str(go),
            str(w),
            str(PER_WRITER),
            "30",
        )
        for w in range(WRITERS)
    ]
    reports: list[dict[str, float]] = []
    try:
        while not all(p.exists() for p in ready):
            assert time.monotonic() < deadline, "writers did not start in time"
            time.sleep(0.005)
        go.touch()
        for proc in procs:
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
            reports.append(json.loads(out))
    finally:
        for proc in [*procs, *burners]:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    events = EventLog(path, lock).read(strict=True).events
    total = 1 + WRITERS + WRITERS * PER_WRITER  # seed, warm-ups, measured appends
    assert [e.seq for e in events] == list(range(1, total + 1))
    for w in range(WRITERS):
        assert [e.data["i"] for e in events if e.data["w"] == str(w)] == [
            -1,
            *range(PER_WRITER),
        ]
    measured = [e for e in events if e.data["i"] >= 0]
    # Count from the moment the last writer asked for the lock for the first time (entering `acquire`, not
    # getting it): a writer that started late is not barging, a writer kept waiting is.
    all_asking = max(e.data["t"] for e in measured if e.data["i"] == 0)
    start = next(j for j, e in enumerate(measured) if e.data["t"] >= all_asking)
    ratio = handoff_ratio([str(e.data["w"]) for e in measured], start)
    waits = [round(r["wait_s"], 3) for r in reports]
    print(
        f"hand-off ratio {ratio:.2f} (from append {start}), "
        f"longest lock wait per writer {waits} s"
    )
    assert ratio >= MIN_HANDOFF_RATIO, (
        f"the lock changed hands on only {ratio:.0%} of appends (barging)"
    )
    assert max(waits) <= MAX_LOCK_WAIT_S, (
        f"a writer waited too long for the lock: {waits} s"
    )
    slowest = max(r["worst_s"] for r in reports)
    assert slowest <= WORST_APPEND_S, f"slowest append took {slowest:.1f} s"

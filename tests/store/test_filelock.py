"""The cross-process lock behind the event log (AC2, AC6).

AC6 asks for the lock tests to pass in CI on both Windows (msvcrt) and Linux (fcntl). A green run only proves the
lock it exercised, so `test_lock_backend_matches_os` names it, and the last test makes sure no T05 test is skipped.

Reopen (lock starvation): a writer that releases the lock and asks again straight away must not get it back while
another writer is already waiting. A polling lock let the releaser win almost every time (barging), so one writer
could wait for the whole run. `test_a_waiting_*` pin the order: holder, then the waiter, then the holder again.
"""

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ultron.store._filelock import LOCK_BACKEND, FileLock, LockTimeout

EXPECTED_BACKEND = "msvcrt" if sys.platform == "win32" else "fcntl"
ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 60

TRY_LOCK = """
import sys
from ultron.store._filelock import FileLock, LockTimeout
try:
    FileLock(sys.argv[1], timeout=float(sys.argv[2])).acquire()
except LockTimeout:
    sys.exit(2)
"""

HOLD_LOCK = """
import sys, time
from pathlib import Path
from ultron.store._filelock import FileLock
FileLock(sys.argv[1]).acquire()
Path(sys.argv[2]).touch()
time.sleep(120)
"""


def _try_in_child(lock: Path, timeout: float) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", TRY_LOCK, str(lock), str(timeout)],
        check=False,
        capture_output=True,
        timeout=TIMEOUT_S,
    )
    assert proc.returncode in (0, 2), proc.stderr.decode(errors="replace")
    return proc.returncode


def test_lock_backend_matches_os() -> None:
    assert LOCK_BACKEND == EXPECTED_BACKEND


def test_lock_timeout_is_a_timeout_error() -> None:
    assert issubclass(LockTimeout, TimeoutError)


def test_second_process_cannot_take_a_held_lock(tmp_path: Path) -> None:
    lock = tmp_path / "events.jsonl.lock"
    with FileLock(lock):
        assert _try_in_child(lock, 0.3) == 2
    assert _try_in_child(lock, 5.0) == 0


def test_lock_is_freed_when_its_holder_is_killed(tmp_path: Path) -> None:
    lock = tmp_path / "events.jsonl.lock"
    held = tmp_path / "held"
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(lock), str(held)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + TIMEOUT_S
        while not held.exists():
            assert proc.poll() is None, proc.communicate()[1].decode(errors="replace")
            assert time.monotonic() < deadline, (
                "the holder did not take the lock in time"
            )
            time.sleep(0.01)
        with pytest.raises(LockTimeout):
            FileLock(lock, timeout=0.2).acquire()
    finally:
        proc.kill()
        proc.communicate(timeout=TIMEOUT_S)
    with FileLock(lock, timeout=10.0):
        pass


def test_two_locks_in_one_process_exclude_each_other(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    first = FileLock(lock)
    first.acquire()
    second = FileLock(lock, timeout=0.1)
    try:
        with pytest.raises(LockTimeout):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_double_acquire_raises_and_release_is_idempotent(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock")
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()
    lock.release()
    with FileLock(tmp_path / "x.lock", timeout=0.5):
        pass


def test_context_manager_releases_on_error(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "x.lock"
    with pytest.raises(ValueError), FileLock(path):
        raise ValueError("boom")
    assert path.exists()
    with FileLock(path, timeout=0.5):
        pass


# --- reopen: a waiter goes before the releaser takes the lock back (no barging) ---------------------------------

ROUNDS = 10
QUEUED_S = 0.3  # time for the waiter to start waiting; it needs a few milliseconds

WAITER = """
import sys
from pathlib import Path
from ultron.store._filelock import FileLock
lock, record, ready = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
ready.touch()
with FileLock(lock, timeout=30):
    with record.open("a", encoding="utf-8") as f:
        f.write("B\\n")
"""


def _note(record: Path, tag: str) -> None:
    with record.open("a", encoding="utf-8") as f:
        f.write(tag + "\n")


def test_a_waiting_thread_goes_before_the_releaser_takes_the_lock_back(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "x.lock"
    for n in range(ROUNDS):
        order: list[str] = []
        holder = FileLock(lock)
        holder.acquire()
        order.append("A1")
        started = threading.Event()

        def wait_for_it(
            order: list[str] = order, started: threading.Event = started
        ) -> None:
            started.set()
            with FileLock(lock, timeout=30):
                order.append("B")

        waiter = threading.Thread(target=wait_for_it)
        waiter.start()
        try:
            assert started.wait(TIMEOUT_S)
            time.sleep(QUEUED_S)
            holder.release()
            with FileLock(lock, timeout=30):  # straight back, like a writer in a loop
                order.append("A2")
        finally:
            holder.release()
            waiter.join(TIMEOUT_S)
        assert order == ["A1", "B", "A2"], f"round {n}: {order}"


def test_a_waiting_process_goes_before_the_releaser_takes_the_lock_back(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "x.lock"
    for n in range(3):
        record, ready = tmp_path / f"order-{n}", tmp_path / f"ready-{n}"
        holder = FileLock(lock)
        holder.acquire()
        _note(record, "A1")
        proc = subprocess.Popen(
            [sys.executable, "-c", WAITER, str(lock), str(record), str(ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + TIMEOUT_S
            while not ready.exists():
                assert proc.poll() is None, proc.communicate()[1].decode(
                    errors="replace"
                )
                assert time.monotonic() < deadline, "the waiter did not start in time"
                time.sleep(0.005)
            time.sleep(QUEUED_S)
            holder.release()
            with FileLock(lock, timeout=30):
                _note(record, "A2")
            _, err = proc.communicate(timeout=TIMEOUT_S)
            assert proc.returncode == 0, err.decode(errors="replace")
        finally:
            holder.release()
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=TIMEOUT_S)
        assert record.read_text(encoding="utf-8").split() == ["A1", "B", "A2"], (
            f"round {n}"
        )


def test_turn_lock_sits_beside_the_lock(tmp_path: Path) -> None:
    locks = tmp_path / ".ultron" / "locks"
    with FileLock(locks / "R1.events.lock") as held:
        assert held.turn_path == locks / "R1.events.turn.lock"
    assert FileLock(tmp_path / "events.jsonl.lock").turn_path == (
        tmp_path / "events.jsonl.turn.lock"
    )
    assert FileLock(tmp_path / "noext").turn_path == tmp_path / "noext.turn"
    assert sorted(p.name for p in locks.iterdir()) == [
        "R1.events.lock",
        "R1.events.turn.lock",
    ]


def test_a_held_turn_lock_times_out_without_taking_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    # holding `lock`'s turn lock: someone else is next in line
    queued = FileLock(FileLock(lock).turn_path)
    queued.acquire()
    try:
        with pytest.raises(LockTimeout):
            FileLock(lock, timeout=0.2).acquire()
    finally:
        queued.release()
    # nothing was left held: the lock and its turn are both free again
    assert _try_in_child(lock, 5.0) == 0
    with FileLock(lock, timeout=0.5):
        pass


# A skipped test is green in CI but proves nothing on that OS (AC6).
_SKIP = re.compile(
    r"\b(?:pytest|unittest)\s*\.\s*(?:mark\s*\.\s*)?(?:skip|skipif|xfail|importorskip)\b"
)


def test_no_t05_test_is_skipped_on_any_os() -> None:
    files = sorted((ROOT / "tests" / "store").glob("test_*.py"))
    files.append(ROOT / "tests" / "acceptance" / "test_t05.py")
    found = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in files
        if path.exists()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _SKIP.search(line)
    ]
    assert len(files) >= 4
    assert found == []

"""The cross-process lock behind the event log (AC2, AC6).

AC6 asks for the lock tests to pass in CI on both Windows (msvcrt) and Linux (fcntl). A green run only proves the
lock it exercised, so `test_lock_backend_matches_os` names it, and the last test makes sure no T05 test is skipped.
"""

import re
import subprocess
import sys
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

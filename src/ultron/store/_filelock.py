"""An exclusive, cross-process lock on a sidecar file, handed to waiters in turn.

Windows uses `msvcrt.locking` on the first byte; its blocking mode only retries once a second and gives up after
about 10 seconds, so both platforms poll a non-blocking lock until an overall timeout. Linux uses `fcntl.flock`. A
lock is held by an open file handle, so it is released if the process dies. `LOCK_BACKEND` names the one in use
(`"msvcrt"` or `"fcntl"`), so a test run can show which lock it proved.

A plain polling lock is unfair: a holder that releases and asks again gets the lock back before the sleeping waiters
poll (barging), so one waiter can wait out its whole timeout while others loop. So `acquire` first takes a second
lock, the **turn lock** on a sibling file (`x.lock` -> `x.turn.lock`), and holds it only while it waits for the main
lock: whoever holds the turn lock goes next. A releaser that asks again has to get the turn lock too, which a waiter
usually holds by then. Waiters poll the turn lock at jittered intervals so they don't poll in lockstep; the one next
in line polls the main lock faster. Only the main lock gives mutual exclusion; the turn lock only decides who is next.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Final, Self

__all__ = ["LOCK_BACKEND", "FileLock", "LockTimeout"]

_O_BINARY: int = getattr(os, "O_BINARY", 0)


class LockTimeout(TimeoutError):
    """The lock was not acquired before the timeout."""


if sys.platform == "win32":
    import msvcrt

    LOCK_BACKEND: Final[str] = "msvcrt"

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    LOCK_BACKEND: Final[str] = "fcntl"

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)


class FileLock:
    """`with FileLock(path):` holds an exclusive lock on `path` (created if missing) for the block.

    `poll` is how often a waiter checks whether it is next (jittered by ±50%); the waiter that is next checks the
    lock itself four times as often. `turn_path` is the turn lock's file, created beside `path`.
    """

    def __init__(
        self, path: Path | str, *, timeout: float = 30.0, poll: float = 0.002
    ) -> None:
        self.path = Path(path)
        self.turn_path = self.path.with_name(
            self.path.stem + ".turn" + self.path.suffix
        )
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError(f"lock {self.path} is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        fd = _open(self.path)
        try:
            turn = _open(self.turn_path)
            try:
                self._wait(turn, deadline, self.poll, jitter=True)  # next in line
                try:
                    self._wait(fd, deadline, self.poll / 4, jitter=False)
                finally:
                    _unlock(turn)
            finally:
                os.close(turn)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def _wait(self, fd: int, deadline: float, interval: float, *, jitter: bool) -> None:
        while not _try_lock(fd):
            left = deadline - time.monotonic()
            if left <= 0:
                raise LockTimeout(f"could not lock {self.path} within {self.timeout} s")
            pause = interval * random.uniform(0.5, 1.5) if jitter else interval
            time.sleep(min(pause, left))

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

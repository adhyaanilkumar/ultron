"""An exclusive, cross-process lock on a sidecar file.

Windows uses `msvcrt.locking` on the first byte; its blocking mode gives up after about 10 seconds, so both
platforms poll a non-blocking lock until an overall timeout. Linux uses `fcntl.flock`. The lock is held by an open
file handle, so it is released if the process dies.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Self

_O_BINARY: int = getattr(os, "O_BINARY", 0)


class LockTimeout(TimeoutError):
    """The lock was not acquired before the timeout."""


if sys.platform == "win32":
    import msvcrt

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

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """`with FileLock(path):` holds an exclusive lock on `path` (created if missing) for the block."""

    def __init__(
        self, path: Path | str, *, timeout: float = 30.0, poll: float = 0.005
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError(f"lock {self.path} is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)
        deadline = time.monotonic() + self.timeout
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not lock {self.path} within {self.timeout} s"
                    )
                time.sleep(self.poll)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

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

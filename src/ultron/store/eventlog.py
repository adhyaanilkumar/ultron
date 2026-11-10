"""The append-only `events.jsonl` writer and its versioned reader (spec §10).

Writers in several processes (the runner and the MCP server) are serialised by a sidecar file lock, so `seq`
stays unique and gap-free. A line, once written, is never rewritten. Readers don't take the lock.

A crash mid-write can leave a last line with no `\\n`. `read()` reports it, `append()` refuses to write until
`repair()` cuts it off, and `repair()` returns the bytes it cut so the caller can log them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ultron import __version__
from ultron.store._filelock import FileLock, LockTimeout
from ultron.store.events import (
    SCHEMA_VERSION,
    Event,
    EventError,
    NewerSchemaError,
    NewEvent,
    from_json,
    to_json_line,
)

__all__ = [
    "CorruptLogError",
    "EventLog",
    "EventLogError",
    "LockTimeout",
    "NewerSchemaError",
    "ReadResult",
    "TruncatedLogError",
    "events_path",
    "format_time",
]

_O_BINARY: int = getattr(os, "O_BINARY", 0)
_REQUIREMENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TAIL_CHUNK = 64 * 1024


class EventLogError(Exception):
    """The event log can't be read or written as it is."""


class TruncatedLogError(EventLogError):
    """The last line has no `\\n` (a crash mid-write). Run `repair()` before appending."""


class CorruptLogError(EventLogError):
    """A complete line is not a valid event, or `seq` is out of order."""

    def __init__(self, path: Path, line_no: int | None, reason: str) -> None:
        where = f"{path}:{line_no}" if line_no is not None else str(path)
        super().__init__(f"{where}: {reason}")
        self.path = path
        self.line_no = line_no
        self.reason = reason


@dataclass(frozen=True)
class ReadResult:
    """The events read, and the partial last line (without `\\n`) if a write was cut off, else `None`."""

    events: list[Event] = field(default_factory=list)
    truncated_tail: bytes | None = None

    @property
    def truncated(self) -> bool:
        return self.truncated_tail is not None


def events_path(root: Path | str, requirement_id: str) -> Path:
    """`<root>/sdlc/requirements/<requirement_id>/events.jsonl`."""
    if not _REQUIREMENT_ID_RE.fullmatch(requirement_id) or ".." in requirement_id:
        raise ValueError(f"not a valid requirement id: {requirement_id!r}")
    return Path(root) / "sdlc" / "requirements" / requirement_id / "events.jsonl"


def format_time(moment: datetime) -> str:
    """UTC ISO 8601 with milliseconds and `Z`, e.g. `2026-11-10T09:30:00.123Z`."""
    if moment.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventLog:
    """One requirement's `events.jsonl`.

    `lock_path` defaults to `<path>.lock` beside the log; the engine should pass a path under `.ultron/locks/` so
    the lock file is never committed.
    """

    def __init__(
        self,
        path: Path | str,
        lock_path: Path | str | None = None,
        *,
        lock_timeout: float = 30.0,
        clock: Callable[[], datetime] = _utc_now,
        ultron_version: str = __version__,
    ) -> None:
        self.path = Path(path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.path.with_name(self.path.name + ".lock")
        )
        self.lock_timeout = lock_timeout
        self._clock = clock
        self._ultron_version = ultron_version

    # --- writing ----------------------------------------------------------------------------------------------

    def append(self, new_events: NewEvent | Sequence[NewEvent]) -> list[Event]:
        """Stamp and append events in one write under the lock, and return them as written.

        Raises `TruncatedLogError` if the last line was cut off, `CorruptLogError` if it isn't a valid event, and
        `LockTimeout` if another writer holds the lock too long. Nothing is written if any event is invalid.
        """
        batch = [new_events] if isinstance(new_events, NewEvent) else list(new_events)
        for item in batch:
            if not isinstance(item, NewEvent):
                raise TypeError(
                    f"append() takes NewEvent objects, got {type(item).__name__}"
                )
        if not batch:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            last_seq = self._last_seq()
            stamp = format_time(self._clock())
            events = [
                Event(
                    schema=SCHEMA_VERSION,
                    seq=last_seq + i,
                    time=stamp,
                    actor=item.actor,
                    type=item.type,
                    requirement=item.requirement,
                    task=item.task,
                    attempt=item.attempt,
                    data=item.data,
                    ultron_version=self._ultron_version,
                )
                for i, item in enumerate(batch, start=1)
            ]
            payload = b"".join(to_json_line(event) for event in events)
            fd = os.open(
                self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        return events

    def _last_seq(self) -> int:
        """The `seq` of the last line, 0 for an empty or missing log. Reads back from the end only."""
        if not self.path.exists():  # stable: only lock holders create or change the log
            return 0
        with self.path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            if size == 0:
                return 0
            f.seek(size - 1)
            if f.read(1) != b"\n":
                raise TruncatedLogError(
                    f"{self.path}: the last line has no newline (a write was cut off); repair it"
                )
            # The last line is the bytes before the final newline, back to the newline before them.
            pos = size - 1
            buf = b""
            while True:
                step = min(_TAIL_CHUNK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                nl = buf.rfind(b"\n")
                if nl != -1 or pos == 0:
                    break
            line = buf[nl + 1 :]
        try:
            event = from_json(line)
        except NewerSchemaError:
            raise
        except EventError as exc:
            raise CorruptLogError(
                self.path, None, f"last line is not a valid event: {exc}"
            ) from exc
        return event.seq

    def repair(self) -> bytes:
        """Cut a partial last line off (back to the last `\\n`) and return the bytes cut; `b""` if none."""
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            try:
                fd = os.open(self.path, os.O_RDWR | _O_BINARY)
            except FileNotFoundError:
                return b""
            try:
                size = os.lseek(fd, 0, os.SEEK_END)
                if size == 0:
                    return b""
                os.lseek(fd, 0, os.SEEK_SET)
                data = b""
                while len(data) < size:
                    chunk = os.read(fd, size - len(data))
                    if not chunk:
                        break
                    data += chunk
                if data.endswith(b"\n"):
                    return b""
                keep = data.rfind(b"\n") + 1
                os.ftruncate(fd, keep)
                os.fsync(fd)
                return data[keep:]
            finally:
                os.close(fd)

    # --- reading ----------------------------------------------------------------------------------------------

    def read(self, *, strict: bool = False) -> ReadResult:
        """Read, upgrade and check every event: `seq` must run 1, 2, 3 ... with no gaps.

        A partial last line is reported in `truncated_tail` (or raises `TruncatedLogError` if `strict`). A bad
        complete line raises `CorruptLogError` with its line number; a newer schema raises `NewerSchemaError`.
        """
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return ReadResult()
        lines = data.split(b"\n")
        tail = lines.pop()  # b"" when the file ends with "\n"
        truncated_tail = tail if tail else None
        if truncated_tail is not None and strict:
            raise TruncatedLogError(
                f"{self.path}: the last line has no newline (a write was cut off); repair it"
            )
        events: list[Event] = []
        for line_no, raw in enumerate(lines, start=1):
            if not raw.strip():
                raise CorruptLogError(self.path, line_no, "blank line")
            try:
                event = from_json(raw)
            except NewerSchemaError:
                raise
            except EventError as exc:
                raise CorruptLogError(self.path, line_no, str(exc)) from exc
            if event.seq != line_no:
                raise CorruptLogError(
                    self.path, line_no, f"seq is {event.seq}, expected {line_no}"
                )
            events.append(event)
        return ReadResult(events=events, truncated_tail=truncated_tail)

"""`<ultron_dir>/activity.jsonl`: the local, uncommitted stream of what ultron agents do (spec section 9.1).

Every row carries a cursor `n`, an integer that only goes up. Writers (any process) take a file lock, give the
rows the next numbers and append them in one write, so file order is cursor order. The highest number ever given is
kept in `activity.seq`, so cursors stay valid after pruning, even when pruning empties the file. That mark only goes
up: every writer stores `max(stored mark, new value)`, and a missing or unreadable mark is recovered from the highest
`n` in the file. Readers take no lock and never write; a half-written last line is ignored.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ultron.engine.states import RequirementState, fold
from ultron.store._filelock import FileLock
from ultron.store.eventlog import EventLog, EventLogError, events_path

__all__ = ["append_activity", "prune", "read_activity"]

ACTIVITY_FILE = "activity.jsonl"
SEQ_FILE = "activity.seq"
RETENTION = timedelta(days=7)
_RETRIES = 150  # about 10 s of backoff in all
_FINISHED = {RequirementState.COMPLETE, RequirementState.PARTIAL}


def _lock(ultron_dir: Path) -> FileLock:
    return FileLock(ultron_dir / "locks" / "activity.lock", timeout=60.0)


def _is_cursor(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse(data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in data.split(b"\n")[:-1]:  # the part after the last newline is unfinished
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _is_cursor(row.get("n")):
            rows.append(row)
    return rows


def _read_bytes(path: Path) -> bytes:
    for attempt in range(_RETRIES):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError:  # replaced by a prune at this moment (Windows)
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_pause(attempt))
    return b""


def _pause(attempt: int) -> float:
    """Backoff for retries on a busy file: 10 ms growing to 100 ms."""
    return min(0.1, 0.01 * (attempt + 1))


def _replace(tmp: Path, target: Path) -> None:
    """`os.replace`, retried while a reader holds the target open (Windows denies the replace then)."""
    for attempt in range(_RETRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_pause(attempt))


def _mend_tail(path: Path) -> None:
    """Under the lock: end the file with a newline, so a writer that died mid-line cannot swallow the next row.

    The torn fragment becomes one garbage line, which readers skip."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return
    except OSError:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, b"\n")
    finally:
        os.close(fd)


def _stored_mark(ultron_dir: Path) -> int:
    """The mark kept in `activity.seq`; 0 when it is missing or unreadable (the file's rows then decide)."""
    text = _read_bytes(ultron_dir / SEQ_FILE).decode("ascii", "replace").strip()
    return int(text) if text.isdigit() else 0


def _high_water(
    ultron_dir: Path, rows: Sequence[Mapping[str, Any]] | None = None
) -> int:
    """The highest cursor ever given: the stored mark or the highest `n` in the file, whichever is higher."""
    if rows is None:
        rows = _parse(_read_bytes(ultron_dir / ACTIVITY_FILE))
    return max([_stored_mark(ultron_dir), *(int(r["n"]) for r in rows)])


def _raise_mark(ultron_dir: Path, value: int) -> None:
    """Under the lock: store `max(stored mark, value)`. The mark never goes down; it is replaced atomically, so a
    crash cannot leave it empty or half-written."""
    stored = _stored_mark(ultron_dir)
    mark = max(value, stored)
    target = ultron_dir / SEQ_FILE
    if mark == stored and target.exists():
        return
    tmp = ultron_dir / (SEQ_FILE + ".tmp")
    tmp.write_text(str(mark), encoding="ascii")
    try:
        _replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def append_activity(ultron_dir: Path | str, rows: Sequence[Mapping[str, Any]]) -> int:
    """Append rows, numbering them with the next cursors; return the last cursor given (0 if no rows)."""
    base = Path(ultron_dir)
    if not rows:
        return _high_water(base)
    base.mkdir(parents=True, exist_ok=True)
    with _lock(base):
        _mend_tail(base / ACTIVITY_FILE)
        n = _high_water(base)
        lines: list[str] = []
        for row in rows:
            n += 1
            lines.append(json.dumps({**row, "n": n}))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        fd = os.open(
            base / ACTIVITY_FILE,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o644,
        )
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
        _raise_mark(base, n)
    return n


def read_activity(ultron_dir: Path | str, since: int = 0) -> list[dict[str, Any]]:
    """Rows with a cursor above `since`, in order. Reads only."""
    rows = _parse(_read_bytes(Path(ultron_dir) / ACTIVITY_FILE))
    return [r for r in rows if int(r["n"]) > since]


def _completed_at(root: Path, requirement: str) -> datetime | None:
    try:
        events = EventLog(events_path(root, requirement)).read().events
        if not events or fold(events).requirement not in _FINISHED:
            return None
        return datetime.fromisoformat(events[-1].time)
    except (EventLogError, ValueError, OSError):
        return None


def prune(
    root: Path | str,
    ultron_dir: Path | str,
    *,
    now: datetime | None = None,
    retention: timedelta = RETENTION,
) -> int:
    """Drop activity of requirements completed more than `retention` ago; return how many rows went.

    Every prune first records the high-water mark (`activity.seq`, never lowered), even when nothing goes."""
    repo, base = Path(root), Path(ultron_dir)
    moment = now if now is not None else datetime.now(UTC)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / (ACTIVITY_FILE + ".tmp")
    with _lock(base):
        tmp.unlink(missing_ok=True)  # left by a prune that crashed before its replace
        rows = _parse(_read_bytes(base / ACTIVITY_FILE))
        # Before any row goes, the mark must cover every cursor given, including the rows removed now.
        _raise_mark(base, _high_water(base, rows))
        decided: dict[str, bool] = {}

        def expired(requirement: str) -> bool:
            if requirement not in decided:
                done = _completed_at(repo, requirement)
                decided[requirement] = done is not None and moment - done > retention
            return decided[requirement]

        keep = [r for r in rows if not expired(str(r.get("requirement", "")))]
        removed = len(rows) - len(keep)
        if removed:
            text = "".join(json.dumps(r) + "\n" for r in keep)
            tmp.write_bytes(text.encode("utf-8"))
            try:
                _replace(tmp, base / ACTIVITY_FILE)
            finally:
                tmp.unlink(missing_ok=True)
    return removed

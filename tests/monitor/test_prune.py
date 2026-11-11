"""AC4: pruning removes only activity of requirements completed more than 7 days ago; cursors stay valid."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from monitor_helpers import finished, in_progress
from monitor_helpers import gate as gate_ev

from ultron.engine.states import RequirementState, fold
from ultron.monitor import append_activity, follow, prune
from ultron.store import events as ev
from ultron.store.eventlog import EventLog, events_path

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
OLD = "R-20261101-aaaa"
EDGE = "R-20261105-dddd"
RECENT = "R-20261110-bbbb"
OPEN = "R-20261001-cccc"


def _row(req: str, text: str) -> dict[str, object]:
    return {
        "requirement": req,
        "task": "T1",
        "role": "developer",
        "kind": "read",
        "text": text,
        "data": {},
    }


def _setup(root: Path) -> Path:
    finished(root, OLD, NOW - timedelta(days=8))
    finished(root, EDGE, NOW - timedelta(days=6, hours=23))
    finished(root, RECENT, NOW - timedelta(days=1))
    in_progress(root, OPEN)  # old but never completed
    for req in (OLD, EDGE, RECENT, OPEN):
        state = fold(EventLog(events_path(root, req)).read().events).requirement
        assert (state is RequirementState.COMPLETE) == (req != OPEN)
    udir = root / ".ultron"
    for i in range(3):
        append_activity(
            udir,
            [_row(r, f"{r}-{i}") for r in (OLD, EDGE, RECENT, OPEN, "R-20260101-nolg")],
        )
    return udir


def test_prune_removes_only_old_completed(tmp_path: Path) -> None:
    udir = _setup(tmp_path)
    assert prune(tmp_path, udir, now=NOW) == 3
    left = follow(udir, 0)["activity"]
    assert {r["requirement"] for r in left} == {EDGE, RECENT, OPEN, "R-20260101-nolg"}
    assert len(left) == 12


def test_cursors_stay_valid_after_pruning(tmp_path: Path) -> None:
    udir = _setup(tmp_path)
    cursor = follow(udir, 0, limit=7)["cursor"]  # rows 1..7
    assert cursor == 7
    prune(tmp_path, udir, now=NOW)
    rest = follow(udir, cursor)["activity"]
    assert [r["n"] for r in rest] == [8, 9, 10, 12, 13, 14, 15]
    assert all(int(r["n"]) > cursor for r in rest)
    # new rows keep counting up from the old high-water mark
    assert append_activity(udir, [_row(RECENT, "later")]) == 16
    assert [r["n"] for r in follow(udir, 15)["activity"]] == [16]


def test_cursor_survives_pruning_everything(tmp_path: Path) -> None:
    finished(tmp_path, OLD, NOW - timedelta(days=30))
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(OLD, "a"), _row(OLD, "b")])
    assert prune(tmp_path, udir, now=NOW) == 2
    assert follow(udir, 0)["activity"] == []
    assert append_activity(udir, [_row(OLD, "c")]) == 3


def test_partial_requirements_are_pruned_like_complete_ones(tmp_path: Path) -> None:
    from monitor_helpers import n

    req = "R-20261101-pppp"
    log = EventLog(events_path(tmp_path, req), clock=lambda: NOW - timedelta(days=8))
    log.append(
        [
            n(req, "spec_started"),
            n(req, "spec_drafted"),
            gate_ev(req, "spec"),
            n(req, "decomposition_started"),
            n(req, "task_created", "T1", depends_on=[], max_attempts=3),
            n(req, "task_created", "T2", depends_on=[], max_attempts=3),
            n(req, "tasks_drafted"),
            gate_ev(req, "tasks"),
            n(req, "task_started", "T1"),
            n(req, "plan_drafted", "T1"),
            gate_ev(req, "plan", "T1"),
            n(req, "attempt_started", "T1", 1),
            n(req, "coding_finished", "T1", 1),
            n(req, "attempt_finished", "T1", 1, outcome="passed"),
            n(req, "result_requested", "T1"),
            gate_ev(req, "result", "T1"),
            n(req, "task_started", "T2"),
            ev.escalation(req, reason="out of attempts", task="T2"),
        ]
    )
    state = fold(log.read().events).requirement
    assert state is RequirementState.PARTIAL
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(req, "a"), _row(req, "b")])
    assert prune(tmp_path, udir, now=NOW) == 2
    assert follow(udir, 0)["activity"] == []


def test_prune_retries_the_replace_while_a_reader_holds_the_file(
    tmp_path: Path,
) -> None:
    finished(tmp_path, OLD, NOW - timedelta(days=30))
    in_progress(tmp_path, OPEN)
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(OLD, "a"), _row(OPEN, "b")])
    path = udir / "activity.jsonl"
    held = threading.Event()
    release = threading.Event()

    def reader() -> None:
        with path.open(
            "rb"
        ) as handle:  # no FILE_SHARE_DELETE on Windows: blocks the replace
            handle.read()
            held.set()
            release.wait(5)

    thread = threading.Thread(target=reader)
    thread.start()
    held.wait(5)
    threading.Timer(0.5, release.set).start()
    try:
        assert prune(tmp_path, udir, now=NOW) == 1
    finally:
        release.set()
        thread.join()
    assert [r["text"] for r in follow(udir, 0)["activity"]] == ["b"]


def test_prune_gives_up_after_a_bounded_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ultron.monitor import activity

    finished(tmp_path, OLD, NOW - timedelta(days=30))
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(OLD, "a")])
    calls: list[int] = []

    def always_denied(src: object, dst: object) -> None:
        calls.append(1)
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", always_denied)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        prune(tmp_path, udir, now=NOW)
    assert 2 < len(calls) < 1000, calls
    assert activity.ACTIVITY_FILE  # module still sane

"""AC4: pruning removes only activity of requirements completed more than 7 days ago; cursors stay valid."""

import os
import random
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


def test_a_second_prune_never_lowers_the_high_water_mark(tmp_path: Path) -> None:
    """Defect E: prune #1 drops the newest rows, prune #2 older ones; the mark must stay at the top cursor given."""
    finished(tmp_path, OLD, NOW - timedelta(days=30))
    finished(tmp_path, EDGE, NOW - timedelta(days=10))
    in_progress(tmp_path, OPEN)
    udir = tmp_path / ".ultron"
    append_activity(
        udir,
        [
            _row(OPEN, "a"),
            _row(EDGE, "e"),
            _row(OPEN, "b"),
            _row(OLD, "o1"),
            _row(OLD, "o2"),
        ],
    )
    follower = follow(udir, 0)["cursor"]
    assert follower == 5
    assert prune(tmp_path, udir, now=NOW, retention=timedelta(days=20)) == 2
    assert prune(tmp_path, udir, now=NOW) == 1
    assert (udir / "activity.seq").read_text(encoding="ascii").strip() == "5"
    assert append_activity(udir, [_row(OPEN, "after")]) == 6
    assert [r["text"] for r in follow(udir, follower)["activity"]] == ["after"]


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 11, 42, 99, 1234, 2026])
def test_cursors_only_go_up_over_random_appends_and_prunes(
    tmp_path: Path, seed: int
) -> None:
    """Property: whatever mix of appends and prunes, every new cursor is above all cursors ever given, the stored
    mark never drops below them, and a follower at the top sees nothing old again."""
    rnd = random.Random(seed)
    ages = {"R-20261101-b0b0": 40, "R-20261102-c0c0": 15, "R-20261103-d0d0": 9}
    for req, days in ages.items():
        finished(tmp_path, req, NOW - timedelta(days=days))
    in_progress(tmp_path, OPEN)
    names = [*ages, OPEN]
    udir = tmp_path / ".ultron"
    issued = 0
    for step in range(80):
        if rnd.random() < 0.6:
            batch = [
                _row(rnd.choice(names), f"{step}-{i}") for i in range(rnd.randint(1, 4))
            ]
            got = append_activity(udir, batch)
            assert got == issued + len(batch), (step, got, issued)
            issued = got
        else:
            prune(
                tmp_path,
                udir,
                now=NOW,
                retention=timedelta(days=rnd.choice([1, 5, 12, 20, 60])),
            )
            assert follow(udir, issued)["activity"] == []
            mark = int((udir / "activity.seq").read_text(encoding="ascii").strip() or 0)
            assert mark >= issued, (step, mark, issued)
        ns = [int(r["n"]) for r in follow(udir, 0)["activity"]]
        assert ns == sorted(set(ns)) and all(0 < x <= issued for x in ns)


def test_a_failed_prune_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished(tmp_path, OLD, NOW - timedelta(days=30))
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(OLD, "a"), _row(OLD, "b")])
    before = (udir / "activity.jsonl").read_bytes()

    real_replace = os.replace

    def deny_activity(src: object, dst: object) -> None:
        if str(dst).endswith("activity.jsonl"):
            raise PermissionError("denied")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", deny_activity)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        prune(tmp_path, udir, now=NOW)
    assert not (udir / "activity.jsonl.tmp").exists()
    assert (udir / "activity.jsonl").read_bytes() == before
    monkeypatch.undo()
    assert append_activity(udir, [_row(OPEN, "c")]) == 3


def test_a_stale_temp_file_from_a_crashed_prune_is_replaced(tmp_path: Path) -> None:
    finished(tmp_path, OLD, NOW - timedelta(days=30))
    udir = tmp_path / ".ultron"
    append_activity(udir, [_row(OLD, "a"), _row(OPEN, "b")])
    (udir / "activity.jsonl.tmp").write_bytes(b'{"n": 99, "text": "stale"}\n')
    assert prune(tmp_path, udir, now=NOW) == 1
    assert not (udir / "activity.jsonl.tmp").exists()
    assert [r["text"] for r in follow(udir, 0)["activity"]] == ["b"]


def test_prune_before_any_activity_records_a_zero_mark_and_clears_a_stale_temp(
    tmp_path: Path,
) -> None:
    udir = tmp_path / ".ultron"
    assert prune(tmp_path, udir, now=NOW) == 0
    assert (udir / "activity.seq").read_text(encoding="ascii").strip() == "0"
    (udir / "activity.jsonl.tmp").write_bytes(b"stale")
    in_progress(tmp_path, OPEN)
    append_activity(udir, [_row(OPEN, "a")])
    assert (
        prune(tmp_path, udir, now=NOW) == 0
    )  # nothing to remove, the stale temp still goes
    assert not (udir / "activity.jsonl.tmp").exists()
    assert append_activity(udir, [_row(OPEN, "b")]) == 2

"""AC4: pruning removes only activity of requirements completed more than 7 days ago; cursors stay valid."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from monitor_helpers import finished, in_progress

from ultron.engine.states import RequirementState, fold
from ultron.monitor import append_activity, follow, prune
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

"""AC1: following with `since` across 1,000 activities from 3 writer processes returns each exactly once, in order."""

import multiprocessing
from pathlib import Path

from ultron.monitor import append_activity, follow

PER_WRITER = [334, 333, 333]


def _writer(directory: str, writer: int, count: int) -> None:
    for i in range(count):
        append_activity(
            directory,
            [
                {
                    "requirement": "R",
                    "task": f"W{writer}",
                    "kind": "thinking",
                    "text": str(i),
                }
            ],
        )


def test_follow_returns_every_activity_once_in_order(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_writer, args=(str(tmp_path), w, c))
        for w, c in enumerate(PER_WRITER)
    ]
    for p in procs:
        p.start()
    seen: list[dict[str, object]] = []
    cursor = 0
    while any(p.is_alive() for p in procs):
        got = follow(tmp_path, cursor)
        seen.extend(got["activity"])
        cursor = got["cursor"]
    for p in procs:
        p.join()
        assert p.exitcode == 0
    got = follow(tmp_path, cursor)
    seen.extend(got["activity"])

    assert [r["n"] for r in seen] == list(range(1, 1001))
    for w, c in enumerate(PER_WRITER):
        texts = [int(str(r["text"])) for r in seen if r["task"] == f"W{w}"]
        assert texts == list(range(c))  # each writer's own order is kept


def test_nothing_new_keeps_the_cursor(tmp_path: Path) -> None:
    append_activity(tmp_path, [{"requirement": "R", "kind": "read", "text": "a"}])
    first = follow(tmp_path, 0)
    again = follow(tmp_path, first["cursor"])
    assert again["activity"] == [] and again["cursor"] == first["cursor"]
    assert again["untrusted"] is True


def test_partial_last_line_is_ignored(tmp_path: Path) -> None:
    append_activity(tmp_path, [{"requirement": "R", "kind": "read", "text": "a"}])
    with (tmp_path / "activity.jsonl").open("ab") as fh:
        fh.write(b'{"n": 2, "kind": "re')
    assert [r["n"] for r in follow(tmp_path, 0)["activity"]] == [1]

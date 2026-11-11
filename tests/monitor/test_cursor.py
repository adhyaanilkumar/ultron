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


def test_append_after_a_torn_tail_keeps_the_new_rows(tmp_path: Path) -> None:
    append_activity(tmp_path, [{"requirement": "R", "kind": "read", "text": "a"}])
    path = tmp_path / "activity.jsonl"
    path.write_bytes(
        path.read_bytes() + b'{"requirement": "R", "ki'
    )  # a writer died mid-line
    append_activity(
        tmp_path,
        [
            {"requirement": "R", "kind": "read", "text": "b"},
            {"requirement": "R", "kind": "read", "text": "c"},
        ],
    )
    got = follow(tmp_path, 0)["activity"]
    assert [r["text"] for r in got] == ["a", "b", "c"]
    assert [r["n"] for r in got] == [1, 2, 3]
    # a torn tail that is a whole JSON row without its newline is not numbered twice
    path.write_bytes(path.read_bytes() + b'{"requirement": "R", "n": 4, "text": "d"}')
    append_activity(tmp_path, [{"requirement": "R", "kind": "read", "text": "e"}])
    ns = [r["n"] for r in follow(tmp_path, 0)["activity"]]
    assert ns == sorted(set(ns))


def _rows(*texts: str) -> list[dict[str, object]]:
    return [{"requirement": "R", "kind": "read", "text": t} for t in texts]


def test_a_missing_mark_is_recovered_from_the_file(tmp_path: Path) -> None:
    append_activity(tmp_path, _rows("a", "b", "c"))
    (tmp_path / "activity.seq").unlink()
    assert append_activity(tmp_path, _rows("d")) == 4
    assert (tmp_path / "activity.seq").read_text(encoding="ascii").strip() == "4"


def test_a_corrupt_mark_is_recovered_from_the_file(tmp_path: Path) -> None:
    append_activity(tmp_path, _rows("a", "b"))
    for junk in ("", "abc", "-5", "1e3", "\x00\x00"):
        (tmp_path / "activity.seq").write_text(junk, encoding="latin-1")
        last = append_activity(tmp_path, _rows("x"))
        ns = [r["n"] for r in follow(tmp_path, 0)["activity"]]
        assert ns == list(range(1, last + 1)), (junk, ns)


def test_a_stale_mark_below_the_file_is_raised_and_a_higher_mark_is_kept(
    tmp_path: Path,
) -> None:
    append_activity(tmp_path, _rows("a", "b", "c"))
    (tmp_path / "activity.seq").write_text(
        "1", encoding="ascii"
    )  # a writer died before updating the mark
    assert append_activity(tmp_path, _rows("d")) == 4
    (tmp_path / "activity.seq").write_text(
        "50", encoding="ascii"
    )  # rows 5..50 were pruned away
    assert append_activity(tmp_path, _rows("e")) == 51


def test_a_boolean_n_is_not_a_cursor(tmp_path: Path) -> None:
    (tmp_path / "activity.jsonl").write_bytes(b'{"n": true, "text": "fake"}\n')
    assert follow(tmp_path, 0)["activity"] == []
    assert append_activity(tmp_path, _rows("a")) == 1


def test_no_temp_file_is_left_beside_the_mark(tmp_path: Path) -> None:
    append_activity(tmp_path, _rows("a"))
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == [
        "activity.jsonl",
        "activity.seq",
    ]

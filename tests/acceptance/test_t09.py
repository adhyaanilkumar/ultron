"""Acceptance tests for T09 (SQLite cache rebuilt from the event log), by the testing agent.

Written from the acceptance criteria and spec §10 "The cache", D10, D21 and §9.1, using real `EventLog` files (T05)
and T08's `fold`. Expected rows are written by hand here, not copied from the developer's tests.

- AC1 `test_ac1_*`: rebuilding gives the same rows as the incremental updates (interleaved requirements).
- AC2 `test_ac2_*`: deleted, garbage, empty, half-written and locked `state.db` give the same result.
- AC3 `test_ac3_*`: an append is seen by the next read (staleness by `seq`, not by mtime).
- AC4 `test_ac4_*`: every waiting-on-the-user kind across requirements; answered questions drop out.
- AC5 `test_ac5_*`: 1,000-event cold read under a second (the `ci` half is the same test on the CI runners).
- Extras: `test_x_*` (escalations, call_for_edit, unreadable logs, concurrency, only the cache writes `state.db`).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ultron.engine.states import fold
from ultron.store import events as ev
from ultron.store._filelock import FileLock, LockTimeout
from ultron.store.cache import Cache
from ultron.store.eventlog import EventLog, events_path

REPO_ROOT = Path(__file__).resolve().parents[2]


def log(root: Path, req: str) -> EventLog:
    return EventLog(events_path(root, req))


def n(req: str, type_: str, task: str | None = None, **data: Any) -> ev.NewEvent:
    return ev.NewEvent(
        actor="engine", type=type_, requirement=req, task=task, data=dict(data)
    )


def gate(req: str, name: str, task: str | None = None) -> ev.NewEvent:
    return ev.gate_decision(
        req, actor="auto", gate=name, decision="approve", channel="auto", task=task
    )


def to_tasks_gate(req: str) -> list[ev.NewEvent]:
    """Up to the tasks gate: the requirement waits on `tasks`."""
    return [
        n(req, "spec_started"),
        n(req, "spec_drafted"),
        gate(req, "spec"),
        n(req, "decomposition_started"),
        n(req, "task_created", "T1", depends_on=[], max_attempts=3),
        n(req, "task_created", "T2", depends_on=["T1"], max_attempts=2),
        n(req, "tasks_drafted"),
    ]


def to_in_progress(req: str) -> list[ev.NewEvent]:
    return [*to_tasks_gate(req), gate(req, "tasks"), n(req, "task_started", "T1")]


def to_result(req: str) -> list[ev.NewEvent]:
    return [
        *to_in_progress(req),
        n(req, "plan_drafted", "T1"),
        gate(req, "plan", "T1"),
        ev.attempt_started(req, "T1", 1),
        n(req, "coding_finished", "T1"),
        ev.attempt_finished(req, "T1", 1, outcome="passed"),
        n(req, "result_requested", "T1"),
    ]


def steps() -> list[tuple[str, list[ev.NewEvent]]]:
    a, b, c, d = "R-a", "R-b", "R-c", "R-d"
    return [
        (a, [n(a, "intake_started")]),
        (b, to_in_progress(b)),
        (a, [n(a, "intent_drafted")]),
        (c, to_tasks_gate(c)),
        (b, [n(b, "plan_drafted", "T1")]),
        (d, [n(d, "spec_started")]),
        (c, [gate(c, "tasks"), n(c, "task_started", "T1")]),
        (
            b,
            [
                n(b, "question_asked", "T1", id="Q1"),
                n(b, "agent_started", "T1", role="dev"),
            ],
        ),
        (d, [n(d, "spec_drafted")]),
        (
            c,
            [
                n(c, "plan_drafted", "T1"),
                gate(c, "plan", "T1"),
                ev.attempt_started(c, "T1", 1),
            ],
        ),
        (b, [n(b, "question_answered", "T1", id="Q1")]),
        (
            c,
            [n(c, "coding_finished", "T1"), n(c, "agent_started", "T1", role="tester")],
        ),
        (b, [ev.escalation(b, reason="stuck", task="T1")]),
        (c, [ev.escalation(c, reason="x", task="T1", ticket="AD-1")]),
        (b, [ev.escalation(b, reason="stuck", task="T1", ticket="AD-2")]),
        (d, [gate(d, "spec")]),
    ]


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in (root / "sdlc").rglob("events.jsonl")
    }


# --- AC1 --------------------------------------------------------------------------------------------------------


def test_ac1_rebuild_equals_incremental_after_every_batch(tmp_path: Path) -> None:
    live = Cache(tmp_path)
    for i, (req, batch) in enumerate(steps()):
        log(tmp_path, req).append(batch)
        if i % 3 == 1:
            continue  # several batches between refreshes
        live.refresh()
        rebuilt = Cache(tmp_path, db_path=tmp_path / f"fresh{i}.db")
        assert rebuilt.dump() == live.dump(), f"diverged after batch {i}"
    assert live.dump()["requirements"]


def test_ac1_rows_match_hand_written_expectations(tmp_path: Path) -> None:
    for req, batch in steps():
        log(tmp_path, req).append(batch)
    cache = Cache(tmp_path)
    reqs = {r.id: (r.state, r.error) for r in cache.requirements()}
    assert reqs["R-a"] == ("awaiting_intent", None)
    assert reqs["R-d"] == ("spec_approved", None)
    assert reqs["R-b"][0] == "escalated"
    t2 = next(t for t in cache.tasks("R-b") if t.task == "T2")
    assert (t2.max_attempts, t2.depends_on) == (2, ("T1",))
    # attempts "n/max": R-c T1 had attempt 1 of 3 when it was escalated
    c1 = next(x for x in cache.tasks("R-c") if x.task == "T1")
    assert (c1.attempt, c1.max_attempts) == (1, 3)
    assert {t.task for t in cache.tasks("R-b")} == {"T1", "T2"}
    assert all(t.requirement == "R-b" for t in cache.tasks("R-b"))


def test_ac1_rows_follow_fold(tmp_path: Path) -> None:
    for req, batch in steps():
        log(tmp_path, req).append(batch)
    cache = Cache(tmp_path)
    for req in ("R-a", "R-b", "R-c", "R-d"):
        events = log(tmp_path, req).read().events
        snap = fold(events)
        row = next(r for r in cache.requirements() if r.id == req)
        assert row.state == snap.requirement.value
        got = {t.task: (t.state, t.attempt, t.max_attempts) for t in cache.tasks(req)}
        want = {
            k: (v.state.value, v.attempt, v.max_attempts) for k, v in snap.tasks.items()
        }
        assert got == want
        assert row.last_seq == events[-1].seq


def test_ac1_rebuild_is_idempotent_and_repeat_reads_stable(tmp_path: Path) -> None:
    for req, batch in steps():
        log(tmp_path, req).append(batch)
    cache = Cache(tmp_path)
    first = cache.dump()
    assert cache.dump() == first
    cache.rebuild()
    assert cache.dump() == first
    cache.rebuild()
    assert cache.dump() == first


def test_ac1_empty_repo_has_empty_tables(tmp_path: Path) -> None:
    dump = Cache(tmp_path).dump()
    assert all(rows == [] for rows in dump.values())


# --- AC2 --------------------------------------------------------------------------------------------------------


def _populate(root: Path) -> dict[str, list[tuple[Any, ...]]]:
    for req, batch in steps():
        log(root, req).append(batch)
    return Cache(root).dump()


def test_ac2_deleted_db(tmp_path: Path) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    db.unlink()
    assert Cache(tmp_path).dump() == before
    assert db.exists()


@pytest.mark.parametrize(
    "junk",
    [b"garbage" * 1000, b"", b"\x00" * 4096, b"SQLite format 3\x00" + b"\xff" * 200],
    ids=["text", "empty", "zeros", "fake-header"],
)
def test_ac2_garbage_db(tmp_path: Path, junk: bytes) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    db.write_bytes(junk)
    assert Cache(tmp_path).dump() == before
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


@pytest.mark.parametrize("keep", [100, 512, 0.5, 0.9])
def test_ac2_half_written_db(tmp_path: Path, keep: float) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    data = db.read_bytes()
    cut = int(len(data) * keep) if isinstance(keep, float) else int(keep)
    db.write_bytes(data[:cut])
    assert Cache(tmp_path).dump() == before


def test_ac2_flipped_bytes_in_the_second_half(tmp_path: Path) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    data = bytearray(db.read_bytes())
    for i in range(len(data) // 2, len(data)):
        data[i] = 0xAB
    db.write_bytes(bytes(data))
    assert Cache(tmp_path).dump() == before


def test_ac2_wrong_schema_version(tmp_path: Path) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    assert Cache(tmp_path).dump() == before


def test_ac2_tampered_content_is_undone_by_rebuild(tmp_path: Path) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    con = sqlite3.connect(db)
    con.execute("UPDATE requirements SET state = 'complete'")
    con.commit()
    con.close()
    Cache(tmp_path).rebuild()
    assert Cache(tmp_path).dump() == before


def test_ac2_stale_journal_file_next_to_garbage_db(tmp_path: Path) -> None:
    before = _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    Path(str(db) + "-journal").write_bytes(b"junk journal" * 100)
    db.write_bytes(b"not a db")
    assert Cache(tmp_path).dump() == before


def test_ac2_sqlite_locked_db_is_waited_for_not_destroyed(tmp_path: Path) -> None:
    """Another connection holds the database briefly: the read waits, and the result is still right."""
    _populate(tmp_path)
    db = tmp_path / ".ultron" / "state.db"
    holder = sqlite3.connect(db, timeout=0, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    released = threading.Timer(0.5, holder.rollback)
    released.start()
    try:
        log(tmp_path, "R-a").append([n("R-a", "probe")])  # the cache has to write
        after = Cache(tmp_path).dump()
    finally:
        released.join()
        holder.close()
    want = Cache(tmp_path, db_path=tmp_path / "fresh.db").dump()
    assert after == want
    assert db.exists()


def test_ac2_held_file_lock_times_out_cleanly(tmp_path: Path) -> None:
    _populate(tmp_path)
    cache = Cache(tmp_path, lock_timeout=0.3)
    lock = tmp_path / ".ultron" / "locks" / "state.db.lock"
    with FileLock(lock, timeout=1), pytest.raises(LockTimeout):
        cache.dump()
    assert Cache(tmp_path).dump()["requirements"]


# --- AC3 --------------------------------------------------------------------------------------------------------


def test_ac3_append_seen_by_next_read(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    log(tmp_path, "R-1").append([n("R-1", "spec_started")])
    (row,) = cache.requirements()
    assert (row.state, row.last_seq) == ("spec_drafting", 1)
    assert cache.waiting() == []
    log(tmp_path, "R-1").append([n("R-1", "spec_drafted")])
    (row,) = cache.requirements()
    assert (row.state, row.last_seq) == ("awaiting_spec", 2)
    assert [(w.kind, w.since_seq) for w in cache.waiting()] == [("spec", 2)]
    assert Cache(tmp_path).requirements() == cache.requirements()


def test_ac3_append_seen_even_when_mtime_is_unchanged(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    path = events_path(tmp_path, "R-1")
    log(tmp_path, "R-1").append([n("R-1", "spec_started")])
    assert cache.requirements()[0].last_seq == 1
    stat = path.stat()
    log(tmp_path, "R-1").append([n("R-1", "spec_drafted")])
    os.utime(
        path, ns=(stat.st_atime_ns, stat.st_mtime_ns)
    )  # a coarse clock: same mtime
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    (row,) = cache.requirements()
    assert (row.state, row.last_seq) == ("awaiting_spec", 2)


def test_ac3_many_single_event_appends_each_seen(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append([n("R-1", "spec_started")])
    for i in range(25):
        lg.append([n("R-1", "probe", i=i)])
        assert cache.requirements()[0].last_seq == i + 2


def test_ac3_new_and_removed_requirements(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    log(tmp_path, "R-1").append([n("R-1", "spec_started")])
    assert [r.id for r in cache.requirements()] == ["R-1"]
    log(tmp_path, "R-2").append([n("R-2", "spec_started")])
    assert [r.id for r in cache.requirements()] == ["R-1", "R-2"]
    events_path(tmp_path, "R-1").unlink()
    assert [r.id for r in cache.requirements()] == ["R-2"]
    assert cache.waiting("R-1") == [] and cache.tasks("R-1") == []


def test_ac3_reads_never_modify_the_logs(tmp_path: Path) -> None:
    for req, batch in steps():
        log(tmp_path, req).append(batch)
    before = snapshot(tmp_path)
    cache = Cache(tmp_path)
    cache.dump()
    cache.rebuild()
    cache.waiting()
    assert snapshot(tmp_path) == before


# --- AC4 --------------------------------------------------------------------------------------------------------


def test_ac4_every_waiting_kind_across_requirements(tmp_path: Path) -> None:
    r1, r2, r3, r4, r5 = "R-1", "R-2", "R-3", "R-4", "R-5"
    log(tmp_path, r1).append([n(r1, "intake_started"), n(r1, "intent_drafted")])
    log(tmp_path, r2).append([n(r2, "spec_started"), n(r2, "spec_drafted")])
    log(tmp_path, r3).append(to_in_progress(r3) + [n(r3, "plan_drafted", "T1")])
    log(tmp_path, r4).append(to_result(r4))
    log(tmp_path, r5).append(
        [n(r5, "spec_started"), n(r5, "question_asked", id="Q9", text="which?")]
    )
    got = {(w.requirement, w.kind, w.task, w.ref) for w in Cache(tmp_path).waiting()}
    assert got == {
        (r1, "intent", None, None),
        (r2, "spec", None, None),
        (r3, "plan", "T1", None),
        (r4, "result", "T1", None),
        (r5, "question", None, "Q9"),
    }


def test_ac4_tasks_gate_waits(tmp_path: Path) -> None:
    log(tmp_path, "R-1").append(to_tasks_gate("R-1"))
    assert [(w.kind, w.task) for w in Cache(tmp_path).waiting()] == [("tasks", None)]


def test_ac4_items_drop_out_when_decided(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append([n("R-1", "spec_started"), n("R-1", "spec_drafted")])
    assert [w.kind for w in cache.waiting()] == ["spec"]
    lg.append([gate("R-1", "spec")])
    assert cache.waiting() == []
    lg.append(to_tasks_gate("R-1")[3:])
    assert [w.kind for w in cache.waiting()] == ["tasks"]
    lg.append(
        [
            gate("R-1", "tasks"),
            n("R-1", "task_started", "T1"),
            n("R-1", "plan_drafted", "T1"),
        ]
    )
    assert [(w.kind, w.task) for w in cache.waiting()] == [("plan", "T1")]
    lg.append([gate("R-1", "plan", "T1")])
    assert cache.waiting() == []


def test_ac4_answered_questions_drop_out_others_stay(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append(
        [
            n("R-1", "spec_started"),
            n("R-1", "question_asked", id="A"),
            n("R-1", "question_asked", id="B"),
            n("R-1", "question_asked", id="C"),
        ]
    )
    assert sorted(w.ref or "" for w in cache.waiting()) == ["A", "B", "C"]
    lg.append([n("R-1", "question_answered", id="B")])
    assert sorted(w.ref or "" for w in cache.waiting()) == ["A", "C"]
    lg.append(
        [n("R-1", "question_answered", id="A"), n("R-1", "question_answered", id="C")]
    )
    assert cache.waiting() == []


def test_ac4_same_question_id_in_two_requirements_is_independent(
    tmp_path: Path,
) -> None:
    for r in ("R-1", "R-2"):
        log(tmp_path, r).append([n(r, "spec_started"), n(r, "question_asked", id="Q1")])
    log(tmp_path, "R-1").append([n("R-1", "question_answered", id="Q1")])
    assert [(w.requirement, w.ref) for w in Cache(tmp_path).waiting()] == [
        ("R-2", "Q1")
    ]


def test_ac4_waiting_filter_by_requirement(tmp_path: Path) -> None:
    log(tmp_path, "R-1").append(
        [n("R-1", "intake_started"), n("R-1", "intent_drafted")]
    )
    log(tmp_path, "R-2").append([n("R-2", "spec_started"), n("R-2", "spec_drafted")])
    cache = Cache(tmp_path)
    assert [w.kind for w in cache.waiting("R-2")] == ["spec"]
    assert len(cache.waiting()) == 2


def test_ac4_states_that_wait_on_nobody_are_absent(tmp_path: Path) -> None:
    log(tmp_path, "R-1").append(to_in_progress("R-1"))  # T1 planning
    assert Cache(tmp_path).waiting() == []


def test_ac4_call_for_edit_task_is_a_row_not_a_waiting_item(tmp_path: Path) -> None:
    lg = log(tmp_path, "R-1")
    lg.append(to_in_progress("R-1"))
    lg.append([ev.escalation("R-1", reason="failed", task="T1", ticket="AD-5")])
    lg.append([n("R-1", "edit_requested", "T1", question="why?")])
    cache = Cache(tmp_path)
    t1 = next(t for t in cache.tasks("R-1") if t.task == "T1")
    assert t1.state == "call_for_edit"
    assert cache.waiting() == []
    assert [(e.task, e.ticket) for e in cache.escalations()] == [("T1", "AD-5")]
    lg.append([n("R-1", "edit_done", "T1")])
    t1 = next(t for t in cache.tasks("R-1") if t.task == "T1")
    assert t1.state == "escalated"
    assert [(e.task, e.ticket) for e in cache.escalations()] == [("T1", "AD-5")]


def test_ac4_call_for_edit_rebuild_equals_incremental(tmp_path: Path) -> None:
    lg = log(tmp_path, "R-1")
    cache = Cache(tmp_path)
    for i, batch in enumerate(
        (
            to_in_progress("R-1"),
            [ev.escalation("R-1", reason="failed", task="T1", ticket="AD-5")],
            [n("R-1", "edit_requested", "T1", question="why?")],
            [n("R-1", "edit_done", "T1")],
        )
    ):
        lg.append(batch)
        cache.refresh()
        fresh = Cache(tmp_path, db_path=tmp_path / f"f{i}.db")
        assert fresh.dump() == cache.dump()


# --- escalations / agents ---------------------------------------------------------------------------------------


def test_x_escalation_without_ticket_then_with_ticket(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append(to_in_progress("R-1"))
    lg.append([ev.escalation("R-1", reason="stuck", task="T1")])
    assert [(e.task, e.reason, e.ticket) for e in cache.escalations()] == [
        ("T1", "stuck", None)
    ]
    lg.append([ev.escalation("R-1", reason="stuck", task="T1", ticket="AD-77")])
    assert [(e.task, e.ticket) for e in cache.escalations()] == [("T1", "AD-77")]
    lg.append([ev.escalation("R-1", reason="stuck", task="T1")])
    assert [(e.task, e.ticket) for e in cache.escalations()] == [("T1", "AD-77")]
    assert len(cache.escalations("R-1")) == 1


def test_x_escalations_per_task_and_requirement(tmp_path: Path) -> None:
    lg = log(tmp_path, "R-1")
    lg.append(to_in_progress("R-1"))
    lg.append([ev.escalation("R-1", reason="a", task="T1", ticket="AD-1")])
    lg2 = log(tmp_path, "R-2")
    lg2.append(to_in_progress("R-2"))
    lg2.append([ev.escalation("R-2", reason="b", task="T1")])
    log(tmp_path, "R-3").append(to_in_progress("R-3"))
    got = {(e.requirement, e.task, e.ticket) for e in Cache(tmp_path).escalations()}
    assert got == {("R-1", "T1", "AD-1"), ("R-2", "T1", None)}
    assert Cache(tmp_path).escalations("R-3") == []


def to_coding(req: str) -> list[ev.NewEvent]:
    return [
        *to_in_progress(req),
        n(req, "plan_drafted", "T1"),
        gate(req, "plan", "T1"),
        ev.attempt_started(req, "T1", 1),
    ]


def test_x_running_agent_lifecycle(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append(to_coding("R-1"))
    lg.append([n("R-1", "agent_started", "T1", role="developer")])
    (a,) = cache.agents()
    assert (a.requirement, a.task, a.role) == ("R-1", "T1", "developer")
    lg.append(
        [
            ev.agent_call(
                "R-1",
                role="developer",
                model="m",
                effort="low",
                duration_ms=1,
                tokens={"in": 1},
                cost_usd=0.0,
                transcript="t",
                prompt_sha256="a",
                result_sha256="b",
                task="T1",
                attempt=1,
            )
        ]
    )
    assert cache.agents() == []


def test_x_agent_stopped_ends_running_agent(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    lg = log(tmp_path, "R-1")
    lg.append(to_coding("R-1"))
    lg.append([n("R-1", "agent_started", "T1", role="developer")])
    assert len(cache.agents()) == 1
    lg.append([n("R-1", "agent.stopped", "T1", role="developer", then="hold")])
    assert cache.agents() == []


# --- unreadable logs --------------------------------------------------------------------------------------------


def _good_and(root: Path, bad_bytes: bytes) -> Cache:
    log(root, "R-1").append([n("R-1", "intake_started"), n("R-1", "intent_drafted")])
    bad = events_path(root, "R-2")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(bad_bytes)
    return Cache(root)


def test_x_garbage_log_does_not_block_others(tmp_path: Path) -> None:
    cache = _good_and(tmp_path, b"not json at all\n")
    reqs = {r.id: r for r in cache.requirements()}
    assert reqs["R-1"].state == "awaiting_intent" and reqs["R-1"].error is None
    assert reqs["R-2"].state == "unreadable" and reqs["R-2"].error
    assert [w.requirement for w in cache.waiting()] == ["R-1"]
    assert cache.tasks("R-2") == []


def test_x_newer_schema_log_does_not_block_others(tmp_path: Path) -> None:
    line = (
        b'{"schema":99,"seq":1,"time":"t","actor":"engine","type":"x","requirement":"R-2",'
        b'"task":null,"attempt":null,"data":{},"ultron_version":"9"}\n'
    )
    cache = _good_and(tmp_path, line)
    reqs = {r.id: r for r in cache.requirements()}
    assert reqs["R-1"].state == "awaiting_intent"
    assert reqs["R-2"].state == "unreadable" and reqs["R-2"].error


def test_x_illegal_sequence_does_not_block_others(tmp_path: Path) -> None:
    log(tmp_path, "R-2").append(
        [n("R-2", "spec_drafted")]
    )  # a valid line, an illegal start
    log(tmp_path, "R-1").append([n("R-1", "intake_started")])
    reqs = {r.id: r for r in Cache(tmp_path).requirements()}
    assert reqs["R-1"].state == "intake_drafting"
    assert reqs["R-2"].state == "unreadable" and reqs["R-2"].error


def test_x_unreadable_log_recovers_when_fixed(tmp_path: Path) -> None:
    cache = _good_and(tmp_path, b"junk\n")
    assert next(r for r in cache.requirements() if r.id == "R-2").state == "unreadable"
    events_path(tmp_path, "R-2").unlink()
    log(tmp_path, "R-2").append([n("R-2", "spec_started")])
    reqs = {r.id: r for r in cache.requirements()}
    assert (reqs["R-2"].state, reqs["R-2"].error) == ("spec_drafting", None)


def test_x_unreadable_row_equals_after_rebuild(tmp_path: Path) -> None:
    cache = _good_and(tmp_path, b"junk\n")
    before = cache.dump()
    assert Cache(tmp_path, db_path=tmp_path / "f.db").dump() == before


def test_x_half_written_last_line_is_ignored_until_complete(tmp_path: Path) -> None:
    lg = log(tmp_path, "R-1")
    lg.append([n("R-1", "spec_started")])
    path = events_path(tmp_path, "R-1")
    path.write_bytes(path.read_bytes() + b'{"schema":1,"seq":2,"ti')  # crash mid-write
    cache = Cache(tmp_path)
    (row,) = cache.requirements()
    assert row.last_seq == 1 and row.state == "spec_drafting"
    assert Cache(tmp_path, db_path=tmp_path / "f.db").dump() == cache.dump()
    lg.repair()
    lg.append([n("R-1", "spec_drafted")])
    (row,) = cache.requirements()
    assert (row.state, row.last_seq) == ("awaiting_spec", 2)


def test_x_empty_log_file_does_not_break_others(tmp_path: Path) -> None:
    p = events_path(tmp_path, "R-1")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"")
    log(tmp_path, "R-2").append([n("R-2", "spec_started")])
    reqs = {r.id: r.state for r in Cache(tmp_path).requirements()}
    assert reqs["R-2"] == "spec_drafting"


# --- concurrency ------------------------------------------------------------------------------------------------


def test_x_concurrent_readers_and_writers_threads(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(req: str) -> None:
        try:
            lg = log(tmp_path, req)
            lg.append([n(req, "spec_started")])
            for i in range(15):
                lg.append([n(req, "probe", i=i)])
            lg.append([n(req, "spec_drafted")])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            cache = Cache(tmp_path)
            while not stop.is_set():
                cache.requirements()
                cache.waiting()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=writer, args=(f"R-{i}",)) for i in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in (*readers, *writers):
        t.start()
    for t in writers:
        t.join(60)
    stop.set()
    for t in readers:
        t.join(60)
    assert not errors, errors
    cache = Cache(tmp_path)
    assert {w.requirement for w in cache.waiting()} == {f"R-{i}" for i in range(4)}
    assert Cache(tmp_path, db_path=tmp_path / "f.db").dump() == cache.dump()


_PROC = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from ultron.store.cache import Cache
    cache = Cache(Path(sys.argv[1]))
    for _ in range(40):
        cache.requirements()
        cache.waiting()
    """
)


def test_x_concurrent_processes_and_a_writer(tmp_path: Path) -> None:
    lg = log(tmp_path, "R-1")
    lg.append([n("R-1", "spec_started")])
    Cache(tmp_path).refresh()
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _PROC, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
        )
        for _ in range(3)
    ]
    for i in range(20):
        lg.append([n("R-1", "probe", i=i)])
    lg.append([n("R-1", "spec_drafted")])
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    cache = Cache(tmp_path)
    assert [w.kind for w in cache.waiting()] == ["spec"]
    assert Cache(tmp_path, db_path=tmp_path / "f.db").dump() == cache.dump()


# --- who writes state.db ----------------------------------------------------------------------------------------


def test_x_only_cache_module_writes_state_db() -> None:
    offenders = []
    for p in (REPO_ROOT / "src").rglob("*.py"):
        if p.name == "cache.py" and p.parent.name == "store":
            continue
        text = p.read_text(encoding="utf-8")
        if "state.db" in text and ("sqlite3" in text or "open(" in text):
            offenders.append(str(p))
    assert offenders == []


def test_x_cache_leaves_only_db_and_lock(tmp_path: Path) -> None:
    for req, batch in steps():
        log(tmp_path, req).append(batch)
    Cache(tmp_path).dump()
    names = sorted(p.name for p in (tmp_path / ".ultron").rglob("*") if p.is_file())
    assert "state.db" in names
    assert not [x for x in names if x.endswith(("-wal", "-shm", "-journal"))]


# --- AC5 --------------------------------------------------------------------------------------------------------


def _thousand(root: Path) -> None:
    for i in range(1, 9):
        req = f"R-{i:02d}"
        base = to_result(req)
        pad = 125 - len(base)
        log(root, req).append([*base, *[n(req, "probe", i=k) for k in range(pad)]])


def test_ac5_cold_read_of_1000_events_under_one_second(tmp_path: Path) -> None:
    _thousand(tmp_path)
    total = sum(len(log(tmp_path, f"R-{i:02d}").read().events) for i in range(1, 9))
    assert total == 1000
    assert not (tmp_path / ".ultron" / "state.db").exists()
    start = time.perf_counter()
    waiting = Cache(tmp_path).waiting()
    elapsed = time.perf_counter() - start
    assert len(waiting) == 8
    assert elapsed < 1.0, f"cold read took {elapsed:.3f} s"


def test_ac5_cold_read_after_deleting_db_under_one_second(tmp_path: Path) -> None:
    _thousand(tmp_path)
    Cache(tmp_path).refresh()
    (tmp_path / ".ultron" / "state.db").unlink()
    start = time.perf_counter()
    Cache(tmp_path).requirements()
    assert time.perf_counter() - start < 1.0


def test_ac5_warm_read_under_one_second(tmp_path: Path) -> None:
    _thousand(tmp_path)
    cache = Cache(tmp_path)
    cache.waiting()
    start = time.perf_counter()
    cache.waiting()
    assert time.perf_counter() - start < 1.0

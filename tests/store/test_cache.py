"""The SQLite cache is derived from the event logs and rebuilt when stale, missing or corrupt (T09)."""

import sqlite3
import time
from pathlib import Path

from ultron.store import events as ev
from ultron.store.cache import Cache
from ultron.store.eventlog import EventLog, events_path


def _log(root: Path, req: str) -> EventLog:
    return EventLog(events_path(root, req))


def _n(req: str, type_: str, task: str | None = None, **data: object) -> ev.NewEvent:
    return ev.NewEvent(
        actor="engine", type=type_, requirement=req, task=task, data=dict(data)
    )


def _gate(
    req: str, gate: str, decision: str = "approve", task: str | None = None
) -> ev.NewEvent:
    return ev.gate_decision(
        req, actor="auto", gate=gate, decision=decision, channel="auto", task=task
    )


def _to_in_progress(req: str) -> list[ev.NewEvent]:
    return [
        _n(req, "spec_started"),
        _n(req, "spec_drafted"),
        _gate(req, "spec"),
        _n(req, "decomposition_started"),
        _n(req, "task_created", "T1", depends_on=[], max_attempts=3),
        _n(req, "task_created", "T2", depends_on=["T1"], max_attempts=3),
        _n(req, "tasks_drafted"),
        _gate(req, "tasks"),
        _n(req, "task_started", "T1"),
    ]


def _fixture_steps() -> list[tuple[str, list[ev.NewEvent]]]:
    """Batches of events for three requirements, one of each interesting state."""
    r1, r2, r3 = "R-1", "R-2", "R-3"
    return [
        (r1, [_n(r1, "intake_started"), _n(r1, "intent_drafted")]),  # waits on intent
        (r2, _to_in_progress(r2)),
        (r2, [_n(r2, "plan_drafted", "T1")]),  # plan waits
        (r3, _to_in_progress(r3)),
        (
            r3,
            [
                _n(r3, "plan_drafted", "T1"),
                _gate(r3, "plan", task="T1"),
                ev.attempt_started(r3, "T1", 1, rung="R1"),
                _n(r3, "agent_started", "T1", role="developer"),
                _n(r3, "question_asked", "T1", id="Q1", text="24 hours?"),
            ],
        ),
        (
            r3,
            [
                _n(r3, "coding_finished", "T1"),
                ev.attempt_finished(r3, "T1", 1, outcome="passed"),
                _n(r3, "result_requested", "T1"),
            ],
        ),
        (r2, [ev.escalation(r2, reason="stuck", task="T1")]),
        (r2, [ev.escalation(r2, reason="stuck", task="T1", ticket="AD-9")]),
    ]


def _apply(root: Path) -> None:
    for req, batch in _fixture_steps():
        _log(root, req).append(batch)


def test_rebuild_equals_incremental(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    for req, batch in _fixture_steps():
        _log(tmp_path, req).append(batch)
        cache.refresh()  # an incremental update after every batch
    incremental = cache.dump()
    assert incremental["requirements"]  # not vacuous

    fresh = Cache(tmp_path, db_path=tmp_path / "other.db")
    fresh.rebuild()
    assert fresh.dump() == incremental


def test_projection_content(tmp_path: Path) -> None:
    _apply(tmp_path)
    cache = Cache(tmp_path)
    reqs = {r.id: r for r in cache.requirements()}
    assert reqs["R-1"].state == "awaiting_intent"
    assert reqs["R-2"].state == "escalated"  # T1 escalated, T2 blocked
    tasks = {(t.requirement, t.task): t for t in cache.tasks()}
    assert tasks[("R-3", "T1")].state == "awaiting_result"
    assert tasks[("R-3", "T1")].attempt == 1 and tasks[("R-3", "T1")].max_attempts == 3
    assert tasks[("R-3", "T2")].state == "pending"
    assert [(a.requirement, a.role) for a in cache.agents()] == [("R-3", "developer")]
    assert [(e.requirement, e.task, e.ticket) for e in cache.escalations()] == [
        ("R-2", "T1", "AD-9")
    ]


def test_deleted_db_rebuilds_with_same_result(tmp_path: Path) -> None:
    _apply(tmp_path)
    cache = Cache(tmp_path)
    before = cache.dump()
    cache.db_path.unlink()
    assert Cache(tmp_path).dump() == before
    assert cache.db_path.exists()


def test_garbage_db_rebuilds_with_same_result(tmp_path: Path) -> None:
    _apply(tmp_path)
    cache = Cache(tmp_path)
    before = cache.dump()
    cache.db_path.write_bytes(b"this is not a database \x00\xff" * 500)
    assert Cache(tmp_path).dump() == before
    cache.db_path.write_bytes(b"")
    assert Cache(tmp_path).dump() == before


def test_damaged_tables_rebuild(tmp_path: Path) -> None:
    _apply(tmp_path)
    cache = Cache(tmp_path)
    before = cache.dump()
    con = sqlite3.connect(cache.db_path)
    con.execute("DROP TABLE tasks")
    con.commit()
    con.close()
    assert Cache(tmp_path).dump() == before


def test_append_then_read_sees_new_events(tmp_path: Path) -> None:
    _log(tmp_path, "R-1").append([_n("R-1", "spec_started")])
    cache = Cache(tmp_path)
    (req,) = cache.requirements()
    assert (req.state, req.last_seq) == ("spec_drafting", 1)
    _log(tmp_path, "R-1").append([_n("R-1", "spec_drafted")])
    (req,) = cache.requirements()
    assert (req.state, req.last_seq) == ("awaiting_spec", 2)
    assert [w.kind for w in cache.waiting()] == ["spec"]
    # a requirement that appears later is picked up too
    _log(tmp_path, "R-2").append([_n("R-2", "spec_started")])
    assert {r.id for r in cache.requirements()} == {"R-1", "R-2"}


def test_vanished_log_drops_rows(tmp_path: Path) -> None:
    _log(tmp_path, "R-1").append([_n("R-1", "spec_started")])
    cache = Cache(tmp_path)
    assert cache.requirements()
    events_path(tmp_path, "R-1").unlink()
    assert cache.requirements() == []


def test_waiting_on_user_all_kinds(tmp_path: Path) -> None:
    r1, r2, r3, r4 = "R-1", "R-2", "R-3", "R-4"
    _log(tmp_path, r1).append([_n(r1, "intake_started"), _n(r1, "intent_drafted")])
    _log(tmp_path, r2).append([_n(r2, "spec_started"), _n(r2, "spec_drafted")])
    _log(tmp_path, r3).append([*_to_in_progress(r3), _n(r3, "plan_drafted", "T1")])
    _log(tmp_path, r4).append(
        [
            *_to_in_progress(r4),
            _n(r4, "plan_drafted", "T1"),
            _gate(r4, "plan", task="T1"),
            ev.attempt_started(r4, "T1", 1),
            _n(r4, "coding_finished", "T1"),
            ev.attempt_finished(r4, "T1", 1, outcome="passed"),
            _n(r4, "result_requested", "T1"),
            _n(r4, "question_asked", id="Q1", text="which?"),
            _n(r4, "question_asked", id="Q2", text="answered one"),
            _n(r4, "question_answered", id="Q2"),
        ]
    )
    got = {(w.requirement, w.kind, w.task, w.ref) for w in Cache(tmp_path).waiting()}
    assert got == {
        (r1, "intent", None, None),
        (r2, "spec", None, None),
        (r3, "plan", "T1", None),
        (r4, "result", "T1", None),
        (r4, "question", None, "Q1"),
    }


def test_unreadable_log_does_not_break_others(tmp_path: Path) -> None:
    _log(tmp_path, "R-1").append([_n("R-1", "spec_started")])
    bad = events_path(tmp_path, "R-2")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not json\n")
    reqs = {r.id: r for r in Cache(tmp_path).requirements()}
    assert reqs["R-1"].state == "spec_drafting"
    assert reqs["R-2"].state == "unreadable" and reqs["R-2"].error


def test_newer_schema_marks_unreadable(tmp_path: Path) -> None:
    p = events_path(tmp_path, "R-1")
    p.parent.mkdir(parents=True)
    p.write_bytes(
        b'{"schema":99,"seq":1,"time":"t","actor":"engine","type":"x","requirement":"R-1",'
        b'"task":null,"attempt":null,"data":{},"ultron_version":"9"}\n'
    )
    (req,) = Cache(tmp_path).requirements()
    assert req.state == "unreadable" and "newer" in (req.error or "")


def test_lock_lives_under_ultron_locks(tmp_path: Path) -> None:
    _log(tmp_path, "R-1").append([_n("R-1", "spec_started")])
    Cache(tmp_path).refresh()
    assert (tmp_path / ".ultron" / "state.db").exists()
    assert list((tmp_path / ".ultron" / "locks").glob("*"))


def test_rebuild_restores_a_tampered_cache(tmp_path: Path) -> None:
    """The cache is not the source of truth: a rebuild from the log undoes any edit to it."""
    _log(tmp_path, "R-1").append([_n("R-1", "spec_started")])
    cache = Cache(tmp_path)
    before = cache.dump()
    con = sqlite3.connect(cache.db_path)
    con.execute("UPDATE requirements SET state = 'complete'")
    con.commit()
    con.close()
    cache.rebuild()
    assert cache.dump() == before


def test_cold_read_1000_events_under_one_second(tmp_path: Path) -> None:
    for i in range(1, 6):
        req = f"R-{i}"
        _log(tmp_path, req).append(
            [
                *_to_in_progress(req),
                _n(req, "plan_drafted", "T1"),
                *[_n(req, "probe", i=k) for k in range(190)],
            ]
        )
    total = sum(len(_log(tmp_path, f"R-{i}").read().events) for i in range(1, 6))
    assert total == 1000
    start = time.perf_counter()
    waiting = Cache(tmp_path).waiting()
    elapsed = time.perf_counter() - start
    assert len(waiting) == 5
    assert elapsed < 1.0, f"cold read took {elapsed:.2f} s"

"""The SQLite cache (`.ultron/state.db`): derived from the event logs, never the source of truth (spec §10, D10).

Only this module writes `state.db`. Every query first brings the cache up to date (`refresh`): a requirement whose
log changed since it was last applied (its size, modification time or last `seq` differs) is projected again from
its whole log, in one transaction, and a requirement whose log is gone loses its rows. A missing, corrupt or
wrong-version database is deleted and built again. Incremental updates and a rebuild use the same projector, so
they give the same rows.

Event vocabulary read here, beyond the state machines' (`ultron.engine.states`), because nothing else defines it:

- `question_asked` / `question_answered`: a pending human question, matched by `data.id`;
- `agent_started` (`data.role`): an ultron agent is running until an `agent_call` or `agent.stopped` for the same
  task (and role, when given) arrives;
- `escalation` with a task: one row per task, the ticket from the latest record that has one.

A requirement whose log can't be read (corrupt, a newer schema, an illegal sequence) gets state `unreadable` and the
reason in `error`; it never stops the other requirements from being read.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultron.engine.states import RequirementState, TaskState, fold
from ultron.store._filelock import FileLock
from ultron.store.eventlog import EventLog, EventLogError
from ultron.store.events import AGENT_CALL, ESCALATION, Event, EventError

__all__ = [
    "AgentRow",
    "Cache",
    "EscalationRow",
    "Projection",
    "RequirementRow",
    "TaskRow",
    "WaitingItem",
    "project",
]

CACHE_SCHEMA_VERSION = 1
UNREADABLE = "unreadable"

QUESTION_ASKED = "question_asked"
QUESTION_ANSWERED = "question_answered"
AGENT_STARTED = "agent_started"
AGENT_STOPPED = "agent.stopped"

_TABLES: dict[str, str] = {
    "requirements": "id TEXT PRIMARY KEY, state TEXT NOT NULL, last_seq INTEGER NOT NULL, error TEXT",
    "tasks": (
        "requirement TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL, "
        "max_attempts INTEGER NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (requirement, task)"
    ),
    "waiting": (
        "requirement TEXT NOT NULL, kind TEXT NOT NULL, task TEXT, ref TEXT, since_seq INTEGER NOT NULL"
    ),
    "agents": (
        "requirement TEXT NOT NULL, task TEXT, attempt INTEGER, role TEXT NOT NULL, since_seq INTEGER NOT NULL"
    ),
    "escalations": (
        "requirement TEXT NOT NULL, task TEXT, reason TEXT NOT NULL, ticket TEXT, seq INTEGER NOT NULL"
    ),
}
_ORDER: dict[str, str] = {
    "requirements": "id",
    "tasks": "requirement, task",
    "waiting": "requirement, since_seq, kind, task, ref",
    "agents": "requirement, since_seq, task, role",
    "escalations": "requirement, seq, task",
}
_WAITING_REQUIREMENT_STATES = {
    RequirementState.AWAITING_INTENT: "intent",
    RequirementState.AWAITING_SPEC: "spec",
    RequirementState.AWAITING_TASKS: "tasks",
}
_WAITING_TASK_STATES = {
    TaskState.AWAITING_PLAN: "plan",
    TaskState.AWAITING_RESULT: "result",
}


@dataclass(frozen=True)
class RequirementRow:
    id: str
    state: str
    last_seq: int
    error: str | None


@dataclass(frozen=True)
class TaskRow:
    requirement: str
    task: str
    state: str
    attempt: int
    max_attempts: int
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class WaitingItem:
    """Something waiting on the user: kind `intent`, `spec`, `tasks`, `plan`, `result` or `question`."""

    requirement: str
    kind: str
    task: str | None
    ref: str | None
    since_seq: int


@dataclass(frozen=True)
class AgentRow:
    requirement: str
    task: str | None
    attempt: int | None
    role: str
    since_seq: int


@dataclass(frozen=True)
class EscalationRow:
    requirement: str
    task: str | None
    reason: str
    ticket: str | None
    seq: int


@dataclass(frozen=True)
class Projection:
    """Everything the cache keeps for one requirement."""

    state: str
    last_seq: int
    tasks: tuple[TaskRow, ...] = ()
    waiting: tuple[WaitingItem, ...] = ()
    agents: tuple[AgentRow, ...] = ()
    escalations: tuple[EscalationRow, ...] = ()


def project(requirement: str, events: list[Event]) -> Projection:
    """Project one requirement's events into cache rows. Pure; raises what `fold` raises on a bad sequence."""
    snapshot = fold(events)
    last_seq = events[-1].seq if events else 0
    tasks = tuple(
        TaskRow(
            requirement,
            name,
            status.state.value,
            status.attempt,
            status.max_attempts,
            status.depends_on,
        )
        for name, status in snapshot.tasks.items()
    )
    waiting: list[WaitingItem] = []
    kind = _WAITING_REQUIREMENT_STATES.get(snapshot.requirement)
    if kind is not None:
        waiting.append(WaitingItem(requirement, kind, None, None, last_seq))
    for name, status in snapshot.tasks.items():
        task_kind = _WAITING_TASK_STATES.get(status.state)
        if task_kind is not None:
            waiting.append(WaitingItem(requirement, task_kind, name, None, last_seq))

    questions: dict[str, WaitingItem] = {}
    agents: list[AgentRow] = []
    escalations: dict[str | None, EscalationRow] = {}
    for event in events:
        if event.type in (QUESTION_ASKED, QUESTION_ANSWERED):
            ref = str(event.data.get("id", f"seq-{event.seq}"))
            if event.type == QUESTION_ASKED:
                questions.setdefault(
                    ref,
                    WaitingItem(requirement, "question", event.task, ref, event.seq),
                )
            else:
                questions.pop(ref, None)
        elif event.type == AGENT_STARTED:
            agents.append(
                AgentRow(
                    requirement,
                    event.task,
                    event.attempt,
                    str(event.data.get("role", "unknown")),
                    event.seq,
                )
            )
        elif event.type in (AGENT_CALL, AGENT_STOPPED):
            role = event.data.get("role")
            agents = [
                a
                for a in agents
                if not (a.task == event.task and (role is None or a.role == role))
            ]
        elif event.type == ESCALATION and event.task is not None:
            reason = str(event.data.get("reason", ""))
            ticket = event.data.get("ticket")
            old = escalations.get(event.task)
            if ticket is None and old is not None:
                ticket = old.ticket
            escalations[event.task] = EscalationRow(
                requirement,
                event.task,
                reason,
                str(ticket) if ticket is not None else None,
                old.seq if old is not None else event.seq,
            )
    waiting.extend(questions.values())
    return Projection(
        state=snapshot.requirement.value,
        last_seq=last_seq,
        tasks=tasks,
        waiting=tuple(waiting),
        agents=tuple(agents),
        escalations=tuple(escalations.values()),
    )


class Cache:
    """The cache for the repo at `root`. `db_path` defaults to `<root>/.ultron/state.db`."""

    def __init__(
        self,
        root: Path | str,
        *,
        db_path: Path | str | None = None,
        lock_timeout: float = 30.0,
    ) -> None:
        self.root = Path(root)
        self.db_path = (
            Path(db_path) if db_path is not None else self.root / ".ultron" / "state.db"
        )
        self.lock_path = self.root / ".ultron" / "locks" / "state.db.lock"
        self.lock_timeout = lock_timeout

    # --- maintenance --------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Bring the cache up to date with the logs (building it if missing or corrupt)."""
        with self._session():
            pass

    def rebuild(self) -> None:
        """Throw the database away and build it again from the logs."""
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            self._delete_db()
        self.refresh()

    # --- queries (each one refreshes first) -----------------------------------------------------------------------

    def requirements(self) -> list[RequirementRow]:
        return [RequirementRow(*r) for r in self._select("requirements")]

    def tasks(self, requirement: str | None = None) -> list[TaskRow]:
        return [
            TaskRow(r[0], r[1], r[2], r[3], r[4], tuple(json.loads(r[5])))
            for r in self._select("tasks", requirement)
        ]

    def waiting(self, requirement: str | None = None) -> list[WaitingItem]:
        """Items waiting on the user across all requirements (or one): intent, spec, tasks, plan, result, questions."""
        return [WaitingItem(*r) for r in self._select("waiting", requirement)]

    def agents(self, requirement: str | None = None) -> list[AgentRow]:
        return [AgentRow(*r) for r in self._select("agents", requirement)]

    def escalations(self, requirement: str | None = None) -> list[EscalationRow]:
        return [EscalationRow(*r) for r in self._select("escalations", requirement)]

    def dump(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every data row of every table, ordered, for comparing two caches."""
        return {name: self._select(name) for name in _TABLES}

    # --- internals ----------------------------------------------------------------------------------------------

    def _select(
        self, table: str, requirement: str | None = None
    ) -> list[tuple[Any, ...]]:
        key = "id" if table == "requirements" else "requirement"
        sql = f"SELECT * FROM {table}"
        args: tuple[str, ...] = ()
        if requirement is not None:
            sql += f" WHERE {key} = ?"
            args = (requirement,)
        sql += f" ORDER BY {_ORDER[table]}"
        with self._session() as con:
            return [tuple(r) for r in con.execute(sql, args).fetchall()]

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            con = self._open()
            try:
                try:
                    self._sync(con)
                except sqlite3.Error:
                    con.close()
                    self._delete_db()
                    con = self._open()
                    self._sync(con)
                yield con
            finally:
                con.close()

    def _delete_db(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)

    def _open(self) -> sqlite3.Connection:
        """Connect and check the database; delete and create it afresh if it is missing, corrupt or old."""
        for _ in range(2):
            existed = self.db_path.exists()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.db_path)
            try:
                con.execute("PRAGMA synchronous = OFF")
                if existed and self._healthy(con):
                    return con
                if not existed:
                    self._create(con)
                    return con
            except sqlite3.Error:
                pass
            con.close()
            self._delete_db()
        raise RuntimeError(f"could not create the cache at {self.db_path}")

    def _healthy(self, con: sqlite3.Connection) -> bool:
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or row[0] != str(CACHE_SCHEMA_VERSION):
                return False
            for table in (*_TABLES, "applied"):
                con.execute(f"SELECT * FROM {table} LIMIT 0")
            check = con.execute("PRAGMA quick_check").fetchone()
            return bool(check) and check[0] == "ok"
        except sqlite3.Error:
            return False

    def _create(self, con: sqlite3.Connection) -> None:
        with con:
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute(
                "CREATE TABLE applied (requirement TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, "
                "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)"
            )
            for name, columns in _TABLES.items():
                con.execute(f"CREATE TABLE {name} ({columns})")
            con.execute(
                "INSERT INTO meta VALUES ('schema_version', ?)",
                (str(CACHE_SCHEMA_VERSION),),
            )

    def _logs(self) -> dict[str, Path]:
        base = self.root / "sdlc" / "requirements"
        if not base.is_dir():
            return {}
        return {
            p.parent.name: p for p in sorted(base.glob("*/events.jsonl")) if p.is_file()
        }

    def _sync(self, con: sqlite3.Connection) -> None:
        logs = self._logs()
        applied = {
            r[0]: (r[1], r[2], r[3])
            for r in con.execute(
                "SELECT requirement, last_seq, size, mtime_ns FROM applied"
            )
        }
        with con:
            for gone in applied.keys() - logs.keys():
                self._forget(con, gone)
            for requirement, path in logs.items():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                before = applied.get(requirement)
                if before is not None and before[1:] == (
                    stat.st_size,
                    stat.st_mtime_ns,
                ):
                    continue
                self._apply(con, requirement, path, stat)

    def _forget(self, con: sqlite3.Connection, requirement: str) -> None:
        con.execute("DELETE FROM applied WHERE requirement = ?", (requirement,))
        con.execute("DELETE FROM requirements WHERE id = ?", (requirement,))
        for table in ("tasks", "waiting", "agents", "escalations"):
            con.execute(f"DELETE FROM {table} WHERE requirement = ?", (requirement,))

    def _apply(
        self,
        con: sqlite3.Connection,
        requirement: str,
        path: Path,
        stat: os.stat_result,
    ) -> None:
        self._forget(con, requirement)
        error: str | None = None
        try:
            events = EventLog(path).read().events
            projection = project(requirement, events)
        except (EventLogError, EventError, ValueError, OSError) as exc:
            error = str(exc) or type(exc).__name__
            projection = Projection(state=UNREADABLE, last_seq=0)
        con.execute(
            "INSERT INTO requirements VALUES (?, ?, ?, ?)",
            (requirement, projection.state, projection.last_seq, error),
        )
        con.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    t.requirement,
                    t.task,
                    t.state,
                    t.attempt,
                    t.max_attempts,
                    json.dumps(list(t.depends_on)),
                )
                for t in projection.tasks
            ],
        )
        con.executemany(
            "INSERT INTO waiting VALUES (?, ?, ?, ?, ?)",
            [
                (w.requirement, w.kind, w.task, w.ref, w.since_seq)
                for w in projection.waiting
            ],
        )
        con.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?)",
            [
                (a.requirement, a.task, a.attempt, a.role, a.since_seq)
                for a in projection.agents
            ],
        )
        con.executemany(
            "INSERT INTO escalations VALUES (?, ?, ?, ?, ?)",
            [
                (e.requirement, e.task, e.reason, e.ticket, e.seq)
                for e in projection.escalations
            ],
        )
        con.execute(
            "INSERT INTO applied VALUES (?, ?, ?, ?)",
            (requirement, projection.last_seq, stat.st_size, stat.st_mtime_ns),
        )

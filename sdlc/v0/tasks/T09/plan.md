# T09 plan: SQLite cache rebuilt from the event log
Author: developer agent (R1, attempt 1). Spec: §10 "The cache", D10, D21, §21. Depends on T05 (`EventLog`, events) and T08 (`fold`).

## Files that change
- `src/ultron/store/cache.py` (new): the only module that writes `.ultron/state.db`. `Cache(root)` with `refresh()`, `rebuild()`, `requirements()`, `tasks()`, `waiting()`, `agents()`, `escalations()`, `dump()`.
- `tests/store/test_cache.py` (new). No other file changes (T05 and T08 untouched; `store/__init__.py` stays as is).

## Design
- Source of truth stays the logs under `<root>/sdlc/requirements/*/events.jsonl`. Every query first calls `refresh()`, so a read never returns stale rows.
- Tables: `meta(schema_version)`, `applied(requirement, last_seq, size, mtime_ns)`, `requirements(id, state, last_seq, error)`, `tasks(requirement, task, state, attempt, max_attempts, depends_on)`, `waiting(requirement, kind, task, ref, since_seq)`, `agents(requirement, task, attempt, role, since_seq)`, `escalations(requirement, task, reason, ticket, seq)`.
- Projection is one pure function `project(events) -> Projection` using `fold` (T08) for states plus a scan for waiting items, running agents and escalations. A stale requirement (log size/mtime differs from `applied`, or `last_seq` differs) is re-projected from its whole log and its rows replaced in one transaction. A requirement whose log is gone loses its rows. So the incremental path and the rebuild share one projector, and the test proves a cache brought up to date step by step equals one rebuilt in one go.
- Unreadable logs (`EventLogError`, `EventError` incl. `NewerSchemaError`, `IllegalTransition`) do not break other requirements: the requirement gets state `unreadable` and the message in `error`.
- Corruption: opening/using the db raises `sqlite3.DatabaseError`, or `meta.schema_version` is wrong, or `PRAGMA quick_check` fails: delete the file (and journal files) and rebuild. Missing file: build. The cache never reads state from itself except to decide staleness.
- Writers (several processes) are serialised by `FileLock` on `.ultron/locks/state.db.lock`. Default rollback journal (no WAL files) so deleting is simple.
- Event vocabulary the projector reads, defined in `cache.py` because T05/T08 define none (spec silent; closest reading): waiting items come from state (`awaiting_intent` -> kind `intent`, `awaiting_spec` -> `spec`, `awaiting_tasks` -> `tasks`, task `awaiting_plan` -> `plan`, `awaiting_result` -> `result`) and from `question_asked` / `question_answered` events (matched by `data.id`, kind `question`, ref = id). Running agents: `agent_started` (data.role) until an `agent_call` or `agent.stopped` for the same task and role. Escalations: `escalation` events with a task, one row per task, ticket from the latest record that has one.

## Order of work
1. Tests (fixtures built with `EventLog.append`), fail. 2. `cache.py`. 3. build, lint, test.

## Proof per criterion
- AC1 `test_rebuild_equals_incremental`: append a multi-requirement fixture in batches, `dump()` after each; a fresh `Cache` rebuild equals it.
- AC2 `test_deleted_db_rebuilds`, `test_garbage_db_rebuilds`: same `dump()` after delete / garbage bytes.
- AC3 `test_append_then_read_sees_new_events`: read, append, read; `last_seq` advances and state changes.
- AC4 `test_waiting_on_user_all_kinds`: one intent, spec, plan, result and question across two requirements; `waiting()` lists all.
- AC5 `test_cold_read_1000_events_under_one_second`: 1,000 events over several requirements, cold `waiting()` under 1 s (the CI runners are the same test).

## Risks
- Full re-projection of a changed requirement is O(events) per change; fine at V0 sizes (AC5 measures it).
- The question and agent event names are new vocabulary; later tasks (broker, runner) must emit them or the plan is amended.

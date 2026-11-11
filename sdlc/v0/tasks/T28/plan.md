# T28 plan: activity stream and the monitor query (R1)

Spec §9.1, FR19a. Plain file IO, no MCP tool use (the MCP `ultron_monitor` wrapper is T-later; this task ships the
query functions it will call).

## Files
- `src/ultron/monitor/__init__.py` (new package, exports below)
- `src/ultron/monitor/activity.py`: the stream. `append_activity(ultron_dir, rows)` assigns a monotonically
  increasing cursor `n` to each row under a cross-process `FileLock` (`<ultron_dir>/locks/activity.lock`), keeps the
  high-water mark in `<ultron_dir>/activity.seq` so cursors survive pruning, and writes each batch in one append.
  `read_activity(ultron_dir, since)` reads rows with `n > since`, ignoring a half-written last line. `prune(...)`.
- `src/ultron/monitor/query.py`: `snapshot(root, ultron_dir, now)`, `follow(ultron_dir, since)`,
  `transcript_tail(ultron_dir, ref, lines)`. All return plain dicts, labelled untrusted.
- `src/ultron/agents/calllog.py` (T15, minimal change): use `append_activity` instead of a bare append, and stream
  activity live from the `on_activity` tee so a running agent shows steps; if the adapter streamed nothing, the
  result's activity is appended at the end as before. Row format is unchanged plus `n`.
- tests: `tests/monitor/test_cursor.py`, `test_snapshot.py`, `test_readonly.py`, `test_prune.py`,
  `test_transcript.py`.

## Order
1. Tests (cursor with 3 writer processes, snapshot, read-only hashes, prune, transcript). 2. `activity.py`.
3. `query.py`. 4. calllog switch. 5. build, lint, test.

## Decisions where the spec is silent
- Cursor is an integer `n` per row, not a byte offset, because pruning rewrites the file; the high-water mark in
  `activity.seq` keeps it valid even if every row is pruned.
- "Completed" = requirement state `complete` or `partial`; the time is its last event's `time`. Rows of requirements
  with no log, or not completed, are kept.
- Per-agent usage = the data of the newest `usage` activity of that agent's current call (usage is "so far").
  Last 5 activities are those after the agent's newest `started` row for the same requirement, task and role.
- Task reference is `<requirement id>/<task>` (for example `R-20261109-a3f2/T2`). "Newest" transcript = newest by
  modification time, name as tie-break. Lines are the file's lines (transcripts are indented JSON).
- Untrusted: every result has `untrusted: true` and a `notice`; transcript text is wrapped in
  `<untrusted-data>` markers. T-later MCP layer adds its own wrapper on top.
- Queued tasks = tasks in state `pending`. Waiting = `Cache.waiting()`.

## Risks
- Windows file lock cost with 1,000 appends from 3 processes (batch size 1 per row); expected well under 30 s.
- Reader racing a prune on Windows: reads retry briefly on `OSError`.
- Cache refresh in the snapshot writes only if a log changed; the read-only test refreshes first, then hashes.

## Proof
- AC1: `tests/monitor/test_cursor.py`, 3 `multiprocessing` writers x ~334 rows, follow with `since` in a loop.
- AC2: `test_snapshot.py`, fake running agent with 7 activities, `attempt 2/3`, last 5 shown.
- AC3: `test_readonly.py`, sha256 of `activity.jsonl`, `state.db`, `events.jsonl` before and after every query.
- AC4: `test_prune.py`. AC5: `test_transcript.py`.

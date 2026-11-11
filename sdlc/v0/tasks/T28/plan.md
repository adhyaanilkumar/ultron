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

## Attempt 2 (R2)
Attempt 1 failed at the tester with 4 real defects. Fixes, each reproduced first by a test in `tests/monitor/`:
- A. `prune` crashed with `PermissionError` at `os.replace` while a reader had the file open (Windows). Retry the
  replace with a bounded backoff (about 10 s, under the lock); `os.replace` is atomic, so readers see the old or the
  new file, never a partial one. Readers' retry window is also widened. Files: `activity.py`, `test_prune.py`.
- B. A writer that died mid-line left a torn tail and the next append glued its first row onto it. Under the lock,
  before numbering, write a newline if the file does not end with one (the torn fragment becomes a skipped garbage
  line, nothing new is lost). Files: `activity.py`, `test_cursor.py`.
- C. A new attempt's agent showed the previous attempt's rows. In `snapshot`, keep only rows of the agent's current
  attempt (rows without an attempt stay), then take the rows after the newest `started`. Files: `query.py`,
  `test_snapshot.py`.
- D. Transcript content holding `</untrusted-data>` closed the wrapper early. Neutralise `<` of any
  `untrusted-data` tag inside the content (case and space tolerant) as `&lt;`; wrapper, `untrusted: true` and notice
  stay. Files: `query.py`, `test_transcript.py`.
- Also a test that `partial` requirements are pruned like `complete` ones (`test_prune.py`).
Risks: a longer lock hold in prune while a reader keeps the file open; bounded, then the error surfaces.
Proof: new tests plus all of `tests/acceptance/test_t28.py`, `make build`, `make lint`, `make test`.

## Attempt 3 (R3)
Attempt 2 failed at the tester with defect E. Fixes, each reproduced first by a test in `tests/monitor/`:
- E. `prune` wrote `activity.seq` as the max `n` of the rows still in the file, so a second prune after the newest
  rows were gone lowered the high-water mark and the next append reused cursors a follower had passed. The mark now
  never goes down: one helper writes `max(stored mark, new value)` under the lock, atomically (temp file and
  `os.replace`, so a crash cannot leave it empty), and every writer path (append, prune) goes through it. Reading
  the mark retries a busy file and treats a missing or corrupt `activity.seq` as "unknown": the mark is then
  recovered from the highest `n` in the file, never below a readable stored value. A boolean `n` is not a cursor.
  Files: `activity.py`, `test_prune.py` (repeat-prune case and a seeded random append/prune property test),
  `test_cursor.py` (missing/corrupt mark).
- Tester advice: a failed prune removes its `activity.jsonl.tmp` (and a stale one is cleared before writing);
  the defang step also catches NFKC look-alikes (fullwidth `＜`, `／`, fullwidth letters) and ignores invisible
  format characters (zero-width space) between `<` and the tag name. Files: `activity.py`, `query.py`,
  `test_prune.py`, `test_transcript.py`.
- Not done: defanging the raw `lines` field of `transcript_tail`. `tests/acceptance/test_t28.py`
  (`test_attempt2_lines_field_is_raw_but_the_result_is_labelled`) requires `lines` to stay raw, and acceptance tests
  are not mine to change; the result stays labelled `untrusted: true` with the notice.
Risks: an unreadable mark plus a file whose newest rows were already pruned cannot be fully recovered (only by
hand-editing `.ultron`); the atomic write makes this need outside tampering.
Proof: new tests plus all of `tests/acceptance/test_t28.py`, `make build`, `make lint`, `make test`.
# T05 plan: event log (`events.jsonl`), schema and versioned reader
Author: developer agent (R3; rerun at R4). Task: `tasks.md` §T05. Spec: §10, FR16, FR17, §5.3 "Updating". Depends on
T01 only.

**Rung:** R3 at the start (Risk 2: the source of truth, concurrent writers, data loss). Attempt 2 climbed to R4 and
passed. The rerun below stays at R4: a send-back to decomposition doesn't climb.

## Reopened: lock starvation (R4, 2026-11-11)
The owner reopened T05 (send-back, not counted): `test_t05.py::test_ac2_engine_lock_path_non_ascii_paths_and_payloads`
(4 writer processes × 50 appends) took ~40 s and failed about 1 run in 11 on Windows, always while the machine was
busy. Suspected cause: the 5 ms `LK_NBLCK` polling lock is unfair, so one writer starves. I measured before assuming.

### Evidence (Windows 11, 24 logical CPUs, Defender real-time protection on)
An instrumented replay of that test (same payloads, paths and lock path) recorded every lock acquire per writer:
wait, polls, hold time, and the time of each step inside the lock. CPU load came from 24 busy-loop processes.
- **Slow rounds are not starvation.** About 1 round in 8 to 10 under load took 27 to 50 s. In those rounds the lock
  was idle only 0.14 to 0.2 s of the 33 to 50 s. Every writer was slow, and the median hold went from 12.5 ms to
  198 to 349 ms. A fairer lock can't fix that: the lock was busy 99.6% of the time, and 200 holds of about 350 ms
  is 70 s, more than the test's 45 s deadline.
- **The time inside the lock is `_last_seq`**, which reopens the log for reading on every append: median 11 ms
  normally and 347 ms in the slow round. `fsync` took 0.7 to 1 ms and `write` 0.1 ms.
- **A read-open after another process's write is what's slow.** A micro-benchmark: the first open with read access
  (`O_RDONLY` or `O_RDWR`) after a write took 9 to 60 ms at every file size from 1 KB to 50 MB. A write-only open
  took 0.05 ms, and a second read-open took 0.08 ms. This is how an on-access antivirus scan of a modified file
  behaves. In the test every append follows another writer's write, so every append pays the scan, inside the lock,
  one writer after another. CPU load slows the scanner, which explains why failures need a busy machine.
- **The lock is also unfair, as suspected.** The writer that releases the lock takes it back within about 0.1 ms,
  before the sleeping writers poll again. One writer got up to 37 to 40 holds in a row, there were only 14 to 76
  handoffs per 200 appends, and single waits reached 1 to 2 s in normal rounds and 29.9 s in the 50 s round, just
  under the 30 s `LockTimeout`.
- No round raised an exception. The slowest rounds would fail the test at its 45 s deadline or, when one writer is
  starved as well, with `LockTimeout` at 30 s. 15 runs of the real test under load all passed, but 4 took 27 to
  36 s.

### Cause
1. **Main cause:** `append` pays a read-open of the log (an antivirus scan on Windows) inside the lock, so writers
   pay it one at a time. Under load that is about 350 ms per append.
2. **Second cause:** the polling lock lets the releasing writer take the lock straight back (barging), so one waiter
   can wait almost the whole run.

### Fix
1. **`eventlog.py`: read-open the log before taking the lock.** `append` opens the log read-only before it waits for
   the lock, so the scan overlaps the wait instead of adding to every other writer's wait. Under the lock it reads
   the tail through that handle, which sees other writers' appends (measured: 0.03 ms). If the log was created,
   replaced or deleted in between (`os.stat(path)` and `os.fstat(handle)` give a different `st_dev`/`st_ino`, or no
   file), it reopens under the lock, as today. Writing is unchanged: write-only append, one `os.write`, `fsync`.
   There's no API or file-format change.
2. **`_filelock.py`: a turn lock, so the next waiter is served before the releaser can return.** Every `acquire`
   first takes the **turn lock**, a second lock on a sibling file (`R1.events.lock` → `R1.events.turn.lock`). Holding
   it, it waits for the main lock, then lets the turn lock go. A writer that just released has to get the turn lock
   too, and a waiter usually already holds it, so the waiter goes next. Waiters poll the turn lock at a jittered
   interval (polling in lockstep would keep favouring the same writer). The one holder of the turn lock polls the
   main lock fast. Only the main lock (the same file, the same `msvcrt.locking`/`fcntl.flock` calls, and `LOCK_BACKEND`
   unchanged) gives mutual exclusion. The turn lock only decides who goes next. Both locks are OS locks held by an
   open handle, so both are freed when their holder dies. The single timeout covers both waits.

Options weighed:
- **Blocking OS lock.** `msvcrt.LK_LOCK` is no better: the C runtime tries 10 times, 1 s apart, then fails, so it's
  a coarser poll. A truly blocking `LockFileEx` (served in order by the kernel) would need overlapped handles and
  cancellation through `ctypes`, and the backend would no longer be `msvcrt` (`LOCK_BACKEND`, AC6). On Linux a
  blocking `flock` has no timeout except signals, which only work on the main thread, and `EventLog` is used from
  threads. Rejected.
- **Exponential backoff with jitter.** It makes long waiters poll less often, which makes starvation worse. Jitter
  alone only breaks lockstep polling. Kept only as jitter inside the turn lock.
- **Ticket or queue scheme** (a counter plus a lock per waiter so dead waiters can be skipped). It gives strict FIFO
  order but needs a shared counter, detection of dead ticket holders and clean-up. That's too much machinery for
  what it adds over the turn lock.
- **Chosen: fix 1 plus the turn lock.** Fix 1 removes the main cause, and the turn lock removes barging, using only
  the primitives already in use.

### Files that change (reopen)
- `src/ultron/store/eventlog.py`: `append` opens the read handle before the lock. `_last_seq` reads through a handle
  instead of opening the path. There's a helper to check or reopen the handle under the lock.
- `src/ultron/store/_filelock.py`: the turn lock and jittered polling. The docstring explains both.
- `tests/store/test_filelock.py`: a waiter is served before the releaser takes the lock back, in-process and across
  processes, repeated over many rounds, plus checks on the turn file's name and that a held turn lock times out
  cleanly.
- `tests/store/test_lock_stress.py` (new): (a) `append` never read-opens the log while holding the lock (deterministic,
  both OSes). (b) A stress run: 6 writer processes loop on `append` alongside CPU-burning processes. Every append's
  wait must stay bounded, and no writer may get more than a few holds in a row. Under 60 s. (Changed during the
  build: (b) bounds the longest lock wait; see below.)
- `sdlc/v0/tasks/T05/plan.md`: this section.

### Order of work
1. Plan commit alone (`T05 reopen: plan (plan gate: auto)`).
2. Tests first: run them on the current code and record the failures (red).
3. Fix 1, then the turn lock (green). Then the original acceptance test 30 times in a row, timed, and under load.
4. `make build`, `make lint`, `make test`, plus `mypy --platform linux src` for the `fcntl` branch.

### Risks
1. **A queued writer now holds a read handle on the log while it waits.** On Windows, a file open without
   `FILE_SHARE_DELETE` can't be replaced or deleted (measured: `os.replace` fails with `PermissionError`). Before,
   only writers holding the lock and brief lock-free readers held it open. T07, if it resets `events.jsonl` while it
   holds the events lock, could be refused until queued writers time out. T07 has to retry on sharing violations
   anyway (readers don't lock), but now has to expect queued writers too. Noted for T07, not solved here.
2. **One more file per lock** (`*.turn.lock` beside each lock). It ends in `.lock`, so ignore rules for `*.lock`
   cover it. Engine locks live in `.ultron/locks/`. The default lock beside the log gets a sibling there too.
3. **The order isn't strict FIFO.** Waiters race for the turn lock, so the order among waiters is random. But each
   hand-off goes to one of them rather than back to the releaser, so a long wait gets exponentially unlikely
   instead of being the normal case for one writer.
4. **The stress test must not be flaky itself.** Its limits are set well above what the fixed code shows, and it
   checks hold order and the run length per writer rather than raw speed. The proof records the max duration of the
   30 runs. (Changed during the build: it bounds the longest lock wait, calibrated below.)
5. **The scan isn't gone, only moved out of the lock.** Each append still pays it once, in parallel with the other
   writers. Under heavy load a single append can still take a few hundred ms.

### Changes during the reopen build
- **The stress test bounds the longest lock wait, not the longest run.** With every CPU busy, the fixed lock still
  showed runs of 9 to 17 appends by one writer. The writers that weren't running hadn't reached the lock (they were
  opening the log or waiting for the scheduler), so the order followed when each scan finished, not the lock.
  Measured over 6 × 60 appends with as many busy processes as CPUs, the longest single wait in `FileLock.acquire`
  was 0.04 to 0.17 s with the fix (10 rounds) and 1.9 to 6.5 s before it (8 rounds). The test failed above 1.0 s
  (changed again at the end; see Proof).
  Through pytest it was red 4 of 4 on the old code (worst waits of 7.0 to 9.4 s) and green 6 of 6 on the new code
  (5 to 9 s per run). The handoff ratio didn't separate the two reliably at half load (old code 0.44 to 0.65).
- **Tried and dropped: a releaser that yields once before asking again.** Under load it gave the same numbers as
  without it, at a cost to single writers.
- **Also added:** a test that `append` reads a log created by another writer while it waited, and one where the
  log is replaced while it waits (Linux: the next seq follows the new file; Windows: the replace is refused, risk 1).
- **Fix 1 went further: the `EventLog` object keeps its read handle between appends** (risk 5 measured, then
  removed). With a read-open before every append, the original test still took 7 to 33 s in about 1 run in 10 with
  nothing else running, and up to 42.8 s of its 45 s under load (30 of 30 passed in each case). Instrumented, those
  runs spent all their time in that read-open: a median of 200 to 400 ms per open, up to 2.7 s for one. Lock waits
  stayed at most 0.18 s per writer. The scanner has slow phases of its own, so moving the scan out of the lock only
  bought about 4 times the headroom. A handle that is kept is scanned once: reads through it after other writers'
  appends cost 0.03 to 0.04 ms. So `append` now opens the reader once per object (before the lock), keeps it, and
  checks it under the lock against the file at the path (`st_dev`/`st_ino`), reopening if the log was created,
  deleted or replaced. A `threading.Lock` per object guards the handle, and `weakref.finalize` closes it with the
  object. On Windows it is opened with `_winapi.CreateFile` and `FILE_SHARE_DELETE` (`os.open` can't ask for that)
  and wrapped with `msvcrt.open_osfhandle`. Measured: with that sharing, the log can be deleted and recreated while
  the handle is open (git's checkout does exactly that). An atomic `os.replace` onto it is still refused.
- **Risk 1, revised.** Any process with a live `EventLog` that has appended keeps a read handle on that log. On
  Windows, deleting the log and writing a new one (git) works, and the next append follows the new file (tested).
  An atomic `os.replace` onto the log fails while such a handle is open, as it already does while any reader has
  the file open. T07 should reset with git or delete-then-write, not `os.replace`. Note for T07.
- **Risk 5, revised.** One scan per writer object instead of one per append. `_winapi` is a private stdlib module
  (multiprocessing and asyncio use it), and typeshed covers it.
- **Also added:** a test that a kept reader follows a log deleted and recreated, and one that a fresh object opens
  exactly one reader, before the lock, for 7 appends.

### Proof (reopen)
`test_t05.py::test_ac2_engine_lock_path_non_ascii_paths_and_payloads`, 30 runs in a row, each in its own pytest
process. "Load" means busy-loop processes, one per logical CPU (24).

| Code | No load | Load 24 |
|---|---|---|
| Before (HEAD) | about 1 run in 11 failed (owner's report) | 15 runs: mean 15.1 s, max 35.8 s. At load 36: failed on run 1 (`TimeoutExpired` at the 45 s deadline) |
| Read-open before the lock only | 30/30, mean 5.0 s, max 33.0 s | 30/30, mean 8.6 s, max 42.8 s |
| Final (kept reader + turn lock) | **30/30, mean 0.54 s, max 0.76 s** | **30/30, mean 3.6 s, max 9.5 s**. At load 36: 10/10, mean 7.4 s, max 15.5 s |

The stress test was changed again at the end. With the scan gone from each append, the order separates cleanly, so
it now uses 6 × 100 appends and fails if any writer's longest lock wait is above 2.0 s **or** the lock changed hands
on fewer than 60% of appends while all six were writing. Old code, load 24: red 4 of 4 (hand-offs on 13-31%,
waits up to 10 s; 11-42% over 27 calibration rounds). New code: green 8 of 8 (hand-offs on 100%, waits at most
0.59 s; 95-100% and at most 0.57 s over 18 calibration rounds). 1-2 s per run with the fix.

Checks, Windows: `make build` → `Build succeeded`; `make lint` → `Lint succeeded` (plus `mypy --platform linux
src`: no issues); `make test` → `3138 passed, 7 skipped` (the skips are outside T05) and `All tests passed`.

Linux stand-in (the `uv` Python 3.13 image, 24 CPUs, a `git archive` of the staged tree streamed in, no bind
mount): `LOCK_BACKEND fcntl`, `mypy src` no issues, `tests/store` plus `test_t05.py` 334 passed, the stress test
6 of 6 green (hand-offs on 100%, waits at most 0.38 s). The new tests over HEAD's store modules were red 3 of 3 (6
failed each time). There the old code's waits stayed under 2 s (0.39-1.0 s); the hand-off check (8-12%) caught it.
That is evidence, not the `ci` proof: AC2 `ci` and AC6 are still ci-blocked (no remote).

## Rerun after the tasks.md revision (2026-11-11)
The owner sent the work back to decomposition (lesson L3). `tasks.md` now gives each criterion a `verify_with`, gives
T05 `mcp: none` (plain file IO and locks), adds `ci` to AC2 and adds **AC6**: the concurrency and crash tests pass in
CI on both `windows-latest` (msvcrt lock) and `ubuntu-latest` (fcntl lock). The attempt 2 code stays; nothing in
AC1–AC5 changed except the named proof methods.

What the rerun can and can't do:
- `ci` (AC2's second method, all of AC6) needs a real GitHub Actions run. The repo has no GitHub remote yet (owner's
  open item in `status.md`), so these stay **ci-blocked**. I won't fake a run.
- What I can do: make each CI run show which lock it exercised, make sure no T05 test is skipped on either OS (a
  skip would turn a green run into a false proof), and check the Linux path locally as a stand-in.

### Files that change in the rerun
- `src/ultron/store/_filelock.py`: add `LOCK_BACKEND` (`"msvcrt"` on Windows, `"fcntl"` elsewhere) so tests and the
  CI log name the lock in use. No behaviour change.
- `tests/store/test_filelock.py` (new): the lock tested directly. `LOCK_BACKEND` matches the OS; a second process
  can't take a held lock (`LockTimeout`); the lock frees when its holder process is killed; two `FileLock`s in one
  process exclude each other; double `acquire` raises; `release` is idempotent. Plus a guard that the T05 test files
  (`tests/store/`, `tests/acceptance/test_t05.py`) contain no `skip`/`skipif`/`xfail`, so a CI pass ran them all.
- `tests/store/test_events_concurrency.py`: each worker prints `LOCK_BACKEND`; the test asserts it is this OS's lock,
  so the AC2 test itself names the lock it proved.
- `tests/store/test_eventlog.py`: `test_truncated_tail` parametrized over several byte offsets (1 byte, mid-line, all
  but the newline), matching AC4's `verify_with`.

### Order of work (rerun)
1. Tests first (red): `test_filelock.py` backend test fails until `LOCK_BACKEND` exists; concurrency test asserts the
   backend; truncation parametrized.
2. Add `LOCK_BACKEND` (green).
3. `make build`, `make lint`, `make test` on Windows. Also `mypy --platform linux src` (Windows mypy checks only the
   win32 branch) and the same three steps in a local Linux container (`uv` image, Docker already installed, ₹0) as a
   stand-in for `ubuntu-latest`. The container run is evidence, not the `ci` proof.

### Proof per criterion (rerun, by `verify_with`)
| AC | verify_with | Proof | Status expected |
|---|---|---|---|
| AC1 | `pytest` | `test_eventlog.py::test_seq_gap_free`, `::test_prefix_bytes_unchanged` | met |
| AC2 | `pytest` · `ci` | `test_events_concurrency.py` (4 real processes, asserts `LOCK_BACKEND`); `ci` needs a run on both OSes | pytest met; ci blocked |
| AC3 | `pytest` | `test_eventlog.py::test_reads_old_version_fixture` round-trips `fixtures/events_v0_0_1.jsonl` | met |
| AC4 | `pytest` | `test_eventlog.py::test_truncated_tail[offset]` at several offsets, `::test_truncated_only_line` | met |
| AC5 | `pytest` | `test_events_model.py::test_constructor_round_trips[...]`, one per event type | met |
| AC6 | `ci` | a pull-request run green on `windows-latest` and `ubuntu-latest`; `test_filelock.py::test_lock_backend_matches_os` passing on each names the lock. Local stand-in: Windows run plus a Linux container run | ci blocked |

### Risks (rerun)
1. **No remote, so no CI.** AC2's `ci` and AC6 can't be proven until the owner creates the GitHub repo. The Linux
   container is close to `ubuntu-latest` but isn't it (different kernel config, file system, runner load).
2. **Skip guard is a text scan.** It catches `skip`, `skipif` and `xfail` in the T05 test files only. A skip added by
   a `conftest.py` elsewhere wouldn't show; the CI log's pass count is the backstop.
3. **PATH under MSYS make on Windows.** Appending `C:\msys64\usr\bin` to PATH makes MSYS drop it for child processes,
   so T01's acceptance tests can't find `make`. Prepend it instead (environment only; no code change).

### Changes during the rerun build
- `_filelock.py` also gained `__all__`. The truncation test runs at five offsets (1 byte, inside a key, mid-line,
  before the closing brace, all but the newline).
- The Linux stand-in can't bind-mount the Windows worktree: NTFS files show as mode 0777, so ruff reports `EXE002`
  on every file (27 false errors), and the files have CRLF endings. It runs instead on
  `git -c core.autocrlf=false archive` of the staged tree, which has git's modes and LF endings, as a CI checkout does.

## Original plan (attempts 1 and 2, kept as written)

### Files that change
New (stdlib only, so `pyproject.toml` and `uv.lock` don't change and can't conflict with T04, which runs in parallel):
- `src/ultron/store/events.py`: `Event` (frozen dataclass: `schema`, `seq`, `time`, `actor`, `type`, `requirement`,
  `task`, `attempt`, `data`, `ultron_version`, plus `extra` for unknown fields), `NewEvent` (no seq/time/version yet),
  `to_json_line` / `from_json`, the `UPGRADES` rename table keyed by `schema`, and the typed constructors (AC5):
  `intake`, `gate_decision` (gate, decision, channel, comments; FR16), `attempt_started`, `attempt_finished`,
  `escalation`, `agent_call` (role, model, effort, duration_ms, tokens, cost_usd, transcript path, prompt/result
  sha256; FR17), `config_snapshot` (max_attempts, gates).
- `src/ultron/store/eventlog.py`: `EventLog(path, lock_path=None)` with `append(new_events) -> list[Event]`,
  `read() -> ReadResult(events, truncated_tail)`, `repair() -> bytes`; errors `TruncatedLogError`, `CorruptLogError`,
  `NewerSchemaError`; `events_path(root, requirement_id)` -> `sdlc/requirements/<id>/events.jsonl`.
- `src/ultron/store/_filelock.py`: an exclusive cross-process lock (`msvcrt.locking` polled with a timeout on
  Windows, `fcntl.flock` on Linux), raising `LockTimeout`.
- `tests/store/test_events_model.py`, `tests/store/test_eventlog.py`, `tests/store/test_events_concurrency.py`,
  `tests/store/fixtures/events_v0_0_1.jsonl`.

Choices for the plan gate to confirm:
1. **Actor** is one string: `auto`, `engine`, `agent:<role>` or `user:<git name> <email>`. A prefix keeps a git user
   called "engine" apart from the engine. Validated on construction.
2. **Two versions per line:** `ultron_version` (package version, recorded for audit) and `schema` (an int, 1 now).
   Upgrades key off `schema` only. The AC3 fixture says `0.0.1`, which is *newer* than today's `0.0.0`, so the reader
   must never compare `ultron_version`. A line with `schema` above the reader's raises `NewerSchemaError`.
3. **Renames:** `UPGRADES` is empty today (no older schema exists). The mechanism is tested with a patched table
   rather than an invented history.
4. **Lock file** defaults to `<path>.lock` beside the log; T07 should pass a path under `.ultron/locks/` so it is
   never committed (spec §10: locks live in `.ultron/`).
5. **Repair is explicit** (`repair()` cuts the file back to the last `\n` and returns the cut bytes for the caller to
   log). No automatic repair: the caller (T07 rollback or `doctor`) decides.
6. `type` stays an open string; constructors cover the AC5 set, and T08/T15 add their own types later.
7. Time is UTC ISO 8601 with milliseconds and `Z`; the clock is injectable for tests.

### Order of work
1. Tests first (red): model round-trips, seq 1..N, byte-prefix unchanged, fixture read, truncation, concurrency.
2. `events.py`: dataclass, JSON line format (UTF-8, compact separators, fixed key order, `\n` only via binary mode),
   unknown keys into `extra` and written back unchanged, constructors.
3. `_filelock.py`, then `eventlog.py`. `append` under the lock: check the tail (empty, or ends in `\n` and the last
   line parses), read the last `seq` by scanning back from EOF, stamp seq/time/version/schema, write the whole batch
   with one `os.write` on an `O_APPEND` binary fd, `fsync`, unlock. Never open the log in any mode but append/read.
4. `read`: parse each line, upgrade, check `seq` starts at 1 and steps by 1, report a tail without `\n` as
   `truncated_tail` (strict mode raises). A bad line in the middle raises `CorruptLogError` with the line number.
5. `make build`, `make lint`, `make test` (unset `UV`; PATH has uv and MSYS2). Paste the three result lines.

### Proof per AC
| AC | Proof |
|---|---|
| AC1 | `test_eventlog.py::test_seq_gap_free` (N=25 over several `append` calls gives 1..25) and `::test_prefix_bytes_unchanged` (sha256 of the first K bytes is equal before and after more appends). |
| AC2 | `test_events_concurrency.py`: 4 `subprocess` workers (`sys.executable -c`, start barrier via a file) append 50 each; assert 200 lines, every line valid JSON, `sorted(seq) == 1..200`. Runs on Windows and Linux CI. |
| AC3 | `test_eventlog.py::test_reads_old_version_fixture`: fixture lines with `ultron_version: "0.0.1"` and an unknown field read without error; `extra` holds it and re-serialising gives the same key/value. Plus `::test_rename_upgrade` with a patched `UPGRADES`, and `::test_every_event_records_version`. |
| AC4 | `test_eventlog.py::test_truncated_tail`: write good lines, then a partial line with no `\n`; `read()` reports `truncated_tail`, `append` raises `TruncatedLogError`, `repair()` leaves exactly the good lines and appends work again from the next seq. |
| AC5 | `test_events_model.py`: parametrized over all seven constructors; write through `EventLog`, read back, compare equal. |
| all | `make build` / `make lint` (mypy strict) / `make test` end with their success lines. |

### Risks
1. **Windows locking.** `msvcrt.locking` locks a byte range and its blocking mode gives up after ~10 s; I poll
   `LK_NBLCK` with a sleep and an overall timeout instead. The lock is on a sidecar file so readers are never blocked.
2. **Torn batch.** A crash can leave whole lines of a batch plus a partial one. The reader catches the partial line;
   dropping the complete-but-uncommitted lines is T07's job (reset the state worktree to its last commit).
3. **CRLF.** Text mode on Windows would write `\r\n`; binary I/O only. A test asserts no `\r` in the file.
4. **Concurrency test flakiness / slowness.** Subprocess start-up on Windows is slow (~1 s each); a 60 s timeout and
   a start barrier keep it deterministic.
5. **mypy strict with platform modules.** Branch on `sys.platform == "win32"` so mypy checks each OS's path on CI.
6. **pydantic vs dataclasses.** T04 likely uses pydantic; T05 uses dataclasses to avoid a lockfile change. Easy to
   switch after T04 merges if the owner wants one style.

### Changes during build
- `repair()` opens the log read-write only to `ftruncate` the partial tail and `fsync`; appends still use `O_APPEND`.
- A line with no `schema` key reads as schema 1 (the first), so a hand-written or early log isn't refused.
- `gate_decision` ties `actor == "auto"` to `channel == "auto"`; decisions are `approve`, `changes`, `reject` (spec §2).

## Not in this task
The `ultron/state` commit and its lock (T07), state folding (T08), the SQLite cache (T09), cost maths (T13).

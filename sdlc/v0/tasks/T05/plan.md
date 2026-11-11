# T05 plan: event log (`events.jsonl`), schema and versioned reader
Author: developer agent (R3; rerun at R4). Task: `tasks.md` §T05. Spec: §10, FR16, FR17, §5.3 "Updating". Depends on
T01 only.

**Rung:** R3 at the start (Risk 2: the source of truth, concurrent writers, data loss). Attempt 2 climbed to R4 and
passed. The rerun below stays at R4: a send-back to decomposition doesn't climb.

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

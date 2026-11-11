# T03 plan: verify first (TTY check, Agent SDK on the Pro login)

Rerun (2026-11-11, after the tasks.md revision): existing code kept. New: AC5 now needs a pytest that the SDK pin agrees in `pyproject.toml`, `uv.lock` and `findings.md`; AC6 is new (no PENDING in the Local section, owner fills). Task has `mcp: none`, nothing to wire.
Rung: **R1 kept** (rubric total 3). No reason to change: two small scripts, one test, no production code.
Status: plan step, no code written. Depends on T01 (done).

## Files that change
New:
- `spikes/local/tty_check.py`: stdlib only. Prints one line, `terminal` or `not a terminal (stdin=<bool> stdout=<bool>)`, plus `platform`, and exits 0 if `sys.stdin.isatty() and sys.stdout.isatty()`, else 3. The function `is_interactive()` is the exact method T39 will reuse (the plan for T39 should import or copy it).
- `spikes/local/sdk_check.py`: for each of `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` (model ids confirmed from the SDK/`claude --help` at code time, not guessed) run one tiny `query()` ("Reply with the word ok", `max_turns=1`, no tools, an `effort` option if the pinned SDK exposes one). Records per model: requested model and effort, the model the SDK's init/result message reports, effort applied if reported (else "not reported"), success or the error text, duration, and `ANTHROPIC_API_KEY` present/absent. Refuses to run (exit 2) if `ANTHROPIC_API_KEY` is set, so the call can only use the Pro login. Prints a markdown table the owner pastes into findings.
- `tests/spikes/test_sdk_pin.py`: AC5 pin agreement (pyproject, uv.lock, findings.md).
- `tests/spikes/test_tty_check.py` (+ `tests/spikes/__init__.py` only if needed by pytest config): AC1.
- `sdlc/v0/findings.md`: new file, a "Local" section with the T03 questions and result placeholders: TTY per shell (4 rows), Claude Code Bash tool row, SDK per-model table, API key unset, default adapter, pinned SDK version. Placeholders are filled after the owner's runs.
Edited:
- `pyproject.toml`: pin `claude-agent-sdk==<version>` (exact) in a new `[dependency-groups] spikes` group (not runtime yet; T16 moves it to dependencies). Version chosen at code time from `uv add --group spikes claude-agent-sdk` and recorded in findings.
- `uv.lock`: regenerated.
- Possibly `Makefile`: none. `make lint` already runs ruff on `spikes/`; mypy stays on `src` only.

## Order of work
1. Write `tests/spikes/test_tty_check.py` first (TDD), see it fail (no script).
2. Write `tty_check.py`; test passes.
3. `uv add --group spikes claude-agent-sdk`, pin exactly, `uv lock`.
4. Write `sdk_check.py` (importable `run_check(model, effort)` plus `main`). No real call by me.
5. Create `findings.md` skeleton. Run `make build`, `make lint`, `make test`.
6. Stop. Owner runs AC2-AC4; then findings and the adapter decision are filled in a follow-up commit (owner or a later step).

## Owner steps (AC2-AC4), in order
1. From the repo root, run `uv run python spikes/local/tty_check.py` in: PowerShell 5.1, Windows Terminal, Git Bash (Git Bash may need `winpty`; record "terminal" or "not a terminal" as it is), and Linux/WSL if available. Each must say "terminal".
2. In Claude Code, ask: "run `uv run python spikes/local/tty_check.py` with your Bash tool". Expect "not a terminal", exit 3.
3. In a shell where `ANTHROPIC_API_KEY` is unset and you are logged in to Claude Code, run `uv run --group spikes python spikes/local/sdk_check.py` once (3 small calls, Pro plan limits). Paste the table into `findings.md`.
4. Choose the default adapter: T16 (SDK) if all 3 models succeed on the login, else T17 (`claude -p`).

## Risks
- Git Bash (mintty) may report "not a terminal" for Python on Windows (no real console). If so, that is a finding: the gate fallback must be run from PowerShell or Windows Terminal. Do not paper over it.
- On Windows `isatty()` has returned True for the NUL device in old Pythons; Python 3.13 is fine, but the test also checks a pipe, not NUL.
- The SDK may not expose the effort actually applied, or may reject `effort`; record that as is. Findings may force a spec change (§19.10, §19.19).
- The SDK may need the `claude` CLI on PATH, or fall back to an API key silently; the script refuses when the key is set.
- Model ids may differ from the names in the task; wrong ids appear as per-model errors, not a crash.
- `uv.lock` / `pyproject.toml` conflicts with sibling worktrees; resolve at merge.
- Model calls spend Pro limits: only the owner runs step 3.

## Proof
| AC | Proof |
|---|---|
| AC1 | `tests/spikes/test_tty_check.py::test_piped_stdin_reports_not_a_terminal` runs the script via `subprocess` with `stdin=PIPE`, asserts "not a terminal" in output and returncode 3. Run by `make test`. |
| AC2 | Owner steps 1; results in `findings.md` Local section, each "terminal". |
| AC3 | `command`: run `uv run python spikes/local/tty_check.py` via Claude Code's Bash tool (done 2026-11-10, row in findings.md); owner confirms. Owner step 2; result in `findings.md`. |
| AC4 | Owner step 3; per-model table plus API-key-unset line in `findings.md`. |
| AC5 | `pytest` `tests/spikes/test_sdk_pin.py`: exact pin in `pyproject.toml`, same version in `uv.lock` and `findings.md`; `uv lock --check` exits 0. The default-adapter line is the owner's (AC6). |
| AC6 | Owner fills the cells; `grep PENDING` in the Local section must find none. Currently PENDING, expected until the owner runs. |
| Gates | `make build` ends `Build succeeded`, `make lint` `Lint succeeded`, `make test` `All tests passed`. |

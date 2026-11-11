# V0 findings

## Local: TTY and Agent SDK (T03)

Pinned SDK: `claude-agent-sdk==0.2.180` (dependency group `spikes`; T16 moves it to runtime).

### Owner steps
1. In PowerShell 5.1, Windows Terminal, Git Bash and WSL (if present), from the repo root run `uv run python spikes/local/tty_check.py`; record the output in the table below.
2. In Claude Code, ask: "run `uv run python spikes/local/tty_check.py` with your Bash tool". Expect "not a terminal", exit 3.
3. With `ANTHROPIC_API_KEY` unset and logged in to Claude Code, run `uv run --group spikes python spikes/local/sdk_check.py` once (3 small calls on Pro limits) and paste the table under "SDK per model".
4. Pick the default adapter: T16 (SDK) if all 3 models succeed on the login, else T17 (`claude -p`). Write it below.
5. Commit the filled `findings.md`.

### TTY per shell
| shell | result |
|---|---|
| PowerShell 5.1 | PENDING owner |
| Windows Terminal | PENDING owner |
| Git Bash | PENDING owner |
| Linux/WSL | PENDING owner |
| Claude Code Bash tool | `not a terminal (stdin=True stdout=False)`, exit 3 (run by the engine 2026-11-10). Note: stdin reports a TTY here, so a stdin-only check would be fooled; T39 must check both. |

### SDK per model
PENDING owner (paste the `sdk_check.py` table here)

- ANTHROPIC_API_KEY unset during the run: PENDING owner
- Default adapter: PENDING owner

## Host

Spike: `spikes/host/` (see its README). Every cell marked PENDING owner is filled after the owner steps; record what was observed, including "no".

| Question | Result | Claude Code version | Evidence | Implied decision for T36, T38, T41 |
|---|---|---|---|---|
| Elicitation: does a form appear, and can the host model answer it without the owner? | PENDING owner | PENDING owner | PENDING owner (screenshot path or description) | PENDING owner (e.g. unavailable: T38 uses the terminal fallback for every gate) |
| Notifications: are progress and resource-update messages shown? | PENDING owner | PENDING owner | PENDING owner | PENDING owner |
| `!` commands: does the PreToolUse hook see them? | PENDING owner | PENDING owner | PENDING owner (`hook.log` lines, incl. the model-issued Bash control) | PENDING owner |
| Version: which Claude Code version was tested? | PENDING owner (`claude --version`) | PENDING owner | PENDING owner | n/a |

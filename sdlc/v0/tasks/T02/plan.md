# T02 plan: verify first, host behaviour in Claude Code
Rung: R1 (kept). Rubric total 3: one throwaway spike, one self-test, the rest is owner observation. Nothing here touches production code.

## Files that change
- `pyproject.toml`: add `mcp` to the `dev` dependency group (the spike runs under plain `uv run python`, which ignores PEP 723 inline metadata). Pin to the current release; `uv.lock` updates with it (`uv lock`).
- `spikes/host/server.py` (new): stdio MCP server (`mcp.server.fastmcp.FastMCP`), one tool `spike_probe`. It (a) emits 3 progress notifications (`ctx.report_progress`, 1s apart), (b) sends a resource-updated notification for `spike://status`, (c) calls `ctx.elicit()` with a small form (`approve: bool`, `comment: str`) and returns the outcome. `--self-test` starts the server as a subprocess over stdio, runs `initialize` + `list_tools` through the SDK client, checks `spike_probe` is listed, prints `self-test ok`, exits 0 (non-zero otherwise, with a timeout).
- `spikes/host/hook.py` (new): PreToolUse hook. Reads the JSON event from stdin and appends one line (UTC time, tool name, command text, raw JSON) to `spikes/host/hook.log`. Always exits 0 and prints nothing, so it can never block. Logs every tool, not only Bash.
- `spikes/host/settings.snippet.json` and `spikes/host/README.md` (new, short): the exact `claude mcp add` command and hook settings to paste, and the owner steps below.
- `tests/test_spike_host.py` (new): runs `server.py --self-test` via subprocess; feeds `hook.py` a sample event and checks the log line and exit 0.
- `sdlc/v0/findings.md` (new): "Host" section, a table with one row per question (question | result | Claude Code version | evidence | implied decision for T36, T38, T41). Rows start as "pending owner"; filled after the owner steps.
- `.gitignore`: add `spikes/host/hook.log`. The spike is throwaway: ruff/mypy cover `src` and `tests` only, so no lint changes (check `ruff check` does not choke on `spikes/`; if it does, exclude it in `[tool.ruff]`).

## Order of work
1. Failing test first: `tests/test_spike_host.py` (self-test and hook cases). Confirm it fails because the files are missing.
2. Add the `mcp` dependency, `uv lock`.
3. Write `server.py` and `hook.py` until the tests pass.
4. Write the README, settings snippet and `findings.md` skeleton (version recorded: `claude --version` gave 2.1.322 here; the owner's value is what counts).
5. Run `make build`, `make lint`, `make test`. Stop; owner does AC2-AC4; then fill in findings.md and commit.

## Owner steps (AC2, AC3, AC4), about 5 minutes
1. Run the one command in `spikes/host/README.md` (`claude mcp add spike -- uv run python <abs path>\spikes\host\server.py`) and paste the hook block into `.claude/settings.local.json`. Restart Claude Code; run `claude --version`.
2. Ask Claude: "call the spike_probe tool". Note whether a form appeared (screenshot path or one line), whether progress and resource-update messages showed, and whether the model answered the form without you.
3. Type `! echo spike-bang-test`. Open `spikes/host/hook.log` and note whether the line is there. Tell me (or edit `findings.md`) with the three results.

## Risks
- `mcp` SDK version may lack `ctx.elicit` or changed its signature; pin a recent version and check the installed API before writing. Fallback is the low-level `session.create_message`-style `elicitation/create` request.
- Windows path with spaces in the owner's home: the registered command uses the absolute worktree-free repo path and `uv run`; set `UV_PROJECT_ENVIRONMENT` outside the home folder if registration breaks (see CLAUDE.md note).
- Hook runs as a Python process on every tool call: keep it dependency-free (`python hook.py`), exits 0 on any exception.
- A "no" outcome is valid evidence; findings must record the observed fact, not the hoped-for one. A hook log with the `!` line missing may also mean the hook was not loaded: the owner also runs one model-issued Bash call as a control.
- Parallel sibling worktrees edit `pyproject.toml`/`uv.lock` too: expect a trivial merge conflict on the dependency list.

## Proof (revised criteria, by verify_with)
- AC1: `command` `uv run python spikes/host/server.py --self-test` (exit 0) and `mcp-client` `tests/acceptance/test_t02.py::test_ac1_server_lists_tool_over_stdio` (scripted stdio client lists exactly `spike_probe`); `tests/test_spike_host.py` covers the same plus the hook.
- AC2: [owner] owner steps 1-3; form result and screenshot path go in `findings.md` row 1. README step 3 now says to save a screenshot and record its path. Optional preview: MCP Inspector via `npx` (free, not connected) to see the elicitation request; it does not replace the Claude Code check.
- AC3: [owner] owner step 3; row 2, screenshot path recorded.
- AC4: [owner] owner step 4 plus control call; `hook.log` lines quoted in row 3.
- AC5: `tests/acceptance/test_t02.py::test_ac5_*` and `tests/test_spike_host.py::test_findings_host_table`: Host table has 5 columns and one row per question.
- AC6: [owner] owner fills every PENDING cell; the check is `grep -c PENDING sdlc/v0/findings.md` is 0 in the Host section and each named screenshot path exists. Cannot pass until the owner steps are done; README step 5 states the rule.
- All commits gated on `make build`, `make lint`, `make test` success lines.

## Departures from plan (code step)
- `mcp` is pinned `<2` (resolved 1.30.0): mcp 2.x removed `mcp.server.fastmcp` (renamed `MCPServer`, different elicit flow). `ruff` excludes `spikes/` (`[tool.ruff] extend-exclude`).
- Owner steps in the README are 5 numbered steps; step 4 includes the model-issued Bash control call.


# V0 self-build: where we stopped (2026-11-10, evening)

**Next step, when the owner says go:** rerun the developer agents for the 8 tasks below from the revised `tasks.md`, keeping their code. Check `/usage` with the owner first and pick how many to run at once.

## How the build runs
- The main Claude Code session plays ultron's engine. Subagents play ultron agents at their spec settings (§11.4).
- Each task has a worktree at `C:\git\.ultron-work\v0-T<k>` on branch `v0/T<k>`. The working branch is `v0-design`. Gate decisions are logged in `decisions.md` and lessons in `lessons.md`.
- **Gates:**
  - auto: plan, tasks
  - human: result, merge, deploy
- **Attempts:**
  - Max 3, and each counted failure climbs one rung. A send-back doesn't climb.
  - The testing agent never runs below R1, and developers never edit `tests/acceptance/` (FR11).
- **Rungs (self-build only, owner 2026-11-11):** spec §11.4's R0–R5, plus **R6 = Fable 5.2 high**. Fable is used **only** when a task has failed at the highest rung, R5 (Opus max); a task never starts at R6 and doesn't reach it by any other route. `spec.md` is unchanged; ultron itself still keeps Fable off the ladder.
- Jira is used only on escalation.
- **Jira for testing (owner 2026-11-11):** tests and live checks never use AD (adhya-work), not even read-only. They use project **AK (anil-ws)** on the same site. Fixtures come from the public REST API shapes with placeholder keys.
- **MCPs (owner 2026-11-11):** every developer and testing agent prompt includes the task's `mcp` line from `tasks.md`. If it names a connected MCP, the agent must use it for the check it names and report what it saw. If it names one that isn't connected, the agent sets it up only if it costs ₹0 (e.g. `npx`), otherwise says so. `none` means no MCP.
- **Scheduling (FR6):** no waves. A task starts as soon as all its dependencies are approved, up to `max_parallel`.
- MCPs connected: Playwright, git, Docker MCP Toolkit, Atlassian Rovo, GitHub.

## The rerun (owner decision 2026-11-10, lesson L3)
The owner sent T35's result back to **decomposition**. `tasks.md` was revised to add `verify_with` per criterion, `mcp` per task and 31 new criteria, and auto-approved. Every task with code reruns: first merge `v0-design` into its worktree, then plan (auto gate) → code → test using each criterion's `verify_with` → engine rerun of make → owner's result gate.

| Task | What | Rung | State before the rerun |
|---|---|---|---|
| T01 | Python stack | R0 | Merged; reopen. AC9 already passes from PowerShell. AC8 (CI) needs a GitHub remote |
| T02 | Host checks | R1 | Tests pass; owner steps pending |
| T03 | TTY and SDK checks | R1 | Tests pass; owner steps pending. Claude Code's Bash tool result recorded |
| T04 | ultron.toml config | R1 | Passed attempt 1; no new criteria |
| T05 | Event log | R4 | Passed attempt 2 |
| T06 | Git layer | R4 | Attempt 2 coded; its tester saw about 10 failures before it was stopped; partial tests uncommitted in the worktree |
| T14 | Version bumps | R0 | Passed attempt 1; no new criteria |
| T35 | Mascot | R1 | Attempt 2 fixed the colour bleed; untested; partial tests uncommitted in the worktree |

## Open items for the owner
- **GitHub remote:** none yet, so the 20 `ci` criteria can't be proven. Decide whether to create the repo (public or private).
- **Cap defaults in T04:** $5 per requirement and $50 per eval run, the developer's proposal.
- **Steps to run:** T02 and T03 owner steps, and the T35 look in a terminal.

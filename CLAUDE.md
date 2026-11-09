# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# ultron

An AI-native SDLC pipeline that is **injected into coding agents**, not a standalone app: an MCP server plus a package for each host (the Claude Code plugin in V0: MCP registration, `/ultron:*` commands, a skill and a guard hook). It drives its own worker agents (Claude Agent SDK by default, swappable per role) over a **target** git repo through intent, spec, tasks, plans, code and tests, with human gates. Workers communicate only through the engine (broker). The human approves through MCP elicitation forms, or the terminal-only `ultron gate` fallback, never through a model. The brief is `sdlc/v0/intent.md`. The method is Anthropic's *AI-Native SDLC Playbook* (the PDF in the repo root), applied the way the owner applied it in `C:\git\social-grid`, which you can use as a reference for patterns.

Status: pre-implementation. There's no stack, build or tests yet. When the stack is chosen, add a Commands section like social-grid's: `make build`, `make lint` and `make test`, each ending in a fixed success line.

## Terms (decided 2026-11-09)
- **Requirement:** one `intent.md` and everything derived from it. Results and spending caps are per requirement. The intent calls this a "ticket".
- **Task:** one unit of work split from a requirement, with its own plan, worktree, attempts and tester gate.
- **Ticket:** only a Jira issue, created on the escalation board when a task fails.

## How we work: the AI-native SDLC (playbook)
Every stage commits an artifact the next stage reads, and the owner approves each one. Cycles live in `sdlc/v<version>/` (V0 first).
1. **Plan:** propose the next version from the intent (big feature: 1→2; small feature: 1→1.1; small tweak or fix: 1.1→1.1.1) and the owner confirms it. Then `intent.md` (Author/Status line; Problem, Proposed outcome, Affected users and systems, Constraints, Open questions).
2. **Design:** `spec.md` from the approved intent. Resolve every open question in the intent (V0: §10) with the owner, and flag concerns and conflicts. Record decisions in it, with the date. **Stop for review.**
3. **Tasks:** `tasks.md`, split from the approved spec (id, title, description, acceptance criteria, depends-on, ships-independently). **Stop for review.**
4. **Build:** for each task, start in plan mode and commit its `plan.md` (files that change, order of work, risks, proof) before any code. If the code departs from the plan, update `plan.md` in the same commit. Build one task at a time, and stop for review after each.
5. **Test:** TDD. For a bug, first commit a test that fails for the expected reason, then fix the code, never the test. At most 3 rounds, then stop and hand over to the owner.
6. **Deploy:** work on a branch and never push to `main`. The PR is the gate, and the owner reviews and merges. Planned to follow social-grid: a `REVIEW.md` with Bugs, Security and Compliance passes (advice only), a hook that opens the PR when all checks pass, and evals gating changes to CLAUDE.md, `.claude/` and the pinned model.
7. **Maintain:** anything found becomes the next cycle's `intent.md`.

When Claude makes the same mistake twice, the correction goes under "Things Claude gets wrong" below. Keep this file to about a page.

## Verifying your work
Run build, lint and test before reporting anything done, and paste the result lines. Never skip, weaken or delete a failing test.

## Budget
₹12,000 a month is the cap across ultron and social-grid together (raised from ₹3,500 on 2026-11-09). ultron's agent calls run on the owner's Claude subscription (Claude Code headless, Pro plan), so they use plan limits, not money. Before adding anything that costs money, state its monthly cost in INR and stop for approval. Before a long run (for example an evaluation), estimate the usage and stop for approval.

## What ultron must enforce (from `intent.md`)
The system enforces these, not the agents:
- Flow: intent → spec agent → **architect gate** → decomposition (task DAG) → per task, in parallel in isolated worktrees: plan → **plan gate** → up to `max_attempts` attempts (code, then tests; the user chooses the limit, default 3) → **tester gate** → human merge in dependency order → human deploy (a script) → the maintenance agent files a new `intent.md`.
- A revised plan goes back through the plan gate. Time waiting at a gate isn't an attempt. Failed or rejected tasks become Jira tickets on the escalation board (a Mon–Fri sprint) with all attempt logs.
- Agents never pass a gate, merge or deploy. The maintenance agent never edits code.
- The user chooses which gates are human or auto per repo or requirement (spec §6). `merge` and `deploy` are always human. Every auto decision is recorded as `auto`.
- Every artifact is a file on disk. Every gate decision records who, when and comments. Every agent call is logged with inputs, outputs, duration and tokens/cost.
- The agent layer stays behind a small swappable interface. Spending caps apply per requirement and per evaluation run.
- Evaluation harness: replay closed OSS issues at the pre-fix commit, judged by the real fix's tests, which the agents never see. One command produces a report file.

## Things Claude gets wrong
- PowerShell 5.1 breaks quoted arguments to git. Write the commit message to a file and use `git commit -F <file>`.
- The owner's user folder path has spaces (`C:\Users\Anil Kumar ML`). Tools that break on that need a work directory without spaces, such as `C:\git\.<name>-work`.
- If PowerShell says `make` isn't recognized, add MSYS2 for that command: `$env:Path += ";C:\msys64\usr\bin"`.

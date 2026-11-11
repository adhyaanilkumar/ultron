# Backlog: features for later versions

Ideas and decisions for versions after V0. Nothing here is built until it becomes the `intent.md` of a later cycle and
goes through the normal stages. Each entry records what was decided and when.

## Web page for the gates (planned for v2)
Decided 2026-11-09 by the owner:
- **Frontend:** social-grid's frontend stack: React 19 + Vite, Tailwind and radix-ui.
- **Backend:** a thin Python API (FastAPI) inside ultron. It calls the same engine functions as the CLI, so the gate
  rules exist in one place only. Not Spring Boot: that would mean writing the rules a second time in Java and moving
  state off SQLite.
- **Scope:**
  - the three gates (spec, plan, result) with approve, reject or change buttons and a comments box;
  - the plan, the test evidence and the diff shown in the browser;
  - `ultron status` as a page.
- **Launch:** `ultron serve` runs it locally. The CLI stays fully usable.
- **Theme:** the same palette and mascot as the CLI (spec §11).

## Other coding agents (Codex first)
Codex matters in two separate ways, and they can come separately:
- **As a host (ultron injected into Codex):**
  - register the same ultron MCP server in Codex's MCP config;
  - give Codex its instructions in Codex's own way, in place of the Claude Code skill;
  - add a guard if Codex supports one.

  The core doesn't change. Check whether Codex shows MCP elicitation forms. If it doesn't, approvals use the
  terminal fallback.
- **As an ultron agent (ultron running Codex for a role):**
  - add a new `AgentRunner` adapter that runs `codex exec` non-interactively in the task worktree;
  - give it the broker's ultron agent tools through its MCP config, so it talks to other ultron agents the same way
    Claude-based ones do (spec §11.2);
  - choose it per role in `ultron.toml`.

Verify Codex's current non-interactive and MCP options when this is planned.

## Multi-project registry
A list of target repos in the ultron home, with totals across repos, and `ultron serve` covering all of them. V0 uses
`--repo` instead.

## Reviewer agent at the gates in evaluation runs
An optional agent that reviews specs and plans when the gates are on `auto`. That would make the send-back
statistics measurable in unattended runs (spec §15.2).

## Team sync (multiple people on one repo)
Decided 2026-11-09: V0 already keeps state as a git-committed event log (spec §5, D10). This feature adds sharing:
- **Sync:** every ultron command pulls `ultron/state` before acting and pushes after writing an event.
- **Collisions:** a rejected push (someone pushed first) means pull, recheck, and retry. A git push to one branch is
  atomic, so this works as an optimistic lock without a server.
- **Task claims:** `task.claimed` events, with an expiry, so two people never run the same task. Agent calls run on the
  claimer's machine with their own Claude login.
- **Agent transcripts** stay local. Only call summaries are shared.

Alternative for later: a central `ultron serve` with Postgres, combined with the v2 web page, for live multi-user use.

## Direct peer-to-peer messaging between agents (backup)
Decided 2026-11-09 as the backup to the engine broker (spec D18): agents message each other directly, for example
with the A2A (Agent2Agent) protocol. Use it only if brokered messaging proves too limiting. Before adopting it, work
out how gates, limits and the audit trail would still be enforced, because the broker currently guarantees all three.

## Hosted HTTP MCP server
- **What:** one shared ultron server that hosts connect to by URL, instead of each user running their own copy (spec
  §5.3).
- **Why later:** useful for teams sharing one instance. It needs a server, a domain, user login and access control,
  and its monthly cost must be stated in INR and approved against the ₹4,300 cap first.
- **Depends on:** team sync and team roles.

## Team roles and permissions
Decided 2026-11-09 to keep V0 single-user (spec D19). Later, different people could hold the human roles (architect,
developer, tester), and each gate would check that the person deciding has that role. This builds on team sync above.


## Tool research per task (MCP scout)
Decided 2026-11-10 by the owner, from the V0 self-build (lessons L1 and L2, and the MCP research run):
- **What:** after decomposition, a research step finds the best MCP servers (or none) to **build and verify** each task. It works the way the intake scout does: it cites facts and sources and picks nothing. The decomposition agent then sets each task's `mcp` and each criterion's `verify_with` from that research.
- **Tell the user what they have:** ultron checks the host's configured MCP servers and shows, per task:
  - **have:** connected and ready;
  - **missing:** recommended but not connected, with how to add it, its monthly cost in INR (stated before anything that costs money, CLAUDE.md budget rule) and any security caveat, such as a token with write access;
  - **none needed.**
- **Where it shows:** at the `tasks` gate (even when that gate is `auto`, the report is recorded and shown in `ultron_status`), and in `/ultron:doctor`.
- **Rules:**
  - The user decides whether to add a missing MCP. ultron never installs or registers one itself.
  - A task whose `verify_with` needs a missing tool falls back to an `owner` check and is flagged.
  - An MCP never replaces ultron's own engine checks. Tests of ultron's code still run against ultron's code.
- **Why:** T35's mascot passed 80 tests but looked wrong on screen, because no criterion said how it had to be checked (lesson L1). In social-grid the owner picked the 21st.dev MCP for UI by hand. This makes that choice part of the pipeline.

## Send back to an earlier stage (from any gate)
Decided 2026-11-10 by the owner (lesson L3 in `sdlc/v0/lessons.md`):
- **What:** every gate form gets a "send back to" choice: intake, spec, decomposition or plan, plus a required comment. Today a gate can only approve, request changes to the same stage, or reject.
- **Engine behaviour:**
  - It reruns the chosen stage's agent with the comment.
  - It marks every artifact and task built on the old version **stale**.
  - It shows the user which tasks are affected and the estimated usage before rerunning.
  - Each affected task reruns plan → code → test from the new artifact, starting from its existing code.
  - Merged tasks reopen.
  - A send-back isn't a failed attempt and doesn't climb a rung.
- **Recorded** as a `gate.sent_back` event (who, when, from which gate, to which stage, comment), in `decisions.md`.

## Resume an agent's session after a usage limit
Decided 2026-11-11 by the owner (lesson L11 in `sdlc/v0/lessons.md`):
- **What:** record each attempt's Agent SDK session id in the event log. After a usage limit, crash or reboot, `ultron resume` reopens that session so the agent continues mid-thought, instead of rerunning the attempt in a fresh session (spec §11.4 today).
- **Engine behaviour:**
  - The resumed session keeps the attempt's number and rung; the stop isn't counted.
  - If the session can't be reopened (expired, missing, different adapter), fall back to rerunning the attempt from the worktree as now, and record which happened.
  - `ultron_resume_task` for a `held` task uses the same path, adding the user's note as a new message.
- **Why:** in the V0 self-build, T06 (R5) and T25 hit the 5-hour limit mid-attempt and were resumed from where they stopped, which saved repeating the expensive part of the run.

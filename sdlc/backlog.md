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
  and its monthly cost must be stated in INR and approved against the ₹12,000 cap first.
- **Depends on:** team sync and team roles.

## Team roles and permissions
Decided 2026-11-09 to keep V0 single-user (spec D19). Later, different people could hold the human roles (architect,
developer, tester), and each gate would check that the person deciding has that role. This builds on team sync above.


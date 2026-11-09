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

## Other coding agents
New adapters behind the `AgentRunner` interface (spec §5), alongside Claude Code. Which agents is still to be decided.

## Claude Code plugin front-end
An optional plugin that provides `/ultron:*` commands. They call the CLI, so the CLI stays the only authority. It may
also add PreToolUse hooks as a second guard on top of the engine's own checks.

## Multi-project registry
A list of target repos in the ultron home, with totals across repos, and `ultron serve` covering all of them. V0 uses
`--repo` instead.

## Reviewer agent at the gates in evaluation runs
An optional agent that reviews specs and plans when the gates are on `auto`. That would make the send-back
statistics measurable in unattended runs (spec §15.2).

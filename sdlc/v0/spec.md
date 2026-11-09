# Spec: ultron V0
Author: Claude (from `intent.md`), for adhyaanilkumar. Cycle: V0. Status: **draft (rewrite 2), awaiting review**.
Date: 2026-11-09.

ultron is an **MCP server** that runs an AI-native SDLC pipeline on a target git repository. The user works with it
from an MCP client, which is Claude Code in V0, in plain conversation:
1. The user describes what they need.
2. ultron turns it into a requirement, a spec, tasks and plans.
3. Worker agents code and test each task.
4. The user approves at the gates and merges and deploys.
5. After a release, a maintenance check turns new failures into new requirements.

An evaluation harness measures how often this works on real, closed GitHub issues.

---

## 1. Decisions

**Confirmed** means the owner decided it on 2026-11-09. **Proposed** means Claude recommends it and the owner should
confirm or change it during this review.

| # | Decision | Status |
|---|---|---|
| D1 | Language: **Python**. | Confirmed |
| D2 | **ultron is an MCP server.** The user talks to it from Claude Code. Being a standard MCP server, other MCP clients can use it too. A small CLI remains for setup, health checks, evaluation, scheduled maintenance and as a fallback for approvals (§5). | Confirmed |
| D3 | **Worker agents are ultron's choice, made in config.** ultron runs its own worker agents (intake, spec, decomposition, developer, testing, maintenance) behind one `AgentRunner` interface. The default and only V0 adapter is **Claude Code headless on the user's Claude subscription**. Other coding agents are later adapters, selected in `ultron.toml`. | Confirmed |
| D4 | Terms: **requirement** = one `intent.md` and everything derived from it; **task** = one unit of work split from a requirement; **ticket** = only a Jira issue. Where the intent says "ticket" (§6, §8), this spec says "requirement". | Confirmed |
| D5 | Failed tasks become **Jira tickets** on a Mon–Fri sprint (§12). This replaces the intent's CLI or web escalation board. Intake stays local, never through Jira, so intent §7 still holds. | Confirmed (Jira); details proposed |
| D6 | **Hybrid intake.** The user writes a template `intent.md` or just describes the need. Free text goes to the intake agent, and the draft is approved at the `intent` gate (§7.1). | Confirmed |
| D7 | **The user chooses** which approvals are human or automatic, and the dev→test attempt limit (`max_attempts`, default 3). `merge` and `deploy` are always human (§8). | Confirmed (feature); defaults, range and locks proposed |
| D8 | **Approvals come from the human, never the model.** The preferred channel is an MCP **elicitation** form that Claude Code shows to the user. The model can't fill it in. The fallback is the `ultron gate` CLI command, protected by a guard hook (§8). | Proposed |
| D9 | **Long work runs outside the chat.** Tools return immediately. A detached `ultron run` worker advances a requirement until it needs a human, then stops. Closing Claude Code doesn't stop work in progress (§9). | Proposed |
| D10 | **State is an append-only event log committed to git.** SQLite is a local cache rebuilt from it. V0 is single-user, and team sync is in the backlog (§10). | Confirmed |
| D11 | ultron commits each requirement's artifacts and event log to an `ultron/state` branch in the target repo. `ultron_merge` brings the requirement's folder into the target branch with its code. | Proposed |
| D12 | ultron **proposes each release's version** from the intent: major, minor or patch, as in social-grid. A person confirms it at the spec gate (§13). | Confirmed |
| D13 | ultron checks that the user is **signed in to a Claude Code account** before any worker runs (§14). | Confirmed |
| D14 | Look: the owner's Ultron pixel-art mascot in gunmetal with `#941212` eyes, a crimson `#E5484D` wordmark, and the owner's palette (§18). | Confirmed |
| D15 | Evaluation: **SWE-bench Verified, sympy (75) and pytest (19)**, with a **5-requirement pilot** first. Evaluation runs don't create Jira tickets unless asked (§16). | Proposed |
| D16 | Worker model: pinned in `ultron.toml`, default `claude-sonnet-5-5` for every role, and overridable per role. | Proposed |

Features for later versions are in `sdlc/backlog.md`: the web page, ultron's own chat screen, team sync, other
agents, the registry, and the eval reviewer agent.

---

## 2. How it's used (V0 walkthrough)

```
In Claude Code, with the ultron plugin installed:

you    > customers keep phoning to ask where their order is, let them see it in the app
claude   (calls ultron_submit)  ultron's intake agent has 2 questions: only logged-in users? delivery dates too?
you    > logged in only, yes dates
claude   (calls ultron_answer)  Draft intent R-20261109-a3f2 is ready.
         ┌ ultron · approve intent R-20261109-a3f2? ──────────────── [Approve] [Request changes] ┐  ← elicitation form,
         └ comment: ____________________________________________________________________________┘    answered by you
...later...
you    > what's waiting on me?
claude   (calls ultron_status)  The spec is ready (proposes v1.1.0, a minor bump), and 2 plans need review.
you    > show me the spec
claude   (calls ultron_show) …summarises it, answers questions…
you    > ok let me review it
claude   (calls ultron_review)  → elicitation form: Approve / Request changes / version / comment
```

Workers run in the background between those steps. `ultron_status` shows progress, and Claude Code shows a message
when something new is waiting.

---

## 3. Actors and lifecycle

| Actor | Type | In ultron |
|---|---|---|
| Requester | Human | Describes the need or supplies `intent.md`, and approves an intake draft |
| Intake agent | Worker | Turns free text into `intent.md` and asks up to 5 clarifying questions. It adds no scope and has no tools. |
| Spec agent | Worker | Writes `spec.md` and proposes the version bump |
| Architect | Human | The `spec` gate |
| Decomposition agent | Worker | Writes `tasks.md` and `tasks.json` (the dependency graph) |
| Developer agent | Worker, one per task | Writes `plan.md`, then changes code following the approved plan |
| Testing agent | Worker, one per task | Writes and runs tests against the acceptance criteria |
| Developer | Human | The `plan` gate |
| Tester | Human | The `result` gate |
| Developers | Human | Work the escalated Jira tickets |
| Maintenance agent | Worker | Diagnoses a detected failure and writes `intent.md`. It never edits code or deploys. |
| The chat model (Claude in Claude Code) | Client | Calls ultron's tools for the user. **It never decides a gate.** |

**Requirement states** (only the engine changes them):
`submitted → [intake_drafting → awaiting_intent] → spec_drafting → awaiting_spec → (changes → spec_drafting)* →
spec_approved → decomposing → [awaiting_tasks] → in_progress → complete | partial | escalated`.
The bracketed steps happen only when they apply.

**Task states:**
`pending (deps unmet) → planning → awaiting_plan → (changes → planning)* → plan_approved → attempt k (coding →
testing) → passed → awaiting_result → approved | rejected`, plus `failed` after `max_attempts` attempts.
`rejected` and `failed` lead to `escalated`. A task whose dependency is escalated becomes `blocked` and is never
merged on its own.

---

## 4. Functional requirements

**Setup**
- **FR1** `ultron init --repo <path>` prepares a target repo:
  - writes `ultron.toml`;
  - gitignores `.ultron/`;
  - creates the `ultron/state` branch;
  - registers the MCP server with Claude Code (through the plugin, §5.2).

**Intake and spec**
- **FR2** `ultron_submit` takes a template `intent.md` (a path or its text) or free text (§7.1). It returns the new
  requirement id at once.
- **FR3** The spec agent reads the intent and the target codebase (read-only) and writes `spec.md`. The spec has
  requirements, design, acceptance criteria, flagged concerns, and a proposed version bump with its reason.
- **FR4** At the `spec` gate the user approves (optionally changing the version) or requests changes with comments.
  When changes are requested, the spec agent runs again. Nothing downstream starts before approval.

**Tasks and plans**
- **FR5** The decomposition agent writes `tasks.md` and `tasks.json`. Each task has id, title, description,
  acceptance criteria, `depends_on` and `ships_independently`. The engine rejects a cycle in the graph or an unknown
  dependency and reruns the agent, once.
- **FR6** Tasks whose dependencies are all approved run in parallel (`max_parallel`, default 2), each in its own git
  worktree and branch. A dependent task starts from its dependencies' approved work, merged in by the engine (never
  by an agent).
- **FR7** The developer agent writes `plan.md` (files that change, order of work, risks, proof). No code is written
  before the `plan` gate approves it.

**Attempts** (the engine enforces all of these)
- **FR8** One attempt has three steps:
  1. The developer agent codes to the approved plan.
  2. The testing agent writes tests for the acceptance criteria and runs them.
  3. The engine reruns those tests plus the repo's own test command and decides pass or fail from the real exit codes.
- **FR9** If an attempt fails, the engine's test output goes back to the developer agent. A revised plan goes back
  through the `plan` gate, and the attempt count is kept.
- **FR10** A task has at most `max_attempts` attempts:
  - the user chooses the number: default 3, range 1–10, set per repo or per requirement;
  - it's fixed for each task when the task is created;
  - time waiting at a gate isn't an attempt.
- **FR11** If a developer agent's diff changes a test file the testing agent wrote, that attempt fails.

**Result, escalation, merge and deploy**
- **FR12** At the `result` gate the user sees the plan, the test evidence and the diff, then approves or rejects.
- **FR13** A task that fails `max_attempts` times, or is rejected, is escalated to Jira (§12).
- **FR14** `ultron_merge` (always human-confirmed) merges approved task branches into the target branch in dependency
  order, together with the requirement's `sdlc/` folder. It skips tasks with an escalated dependency and stops on a
  conflict.
- **FR15** `ultron_deploy` (always human-confirmed) runs the configured deploy script, records the release (version,
  commit, time, who, requirements and tasks) and tags `v<version>`.

**Records, caps and status**
- **FR16** Every gate decision is an event: who (the git identity, or `auto`), when, the channel (form or CLI), the
  decision and the comments. `decisions.md` is generated from the events.
- **FR17** Every worker call is logged: role, requirement, task, attempt, prompt, result, duration, tokens and an
  API-equivalent cost estimate. The full transcript is kept locally, and a summary goes in the event log.
- **FR18** Spending caps (API-equivalent USD) apply per requirement and per evaluation run. The engine checks the
  total before each worker call. If a requirement hits its cap, its unfinished tasks are escalated.
- **FR19** `ultron_status` shows requirements, tasks, states, attempts (`2/3`) and what's waiting on the user.

**Maintain**
- **FR20** A scheduled `ultron maintain` checks the released version and files linked intents for new failures (§15).

---

## 5. Architecture

```
 Claude Code (chat UI, MCP client) ── user types; elicitation forms come back to the user
        │  MCP over stdio
        ▼
 ultron MCP server ── tools (§6) ── engine (state machine, gates, attempt limit, caps)  ← the authority
        │                                   │ writes events + artifacts (one commit per step)
        │ starts, detached                  ▼
        ▼                          target repo: ultron/state branch (event log, sdlc/ artifacts)
 ultron run <requirement>  ── worker process: advances until a human is needed, then exits
        │
        ▼
 AgentRunner ── Claude Code headless adapter (V0) ── worker agents in task worktrees
 ultron CLI ── init · doctor · eval · maintain · gate (fallback) · run · resume
```

### 5.1 Components
| Component | Job |
|---|---|
| `mcp/` | The MCP server: tools, elicitation forms, resource links to artifacts |
| `engine/` | States, gates, attempt limits, caps, dependency order. It's the only thing that changes state. |
| `runner/` | `ultron run`: a detached worker per requirement, with a lock, resume and scheduling of parallel tasks |
| `agents/` | The `AgentRunner` interface, the Claude Code adapter, prompts per role and output schemas |
| `store/` | The event log (source of truth), the SQLite cache and the artifact writer |
| `git/` | Worktrees, branches, merges and diffs |
| `intake/` | The template check and the intake flow |
| `jira/` | The escalation client |
| `maintain/` | Checks, the log reader, fingerprints and the intent writer |
| `eval/` | Dataset prep, runs, grading and the report |
| `cli/` | Setup, doctor, eval, maintain, the gate fallback, run and resume |
| `plugin/` | The Claude Code plugin: MCP config, `/ultron:*` commands, a skill and the guard hook |

### 5.2 The Claude Code plugin
The plugin is how a user installs ultron into Claude Code. It contains:
- the MCP server registration;
- slash commands: `/ultron:status`, `/ultron:submit` and `/ultron:review`;
- a skill that explains ultron to the chat model: the gates, and that only the user approves;
- a **guard hook** that blocks the model's own shell calls to `ultron gate`, `ultron merge`, `ultron deploy` and
  `ultron gates set`.

---

## 6. MCP tools

| Tool | Does | Human-only? |
|---|---|---|
| `ultron_submit` | Starts a requirement from a file or text, and returns its id | No |
| `ultron_answer` | Passes the user's answers to the intake agent's questions | No |
| `ultron_status` | Requirements, tasks, states, and what's waiting on the user | No (read) |
| `ultron_show` | Any artifact: request, intent, spec, tasks, a plan, a diff, test evidence, an attempt log, decisions | No (read) |
| `ultron_explain` | Why an attempt failed or a task was escalated, from the engine's records | No (read) |
| `ultron_settings` | The gate settings and `max_attempts` in force | No (read) |
| `ultron_review` | Opens the review form for something waiting at a gate | **The decision is the human's**, through the form |
| `ultron_merge` | Merges a requirement's approved work | **Human-confirmed** through a form. Locked. |
| `ultron_deploy` | Runs the deploy script and records the release | **Human-confirmed** through a form. Locked. |

**No tool takes a decision as an argument.** For example, `ultron_review(id)` doesn't accept `approve=true`. The
decision only comes back from the form the user fills in. Changing gate settings or `max_attempts` is done in
`ultron.toml` or with the CLI, never through a tool.

**Tool results are untrusted data.** They include text from the target repo (specs, diffs, issue text), which could
contain instructions. Results are labelled as data. Because no tool lets the model decide a gate, injected text can't
pass one.

---

## 7. Intake

### 7.1 Hybrid intake
- **Stored first:** the submission is saved byte for byte as `sdlc/requirements/R<n>/request.md`.
- **Template check (no AI):** a parser looks for the five headings (Problem, Proposed outcome, Affected users and
  systems, Constraints, Open questions). If they are all there, the file becomes `intent.md` unchanged and the spec
  step starts.
- **Intake agent:** used for free text, or a file that fails the check.
  - It drafts the template, reusing the user's phrases.
  - It asks at most 5 questions. The chat model relays them, and `ultron_answer` returns the answers.
  - It adds no scope. Inferences are labelled "Assumption:", and unknowns go to Open questions.
  - Non-English input (e.g. Hindi or Hindi–English) gives an English intent that quotes the original lines.
  - It saves `intake.json`: the questions, answers, model and usage.
- **The `intent` gate:** this applies only when the agent wrote the intent. The form shows the request and the draft
  side by side. The engine records time to intent, and whether the draft was approved unchanged (the playbook's
  survival rate).
- **Machine-written intents skip the intake agent:**
  - maintenance intents get the template check, then the `maintain` gate;
  - evaluation issues use the raw issue text.

### 7.2 Requirement ids
Requirement ids are collision-free, `R-<yyyymmdd>-<4 hex>` (for example `R-20261109-a3f2`), so team sync can come
later without renaming anything. This spec writes `R<n>` for short.

---

## 8. Human approvals

| Gate | What's approved | Default | Can it change? |
|---|---|---|---|
| `intent` | An intent the intake agent drafted | human | yes |
| `spec` | `spec.md` and the proposed version | human | yes |
| `tasks` | The task split and its dependencies | auto | yes (an extra human gate) |
| `plan` | Each task's `plan.md` | human | yes |
| `result` | Each tested task: diff, plan and evidence | human | yes |
| `escalation` | Confirm before a Jira ticket is created | auto | yes (an extra human gate) |
| `maintain` | Accept a maintenance intent | auto | yes (an extra human gate) |
| `merge`, `deploy` | Merging, and running the deploy | human | **no: locked**, because the intent requires a human action |

**Choosing the settings**
- **Per repo:** in `ultron.toml`. **Per requirement:** at submit. **With the CLI:** `ultron gates set …`.
- **Presets:** `strict` (every changeable gate human), `default` (the table above) and `unattended` (every changeable
  gate auto). Evaluation uses `unattended`.
- **Snapshot:** a requirement keeps a snapshot of its settings from when it was submitted. A mid-run change applies
  only to gates it hasn't reached yet, and is recorded.
- **Warning:** switching a default-human gate to `auto` warns and is recorded.

**What `auto` means.** The engine passes the gate as soon as that stage's own checks pass. A failing task is never
auto-approved. An auto decision is recorded with who = `auto`.

**How a human decides (D8)**
1. **Preferred: an MCP elicitation form.** `ultron_review` makes the server ask the *user* through Claude Code's
   interface (Approve, Request changes or Reject, plus a comment, plus a version for `spec`). The chat model can't
   answer it.
2. **Fallback: the CLI.** `ultron gate <gate> <id> approve|changes|reject -m "…"`, run by the user in a terminal or
   with `!` in Claude Code. The plugin's guard hook blocks the model from running it.
3. **The record:** the engine records the git identity, the time and the channel.

**Verify first** (the first task checks these before anything else is built on them):
- that the current Claude Code supports MCP elicitation, and how the forms look;
- whether the user's `!` commands bypass PreToolUse hooks.

If elicitation isn't available, V0 uses the CLI fallback for every gate and the design stays the same.

---

## 9. Running long work (D9)

MCP tool calls are short, but a requirement runs for hours. So:
- **Starting work:** tools never do long work themselves. `ultron_submit`, and every gate decision, starts a
  **detached** `ultron run <R>` process and returns at once.
- **Run until blocked:** `ultron run` advances the requirement (workers, attempts, parallel tasks) until it reaches a
  human gate, finishes or escalates, and then exits. Approving a gate starts it again.
- **Lock:** each requirement has a lock file, so it never has two runners. A stale lock (dead process) is cleared.
- **Resume:**
  - after a crash, a reboot or a usage limit, `ultron resume` (or the next tool call) restarts runners for any
    requirement that isn't waiting on a human;
  - a rate limit pauses the run and doesn't count as a failed attempt.
- **Closing Claude Code** doesn't stop runners. Opening it again and asking for status picks up where things are.

---

## 10. State: the event log (D10)
- **Source of truth:** each requirement has an append-only `sdlc/requirements/R<n>/events.jsonl`. Each line holds
  `seq`, time, actor (a git user, `auto`, `engine` or a worker role), type, requirement, task, attempt and data. Event
  types include intake, gate decisions, attempts, escalations, worker-call summaries, and the config snapshot
  (`max_attempts`, gates).
- **Writing:** the engine writes events and the files they refer to in **one commit** on the `ultron/state` branch,
  through its own worktree. State and artifacts can't drift apart, and git history is the audit trail.
- **The cache:** SQLite (`.ultron/state.db`) is only a cache. ultron rebuilds it when it's stale or missing.
- **Single-user V0:** nothing is pushed or pulled automatically (team sync is in the backlog).

| Location | Contents | In git? |
|---|---|---|
| `sdlc/requirements/R<n>/` | `request.md`, `intent.md`, `intake.json`, `events.jsonl`, `spec.md`, `tasks.md`, `decisions.md`, `tasks/T<k>/plan.md`, attempt reports and test output | Yes, on `ultron/state`; merged with the code |
| `sdlc/releases/v<version>.md` | What each release contains | Yes |
| `.ultron/` | `state.db` (cache), worker transcripts, locks, the maintain log offset | No |
| `ultron.toml` | Commands (test, smoke, deploy), log path, caps, `max_attempts`, gates, worker adapter and models, parallelism, Jira | Yes |
| Work folder (default `C:\git\.ultron-work`, no spaces) | Task worktrees and the state worktree | No |
| ultron home (default `C:\git\.ultron`) | Evaluation workspaces, hidden tests, reports, totals across repos | No |

---

## 11. Worker agents (D3)
- **One interface:** every worker call goes through `AgentRunner.run(role, prompt, workdir, allowed tools, output
  schema, limits) → result (output, structured data, usage, cost estimate, duration, transcript, stop reason)`.
- **Prompts:** each role's prompt is a versioned template in the ultron package.
- **The V0 adapter, Claude Code headless:**
  - it runs on the user's Claude subscription;
  - every call is least-privilege: only the tools that role needs, no web access, and no git push, merge or branch
    switching;
  - writes are limited to the task's worktree;
  - the target repo's own Claude settings, hooks and MCP servers are **not** loaded, because target repos may be
    untrusted;
  - the exact flags are decided and tested in the agent-layer task's plan;
  - after every call, the engine checks the diff itself.
- **Adapter choice:** `ultron.toml` picks the adapter (`agent = "claude-code"`) and the model per role.

---

## 12. Escalation to Jira (D5)
- **Connection:** Jira Cloud, configured through environment variables (`JIRA_BASE_URL`, `JIRA_EMAIL`,
  `JIRA_API_TOKEN`) plus the project and board in `ultron.toml`. Credentials are never stored in files. Jira Cloud Free
  costs ₹0 for up to 10 users.
- **The engine creates the ticket, never a model.** The ticket has:
  - the title `[ultron] R<n>/T<k> <title>`;
  - the requirement, the task, the acceptance criteria and the reason (all attempts failed / rejected with comments /
    cap reached);
  - the last test output;
  - attachments: all attempt logs and `plan.md`;
  - labels.
- **Sprint:** the ticket goes into the current Mon–Fri sprint. If there isn't one, ultron creates
  `Escalations <Monday date>`. Weekend escalations go into the next week's sprint.
- **Reliability:**
  - if Jira can't be reached, the escalation is recorded as pending and retried until it succeeds;
  - a local `sdlc/board/<ISO-week>.md` is always written too.
- **Counting:** a tester-rejected task counts as escalated. The report also shows it in its own column, "passed by
  agents, rejected by human".

## 13. Release versioning (D12)
- **Scheme:** major, minor or patch, as in social-grid.
- **Current version:** the newest release ultron has recorded, or `0.0.0`.
- **Proposal:** the spec agent proposes the bump with a one-line reason drawn from the intent. The user confirms it or
  changes it at the `spec` gate.
- **Maintenance intents** default to a patch bump.
- **At deploy:** if several requirements are being released together, the release takes the largest bump among them,
  and the deploy form asks the user to confirm it.

## 14. Claude Code connection check (D13)
`ultron doctor` runs, and `ultron run` checks before any worker, that:
1. Claude Code is installed and at least the pinned version.
2. The user is logged in with a claude.ai account. ultron shows the plan type.
3. No API key in the environment would override the subscription. If one is set, ultron refuses unless the user
   allows it explicitly.

If a check fails, ultron stops and says how to fix it, for example `claude auth login`. ultron never reads or stores
credentials.

## 15. Maintain
- **Trigger:** a scheduled `ultron maintain --repo <path> --once`, every 30 minutes by default. `ultron init` offers
  to register it with Windows Task Scheduler. The detection itself is deterministic and uses no model.
- **Checks:**
  - the full test command and the smoke command against the released commit;
  - new lines in the configured log file.
- **Fingerprints:**
  - a failing test's fingerprint is its test id;
  - a log error's fingerprint is a hash of its exception type, its normalised message, and its top 3 `file:function`
    frames.
- **Dedup:** if an open requirement already has the same fingerprint, the new failure is recorded as another
  occurrence and nothing is filed. A fingerprint closes when its fix is released and the check passes.
- **New failure:** the maintenance agent (read-only) diagnoses it and writes `intent.md`. The intent links the
  release, the tasks whose changes touched the failing area, and the evidence. It then goes through the `maintain`
  gate and the normal pipeline.
- **Notifying the user:** new maintenance findings appear in `ultron_status`.

## 16. Evaluation harness (intent §6)
- **Command:** `ultron eval --suite <file> [--limit 5] [--resume] [--max-attempts N] [--intake auto]` is one CLI
  command. It writes `report.md` and `report.json` under the ultron home.
- **The run:** each instance becomes a requirement whose `intent.md` is the raw issue text. The gate preset is
  `unattended`, and the report lists the gate settings used. No Jira tickets are created unless `--jira` is given.
- **Hidden tests:**
  - each repo is rebuilt from the pre-fix commit with no history, so `git log` can't reveal the fix;
  - the hidden tests live only in the ultron home;
  - grading applies the final diff in a fresh SWE-bench container, adds the hidden tests and checks
    FAIL_TO_PASS and PASS_TO_PASS.
- **Testing agent's tests:** they stay in the task branch and are reported separately from the hidden tests.
- **Metrics:**
  - per requirement: pass rate after each attempt (1 to `max_attempts`); completed, partial and escalated shares;
    send-backs and their effect; cost and time;
  - per task: pass rate after each attempt.
- **Comparisons:**
  - a plain single-agent baseline on the same issues;
  - optionally, a labelled `--intake auto` arm, to show whether structured intake raises the pass rate.
- **Approval before running:** before the pilot and before the full run, ultron prints a usage and duration estimate
  and waits for confirmation.

## 17. Non-functional requirements
- **NFR1** **The system enforces every rule, never the agents:**
  - the gates, the attempt limit, the caps and dependency order;
  - no model or worker can pass a gate, merge, push or deploy (§6, §8, §11).
- **NFR2** Every artifact is a readable file, including the event log.
- **NFR3** Works with any local git repo on Windows 11 and Linux. Work folders have no spaces in their paths.
- **NFR4** Untrusted target repos can't steer the workers (§11) or the chat model (§6).
- **NFR5** Work survives closing Claude Code, crashes and usage limits (§9).
- **NFR6** Simple enough for one person to build and explain. No hosted server. Docker is needed only for evaluation
  grading.

## 18. Look: mascot and palette (D14)
- **Where it shows:** in the CLI (`ultron doctor`, `ultron eval`, `ultron run` in a terminal) with full colour. In
  Claude Code, tool results are plain text, so they use the ✔ ⏳ ✖ ● markers without colour.
- **On Windows:** ultron forces UTF-8 output, falls back to an ASCII mascot when Unicode isn't available, and
  respects `NO_COLOR`.

**Mascot.** The owner's Ultron pixel-art head, redrawn cell by cell without the reference image's watermark. The
image itself isn't committed. 20×28 pixels, drawn in 14 terminal lines with half-blocks.

```
.......HHHHHH.......
.....HHHLLLLHHH.....
....HHHHHHHHHHHH....
...HHHHHHHHHHHHHH...
..HCHHHHHHHHHHHHCH..
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HCHHHHHHHHHHHHCH.S
S.HHHHHHHHHHHHHHHH.S
HHHEHHHHHHHHHHHHEHHH
HHHHEEHHHHHHHHEEHHHH
HHHHEEEEHHHHEEEEHHHH
HHHHHHEEEHHEEEHHHHHH
HHHHHHHHCHHCHHHHHHHH
.HHHHFHHCHHCHHFHHHH.
.HFHFFFHHHHHHFFFHFH.
.HFHFFFFHHHHFFFFHFH.
..HHFFFFHFFHFFFFHH..
...HHFFFFFFFFFFHH...
....HFFFHFFHFFFH....
....HHFFHHHHFFHH....
.....HHHHHHHHHH.....
......HHHHHHHH......
.......HHHHHH.......
........HHHH........
```

| Code | Part | Colour |
|---|---|---|
| `H` | Head | Gunmetal `#7A828A` |
| `C`, `S` | Creases, side fins | Dark gunmetal `#5A6168` |
| `L` | Shine | `#b3b3b3` |
| `E` | Eyes | `#941212` |
| `F` | Mouth and cheek gaps | `#262626` |

**Text.**
| Use | Colour |
|---|---|
| Wordmark `U L T R O N` | Crimson `#E5484D`, bold |
| Main text | `#c9c9c9` |
| Emphasis | `#f2f2f2` |
| Dim text and the tagline | `#7A828A` |
| ✖ failures | `#941212`. The words stay `#f2f2f2`, because the dark red is hard to read as text. |
| ● agent running | Claude orange `#D97757` |
| ✔ approved | Green `#3FB950` |
| ⏳ waiting on you | Amber `#E3B341` |

## 19. Areas of concern
1. **Pro plan limits.** Each requirement takes about 5–12 worker calls. The full 94-issue evaluation will hit the
   limits often and could take weeks. Runs pause and resume, and the pilot measures real usage first. A bigger plan
   would cost money, so it needs the owner's approval, and this spec doesn't propose one.
2. **Elicitation support** (§8) has to be verified in the current Claude Code. The CLI fallback keeps V0 working
   without it.
3. **The model has shell access in Claude Code.** The guard hook blocks it from running `ultron gate`, merge or deploy,
   and a test covers the hook.
4. **Detached processes on Windows.** The runner has to survive the MCP server and Claude Code closing. This is
   verified in the runner task.
5. **Send-backs can't be measured in unattended runs**, because auto gates never send anything back. An optional
   reviewer agent is in the backlog.
6. **Benchmark credibility.** The research found that SWE-bench Verified has known contamination and test problems
   (not re-checked here). The README will say so and compare against the baseline.
7. **Intake in other languages** is untested. Test it with 2–3 Hindi or Hindi–English requests.
8. **Many small commits** on `ultron/state`. This is intended, as the audit trail. They can be squashed at merge.
9. **Differences from the intent**, recorded here; `intent.md` itself is unchanged:
   - the escalation board is Jira;
   - "ticket" becomes "requirement";
   - the attempt limit is configurable;
   - ultron is an MCP server used from chat, rather than "a CLI or minimal web page".

## 20. Out of scope for V0
- **From intent §7:** human wait-time metrics, Jira/Linear/GitHub intake, real production deployment, hosting, auth,
  billing, production monitoring, and model training.
- **Later versions:** everything in `sdlc/backlog.md`.

## 21. How V0 is proven (intent §9)
- **Unit tests** cover the engine, using a fake `AgentRunner`:
  - attempt limits, gate settings and snapshots, and dependency blocking and merge order;
  - caps, version bumps and fingerprint dedup;
  - the template check and the intake flow;
  - rebuilding the cache from the event log, which must give the same state as before;
  - the doctor checks.
- **MCP tests:**
  - the tool list has no tool that accepts a gate decision;
  - merge and deploy require a form;
  - the guard hook blocks gate, merge and deploy commands from the model's shell.
- **Integration test:** the real pipeline on `samples/sample-app`, a small Python app with tests.
- **The demo, inside Claude Code:**
  1. Describe a requirement for the sample app.
  2. Approve it through the gates.
  3. One task is forced to escalate to Jira.
  4. Merge and deploy.
  5. A deliberate regression is caught by `ultron maintain`, which files a linked intent.
- **The evaluation:** the pilot (5), then the full run (≥50), each producing `report.md`.
- **README:** the architecture, the workflow and the results, including where the pipeline fails.

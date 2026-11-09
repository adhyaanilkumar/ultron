# Spec: ultron V0
Author: Claude (from `intent.md`), for adhyaanilkumar. Cycle: V0. Status: **draft, awaiting review**. Date: 2026-11-09.

ultron takes a requirement (`intent.md`) for a target git repository and turns it into tested, human-approved code changes.
Agents specify, split, plan, code and test. A person approves the spec, each plan and each tested result, then merges
and deploys. After a release, a maintenance agent turns new failures into new requirements. ultron also has an
evaluation harness that measures how often this works.

---

## 1. Decisions

**Confirmed** means the owner decided it in conversation on 2026-11-09. **Proposed** means Claude recommends it and
the owner should confirm or change it during this review.

| # | Decision | Status |
|---|---|---|
| D1 | Orchestrator language: **Python**. | Confirmed |
| D2 | Agents run through **Claude Code headless** (`claude -p`) on the owner's **Claude subscription**. Every agent call goes through one small `AgentRunner` interface, so later versions can add other coding agents as adapters. | Confirmed |
| D3 | Terms: **requirement** = one `intent.md` and everything derived from it; **task** = one unit of work split from a requirement; **ticket** = only a Jira issue. Where the intent says "ticket" (§6, §8), this spec says "requirement". | Confirmed |
| D4 | Human UI: a **CLI in v1**. A web page comes in **v2**: social-grid's React + Vite frontend on a thin Python API over the same engine. The details are in `sdlc/backlog.md`. | Confirmed |
| D5 | Failed tasks go to **Jira** as tickets on a Mon–Fri sprint (§8). This replaces "CLI or minimal web page" for the escalation board in intent §5. Intake stays local (a file or text, never Jira), so intent §7 still holds. | Confirmed (Jira); details proposed |
| D6 | ultron **proposes each release's version** from the intent, and a person confirms it at the spec gate (§9). Same scheme as social-grid. | Confirmed |
| D7 | ultron checks before every run that the user is **signed in to a Claude Code account** (§10). | Confirmed |
| D8 | CLI look: the owner's Ultron pixel-art head, full size (20×28, 14 terminal lines), in dark gunmetal steel `#7A828A` with `#941212` eyes. The wordmark is crimson `#E5484D`. Other CLI text uses the owner's palette. Claude's orange marks agent activity (§11). | Confirmed |
| D9 | Shape: one **CLI engine outside the agents**. Each requirement's files live in the **target repo**. The evaluation harness and cross-repo totals live in a central **ultron home**. A Claude Code plugin is optional and later. Researched 2026-11-09: a Claude session can't guarantee the gates and attempt limit, but a Python state machine can. | Proposed |
| D10 | State: an append-only **event log committed to git** is the source of truth. SQLite is a local cache rebuilt from it. Every artifact is a file (§5). V0 is single-user, and team sync goes in `sdlc/backlog.md`. Answers intent §10 "How task state is stored". | Confirmed (2026-11-09) |
| D11 | ultron **commits each requirement's artifacts and event log** to an `ultron/state` branch in the target repo. `ultron merge` brings a requirement's folder into the target branch with its code. A person merges. | Proposed |
| D12 | Evaluation set: **SWE-bench Verified, sympy (75) and pytest (19) issues**. Start with a **5-requirement pilot** to measure real usage, then ask before the full run. | Proposed |
| D13 | Evaluation runs don't create Jira tickets unless `--jira` is passed. They record escalations in the report and a local board file. | Proposed |
| D14 | Model: pinned in `ultron.toml`, default `claude-sonnet-5-5` for every agent role, and overridable per role. | Proposed |
| D15 | The user chooses how many dev→test attempts a task gets before it is escalated to Jira: `max_attempts`, default **3** as in the intent, allowed range 1–10 (§3 FR11). This changes the intent's fixed 3. | Confirmed (configurable); default and range proposed |
| D16 | The user chooses which approvals are human and which are automatic, and can add extra human gates (`tasks`, `escalation`, `maintain`). `merge` and `deploy` stay human (§6). | Confirmed (feature); gate list, defaults and locks proposed |
| D17 | **Hybrid intake.** `ultron submit` takes a template `intent.md` (used as-is) or free text in the user's own words. Free text goes to a small intake agent, which drafts `intent.md` and asks clarifying questions, and a new `intent` gate approves the draft (FR2). Researched 2026-11-09. | Confirmed (2026-11-09) |

---

## 2. Actors and lifecycle

| Actor | Type | In ultron |
|---|---|---|
| Requester | Human | Writes `intent.md` or describes the need in their own words, runs `ultron submit`, and approves an intake draft at the `intent` gate |
| Intake agent | Agent | Turns free text into `intent.md`, asks clarifying questions, adds no scope. It has no tools and no codebase access. |
| Spec agent | Agent | Writes `spec.md` and proposes the version |
| Architect | Human | Spec gate: `ultron gate spec` |
| Decomposition agent | Agent | Writes `tasks.md` plus a machine-readable `tasks.json` (the DAG) |
| Developer agent | Agent, one per task | Writes `plan.md`, then changes code following the approved plan |
| Testing agent | Agent, one per task | Writes and runs tests against the acceptance criteria and reports with evidence |
| Developer | Human | Plan gate: `ultron gate plan` |
| Tester | Human | Result gate: `ultron gate result` |
| Developers | Human | Work escalated Jira tickets |
| Maintenance agent | Agent | Diagnoses a detected failure and writes a new `intent.md`. It never edits code or deploys. |

**Requirement states** (only the engine changes them):
`submitted → [intake_drafting → awaiting_intent_gate →] spec_drafting → awaiting_spec_gate → (changes_requested → spec_drafting)* → spec_approved → decomposing →
in_progress → complete | partial | escalated`. The bracketed intake steps happen only for free-text submissions (FR2).

**Task states:**
`pending (deps unmet) → planning → awaiting_plan_gate → (plan_changes → planning)* → plan_approved → attempt_n
(coding → testing) → passed → awaiting_result_gate → approved | rejected`, plus `failed` once `max_attempts` attempts are used.
`rejected` and `failed` lead to `escalated`. A task with an escalated dependency becomes `blocked` and is never merged
on its own.

---

## 3. Functional requirements

**Intake and spec**
- **FR1** `ultron init --repo <path>` prepares a target repo: it writes `ultron.toml`, adds `.ultron/` to `.gitignore`
  and creates `sdlc/`.
- **FR2** Intake. `ultron submit [<file> | --text "..." | -] --repo <path>` registers a requirement.
  - **Ids:** each requirement gets a collision-free id, `R-<yyyymmdd>-<4 hex>` (e.g. `R-20261109-a3f2`), so team sync
    can come later without renaming anything. This spec writes `R<n>` for short.
  - **What's stored:** the submission is saved byte for byte as `sdlc/requirements/R<n>/request.md`.
  - **FR2a Template check (no AI):** a parser checks for the five template headings (Problem, Proposed outcome,
    Affected users and systems, Constraints, Open questions). If they are all there, the file becomes `intent.md`
    unchanged and the spec agent starts.
  - **FR2b Intake agent:** for free text, or a file that fails the check:
    - It drafts `intent.md` in the template, reusing the user's own phrases.
    - It asks at most 5 clarifying questions in the CLI.
    - It must not add scope. Anything inferred is labelled "Assumption:", and anything unresolved goes under Open
      questions.
    - It has no tools and no codebase access; the spec agent reads the code later.
    - Input in another language (e.g. Hindi or Hindi–English) produces an English `intent.md` that quotes the original
      lines. Unclear translations go to Open questions.
    - The questions, answers, model and usage are saved in `intake.json`.
  - **FR2c Intent gate:** this happens only when the intake agent wrote the intent.
    `ultron show intent R<n>` shows `request.md` and `intent.md` side by side. Then
    `ultron gate intent R<n> approve|changes -m ...`. Changes rerun the intake agent with the comments.
    - The engine records two playbook measures: time to intent, and whether the draft was approved with no changes
      (survival rate).
  - **Machine-written intents skip the intake agent:**
    - Maintenance-agent intents (§12) go through the template check only, and then the `maintain` gate.
    - Evaluation issues use the raw issue text as `intent.md`, with no intake (§14).
- **FR3** The spec agent reads the intent and the target codebase (read-only tools) and writes `spec.md`. The spec
  contains requirements, design, acceptance criteria, flagged concerns, and a **proposed version bump with its reason**
  (§9).
- **FR4** Spec gate (when `spec` is `human`; see §6): `ultron gate spec R<n> approve [--version X.Y.Z] [-m ...]` or `... changes -m "<comments>"`.
  When changes are requested, the spec agent runs again with the comments and the previous spec. The loop repeats
  until the spec is approved. Nothing downstream starts before approval.

**Decomposition and planning**
- **FR5** The decomposition agent writes `tasks.md` (for humans) and `tasks.json` (validated against a JSON schema).
  Each task has id, title, description, acceptance criteria, `depends_on` and `ships_independently`. The engine
  rejects a cycle in the graph or an unknown dependency and reruns the agent, once.
- **FR6** Tasks whose dependencies are all approved run in parallel, up to `max_parallel` (default 2). Each runs in
  its own git worktree on branch `ultron/R<n>/T<k>`. The worktree starts from the requirement's base commit, with the
  approved branches of its dependencies merged in by the engine (never by an agent).
- **FR7** The developer agent writes `plan.md` (files that change, order of work, risks, proof). Plan gate:
  `ultron gate plan R<n>/T<k> approve|changes -m ...`. No code is written before approval.

**Attempts** (the engine enforces all of these)
- **FR8** One attempt has three steps:
  1. The developer agent changes code following the approved plan.
  2. The testing agent writes tests for the acceptance criteria in the repo's normal test layout and runs them.
  3. The engine reruns the testing agent's tests plus the repo's existing test command (`ultron.toml`) and decides
     pass or fail from the real exit codes. The agent's claims are not the verdict.
- **FR9** If an attempt fails, the engine's test output goes back to the developer agent for the next attempt.
- **FR10** If the developer agent says it needs a different plan, it returns a revised `plan.md`. That plan goes back
  through the plan gate, and the attempt count is kept.
- **FR11** A task has at most **`max_attempts`** attempts. The user chooses this number:
  - default **3**, allowed range 1–10;
  - set per repo in `ultron.toml`;
  - overridden per requirement with `ultron submit --max-attempts N`, or per evaluation run with
    `ultron eval --max-attempts N`.

  The engine copies the limit into each task when the task is created and enforces it from there. A later config
  change doesn't affect tasks already in progress, and an agent can never change it. The limit in force is recorded
  with the task and shown in `ultron status` (e.g. `attempt 2/5`). Time waiting at the plan gate is not an attempt.
- **FR12** If the developer agent's diff changes a test file the testing agent wrote, that attempt fails.

**Result gate, escalation, merge and deploy**
- **FR13** A task that passes goes to the result gate. `ultron show result R<n>/T<k>` shows the plan, the test
  evidence and the diff (in the terminal, or in VS Code with `--open`). Then
  `ultron gate result R<n>/T<k> approve|reject -m ...`.
- **FR14** A task that uses all `max_attempts` attempts without passing, or is rejected by the tester, is escalated to
  Jira (§8).
- **FR15** `ultron merge R<n>` is run by a person. It merges the approved task branches into the target branch in
  dependency order, skips any task whose dependency is escalated, and stops on a conflict.
- **FR16** `ultron deploy --repo <path>` is run by a person. It runs the configured deploy script, then records the
  release: version, commit, time, who, and the requirements and tasks it contains. It also tags `v<version>`.

**Records and logs**
- **FR17** Every gate decision is an event in the requirement's event log (§5): who (git `user.name` and email, or
  `auto`), when, the decision and the comments. `decisions.md` is generated from the log for people to read.
- **FR18** Every agent call is logged with:
  - role, requirement and task, and attempt;
  - the prompt (input) and the final result (output);
  - the raw transcript (stream-json), kept locally in `.ultron/calls/` because it's too large for git; the event log
    keeps a summary of every call;
  - duration, tokens (input, output and cache), `total_cost_usd` (recorded as an API-equivalent estimate) and stop
    reason.
- **FR19** Spending caps (API-equivalent USD) apply per requirement and per evaluation run. The engine checks the
  running total before each call and refuses calls over the cap. If a requirement hits its cap, its unfinished tasks
  are escalated.

**Status and maintenance**
- **FR20** `ultron status [--repo]` shows every requirement and task with its state, attempt, and what is waiting on a
  person.
- **FR21** Maintain (§12): `ultron maintain --repo <path> --once` runs the test suite and smoke command, reads new
  lines in the log file, and files a linked `intent.md` for each new failure.

## 4. Non-functional requirements
- **NFR1** The agents can't pass the rules:
  - Agents never get a tool that passes a gate, merges, pushes or deploys.
  - Each call runs with `--permission-mode dontAsk`, an explicit `--allowedTools`, and `--disallowedTools` covering
    WebFetch, WebSearch, `git push`, `git merge`, `git rebase`, `git checkout` of other branches, and edits under
    `.ultron/` and `sdlc/`.
  - The engine also checks each diff afterwards.
- **NFR2** Every artifact is a readable file: `request.md`, `intent.md`, `spec.md`, `tasks.md`, `plan.md`, test reports,
  attempt logs, `decisions.md`, and the event log `events.jsonl`.
- **NFR3** Works against any local git repo on Windows 11 (PowerShell and Git Bash) and Linux. Work folders must have
  no spaces in their path (default `C:\git\.ultron-work`).
- **NFR4** Untrusted target repos can't steer the agents. ultron doesn't load the target repo's `.claude/` hooks,
  settings or `.mcp.json` into agent calls. It passes its own settings instead. The exact flags are verified in the
  agent-layer task.
- **NFR5** A run can be resumed. If ultron is stopped, or a usage limit is hit, `ultron resume` continues from the
  event log. A rate limit pauses the run and doesn't count as a failed attempt.
- **NFR6** Simple enough for one person to build and explain. No server, queue or container is needed for the core
  pipeline. Docker is needed only for evaluation grading.

---

## 5. Architecture

```
ultron (CLI, Typer + Rich)
 ├─ engine/      state machine, scheduler (asyncio + semaphore), attempt limit, caps, gates  ← the authority
 ├─ intake/      template check + intake agent flow
 ├─ agents/      AgentRunner protocol; ClaudeCodeRunner (claude -p subprocess); prompts per role; JSON schemas
 ├─ git/         worktrees, branches, merges, diffs (shells out to git)
 ├─ store/       event log (source of truth) + SQLite cache + artifact writer
 ├─ jira/        escalation client (REST)
 ├─ maintain/    checks, log reader, fingerprints, intent writer
 └─ eval/        dataset prep, run, grade (swebench on Docker), report
```

**AgentRunner** is the only thing that knows about Claude:
`run(role, prompt, workdir, allowed_tools, schema?, max_turns, budget_usd) -> AgentResult(output, structured, usage,
cost_usd, duration_ms, transcript_path, stop_reason)`. The prompt goes in on stdin, which avoids Windows' 32 KB
command-line limit. Each role is a prompt template file in `ultron/prompts/<role>.md`, versioned with the code.

**State: the event log (decided 2026-11-09)**
- **Source of truth:** each requirement has an append-only log, `sdlc/requirements/R<n>/events.jsonl`. Each line is
  one event: `seq`, time, actor (a git user, `auto`, `engine`, or an agent role), type, requirement, task, attempt,
  and data. Examples: `gate.decided`, `attempt.finished`, `task.escalated`, `agent.call` with a usage summary,
  `config.snapshot` with `max_attempts` and the gate settings.
- **The cache:** SQLite (`.ultron/state.db`) is only a cache for fast queries and reports. ultron checks it against
  the log's latest commit on start, and rebuilds it if it's stale or missing.
- **Writing:** the engine writes an event and the files it refers to in **one commit** to the `ultron/state` branch,
  through a dedicated worktree. State and artifacts can't drift apart, and the commit history is the audit trail.
- **V0 is single-user.** Nothing is pushed or pulled automatically. Team sync (pull before / push after, task claims,
  retry on a rejected push) is in `sdlc/backlog.md`. It works on this same log without changing its format.

**Where things live**

| Location | Contents | In git? |
|---|---|---|
| `<repo>/sdlc/requirements/R<n>/` | `request.md`, `intent.md`, `intake.json`, `events.jsonl`, `spec.md`, `tasks.md`, `decisions.md`, `tasks/T<k>/plan.md`, `tasks/T<k>/attempt-<a>/{report.md,test-output.txt}` | Yes, on `ultron/state`; brought into the target branch by `ultron merge` (D11) |
| `<repo>/sdlc/releases/v<version>.md` | What each release contains | Yes |
| `<repo>/.ultron/` | `state.db` (cache), `calls/<id>.jsonl` transcripts, the log-file read offset for maintain | No (gitignored) |
| `<repo>/ultron.toml` | Test, smoke and deploy commands, log path, caps, `max_attempts`, model, parallelism, gate modes, Jira project | Yes |
| `C:\git\.ultron-work\<repo>\R<n>\T<k>` | Task worktrees | No |
| `C:\git\.ultron-work\<repo>\_state` | Worktree of the `ultron/state` branch, where the engine commits | No |
| ultron home (default `C:\git\.ultron`) | Eval workspaces, hidden tests, eval reports, totals across repos | No |

**Stack** (from the 2026-11-09 research; versions are re-checked when the project is set up)

| Layer | Choice |
|---|---|
| Python and tooling | Python 3.14 (`requires-python >=3.12`), uv, ruff, mypy (strict), pytest |
| CLI | Typer and Rich |
| Concurrency | asyncio, with `create_subprocess_exec` and a semaphore |
| Git | Shell out to `git` (worktree, merge, diff) |
| State | JSON-lines event log in git; stdlib `sqlite3` as the cache |
| Config | `ultron.toml` read by `tomllib` and validated with pydantic |
| Jira | Jira Cloud REST v3 and Agile API through `httpx` |
| Evaluation grading | `swebench` harness on Docker Desktop (WSL2) |
| Checks | Makefile with `make build`, `make lint`, `make test`, each ending in a fixed success line (as in social-grid) |

---

## 6. Choosing the human approvals (feature)
The user chooses which approval points need a person (`human`) and which the engine passes on its own (`auto`), and
can also add human approvals the intent doesn't have.

| Gate | What gets approved | Default | Can be changed? |
|---|---|---|---|
| `intent` | An `intent.md` the intake agent drafted from free text (FR2c). Not used for template files. | human | yes |
| `spec` | `spec.md` and the proposed version (architect) | human | yes |
| `tasks` | `tasks.md`, the task split and its dependencies | auto | yes (an extra human gate) |
| `plan` | Each task's `plan.md` (developer) | human | yes |
| `result` | Each tested task: the diff, plan and evidence (tester) | human | yes |
| `escalation` | Confirm before ultron creates a Jira ticket | auto | yes (an extra human gate) |
| `maintain` | Accept a maintenance-agent intent before it enters the spec step | auto | yes (an extra human gate) |
| `merge` | Merging approved tasks into the target branch | human | **no**: locked |
| `deploy` | Running the deploy script and recording the release | human | **no**: locked |

**Locked gates.** `merge` and `deploy` stay human because the intent says only a human action merges or deploys
(intent §5). Unlocking them would change the intent, so it needs the owner's explicit decision. See §15.

**What `auto` means.** The engine passes the gate as soon as that stage's own checks pass:
- `intent`: the draft passes the template check.
- `spec` and `tasks`: the output is valid against its schema. For `tasks`, the graph also has no cycles.
- `plan`: `plan.md` has all four sections.
- `result`: the engine's own test run passed. A failing task is never auto-approved; it uses up its attempts.

An `auto` decision is recorded like a human one, with who = `auto`, in SQLite and `decisions.md`.

**How the user chooses.**
- **Per repo:** `[gates]` in `ultron.toml`, or `ultron gates set plan=auto tasks=human`.
- **Per requirement:** `ultron submit … --gates plan=auto`.
- **Presets:** `--gates strict` makes every changeable gate human, `default` is the table above, and `unattended`
  makes every changeable gate auto. The evaluation harness uses `unattended`.
- **Seeing the current setting:** `ultron gates` shows the gates in force. `ultron status` marks auto-passed steps
  "(auto)".

**Rules the engine enforces.**
- **Snapshot:** a requirement takes a snapshot of its gate settings when it's submitted, as with `max_attempts`.
- **Mid-run changes:** `ultron gates set R<n> plan=human` changes a running requirement, but only for gates it hasn't
  reached yet. The change is recorded in `decisions.md`.
- **Warning:** switching a default-human gate (`intent`, `spec`, `plan`, `result`) to `auto` prints a one-line warning and is
  recorded.
- **Agents:** no agent can read or change gate settings.
- **Evaluation:** every evaluation report lists the gate settings used for the run (intent §6).

## 7. Agent tests vs hidden evaluation tests (intent §10)
- **The testing agent's tests** live in the task branch, in the repo's normal test folders, and are part of the
  change the tester reviews.
- **The hidden tests** (the real fix's `test_patch` in the evaluation) never exist anywhere an agent can read:
  - The evaluation repo is rebuilt with no history: clone, check out the base commit, `git init` again, and make one
    commit. That way `git log` can't reveal the fix.
  - The hidden tests and the dataset are kept under the ultron home, outside every worktree.
  - Web tools are denied.
- **Grading** happens after the pipeline finishes. It takes the final diff (merged approved tasks), applies it in a
  fresh swebench container, adds the hidden tests, and checks FAIL_TO_PASS and PASS_TO_PASS.

## 8. Escalation board: Jira (proposed details)
- **Connection:** `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` come from environment variables and are never
  stored in files. The project key and board id are in `ultron.toml`. Jira Cloud Free costs ₹0 for up to 10 users.
- **The engine creates the ticket**, not an agent:
  - summary `[ultron] R<n>/T<k> <title>`;
  - description with the requirement, the task, acceptance criteria, the reason (all `max_attempts` attempts failed / tester rejection
    with comments / cap hit) and the last test output;
  - attachments: a zip of all attempt logs plus `plan.md`;
  - labels `ultron` and `R<n>`.
- **Sprint:** the ticket is added to the open sprint for the current Mon–Fri week. If none exists, ultron creates
  `Escalations <Monday date>` (Monday 09:00 to Friday 18:00, local time). Tasks escalated on a weekend go to the next
  week's sprint.
- **Reliability:** if Jira can't be reached, the escalation is queued in SQLite and retried on the next ultron
  command. It is never lost. `ultron board` always writes `sdlc/board/<ISO-week>.md` too, as a local copy.
- **Counting rejected tasks (intent §10):** a tester-rejected task counts as **escalated**. The report also shows it
  in its own column, "passed by agents, rejected by human".

## 9. Release versioning (feature)
- **Version scheme:** `MAJOR.MINOR.PATCH`, as in social-grid:
  - a big feature bumps MAJOR (1 → 2);
  - a small feature bumps MINOR (1 → 1.1);
  - a small tweak or fix bumps PATCH (1.1 → 1.1.1).
- **The current version** is the newest release ultron recorded for the repo (its `v*` tag), or `0.0.0` if there is
  none.
- **The spec agent proposes the bump** (major, minor or patch) in `spec.md`, with a one-line reason drawn from the
  intent. The architect confirms it, or overrides it with `--version`, at the spec gate. The decision is recorded.
- **Bugs filed by the maintenance agent** default to a patch bump.
- **At deploy**, if several requirements were merged since the last release, the release takes the largest confirmed
  bump. ultron shows the version and asks the person deploying to confirm it.

## 10. Claude Code connection check (feature)
`ultron doctor` runs, and every pipeline command checks first, that:
1. `claude` is on PATH. On Windows, prefer the native `claude.exe` over the npm `.cmd` shim. The version is at or
   above the pinned minimum.
2. `claude auth status --json` reports `loggedIn: true` and `authMethod: "claude.ai"`. ultron shows the plan type
   (`subscriptionType`, e.g. `pro`).
3. `ANTHROPIC_API_KEY` is **not** set. If it were, `claude -p` would bill the API instead of using the subscription.
   ultron refuses unless `--allow-api-key` is passed.
4. ultron never calls with `--bare`, because bare mode skips the subscription login.

If any check fails, ultron stops and prints the fix, for example `claude auth login`. ultron never reads or stores
credentials itself.

## 11. CLI look: palette and mascot
**Mascot.** The owner's choice (2026-11-09): the Ultron pixel-art head from the owner's reference image, at full
size and redrawn cell by cell without the image's watermark. It's dark gunmetal steel, as in the movie, with red eyes.
The source image is not committed.
- **Where it appears:** beside the wordmark in `ultron`, `ultron doctor` and `ultron status`.
- **Rendering:** with half-block characters, 20 columns by 14 lines. `--quiet`, non-TTY output and `NO_COLOR` hide it.
- **Grid:** 20×28. One trace row was mirrored to make it symmetric.

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
| `C`, `S` | Forehead creases, side fins | Dark gunmetal `#5A6168` |
| `L` | Shine on top of the head | `#b3b3b3` |
| `E` | Eyes | `#941212` |
| `F` | Mouth and cheek gaps | `#262626` |
| `.` | Background | Terminal background |

**CLI text colours.** These come from the owner's palette (2026-11-09):

| Use | Colour |
|---|---|
| The `U L T R O N` wordmark | Crimson `#E5484D`, bold (the owner's choice, 2026-11-09) |
| Main text | `#c9c9c9` |
| Emphasis | `#f2f2f2` |
| Borders, dim text, the tagline | Gunmetal `#7A828A`, which matches the head |
| The ✖ marker on failures and escalations | `#941212` |

Status colours, which stay as they were:

| State | Colour and marker |
|---|---|
| Claude agent is running | Claude orange `#D97757`, with ● |
| Passed or approved | Green `#3FB950`, with ✔ |
| Waiting on you | Amber `#E3B341`, with ⏳ |

**Concern.** `#941212` is a dark red. As text on a dark terminal its contrast is about 2:1, which is hard to read.
So failure lines use it only for the ✖ marker and the eyes, and the words stay `#f2f2f2`. The owner can change this.

**Terminals.**
- On Windows, ultron forces UTF-8 output. The default cp1252 console encoding crashes on block characters; found
  when testing the mascot on 2026-11-09.
- Colours fall back to 256 or 16 colours, and to an ASCII mascot when Unicode isn't available. `NO_COLOR` turns
  colours off.

## 12. Maintain (intent §10)
- **Trigger:** after `ultron deploy`, ultron offers to register a Windows Task Scheduler job (`schtasks`) that runs
  `ultron maintain --repo <path> --once` every 30 minutes (configurable). A long-running `--every 30m` loop is also
  available. Detection is deterministic and uses no model.
- **Checks:**
  - the full test command and the smoke command from `ultron.toml`, run against the released commit;
  - new lines in the configured log file since the last read (offset stored in `.ultron/`).
- **Fingerprints:**
  - a failing test's fingerprint is its test id;
  - a log error's fingerprint is a sha256 of the exception type, the normalised message (numbers, hex, UUIDs,
    timestamps and paths removed) and the top 3 `file:function` frames, without line numbers.
- **Dedup:** if an unresolved requirement already has the same fingerprint, ultron records another occurrence and
  doesn't file again. A fingerprint becomes resolved when its requirement is released and the check passes.
- **New failure:** the maintenance agent (read-only tools) diagnoses it and writes `intent.md`. The intent links:
  - the release;
  - the tasks in that release whose diffs touched the failing test, or the files in the stack trace;
  - the evidence.

  The requirement then enters at the spec step and passes through every gate.

## 13. Later versions
Features after V0 live in `sdlc/backlog.md`, not in this spec:
- the v2 web page (decided: React + Vite on a thin Python API);
- other coding agents;
- a Claude Code plugin front-end;
- a multi-project registry;
- a reviewer agent for evaluation runs.

## 14. Evaluation harness (intent §6)
- **Command:** `ultron eval --suite suites/swebench-verified.yaml [--limit 5] [--resume]` runs everything and writes
  `report.md` and `report.json` under the ultron home. Results can be reproduced from this one command plus the
  pinned model and Claude Code version.
- **Each instance becomes one requirement.** The raw issue text is its `intent.md`, with no intake agent (intent §6).
  All changeable gates are `auto`. Grading follows §7.
- **Optional intake comparison:** `--intake auto` runs a second, labelled arm. The intake agent runs in no-questions
  mode, and anything unclear goes to Open questions. The report compares both arms against the baseline, which
  answers "does structuring the intake raise the pass rate?". Intake outputs are saved so the arm can be
  reproduced.
- **Requirement-level metrics:**
  - pass rate after each attempt, 1 to `max_attempts`;
  - shares completed / partial / escalated;
  - how often the spec and plans were sent back (by auto-review in unattended runs; see the concern in §15), and the
    pass rate with vs without send-backs;
  - API-equivalent cost and wall time per requirement.
- **Task-level metrics:** pass rate after each attempt, 1 to `max_attempts`. The report states the limit used.
- **Baseline:** a single plain `claude -p` run on the same instances, so results compare against a simple baseline
  and not the public leaderboard.
- **Before the pilot and before the full run,** ultron prints an estimate of usage and duration and waits for
  confirmation.

## 15. Areas of concern
1. **Pro plan usage limits.**
   - A requirement takes about 5 to 12 agent calls.
   - Published single-agent SWE-bench runs use about 1M tokens per instance, which is $0.55 to $1.30 API-equivalent.
   - So 94 requirements will hit Pro limits many times, and the full run could take weeks.
   - The pilot measures this. ultron pauses and resumes rather than failing.
   - Upgrading the plan would cost money and needs the owner's approval. This spec doesn't recommend it.
2. **Send-backs in unattended runs.** With gates on `auto`, nothing is ever sent back, so intent §6's "how often specs
   and plans were sent back" can only be measured in attended runs. An optional reviewer agent at the gates in eval
   mode is in `sdlc/backlog.md`. It isn't in V0 unless the owner wants it.
3. **Benchmark credibility.** The stack research found that SWE-bench Verified has known contamination and test
   issues (not re-checked here). The README will say so and compare against the baseline.
4. **Versioning with requirements in flight at the same time.** The version is confirmed per requirement, but a
   release can bundle several. §9's "largest bump wins, confirmed at deploy" keeps this simple. Per-requirement release
   trains are out of scope.
5. **`claude -p` behaviour can change** between Claude Code versions. Example: the docs say `--bare` may become the
   default. Mitigation: pin the minimum version, and `ultron doctor` checks the auth source.
6. **This diverges from the intent.** Jira replaces the CLI or web escalation board (intent §5), "ticket" becomes
   "requirement", and the fixed 3 attempts become a user-chosen `max_attempts` (default 3). `intent.md` is left unchanged; this spec records these changes.
7. **Locked merge and deploy gates (§6).** The intent requires a human action for both, so V0 doesn't let them be
   switched to `auto`. If the owner wants automatic merging in a later version, it goes in `sdlc/backlog.md` and changes
   the intent.
8. **Intake in other languages is untested.** Claude handles Hindi well, but there's no evidence specific to
   requirements. Test intake with 2–3 Hindi or Hindi–English sample requests before relying on it.
9. **Event log commits.** Every engine step commits to `ultron/state`, which makes many small commits. That's
   intended, as the audit trail. A requirement's history can be squashed when it's merged, if the owner wants.

## 16. Out of scope for V0
- **From intent §7:** human wait-time metrics, Jira/Linear/GitHub intake, real production deployment, hosting, auth,
  billing, production monitoring, and model training.
- **Later versions:** everything in `sdlc/backlog.md`.

## 17. How V0 is proven (intent §9)
- **Unit tests** (pytest) cover the engine with a fake `AgentRunner`:
  - the attempt limit (default, per-requirement override, range check, unchanged mid-task);
  - the plan-gate rules;
  - dependency blocking and merge order;
  - caps;
  - auto-gate recording;
  - fingerprint dedup;
  - version bumps;
  - the doctor checks;
  - the template check and the intake flow (with a fake agent);
  - rebuilding the cache from the event log, which must give the same state as before.
- **Integration test:** run the real pipeline against `samples/sample-app`, a small Python app with tests that ultron
  copies to the work folder.
- **The demo** (intent §9 bullet 1):
  1. A requirement on the sample app goes from intent to merged change, through all three gates, with one task forced
     to escalate to Jira.
  2. A deliberately introduced regression is caught by `ultron maintain`, which files a linked intent.
- **The evaluation:** the pilot (5) and then the full run (≥50) produce `report.md`.
- **README:** architecture, workflow and results, including where the pipeline fails.

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
| D5 | Failed tasks go to **Jira** as tickets on a Mon–Fri sprint (§8). This replaces "CLI or minimal web page" for the escalation board in intent §5. Intake stays a file, so intent §7 still holds. | Confirmed (Jira); details proposed |
| D6 | ultron **proposes each release's version** from the intent, and a person confirms it at the spec gate (§9). Same scheme as social-grid. | Confirmed |
| D7 | ultron checks before every run that the user is **signed in to a Claude Code account** (§10). | Confirmed |
| D8 | CLI look: an Ultron red-and-steel theme. The mascot is the owner's Ultron pixel-art head with soft crimson eyes (`#E5484D`). Claude's orange marks agent activity (§11). | Confirmed |
| D9 | Shape: one **CLI engine outside the agents**. Each requirement's files live in the **target repo**. The evaluation harness and cross-repo totals live in a central **ultron home**. A Claude Code plugin is optional and later. Researched 2026-11-09: a Claude session can't guarantee the gates and attempt limit, but a Python state machine can. | Proposed |
| D10 | State: **SQLite** (state machine, attempt counters, gate records, agent-call log) plus **files for every artifact**. Answers intent §10 "How task state is stored". | Proposed |
| D11 | ultron **commits each requirement's artifacts** on its own branches in the target repo. A person merges. | Proposed |
| D12 | Evaluation set: **SWE-bench Verified, sympy (75) and pytest (19) issues**. Start with a **5-requirement pilot** to measure real usage, then ask before the full run. | Proposed |
| D13 | Evaluation runs don't create Jira tickets unless `--jira` is passed. They record escalations in the report and a local board file. | Proposed |
| D14 | Model: pinned in `ultron.toml`, default `claude-sonnet-5-5` for every agent role, and overridable per role. | Proposed |

---

## 2. Actors and lifecycle

| Actor | Type | In ultron |
|---|---|---|
| Requester | Human | Writes `intent.md` and runs `ultron submit` |
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
`submitted → spec_drafting → awaiting_spec_gate → (changes_requested → spec_drafting)* → spec_approved → decomposing →
in_progress → complete | partial | escalated`.

**Task states:**
`pending (deps unmet) → planning → awaiting_plan_gate → (plan_changes → planning)* → plan_approved → attempt_n
(coding → testing) → passed → awaiting_result_gate → approved | rejected`, plus `failed` once 3 attempts are used.
`rejected` and `failed` lead to `escalated`. A task with an escalated dependency becomes `blocked` and is never merged
on its own.

---

## 3. Functional requirements

**Intake and spec**
- **FR1** `ultron init --repo <path>` prepares a target repo: it writes `ultron.toml`, adds `.ultron/` to `.gitignore`
  and creates `sdlc/`.
- **FR2** `ultron submit <intent.md> --repo <path>` registers a requirement as `R<n>-<slug>`. It copies the intent to
  `sdlc/requirements/R<n>-<slug>/intent.md` and starts the spec agent.
- **FR3** The spec agent reads the intent and the target codebase (read-only tools) and writes `spec.md`. The spec
  contains requirements, design, acceptance criteria, flagged concerns, and a **proposed version bump with its reason**
  (§9).
- **FR4** Spec gate: `ultron gate spec R<n> approve [--version X.Y.Z] [-m ...]` or `... changes -m "<comments>"`.
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
- **FR11** A task has at most **3 attempts**. Time waiting at the plan gate is not an attempt.
- **FR12** If the developer agent's diff changes a test file the testing agent wrote, that attempt fails.

**Result gate, escalation, merge and deploy**
- **FR13** A task that passes goes to the result gate. `ultron show result R<n>/T<k>` shows the plan, the test
  evidence and the diff (in the terminal, or in VS Code with `--open`). Then
  `ultron gate result R<n>/T<k> approve|reject -m ...`.
- **FR14** A task that fails 3 times, or is rejected by the tester, is escalated to Jira (§8).
- **FR15** `ultron merge R<n>` is run by a person. It merges the approved task branches into the target branch in
  dependency order, skips any task whose dependency is escalated, and stops on a conflict.
- **FR16** `ultron deploy --repo <path>` is run by a person. It runs the configured deploy script, then records the
  release: version, commit, time, who, and the requirements and tasks it contains. It also tags `v<version>`.

**Records and logs**
- **FR17** Every gate decision records who (git `user.name`), when, the decision and the comments. Records are kept
  in SQLite and appended to `sdlc/requirements/R<n>-<slug>/decisions.md`.
- **FR18** Every agent call is logged with:
  - role, requirement and task, and attempt;
  - the prompt (input) and the final result (output);
  - the raw transcript (stream-json);
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
- **NFR2** Every artifact is a readable file: `intent.md`, `spec.md`, `tasks.md`, `plan.md`, test reports, attempt logs,
  `decisions.md`.
- **NFR3** Works against any local git repo on Windows 11 (PowerShell and Git Bash) and Linux. Work folders must have
  no spaces in their path (default `C:\git\.ultron-work`).
- **NFR4** Untrusted target repos can't steer the agents. ultron doesn't load the target repo's `.claude/` hooks,
  settings or `.mcp.json` into agent calls. It passes its own settings instead. The exact flags are verified in the
  agent-layer task.
- **NFR5** A run can be resumed. If ultron is stopped, or a usage limit is hit, `ultron resume` continues from SQLite
  state. A rate limit pauses the run and doesn't count as a failed attempt.
- **NFR6** Simple enough for one person to build and explain. No server, queue or container is needed for the core
  pipeline. Docker is needed only for evaluation grading.

---

## 5. Architecture

```
ultron (CLI, Typer + Rich)
 ├─ engine/      state machine, scheduler (asyncio + semaphore), attempt limit, caps, gates  ← the authority
 ├─ agents/      AgentRunner protocol; ClaudeCodeRunner (claude -p subprocess); prompts per role; JSON schemas
 ├─ git/         worktrees, branches, merges, diffs (shells out to git)
 ├─ store/       SQLite (WAL) + artifact writer
 ├─ jira/        escalation client (REST)
 ├─ maintain/    checks, log reader, fingerprints, intent writer
 └─ eval/        dataset prep, run, grade (swebench on Docker), report
```

**AgentRunner** is the only thing that knows about Claude:
`run(role, prompt, workdir, allowed_tools, schema?, max_turns, budget_usd) -> AgentResult(output, structured, usage,
cost_usd, duration_ms, transcript_path, stop_reason)`. The prompt goes in on stdin, which avoids Windows' 32 KB
command-line limit. Each role is a prompt template file in `ultron/prompts/<role>.md`, versioned with the code.

**Where things live**

| Location | Contents | In git? |
|---|---|---|
| `<repo>/sdlc/requirements/R<n>-<slug>/` | `intent.md`, `spec.md`, `tasks.md`, `decisions.md`, `tasks/T<k>/plan.md`, `tasks/T<k>/attempt-<a>/{report.md,test-output.txt}` | Yes, on `ultron/R<n>` (D11) |
| `<repo>/sdlc/releases/v<version>.md` | What each release contains | Yes |
| `<repo>/.ultron/` | `state.db`, `calls/<id>.jsonl` transcripts, maintain fingerprints | No (gitignored) |
| `<repo>/ultron.toml` | Test, smoke and deploy commands, log path, caps, model, parallelism, gate modes, Jira project | Yes |
| `C:\git\.ultron-work\<repo>\R<n>\T<k>` | Task worktrees | No |
| ultron home (default `C:\git\.ultron`) | Eval workspaces, hidden tests, eval reports, totals across repos | No |

**Stack** (from the 2026-11-09 research; versions are re-checked when the project is set up)

| Layer | Choice |
|---|---|
| Python and tooling | Python 3.14 (`requires-python >=3.12`), uv, ruff, mypy (strict), pytest |
| CLI | Typer and Rich |
| Concurrency | asyncio, with `create_subprocess_exec` and a semaphore |
| Git | Shell out to `git` (worktree, merge, diff) |
| State | stdlib `sqlite3` (WAL, `BEGIN IMMEDIATE`) |
| Config | `ultron.toml` read by `tomllib` and validated with pydantic |
| Jira | Jira Cloud REST v3 and Agile API through `httpx` |
| Evaluation grading | `swebench` harness on Docker Desktop (WSL2) |
| Checks | Makefile with `make build`, `make lint`, `make test`, each ending in a fixed success line (as in social-grid) |

---

## 6. Gates and auto-approve
- Each gate has a mode in `ultron.toml`: `manual` (the default) or `auto`. Only the evaluation harness sets `auto`.
  An auto decision is recorded with who = `auto`, and the report lists which gates were automatic (intent §6).
- Each gate is only a CLI command a person runs, or the harness's `auto` setting. No agent can trigger a gate.

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
  - description with the requirement, the task, acceptance criteria, the reason (3 failed attempts / tester rejection
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
**Mascot.** The owner's choice (2026-11-09): the Ultron pixel-art head from the owner's reference image, redrawn
cell by cell without the image's watermark. The eyes are soft crimson and always stay red. The source image is not
committed.
- **Where it appears:** beside the wordmark in `ultron`, `ultron doctor` and `ultron status`.
- **Rendering:** with half-block characters, 10 columns by 7 lines. `--quiet`, non-TTY output and `NO_COLOR` hide it.
- **Grid:** the owner asked for it smaller (2026-11-09), so this is the 20×28 trace scaled down to 10×14. Any eye pixel
  survives the scale-down, and the eye rows were adjusted by hand to keep the two eyes as separate slanted red slits.

| Code | Part | Colour |
|---|---|---|
| `H` | Head | Steel `#C0C6CC` (on a dark terminal the original black would be invisible) |
| `C` | Forehead creases | Dim steel `#7A828A` |
| `E` | Eyes | Soft crimson `#E5484D` |
| `F` | Mouth and cheek gaps | Dark `#2A2E33` |
| `.` | Background | Terminal background |

```
...HHHH...
..HHHHHH..
.HHHHHHHH.
HHCHHHHCHH
HHCHHHHCHH
HHHHHHHHHH
HEEHHHHEEH
HHEEHHEEHH
HHHHCCHHHH
HFFFHHFFFH
.HFFFFFFH.
..HFHHFH..
...HHHH...
....HH....
```

**Palette.** Soft crimson, the owner's choice (2026-11-09). The first draft's `#E10600` was too bright.

| Use | Colour |
|---|---|
| Brand, the mascot's eyes, headers | Soft crimson `#E5484D` (lighter `#F2777B` for highlights) |
| Main text, borders, the mascot's head | Steel `#C0C6CC`, dim steel `#7A828A` |
| Claude agent activity | Claude orange `#D97757`, with ● |
| Passed or approved | Green `#3FB950`, with ✔ |
| Waiting on you | Amber `#E3B341`, with ⏳ |
| Failed, rejected or escalated | Bold brand red, always with ✖ and a word, so failure doesn't rely on colour alone |

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
- **Each instance becomes one requirement.** The issue text is its `intent.md`. All gates are set to `auto`.
  Grading follows §7.
- **Requirement-level metrics:**
  - pass rate after 1, 2 and 3 attempts;
  - shares completed / partial / escalated;
  - how often the spec and plans were sent back (by auto-review in unattended runs; see the concern in §15), and the
    pass rate with vs without send-backs;
  - API-equivalent cost and wall time per requirement.
- **Task-level metrics:** pass rate after 1, 2 and 3 attempts.
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
6. **This diverges from the intent.** Jira replaces the CLI or web escalation board (intent §5), and "ticket" becomes
   "requirement". `intent.md` is left unchanged; this spec records both changes.

## 16. Out of scope for V0
- **From intent §7:** human wait-time metrics, Jira/Linear/GitHub intake, real production deployment, hosting, auth,
  billing, production monitoring, and model training.
- **Later versions:** everything in `sdlc/backlog.md`.

## 17. How V0 is proven (intent §9)
- **Unit tests** (pytest) cover the engine with a fake `AgentRunner`:
  - the 3-attempt limit;
  - the plan-gate rules;
  - dependency blocking and merge order;
  - caps;
  - auto-gate recording;
  - fingerprint dedup;
  - version bumps;
  - the doctor checks.
- **Integration test:** run the real pipeline against `samples/sample-app`, a small Python app with tests that ultron
  copies to the work folder.
- **The demo** (intent §9 bullet 1):
  1. A requirement on the sample app goes from intent to merged change, through all three gates, with one task forced
     to escalate to Jira.
  2. A deliberately introduced regression is caught by `ultron maintain`, which files a linked intent.
- **The evaluation:** the pilot (5) and then the full run (≥50) produce `report.md`.
- **README:** architecture, workflow and results, including where the pipeline fails.

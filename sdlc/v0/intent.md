# intent.md — AI-Native SDLC Pipeline
Author: adhyaanilkumar. Cycle: V0. Status: approved.

## How to use this file

This is the intent for the project. It says **what** to build and **why**, not how.
Work through it in this order, and stop for my review at each checkpoint:

1. Read this file and write `spec.md`. **Stop for review.**
2. Split `spec.md` into tasks with dependencies (`tasks.md`). **Stop for review.**
3. For each task, write a `plan.md` before coding it. Build one task at a time.

Ask me questions whenever something here is unclear or contradicts itself. Don't guess.

---

## 1. What I want

A system that takes a business request (`intent.md`) and turns it into tested code changes in a target git repository.
Agents do the specifying, splitting, planning, coding and testing. People approve the spec, approve each plan, approve the tested result, merge and deploy.
After deployment, a maintenance agent watches the release and turns any bug or regression it finds into a new `intent.md`, which goes back through the same pipeline.
Work the agents can't finish after 3 attempts goes to human developers on a 5-day sprint board.

This is a personal portfolio project. The goal is a working system with measured results, not a product.

## 2. Why

Coding agents can now write and test most small changes. I want to build the pipeline around them myself, end to end, to:

- learn how to orchestrate multiple agents with clear handoffs and gates;
- measure how often an autonomous flow produces a change a human would approve.



## 3. Who's involved


| Actor               | Type                | Role                                                                                |
| ------------------- | ------------------- | ----------------------------------------------------------------------------------- |
| Business requester  | Human               | Writes `intent.md`                                                                  |
| Spec agent          | Agent               | Turns `intent.md` into `spec.md`                                                    |
| Architect           | Human               | Approves `spec.md` or sends it back with changes                                    |
| Decomposition agent | Agent               | Splits `spec.md` into tasks with a dependency graph                                 |
| Developer agent     | Agent, one per task | Writes `plan.md` for its task, then implements the approved plan                    |
| Testing agent       | Agent, one per task | Writes and runs its own tests on the change                                         |
| Tester              | Human               | Approves or rejects each passing task                                               |
| Developer           | Human               | Approves each task's `plan.md` or sends it back with changes                        |
| Developers          | Human               | Fix tasks that failed 3 times                                                       |
| Maintenance agent   | Agent               | Monitors deployed releases and files new `intent.md` files for bugs and regressions |




## 4. The workflow

1. A requester provides `intent.md` for a target repository.
2. The spec agent reads the intent and the target codebase and writes `spec.md`.
3. **Architect gate:** the architect approves `spec.md` or requests changes. Changes go back to the spec agent with the architect's comments, and the loop repeats until approved. Nothing downstream starts before approval.
4. The decomposition agent splits the approved spec into tasks. Each task has:
  - an id, title, description and acceptance criteria;
  - the tasks it depends on;
  - a flag saying whether it can ship independently.
5. Tasks with no unmet dependencies run in parallel. Each runs in its own isolated branch/worktree.
6. For each task, the developer agent writes `plan.md`.
7. **Plan gate:** a developer approves `plan.md` or requests changes. Changes go back to the developer agent with comments. No code is written before approval.
8. One attempt is:
  - the developer agent changes the code following the approved plan;
  - the testing agent writes and runs tests against the acceptance criteria and reports pass/fail with evidence.
  - On failure, the test feedback goes back to the developer agent for the next attempt.
  - If the agent needs a different plan to fix the failure, the revised `plan.md` goes back through the plan gate before coding continues.
9. A task gets at most **3 attempts**. Time spent waiting at the plan gate doesn't count as an attempt.
10. **Passed** tasks go to the tester for approval. The tester sees the diff, the plan and the test evidence, and approves or rejects.
11. **Failed** tasks (3 attempts used, or rejected by the tester) go onto an **escalation board**: a new sprint running Monday to Friday. Each card includes the task, all attempt logs and the last test output.
12. Approved tasks are merged in dependency order. A task whose dependency failed waits and is not merged alone.
13. Merged work is deployed by a person. In v1, "deploy" can be a configurable script.
14. **Maintain:** after each deployment, the maintenance agent:
  - runs the full test suite plus smoke checks on the deployed version, on a schedule;
  - reads the target app's error logs;
  - when it finds a bug or regression, writes a new `intent.md` describing it, linked to the release and the tasks that likely caused it.
  - New intents enter the pipeline at step 2 and pass through every gate. The maintenance agent never changes code or deploys by itself.



## 5. Must-haves (v0)

- Runs against any local git repository I point it at.
- Every artifact is a file on disk I can read: `intent.md`, `spec.md`, `tasks.md`, per-task `plan.md`, test reports and attempt logs.
- Parallel task execution with isolation between tasks.
- The 3-attempt limit and escalation rules above, enforced by the system, not by the agents.
- Simple human interfaces for the three approval gates (spec, plan, tested result) and for the escalation board. A CLI or a minimal web page is fine.
- Every approval or change request is recorded with who made it, when, and their comments.
- A full log of every agent call: inputs, outputs, duration, tokens/cost.
- Agents never skip a gate, merge or deploy. Only a human action does.
- A maintain phase that can detect at least a failing test or a new error in the logs after deployment and file a linked `intent.md`.



## 6. Evaluation harness (important)

I want measured results to show in my portfolio.

- Replay closed issues from 1–2 well-tested open-source repos: check out the commit before the fix and give the pipeline only the issue text as `intent.md`.
- Judge success with the tests that the real fix added (hidden from the agents).
- Report, at the **ticket** level and the **task** level:
  - pass rate after 1, 2 and 3 attempts;
  - share of tickets fully completed vs partly completed vs escalated;
  - how often specs and plans were sent back for changes, and whether that changed pass rates.
  - cost and time per ticket.
- For unattended runs, the gates can be set to auto-approve, and the report says which gates were automatic.
- Results should be reproducible from a single command and saved as a report file.



## 7. Out of scope (v1)

- Measuring or reducing human wait time.
- Jira/Linear/GitHub Issues integration (intake is a file).
- Real production deployment, multi-tenant hosting, auth, billing.
- Production-grade monitoring (metrics, alerting, paging). In v1, maintain works from the test suite and a log file.
- Training or fine-tuning models.



## 8. Constraints

- Use a current Claude model through an agent SDK or Claude Code in headless mode.
- Keep the agent layer swappable behind a small interface, so another coding agent could be plugged in later.
- Set a configurable spending cap per ticket and per evaluation run.
- Keep it simple enough for one person to build and explain in an interview.



## 9. What done looks like

- I can run the full pipeline on a sample repo from `intent.md` to merged change, with all three approval gates and escalation working, and see the maintain phase file a new intent for a regression I introduce on purpose.
- The evaluation harness runs on at least 50 historical issues and produces a report.
- A README explains the architecture, the workflow and the results, including where the pipeline fails.



## 10. Open questions for the spec

- Language and framework for the orchestrator (e.g. Python vs Java/Spring Boot), and why.
- How task state is stored (files only, SQLite, or something else).
- How the testing agent's tests are kept separate from the hidden evaluation tests.
- How rejected-by-tester tasks are counted in the results.
- What the escalation board looks like in v1.
- How the maintenance agent decides a problem is new rather than one it already reported, and how often it runs.


# Gate decisions: V0 self-build

| When | Gate | Item | Who | Decision | Comments |
|---|---|---|---|---|---|
| 2026-11-10 | spec | spec.md | adhyaanilkumar | approve | |
| 2026-11-10 | tasks | tasks.md | adhyaanilkumar | approve | T01 first; Jira only on escalation; gap choices accepted |
| 2026-11-10 | plan | T01 plan.md | auto | approve | owner set the plan gate to auto for the self-build; rung R0 kept |
| 2026-11-10 | result | T01 attempt 1 | adhyaanilkumar | approve | 46 tests pass; AC6 awaits a real CI run; known minor defect: Makefile UV ?= uv breaks under nested uv run (path with spaces) |
| 2026-11-10 | merge | v0/T01 into v0-design | adhyaanilkumar | approve | fast-forward |
| 2026-11-10 | settings | max_parallel | adhyaanilkumar | 7 | owner: run all of wave 2 at once |
| 2026-11-10 | tasks | tasks.md rerun | adhyaanilkumar | changes | add verify_with per criterion after L1 (T35 asymmetry missed by tests); keep existing code; re-verify wave 1-2 tasks |
| 2026-11-10 | scope | MCP research feature | adhyaanilkumar | later version | kept out of V0; recorded in sdlc/backlog.md |
| 2026-11-10 | tasks | tasks.md rev 2026-11-10 (verify_with, mcp, +31 ACs) | auto | approve | tasks gate default auto; no ids/deps/rungs changed |
| 2026-11-10 | result | T35 attempt 1 | adhyaanilkumar | changes | mascot asymmetric on screen (bg colour bleeds into transparent cells); retry with terminal-render check |
| 2026-11-10 | stop | T06 attempt 2 testing, T35 attempt 2 testing | adhyaanilkumar | hold | owner: back at decomposition, stop all development; attempts not counted; partial tests left uncommitted in worktrees |
| 2026-11-10 | sent back | tasks (from T35 result gate) | adhyaanilkumar | rerun | rerun developer agents for T01, T02, T03, T04, T05, T06, T14, T35 from the revised tasks.md, keeping code; starts after the usage limit resets |
| 2026-11-11 | settings | rung ladder (self-build) | adhyaanilkumar | R6 = Fable 5.2 high | self-build only; spec.md unchanged |
| 2026-11-11 | settings | usage credits, max_parallel | adhyaanilkumar | allow; 8 | owner: usage credits are fine; run every ready task at once (the 8-task rerun) |
| 2026-11-11 | result | T01 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | result | T02 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | result | T03 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | result | T04 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | result | T14 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | result | T35 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T01 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T02 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T03 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T04 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T14 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T35 into v0-design | adhyaanilkumar | approve | |

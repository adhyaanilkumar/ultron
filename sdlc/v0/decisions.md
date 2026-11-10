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

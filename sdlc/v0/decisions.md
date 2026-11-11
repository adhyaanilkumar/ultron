# Gate decisions: V0 self-build

| When | Gate | Item | Who | Decision | Comments |
|---|---|---|---|---|---|
| 2026-11-10 | spec | spec.md | adhyaanilkumar | approve | |
| 2026-11-10 | tasks | tasks.md | adhyaanilkumar | approve | T01 first; Jira only on escalation; gap choices accepted |
| 2026-11-10 | plan | T01 plan.md | auto | approve | owner set the plan gate to auto for the self-build; rung R0 kept |
| 2026-11-10 | result | T01 attempt 1 | adhyaanilkumar | approve | 46 tests pass; AC6 awaits a real CI run; known minor defect: Makefile UV ?= uv breaks under nested uv run (path with spaces) |
| 2026-11-10 | merge | v0/T01 into v0-design | adhyaanilkumar | approve | fast-forward |
| 2026-11-10 | settings | max_parallel | adhyaanilkumar | 7 | owner: run all of wave 2 at once |
| 2026-11-10 | attempt | T05 attempt 1 (R3) | engine | fail | 1 failed, 165 passed: UnicodeEncodeError escapes to_json_line; next attempt climbs to R4 |
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
| 2026-11-11 | settings | Fable use (self-build) | adhyaanilkumar | restrict | Fable 5.2 (R6) only after a counted failure at R5 |
| 2026-11-11 | settings | MCP use (self-build) | adhyaanilkumar | require | agents use the MCP named in each task's mcp line from now on |
| 2026-11-11 | result | T05 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T05 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | settings | Jira for testing | adhyaanilkumar | AK (anil-ws) | tests never use AD (adhya-work); owner then chose AK (anil-ws) instead of a new project; AD stays for real escalations |
| 2026-11-11 | attempt | T06 attempt 2 (R4, rerun) | engine | fail | 3 failed, 429 passed: merge into a symbolic-ref alias moves a checked-out branch (high); marker-write failure leaves the branch behind (low); climb to R5 for attempt 3 (last) |
| 2026-11-11 | attempt | T08 attempt 1 (R2) | engine | fail | 2 failed, 1358 passed: repeat escalation record (Jira ticket) after the requirement settles raises IllegalTransition, so the log can't be replayed; climb to R3 |
| 2026-11-11 | result | T11 attempt 1 | adhyaanilkumar | approve | |
| 2026-11-11 | result | T13 attempt 1 | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T11 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T13 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | attempt | T25 attempt 1 (R3) | engine | fail | 2 failed, 781 passed: auth header kept in JiraUnreachable.__context__ (token leak path); missing attachment file after create raises without issue_key (double filing on retry); climb to R4 |

## Attempt history (owner 2026-11-11)
For every task that needs more than one attempt: what each attempt did differently and why. Updated by the engine after each attempt.

### T05 · Event log
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R3 | First implementation | Failed: a `UnicodeEncodeError` escaped `to_json_line` |
| 2 | R4 | Unencodable text rejected inside the event builders as `EventError`, nothing written | Passed |
| Rerun (send-back, not counted) | R4 | Added `LOCK_BACKEND` so tests prove which OS lock ran; skip/xfail guard; truncation tested at 5 offsets. No behaviour change | Passed, merged |

### T06 · Git layer
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R3 | First implementation | Failed (5): work folder could enclose the repo; bare `git config` write; inherited `GIT_DIR`/`GIT_INDEX_FILE` |
| 2 | R4 | Revised plan: stricter merge rule, `WorkFolderOverlapsRepo`, checked command allowlist, sanitised git env, diff on a temp index | Tester stopped mid-run (owner sent tasks back); not counted |
| 2 (rerun) | R4 | Identity (`samefile`) checks against path aliases of the user's checkout; `ultron-worktree` marker required for commit/remove; refuses tampered `.git` and off-branch worktrees; refuses all-zero sha | Failed (3 tests): merge into a symbolic-ref alias moved a checked-out branch; marker-write failure left the branch behind |
| 3 | R5 | Resolve refs to their final target before the checked-out-branch check; cleanup removes only the branch it just created via `update-ref -d <ref> <sha>`. Found and closed 3 more holes of the same kind: case aliases (`MAIN` = `main` on Windows), dangling symrefs, rebase/bisect in progress. One guard before every ref write; `update-ref --no-deref` only; branch created before the worktree. Interrupted once by the usage limit (resumed, not counted) | Failed (4 tests): repo-wide `worktree prune` can delete a moved user worktree's admin dir; a rebase naming the branch through an alias isn't resolved, so merge moved `main` |

### T08 · State machines
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R2 | First implementation | Failed (2 tests): repeat escalation (Jira ticket) record after the requirement settled raised `IllegalTransition` |
| 2 | R3 | Repeat escalation for an already-escalated task checked first and made a no-op; a first escalation on a settled requirement stays illegal | Passed: 163/163 T08 acceptance tests (24 new); new tests fail on attempt-1 code for the right reason |
| Rerun for D27 (send-back, not counted) | R3 | Added `call_for_edit` with triggers `attachment_missing`, `edit_requested`, `edit_done`; a filed task in call_for_edit counts as escalated; not an attempt | Passed: 220 T08 acceptance tests, 1844 total |
| Reopened for D28 (send-back, not counted) | R3 | One new requirement transition: `awaiting_intent` + `intent.changes` → `intake_drafting` | Passed: tester updated the 3 tests (owner-approved) and added the full loop; 1881 total |

### T13 · Usage and caps (correction, not a failed attempt)
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R1 | Placeholder prices from the previous model generation | Engine sent back before testing |
| 1 (corrected) | R1 | Current list prices (2026-11-03), exact model ids plus aliases, Fable added, Haiku costed at the upper tier | Passed, merged |

### T25 · Jira client
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R3 | First implementation | Failed (2 tests): auth header reachable via `JiraUnreachable.__context__`; missing attachment after create raised without `issue_key` |
| 2 | R4 | Raise outside the `except` so no exception keeps the httpx error; check attachments before creating; wrap post-create failures with `issue_key`; kanban board treated as backlog | Developer green (800 passed); in testing. Pre-create attachment check not added: an acceptance test requires `issue_key` for a file missing from the start Failed at testing (78 tests): HTTP error-status path still reaches the token through the traceback frame local `response` |
| 3 | R5 | `_exchange` is the only httpx caller and returns plain values; errors raised fresh from those; found and closed 4 more leak paths (non-JSON 2xx, non-HTTPError send errors, `from_env`, bad base URL/proxy); token masked in everything uploaded. Same attempt adds D27: ≤280-char description + `report.md`, attachments checked before any call (`MissingAttachments`), `comment()` | Developer: 1139 passed; 10 old acceptance tests updated to D27 by the tester (owner-approved). Passed: 951 T25 acceptance tests, 1745 total; 13 planted bugs all caught |

### T35 · Mascot
| Attempt | Rung | What was different | Result |
|---|---|---|---|
| 1 | R1 | First implementation | Owner sent back at result gate: background colour bled into transparent cells |
| 2 | R1 | Reset colour before every transparent cell | Untested (owner stopped all work) |
| Rerun (send-back, not counted) | R1 | Tests render through `pyte` with colour state carried across cells; Chrome screenshot check | Passed, merged |
| 2026-11-11 | stop | T06 attempt 3, T25 attempt 2 testing | engine | rerun | Pro 5-hour usage limit hit; not counted, no climb; resumed from where they stopped |
| 2026-11-11 | attempt | T25 attempt 2 (R4) | engine | fail | 78 failed, 992 passed: HTTP-status JiraError keeps the token reachable via __traceback__ frame local 'response' (.request.headers authorization); climb to R5 for attempt 3 (last) |
| 2026-11-11 | spec | D27 call_for_edit and short tickets | adhyaanilkumar | approve | failed/rejected → escalated is automatic; attachments checked before filing, missing → call_for_edit (task's developer agent adds it); after filing the user can call for edit with a question; Jira text ≤280 chars unless required |
| 2026-11-11 | tasks | tasks.md rev 2026-11-11 (D27; T25 live checks on AK) | auto | approve | T08 AC6, T25 AC9-AC10, T26 AC7-AC8, T38 AC10 added; no ids/deps/rungs changed |
| 2026-11-11 | sent back | T08 (awaiting result), T25 attempt 3 | engine | rerun | new criteria from D27; T08 reruns at R3 from its code (not counted); T25 attempt 3 takes AC9-AC10 in the same attempt |
| 2026-11-11 | attempt | T06 attempt 3 (R5, last) | engine | fail | 4 failed, 573 passed (same on Linux): repo-wide 'git worktree prune' in remove/cleanup deletes a moved user worktree's admin dir (since attempt 1); branch rebased under a symref alias not resolved, so merge moved main. max_attempts reached |
| 2026-11-11 | escalation | T06 → AD-437 | adhyaanilkumar | escalate | owner chose escalation over a 4th attempt on Fable; Feature under AD-378, active sprint SP-105, 5 story points; T07, T44 and their dependents blocked |
| 2026-11-11 | result | T08 D27 rerun | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T08 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | spec | D28 intent gate allows changes | adhyaanilkumar | approve | T10 AC6 conflicted with T08's strict §3 reading; owner chose to allow intent → changes → intake_drafting |
| 2026-11-11 | sent back | T08 (merged; reopens) | engine | rerun | add the D28 transition; owner-approved update of T08's strict acceptance test; not counted |
| 2026-11-11 | result | T09 | adhyaanilkumar | approve | |
| 2026-11-11 | result | T15 | adhyaanilkumar | approve | |
| 2026-11-11 | result | T25 | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T09 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T15 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T25 into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | flaky | test_t05 lock test, test_t01 make-lint test | engine | note | each failed once under load in full runs and passed alone and on rerun (lesson L10) |
| 2026-11-11 | result | T08 D28 | adhyaanilkumar | approve | |
| 2026-11-11 | merge | v0/T08 (D28) into v0-design | adhyaanilkumar | approve | |
| 2026-11-11 | spec | D29 call_for_edit edge cases | adhyaanilkumar | approve | stop returns to origin; check on every escalation; file with note at cap or after a 2nd round |
| 2026-11-11 | sent back | T05 (merged; reopens) | adhyaanilkumar | rerun | lock starvation: 1 in ~11 runs of the 4-writer test hangs to the 30 s lock timeout; make the lock fair; R4, not counted |
| 2026-11-11 | sent back | T08 (merged; reopens) | engine | rerun | D29 transitions (AC7); R3, not counted |
| 2026-11-11 | plan | AD-437 (T06) | adhyaanilkumar | defer | owner will take AD-437 up at the end |

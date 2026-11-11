# T08 plan: requirement and task state machines, and requirement ids
Author: developer agent (R2, attempt 1). Spec: §3 (states), §7.2 (ids), §8 (gates), §9.1 (stops), FR9, FR10, FR13,
FR18, NFR1. Depends on T05 (`store/events.py`).

## Files that change
- `src/ultron/engine/states.py` (new): `RequirementState` and `TaskState` enums, `IllegalTransition`, two transition
  tables keyed by `(state, trigger)`, `next_requirement_state`, `next_task_state`, `settle`, and `fold(events)` that
  turns a requirement's `Event` list into a `Snapshot` (requirement state plus each task's state and attempt count).
- `src/ultron/engine/ids.py` (new): `new_requirement_id(requirements_dir, ...)` and `REQUIREMENT_ID_RE`.
- `tests/engine/test_states.py`, `tests/engine/test_ids.py` (new). No other file changes.

## Design
- A **trigger** is a string. Most are the event type (`plan_drafted`); a gate decision is `<gate>.<decision>`
  (`plan.approve`); `attempt_finished`, `agent.stopped` and `escalation` are resolved from their data. Events whose
  type is not part of the state vocabulary (`intake`, `agent_call`, `config_snapshot`, merge/deploy gates) are ignored
  by the fold, so later tasks can add event types without breaking it.
- The tables are pure on `(state, trigger)`. Three things need context and are resolved in `fold`, not in the table:
  - a failed attempt is `attempt_failed` (attempts left: back to `planning`, FR9) or `attempts_exhausted` (to
    `failed`), decided from the attempt number and the task's `max_attempts` (set by `task_created`, default 3);
  - `task_resumed` goes back to the state the task was held from;
  - blocked is derived: a `pending` task with a dependency that is `escalated` or `blocked` gets `dependency_escalated`
    (transitively, so a chain behind an escalated task is all blocked).
- A requirement settles when it is `in_progress` and every task is `approved`, `escalated` or `blocked`:
  all approved gives `complete`, none approved gives `escalated`, otherwise `partial`.
- Task events need the requirement `in_progress` (`task_created` needs `decomposing` or `awaiting_tasks`). Starting a
  task whose dependencies are not all `approved` is illegal (dependency order, NFR1).
- Escalation uses the existing `escalation` event (T05) as the driver; a repeat for an already escalated task is a
  no-op (it may only add the Jira key).
- Ids: `R-<yyyymmdd>-<4 hex>` from the local date (`datetime.now()`, injectable) and `secrets.token_hex(2)`
  (injectable); a candidate that exists as a folder in `requirements_dir` is skipped; after 1000 clashes it raises.

## Interpretations (spec ambiguous)
- Strictly §3 for the table: no `changes` at the `intent` gate, no `reject` at `intent`/`spec`/`tasks`/`plan`, no
  `changes` at `result`; each is illegal. The bracketed steps are skipped by direct edges only where the brackets
  say so: `submitted → spec_drafting` (no intake) and `decomposing → in_progress` (auto `tasks` gate). An auto
  `intent`, `spec`, `plan` or `result` gate still goes through its `awaiting_*` state, then an `auto` decision.
- `escalated` is reachable from `rejected` and `failed` (AC3), and also from `held` and the agent states (stop with
  `then=escalate`, §9.1) and from any unfinished task (cap hit, FR18). Not from `approved` or `blocked`.
- `agent.stopped` data `then`: `hold` (default) to `held` (not counted), `retry` counts as a failed attempt,
  `escalate` to `escalated`. Hold works from `planning`, `coding`, `testing`; retry from `coding`, `testing`.

## Order of work
1. Tests first (red): table over every `(state, trigger)` pair; folds; blocked; settle; ids.
2. `states.py`, then `ids.py` (green).
3. `make build`, `make lint`, `make test`.

## Risks
- The trigger vocabulary is new and later tasks (T09 cache, the engine loop) must emit these event types. Kept in one
  place (`states.py`) with constants.
- A stricter-than-needed table could reject a real event order; the tests pin the table so any change is deliberate.

## Proof per criterion
- AC1: `pytest tests/engine/test_states.py`: legal transitions listed by hand in the test, compared to the module
  tables, and every other `(state, trigger)` pair asserted to raise `IllegalTransition`.
- AC2: same file: fold a full-run event list twice, compare snapshots.
- AC3: same file: `rejected` and `failed` escalate; a task behind an escalated dependency is `blocked`.
- AC4: same file: one case each for complete, partial, escalated.
- AC5: `tests/engine/test_ids.py`: regex, local date, forced collision.

## Attempt 2 (R3)
Author: developer agent (R3, attempt 2). Attempt 1 failed 2 of 139 acceptance tests (`test_ac3_a_repeated_escalation_
record_is_harmless`, `test_ac4_escalation_record_with_the_jira_ticket_after_the_requirement_settled`).

**Cause.** In `_apply_task` the check "task events need the requirement `in_progress`" runs before the repeat-escalation
no-op. When a task's escalation settles the requirement (`partial` or `escalated`), the later `escalation` record that
carries the Jira key (spec §12: the ticket and its sprint placement are recorded in the log after filing, and a
pending filing is retried) raises `IllegalTransition`, so a real log can't be replayed. The unit test for the repeat
only covered a requirement still `in_progress` (another task was pending).

**Fix.** In `_apply_task`, handle an `escalation` for a known task that is already `escalated` as a no-op before the
requirement check, whatever the requirement's state. A *first* escalation (task not yet `escalated`) still needs the
requirement `in_progress`, and an `approved` or `blocked` task still can't escalate. No table changes.

**Other events after settling.** Checked T05's types and spec §10/§12: `gate_decision` for `merge`, `deploy` and
`escalation`, `agent_call`, `config_snapshot` and `intake` are already ignored by the fold, and a requirement-level
`escalation` (no task) is already a no-op. The repeated task `escalation` record was the only one that failed.

**Files.** `src/ultron/engine/states.py`, `tests/engine/test_states.py`.

**Order.** Unit tests first (red): a single-task requirement escalates (requirement `escalated`), then the ticket
record; the last unfinished task escalates to `partial`, then the ticket record; a first escalation of an approved
task after the requirement settled is still illegal. Then the fix (green), then build, lint, test.

**Risks.** A repeat record for an escalated task is accepted at any time, so a duplicate written by mistake is silently
accepted; that is harmless because it changes no state. The Jira key itself is not kept in the snapshot (state only).

**Proof.** `pytest tests/engine/test_states.py` (new tests) and `pytest tests/acceptance/test_t08.py` (all pass), then
`make build`, `make lint`, `make test`.

## Rerun for D27 (R3)
Author: developer agent (R3, owner send-back, not a counted attempt). Attempt 2 passed all 163 acceptance tests; its
code stays. This adds only what owner decision D27 needs (spec §3 task states, §12 "Check, then file" and "Call for
edit after filing", T08 AC6).

**New state and events.** `TaskState.CALL_FOR_EDIT = "call_for_edit"`. Three new event types, each its own trigger,
as constants in `store/events.py` beside the others (the module invites later tasks to add types):
- `ATTACHMENT_MISSING = "attachment_missing"`: the engine's pre-filing attachment check found a file missing.
- `EDIT_REQUESTED = "edit_requested"`: the user's question through `ultron_call_for_edit` (human-confirmed).
- `EDIT_DONE = "edit_done"`: the task's developer agent finished the edit (file added, or answer attached).

**Table (strict §3, four new rows).** `failed`/`rejected` + `attachment_missing` → `call_for_edit`;
`call_for_edit` + `edit_done` → `escalated`; `escalated` + `edit_requested` → `call_for_edit`. Nothing else leaves
`call_for_edit`: no attempt events, no hold/retry, no `escalation` (the engine records `edit_done` to leave it). The
existing rows stay, so `failed`/`rejected` + `escalation` → `escalated` (attachments all present) is unchanged.

**Fold.**
- `TaskStatus` gains `edit_from` (default `None`): the state `call_for_edit` was entered from (`failed`, `rejected` or
  `escalated`), like `held_from`. Attempt count is never touched by these events (not an attempt, §3).
- A task is **filed** when it is `escalated`, or in `call_for_edit` entered from `escalated`. For a filed task,
  `edit_requested`, `edit_done` and `escalation` are applied whatever the requirement's state, so a requirement that
  already settled (`partial`/`escalated`) still folds the same way when call-for-edit round trips (and ticket records)
  come after it. A repeated `escalation` record (Jira key, retried filing) on a filed task stays a no-op, also while
  it is in `call_for_edit`.
- For settling and blocking, a filed `call_for_edit` task counts as `escalated`, so the requirement's outcome doesn't
  depend on when the user asks a question. A pre-filing `call_for_edit` task (from `failed`/`rejected`) is unfinished.
- Pre-filing round trips need the requirement `in_progress` (they always are: the task is unfinished).

**Files.** `src/ultron/engine/states.py`, `src/ultron/store/events.py` (three constants),
`tests/engine/test_states.py`. Acceptance tests untouched.

**Order.** Unit tests first (red): table rows and every other `call_for_edit` pair illegal; fold for both paths;
attempt count unchanged; double request and `edit_done` on `escalated` illegal; replay after settling (partial and
escalated), with interleaved ticket records; settling while a filed task is in `call_for_edit`. Then code (green),
then `make build`, `make lint`, `make test`.

**Risks.** A stop of the developer agent while in `call_for_edit` (`agent.stopped`) is illegal in the fold; the engine
must record `edit_done` instead (to settle in T25/T26). `attachment_missing` is legal only from `failed`/`rejected`
(strict §3); escalations from a stop or the cap file without that check unless a later decision widens it. The
acceptance table pins (`LEGAL_TASK`, `UNIVERSE`) don't name the new triggers, so they should still pass; any that
fail are listed in the report for the testing agent.

**Proof (AC6).** `pytest tests/engine/test_states.py` (new table and fold tests), `pytest tests/acceptance/test_t08.py`,
then `make build`, `make lint`, `make test`.

## Reopened for D28 (R3)

**Change.** D28 (spec §3): the intent gate offers Request changes, like spec and plan. One new row in the requirement
table: `awaiting_intent` + `intent.changes` → `intake_drafting`. The comment goes to the intake agent (engine side,
not this task). The fold needs no change: a gate decision on `intent` already maps to `intent.<decision>` through the
table, and a later `intent_drafted` returns the requirement to `awaiting_intent`.

**Files.** `src/ultron/engine/states.py`, `tests/engine/test_states.py`. Acceptance tests untouched.

**Order.** Unit tests first (red): the row in the hand-written legal table (so every other `intent.*` pair stays
illegal), and a fold through intent changes, a redraft and approval. Then the row (green), then `make build`,
`make lint`, `make test`.

**Risks.** `tests/acceptance/test_t08.py::test_ac1_strict_s3_gate_decisions_are_illegal` pins `intent.changes` as
illegal. The owner approved the change (D28); that test belongs to the testing agent and is expected to fail until
it is updated.

**Proof.** `pytest tests/engine/test_states.py`, then `make build`, `make lint`, `make test` (only the acceptance test
above fails).

## Reopened for D29 (R3)
Author: developer agent (R3, owner send-back, not a counted attempt). D29 (spec §3, T08 AC7) settles the
`call_for_edit` edge cases the D27 rerun left open. The D27/D28 code stays; this adds to it.

**Names (new).**
- Event type `FILED_WITH_MISSING = "filed_with_missing"` in `store/events.py` (additive), the "file anyway" record:
  `call_for_edit` → `escalated`, the ticket noting the missing file. Data `reason` is `"cap"`
  (`FILED_REASON_CAP`, the spending cap was reached during the edit) or `"edit_rounds"` (`FILED_REASON_EDIT_ROUNDS`,
  still missing after the last round); `missing` lists the files. Its trigger is the event type.
- Derived trigger `EDIT_STOPPED = "edit_stopped"` (in `states.py`): `agent.stopped` (any `then`) while in
  `call_for_edit` → back to `edit_from`, like `task_resumed` → `held_from`. Never an attempt.
- `EDIT_ROUNDS_MAX = 2` (in `states.py`) and `TaskStatus.edit_round`: the `call_for_edit` round, 1 on entry, 2 after a
  second `attachment_missing`, 0 outside `call_for_edit`.

**Table.**
- `attachment_missing` → `call_for_edit` from every escalatable state (`_ESCALATABLE`: failed, rejected, held, every
  unfinished state at the cap, the agent states for a stop with `then=escalate`), not only failed/rejected.
- `call_for_edit` + `attachment_missing` → `call_for_edit` (second round).
- `call_for_edit` + `filed_with_missing` → `escalated`.
- `edit_stopped` is resolved in `next_task_state(..., edit_from=...)`, legal only from `call_for_edit` with an origin.

**Fold.**
- A second `attachment_missing` is legal only before filing and while `edit_round < EDIT_ROUNDS_MAX`; a third is
  illegal (the engine records `filed_with_missing`, reason `edit_rounds`). `filed_with_missing` is legal only before
  filing; reason `edit_rounds` needs `edit_round == EDIT_ROUNDS_MAX`; reason `cap` is legal in any round; any other
  reason is illegal.
- `agent.stopped` in `call_for_edit` returns to `edit_from` and resets the round; attempt count unchanged. On a filed
  task (`edit_from` escalated) it applies whatever the requirement's state, like `edit_done`, so replay after
  settling still works. `held_from` is kept while a task entered `call_for_edit` from `held`, so a stop back to
  `held` can still resume.
- Everything else from D27/D28 is unchanged: filed tasks count as escalated, a repeated `escalation` is a no-op,
  `escalation` still cannot leave a pre-filing `call_for_edit` (the engine records `filed_with_missing`).

**Files.** `src/ultron/engine/states.py`, `src/ultron/store/events.py` (constants only), `tests/engine/test_states.py`.
Unit tests that pinned the D27-only rules (only `edit_done` leaves `call_for_edit`; `attachment_missing` only from
failed/rejected; a stop or second `attachment_missing` in `call_for_edit` illegal) are updated to D29. Acceptance
tests untouched.

**Order.** Unit tests first (red): table rows; `edit_stopped` resolution; fold for each D29 path (stop pre- and
post-filing, after settling; attachment check from held, coding and a stop with `then=escalate`; two rounds then
`filed_with_missing`; cap in round 1 and 2; illegal third round, early `edit_rounds` filing, unknown reason, filing a
filed task); replay at every prefix. Then code (green), then `make build`, `make lint`, `make test`.

**Risks.** The acceptance tests from D27 pin the old rules (`attachment_missing` only from failed/rejected,
`(call_for_edit, attachment_missing)` illegal, the exact `LEGAL_TASK` table); they will fail until the testing agent
updates them for D29. A stop returning to an agent state (`coding` after a cap check) leaves the engine to escalate
again; that loop is bounded by human stops. `edit_round` is a new `TaskStatus` field (default 0), so snapshots built
by hand elsewhere still compare equal outside `call_for_edit`.

**Proof (AC7).** `pytest tests/engine/test_states.py`, then `make build`, `make lint`, `make test` (only D27-pinned
acceptance tests fail, listed in the report).

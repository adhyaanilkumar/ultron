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

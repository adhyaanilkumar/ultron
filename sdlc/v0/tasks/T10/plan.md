# T10 plan: gates (settings, snapshots, auto decisions and `decisions.md`)

Rung R3, attempt 1. Spec §8, FR4, FR16, D7, D8, NFR1. Depends on T04 (`ultron.config`), T05 (`ultron.store`) and
T08 (`ultron.engine.states`). No MCP (plain Python).

## Files that change
- `src/ultron/engine/gates.py` (new): the gate table, the settings snapshot, mid-run changes, `decide`,
  `request_changes`, the feedback lookup for the agent's next run, and the `decisions.md` renderer.
- `tests/engine/test_gates.py` (new): all proof.
No edits to T04/T05/T08 code or tests, `tests/acceptance/`, `tasks.md`, `tasks.json`, `spec.md` or `decisions.md`.

## Design (uses the existing contracts, redefines none, L9)
- **Gate table** `GATES`: the ten gates (`intent`, `spec`, `tasks`, `plan`, `result`, `escalation`, `maintain`,
  `merge`, `deploy`, `rollback`), each with its level (requirement or task), the state it waits in, the drafting state
  `changes` sends it to (T08's `RequirementState`/`TaskState`), the event that means it was reached, and whether it is
  locked (from T04's `LOCKED_GATES`). A test checks the table matches T04's gate lists.
- **Snapshot at submit**: `Gatekeeper.record_snapshot(loaded)` writes T05's `config_snapshot` (all ten gates resolved
  from the T04 `Config`, plus `max_attempts`) and one `config_warning` event per T04 `ConfigWarning`, in one append.
  A second snapshot for the same requirement is refused.
- **Mid-run change**: `change_gate(gate, mode, who=user)` writes a `config_change` event: gate, from, to, whether the
  requirement-level gate was already reached, or the task ids that already reached a task-level gate, and a warning
  when a default-human gate goes `auto`. `settings()` folds snapshot + changes; `mode(gate, task)` keeps the old mode
  for anything already reached, so a passed gate is never re-opened (and T08's fold ignores these events). Locked
  gates refuse anything but `human`; only a human git identity can change a setting.
- **`decide(gate, item, decision, comment, who, channel, version=None, *, checks_passed=False)`**, the single function
  that records a decision through T05's `gate_decision` builder:
  - `who` is `auto` or a human git identity (`user:…`); `engine` and `agent:*` are refused (agents never pass a gate);
    `auto` ⇔ channel `auto`; human channels are `form` or `terminal`;
  - locked gates (`merge`, `deploy`, `rollback`) refuse `auto` and any non-human `who`;
  - `auto` only `approve`s, only when the snapshot/changes say the gate is `auto` for that item, only with
    `checks_passed=True` (the stage's own checks, passed by the engine), and never on a failing task (state `failed`
    or `rejected`, or a `result` whose last attempt did not pass);
  - `spec` `approve` needs the confirmed version (validated with `engine.version.parse_version`); `version` is
    refused on other gates; `changes` needs a non-empty comment;
  - the candidate event is folded with T08's `fold` before it is written, so an illegal decision (a gate not waiting,
    a gate already passed) raises `IllegalTransition` and writes nothing. The decision event also carries the task's
    current attempt number; gate time never changes an attempt count.
- **`request_changes`** = `decide(..., "changes", ...)` and returns the drafting state; `feedback(gate, item)` returns
  the latest `changes` comment until the next draft of that item, for the agent's next run.
- **`decisions.md`**: `render_decisions(events, requirement)` builds a Markdown table from the `gate_decision` events
  in `seq` order (seq, time, gate, item, decision, who, channel, version, comment; `|` and newlines escaped). No
  generation time, `\n` line endings, written as UTF-8 bytes, so two regenerations are byte-identical. It is derived,
  never edited (L4).

## Order of work
1. Tests first (red): presets × gates and the snapshot event; mid-run change (requirement and task gates, passed gate
   not re-opened, warning recorded); locked and failing-task refusals; decision event fields; `decisions.md` twice;
   `changes` per drafting stage.
2. `gates.py` (green).
3. `make build`, `make lint`, `make test`.

## Interpretations (spec silent or in conflict)
- **AC6 for `intent` (resolved by D28).** Attempt 1 found T08 had chosen strict §3 (no `changes` loop at the `intent`
  gate), so the intent case was a strict `xfail`. The owner decided D28 (the intent gate allows Request changes:
  `awaiting_intent` + `intent.changes` → `intake_drafting`); T08 added the edge and was merged in. The xfail marker is
  removed and the interim "intent changes writes nothing" test deleted; `gates.py` needed no change (the gate table
  already named `intake_drafting`).
- "A failing task is never auto-approved" applies to approval of the task's work. The `escalation` gate (default
  `auto`) exists for failed and rejected tasks, so `auto` is allowed there. A revised plan after a failed attempt may
  pass an `auto` `plan` gate (otherwise `unattended` evaluation could never retry); the task is not failing, it is on
  its next attempt.
- `decide` takes an extra keyword `checks_passed` (default `False`) so the engine states the stage's checks passed;
  auto without it raises.
- "Reached" is event-based: `intent`/`spec`/`tasks`/`plan`/`result` are reached when their draft event (or a decision
  on them) exists; `escalation` when the task is failed, rejected or later; `maintain`, `merge`, `deploy`, `rollback`
  when a decision on them exists. A plan gate a task already reached keeps its mode for that task's revised plans.
- New event types `config_change` and `config_warning` are defined in `gates.py` (T05 keeps `type` open).

## Risks
- The human-only rule is enforced here (security): tested for `engine`, `agent:*` and `auto` on every locked gate.
- `decide` reads, folds, then appends; another writer between read and append could race. V0 runs one runner per
  requirement (lock, §9), so this is accepted and noted.
- Mid-run semantics for task gates are per task; tested with one task past the gate and one before it.

## Proof per criterion (`pytest tests/engine/test_gates.py`)
- AC1: each preset × each gate resolves to the hand-written §8 table; the snapshot event is written at submit.
- AC2: a change before the gate applies; after it, it doesn't, the gate is not re-opened, a `config_change` event is
  written; per-task for `plan`.
- AC3: `auto` on `merge`/`deploy`/`rollback` raises; `auto` on a task whose attempt failed raises; nothing is written.
- AC4: every decision event has who, time, channel, decision, comment; `spec` has the version.
- AC5: regenerate `decisions.md` twice, compare bytes; rows in event order.
- AC6: one case each for `spec` and `plan` and `intent` (drafting state + comment for the next run; intent after D28).

## Attempt 2 (R4)
Attempt 1 failed 4 acceptance tests (`tests/acceptance/test_t10.py`, ba396c2). Only `gates.py` and
`tests/engine/test_gates.py` change.

**Causes**
1. `test_ac2_a_forged_config_change_cannot_make_a_locked_gate_auto[merge|deploy|rollback]`: `settings_from` forced
   locked gates to `human` in the snapshot but then folded every `config_change`, so a `config_change` in the log (one
   `change_gate` would never write) made `mode("merge")` read `auto`.
2. `test_ac3_auto_escalation_is_refused_for_a_task_that_is_not_failing`: `_check_auto` returned early for the
   `escalation` gate, and nothing else checked the task had reached that gate, so an auto approval (and a human one,
   the same gap) was written for a task in `awaiting_plan`. `gate_reached` also counted any `escalation` decision as
   "reached", so such a decision would have frozen that task's mode against later changes.

**Fixes (order of work: failing tests in `tests/engine/test_gates.py` first, then `gates.py`)**
- Locked gates read `human` at every layer: `GateSettings.mode` returns `human` for a locked gate whatever the modes
  or changes say; `settings_from` forces them `human` and skips any `config_change` for a locked gate; `change_gate`
  keeps refusing anything but `human` for them. `settings_from` also ignores a `config_change` that `change_gate`
  could not have written (not by a human identity, an unknown gate, a mode outside `human`/`auto`), and reads a
  snapshot mode outside `human`/`auto` as `human` (fail safe).
- The escalation gate is reached only by a task in `failed`, `rejected`, `call_for_edit` or `escalated` (T08 as it
  stands, D27): failed or rejected tasks, a stop with `then=escalate` and the spending cap (§9.1, FR18) all go there,
  and D29's `attachment_missing` from any escalating state goes to `call_for_edit`. `decide` refuses an `escalation`
  decision (auto or human) for a task in any other state and writes nothing. `blocked` is no longer counted: T08
  never escalates a blocked task, so it gets no ticket and no escalation gate.
- `gate_reached("escalation", task)` is decided by the task's current state only, never by a decision event, so a
  decision on an unreached escalation gate can't freeze the task's mode. (A D29 stop in `call_for_edit` that returns
  the task to an unfinished state leaves the gate; its next arrival takes the mode in force then.)

**Risks**: D29's T08 rerun may add transitions into `call_for_edit` from more states; the set above already covers
them (the reached check reads states, not triggers). Repeated escalation decisions for one task are not refused
(T08's fold ignores escalation decisions; the engine's filing retries are out of T10's scope).

**Proof**: new tests in `tests/engine/test_gates.py` (forged change and snapshot on locked gates, `GateSettings`
built by hand, escalation decisions refused for every non-escalating state, auto and human, nothing written;
reached ignores a forged escalation decision), plus all of `tests/acceptance/test_t10.py`; `make build`, `make lint`,
`make test`.

## Attempt 3 (R5)
Attempt 2 failed 10 acceptance tests (`tests/acceptance/test_t10.py`, 0bed88f), one cause. The owner also decided D30
(one decision per escalation gate), added to this attempt. Only `gates.py` and `tests/engine/test_gates.py` change.

**Cause.** `value in GATE_MODES` (snapshot modes) and `to not in GATE_MODES` (changes) hash the value, so a JSON list
or object there raised `TypeError: unhashable type`, and `settings()`, `change_gate` and every auto decision of that
requirement crashed instead of reading `human` / skipping the change, as attempt 2 said. The audit found the same
class of crash in `int(max_attempts)` (missing, null, list, object, text) and in membership tests on caller values
(`item`, `decision`, `channel`, `mode`, `task`).

**Fix: every value is type-checked before it is compared or converted** (all reads of the log and of callers):
- Snapshot: `gates` must be an object, and an entry counts only if its key names a gate and its value is the string
  `human` or `auto`; any other value (list, object, number, bool, null, other text) reads `human`. Locked gates stay
  `human`. `max_attempts` counts only if it is a positive integer (T05's builder rule); anything else (missing, null,
  bool, float, text, list, object, 0 or less) reads as the default, 3.
- Change: counts only if the actor is a human identity, `gate` names a changeable gate, `to` is the string `human` or
  `auto`, `reached` is a bool and `reached_tasks` a list of task ids (non-empty strings). Anything else, null
  included, is a change `change_gate` could not have written, so it is skipped. An absent `reached` or
  `reached_tasks` still reads as nothing reached. Extra fields are ignored.
- `GateSettings.mode` (also when built by hand): a mode or `to` that is not the string `human` or `auto` reads
  `human`, whatever its type; a change whose `reached` is not a bool or `reached_tasks` not a tuple of strings is
  skipped.
- `feedback`: a `changes` decision whose comment is not a string gives `""` (changes were asked; no readable comment).
  `gate_reached`, `feedback` and `render_decisions` only compare log values with `==`, never hash them.
- Callers: `decide` raises `GateError` for a gate, item, decision, channel or version that is not a string, and auto
  needs `checks_passed` to be exactly `True`; `change_gate` likewise for gate and mode; `gate_reached` on the
  escalation gate is `False` for a task that is not a string.
So no event T05 accepts can crash `settings()`. A log T08 cannot fold (e.g. a decision whose gate is a list) still
makes `decide` and `change_gate` raise `IllegalTransition` and write nothing (unchanged, fails closed).

**D30 (owner, 2026-11-11).** Once a task's escalation gate is decided, `decide` refuses any further escalation
decision on it (auto or human, same or another decision) with `GateError` and writes nothing. Retrying the filing
(`escalation`, `edit_requested`, `edit_done` events) is unaffected.
- "Decided" means a decision recorded during the task's current stay at the gate that `decide` could have written:
  a human identity on `form` or `terminal`, or `auto` approving while the gate was `auto` for that task; with a string
  decision and comment (not blank for `changes`) and no version. One recorded before the task reached the gate (attempt 2's rule), or by the
  engine or an agent, or with another shape, never counts.
- A stay lasts while the task is `failed`, `rejected`, `call_for_edit` or `escalated`. With T08 as on this branch a
  task never leaves them. With D29's T08 (`v0-design`) a stop in `call_for_edit` can return the task to an unfinished
  state; its next arrival is a new stay with a new decision, as a new draft re-opens the other gates. Its test runs
  once D29's T08 is in this branch (skipped until then; checked against `v0-design`'s T08 in a scratch copy).
- A human `changes` decision on the escalation gate is still taken (its meaning is the owner's open question) and is
  the gate's one decision.

**Out of scope (owner decisions pending; unchanged):** the `maintain` gate's reach and repeat rules (T33), `changes`
on gates with no changes loop.

**Risks.** D30 folds a prefix of the log at each of the task's events during a stay (task events x log length); fine
at V0 sizes and only on an escalation decision. A malformed `max_attempts` reads as 3 silently (T10 has no logger).

**Proof** (`tests/engine/test_gates.py`, failing first): each type that is not a string, bool or count (list, object,
number, bool, null) at every field settings read (each snapshot mode, `gates`, `max_attempts`, a change's `gate`,
`to`, `reached`, `reached_tasks`) leaves `settings()`, `change_gate` and auto decisions working; decision events with
odd fields; callers' values of the wrong type raise `GateError`; D30 for every pair of decisions in every reached
state (log byte-identical), per task, forged decisions not counting, filing retries unaffected. Plus all of
`tests/acceptance/test_t10.py`; `make build`, `make lint`, `make test`.

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
- **AC6 for `intent` conflicts with T08.** T08 chose strict §3 (no `changes` loop at the `intent` gate) and its
  acceptance test pins `intent.changes` as illegal. T10 must not change T08, so `request_changes` on `intent` raises
  `IllegalTransition` and writes nothing. The gate table still names `intake_drafting` as intent's drafting state; the
  AC6 intent case is a strict `xfail` test that will start failing (and so must be un-marked) once T08 adds the edge.
  Reported to the owner.
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
- AC6: one case each for `spec` and `plan` (drafting state + comment for the next run); `intent` strict xfail (above).

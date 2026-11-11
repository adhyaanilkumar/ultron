# T15 plan: `AgentRunner` interface, fake runner, activity events and call log

Rung R1, attempt 1. Spec §11.1, §9.1 "What an ultron agent reports", FR17, FR18.

## Files that change (all new)
- `src/ultron/agents/interface.py`: the swappable interface. Imports only the standard library (AC5). Holds `AgentRunner` (Protocol), `AgentResult`, `Usage`, `RunLimits` (requested model and effort, turn and time limits), `StopReason`, `ActivityKind`, `ActivityEvent`, and `to_dict`/`from_dict` JSON helpers.
- `src/ultron/agents/calllog.py`: `LoggingRunner`, a wrapper around any `AgentRunner`. Order per call: `check_cap` (T13) -> refuse with `CapRefusedError` before anything is written or started -> run -> cost with `usage_cost` -> write one transcript under `.ultron/transcripts/` -> append one `agent_call` event (T05 builder) -> add to the `UsageLedger` -> append activity to `.ultron/activity.jsonl`.
- `src/ultron/agents/fake.py`: `FakeAgentRunner` with `FakeStep` scripts (output, data, activity, usage, diff files written into the workdir, stop reason, model/effort that "ran", raise).
- `tests/agents/__init__.py`, `test_fake_runner.py`, `test_call_log.py`, `test_result_types.py`, `test_interface_imports.py`.

## Order of work
1. Tests first (red), then interface, fake, logger.
2. build, lint, test.

## Decisions and interpretations (spec is silent)
- Spec's `run(role, prompt, workdir, allowed_tools, output_schema, limits)` has no model/effort argument, so they travel in `RunLimits` (engine-chosen, requested). `AgentResult.model` and `.effort` are what actually ran; the `agent_call` event and the cost use the ran values. The transcript records both so a silent effort downgrade is visible.
- Extra keyword `on_activity` on `run` for live activity; the full list is also in `AgentResult.activity`.
- The cost in the event is recomputed by the wrapper from usage with the T13 price table, not trusted from the adapter (`AgentResult.cost_usd` is the adapter's own estimate, kept for the record).
- A refused call raises `CapRefusedError` (carries the `CapDecision`); the caller (engine) decides escalate vs stop.
- Context (requirement, task, attempt) is passed per call to the wrapper as `CallContext`.
- Event `transcript` field is the path relative to the `.ultron` dir, in POSIX form.
- Stop reasons: done, turn_limit, budget, usage_limit, overload, error, interrupted ("stop").
- `interrupt` is adapter-specific (T16); not on the V0 protocol. The fake can script an `interrupted` result.

## Risks
- Every module depends on result shape (rubric). Mitigated by keeping the dataclasses small and adding fields only as optional later.
- Windows paths: transcripts named by ids only; no spaces assumed.

## Proof per criterion
- AC1: `tests/agents/test_fake_runner.py` scripted results in order, activity emitted, diff written, failures.
- AC2: `tests/agents/test_call_log.py` one transcript file, one event, all FR17 fields.
- AC3: `tests/agents/test_call_log.py` refused call: no transcript, no event, fake never invoked.
- AC4: `tests/agents/test_result_types.py` JSON round-trip.
- AC5: `tests/agents/test_interface_imports.py` AST import-graph test.

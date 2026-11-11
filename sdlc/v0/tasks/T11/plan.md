# T11 plan: task graph (schema, validation, rubric and rungs)

Rung R1, attempt 1. Spec FR5, FR6, FR14, section 11.4. No MCP (plain Python).

## Files that change
- `src/ultron/engine/task_graph.py` (new): pydantic schema, validation, `compute_rung`, `ready`, `merge_order`, and a `main()` so `python -m ultron.engine.task_graph <file>` validates a file (exit 0 ok, 1 invalid).
- `tests/engine/test_task_graph.py` (new): all proof.
No edits to config, cli, tasks.json, tasks.md, spec.md or acceptance tests.

## Order of work
1. Write tests first (cycle, unknown dep, rung table 0-12 x Risk 0-2, rung replacement, ready, merge_order, real tasks.json, unknown verify_with, mcp field).
2. Implement schema: `Task` (id, title, type story/feature/bug, description, acceptance_criteria[id, text, needs_owner, added?, verify_with[{method, detail}]], depends_on, ships_independently, rubric of six factors {score 0-2, reason}, total, rung, jira, mcp {servers, reason}). Top-level file keys other than `tasks` are ignored.
3. Rung: total of scores -> R0..R4 by table; Risk 2 raises to at least R3; the result is capped at `max_rung` (default R5, ladder order from `ultron.config.defaults.RUNGS`).
4. Validation collects every problem and raises `TaskGraphError` with one actionable line each: unknown dependency (`T3 depends on unknown task T9`), cycle (`dependency cycle: T1 -> T2 -> T1`), duplicate ids, unknown `verify_with` method (`T11 AC7: unknown verify_with method 'x'; allowed: ...`).
5. A supplied rung or total that disagrees with the scores is replaced by the computed one; `graph.replacements` lists each (task, field, supplied, computed) with a readable message.
6. `ready(graph, states)`, `merge_order(graph)` (Kahn with a min-heap on id: stable, ties by id).

## Interpretations
- `ready` returns task ids sorted by id, literally those whose dependencies are all `approved` (a task with no dependencies is always included; the scheduler filters out tasks already started). A task missing from `states` counts as not approved.
- The agent-supplied `total` is also recomputed and reported if wrong.
- `max_rung` is a parameter (from config `Ladder.max_rung` by the caller), not read from config here.

## Risks
- Real tasks.json field names are inferred from the file, not a spec table; test AC6/AC7 against the real file guards that.
- Windows console encoding for the CLI: output stays ASCII-safe via the messages only containing ids.

## Proof per criterion
- AC1: pytest `tests/engine/test_task_graph.py` (cycle ids and unknown-dependency ids in the message).
- AC2: pytest parametrised totals 0-12 x Risk 0-2 against an independent expected table.
- AC3: pytest, supplied wrong rung is replaced and reported.
- AC4: pytest, ready set.
- AC5: pytest, topological validity and tie-break by id.
- AC6: pytest over `sdlc/v0/tasks.json`; command `uv run python -m ultron.engine.task_graph sdlc/v0/tasks.json` exits 0.
- AC7: pytest, real file passes, fixture with unknown method fails naming task and criterion ids.

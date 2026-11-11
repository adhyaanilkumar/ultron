"""Task graph: the `tasks.json` schema, validation, rungs, ready set and merge order (spec FR5, FR6, FR14, 11.4).

Pure logic. The engine recomputes each task's rung from its rubric scores, so an agent cannot pick a rung
that does not match them. Run `python -m ultron.engine.task_graph <tasks.json>` to validate a file.
"""

from __future__ import annotations

import heapq
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ultron.config.defaults import RUNGS

TaskType = Literal["story", "feature", "bug"]

VERIFY_METHODS: tuple[str, ...] = (
    "pytest",
    "command",
    "terminal-render",
    "screenshot-browser",
    "screenshot-terminal",
    "mcp-client",
    "ci",
    "owner",
    "live-call",
)

FACTORS: tuple[str, ...] = (
    "files",
    "change_type",
    "risk",
    "ambiguity",
    "algorithmic",
    "test_surface",
)

# (highest total, rung); anything above the last limit is R4 (spec 11.4).
_TOTAL_TO_RUNG: tuple[tuple[int, str], ...] = (
    (2, "R0"),
    (4, "R1"),
    (6, "R2"),
    (8, "R3"),
)
_RISK_FLOOR = "R3"


class TaskGraphError(ValueError):
    """The graph is invalid. The message lists every problem, one per line, naming the ids."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Verify(_Model):
    method: str
    detail: str = ""


class Criterion(_Model):
    id: str
    text: str
    needs_owner: bool = False
    added: str | None = None
    verify_with: list[Verify] = Field(min_length=1)


class Factor(_Model):
    score: int = Field(ge=0, le=2)
    reason: str = ""


class Rubric(_Model):
    files: Factor
    change_type: Factor
    risk: Factor
    ambiguity: Factor
    algorithmic: Factor
    test_surface: Factor

    def scores(self) -> dict[str, int]:
        return {f: getattr(self, f).score for f in FACTORS}


class Mcp(_Model):
    servers: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""


class Task(_Model):
    id: str
    title: str
    type: TaskType
    description: str
    acceptance_criteria: list[Criterion]
    depends_on: list[str] = Field(default_factory=list)
    ships_independently: bool
    rubric: Rubric
    total: int
    rung: str
    jira: str = ""
    mcp: Mcp


@dataclass(frozen=True)
class Replacement:
    """An agent-supplied value the engine replaced with the computed one."""

    task_id: str
    field: str
    supplied: str
    computed: str

    @property
    def message(self) -> str:
        return (
            f"{self.task_id}: supplied {self.field} {self.supplied} does not match the "
            f"rubric scores; replaced with computed {self.computed}"
        )


@dataclass(frozen=True)
class TaskGraph:
    tasks: dict[str, Task]
    replacements: list[Replacement]


def compute_rung(scores: Mapping[str, int], max_rung: str = RUNGS[-1]) -> str:
    """The starting rung for rubric `scores`: table by total, Risk 2 floors at R3, capped at `max_rung`."""
    if max_rung not in RUNGS:
        raise ValueError(f"unknown max_rung {max_rung!r}; allowed {list(RUNGS)}")
    total = sum(scores.values())
    rung = next((r for limit, r in _TOTAL_TO_RUNG if total <= limit), "R4")
    if scores.get("risk", 0) >= 2 and RUNGS.index(rung) < RUNGS.index(_RISK_FLOOR):
        rung = _RISK_FLOOR
    return rung if RUNGS.index(rung) <= RUNGS.index(max_rung) else max_rung


def _structural_errors(index: int, raw: Any, err: ValidationError) -> list[str]:
    tid = raw.get("id", f"#{index}") if isinstance(raw, dict) else f"#{index}"
    lines: list[str] = []
    for e in err.errors():
        loc = e["loc"]
        where = f"task {tid}"
        if (
            loc
            and loc[0] == "acceptance_criteria"
            and len(loc) > 1
            and isinstance(loc[1], int)
        ):
            crit = raw.get("acceptance_criteria", [])
            cid = (
                crit[loc[1]].get("id", f"#{loc[1]}")
                if loc[1] < len(crit)
                else f"#{loc[1]}"
            )
            where = f"task {tid} criterion {cid}"
        path = ".".join(str(p) for p in loc)
        lines.append(f"{where}: {path}: {e['msg']}")
    return lines


def _verify_errors(task: Task) -> list[str]:
    return [
        f"task {task.id} criterion {c.id}: unknown verify_with method {v.method!r}; "
        f"allowed: {', '.join(VERIFY_METHODS)}"
        for c in task.acceptance_criteria
        for v in c.verify_with
        if v.method not in VERIFY_METHODS
    ]


def _find_cycle(deps: Mapping[str, Sequence[str]]) -> list[str] | None:
    """One cycle as `[a, b, ..., a]`, or None. Only edges to known tasks are followed."""
    state: dict[str, int] = {}  # 1 visiting, 2 done
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(deps[node]):
            if nxt not in deps:
                continue
            if state.get(nxt) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if nxt not in state and (found := visit(nxt)) is not None:
                return found
        stack.pop()
        state[node] = 2
        return None

    for start in sorted(deps):
        if start not in state and (found := visit(start)) is not None:
            return found
    return None


def validate_graph(data: Mapping[str, Any], max_rung: str = RUNGS[-1]) -> TaskGraph:
    """Validate `data` (the parsed `tasks.json`) and return the graph with engine-computed totals and rungs.

    Raises `TaskGraphError` listing every problem (schema, duplicate ids, unknown dependencies, cycles).
    """
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        raise TaskGraphError("tasks.json needs a top-level 'tasks' list")
    errors: list[str] = []
    parsed: list[Task] = []
    for i, raw in enumerate(raw_tasks):
        try:
            t = Task.model_validate(raw)
        except ValidationError as e:
            errors.extend(_structural_errors(i, raw, e))
            continue
        errors.extend(_verify_errors(t))
        parsed.append(t)
    if errors:
        raise TaskGraphError("\n".join(errors))

    by_id: dict[str, Task] = {}
    for t in parsed:
        if t.id in by_id:
            errors.append(f"duplicate task id {t.id}: ids must be unique")
        by_id[t.id] = t
    for t in parsed:
        for d in t.depends_on:
            if d not in by_id:
                errors.append(
                    f"task {t.id} depends on unknown task {d}: "
                    "fix the id or add the task"
                )
    cycle = _find_cycle({t.id: t.depends_on for t in by_id.values()})
    if cycle is not None:
        errors.append(
            f"dependency cycle: {' -> '.join(cycle)}: remove one of these dependencies"
        )
    if errors:
        raise TaskGraphError("\n".join(errors))

    replacements: list[Replacement] = []
    fixed: dict[str, Task] = {}
    for t in by_id.values():
        scores = t.rubric.scores()
        total = sum(scores.values())
        rung = compute_rung(scores, max_rung)
        if t.total != total:
            replacements.append(Replacement(t.id, "total", str(t.total), str(total)))
        if t.rung != rung:
            replacements.append(Replacement(t.id, "rung", t.rung, rung))
        fixed[t.id] = t.model_copy(update={"total": total, "rung": rung})
    return TaskGraph(tasks=fixed, replacements=replacements)


def load_graph(path: Path | str, max_rung: str = RUNGS[-1]) -> TaskGraph:
    """Read and validate a `tasks.json` file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TaskGraphError("tasks.json must be a JSON object")
    return validate_graph(data, max_rung)


def ready(graph: TaskGraph, states: Mapping[str, str]) -> list[str]:
    """Ids (sorted) of the tasks whose dependencies are all `approved`. Unknown states count as not approved."""
    return sorted(
        t.id
        for t in graph.tasks.values()
        if all(states.get(d) == "approved" for d in t.depends_on)
    )


def merge_order(graph: TaskGraph) -> list[str]:
    """A topological order of the task ids; among tasks that could go next, the smaller id goes first."""
    remaining = {t.id: len(set(t.depends_on)) for t in graph.tasks.values()}
    dependents: dict[str, list[str]] = {tid: [] for tid in graph.tasks}
    for t in graph.tasks.values():
        for d in set(t.depends_on):
            dependents[d].append(t.id)
    heap = [tid for tid, n in remaining.items() if n == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        tid = heapq.heappop(heap)
        order.append(tid)
        for nxt in dependents[tid]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                heapq.heappush(heap, nxt)
    return order


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m ultron.engine.task_graph <tasks.json>", file=sys.stderr)
        return 2
    try:
        graph = load_graph(args[0])
    except (TaskGraphError, OSError, json.JSONDecodeError) as e:
        print(f"invalid {args[0]}:\n{e}", file=sys.stderr)
        return 1
    for r in graph.replacements:
        print(r.message)
    print(f"ok: {len(graph.tasks)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())

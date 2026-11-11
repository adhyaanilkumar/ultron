"""T11: task graph schema, validation, rung computation, ready set and merge order."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ultron.engine.task_graph import (
    TaskGraph,
    TaskGraphError,
    compute_rung,
    load_graph,
    merge_order,
    ready,
    validate_graph,
)

REPO = Path(__file__).resolve().parents[2]
REAL = REPO / "sdlc" / "v0" / "tasks.json"
FACTORS = ["files", "change_type", "risk", "ambiguity", "algorithmic", "test_surface"]


def scores(**kw: int) -> dict[str, dict[str, Any]]:
    return {f: {"score": kw.get(f, 0), "reason": "r"} for f in FACTORS}


def task(tid: str, deps: list[str] | None = None, **kw: int) -> dict[str, Any]:
    s = scores(**kw)
    total = sum(v["score"] for v in s.values())
    return {
        "id": tid,
        "title": f"Task {tid}",
        "type": "feature",
        "description": "d",
        "acceptance_criteria": [
            {
                "id": "AC1",
                "text": "t",
                "needs_owner": False,
                "verify_with": [{"method": "pytest", "detail": "x"}],
            }
        ],
        "depends_on": deps or [],
        "ships_independently": True,
        "rubric": s,
        "total": total,
        "rung": compute_rung({f: v["score"] for f, v in s.items()}),
        "jira": "",
        "mcp": {"servers": [], "reason": "none"},
    }


def graph(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": list(tasks)}


def test_cycle_names_ids() -> None:
    with pytest.raises(TaskGraphError) as e:
        validate_graph(
            graph(task("T1", ["T3"]), task("T2", ["T1"]), task("T3", ["T2"]))
        )
    msg = str(e.value)
    assert "cycle" in msg
    for tid in ("T1", "T2", "T3"):
        assert tid in msg


def test_self_dependency_is_a_cycle() -> None:
    with pytest.raises(TaskGraphError, match="cycle.*T1"):
        validate_graph(graph(task("T1", ["T1"])))


def test_unknown_dependency_names_ids() -> None:
    with pytest.raises(TaskGraphError) as e:
        validate_graph(graph(task("T1"), task("T2", ["T1", "T9"])))
    assert "T2" in str(e.value)
    assert "T9" in str(e.value)


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(TaskGraphError, match="duplicate.*T1"):
        validate_graph(graph(task("T1"), task("T1")))


def _expected(total: int, risk: int) -> str:
    table = [(2, "R0"), (4, "R1"), (6, "R2"), (8, "R3")]
    rung = next((r for limit, r in table if total <= limit), "R4")
    if risk == 2 and rung in ("R0", "R1", "R2"):
        rung = "R3"
    return rung


def _split(total: int, risk: int) -> dict[str, int] | None:
    """Scores (each 0-2) with the given risk and total, or None if impossible."""
    rest = total - risk
    if not 0 <= rest <= 10:
        return None
    out = {"risk": risk}
    for f in FACTORS:
        if f != "risk":
            take = min(2, rest)
            out[f] = take
            rest -= take
    return out


@pytest.mark.parametrize("risk", [0, 1, 2])
@pytest.mark.parametrize("total", range(13))
def test_rung_table(total: int, risk: int) -> None:
    s = _split(total, risk)
    if s is None:
        pytest.skip("total not reachable with this Risk score")
    assert sum(s.values()) == total
    assert compute_rung(s) == _expected(total, risk)


def test_rung_capped_at_max_rung() -> None:
    s = {f: 2 for f in FACTORS}
    assert compute_rung(s) == "R4"
    assert compute_rung(s, max_rung="R2") == "R2"
    low_risk2 = {**{f: 0 for f in FACTORS}, "risk": 2}
    assert compute_rung(low_risk2, max_rung="R1") == "R1"


def test_wrong_rung_replaced_and_reported() -> None:
    t = task("T1", files=1, algorithmic=1, test_surface=1)  # total 3 -> R1
    t["rung"] = "R4"
    g = validate_graph(graph(t))
    assert g.tasks["T1"].rung == "R1"
    assert len(g.replacements) == 1
    r = g.replacements[0]
    assert (r.task_id, r.field, r.supplied, r.computed) == ("T1", "rung", "R4", "R1")
    assert "T1" in r.message and "R4" in r.message and "R1" in r.message


def test_wrong_total_replaced_and_reported() -> None:
    t = task("T1", files=1)
    t["total"] = 9
    g = validate_graph(graph(t))
    assert g.tasks["T1"].total == 1
    assert [r.field for r in g.replacements] == ["total"]


def test_matching_rung_reports_nothing() -> None:
    assert validate_graph(graph(task("T1", files=2))).replacements == []


def test_rung_capped_during_validation() -> None:
    t = task("T1", risk=2)  # R3
    g = validate_graph(graph(t), max_rung="R2")
    assert g.tasks["T1"].rung == "R2"
    assert [r.field for r in g.replacements] == ["rung"]


def _diamond() -> TaskGraph:
    return validate_graph(
        graph(
            task("T4", ["T2", "T3"]),
            task("T3", ["T1"]),
            task("T2", ["T1"]),
            task("T1"),
            task("T5"),
        )
    )


def test_ready_only_when_all_deps_approved() -> None:
    g = _diamond()
    assert ready(g, {}) == ["T1", "T5"]
    assert ready(g, {"T1": "approved"}) == ["T1", "T2", "T3", "T5"]
    done = {t: "approved" for t in ("T1", "T2", "T3")}
    assert "T4" in ready(g, done)
    assert "T4" not in ready(g, {"T1": "approved", "T2": "approved", "T3": "passed"})
    assert "T4" not in ready(g, {"T1": "approved", "T2": "approved"})


def test_merge_order_topological_and_stable() -> None:
    g = _diamond()
    order = merge_order(g)
    assert order == ["T1", "T2", "T3", "T4", "T5"]
    pos = {t: i for i, t in enumerate(order)}
    for t in g.tasks.values():
        for d in t.depends_on:
            assert pos[d] < pos[t.id]
    g2 = validate_graph(
        graph(
            task("T5"),
            task("T1"),
            task("T3", ["T1"]),
            task("T2", ["T1"]),
            task("T4", ["T2", "T3"]),
        )
    )
    assert merge_order(g2) == order


def test_ties_broken_by_id() -> None:
    g = validate_graph(graph(task("T3"), task("T10"), task("T2", ["T3"])))
    assert merge_order(g) == ["T10", "T3", "T2"]


def test_real_tasks_json_passes() -> None:
    g = load_graph(REAL)
    assert "T11" in g.tasks
    assert merge_order(g)[0] == "T01"
    assert g.replacements == []


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ultron.engine.task_graph", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_tasks_json_command_exits_zero() -> None:
    r = _run(REAL)
    assert r.returncode == 0, r.stderr


def test_command_exits_nonzero_on_bad_file(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text('{"tasks": [{"id": "T1", "depends_on": ["T1"]}]}', encoding="utf-8")
    r = _run(p)
    assert r.returncode == 1
    assert "T1" in r.stderr


def test_schema_accepts_verify_with_and_mcp() -> None:
    g = load_graph(REAL)
    t = g.tasks["T11"]
    assert t.mcp.servers == []
    assert t.acceptance_criteria[0].verify_with[0].method == "pytest"


def test_unknown_verify_with_method_names_task_and_criterion() -> None:
    t = task("T7")
    bad = copy.deepcopy(t["acceptance_criteria"][0])
    bad["id"] = "AC2"
    bad["verify_with"] = [{"method": "telepathy", "detail": "x"}]
    t["acceptance_criteria"].append(bad)
    with pytest.raises(TaskGraphError) as e:
        validate_graph(graph(t))
    msg = str(e.value)
    assert "T7" in msg and "AC2" in msg and "telepathy" in msg


def test_criterion_without_verify_with_rejected() -> None:
    t = task("T7")
    t["acceptance_criteria"][0]["verify_with"] = []
    with pytest.raises(TaskGraphError, match="T7.*AC1"):
        validate_graph(graph(t))


def test_bad_score_and_missing_field_name_the_task() -> None:
    t = task("T8")
    t["rubric"]["risk"]["score"] = 3
    with pytest.raises(TaskGraphError, match="T8"):
        validate_graph(graph(t))
    t2 = task("T9")
    del t2["title"]
    with pytest.raises(TaskGraphError, match="T9"):
        validate_graph(graph(t2))

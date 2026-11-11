"""Acceptance tests for T11 (task graph), written by the testing agent from the ACs and spec 11.4 / FR6.

Each AC is `pytest` except AC6 (also `command`). The expected rung table is built here from the spec text.
"""

import copy
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ultron.engine import task_graph as tg
from ultron.engine.task_graph import (
    TaskGraphError,
    compute_rung,
    load_graph,
    merge_order,
    ready,
    validate_graph,
)

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "sdlc" / "v0" / "tasks.json"
FACTORS = ["files", "change_type", "risk", "ambiguity", "algorithmic", "test_surface"]
LADDER = ["R0", "R1", "R2", "R3", "R4"]


def spec_rung(total: int, risk: int) -> str:
    """Spec 11.4: 0-2 R0, 3-4 R1, 5-6 R2, 7-8 R3, 9+ R4; Risk 2 forces at least R3."""
    if total <= 2:
        r = 0
    elif total <= 4:
        r = 1
    elif total <= 6:
        r = 2
    elif total <= 8:
        r = 3
    else:
        r = 4
    if risk == 2:
        r = max(r, 3)
    return LADDER[r]


def feasible(total: int, risk: int) -> bool:
    """Six factors of 0..2, one of them fixed at `risk`, can make `total`."""
    return 0 <= total - risk <= 10


def scores_for(total: int, risk: int) -> dict[str, int]:
    rest = total - risk
    assert 0 <= rest <= 10
    out = {"risk": risk}
    for f in (f for f in FACTORS if f != "risk"):
        take = min(2, rest)
        out[f] = take
        rest -= take
    assert rest == 0
    return out


def task(
    tid: str,
    deps: list[str] | None = None,
    scores: dict[str, int] | None = None,
    rung: str = "R1",
    total: int | None = None,
    methods: tuple[str, ...] = ("pytest",),
) -> dict[str, Any]:
    scores = scores or dict.fromkeys(FACTORS, 0)
    return {
        "id": tid,
        "title": f"title {tid}",
        "type": "feature",
        "description": "d",
        "acceptance_criteria": [
            {
                "id": "AC1",
                "text": "x",
                "verify_with": [{"method": m, "detail": "d"} for m in methods],
            }
        ],
        "depends_on": deps or [],
        "ships_independently": True,
        "rubric": {f: {"score": s, "reason": "r"} for f, s in scores.items()},
        "total": sum(scores.values()) if total is None else total,
        "rung": rung,
        "jira": "",
        "mcp": {"servers": [], "reason": "none"},
    }


def graph(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": list(tasks)}


def fails(data: dict[str, Any]) -> str:
    with pytest.raises(TaskGraphError) as ei:
        validate_graph(data)
    return str(ei.value)


# ---------------- AC1 ----------------
def test_ac1_self_cycle() -> None:
    msg = fails(graph(task("A", ["A"])))
    assert "A" in msg and "cycle" in msg.lower()


def test_ac1_two_cycle() -> None:
    msg = fails(graph(task("A", ["B"]), task("B", ["A"])))
    assert "A" in msg and "B" in msg


def test_ac1_long_cycle_with_innocent_tail() -> None:
    msg = fails(
        graph(
            task("T1"),
            task("T2", ["T1", "T5"]),
            task("T3", ["T2"]),
            task("T4", ["T3"]),
            task("T5", ["T4"]),
            task("T6", ["T1"]),
        )
    )
    for t in ("T2", "T3", "T4", "T5"):
        assert t in msg
    assert "T6" not in msg


def test_ac1_cycle_not_reachable_from_roots() -> None:
    msg = fails(graph(task("A"), task("X", ["Y"]), task("Y", ["Z"]), task("Z", ["X"])))
    assert all(t in msg for t in "XYZ")


def test_ac1_diamond_is_not_a_cycle() -> None:
    g = validate_graph(
        graph(task("A"), task("B", ["A"]), task("C", ["A"]), task("D", ["B", "C"]))
    )
    assert set(g.tasks) == {"A", "B", "C", "D"}


def test_ac1_unknown_dependency_names_both_ids() -> None:
    msg = fails(graph(task("T1"), task("T2", ["T1", "T99"])))
    assert "T2" in msg and "T99" in msg


def test_ac1_multiple_unknowns_all_reported() -> None:
    msg = fails(graph(task("A", ["N1"]), task("B", ["N2"])))
    assert all(x in msg for x in ("A", "B", "N1", "N2"))


def test_ac1_duplicate_id() -> None:
    msg = fails(graph(task("A"), task("A")))
    assert "A" in msg and "duplicate" in msg.lower()


def test_ac1_missing_id_and_missing_tasks_list() -> None:
    bad = task("A")
    del bad["id"]
    with pytest.raises(TaskGraphError):
        validate_graph(graph(bad))
    with pytest.raises(TaskGraphError):
        validate_graph({})


def test_ac1_valid_graph_passes_and_empty_graph_ok() -> None:
    assert validate_graph(graph(task("A"), task("B", ["A"]))).tasks.keys() == {"A", "B"}
    assert validate_graph(graph()).tasks == {}


# ---------------- AC2 ----------------
@pytest.mark.parametrize("risk", [0, 1, 2])
@pytest.mark.parametrize("total", range(13))
def test_ac2_rung_table(total: int, risk: int) -> None:
    if not feasible(total, risk):
        # Six factors of 0..2 cannot make this total with this Risk, so no input exists.
        assert (total, risk) in {(0, 1), (0, 2), (1, 2), (11, 0), (12, 0), (12, 1)}
        return
    sc = scores_for(total, risk)
    assert sum(sc.values()) == total
    assert compute_rung(sc) == spec_rung(total, risk)
    g = validate_graph(graph(task("A", scores=sc, rung="R0")))
    assert g.tasks["A"].rung == spec_rung(total, risk)
    assert g.tasks["A"].total == total


@pytest.mark.parametrize("seed", range(30))
def test_ac2_total_decides_not_which_factor(seed: int) -> None:
    rnd = random.Random(seed)
    sc = {f: rnd.randint(0, 2) for f in FACTORS}
    assert compute_rung(sc) == spec_rung(sum(sc.values()), sc["risk"])


@pytest.mark.parametrize("cap", LADDER)
@pytest.mark.parametrize(
    "total,risk", [(0, 0), (4, 0), (6, 0), (8, 0), (11, 2), (2, 2), (5, 2)]
)
def test_ac2_max_rung_caps(cap: str, total: int, risk: int) -> None:
    sc = scores_for(total, risk)
    want = min(spec_rung(total, risk), cap, key=LADDER.index)
    assert compute_rung(sc, cap) == want
    g = validate_graph(graph(task("A", scores=sc)), cap)
    assert g.tasks["A"].rung == want


def test_ac2_cap_below_risk_floor_wins() -> None:
    assert compute_rung(scores_for(3, 2), "R1") == "R1"


def test_ac2_unknown_max_rung_rejected() -> None:
    with pytest.raises((ValueError, TaskGraphError)):
        compute_rung(scores_for(3, 0), "R9")


def test_ac2_score_out_of_range_rejected() -> None:
    bad = task("A")
    bad["rubric"]["risk"]["score"] = 3
    with pytest.raises(TaskGraphError):
        validate_graph(graph(bad))


# ---------------- AC3 ----------------
def test_ac3_wrong_rung_replaced_and_reported() -> None:
    sc = scores_for(3, 0)  # R1
    g = validate_graph(graph(task("A", scores=sc, rung="R4")))
    assert g.tasks["A"].rung == "R1"
    assert len(g.replacements) == 1
    r = g.replacements[0]
    assert "A" in r.message and "R4" in r.message and "R1" in r.message


def test_ac3_wrong_total_replaced_and_reported() -> None:
    sc = scores_for(3, 0)
    g = validate_graph(graph(task("A", scores=sc, rung="R1", total=9)))
    assert g.tasks["A"].total == 3
    assert len(g.replacements) == 1
    assert "9" in g.replacements[0].message and "3" in g.replacements[0].message


def test_ac3_both_wrong_two_reports_and_matching_not_reported() -> None:
    sc = scores_for(3, 0)
    g = validate_graph(
        graph(
            task("A", scores=sc, rung="R4", total=11),
            task("B", scores=sc, rung="R1", total=3),
        )
    )
    assert len(g.replacements) == 2
    assert all(r.task_id == "A" for r in g.replacements)


def test_ac3_risk_floor_disagreement_replaced() -> None:
    g = validate_graph(graph(task("A", scores=scores_for(3, 2), rung="R1")))
    assert g.tasks["A"].rung == "R3" and g.replacements


def test_ac3_cap_disagreement_replaced() -> None:
    data = graph(task("A", scores=scores_for(11, 2), rung="R4"))
    g = validate_graph(data, "R2")
    assert g.tasks["A"].rung == "R2" and g.replacements


def test_ac3_input_not_mutated() -> None:
    data = graph(task("A", scores=scores_for(3, 0), rung="R4"))
    before = copy.deepcopy(data)
    validate_graph(data)
    assert data == before


# ---------------- AC4 ----------------
def _chain() -> Any:
    return validate_graph(
        graph(
            task("T1"),
            task("T2"),
            task("T3", ["T1"]),
            task("T4", ["T1", "T2"]),
            task("T5", ["T3", "T4"]),
        )
    )


def test_ac4_nothing_approved_only_roots() -> None:
    assert ready(_chain(), {}) == ["T1", "T2"]


def test_ac4_all_deps_must_be_approved() -> None:
    g = _chain()
    assert ready(g, {"T1": "approved"}) == ["T1", "T2", "T3"]
    assert ready(g, {"T1": "approved", "T2": "approved"}) == ["T1", "T2", "T3", "T4"]


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "running",
        "planned",
        "merged",
        "failed",
        "rejected",
        "Approved",
        "",
        "approved ",
    ],
)
def test_ac4_only_exact_approved_counts(state: str) -> None:
    assert "T3" not in ready(_chain(), {"T1": state})


def test_ac4_missing_state_is_not_approved() -> None:
    assert "T3" not in ready(_chain(), {"T2": "approved"})


def test_ac4_extra_unknown_state_keys_ignored() -> None:
    assert ready(_chain(), {"ZZZ": "approved"}) == ["T1", "T2"]


def test_ac4_all_approved_everything_ready_incl_approved_tasks() -> None:
    # Literal reading of AC4: "exactly the tasks whose dependencies are all approved".
    g = _chain()
    allapp = dict.fromkeys(g.tasks, "approved")
    assert ready(g, allapp) == ["T1", "T2", "T3", "T4", "T5"]


def test_ac4_empty_graph() -> None:
    assert ready(validate_graph(graph()), {}) == []


# ---------------- AC5 ----------------
def _valid_order(g: Any, order: list[str]) -> bool:
    pos = {t: i for i, t in enumerate(order)}
    return len(order) == len(g.tasks) == len(pos) and all(
        pos[d] < pos[t.id] for t in g.tasks.values() for d in t.depends_on
    )


def test_ac5_ties_by_id() -> None:
    g = validate_graph(graph(task("C"), task("A"), task("B")))
    assert merge_order(g) == ["A", "B", "C"]


def test_ac5_dependency_beats_id() -> None:
    g = validate_graph(graph(task("A", ["Z"]), task("Z"), task("B")))
    # Smallest-ready-first: B and Z ready -> B, then Z, then A.
    assert merge_order(g) == ["B", "Z", "A"]


def test_ac5_deterministic_regardless_of_input_order() -> None:
    base = [
        task("T01"),
        task("T02", ["T01"]),
        task("T03", ["T01"]),
        task("T04", ["T02", "T03"]),
        task("T05"),
        task("T06", ["T05", "T04"]),
        task("T07", ["T05"]),
    ]
    g0 = validate_graph(graph(*base))
    expected = merge_order(g0)
    assert _valid_order(g0, expected)
    assert expected == ["T01", "T02", "T03", "T04", "T05", "T06", "T07"]
    for seed in range(25):
        shuffled = base[:]
        random.Random(seed).shuffle(shuffled)
        assert merge_order(validate_graph(graph(*shuffled))) == expected


def test_ac5_random_dags_valid_and_stable() -> None:
    for seed in range(40):
        rnd = random.Random(seed)
        n = rnd.randint(1, 15)
        ids = [f"T{i:02d}" for i in range(n)]
        perm = ids[:]
        rnd.shuffle(perm)  # hidden topological ranking unrelated to ids
        rank = {t: i for i, t in enumerate(perm)}
        tasks = []
        for t in ids:
            cands = [u for u in ids if rank[u] < rank[t]]
            deps = rnd.sample(cands, k=min(len(cands), rnd.randint(0, 3)))
            tasks.append(task(t, deps))
        g = validate_graph(graph(*tasks))
        o = merge_order(g)
        assert _valid_order(g, o)
        rnd.shuffle(tasks)
        assert merge_order(validate_graph(graph(*tasks))) == o


def test_ac5_duplicate_dependency_entries() -> None:
    g = validate_graph(graph(task("A"), task("B", ["A", "A"])))
    assert merge_order(g) == ["A", "B"]


def test_ac5_empty() -> None:
    assert merge_order(validate_graph(graph())) == []


# ---------------- AC6 ----------------
def test_ac6_real_tasks_json_validates() -> None:
    g = load_graph(REAL)
    assert len(g.tasks) >= 40
    assert g.tasks["T11"].rung == "R1"
    assert merge_order(g)[0] == "T01"
    assert _valid_order(g, merge_order(g))


def test_ac6_real_tasks_json_rungs_match_my_table() -> None:
    data = json.loads(REAL.read_text(encoding="utf-8"))
    g = load_graph(REAL)
    for t in data["tasks"]:
        sc = {f: t["rubric"][f]["score"] for f in FACTORS}
        assert g.tasks[t["id"]].total == sum(sc.values())
        assert g.tasks[t["id"]].rung == spec_rung(sum(sc.values()), sc["risk"]), t["id"]


def test_ac6_command_exits_zero() -> None:
    p = subprocess.run(
        [sys.executable, "-m", "ultron.engine.task_graph", str(REAL)],
        capture_output=True,
        check=False,
        text=True,
        cwd=ROOT,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert "ok" in p.stdout


def test_ac6_command_nonzero_on_bad_file(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    f.write_text(json.dumps(graph(task("A", ["B"]))), encoding="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "ultron.engine.task_graph", str(f)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert p.returncode != 0
    assert "B" in p.stdout + p.stderr
    p = subprocess.run(
        [sys.executable, "-m", "ultron.engine.task_graph", str(tmp_path / "nope.json")],
        capture_output=True,
        check=False,
        text=True,
    )
    assert p.returncode != 0


# ---------------- AC7 ----------------
@pytest.mark.parametrize("m", list(tg.VERIFY_METHODS))
def test_ac7_known_methods_accepted(m: str) -> None:
    validate_graph(graph(task("A", methods=(m,))))


def test_ac7_methods_in_real_file_are_the_accepted_set() -> None:
    data = json.loads(REAL.read_text(encoding="utf-8"))
    used = {
        v["method"]
        for t in data["tasks"]
        for c in t["acceptance_criteria"]
        for v in c["verify_with"]
    }
    assert used <= set(tg.VERIFY_METHODS)


def test_ac7_unknown_method_names_task_and_criterion() -> None:
    t = task("T07", methods=("pytest", "telepathy"))
    t["acceptance_criteria"][0]["id"] = "AC3"
    msg = fails(graph(t))
    assert "T07" in msg and "AC3" in msg and "telepathy" in msg


def test_ac7_unknown_method_in_second_task_second_criterion() -> None:
    a = task("T01")
    b = task("T02")
    b["acceptance_criteria"].append(
        {"id": "AC2", "text": "y", "verify_with": [{"method": "vibes", "detail": ""}]}
    )
    msg = fails(graph(a, b))
    assert "T02" in msg and "AC2" in msg and "T01" not in msg


def test_ac7_empty_verify_with_rejected() -> None:
    t = task("A")
    t["acceptance_criteria"][0]["verify_with"] = []
    with pytest.raises(TaskGraphError):
        validate_graph(graph(t))


def test_ac7_missing_verify_with_and_missing_mcp_rejected() -> None:
    t = task("A")
    del t["acceptance_criteria"][0]["verify_with"]
    msg = fails(graph(t))
    assert "A" in msg and "AC1" in msg
    t2 = task("B")
    del t2["mcp"]
    assert "B" in fails(graph(t2))


def test_ac7_mcp_field_accepted_with_servers() -> None:
    t = task("A")
    t["mcp"] = {"servers": [{"name": "x", "why": "y"}], "reason": "r"}
    validate_graph(graph(t))


def test_ac7_needs_owner_and_added_accepted() -> None:
    t = task("A", methods=("ci", "owner"))
    t["acceptance_criteria"][0]["needs_owner"] = True
    t["acceptance_criteria"][0]["added"] = "2026-11-10"
    validate_graph(graph(t))

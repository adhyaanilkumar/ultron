"""Acceptance tests for T13 (usage accounting, cost estimates, spending caps), by the testing agent.

Reference prices (USD per million tokens, engine-supplied list prices cached 2026-11-03):
haiku 0.10/0.50 up to 100K-token prompts and 0.50/2.50 beyond; sonnet 2/10, cache read 0.10; opus 4/20, cache
read 0.20; fable 8/40, cache read 0.20. Cache write 1.25x input is an assumption. All expected values below are
computed by hand from these, not copied from the code.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ultron.config import defaults as d
from ultron.config.models import Caps
from ultron.engine.costs import (
    DEFAULT_PRICES,
    CapAction,
    CapLimits,
    ModelPrice,
    Scope,
    UsageLedger,
    UsageRecord,
    caps_from_config,
    check_cap,
    ledger_from_events,
    usage_cost,
)
from ultron.store import events as ev
from ultron.store.eventlog import EventLog

M = 1_000_000
CAPS = CapLimits(Decimal(5), Decimal(3))
EXACT = {
    "haiku-5.5": "claude-haiku-5-5",
    "sonnet-5.5": "claude-sonnet-5-5",
    "opus-5.5": "claude-opus-5-5",
    "fable-5.2": "claude-fable-5-2",
}


def cost(model: str, **tokens: int) -> Decimal:
    return usage_cost(UsageRecord(model, tokens), DEFAULT_PRICES).cost


# ---- AC1: per-model hand-computed cost -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # 1M in + 1M out: haiku (upper tier) 0.50+2.50=3.00; sonnet 2+10=12; opus 4+20=24; fable 8+40=48
        ("haiku-5.5", Decimal("3.00")),
        ("sonnet-5.5", Decimal(12)),
        ("opus-5.5", Decimal(24)),
        ("fable-5.2", Decimal(48)),
    ],
)
def test_ac1_input_plus_output_per_model(model: str, expected: Decimal) -> None:
    assert cost(model, input=M, output=M) == expected
    assert cost(EXACT[model], input=M, output=M) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("sonnet-5.5", Decimal("0.10")),
        ("opus-5.5", Decimal("0.20")),
        ("fable-5.2", Decimal("0.20")),
    ],
)
def test_ac1_cache_read_per_model(model: str, expected: Decimal) -> None:
    # 1M cache-read tokens at the listed cache-read price
    assert cost(model, cache_read=M) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("sonnet-5.5", Decimal("2.5")),
        ("opus-5.5", Decimal(5)),
        ("fable-5.2", Decimal(10)),
    ],
)
def test_ac1_cache_write_assumed_1_25x_input(model: str, expected: Decimal) -> None:
    # assumption: 1.25 x input: sonnet 2.5, opus 5, fable 10
    assert cost(model, cache_write=M) == expected


def test_ac1_mixed_record_hand_computed() -> None:
    # opus: 123,456 in * 4 = 493,824; 7,890 out * 20 = 157,800; 50,000 read * 0.2 = 10,000; 1,000 write * 5 = 5,000
    # total 666,624 micro-dollars -> 0.666624
    got = cost(
        "opus-5.5", input=123_456, output=7_890, cache_read=50_000, cache_write=1_000
    )
    assert got == Decimal("0.666624")


def test_ac1_zero_tokens_costs_zero_and_is_unflagged_for_known_model() -> None:
    r = usage_cost(UsageRecord("sonnet-5.5", {}), DEFAULT_PRICES)
    assert r.cost == 0 and r.price_known and not r.flagged


def test_ac1_cost_is_decimal_not_float() -> None:
    assert isinstance(cost("sonnet-5.5", input=1), Decimal)
    assert cost("sonnet-5.5", input=1) == Decimal("0.000002")  # 1 * 2 / 1e6


def test_ac1_price_table_is_versioned_dated_and_editable() -> None:
    assert DEFAULT_PRICES.version
    assert DEFAULT_PRICES.as_of == "2026-11-03"
    edited = DEFAULT_PRICES.replace("claude-sonnet-5-5", ModelPrice(*(Decimal(1),) * 4))
    r = usage_cost(UsageRecord("sonnet-5.5", {"input": M}), edited)
    assert r.cost == Decimal(1)
    # the original table is untouched
    assert cost("sonnet-5.5", input=M) == Decimal(2)


def test_ac1_haiku_is_flagged_upper_bound_others_are_not() -> None:
    h = usage_cost(UsageRecord("haiku-5.5", {"input": 10}), DEFAULT_PRICES)
    assert h.upper_bound and h.flagged and h.price_known
    for m in ("sonnet-5.5", "opus-5.5", "fable-5.2"):
        r = usage_cost(UsageRecord(m, {"input": 10}), DEFAULT_PRICES)
        assert not r.upper_bound and not r.flagged


def test_ac1_haiku_never_below_lower_tier() -> None:
    # lower tier 0.10/0.50 would give 0.60 for 1M+1M; the estimate must be an upper bound, never under
    assert cost("haiku-5.5", input=M, output=M) >= Decimal("0.60")


def test_ac1_aliases_in_config_resolve_to_exact_model_ids() -> None:
    models = {m for m, _ in d.DEFAULT_AGENTS.values()} | {
        m for m, _ in d.DEFAULT_LADDER.values()
    }
    assert models  # config uses short ids
    for m in models:
        assert DEFAULT_PRICES.resolve(m) == EXACT[m]
        r = usage_cost(UsageRecord(m, {"input": M, "output": M}), DEFAULT_PRICES)
        assert r.price_known
        assert r.cost == cost(EXACT[m], input=M, output=M)


def test_ac1_exact_decimal_no_float_drift_over_many_calls() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    # 10,000 calls of 1 sonnet input token: 10,000 * 0.000002 = 0.02 exactly (a float sum would drift)
    for _ in range(10_000):
        ledger.add("req-1", UsageRecord("sonnet-5.5", {"input": 1}))
    assert ledger.total("req-1") == Decimal("0.02")
    # 3 calls of 0.1 (opus 500,000 cache_read * 0.2 / 1e6) sum to exactly 0.3
    other = UsageLedger(DEFAULT_PRICES)
    for _ in range(3):
        other.add("r", UsageRecord("opus-5.5", {"cache_read": 500_000}))
    assert other.total("r") == Decimal("0.3")


# ---- AC2: check_cap --------------------------------------------------------------------------------------------


def _ledger_with(sonnet_output_tokens: int, req: str = "req-1") -> UsageLedger:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add(req, UsageRecord("sonnet-5.5", {"output": sonnet_output_tokens}))
    return ledger


def test_ac2_cap_boundary_under_at_over() -> None:
    scope = Scope.requirement("req-1")
    # sonnet output is 10 USD/M tokens; the cap is 5 USD
    under = check_cap(_ledger_with(499_999), scope, Decimal(0), CAPS)  # 4.99999 < 5
    assert under.allowed and under.action is CapAction.ALLOW
    at = check_cap(_ledger_with(500_000), scope, Decimal(0), CAPS)  # exactly 5.00
    assert not at.allowed and at.action is CapAction.ESCALATE_UNFINISHED
    over = check_cap(_ledger_with(500_001), scope, Decimal(0), CAPS)  # 5.00001
    assert not over.allowed and over.action is CapAction.ESCALATE_UNFINISHED


def test_ac2_decision_reports_total_cap_and_scope() -> None:
    dec = check_cap(
        _ledger_with(100_000), Scope.requirement("req-1"), Decimal("0.5"), CAPS
    )
    assert dec.total == Decimal("1.00") and dec.cap == Decimal(
        5
    )  # 100,000 * 10 / 1e6 = 1.00
    assert dec.scope == Scope.requirement("req-1")
    assert dec.projected_total == Decimal("1.50")


def test_ac2_empty_requirement_is_allowed() -> None:
    dec = check_cap(
        UsageLedger(DEFAULT_PRICES), Scope.requirement("new"), Decimal(0), CAPS
    )
    assert dec.allowed and dec.total == 0


def test_ac2_other_requirement_is_not_affected_by_one_at_cap() -> None:
    ledger = _ledger_with(500_000, "req-1")
    assert not check_cap(ledger, Scope.requirement("req-1"), Decimal(0), CAPS).allowed
    assert check_cap(ledger, Scope.requirement("req-2"), Decimal(0), CAPS).allowed


def test_ac2_requirement_cap_is_independent_of_run_cap() -> None:
    # per-requirement cap 5, per-run cap 3: a requirement with total 4 is under its own cap, over the run cap
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add(
        "req-1", UsageRecord("sonnet-5.5", {"output": 400_000}), run="run-1"
    )  # 4.00
    assert check_cap(ledger, Scope.requirement("req-1"), Decimal(0), CAPS).allowed
    run = check_cap(ledger, Scope.evaluation_run("run-1"), Decimal(0), CAPS)
    assert not run.allowed and run.action is CapAction.STOP_RUN


def test_ac2_unknown_scope_kind_raises() -> None:
    with pytest.raises(ValueError):
        check_cap(UsageLedger(DEFAULT_PRICES), Scope("galaxy", "x"), Decimal(0), CAPS)


def test_ac2_caps_from_config_defaults_and_exact_decimals() -> None:
    c = caps_from_config(Caps())
    assert c.per_requirement == Decimal("5.0")
    assert c.per_evaluation_run == Decimal("50.0")
    odd = caps_from_config(Caps(per_requirement_usd=0.1, per_evaluation_run_usd=0.3))
    assert odd.per_requirement == Decimal("0.1")
    assert odd.per_evaluation_run == Decimal("0.3")


def test_ac2_tiny_config_cap_boundary_is_exact() -> None:
    # cap 0.1 USD: opus 500,000 cache_read = 0.1 exactly -> refused at exactly the cap
    caps = caps_from_config(Caps(per_requirement_usd=0.1))
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add("r", UsageRecord("opus-5.5", {"cache_read": 499_999}))  # 0.0999998
    assert check_cap(ledger, Scope.requirement("r"), Decimal(0), caps).allowed
    ledger.add(
        "r", UsageRecord("opus-5.5", {"cache_read": 1})
    )  # 0.0999998 + 0.0000002 = 0.1 exactly
    assert not check_cap(ledger, Scope.requirement("r"), Decimal(0), caps).allowed


# ---- AC3: rebuild from events == running totals ----------------------------------------------------------------


def _call(
    req: str, model: str, tokens: dict[str, int], task: str | None = "T01"
) -> ev.NewEvent:
    return ev.agent_call(
        req,
        role="developer",
        model=model,
        effort="medium",
        duration_ms=10,
        tokens=tokens,
        cost_usd=0.0,
        transcript="t.jsonl",
        prompt_sha256="a" * 64,
        result_sha256="b" * 64,
        task=task,
        attempt=1,
    )


def test_ac3_rebuilt_totals_equal_running_totals(tmp_path: Path) -> None:
    calls = [
        (
            "req-1",
            "sonnet-5.5",
            {"input": 1234, "output": 567, "cache_read": 89_000, "cache_write": 321},
        ),
        ("req-1", "opus-5.5", {"input": 10, "output": 20}),
        ("req-2", "haiku-5.5", {"input": 999_999, "output": 1}),
        ("req-2", "claude-fable-5-2", {"output": 3}),
        ("req-1", "mystery-model", {"input": 7, "output": 7}),
        ("req-2", "sonnet-5.5", {"thinking": 55}),  # unknown token kind
    ]
    running = UsageLedger(DEFAULT_PRICES)
    logs: dict[str, EventLog] = {}
    for i, (req, model, tokens) in enumerate(calls):
        running.add(req, UsageRecord(model, tokens), run="run-1")
        log = logs.setdefault(req, EventLog(tmp_path / req / "events.jsonl"))
        log.append(_call(req, model, tokens, task=f"T{i}"))
        if i == 1:  # a non-agent event must be ignored
            log.append(ev.config_snapshot(req, max_attempts=3, gates={"plan": "human"}))
    rebuilt_events = [e for log in logs.values() for e in log.read().events]
    runs = {"req-1": "run-1", "req-2": "run-1"}
    rebuilt = ledger_from_events(rebuilt_events, DEFAULT_PRICES, runs)
    assert rebuilt.requirement_totals() == running.requirement_totals()
    assert rebuilt.total("req-1") == running.total("req-1")
    assert rebuilt.run_total("run-1") == running.run_total("run-1")
    assert (
        rebuilt.flagged_models == running.flagged_models == frozenset({"mystery-model"})
    )


def test_ac3_rebuild_is_order_independent_and_empty_is_zero(tmp_path: Path) -> None:
    assert ledger_from_events([], DEFAULT_PRICES).requirement_totals() == {}
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        [
            _call("r", "sonnet-5.5", {"input": i * 1000}, task=f"T{i}")
            for i in range(1, 6)
        ]
    )
    evs = log.read().events
    fwd = ledger_from_events(evs, DEFAULT_PRICES).total("r")
    rev = ledger_from_events(list(reversed(evs)), DEFAULT_PRICES).total("r")
    assert fwd == rev == Decimal("0.03")  # 15,000 input tokens * 2 / 1e6


def test_ac3_rebuild_does_not_trust_the_logged_float_cost(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        ev.agent_call(
            "r",
            role="developer",
            model="sonnet-5.5",
            effort="medium",
            duration_ms=1,
            tokens={"output": 100_000},
            cost_usd=999.0,
            transcript="t",
            prompt_sha256="a",
            result_sha256="b",
        )
    )
    # 100,000 * 10 / 1e6 = 1.00, whatever the float in the event says
    assert ledger_from_events(log.read().events, DEFAULT_PRICES).total("r") == Decimal(
        "1.00"
    )


# ---- AC4: evaluation-run cap spans requirements ----------------------------------------------------------------


def test_ac4_run_cap_counts_all_requirements_in_the_run() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add("a", UsageRecord("sonnet-5.5", {"output": 150_000}), run="run-1")  # 1.50
    ledger.add("b", UsageRecord("sonnet-5.5", {"output": 100_000}), run="run-1")  # 1.00
    ledger.add(
        "c", UsageRecord("sonnet-5.5", {"output": 900_000}), run="run-2"
    )  # 9.00 (other run)
    scope = Scope.evaluation_run("run-1")
    dec = check_cap(ledger, scope, Decimal(0), CAPS)  # 2.50 < 3
    assert dec.allowed and dec.total == Decimal("2.50")
    ledger.add(
        "b", UsageRecord("sonnet-5.5", {"output": 49_999}), run="run-1"
    )  # 2.99999
    assert check_cap(ledger, scope, Decimal(0), CAPS).allowed
    ledger.add(
        "b", UsageRecord("sonnet-5.5", {"output": 1}), run="run-1"
    )  # exactly 3.00
    at = check_cap(ledger, scope, Decimal(0), CAPS)
    assert (
        not at.allowed
        and at.action is CapAction.STOP_RUN
        and at.total == Decimal("3.00")
    )
    # run-2 is its own total and run-1 usage does not leak into it
    assert ledger.run_total("run-2") == Decimal("9.00")
    assert ledger.run_total("run-1") == Decimal("3.00")
    # a run with no usage is allowed
    assert check_cap(ledger, Scope.evaluation_run("run-3"), Decimal(0), CAPS).allowed


def test_ac4_requirement_in_run_still_checked_against_requirement_cap() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add(
        "a", UsageRecord("sonnet-5.5", {"output": 100_000}), run="run-1"
    )  # 1.00 < 5
    dec = check_cap(ledger, Scope.requirement("a"), Decimal(0), CAPS)
    assert dec.allowed and dec.cap == Decimal(5)


def test_ac4_run_totals_rebuilt_from_events(tmp_path: Path) -> None:
    la, lb = EventLog(tmp_path / "a.jsonl"), EventLog(tmp_path / "b.jsonl")
    la.append(_call("a", "sonnet-5.5", {"output": 200_000}))  # 2.00
    lb.append(_call("b", "sonnet-5.5", {"output": 100_000}))  # 1.00
    evs = la.read().events + lb.read().events
    ledger = ledger_from_events(evs, DEFAULT_PRICES, {"a": "r1", "b": "r1"})
    dec = check_cap(ledger, Scope.evaluation_run("r1"), Decimal(0), CAPS)
    assert dec.total == Decimal("3.00") and dec.action is CapAction.STOP_RUN


# ---- AC5: unknown model / token kind never free ----------------------------------------------------------------


def test_ac5_unknown_model_uses_most_expensive_price_and_is_flagged() -> None:
    r = usage_cost(
        UsageRecord("brand-new-model", {"input": M, "output": M}), DEFAULT_PRICES
    )
    assert not r.price_known and r.flagged
    assert r.cost == Decimal(48)  # most expensive known (fable): 8 in + 40 out
    for m in EXACT.values():
        assert r.cost >= cost(m, input=M, output=M)


def test_ac5_unknown_model_never_zero_for_any_token_kind() -> None:
    for kind in ("input", "output", "cache_read", "cache_write"):
        r = usage_cost(UsageRecord("who-knows", {kind: 1000}), DEFAULT_PRICES)
        assert r.cost > 0, kind
        assert r.cost >= cost("fable-5.2", **{kind: 1000})


def test_ac5_unknown_model_recorded_in_ledger_and_counts_toward_caps() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add(
        "req-1", UsageRecord("who-knows", {"output": 125_000})
    )  # 125,000 * 40 / 1e6 = 5.00
    assert ledger.flagged_models == frozenset({"who-knows"})
    assert ledger.total("req-1") == Decimal(5)
    assert not check_cap(ledger, Scope.requirement("req-1"), Decimal(0), CAPS).allowed


def test_ac5_known_model_not_in_flagged_models() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add("r", UsageRecord("sonnet-5.5", {"input": 5}))
    assert ledger.flagged_models == frozenset()


def test_ac5_unknown_token_kind_is_charged_and_flagged_not_free() -> None:
    r = usage_cost(UsageRecord("sonnet-5.5", {"reasoning": M}), DEFAULT_PRICES)
    assert r.price_known
    assert r.unknown_token_kinds == ("reasoning",)
    assert r.flagged
    assert r.cost >= Decimal(10)  # at least the model's output rate, never 0
    # known kinds in the same record are still priced: 1M in = 2, plus the unknown kind at >= 10
    mixed = usage_cost(
        UsageRecord("sonnet-5.5", {"input": M, "reasoning": M}), DEFAULT_PRICES
    )
    assert mixed.cost >= Decimal(12)


def test_ac5_empty_model_string_is_not_free() -> None:
    r = usage_cost(UsageRecord("", {"input": M}), DEFAULT_PRICES)
    assert not r.price_known and r.cost > 0

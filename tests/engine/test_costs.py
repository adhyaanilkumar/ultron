"""T13: cost estimates, totals and spending caps (FR17, FR18)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ultron.config.models import Caps
from ultron.engine.costs import (
    DEFAULT_PRICES,
    CapAction,
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

TOKENS = {"input": 1000, "output": 500, "cache_read": 2000, "cache_write": 400}


def rec(model: str = "sonnet-5.5", **tokens: int) -> UsageRecord:
    return UsageRecord(model=model, tokens=tokens or dict(TOKENS))


# AC1: hand-computed per model (USD per million tokens: input/output/cache read/cache write)
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # haiku 1/5/0.10/1.25: 1000 + 2500 + 200 + 500 = 4200 micro-dollars
        ("haiku-5.5", Decimal("0.0042")),
        # sonnet 3/15/0.30/3.75: 3000 + 7500 + 600 + 1500 = 12600
        ("sonnet-5.5", Decimal("0.0126")),
        # opus 5/25/0.50/6.25: 5000 + 12500 + 1000 + 2500 = 21000
        ("opus-5.5", Decimal("0.021")),
    ],
)
def test_cost_matches_hand_computation(model: str, expected: Decimal) -> None:
    result = usage_cost(rec(model), DEFAULT_PRICES)
    assert result.cost == expected
    assert result.price_known is True
    assert isinstance(result.cost, Decimal)


def test_table_is_dated_and_covers_the_ladder() -> None:
    assert DEFAULT_PRICES.version
    assert DEFAULT_PRICES.as_of == "2026-11-11"
    assert {"haiku-5.5", "sonnet-5.5", "opus-5.5"} <= set(DEFAULT_PRICES.prices)


def test_table_is_editable() -> None:
    table = DEFAULT_PRICES.replace(
        "sonnet-5.5", ModelPrice(Decimal(1), Decimal(1), Decimal(1), Decimal(1))
    )
    assert usage_cost(rec(), table).cost == Decimal("0.0039")
    assert usage_cost(rec(), DEFAULT_PRICES).cost == Decimal("0.0126")


# AC5
def test_unknown_model_uses_most_expensive_price_and_is_flagged() -> None:
    result = usage_cost(rec("mystery-9"), DEFAULT_PRICES)
    assert result.price_known is False
    assert result.cost == Decimal("0.021")
    assert result.cost > 0


def test_unknown_token_kind_is_costed_at_input_price_and_flagged_never_zero() -> None:
    result = usage_cost(rec("opus-5.5", weird=1_000_000), DEFAULT_PRICES)
    assert result.cost == Decimal(25)  # most expensive kind of that model (output)
    assert result.unknown_token_kinds == ("weird",)


# AC2
def test_check_cap_refuses_once_total_reached_and_escalates_requirement() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    scope = Scope.requirement("r1")
    caps = caps_from_config(Caps(per_requirement_usd=0.02))
    ok = check_cap(ledger, scope, Decimal("0.001"), caps)
    assert ok.allowed and ok.action is CapAction.ALLOW
    ledger.add("r1", rec("opus-5.5"))  # 0.021 >= 0.02
    decision = check_cap(ledger, scope, Decimal("0.001"), caps)
    assert not decision.allowed
    assert decision.action is CapAction.ESCALATE_UNFINISHED
    assert decision.action.value == "escalate_unfinished"
    assert decision.total == Decimal("0.021")


def test_check_cap_refuses_exactly_at_cap_and_allows_just_below() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    caps = caps_from_config(Caps(per_requirement_usd=0.0126))
    scope = Scope.requirement("r1")
    ledger.add("r1", rec("sonnet-5.5"))
    assert not check_cap(ledger, scope, Decimal(0), caps).allowed
    ledger2 = UsageLedger(DEFAULT_PRICES)
    ledger2.add("r1", rec("haiku-5.5"))
    assert check_cap(ledger2, scope, Decimal(0), caps).allowed


# AC3
def test_rebuilt_totals_equal_running_totals() -> None:
    running = UsageLedger(DEFAULT_PRICES)
    events = []
    plan = [
        ("r1", "opus-5.5"),
        ("r1", "haiku-5.5"),
        ("r2", "sonnet-5.5"),
        ("r2", "nova-1"),
    ]
    for i, (req, model) in enumerate(plan, start=1):
        new = ev.agent_call(
            req,
            role="developer",
            model=model,
            effort="medium",
            duration_ms=10,
            tokens=TOKENS,
            cost_usd=0.0,
            transcript="t.jsonl",
            prompt_sha256="a",
            result_sha256="b",
            task="T01",
            attempt=1,
        )
        running.add(req, UsageRecord(model=model, tokens=TOKENS))
        events.append(
            ev.Event(
                schema=1,
                seq=i,
                time="2026-11-11T00:00:00Z",
                actor=new.actor,
                type=new.type,
                requirement=req,
                task=new.task,
                attempt=new.attempt,
                data=new.data,
                ultron_version="0",
            )
        )
    # non-agent events are ignored
    gate = ev.gate_decision(
        "r1", actor="auto", gate="plan", decision="approve", channel="auto"
    )
    events.append(
        ev.Event(
            schema=1,
            seq=99,
            time="t",
            actor="auto",
            type=gate.type,
            requirement="r1",
            task=None,
            attempt=None,
            data=gate.data,
            ultron_version="0",
        )
    )
    rebuilt = ledger_from_events(events, DEFAULT_PRICES)
    assert rebuilt.requirement_totals() == running.requirement_totals()
    assert rebuilt.total("r1") == Decimal("0.0252")
    assert rebuilt.flagged_models == running.flagged_models == frozenset({"nova-1"})


# AC4
def test_evaluation_run_cap_counts_all_its_requirements() -> None:
    ledger = UsageLedger(DEFAULT_PRICES)
    ledger.add("e1", rec("opus-5.5"), run="run-a")
    ledger.add("e2", rec("opus-5.5"), run="run-a")
    ledger.add("other", rec("opus-5.5"), run="run-b")
    assert ledger.run_total("run-a") == Decimal("0.042")
    caps = caps_from_config(Caps(per_requirement_usd=5, per_evaluation_run_usd=0.04))
    stopped = check_cap(ledger, Scope.evaluation_run("run-a"), Decimal(0), caps)
    assert not stopped.allowed
    assert stopped.action is CapAction.STOP_RUN
    # each requirement alone is under its own cap
    assert check_cap(ledger, Scope.requirement("e1"), Decimal(0), caps).allowed
    assert check_cap(ledger, Scope.evaluation_run("run-b"), Decimal(0), caps).allowed


def test_caps_from_config_are_exact_decimals() -> None:
    caps = caps_from_config(Caps())
    assert caps.per_requirement == Decimal("5.0")
    assert caps.per_evaluation_run == Decimal("50.0")

"""Usage accounting, API-equivalent cost estimates and spending caps (spec FR17, FR18).

Money is `Decimal`, never float. Costs are estimates of what the same tokens would cost on the API; ultron's own
calls run on the owner's plan and cost no money. Totals are kept per requirement and per evaluation run, and can
always be rebuilt from the `agent_call` events (`ledger_from_events`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum

from ultron.config.models import Caps
from ultron.store.events import AGENT_CALL, Event

__all__ = [
    "DEFAULT_PRICES",
    "CapAction",
    "CapDecision",
    "CapLimits",
    "CostResult",
    "ModelPrice",
    "PriceTable",
    "Scope",
    "UsageLedger",
    "UsageRecord",
    "caps_from_config",
    "check_cap",
    "ledger_from_events",
    "usage_cost",
]

TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")
_PER = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, for each token kind."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def for_kind(self, kind: str) -> Decimal | None:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }.get(kind)

    def highest(self) -> Decimal:
        return max(self.input, self.output, self.cache_read, self.cache_write)


@dataclass(frozen=True)
class PriceTable:
    """A dated, versioned per-model price table. Edit it by building a new one with `replace`."""

    version: str
    as_of: str
    source: str
    prices: Mapping[str, ModelPrice]
    aliases: Mapping[str, str] = field(default_factory=dict)
    upper_bound_models: frozenset[str] = frozenset()

    def resolve(self, model: str) -> str | None:
        """The table key for a model id or short alias, or None if unknown."""
        key = self.aliases.get(model, model)
        return key if key in self.prices else None

    def replace(self, model: str, price: ModelPrice) -> PriceTable:
        return replace(self, prices={**self.prices, model: price})

    def most_expensive(self) -> ModelPrice:
        """The highest known price of each token kind, taken across all models."""
        highest = [
            max(p.for_kind(kind) or Decimal(0) for p in self.prices.values())
            for kind in TOKEN_KINDS
        ]
        return ModelPrice(highest[0], highest[1], highest[2], highest[3])


def _price(i: str, o: str, cache_read: str) -> ModelPrice:
    # Cache write is ASSUMED at 1.25x input (5-minute TTL); it is not in the list prices we were given.
    return ModelPrice(
        Decimal(i), Decimal(o), Decimal(cache_read), Decimal(i) * Decimal("1.25")
    )


DEFAULT_PRICES = PriceTable(
    version="2",
    as_of="2026-11-03",
    source="Anthropic list prices",
    prices={
        # Haiku has two tiers (0.10/0.50 up to 100K prompt tokens, 0.50/2.50 beyond). The event does not carry
        # the prompt size, so the higher tier is used: an upper bound (see `upper_bound_models`).
        # ASSUMED: haiku cache read 0.1x input (no list price given); cache write 1.25x input for all models.
        "claude-haiku-5-5": _price("0.50", "2.50", "0.05"),
        "claude-sonnet-5-5": _price("2.00", "10.00", "0.10"),
        "claude-opus-5-5": _price("4.00", "20.00", "0.20"),
        # R6, self-build only: included so Fable calls are never costed as unknown.
        "claude-fable-5-2": _price("8.00", "40.00", "0.20"),
    },
    aliases={
        "haiku": "claude-haiku-5-5",
        "sonnet": "claude-sonnet-5-5",
        "opus": "claude-opus-5-5",
        "fable": "claude-fable-5-2",
        "haiku-5.5": "claude-haiku-5-5",
        "sonnet-5.5": "claude-sonnet-5-5",
        "opus-5.5": "claude-opus-5-5",
        "fable-5.2": "claude-fable-5-2",
    },
    upper_bound_models=frozenset({"claude-haiku-5-5"}),
)


@dataclass(frozen=True)
class UsageRecord:
    """The tokens of one agent call, by kind (`input`, `output`, `cache_read`, `cache_write`)."""

    model: str
    tokens: Mapping[str, int]


@dataclass(frozen=True)
class CostResult:
    cost: Decimal
    price_known: bool
    unknown_token_kinds: tuple[str, ...] = ()
    upper_bound: bool = False

    @property
    def flagged(self) -> bool:
        return (
            not self.price_known or bool(self.unknown_token_kinds) or self.upper_bound
        )


def usage_cost(record: UsageRecord, prices: PriceTable) -> CostResult:
    """Estimated USD for one call. An unknown model or token kind is never free: it takes the highest price."""
    key = prices.resolve(record.model)
    known = key is not None
    price = prices.prices[key] if key is not None else prices.most_expensive()
    total = Decimal(0)
    unknown: list[str] = []
    for kind, count in record.tokens.items():
        rate = price.for_kind(kind)
        if rate is None:
            unknown.append(kind)
            rate = price.highest()
        total += rate * count / _PER
    return CostResult(
        total,
        known,
        tuple(sorted(unknown)),
        upper_bound=key in prices.upper_bound_models,
    )


class CapAction(Enum):
    ALLOW = "allow"
    ESCALATE_UNFINISHED = "escalate_unfinished"
    STOP_RUN = "stop_run"


@dataclass(frozen=True)
class Scope:
    kind: str  # "requirement" or "evaluation_run"
    id: str

    @staticmethod
    def requirement(id: str) -> Scope:
        return Scope("requirement", id)

    @staticmethod
    def evaluation_run(id: str) -> Scope:
        return Scope("evaluation_run", id)


@dataclass(frozen=True)
class CapLimits:
    per_requirement: Decimal
    per_evaluation_run: Decimal


def caps_from_config(caps: Caps) -> CapLimits:
    """Exact Decimal caps from the config's numbers (via their decimal text, so 0.1 stays 0.1)."""
    return CapLimits(
        Decimal(str(caps.per_requirement_usd)),
        Decimal(str(caps.per_evaluation_run_usd)),
    )


@dataclass
class UsageLedger:
    """Running totals per requirement and per evaluation run."""

    prices: PriceTable
    _by_req: dict[str, Decimal] = field(default_factory=dict)
    _req_run: dict[str, str] = field(default_factory=dict)
    _flagged: set[str] = field(default_factory=set)

    def add(
        self, requirement: str, record: UsageRecord, run: str | None = None
    ) -> CostResult:
        result = usage_cost(record, self.prices)
        self._by_req[requirement] = (
            self._by_req.get(requirement, Decimal(0)) + result.cost
        )
        if run is not None:
            self._req_run[requirement] = run
        if not result.price_known:
            self._flagged.add(record.model)
        return result

    def total(self, requirement: str) -> Decimal:
        return self._by_req.get(requirement, Decimal(0))

    def run_total(self, run: str) -> Decimal:
        return sum(
            (t for r, t in self._by_req.items() if self._req_run.get(r) == run),
            Decimal(0),
        )

    def requirement_totals(self) -> dict[str, Decimal]:
        return dict(self._by_req)

    @property
    def flagged_models(self) -> frozenset[str]:
        """Models costed at the most expensive price because they are not in the table."""
        return frozenset(self._flagged)


@dataclass(frozen=True)
class CapDecision:
    allowed: bool
    action: CapAction
    scope: Scope
    total: Decimal
    cap: Decimal
    projected_total: Decimal


def check_cap(
    ledger: UsageLedger, scope: Scope, estimate: Decimal, caps: CapLimits
) -> CapDecision:
    """Called by the engine before every agent call.

    A call is refused when the total has already reached the cap. `estimate` is only reported, as
    `projected_total`; it never refuses a call by itself (spec FR18).
    """
    if scope.kind == "requirement":
        total, cap = ledger.total(scope.id), caps.per_requirement
        hit = CapAction.ESCALATE_UNFINISHED
    elif scope.kind == "evaluation_run":
        total, cap = ledger.run_total(scope.id), caps.per_evaluation_run
        hit = CapAction.STOP_RUN
    else:
        raise ValueError(f"unknown cap scope {scope.kind!r}")
    allowed = total < cap
    return CapDecision(
        allowed,
        CapAction.ALLOW if allowed else hit,
        scope,
        total,
        cap,
        total + estimate,
    )


def ledger_from_events(
    events: Iterable[Event],
    prices: PriceTable,
    runs: Mapping[str, str] | None = None,
) -> UsageLedger:
    """Rebuild the totals from `agent_call` events. `runs` maps a requirement to its evaluation run."""
    ledger = UsageLedger(prices)
    for event in events:
        if event.type != AGENT_CALL:
            continue
        tokens = {str(k): int(v) for k, v in event.data.get("tokens", {}).items()}
        record = UsageRecord(model=str(event.data["model"]), tokens=tokens)
        ledger.add(event.requirement, record, (runs or {}).get(event.requirement))
    return ledger

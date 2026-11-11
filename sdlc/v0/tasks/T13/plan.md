# T13 plan: usage accounting, cost estimates and spending caps

## Files that change
- `src/ultron/engine/costs.py` (new): `PriceTable` (dated, versioned, per-million-token Decimal prices for input,
  output, cache read, cache write), `usage_cost`, `UsageLedger` (running totals per requirement and per run),
  `Scope`, `check_cap`, `CapDecision`, `ledger_from_events`.
- `tests/engine/test_costs.py` (new): all five criteria.
No edits to config, store or spec. Caps come from `config.Caps` (floats, converted to Decimal via `str`).

## Order of work
1. Tests first (red). 2. `costs.py`. 3. build, lint, test.

## Decisions and interpretations
- Money is `Decimal`, never float. `agent_call` events store `tokens` as `{input, output, cache_read, cache_write}`
  and a float `cost_usd`; the ledger recomputes cost from tokens and the price table, so totals are exact.
- Prices: the spec gives none. The table is an in-code, dated (2026-11-11) assumption carried from the previous
  generation's tier prices (Haiku 1/5, Sonnet 3/15, Opus 5/25 USD per million tokens; cache read 0.1x input,
  cache write 1.25x input). Marked "unconfirmed, owner to check" and editable (`PriceTable.replace`).
- Cap check: refuse when the total has already reached the cap (`total >= cap`), as AC2 says. `estimate` is
  returned as `projected_total` for display but does not itself refuse a call (spec: "a call that would start
  after the total has reached the cap").
- Requirement scope refusal returns `escalate_unfinished`; evaluation-run scope returns `stop_run`.
- A run's total is the sum of the totals of its requirements (mapping given when recording).
- Unknown model: costed at the most expensive known price per kind and flagged (`price_known=False`).

## Risks
- Placeholder prices may be wrong: cap accuracy depends on them. Flagged for owner.
- Pro plan calls cost no money; the figures are API-equivalent estimates only.

## Proof (verify_with: pytest, tests/engine/test_costs.py)
- AC1: one hand-computed case per model in the table.
- AC2: refuse at total == cap and above; allow below; requirement scope gives `escalate_unfinished`.
- AC3: ledger rebuilt from events equals the running ledger.
- AC4: run cap sums two requirements.
- AC5: unknown model costed at max price, flagged, nonzero.

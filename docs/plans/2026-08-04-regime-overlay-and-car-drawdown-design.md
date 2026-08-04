# Regime overlay (incl. take-profit) + CAR drawdown fix — design (2026-08-04)

**Status:** IMPLEMENTED 2026-08-04 (Part 1 in `631dc1a`, Part 2 in this changeset). See
"Implementation record" at the bottom for what landed where and the evidence it is not inert.
Originally: designed, ready to implement. Supersedes the gene list in
`2026-07-29-regime-risk-scaling-overlay.md` (adds a 4th gene); that document's reasoning about
WHY the overlay is an exposure-management tool, why the range is two-sided, and how the gene can
end up inert all still stands and is not repeated here.

**Trigger:** the 2020-2025 goal2020 pass was launched, then stopped, on noticing that the
take-profit is the only leg of the risk model with no volatility awareness at all.

---

## Part 1 — CAR's drawdown term is a cliff, not a gradient

`consistent_annual_return` ("CAR") scores
`base x dd_guard x consistency x trade_gate`. Today:

```python
dd_guard = 1.0 if dd <= 20.0 else 20.0 / dd
```

The docstring justifies it as "up to 20% drawdown is explicitly acceptable". But "20% is
acceptable" was implemented as "**everything below 20% is equally good**", which is a different
claim. Consequences:

* Between two genomes with equal return and 4% vs 19% drawdown, the search is **indifferent**.
* Because higher drawdown usually accompanies higher return, and `base` rewards return in full
  while `dd_guard` stays silent below the cap, CAR **actively prefers the riskier genome** as
  long as it stays a hair under 20%.
* Observed live in the aborted run: S2 scored 2.86 on **32% drawdowns while losing money in two
  of five years**; S1, at a third of the drawdown, earned no credit for being safer.

### Fix

```python
dd_guard = _CAR_DD_REFERENCE / max(dd, _CAR_DD_FLOOR)   # 20.0 / max(dd, 1.0)
```

| max DD | today | fixed |
|--------|-------|-------|
| 2%     | 1.00  | 10.00 |
| 5%     | 1.00  | 4.00  |
| 10%    | 1.00  | 2.00  |
| 20%    | 1.00  | 1.00  |
| 30%    | 0.667 | 0.667 |
| 40%    | 0.500 | 0.500 |

* **Above the reference it is byte-identical to today** — same `20/dd`. Only the blind region
  changes.
* **Exactly 1.0 at 20%**, so "20% is the risk budget" survives as a reference point rather than
  a cliff.
* **`_CAR_DD_FLOOR = 1.0`** is a divide-by-zero rail, NOT a policy knob. A higher floor (the
  first proposal was 5%) would re-introduce the very flaw being fixed, just relocated: every
  drawdown below the floor flattened to one value. At 1% the floor essentially never binds —
  observed drawdowns run 8.5-34% — so the metric stays continuously calmar-like across the whole
  realistic range, and the 20x ceiling exists only to stop a degenerate near-zero-drawdown
  genome from running away.

### Consequence to accept deliberately

This makes CAR risk-adjusted rather than risk-capped, so it now behaves much like the
`consistent_calmar` that `2026-07-29` proposed — one fix serves both, and no second metric is
needed. **All previous CAR fitness numbers become non-comparable** (the sen5min3 senate grid, the
S6 deploys). Rankings *within* any single run remain valid. This is acceptable because goal2020
is a full re-run anyway.

`consistency` and `trade_gate` are unchanged and still act as brakes on a thin-but-smooth genome.

---

## Part 2 — Regime overlay, now including take-profit

`ba2_common.core.market_regime` already exists, is evidence-driven, live-computable, and pure —
and is **wired into nothing**. Four genes:

```python
"regime_overlay_enabled": {"optimize": True, "min": 0,   "max": 1,   "step": 1,    "type": "int"},
"regime_risk_scale":      {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
"regime_stop_scale":      {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
"regime_tp_scale":        {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
```

All scales apply **only while `market_regime.is_stressed()`**, and are 1.0 (exact no-op)
otherwise. `1.0` is inside every range, so `enabled=1, scale=1.0` must score identically to
`enabled=0` — a built-in leak check.

### Why take-profit is the best-motivated of the three

`2026-07-29` lists two ways `regime_stop_scale` could be an inert gene: the tighter-wins clamp
may discard a widened stop, and it is partly redundant with ATR stops that already scale per
symbol. **Neither applies to take-profit**: there is no tighter-wins precedence on TP, and TP has
no volatility scaling of any kind today — `adjust_take_profit` accepts only
`order_open_price` / `current_price` / `expert_target_price` with a fixed percent offset.

That asymmetry is the actual defect. The stop is ATR-scaled while the target is fixed, so
reward:risk drifts with each symbol's volatility and the GA can only pick one compromise TP%.

### Scaling is a DELTA multiplier, not a replacement

Applied to the existing percent offset, not to the price:

```
effective_tp_pct = base_tp_pct x regime_tp_scale      (stressed only)
tp_price         = reference_price x (1 + effective_tp_pct/100)
```

So a rule saying "TP at +10% from entry" becomes +15% at `regime_tp_scale=1.5` in a stressed
market and stays +10% otherwise. Every persisted ruleset and genome keeps working untouched,
because absent/neutral genes leave the arithmetic exactly as it is today.

---

## Implementation notes

* Compute the regime **once per bar, market-wide, cached** — it does not vary per symbol.
  Computing it inside a per-symbol loop is the mistake that made still-held
  O(feed x symbols x bars) on 2026-07-28.
* The classifier is causal (closes up to the bar only). Feeding it the full series is lookahead.
* SPY history is verified sufficient: 2,146 daily bars before 2020-01-01, against the 524 the
  volatility regime needs (20 window + 504 rank lookback) and 200 for the trend SMA.
* Live and backtest share the one implementation in `ba2_common`, so no divergence.

## Tests required before this goes in a grid

1. `dd_guard` — continuity at the reference, identity above it, floor clamps at 1%.
2. `enabled=1, scale=1.0` scores **identically** to `enabled=0` (the leak check).
3. TP: with `regime_tp_scale=2.0` in a stressed regime, assert the **submitted order's** TP
   distance actually doubled — not merely that the multiplier was computed. This is the ATR
   failure mode (`use_atr_stop` searched for months while the code path did nothing) and the
   same test discipline `2026-07-29` demands for `regime_stop_scale`.
4. Regime cache: classifier called once per bar, not once per symbol.


---

## Implementation record (2026-08-04)

### Where it landed

| Piece | File |
|---|---|
| `dd_guard` fix + tests | `testplatform/backend/app/services/strategy_fitness.py` (commit `631dc1a`) |
| Scales, per-bar seam, `StressedCalendar` | `packages/common/ba2_common/core/regime_overlay.py` |
| `regime_tp_scale` / `regime_stop_scale` on the percent->price paths | `packages/common/ba2_common/core/TradeActions.py` (`_AdjustPriceLevelAction`) |
| `regime_risk_scale` (size) + `regime_stop_scale` (safeguard stop distance) | `packages/common/ba2_common/core/TradeRiskManagement.py` |
| The 4 settings | `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py` |
| The 4 genes (`_REGIME_OPT` -> `_RM_OPT`) | `testplatform/ba2test_launcher.py` |
| Backtest: build calendar, publish per bar | `daily_backtest_handler.py` / `daily_engine.py` |
| Live: publish per refresh, cached per day | `ba2_trade_platform/core/TradeManager.py` |

### Two decisions the design did not anticipate

**There are TWO percent->price sites, not one.** `execute()` computes the price inline for a
single TP-or-SL adjustment, but `compute_price()` serves the MERGED TP+SL path that
`TradeActionEvaluator` uses for entry brackets — the common case in a backtest. The scaling
therefore lives on the shared base `_AdjustPriceLevelAction`, not on a mixin over the two
subclasses, which would have covered only `execute()` and left the dominant path unscaled.

**`risk_per_trade_pct` means two different things** and needed two different genes.
In `_ensure_safeguard_stop` it is a stop DISTANCE, so it scales with `regime_stop_scale`
(alongside `atr_multiplier` and `min_stop_loss_pct` — all three, because the function is a
`max(min(...))` over them and scaling one would change which candidate wins). In
`_risk_atr_quantity` it is a dollar RISK BUDGET, so it scales with `regime_risk_scale`.

`regime_risk_scale` also scales `max_equity_per_instrument`, which is what makes it bite in
`notional` mode (the majority of the grid) and not only in `risk_atr`.

### The missing-input failure is FATAL, not quiet

`DailyBacktestEngine._check_regime_calendar` raises when any expert has
`regime_overlay_enabled` and no benchmark calendar could be built. Without that, an unreadable
SPY cache would degrade the overlay to "never stressed" — four genes searched against dead code,
which is exactly the `use_atr_stop` failure this document set out not to repeat. Live is the
opposite by design: an unavailable regime publishes `None` (neutral) and trading continues.

### Evidence it works

Unit: `packages/common/tests/test_regime_overlay.py` (21) and
`test_regime_overlay_actions.py` (12). The calendar test asserts day-for-day equality with
`market_regime.is_stressed(closes[:i+1])` for every day, plus a guard that the sample actually
reaches STRESSED so the equality cannot pass vacuously.

End-to-end, FMPEarningsDrift over 2022 (69% of its sessions classify STRESSED), 8 symbols,
`risk_atr`:

| run | trades | return | max DD |
|---|---|---|---|
| A — overlay off | 16 | -4.52% | -8.43% |
| B — on, all scales 1.0 | 16 | -4.52% | -8.43% |
| C — on, risk 0.5 / stop 2.0 / tp 2.0 | 15 | -4.55% | -7.65% |

B is identical to A (the leak check, on the real engine rather than in a unit test). C differs,
so the genes reach the trades — and de-risking into a stressed year cut drawdown, the expected
direction.

### Regime frequency on the goal2020 window

Share of sessions classified STRESSED, from the cached SPY daily series (3,777 bars,
2011-06-22 -> 2026-06-30):

    2020  50%   2021   3%   2022  69%
    2023   0%   2024  10%   2025  42%

28.9% over 2020-2025 as a whole. The overlay is inactive for most of 2021 and 2023, so a genome
that enables it is betting specifically on 2020/2022/2025 — worth remembering when reading which
years drive a winner's fitness.

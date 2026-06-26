# Screener `price_drop_pct` degenerate-metric fix

_2026-06-26_

## TL;DR

The screener metric store's `price_drop_pct` column was **all zeros** because the
store was built with `drop_days=1`. Any screen with a `price_drop_pct > 0` filter
therefore selected **zero symbols** ("selected zero symbols for the window/settings").
The default was changed `1 → 5`; **the store must be rebuilt** for the fix to take
effect. Existing optimization results are still valid (they converged to "no dip
filter"), but the dip dimension was never actually explored — see *Impact on the
current optimization* below.

## The bug

`price_drop_pct` measures the pullback from the trailing-window peak:

```python
# packages/providers/ba2_providers/screener/metric_store.py  (compute_daily_metrics)
peak     = close.rolling(max(1, drop_days), min_periods=1).max()   # trailing-window peak
drop_pct = ((peak - close) / peak * 100.0).where(peak > 0, 0.0)
```

With `drop_days=1` the rolling window is a **single bar**, so `peak == close` on
every row and `drop_pct == 0` everywhere. The column is present and well-formed —
it is just uniformly `0.0`, which is why nothing flagged it until a screen used it.

The existing store at `~/Documents/ba2/common/cache/screener/metric_store`
(≈4,977 symbols, 2022-06 → 2025-12, ≈821k rows) was built with the old default of
`drop_days=1`, so its `price_drop_pct` is all zeros.

## Why it surfaced in a single backtest but not in optimization

This is the key subtlety and the reason the bug hid for so long.

| | How the candidate universe is built | Effect of the all-zero column |
|---|---|---|
| **Single backtest** (`handle_daily_backtest` → `_build_config`) | Screens with the **exact** settings the user chose. | A `price_drop_pct >= 5` filter on an all-zero column → **0 symbols** → the run aborts with *"selected zero symbols"*. |
| **Optimizer** (`strategy_optimization_handler` → `_build_daily_trial_config`) | Builds one **candidate union** across the whole GA population using the **loosest** gene value (`price_drop_pct_min = 0`). | The union is built with threshold `0`, which passes everything, so the union is **never empty** and the job runs to completion. |

So the optimizer **masked** the broken metric; the single backtest **exposed** it.

## Impact on the current optimization (jobs already run)

Inside the GA, each individual is still evaluated with **its own** `price_drop_pct`
threshold. Any individual that drew `price_drop_pct > 0` screened against the
all-zero column → **0 symbols → 0 trades → poor fitness**. Natural selection
therefore drove `price_drop_pct → 0` and kept it there.

Consequences:

- **Existing results are valid** — but only for the regime they actually explored:
  `price_drop_pct ≈ 0`, i.e. **the dip filter effectively disabled**. The best
  parameters found are correct *given no dip filter*.
- **The dip dimension was never genuinely searched.** The GA could not learn
  anything about `price_drop_pct > 0` because every such trial was structurally
  zero-trade, not genuinely unprofitable.
- **You do NOT need to re-run for correctness.** Re-run an optimization only if you
  want the price-drop / dip filter to become a **live, meaningful dimension** — and
  only **after** rebuilding the store with a valid lookback.

## The fix

`drop_days` default changed `1 → 5` (≈ one trading week) in all three places:

- `metric_store.compute_daily_metrics(..., drop_days=5)`
- `metric_store.build_store(..., drop_days=5)`
- `ba2-test build-screener-metrics --drop-days` (CLI default `5`, help notes it must be ≥ 2)

`drop_days` is a **build-time** parameter — it is baked into the `price_drop_pct`
column when the store is written. Changing the default does nothing to an existing
store; the store has to be **rebuilt**.

### Rebuilding

```bash
ba2-test build-screener-metrics \
  --store ~/Documents/ba2/common/cache/screener/metric_store \
  --start 2022-06-01 --end 2025-12-31 \
  --market-cap-min <your floor>
# --drop-days now defaults to 5
```

> Note: a vanilla rebuild calls `enumerate_universe → _fetch_screener_rows`, which
> is a **live FMP request** for the symbol list (per-symbol OHLCV / market-cap /
> float are served from the disk cache). A fully **cache-only** rebuild (no FMP at
> all) requires deriving the universe from the existing store instead — tracked
> with the optimizable-lookback work below.

## Known limitation → follow-up: optimizable lookback window

`drop_days` is a single build-time scalar, so today the **only** way to optimize
the lookback window is to build a separate store per value — impractical. The
planned fix stores the pullback for a **range of windows up to a `max_lookback`
(default 30)** in one store, so a single build supports optimizing *both* the
drop threshold *and* the window `Y ≤ max_lookback` with no per-value rebuild.

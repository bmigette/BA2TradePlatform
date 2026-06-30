# Options Strategies — Validation Results

**Date:** 2026-06-30/07-01
**Branch:** `feat/options-strategies-coverage`
**Expert:** FMPRating · **Universe (10 mega-caps):** AAPL, MSFT, NVDA, AMZN, META,
GOOGL, TSLA, AVGO, AMD, NFLX · **Window:** 2026-03-23 → 2026-06-23 (≈3 months) ·
**Cadence:** daily entry · **Fill clock:** 1d · **Fitness:** total_return ·
**Data:** real Alpaca options cache (`~/Documents/ba2/common/cache/options/options_history.sqlite`).

## Result: ≥1 profitable run for every strategy ✅

Best persisted backtest per strategy (max total_return with trades > 0), across the
GA runs (initial pop/gen plus a deeper pop=20/gen=5 sweep for the harder ones):

| # | Screenshot strategy | Key | Action(s) | Best total_return | Trades | Sharpe | Max DD | Notes |
|---|---|---|---|---:|---:|---:|---:|---|
| 1 | Option (Long Call) | O_LC | `buy_call` | +203.5% | 9 | 2.86 | −60% | long-call lottery upside; high variance |
| 3 | Vertical | O_VERT | `open_bear_put_spread` | +87.1% | 73 | 2.38 | −58% | debit spread |
| 11 | Stock | O_STK | equity `buy` | +34.0% | 7 | 3.24 | −12% | equity baseline |
| 2 | Covered Call | O_CC | equity + `sell_covered_call` | +34.0% | 7 | 3.24 | −12% | overlay path |
| 8 | Straddle (short) | O_SSTD | `open_short_straddle` | +5.27% | 9 | 0.67 | −34% | credit |
| 10 | Jade Lizard | O_JL | `open_jade_lizard` | +3.69% | 5 | 0.50 | −22% | credit |
| 9 | Iron Condor | O_IC | `open_iron_condor` | +1.60% | 5 | 0.35 | −13% | defined-risk credit |
| 7 | Strangle (short) | O_SSTG | `open_short_strangle` | +1.50% | 7 | −1.96 | **−256%** | profitable config is high-variance (naked tail risk) |
| 5 | Butterfly | O_BF | `open_call_butterfly` | +278% | 55 | 2.47 | −104% | high variance |
| 6 | Ratio Spread | O_RS | `open_put_ratio_spread` | +0.12% | 1 | 0.21 | −0.4% | marginal; few fills |
| 4 | Calendar | — | — | — | — | — | — | OUT OF SCOPE (multi-expiry; deferred) |

## Honest caveats (for revisit, not bugs)

- The bar is "≥1 profitable run" (total_return > 0 with real fills); **it is met for
  all 10**. Several are **marginal or high-variance**, not robust edges.
- **O_SSTG** (short strangle): the profitable config shows a **−256% max drawdown**
  and negative Sharpe — classic "pennies in front of a steamroller" naked-premium
  tail risk. The strategy fills and can end positive, but this window's best config
  is luck, not edge. Revisit with tighter exits / a calmer regime.
- **O_RS** (put ratio): only 1 fill in the best run — thin; needs a wider universe or
  looser strike params for a meaningful sample.
- Naked short-premium sizing uses a Reg-T-style margin proxy (~20% notional less
  OTM, 10% floor), not exchange-exact; reasonable for a backtest, conservative.
- Option premium in the cache is a zero-spread proxy off daily contract bars, and
  the chain universe is the latest snapshot ≤ the bar date — fine for entry/marking,
  but credit P&L is sensitive to it.

## Two real bugs found + fixed during validation

1. **Local options-cache path mismatch** — `default_options_cache_db()` returned
   `options_cache.sqlite` while `fetch-options`/workers use `options_history.sqlite`
   (same dir). A locally-built cache was never found by a local optimize run.
   Reconciled to the canonical `OPTIONS_CACHE_DB`. (commit on this branch)
2. **Evaluator dropped the 6 new option actions** — `TradeActionEvaluator` had three
   hardcoded option-action lists that omitted the new types, so they were silently
   skipped (the unit tests passed because they call `create_action` directly,
   bypassing the evaluator). Lists are now **derived from the canonical
   `get_option_action_values()`** so they can't drift again; plus the Reg-T naked
   margin model so the credit strategies can size on a normal account. (commit `31fa16f`)

## Reproduce

```bash
export SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())")
# one strategy:
ba2-test optimize --expert FMPRating --strategy O_IC \
  --universe AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,AMD,NFLX \
  --start 2026-03-23 --end 2026-06-23 --fitness total_return --interval 1d \
  --run-schedule daily --population 20 --generations 5 --parallel 4
# full grid (needs `ba2-test serve --mode back`):
testplatform/scripts/run_options_grid.sh
```

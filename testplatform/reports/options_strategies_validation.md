# Options Strategy Coverage — Implementation & Validation Report

**Date:** 2026-06-30 → 2026-07-01
**Branch:** `dev` (merged from `feat/options-strategies-coverage`)
**Scope:** make the options experts run the tastytrade "Strategy" menu (except Calendar),
validate on real Alpaca options data, and harden the backtest option accounting so
equity can never blow arbitrarily negative.

---

## 1. What was built

Six NEW option action classes (`packages/common/ba2_common/core/TradeActions.py`,
all subclassing `_OptionEntryAction`; percent-OTM + DTE + wing-width selection —
the Alpaca cache has no greeks):

| Screenshot strategy | Action type | Notes |
|---|---|---|
| Strangle (short) | `open_short_strangle` | credit |
| Straddle (short) | `open_short_straddle` | credit |
| Iron Condor | `open_iron_condor` | 4-leg defined-risk credit |
| Jade Lizard | `open_jade_lizard` | credit |
| Butterfly | `open_call_butterfly` | 1-2-1 debit |
| Ratio Spread | `open_put_ratio_spread` | 1-2 credit |

Already-supported and included in the grid: Option (`buy_call`/`buy_put`),
Covered Call (`sell_covered_call`), Vertical (`open_bear_put_spread` etc.), Stock
(equity). **Calendar is OUT OF SCOPE** (needs multi-expiry support).

Supporting infrastructure:
- **Entry-option path** — the `enter_market` ruleset can now fire an option action
  directly (no equity leg): `Strategy.entry_action` → `daily_backtest_handler`
  `config["entry_action"]` → `seed_ruleset_from_tree(entry_action=…)` →
  `daily_engine._run_expert_bar` submits it directly (`submit_to_broker=True`);
  `strategy_uses_options` + the optimizer trial-config thread it through.
- **`select_wing`** strike helper + **`option_wing_width`** optimizer gene.
- **10 launcher option-strategy builders** (`O_LC, O_VERT, O_STK, O_CC, O_SSTG,
  O_SSTD, O_IC, O_JL, O_BF, O_RS`) for FMPRating in `ba2test_launcher.py`.
- **`testplatform/scripts/run_options_grid.sh`** — FMPRating × the 10 strategies,
  daily cadence, 1d clock, builds the options cache first.

---

## 2. Bugs found & fixed during validation

### 2.1 Wiring bugs (initial)
1. **Evaluator dropped the 6 new actions.** `TradeActionEvaluator` had three
   hardcoded option-action lists that omitted the new types, so they were silently
   skipped (unit tests passed because they call `create_action` directly). The
   lists are now **derived from canonical `get_option_action_values()`** — can't
   drift. (commit `31fa16f`)
2. **Local options-cache path mismatch.** `fetch-options`/workers write
   `options_history.sqlite`; the local handler read a sibling `options_cache.sqlite`
   → a locally-built cache was never found. Reconciled to canonical `OPTIONS_CACHE_DB`.

### 2.2 Option accounting / risk hardening (the "no negative equity" work)
A short strangle showed a **−256% drawdown** (equity went deeply negative). Root-
cause investigation revealed several distinct, layered accounting bugs. Each was
fixed with a controlled TDD test:

| # | Bug | Fix | Commit |
|---|---|---|---|
| a | Multi-leg legs never settled at expiry (parent order has no `contract_symbol`) | match combos via child legs | `d63e2bf` |
| b | No margin discipline → naked shorts sized ~15× the account | per-bar **maintenance-margin check + forced liquidation** | `2804002` |
| c | Closed/settled legs still reported "held" → phantom re-assignment (−8974%) | net signed qty across opening+closing fills | `953eea9` |
| d | Noisy per-leg cache premiums swung multi-leg MTM ±$100k | **clamp defined-risk combo MTM** to no-arb bounds | `51637fe`, `a302e57` |
| e | Debit entries sized off analysis-quote but filled at divergent premium → cash negative | **cash-secured guard** at fill | `c5ed028` |
| f | Leg-by-leg expiry settlement of defined-risk combos broke the bounded payoff | **unit-settle combos** to net cash payoff, no per-leg stock | `58866a3` |
| g | Leftover long-leg value erased after shorts close (1-bar transient) | **composition-aware clamp** + intrinsic fallback for missing bars | `bf2053e` |
| h | Margin-call liquidated the **covered short legs** of defined-risk combos → unhedged blow-up (O_VERT −$23k) | **exclude covered short legs** from margin requirement & liquidation | `b2a9294`, `78f84fd` |

Plus `b1d850e`: the margin-call is gated to option runs (no impact on equity
backtests). **Net effect:** naked short premium is bounded by a Reg-T-style
margin-call liquidation; defined-risk combos (verticals / butterfly / iron condor)
are unit-settled and their MTM is clamped to their theoretical range; debit entries
are cash-secured. **No strategy can drive equity arbitrarily negative.**

---

## 3. No impact on non-option backtests (verified)

- All new logic is gated to option runs: `_option_positions_mtm` returns `0.0` when
  `self._options is None`; `maybe_margin_call_liquidation` short-circuits when no
  options provider; `_apply_option_expiry`/`get_option_positions` short-circuit for
  equity runs.
- Empirical A/B: **O_STK (equity) is byte-identical** — +34% / same params before
  and after all the option-accounting work.
- Full suites green: **backend 234 passed / 1 skip, ba2_common 88 passed.**

---

## 4. Final validation (FMPRating × 10 mega-caps, 2026-03-23 → 06-23, daily / 1d)

Universe: AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AVGO, AMD, NFLX.
Best persisted backtest per strategy (pop 8/gen 2 unless noted). **Corrected
accounting** — these numbers are honest (the earlier inflated figures were the
accounting artifacts fixed in §2.2).

| # | Strategy | Key | Best return | Trades | Worst DD | Min final eq | Bounded | Profitable |
|---|---|---|---:|---:|---:|---:|:--:|:--:|
| 1 | Long Call | O_LC | +203.5% | 13 | −76.9% | $9,172 | ✅ | ✅ |
| 3 | Vertical (bear put) | O_VERT | +63.3% | 206 | −97.5% | $3,837 | ✅ | ✅ |
| 11 | Stock | O_STK | +33.8% | 8 | −13.1% | $12,784 | ✅ | ✅ |
| 2 | Covered Call | O_CC | +33.8% | 8 | −13.1% | $12,784 | ✅ | ✅ |
| 8 | Short Straddle | O_SSTD | +47.8% | 20 | −19.6% | $9,910 | ✅ | ✅ |
| 7 | Short Strangle | O_SSTG | +3.0%¹ | 8 | −20.9% | $8,329 | ✅ | ✅ |
| 9 | Iron Condor | O_IC | +0.9% | 16 | −45.4% | $6,126 | ✅ | ✅ |
| 5 | Butterfly | O_BF | +360.9% | 234 | −93.1% | $3,692 | ✅ | ✅ |
| 6 | Ratio Spread | O_RS | +22.1% | 18 | −46.4% | $5,826 | ✅ | ✅ |
| 10 | Jade Lizard | O_JL | −16.0%¹ | 15 | −64.0% | $7,742 | ✅ | ❌ |
| 4 | Calendar | — | — | — | — | — | OUT OF SCOPE | — |

¹ O_SSTG / O_JL from a deeper GA (pop 20/gen 5).

**Outcome:**
- **Bounded (no drawdown < −100%, min final equity ≥ 0): 10/10 ✅** — the
  negative-equity problem is fully solved.
- **≥1 profitable run: 9/10.** Only **O_JL (jade lizard)** has no profitable config
  in this window even with a deep GA. It is a credit structure and this window's
  regime is adverse for it; its previously-reported profit was an accounting
  artifact now corrected. Flagged for revisit (different window/params), not forced.

---

## 5. Known limitations / follow-ups

- **O_JL unprofitable in the tested window** — revisit with a calmer window or
  different strike/DTE ranges. Not a code defect (bounded + fills correctly).
- **Calendar spreads** — deferred (multi-expiry fill/cache support).
- **Delta strike selection** — unsupported (Alpaca cache has no greeks); percent-OTM
  + DTE + wing-width used throughout.
- **Options cache** is a zero-spread proxy off daily contract bars with a latest-
  snapshot-as-of chain; credit P&L is sensitive to it (mitigated by the MTM clamp).
- Maintenance-margin is a Reg-T-style proxy (~20% notional less OTM, floored 10%),
  not exchange-exact — conservative and consistent with the entry reserve.

---

## 6. Reproduce

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
Prereqs: options cache built (`ba2-test fetch-options`, account-3 options-entitled
Alpaca key), OHLCV + FMP `analyst_grades` pre-warmed for the universe.

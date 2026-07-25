# Options Data & Intraday Support — Roadmap

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan
> task-by-task. Phases are ordered by dependency; do not start a phase whose predecessor is
> unfinished, because each one changes what the next can measure.

**Goal:** Move the options backtest from "daily bars, no quotes, one bull regime" to a footing
where its results are decision-grade — real quotes, multiple market regimes, and (Phase 3)
intraday bars, which are the precondition for every 0DTE strategy.

**Architecture:** Keep the vendor seam already built (`OptionsDataProviderInterface` +
`AlpacaOptionsProvider` / `ThetaDataOptionsProvider`). Add a second, *intraday* cache tier
alongside the existing daily one rather than migrating it — the daily tier stays the right
shape for 30–45 DTE premium selling and must not regress.

**Tech Stack:** Python, SQLite (daily option cache), Parquet (equity OHLCV cache; and the
proposed intraday option cache), ThetaData Terminal v3 REST, alpaca-py.

---

## Status: what already shipped (2026-07-25, do NOT redo)

| Item | Commit |
|---|---|
| `_annualized_return` returns −100 (not 0) when equity is wiped; MC mirror fixed | `9f2a934` |
| Option spread cost model (percent-of-premium) at `_option_fill_price` | `0488892` |
| Volume sourced from the bar; `min_volume` gate threaded through the selectors | `0488892` |
| `OptionsDataProviderInterface` + Alpaca/ThetaData implementations | `4b7f628` |

### Measured baseline these phases must improve on

```
cache: 13,729,262 daily bars | 98 underlyings | 2024-02-01 -> 2026-07-07 (~2.4y)
quotes:      0 of 958,024 chain rows carry a real spread (701,849 bid==ask, 256,175 NULL)
open_interest: 0 of 958,024 populated
volume:  13,729,262 of 13,729,262 populated  (p10=1, p25=3, p50=14, p75=71, p90=319)
live contracts on a given day: ~22,800
```

---

## Phase 1 — Real quotes (unblocks validating the Phase 0 spread model)

**Why first:** the spread model shipped in `0488892` is an *assumption* (5% of premium, ×2 when
thin). Nothing has confirmed it. Real EOD bid/ask turns it from a guess into a calibrated
number, and every later phase inherits that calibration.

**Prerequisite (user action):** a ThetaData subscription + a locally running Theta Terminal on
`http://127.0.0.1:25503`. Recommend the **$80/8-year** tier — see Phase 2 for why 8 years and
not 4.

### Task 1.1: Wire `OptionsDataProviderInterface` into the cache builder

**Files:**
- Modify: `testplatform/backend/app/services/backtest/fetch_options.py`
- Test: `testplatform/backend/tests/backtest/test_fetch_options_provider_seam.py` (create)

`fetch_options.py` calls alpaca-py directly (`TimeFrame.Day` at line ~471, `_alpaca_keys`,
`GetOptionContractsRequest`). Replace those call sites with the interface so the vendor is
selectable, keeping Alpaca as the default so nothing changes until asked.

1. Add `--options-provider {alpaca,thetadata}` (default `alpaca`) to the `fetch-options`
   subparser in `testplatform/ba2test_launcher.py` (~line 3338).
2. In `build_cache`, resolve via `ba2_providers.get_provider("options", name)` and call
   `discover_contracts()` / `fetch_eod_bars()` instead of the inline alpaca calls.
3. Persist `OptionEodBar.bid` / `.ask` into `option_bar` — **requires a schema change**, see
   Task 1.2.

**Commit:** `refactor(options): build the cache through the provider seam`

### Task 1.2: Add bid/ask to `option_bar` (migration)

**Files:**
- Modify: `testplatform/backend/app/services/backtest/options_cache.py` (`_BAR_DDL`, `_BAR_COLS`)
- Test: `testplatform/backend/tests/backtest/test_options_cache_quote_columns.py` (create)

`option_bar` has no bid/ask at all — quotes live only on the single-snapshot `option_chain`.
Add `bid REAL, ask REAL, open_interest INTEGER` to the bar table. Use `ALTER TABLE ADD COLUMN`
(nullable) so the existing 13.7M-row cache is not rebuilt; old rows keep NULL and fall back to
the modeled spread.

### Task 1.3: Prefer real quotes over the model, per-bar

**Files:**
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py`
  (`_option_half_spread`)
- Test: extend `testplatform/backend/tests/backtest/test_option_spread_cost.py`

```python
# Real quote wins; the model is the fallback for bars that have none.
bid, ask = bar.get("bid"), bar.get("ask")
if bid is not None and ask is not None and ask > bid:
    return (ask - bid) / 2.0
# ...existing modeled path...
```

**Acceptance:** a bar with a real quote uses it; a bar without falls back to the model; a
degenerate `bid==ask` quote (the whole Alpaca problem) does NOT count as real.

### Task 1.4: Calibrate the model against reality

**Files:** Create `test_files/calibrate_option_spread.py` (ad-hoc probe, not pytest)

Over ThetaData-sourced bars, compute actual `(ask-bid)/mid` bucketed by premium and volume.
Compare against the shipped constants (`option_spread_pct=5.0`, `_OPTION_SPREAD_THIN_MULT=2.0`,
`_OPTION_SPREAD_LIQUID_VOLUME=100`). **Report the delta before changing anything** — if the
model is materially off, that also tells us how wrong every pre-calibration options result was.

---

## Phase 2 — Regime coverage (the binding constraint on all conclusions)

**Why:** our 2.4-year window (2024-02 → 2026-07) is almost entirely bull. The Cboe/Bondarenko
study that anchors the whole VRP case finds put-writing's edge is *regime-dependent* — it
outperforms in 2008/2022 and underperforms in rallies. We are fitting premium-selling
parameters on the regime where they look best and cannot test the one that kills them.

An 8-year ThetaData window reaches back to ~2018 and captures:

| Regime | Why it matters here |
|---|---|
| Feb 2018 "Volmageddon" | short-vol blow-up — the exact tail short strangles carry |
| Mar 2020 COVID crash | fastest drawdown on record; tests the DD guard and margin liquidation |
| 2022 bear | the Bondarenko case for when put-writing *wins* |

### Task 2.1: Extend the cache window
Re-run `fetch-options` with `--options-provider thetadata` from 2018-01-01. Expect roughly
4× the current row count (~55M daily bars); confirm SQLite still performs acceptably before
going wider — if not, escalate to the Parquet layout designed in Phase 3.

### Task 2.2: Per-regime backtest slices
**Files:** Modify `testplatform/ba2test_launcher.py`

Add a `--regime-slices` mode that runs the same genome over named sub-windows and reports
per-slice metrics rather than one blended number. A config that is only profitable in
2024–2026 must be *visibly* so.

### Task 2.3: Re-run the option grids on the deeper window
Only after 2.1/2.2. Compare against the pre-calibration winners and expect reordering —
record it, since it measures how misleading the 2.4-year window was.

---

## Phase 3 — Intraday options support

**This is the big one.** It is a new data tier and a new engine clock, not a parameter change.

### 3.0 Why it cannot be done by widening the current path

Three hard blockers, all verified:

1. **Fetch** — `fetch_options.py:471` hardcodes `timeframe=TimeFrame.Day`.
2. **Schema** — `option_bar` is `PRIMARY KEY(occ_symbol, date)`. A *date*. It physically
   cannot hold two bars for one contract on one day.
3. **Fill engine** — `_option_fill_price` calls `get_bar(contract, fill_day)` where `fill_day`
   is always a `.date()`. Even in a `--interval 5min` run, **option legs price once per day
   while equity legs re-price every 5 minutes.** That asymmetry is silent today and is itself
   worth fixing.

And the reason the schema was built that way — cardinality:

| | instruments | bars/session | rows over our window |
|---|---|---|---|
| Equity 5m | ~500 | 78 | ~49M |
| Options daily | ~22,800 | 1 | 13.7M (today) |
| **Options 5m, whole chain** | ~22,800 | 78 | **~1.07B** |

A billion rows in SQLite is not viable. **The design must avoid ever needing the whole chain
intraday.**

### Task 3.1: Selective intraday universe (the key design decision)

**Insight:** no strategy needs the whole chain intraday. 0DTE strategies need *near-ATM strikes
on a handful of very liquid underlyings on the day of expiry*. Sizing that:

```
SPY + QQQ, ATM +/- 10 strikes, calls+puts, 0-2 DTE
= 2 underlyings x 42 contracts x 78 bars x ~250 expiry-days/yr x 8 yrs
~= 13M rows   <-- comparable to today's ENTIRE daily cache
```

That is three orders of magnitude below the naive 1.07B and completely tractable.

**Files:**
- Create: `testplatform/backend/app/services/backtest/intraday_options_cache.py`
- Test: `testplatform/backend/tests/backtest/test_intraday_options_cache.py`

Follow the **Parquet** precedent already used for equity OHLCV
(`CACHE_FOLDER/<Provider>/<SYM>_<interval>.parquet`), not SQLite:
`CACHE_FOLDER/options_intraday/<UNDERLYING>/<YYYY-MM-DD>_<interval>.parquet`,
columns `ts, occ_symbol, open, high, low, close, volume, bid, ask`.
Date-partitioned files keep any single read small and let a run load only the sessions it
touches.

### Task 3.2: Extend the provider interface with an intraday method

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsDataProviderInterface.py`
- Modify: `packages/providers/ba2_providers/options/{alpaca,thetadata}.py`
- Test: extend `packages/providers/tests/test_options_data_providers.py`

Add `fetch_intraday_bars(contracts, *, start, end, interval)` plus a
`supports_intraday()` capability flag. ThetaData v3 exposes `/v3/option/history/ohlc` with an
interval parameter; Alpaca's option bars endpoint accepts `TimeFrame.Minute`. Raise
`NotImplementedError` from any provider that cannot serve it rather than silently returning
daily bars — a silent granularity downgrade is exactly the class of bug that makes a 0DTE
backtest look profitable.

### Task 3.3: Timestamp-aware option bar lookup

**Files:**
- Modify: `testplatform/backend/app/services/backtest/options_provider.py`
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py`
  (`_option_fill_price`)
- Test: `testplatform/backend/tests/backtest/test_option_intraday_fill.py`

Add `get_bar_at(occ_symbol, ts)` resolving to the intraday tier when present and falling back
to the daily bar otherwise. `_option_fill_price` takes the run's `as_of` **timestamp** rather
than `.date()`. Fall back must be explicit and logged — never silently fill an intraday
strategy off a daily bar.

**Acceptance:** in a 5-minute run with an intraday cache present, an option leg and its equity
leg re-price on the same clock. Assert this directly; it is the asymmetry from 3.0.

### Task 3.4: Intraday-aware volume participation

**Files:** Modify `backtest_account.py` (`_volume_cap_reject_reason`)

`_OPTION_FILL_MAX_VOLUME_PARTICIPATION` (10%) is calibrated against a *daily* volume figure.
Against a 5-minute bar's volume it becomes ~78× stricter and would block nearly everything.
Make the cap tier-aware.

### Task 3.5: Intraday clock in the engine

**Files:** Modify `testplatform/backend/app/services/backtest/daily_engine.py`

The engine is daily-expert shaped. Intraday option strategies need per-bar evaluation within
a session plus a **forced time-based exit** (the report's 3:45pm liquidation). This is the
largest single piece of work in the plan — scope it as its own sub-plan once 3.1–3.4 land, and
do not begin it before then.

---

## Phase 4 — 0DTE strategies (blocked on Phase 3)

Only after intraday data exists. Per the report's own Table 3, the strategies are
Iron Butterfly / Iron Condor / credit spreads with 108–251 minute average holds and win rates
that must be high because average losses run ~2× average gains.

**Treat the report's Tables 2–3 as hypotheses, not settings** — they trace to a vendor blog
(Option Alpha's own user population) and a YouTube backtest, not to independent research.
Source [33] in the research doc is a thread documenting two platforms producing *materially
different results on the same 0DTE backtest*, which is precisely the fill-modeling sensitivity
Phase 0 and Phase 1 exist to control.

**Do not skip the forced 3:45pm exit** — without it a 0DTE backtest silently holds to
settlement, which is a different strategy with a different risk profile.

---

## Phase 5 — Wheel state machine expert (independent; can run in parallel)

CSP, assignment and covered calls all already exist separately (`O_CSP`, `O_CC`, assignment
modeled in `test_option_assignment.py`), but nothing rotates the states. The research doc's
recovered Phase-1/Assignment/Phase-2 description is a complete spec.

**Files:** Create `packages/experts/ba2_experts/WheelSeller/` following the `PremiumSeller/`
layout (`__init__.py` settings + `signals.py` + `portfolio.py`).

**Watch item:** `O_CC`/`O_PP` were previously found silently degrading to plain equity at $20k
because `floor(held/100)` never reached 100 contracts. A Wheel on a $20k account has the same
constraint — pick underlyings whose 100-share assignment fits the balance, or the expert will
quietly do nothing.

---

## Phase 6 — Regime awareness (partly shipped; independent of the options data phases)

**Origin:** if strategy performance is regime-dependent, testing across regimes only tells you
*that* a strategy breaks. The system should be able to name the current regime and gate on it.
User decision (2026-07-25): implement as **TradeConditions**, not per-regime parameter sets —
the GA already searches conditions via `rule:<rid>:enabled`, so it can *discover* whether
gating helps rather than us asserting it.

**Key asymmetry that makes this actionable now:** SPY/QQQ daily closes are cached back to
**2011-06-22 (15 years)**, while options only reach 2024-02. So the classifier can be built and
validated against 2018/2020/2022 immediately, and regime gating is fully testable today for
**equity** experts. Only regime-gated *options* work waits on Phase 2.

### 6.1 SHIPPED: `packages/common/ba2_common/core/market_regime.py`

Signal choice was made empirically, not by intuition. Measured on 3,777 SPY sessions, scoring
each causal signal by how well it separates **forward 21-session realized volatility**, split
into three independent sub-periods to reject single-episode artifacts:

| signal | 2011-18 | 2019-22 | 2023-26 | FULL |
|---|---|---|---|---|
| **close vs SMA200** | 7.1 | 10.3 | 10.8 | 10.4 |
| drawdown from 252d high | 9.9 | 11.9 | 5.8 | 12.1 |
| Weinstein stage | 7.7 | 14.7 | 12.6 | 11.4 |
| realized-vol rank | 4.9 | 10.4 | 6.3 | 7.8 |
| hand-combined 2-factor score | 5.5 | 11.9 | 6.7 | 8.8 |

Three findings that shaped the module:

1. **Regime predicts VOLATILITY, not DIRECTION.** Forward returns also separated — but with
   the counter-intuitive sign (higher forward returns in "bad" states). That is a 2011–2026
   sample artifact (dip-buying worked in a period with no sustained bear outside 2022), so no
   directional regime is exposed. Volatility clustering held in *every* sub-period.
2. **Expose orthogonal primitives; do not hand-combine.** The hand-tuned 2-factor score scored
   *worse than plain SMA200* in every sub-period. Trend and volatility ship as two independent
   axes for the GA to combine.
3. **Weinstein stage is not a risk ladder.** Stage 1 (basing) shows *higher* forward vol than
   Stage 4 (declining) — 23.2 vs 20.7 full-sample. Keep it as a per-stock entry filter (its
   actual purpose); do not reuse its stage order as market risk severity.

Validated causally against real history — note 2018-02-06 and 2024-08-05, where trend said
`risk_on` and only the vol axis caught the event, and 2020-02-19, where the classifier
correctly did **not** foresee a crash three weeks out:

```
2018-02-06  trend=risk_on  (+6.25%)  vol=stressed (rank 97)   Volmageddon
2018-12-24  trend=risk_off (-14.8%)  vol=stressed (rank 97)   Q4-18 trough
2020-02-19  trend=risk_on  (+11.5%)  vol=normal   (rank 59)   pre-COVID ATH
2020-03-23  trend=risk_off (-26.5%)  vol=stressed (rv 82%)    COVID trough
2021-11-19  trend=risk_on  (+10.0%)  vol=calm     (rank 3)    melt-up top
2022-06-16  trend=risk_off (-16.9%)  vol=stressed (rank 94)   2022 bear trough
2024-08-05  trend=risk_on  ( +3.5%)  vol=stressed (rank 81)   yen-carry unwind
2026-06-30  trend=risk_on  ( +8.0%)  vol=stressed (rank 84)   most recent bar
```

**Known limitation:** it does not call tops (2021-11-19 reads calm/risk_on). It measures
turbulence, not valuation. Do not market it as a top-detector.

### Task 6.2: `MarketRegime*` TradeConditions

**Files:**
- Modify: `packages/common/ba2_common/core/TradeConditions.py`
- Test: `packages/common/tests/test_market_regime_conditions.py` (create)

Add `MarketRiskOnCondition` / `MarketRiskOffCondition` / `MarketVolatilityStressedCondition` /
`MarketVolatilityCalmCondition` as `FlagCondition` subclasses. Fetch benchmark closes through
the existing `_provider_resolver` seam (`set_provider_resolver`), clamped to the run's as-of
date. Follow the `BearishCondition` shape: try/except → `False`, plus `get_description()` and
`get_actual_value_display()` (surface the SMA distance / vol rank so the UI shows *why*).

**Benchmark symbol must be a setting**, defaulting to `SPY` — a EUR-denominated or
sector-specific book needs a different benchmark.

### Task 6.3: Cache the regime per (benchmark, as_of)

Every condition evaluation would otherwise recompute a 504-session rank for every symbol on
every bar. Memoize per `(benchmark, as_of)` at worker-process level — reuse the pattern from
`options_provider._WORKER_ATM_IV_CACHE` (`_MISSING` sentinel, LRU bound, cleared by
`clear_worker_options_cache`).

### Task 6.4: Register in the rule vocabulary + UI

`rules_documentation.py`, the frontend condition builder, and the GA's condition gene
collection, so the optimizer can toggle regime gates like any other condition.

### Task 6.5: Validate on equity experts (2011+), NOT on options

Re-run an equity grid (S1–S7 / FactorRanker) with regime conditions available and compare
against the current champions. **Do not enable regime genes on the 2024–2026 options window** —
the regime variable barely varies there, so the GA would tune it to noise.

### Task 6.6: Expose the live regime in the UI

The user's explicit requirement is that the current regime be determinable live. Surface it on
the dashboard (benchmark, trend regime + distance from SMA, vol regime + rank, as-of date) so
it is inspectable rather than only implicit in rule evaluation.

---

## Sequencing summary

```
Phase 1 (quotes)  ──> Phase 2 (regimes) ──> re-run grids ──┐
                                                            ├──> decision-grade results
Phase 3 (intraday) ──> Phase 4 (0DTE) ─────────────────────┘
Phase 5 (Wheel)   ── independent, any time
Phase 6 (regime)  ── independent; 6.1 SHIPPED. Validate on EQUITY (2011+ data) now;
                     regime-gated OPTIONS work waits on Phase 2
```

**Do not relaunch the options grids for keeps until Phase 1 and Phase 2 land.** Phase 0 already
changed the cost model; running a long grid now optimizes against an uncalibrated assumption on
a single-regime window.

# Regime risk-scaling overlay — plan (2026-07-29)

**Status:** designed, NOT implemented. Deliberately deferred to the **2020-01-01 → 2026-06-30**
optimization pass, not the running 2023-2026 sen5min3 grid. See "Why not the current window".

**Goal:** let the GA discover whether scaling position risk by market regime helps, using the
existing `ba2_common.core.market_regime` classifier, with an on/off gene so it can be rejected.

---

## What the overlay is (and is NOT)

**IS:** a multiplier on `risk_per_trade_pct` applied when the benchmark is in a *stressed* regime.

**IS NOT:** a directional bull/bear entry gate. That was the original idea and the repo has
already refuted it — see `dc1c14a`, which corrected `c4f3c7a`'s claim:

> the documented value of THIS construction — a 200-day SMA cross on a single equity index — is
> volatility and drawdown reduction rather than return enhancement. Since 1951 SMA200 timing
> returns 7.11% at 10.1% vol (Sharpe 0.704) vs buy-and-hold 7.24% at 15.37% (Sharpe 0.471).

Lower return, much lower risk. So the honest use is **exposure management**, not signal
generation. If a genuinely directional regime is wanted later, `dc1c14a` names the path:
12-month time-series momentum across assets (Moskowitz/Ooi/Pedersen 2012), NOT an SMA cross on
one index.

## Genes

```python
"regime_overlay_enabled": {"optimize": True, "min": 0,   "max": 1,   "step": 1,    "type": "int"},
"regime_risk_scale":      {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
"regime_stop_scale":      {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25, "type": "float"},
```

Both multipliers apply while `market_regime.is_stressed()` is true, and are 1.0 (no-op) otherwise:

* `regime_risk_scale`  -> multiplies `risk_per_trade_pct` (position SIZE)
* `regime_stop_scale`  -> multiplies the stop-loss DISTANCE

### Why a stop-width gene is the best-matched use of this classifier

A fixed percentage stop is hit by noise when volatility rises, so a position gets closed early on
a move that is not informative. Widening the stop in a stressed regime is the direct remedy.

Crucially this is the use the classifier's OWN evidence supports. Its measured strength is
separating **forward realized volatility** — from the module docstring, close-vs-SMA200 gives
10.4 pct points of separation on the full sample and is the most consistent of the four signals
tested. Using a volatility signal to set a volatility-sensitive parameter is well-matched, unlike
using it to forecast direction (which `dc1c14a` refuted).

**Why 0.5–2.0 and not 0.25–1.0 (the first proposal).** A de-risk-only range presupposes that
stress means "take less". Two-sided lets the GA express the opposite — scale UP into stress,
i.e. buy the dip — so the gene tests the hypothesis instead of encoding it. **1.0 is inside the
range and is an exact no-op**, which doubles as a consistency check: an `enabled=1, scale=1.0`
trial must score identically to `enabled=0`. If it does not, the overlay is leaking somewhere it
should not.

## Why NOT the current (2023-2026) window

`dc1c14a` counted the independent below-SMA200 episodes: **2015×2, 2018, 2020, 2022×2, 2025,
2026 — eight in fifteen years.** The sen5min3 window contains **two** (2025, 2026).

A 480-trial GA optimizing a regime gate against two independent events fits those two events.
Worse, it would repeat the exact statistical error `dc1c14a` was written to record: treating
overlapping daily windows as independent observations, inflating n=8 into an apparent n=568.
The GA's search power makes that failure mode stronger, not weaker.

2020-2026 contains **~5** episodes (2020 COVID, 2022×2, 2025, 2026). Still small — this is a
low-power test whatever we do, and the result should be read as "not refuted" rather than
"validated". But it is 2.5× the evidence for the same compute.

## Fitness: a calmar-based COMPOSITE (`consistent_calmar`), not raw calmar, not CAR

`consistent_annual_return` already composites the right things:

```
fitness = base x dd_guard x consistency x trade_gate
   base        = annualized_return (or adjusted_annualized_return under a profit cap), %/yr
   dd_guard    = 1.0 if dd <= _CAR_DD_SOFT_CAP (20%)  else 20/dd
   consistency = clamp(worst_year / mean_year, 0.25, 1.0)     <- year variance
   trade_gate  = clamp(avg_trades_per_year / 30, 0.0, 1.0)    <- trade count
```

**WHY CAR CANNOT SCORE THIS OVERLAY.** Look at `dd_guard`. Above 20% drawdown,
`base x dd_guard` = `20 x (return / dd)` — proportional to calmar. **Below 20% it is pinned at
1.0**, so CAR is completely drawdown-INSENSITIVE in that region. A regime overlay whose entire
documented benefit is cutting drawdown therefore earns nothing for it unless the strategy is
already losing >20%, while still paying the return cost. CAR would reject the overlay by
construction — and that rejection would carry no information about regime.

**Proposed metric, mirroring `_consistent_annual_return` with the base swapped:**

```
consistent_calmar = calmar_ratio x consistency x trade_gate
```

* `calmar_ratio` is already present in `_convert_bt_results` (confirmed in the module header),
  and `calmar`/`calmar_ratio` are registered aliases.
* `dd_guard` is DROPPED — calmar already contains drawdown; keeping both double-counts it.
* `consistency` and `trade_gate` are reused unchanged, so the "~30 trades/yr, every year" goal
  still holds.

**Three things to get right when implementing:**

1. **Floor the drawdown.** `calmar = return / dd` explodes as `dd -> 0`, so a freak 2-trade
   genome with a 0.1% drawdown outranks everything real. Clamp `dd` to a floor (>=1%) before
   dividing. `trade_gate` mitigates but does not eliminate this.
2. **Scale is NOT comparable to CAR.** Calmar is a ratio (~0-5); CAR is %/yr (~5-30). Fitness
   numbers from a `consistent_calmar` run cannot be compared against sen5min3's CAR numbers —
   only rankings within a run are meaningful. Say so on any report that shows both.
3. **Report raw annualized return and max_drawdown alongside**, or the return/risk trade-off the
   overlay makes is invisible inside a single number.

## Scope: one strategy, not all seven

Add the two genes to whichever strategy wins sen5min3, not S1-S7. Two extra genes across seven
strategies multiplies the search space for no additional information about the overlay itself.

An **S8** ("dedicated regime strategy") is defensible later, but the honest version is an
exposure-management strategy, not a bull/bear one.

## TWO WAYS `regime_stop_scale` COULD BE AN INERT GENE — check both FIRST

This is the ATR failure mode: `use_atr_stop`/`atr_multiplier`/`atr_period` were searched by the
GA for months while the code path silently did nothing, so the GA "learned" ATR was useless. A
widened stop has two plausible routes to the same fate, and both must be verified with a test
BEFORE the gene is put in a grid.

**1. The tighter-wins clamp may discard the widening.** There is deliberate precedence between a
ruleset stop-loss and the TradeRiskManagement safeguard where **the tighter of the two wins**
(see the entry-bracket work, "Tighter-wins precedence between ruleset SL and RM safeguard"). A
regime that WIDENS the stop is, by definition, proposing the looser value — so it may be
overridden and never reach the order. `min_stop_loss_pct` is a second floor with the same effect.
Required test: with `regime_stop_scale=2.0` in a stressed regime, assert the stop distance on the
SUBMITTED order actually doubled — not merely that the multiplier was computed.

**2. It is partly redundant with ATR stops.** When `use_atr_stop=1` the stop ALREADY scales with
realized volatility per symbol, which is most of what a regime multiplier provides — only
market-wide rather than per-symbol. So the gene's value is concentrated in genomes with
`use_atr_stop=0`, and a GA searching both together may produce a muddled answer.

Analyse `regime_stop_scale` **split by `use_atr_stop`**, not pooled. If it only helps when ATR
stops are off, the honest conclusion is "ATR already does this", not "regime stops work".

## Implementation notes

* `market_regime` lives in `ba2_common.core` and is already live-computable, so live and backtest
  share one implementation. API: `classify_trend_regime`, `classify_volatility_regime`,
  `is_risk_on(closes)`, `is_stressed(closes)`.
* It takes **benchmark daily closes** (SPY by default) — SPY must be in the OHLCV prewarm for the
  whole window, warmup included (SMA200 needs 200 sessions BEFORE the first trading bar).
* **Compute once per bar, market-wide, and cache it.** It does not vary per symbol. Computing it
  inside a per-symbol loop is exactly the mistake that made still-held O(feed × symbols × bars)
  on 2026-07-28.
* The classifier is causal (uses only closes up to the bar) — keep it that way; feeding it the
  full series would be lookahead.

## Data readiness for the 2020-2026 pass — VERIFIED 2026-07-29

All checks below were run against the real cache, not assumed.

| what | needed | have | verdict |
|---|---|---|---|
| SPY daily (regime input) | 524 sessions before start (`_VOL_WINDOW` 20 + `_VOL_RANK_LOOKBACK` 504); trend needs 200 | `SPY_1d.parquet` 2011-06-22 -> 2026-06-30, **2146 sessions before 2020-01-01** | OK, 4x margin |
| universe 5min | 2020-01-01 -> 2026-06-30 | **498/498 cached**, 2020-01-02 -> 2026-06-30 | OK |
| universe 1d | warmup + daily indicators | **498/498 cached**; most from 2011-06-22, some later (GBIL 2020-01-02, AMCR 2019-06-11) | OK, see caveat |
| screener metric_store | ym 2020-01+ | still **ym=2022-01..2026-06** | **NOT READY** — the known blocker |

**THE TRAP: 5min data starts 2020-01-02, which IS the pass start date — so a 2020-01-01 start has
ZERO intraday warmup.** `derive_warmup_days` returns >=60 calendar days, and the engine will look
for 5min bars before 2020-01-02 and find none. Whether that raises or silently starts with cold
indicators has NOT been established, and "silently degraded" is the more likely of the two given
how the rest of this codebase behaves.

Mitigations, in order of preference:
1. **Start the pass at 2020-04-01** (a full quarter of 5min warmup available) and accept the
   slightly shorter window — it still contains the COVID crash episode, which is the point.
2. Confirm by experiment that warmup falls back to daily bars (1d goes back to 2011 for most
   symbols) and that indicators are genuinely warm at the first trading bar.

Do NOT simply start at 2020-01-01 and assume it worked. A handful of symbols also lack deep daily
history (GBIL from 2020-01-02), so even the daily fallback is not universal.

## Acceptance

1. `enabled=1, risk_scale=1.0, stop_scale=1.0` scores **identically** to `enabled=0` — the no-op
   check. A difference means the overlay leaks somewhere it should not.
2. **`regime_stop_scale=2.0` measurably doubles the stop distance on the SUBMITTED order** in a
   stressed regime — not just in the computed value. This is the anti-inert-gene test; see the
   two clamp/redundancy routes above.
3. Overlay changes SIZE and STOP DISTANCE only — entry timing and the set of symbols entered must
   be unchanged versus the same genome with the overlay off. (Stop width does change EXIT timing;
   that is the point. Entries must not move.)
4. Regime is evaluated once per bar, market-wide (assert call count in a test, as with the
   holdings index).
5. Report `regime_stop_scale` results SPLIT BY `use_atr_stop`, never pooled.
6. Read any winner against the ~5-episode caveat before believing it. "Not refuted" is the
   strongest available conclusion from this window; "validated" is not on the table.

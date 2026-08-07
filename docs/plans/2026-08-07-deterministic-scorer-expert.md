# Plan: `DeterministicScorer` — LLM-free multi-section scoring expert

**Date:** 2026-08-07 · **Author:** bababot (research memo: `workspace/ba2/research-deterministic-scoring.md`)
**Status:** v1 IMPLEMENTED + post-review corrections applied (2026-08-07)

## Post-review amendments (2026-08-07)

A review of the shipped v1 found 13 defects; all are fixed, each locked by a test in
`packages/experts/tests/test_deterministic_scorer_regressions.py`. Where a fix required a
design call rather than a mechanical correction, the decision is recorded here because it
DEVIATES from the plan text below:

| # | Decision | Why it deviates |
|---|---|---|
| §2 veto | `veto_cap` default **0.0**, not −0.5 | −0.5 sits below `−theta_sell`, so a veto did not block an entry — it opened a SHORT, including on a data-integrity flag. 0.0 blocks; go negative to opt back in. |
| §2 macro | Hard risk-off now needs **≥2 macro inputs** | With only the index trend available (the hermetic-backtest case) the composite is exactly ±1, so `hard_riskoff` fired on one uncorroborated binary reading and pinned the whole book to HOLD below SMA200. |
| §3 Altman | Separate `z_veto_adjusted` (**1.1**) for Z″ | One threshold across two scales: 1.8 is the *original* Z cutoff; Z″ breaks at 1.1, so non-manufacturers were mass-vetoed. |
| §3 data policy | `fundamentals_max_age_days` default **450**, not 180 | Statements are fetched ANNUALLY; 180d blanks the section 8 months out of 12. |
| §3 macro | `yc_scale` default **0.5**, not 0.005 | FRED serves 10y-3m in percent, so the decimal scale saturated tanh on every observation. |
| §3 decision | `min_score_delta` **removed** from settings | It only acts when the caller threads `prev_signal`, which the stateless per-bar `_process` does not — a declared GA gene that cannot move anything is worse than a missing feature. `exit_hysteresis` is supported in `schmitt_trigger` on the same terms and likewise unexposed. |
| §3 combine | `skip_on_missing_section` **implemented** (default `skip`) | Was specified but never built; a missing section scored 0.0 with full weight, silently cutting every score ~37%. |

Still open (unchanged from §8): breadth needs screener integration; ALFRED vintage macro;
prev-state plumbing before hysteresis can be re-exposed as a tunable.

## Goal

A market expert that reproduces the TradingAgents-style verdict (BUY/SELL/HOLD +
target profit) with **pure local math, zero LLM calls**. Three mandatory sections
(TECHNICAL / FUNDAMENTAL / MACRO) + one optional section (ANALYST, FMP/FinnHub
ratings), each producing a score in [-1, +1]. A tunable formula combines them into
a final score that drives the decision and the profit target. **Every section
weight, sub-weight, period, threshold and multiplier is an `ExpertSetting`** so it
is hand-tunable in the live UI and GA-optimizable by the testplatform grid.

Non-goals (v1): options, shorting (uses platform `enable_sell`), cross-sectional
rank normalization across the whole screener universe (single-symbol tanh mode;
see §7).

## 1. Architecture

New clean expert package (single source of truth per Phase-6 rules):

```
packages/experts/ba2_experts/DeterministicScorer/
├── __init__.py      # DeterministicScorer(MarketExpertInterface): settings, _gather/_process/run_analysis
├── data.py          # provider-backed fetchers, symbol-keyed TTLCache + disk cache, patchable (FactorRanker pattern)
├── technical.py     # PURE calculators: momentum12-1, vol-adj momentum, d200, RSI14inv, Donchian, ADX gate, ATR14
├── fundamental.py   # PURE: Piotroski F, Altman Z, quality/value/growth sub-scores
├── analyst.py       # PURE: dated-grades revision momentum (no lookahead, as_of reconstruction)
├── macro.py         # PURE: regime composite from FRED series + VIX + index trend
└── combine.py       # PURE: normalization (winsorize/tanh), weighted formula, Schmitt trigger, ATR targets
```

Registration (4 places, following existing experts):
1. `ba2_experts/__init__.py` → import + `experts` list.
2. `ba2_trade_platform/modules/experts/__init__.py` → live registry list + alias shim module
   `ba2_trade_platform/modules/experts/DeterministicScorer.py` (same pattern as FMPRating.py shim).
3. testplatform `seam_wiring` / expert resolution picks experts up from `ba2_experts.experts`
   (verify `data_build_handler.py` + `daily_backtest_handler.py` lists).
4. Prewarm hook in `testplatform/ba2test_launcher.py` `--prewarm` for the grid.

## 2. Decision pipeline (all pure, all deterministic)

```
per symbol, at as_of:
  T = technical_score(ohlcv, settings.tech)            # [-1,1]
  F = fundamental_score(statements, overview, settings) # [-1,1]; Altman Z<z_veto -> hard veto flag
  A = analyst_score(grades_history, as_of, settings)    # [-1,1]; weight default 0 (disabled)
  R = macro_regime(macro_series, index_ohlcv, settings) # [-1,1]

  raw   = wT*T + wF*F + wA*A                      # weights normalized to sum 1
  score = tanh(raw / k_compress)                  # k_compress default 0.6
  if veto: score = min(score, veto_cap)           # Altman distress / data-integrity vetoes
  M     = exposure_multiplier(R)                  # M = clip(m_floor + (1-m_floor)*(R+1)/2, 0, 1)
  final = score * M

  BUY   if final >  +theta_buy
  SELL  if final <  -theta_sell   (platform enable_sell governs)
  else  HOLD
  confidence   = 100 * |final| (floor 5 when actionable)
  target_price = price + k_target * ATR14  (BUY)  -> expected_profit_percent derived
  stop hint    = price - k_stop * ATR14    (passed in raw_outputs for risk manager)
```

Hysteresis / churn control is delegated to the platform where it already exists
(min holding via execution schedules + rulesets); `min_score_delta` (re-enter only
if |Δfinal| > delta) is implemented in-expert for the single-symbol flow.

### Section formulas (defaults — all tunable)

**TECHNICAL** (time-series, tanh-normalized, single-symbol):
- `mom = P[t-21]/P[t-252] - 1` (12-1 construction, skip last month)
- `mom_vol = mom / sigma_daily(63d)` primary; sub-weight w_mom
- `d200 = P/SMA200 - 1` → tanh(d200/scale_d200)
- `rsi14` → tanh((50 - RSI)/15) mean-reversion leg, weight w_rsi (routed down when ADX14 ≥ adx_gate)
- Donchian-20 breakout state → {-1,0,+1}, weight w_don
- T = Σ wᵢ·sᵢ / Σ wᵢ

**FUNDAMENTAL**:
- Piotroski F (9 binary, latest vs prior fiscal year, point-in-time via filing date): `(F-4.5)/4.5`
- Quality: GP/A and ROE, tanh vs own 4y history (single-symbol) or sector rank when universe available
- Value: EV/EBIT and FCF yield vs history, inverted tanh
- Growth: revenue & EPS acceleration (QoQ vs trailing 4Q average), tanh(accel/5%)
- F = Σ wᵢ·sᵢ / Σ wᵢ ; **Altman Z < 1.8 ⇒ veto** (Z″ variant for non-manufacturers; financials: skip Z, use F only)

**ANALYST** (optional, default weight 0):
- Revision momentum over trailing window (default 90d, dated, as_of-filtered):
  `rev = (upgrades - downgrades) / total` → weighted by recency decay; plus
  analyst price-target consensus drift vs price when available.
- Data: FMP `stable/grades-historical` via `FMPRating.fetch_grades_historical_cached`
  (re-use, do not duplicate) + FinnHub ratings fetcher if enabled.

**MACRO** (regime composite, monthly-frequency inputs):
- Index trend: SPY vs SMA200 → ±1 (w 0.30)
- Breadth proxy: % of expert universe above SMA200 when universe known, else skip (w renormalized)
- VIX: clip((30-VIX)/15, 0, 1) mapped to [-1,1] (w 0.15)
- HY credit OAS (FRED BAMLH0A0HYM2) z-score inverted (w 0.10)
- Yield curve 10y-3m 3mo-avg: tanh(spread/0.5%) (w 0.10)
- ISM/NAPM: clip((PMI-50)/5, -1, 1) (w 0.10)
- Sahm rule (UNRATE): soft −clip(sig/0.8,0,1) (w 0.05)
- R = Σ wᵢ·sᵢ / Σ wᵢ (renormalize when inputs missing)

## 3. Settings (all `ExpertSetting`, GA-tunable)

Section weights: `w_technical` (0.5), `w_fundamental` (0.3), `w_analyst` (0.0),
`macro_mode` ∈ {multiply, gate, off} (multiply), `m_floor` (0.25), `k_compress` (0.6),
`theta_buy` (0.5), `theta_sell` (0.2), `min_score_delta` (0.0), `veto_z_threshold` (1.8).

Technical: `mom_fast_skip_days` (21), `mom_lookback_days` (252), `vol_window` (63),
`sma_trend_period` (200), `rsi_period` (14), `donchian_period` (20), `adx_period` (14),
`adx_gate` (25), sub-weights `tw_mom`/`tw_d200`/`tw_rsi`/`tw_don`, scale params.

Fundamental: sub-weights `fw_piotroski`/`fw_quality`/`fw_value`/`fw_growth`,
`fscore_min` (optional disqualify), `altman_variant` (auto/original/adjusted).

Analyst: `analyst_window_days` (90), `analyst_recency_halflife_days` (30).

Macro: per-input weights `mw_*`, `vix_calm`/`vix_stress` (15/30), `yc_scale` (0.5).

Targets/exits: `atr_period` (14), `k_stop` (2.5), `k_target` (4.5),
`target_from_score` bool (scale k_target by |final| up to ±30%).

Data policy: `min_history_days` (260), `fundamentals_max_age_days` (180),
`skip_on_missing_section` ∈ {neutral, skip} (neutral: missing section counts 0 and
renormalizes weights).

## 4. Caching (must match existing grid behavior)

- `_gather` reads ONLY through `ProviderBundle` → backtest automatically uses the
  parquet/SQLite as_of cache (hermetic), live uses provider TTL caches.
- OHLCV for indicators: `bundle.ohlcv().get_ohlcv_data(end_date=as_of)` (causal,
  same as PandasIndicatorCalc) — ONE fetch per symbol per run, slice locally per bar.
- Dated FMP histories (statements w/ filing dates, grades): process-wide
  `TTLCache` keyed by **symbol** + `fmp_history_disk_cached` (GA workers share),
  date filtering happens in pure calculators at as_of (FMPRating pattern).
- FRED series: tiny monthly payloads; `fmp_common`-style TTLCache keyed by series
  id + optional disk cache file. In hermetic backtests without FRED cache → macro
  section degrades to index-trend-only (documented, logged once).
- `data.py` exposes standalone cached fetchers (no expert instance) for the
  `--prewarm` hook.

## 5. No-lookahead rules (hard requirements)

1. Every dated input filtered by `publication/filing date <= as_of` (not period end).
2. Statement lag sensitivity: settings expose `fundamentals_lag_days` simulation offset for tests.
3. Indicators computed on OHLCV sliced to `<= as_of`.
4. Macro series use observation date <= as_of (revisions ignored = use vintage-free current series, documented limitation).
5. Hermetic guard: any network call in backtest path raises `FMPHermeticViolation`.

## 6. Recommendation contract

`Recommendation(signal, confidence, current_price, details, expected_profit_percent,
target_price, raw_outputs)` where `raw_outputs` carries the full score tree
(section scores, sub-scores, inputs, vetoes, regime M) → AnalysisOutput rows for
auditability (UI shows why). `details` = one-line human summary. SKIP with
skip_reason when OHLCV history < min_history_days.

## 7. Testing assumptions (from research memo §5.5)

Unit tests (packages/experts/tests/, pure calculators): golden values for F-score,
Altman Z, momentum 12-1, RSI/ATR vs stockstats, Schmitt-trigger edges, tanh bounds,
no-lookahead filtering on synthetic dated rows. Grid experiments to run after v1
lands: with/without analyst section; macro multiply vs gate vs off; vol-adj vs raw
momentum; θ sweeps with trial counts + Deflated Sharpe reported per memo §5.2.

## 8. Open questions

- Universe breadth input needs screener integration → v2 (FactorRanker-style batch mode).
- FRED vintage data (ALFRED) for true point-in-time macro — v2 if macro section proves useful.
- Kelly sizing: not in v1 (platform risk manager sizes); score→confidence only.

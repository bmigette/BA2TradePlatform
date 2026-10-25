# ML forecast expert: design

*Status 2026-10-25: design approved by the owner in a brainstorm. Nothing is built yet. The idea was parked on
2026-08-15 and became active on 2026-10-25.*

`MLForecast` is one generic MarketExpert. It turns any forecasting model's output into a BUY or SELL signal with a
confidence from 1 to 100, and it runs identically in a backtest, in the GA and in live trading. Models plug in through
adapters, and a GA optimises the mapping from forecast to signal. The forecast itself is precomputed once, not per
genome.

**Models in scope:**

| Model | Source | Output | Weights licence |
|---|---|---|---|
| TimesFM 3.0 (milestone 1) | `google/timesfm-3.0-pytorch`, `pip install timesfm[torch]`, `timesfm3.TimesFM3Evaluator.predict_batch` | 9 deciles; covariates (past-only and past-future); context up to 15,360 | **non-commercial, non-production** (code Apache-2.0) |
| Kronos (milestone 2) | https://github.com/shiyu-coder/Kronos (mini 4M / small 25M / base 102M) | sampled OHLCV paths | MIT |
| Our test-platform models | `trained_models` table: tsai classifiers, Darts regressors | class probabilities, or point and quantile forecasts | ours |
| Chronos | already in `testplatform/.../chronos_service.py` | sampled paths and quantiles | Apache-2.0 |

**Licence (owner decision 2026-10-25, "3.0 everywhere, my call"):**
- TimesFM 3.0 runs in backtests and in live trading.
- Its model card records the licence, and a deploy prints a warning. Nothing blocks it, and the question is not
  raised again.

## 1. Architecture

### 1.1 Pure code (no torch)

`ba2_common/core/ml_forecast/` holds:
- **Model card schema:**
  - `model_id`, `family` and `version`;
  - inputs (fields, timeframe, context length) and covariates (past-only and past-future);
  - horizons, scaler and output type (`deciles`, `paths` or `class_probs`);
  - pretraining data cutoff and licence.
- **Forecast row format:** the store schema (§1.3).
- **Store reader:** point in time, through `scan_cutoff_date`.
- **Mapping:** a pure function that takes a forecast row, the GA genes and the decision price, and returns the signal,
  confidence, expected profit, target price, TP and SL (§2).

`ba2_experts/MLForecast/` holds the expert:
- `_gather(providers, as_of)` reads the forecast row (from the store in a backtest, from the model server live) and
  the decision price.
- `_process` calls the mapping and returns a `Recommendation`.
- It analyses held symbols at manage time too. A bearish fresh forecast flips the rating from BUY to SELL, and the
  existing rules for an opposite recommendation close the position. There is no trade loop in the expert:
  experts express risk as order parameters.

### 1.2 Inference runtime (heavy)

Inference runs in a separate venv, `ba2-ml`, holding torch, `timesfm[torch]`, Kronos and the testplatform model
loaders. The trade app stays on `torch==2.6.0+cpu`. It has three parts:
- `tools/ml_forecast/build_store.py` backfills the store per model key, batched over the symbols of each session.
- `tools/ml_forecast/model_server.py` is a localhost service for live trading:
  - `POST /forecast {model_key, symbols, as_of}` returns rows in the store format.
  - The expert calls it through a thin client in `ba2_common`.
  - A refusal or timeout is an error, never a default value.
- **History basis (live rule).** The store records its history source in `meta.json` build params
  (`history_source` = `alpaca_build`, `build_manifest_sha256` of `AlpacaHistoryOHLCVProvider/_meta/build_manifest.json`,
  `context_function_version`). The live expert builds its forecast context with the SAME function
  (`ba2_common.core.ml_forecast.context.context_for`) from the SAME history source the store was built from, NOT from
  the account's `ohlcv_history_source`. It refuses if the sources differ, or if the live context's last close differs
  from the store's recorded close of session S beyond a tolerance (the store row format has no close column yet:
  adding one is part of milestone 3/4). The account's decision price is used only to scale the TP and SL from the
  forecast fractions; the small basis difference between the account price and the history close is accepted.
- Parity: each live day's rows are appended to the store, and a nightly check recomputes them with `build_store.py`
  and compares. Sampling models (Kronos, Chronos) use a fixed seed per (model key, symbol, session), so their rows are
  deterministic.

### 1.3 Forecast store

- **Layout:** `<cache>/ml_forecast/<model_key>/<SYMBOL>.parquet`.
- **`model_key`:** a hash of the model card, context length, covariate set and horizon list. The GA's `model` gene
  chooses among the model keys that were built.
- **One row per (symbol, session S):**
  - It is forecast from the bars of sessions up to and including S, so it is visible per the Visibility rule below (intraday: after session S finished; daily clock: on D = S).
  - Columns: the 9 deciles of the H-session return for each horizon (5, 10, 20).
  - For path models, also P(TP before SL) over a small grid of TP and SL percentages, computed from the sampled OHLC
    paths. Whole paths are not stored.
- **Visibility.** Row S is forecast from the close of session S. On the intraday clock it is visible only after
  session S has finished (`metric_store.visible_scan_date` / `scan_cutoff_date(..., intraday=True)`): a 10:00 decision
  on day D sees the row of the previous session, never D's. On the daily clock a decision on D sees row D, by the
  repo's D-close convention. Tested in `tests/test_ml_forecast_visibility.py`.
- **Coverage:** every universe symbol on every session in the window. That covers held symbols on manage days.
- **Covariates are point in time.** The FMP historical earnings calendar gives actual dates, not the dates as known
  in advance, so the earnings-date covariate is flagged until checked.

### 1.4 Our own models

An export step in the test platform writes a bundle from a `trained_models` row: model card, weights, scaler and a
feature-builder spec.
- At each decision the features are rebuilt from BA2 providers, point in time. The cached training CSV is not
  reused.
- The step fixes the Darts reload (today `load_model` maps tft, tcn, transformer and gru to the wrong class).
- It records the feature list, which saved models lack today.

## 2. Signal mapping (GA genes)

| Gene | Range | Meaning |
|---|---|---|
| `model` | choice of built model keys | model × context × covariate set |
| `horizon` | 5, 10, 20 sessions | forecast horizon traded; also the maximum hold |
| `min_edge_pct` | 0.5 .. 5 | minimum median (q50) expected return |
| `min_prob` | 55 .. 80 | P(right direction) for decile models; P(TP before SL) for path models |
| `max_uncertainty` | 1 .. 6 ATR | maximum q10-q90 width |
| `tp_quantile` / `sl_quantile` | q60 .. q90 / q10 .. q40 | entry take-profit and stop-loss, set as order parameters |
| `exit_flip_prob` | 50 .. 70 | opposite-side probability that flips a held position to SELL |
| `enable_sell` | 0, 1 | short-side signals |

- **Confidence:** the probability used, scaled to 1-100. **Expected profit:** q50 at the horizon. **Target price:**
  the TP decile's price.
- **Exits:** forecast TP and SL, plus the existing S1-S7 rulesets and the ATR stop, with the GA choosing. The flip to
  SELL reuses the existing exit mechanism.
- **Confidence ceiling:** add an `_EXPERT_CONFIDENCE_CEILING` entry, set from the pilot's measured maximum.
  Otherwise genomes gate at confidence levels the expert never reaches.
- **Registration:** four places:
  - the `ba2_experts/__init__.py` list;
  - `_SUPPORTED_EXPERTS`;
  - `_EXPERT_OPT` (the genes above);
  - the confidence ceiling.

## 3. Look-ahead and evidence

- **Pretraining contamination:** a foundation model may have seen the backtest years.
  - Every model card records the pretraining cutoff.
  - Only dates after it count as clean evidence.
  - Kronos (paper mid-2025, more than 45 exchanges, no cutoff published): 2020-2024 is almost certainly in its
    training data.
  - TimesFM 3.0 lists GiftEval pretrain, Wikipedia pageviews (to 2023-11), Google Trends (to 2022) and synthetic data,
    with no stock data listed. Check the GiftEval contents.
- **Our models:** the scaler and the training window must end before the test window.
- The 2026 fold and live forward testing are the main evidence.

## 4. Pilot, then goal2027

- **Pilot:** a GA on the screener **large** band, trained 2020-2025 with 2026 out of sample, on TimesFM 3.0 store keys
  (two context lengths, covariates none or volume plus SPY).
- It measures:
  - store build cost (symbol-sessions per hour on CPU, and whether a GPU is needed);
  - GA trial cost;
  - the edge.
- The expert joins the goal2027 grid ([2026-10-25-goal2027-grid-design.md](2026-10-25-goal2027-grid-design.md)) as
  extra cells only if the pilot shows an edge.

## 5. Milestones

1. **Contract and store:** model card schema, row format, store reader, mapping and its tests (pure code, no torch).
2. **TimesFM 3.0 adapter:**
   - the `ba2-ml` venv;
   - `build_store.py` over the large-band universe, 2020-2026;
   - a measured CPU cost.
3. **Expert and GA:**
   - `MLForecast` with the registrations and genes;
   - backtests read from the store;
   - the pilot GA.
4. **Live:**
   - the model server and client;
   - store append and the nightly parity check;
   - deploy with the licence warning.
5. **Kronos adapter:** path sampling with fixed seeds, P(TP before SL) columns.
6. **Test-platform model export:** the bundle, feature builder and Darts reload fix.
7. **Phase 2:** the forecast store as conditions (`ml_forecast_edge`, `ml_prob_up`) that any expert's ruleset can use.

## 6. Owner decisions (2026-10-25)

| Topic | Decision |
|---|---|
| Shape | one generic expert; adapters + GA mapping + precomputed store |
| First model | TimesFM 3.0 (not 2.5) |
| TimesFM 3.0 licence | runs everywhere; the owner's call; warn, never block |
| Second model | Kronos |
| Exits | forecast TP/SL + S1-S7 rulesets (GA picks); forecast flips the rating to SELL to reuse existing exits |
| Universe | screener bands; own pilot first; joins goal2027 only with an edge |

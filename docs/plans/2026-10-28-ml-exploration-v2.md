# ML Exploration v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the data, target, model, evaluation and grid infrastructure of
`docs/plans/2026-10-27-ml-exploration-v2-design.md`, then run the stage-1 factor grid on remote150.

**Architecture:**
- **Feature store:** one point-in-time daily store per (symbol, session), with feature groups as column prefixes,
  plus the existing intraday panel (`ml_panel.py`) extended to more symbols and history.
- **Grid runner:** a new task type (`ml_grid`) runs a fixed list of configurations × seeds through a walk-forward
  and scores out-of-sample predictions with track metrics and shuffle controls. It writes resumable JSONL plus a
  factor-effect report. Models sit behind one small adapter interface (tabular / tsai / Darts).
- **Stage 2:** reuses the forecast store, the MLForecast expert and the walk-forward tool, plus a new ranking mode
  and a rotation strategy.

**Tech Stack:** Python 3.12, pandas / numpy / pyarrow, LightGBM / XGBoost / CatBoost, scikit-learn (linear
baselines), tsai + torch (CUDA), Darts, pytest. Code lives in `testplatform/backend/app/services/`; tests in
`testplatform/backend/tests/`.

**Conventions (from CLAUDE.md and earlier incidents):**
- No `.get()` defaults on configuration.
- Refuse loudly on missing data.
- Every feature group has a cut test.
- Cached data only.
- Use the Alpaca-built prices only (never FMP bars).
- FRED values are read with `ba2_providers.macro.fred_series.get_series_as_of`, which applies `realtime_start`.
- Screener universes are read via `metric_store.visible_scan_date`.
- Locked symbols never enter training or selection.
- Run tests with the mlga venv from `testplatform/backend`:
  `C:\Users\basti\ba2-venvs\mlga\Scripts\python.exe -m pytest tests/<file> -q -p no:cacheprovider`.
- Commit after every task, ending the message with the two attribution lines used on this branch.
- No version bumps until the end (Task 9.2).

---

## Phase 0: holdout lock

### Task 0.1: Locked symbol lists and guard

**Files:**
- Create: `testplatform/backend/app/services/ml_holdout.py`
- Create: `testplatform/ml_holdout_symbols.json` (generated once, then committed; never regenerated)
- Test: `testplatform/backend/tests/test_ml_holdout.py`

**Step 1: Write the failing test**

```python
import pytest
from app.services import ml_holdout as H

def test_locked_lists_are_fixed_and_disjoint_from_dev():
    lists = H.load()
    assert set(lists) == {"ndq30", "largecap"}
    nd = lists["ndq30"]
    assert len(nd["locked"]) == 10 and len(nd["dev"]) == 20 and not set(nd["locked"]) & set(nd["dev"])

def test_guard_refuses_a_locked_symbol():
    locked = H.load()["ndq30"]["locked"][0]
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked([locked, "AAPL"], purpose="training")
```

**Step 2: Run it.** Expect FAIL (no module).

**Step 3: Implement**

`ml_holdout.py`:
- `load()` reads the JSON (path resolved from the repo root).
- `assert_unlocked(symbols, purpose)` raises `LockedSymbolError` with the purpose and the offending symbols.
  It checks both lists.
- `make_lists(seed=20261028)` is the one-time generator:
  - NDQ30 from `run_screener_capband_matrix._PLACEHOLDER_UNIVERSE`; the locked third is
    `random.Random(seed).sample(sorted(ndq30), 10)`.
  - `largecap`: `{"rule": "third of each scan's universe by hash(symbol, seed) % 3 == 0"}`. Membership is
    deterministic per symbol, so a symbol entering the universe later is also covered.
- `is_locked_largecap(sym)` implements that rule.
- Generate the JSON once from a REPL, inspect it, and commit it.

**Step 4: Run tests.** Expect PASS.

**Step 5: Commit:** `feat(ml-v2): locked holdout symbol lists and guard`.

---

## Phase 1: daily point-in-time feature store

All groups land in `testplatform/backend/app/services/ml_features.py`, which holds:
- `build_features(symbols, first_day, last_day, groups, cache_root) -> DataFrame` (index `[symbol, session]`);
- a `GROUPS` registry (`name -> builder`);
- `coverage_report(df, groups) -> DataFrame` (share of non-NaN per group, per year).

The row of session S uses only data public before the decision at S's close (daily clock): bars up to and
including S, and statements, grades and FRED values visible at or before S.

### Task 1.1: Daily bars and the calendar

**Files:** create `ml_features.py`; test `tests/test_ml_features.py`. Reuse the synthetic-cache fixtures from
`tests/test_ml_panel.py`, extended to write `_1d.parquet` files.

1. **Test:**
   - `load_daily(symbols, first, last, cache_root)` returns OHLCV aligned on the NYSE calendar
     (`ml_panel.regular_sessions`).
   - A symbol that is missing raises `PanelDataError`.
   - A symbol that lists after `first` starts with NaN rows and has `listed=False` before its first bar.
2. Implement it, reading `AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet`, column `Date`, normalised to a date.
3. Run the tests; commit.

### Task 1.2: Group T (daily technical) and the MTF group

1. **Test (the cut test):**
   - Build T+MTF over a synthetic cache, then build again with every daily file truncated at day D.
   - Every common row must be identical.
   - Weekly MTF values on day D must equal the values computed from weeks that ended before D.
2. **Implement T (normalised):**
   - returns over 1/5/20/60 days, realised volatility over 20/60;
   - ATR%, RSI, MACD histogram / ATR, Bollinger %B and width;
   - distance to SMA 20/50/200 in ATR;
   - ADX, SAR distance / ATR, Donchian position (20/55), OBV 20-day slope z;
   - pivot distance / ATR, volume z.
   - Port the formulas from `app/indicators.py` (reuse `TechnicalIndicators` where it is causal) and normalise
     every price-level output to a ratio or an ATR multiple.
3. **Implement MTF:**
   - resample to weekly bars ending Friday (or the last session of the week);
   - compute RSI, distance to the 10/40-week SMA and the 4/13-week returns;
   - join a week's values only to sessions AFTER that week's last session (`shift(1)` on the weekly index, then
     forward-fill).
4. Run the tests; commit.

### Task 1.3: Group M (market, sector, cross-asset, breadth)

1. **Test:**
   - Relative strength vs SPY equals own 20-day return minus SPY's.
   - Breadth on day S uses only closes up to S (cut test).
   - The sector ETF comes from a committed map (`SECTOR_ETF = {"Technology": "XLK", ...}`), and the sector
     itself from the metric store (Task 1.4).
2. **Implement:**
   - returns of SPY, QQQ, IWM, TLT and GLD over 1/5/20 days;
   - own-sector-ETF relative strength over 5/20;
   - breadth: the share of the day's universe above its SMA50.
3. Run the tests; commit.

### Task 1.4: Group X (cross-sectional) and the point-in-time universe

**Files:** `ml_features.py` and a new `ml_universe.py`.

1. **Test:**
   - `universe_as_of(day, top_n, cache_root)` reads `metric_store_alpaca_20261022` through
     `ba2_providers.screener.metric_store.load_store`, `scan_dates` and `visible_scan_date(..., intraday=False)`.
   - A scan dated after the day is never used.
   - Locked large-cap symbols are excluded when `purpose="dev"`.
   - X features (cross-sectional rank and z of the T features, plus momentum_12_1, weinstein_stage,
     relative_volume and sector code) for day S use the scan visible at S.
2. **Implement:** keep the store in memory per process (`lru_cache` on the path). Rank within the day's universe
   (NDQ30 dev symbols in the development runs, the top-300 point-in-time universe at scale-up).
3. Run the tests; commit.

### Task 1.5: Group MAC (FRED first release)

1. **Test:**
   - Monkeypatch the FRED cache dir to a temp file with two vintages of one observation.
   - Before the second vintage's `realtime_start` the feature shows the first value; on and after it, the second.
   - A day before the first `realtime_start` is NaN.
2. **Implement:**
   - call `fred_series.get_series_as_of(sid, as_of)` once per session (or vectorise from its parsed series if
     `_parsed` exposes the realtime table; prefer the public function, and cache per (sid, session));
   - series: VIXCLS, DGS10, DGS3MO, T10Y3M, BAA10Y, CPIAUCSL (YoY), PAYEMS (3-month change), UNRATE;
   - features: level, 20-day change, 1-year z.
3. Run the tests; commit.

### Task 1.6: Group F (statements, extended)

1. **Test:**
   - Balance-sheet and cash-flow ratios become visible on the session after `acceptedDate` (the
     `ml_panel.fundamentals_visible_from` rule).
   - The existing earnings features are unchanged.
2. **Implement:**
   - reuse `ml_panel.fundamentals_table` for earnings and margins;
   - add from the annual balance sheet and cash flow:
     - debt/assets, current ratio;
     - FCF margin, FCF yield (FCF / market cap from the metric store);
     - buyback yield, SBC / revenue;
     - capex / revenue.
3. Run the tests; commit.

### Task 1.7: Group A (analysts)

1. **Test:**
   - Only dated rows are used (`analyst_grades.date`, `price_target.publishedDate`), each visible from the
     following session.
   - `a_target_gap` on day S is the mean of targets published in the 90 days before S, over the S close, minus 1.
   - `a_n_analysts` counts DISTINCT `analystCompany` values in that window.
   - The consensus file (`price_target_consensus*`, if any) is never read: assert by monkeypatching `open` and
     checking the opened paths.
2. **Implement:**
   - net upgrades over 30/90 days (upgrade +1, downgrade −1, by `action`);
   - target gap; target revision (mean `adjPriceTarget` change over 30 days);
   - `a_n_analysts`.
3. Run the tests; commit.

### Task 1.8: Group I (insider) with a coverage flag

1. **Test:**
   - Filings are visible the session after `filingDate`.
   - Net open-market buying (formType 4, transactionType starting `P-` minus `S-`, in $) over 90 days, divided by
     market cap.
   - `i_covered` is 0 on days before the first filing in the file.
2. **Implement** as above. Coverage is a per-row flag; the masking rule comes in Task 1.10.
3. Run the tests; commit.

### Task 1.9: Group S (market structure)

1. **Test:** read `market_conditions/ohlcv-v1` (`symbol`, `session` keyed). A value whose `*_status` is not 0
   becomes NaN. Session S's value is visible at S's close (it is computed from bars up to S; verify in the store's
   manifest, and if that can't be confirmed, shift by one session and say so in the docstring).
2. **Implement** `s_trend_slope_atr`, `s_adx`, `s_rv_ratio` (+ ta-structure-v1 fields if present for the
   symbols).
3. Run the tests; commit.

### Task 1.10: Coverage report and masking

1. **Test:**
   - `coverage_report` returns, per (group, year), the share of rows with all of that group's features non-NaN.
   - `apply_coverage_mask(df, report, min_share=0.7)` NaNs a group's columns in years under 70% and adds
     `<group>_missing`.
2. Implement; run the tests; commit.

### Task 1.11: Store build CLI

1. **Test:** `ensure_feature_store(dir, symbols, first, last, groups)` writes parquet plus meta (source
   fingerprints, coverage report), and rebuilds when the fingerprints change.
2. Implement it, and add `ba2-test ml-features --symbols-file --start --end --out` to the launcher (pattern:
   `_cmd_ml_search`).
3. Build the NDQ30 + timing-instrument store for 2016-01-01..2026-10-12 and save the coverage report under
   `C:\Users\basti\ba2-ml\mlv2\`.
4. Commit.

### Task 1.12: Intraday panel extension

1. **Test:** `ml_panel.build_panel` with 30 symbols from 2019-01-02 and context symbols
   `SPY QQQ IWM DIA SMH XL*` stays point-in-time (rerun the intraday-cut test with more symbols).
2. **Implement:** make `CONTEXT_SYMBOLS` a parameter. Add `sample_bars(panel, per_session=3, seed)`, which keeps
   the end-of-day bar and 2 random intraday bars per session for deep-model training.
3. Run the tests; commit.

---

## Phase 2: targets

### Task 2.1: Daily targets for both tracks

**Files:** `testplatform/backend/app/services/ml_targets.py` (extend); test `tests/test_ml_targets_v2.py`.

1. **Tests:**
   - `rel_return`: the H-session forward return (next open to close at S+H) minus the same-session universe mean.
   - `quintile`: top vs bottom quintile within the session (other rows NaN).
   - `beats_universe`: rel_return > 0.
   - `rank_groups`: the session id per row, for lambdarank.
   - Horizons 1/3/5/10/20.
   - `multistep`: a matrix with one column per horizon.
   - `volatility`: realised volatility over the next H sessions.
   - The existing timing targets work on daily rows.
   - The purge removes training rows whose target ends at or after the block start.
2. **Implement** on the daily store's `[symbol, session]` index:
   - entries at the next open: `open.shift(-1)`;
   - exits at the close at S+H: `close.shift(-H)`, per symbol.
3. Run the tests; commit.

---

## Phase 3: model families

### Task 3.1: Model adapter interface plus tabular families

**Files:** create `testplatform/backend/app/services/ml_models.py`; test `tests/test_ml_models.py`.

```python
class ModelAdapter:
    family: str                                   # "lgbm" | "xgb" | "catboost" | "linear" | "naive" | "tsai" | "darts"
    def fit(self, X_train, y_train, X_val, y_val, task: str, seed: int, groups_train=None, groups_val=None): ...
    def predict(self, X) -> np.ndarray:           # class probs (n, k), point (n,), or quantiles (n, 9)
        ...
```

1. **Tests:** on a synthetic dataset with a planted signal, each tabular family beats random (AUC > 0.6 or
   correlation > 0.2) for each task: binary, multiclass, regression, rank (lgbm only), quantile (lgbm, 9 alphas).
   Same seed gives the same predictions.
2. **Implement:**
   - LightGBM: `LGBMClassifier` / `LGBMRegressor` / `LGBMRanker` (lambdarank) / quantile models; early stopping
     on validation; `n_jobs` from the runner's CPU slots.
   - XGBoost and CatBoost: classifier and regressor only.
   - `linear`: `LogisticRegression(C=1, max_iter=2000)` or `Ridge(alpha=1)` on standardised features.
   - `naive`: the score is the 20-session momentum (no fit).
   - The robust scaler is fitted on train only.
3. Run the tests; commit.

### Task 3.2: tsai sequence family (more architectures)

1. **Test:** the adapter builds windows from the intraday panel (reuse `ml_search.fold_data`, `usable` and
   `train_classifier`) and trains `minirocket`, `patchtst`, `tcn` and `resnet` through
   `TSAIModelService.create_model` on a tiny synthetic panel for 1 epoch without error. It returns probabilities
   that sum to 1.
2. **Implement:** wrap the `ml_search` training loop in a `TsaiAdapter(ModelAdapter)`. Add the architectures to
   `_tsai_params`:
   - `minirocket`: no params; it uses `MiniRocket` from `tsai.models.all`, created with `c_in`, `c_out`,
     `seq_len`.
   - `patchtst`: d_model = width, n_heads 4.
   - `tcn`: layers [width] × 3.
   - `resnet`: no params.
3. Run the tests; commit.

### Task 3.3: Darts family (N-BEATS, TCN)

1. **Test:** the `DartsAdapter` with `nbeats` and `tcn` and a quantile likelihood predicts deciles on a tiny panel.
2. **Implement:** extend `ml_search._darts_model`; TFT stays available but off in the stage-1 configurations.
3. Run the tests; commit.

---

## Phase 4: evaluation and the grid runner

### Task 4.1: Walk-forward splitter and regime tags

**Files:** create `ml_walkforward.py`; test `tests/test_ml_walkforward.py`.

1. **Tests:**
   - `blocks(start=2019-01-01, end=2026-10-12, freq="Q"|"H")` gives about 31 or 16 blocks.
   - `train_window(block, length_months|"expanding")` starts no earlier than 2016-01.
   - The purge drops rows whose target ends at or after the block start.
   - `regime(session)` maps to `{"2019", "2020crash"(2020-02-19..2020-06-30), "2021", "2022bear",
     "2023-24", "2025-26"}`.
2. Implement; run the tests; commit.

### Task 4.2: Track metrics with overlap-corrected statistics

**Files:** create `ml_eval.py`; test `tests/test_ml_eval.py`.

1. **Tests:**
   - `rank_ic(pred, realised, by=session)` is the mean daily Spearman.
   - `quintile_spread` is the top-minus-bottom mean of rel_return / (ATR% × sqrt(H)).
   - `timing_excess` is the mean volatility-scaled return on "in" days minus all days.
   - `timing_sharpe` is the Sharpe of an in/out rule at a probability threshold.
   - `newey_west_t(series, lag=H)` matches a reference value on a known AR(1) series.
   - The cost charge reduces each signal's return by 7 bps per entry.
   - Every metric returns the overall value plus per-regime values.
2. Implement; run the tests; commit.

### Task 4.3: Controls

1. **Tests:**
   - `shuffle_symbols(preds, seed)` permutes predictions within each session.
   - `shuffle_time(preds, seed)` permutes each symbol's predictions across sessions.
   - Both keep the marginal distributions.
   - `controls(preds, ...)` returns the metric of each control next to the model's.
2. Implement; run the tests; commit.

### Task 4.4: Grid runner (task type `ml_grid`, CLI `ba2-test ml-grid`)

**Files:** create `ml_grid.py`; modify `app/main.py` (register the handler), `app/api/ml_search.py` (route
`/api/ml-search/grid`) and `ba2test_launcher.py` (`_cmd_ml_grid` plus the parser); test `tests/test_ml_grid.py`.

**Config format** (JSON list; one entry is a cell, which runs once per seed):

```json
{"track": "selection", "universe": "ndq30_dev", "inputs": "daily", "groups": ["T","M","X"],
 "target": {"kind": "rel_return", "horizon": 5}, "family": "lgbm", "params": {},
 "train_months": 36, "blocks": "Q", "seeds": [1, 2, 3]}
```

1. **Tests (tiny synthetic store):**
   - The runner executes 2 cells × 2 seeds.
   - It writes `results.jsonl` (one row per cell and seed, with all metrics, controls, regime breakdown and
     timings) and `predictions/<cell>_<seed>.parquet`.
   - A restart skips finished rows.
   - A locked symbol in a configuration raises `LockedSymbolError`.
   - `factor_effects(results)` returns the mean and standard deviation per factor level.
2. **Implement:**
   - **Scheduling:** two pools, a GPU pool (`--gpu-slots`, default 3) for the tsai and Darts families and a CPU
     pool (`--cpu-slots`) for the rest. Each worker loads the stores once.
   - **Per cell and seed:** walk the blocks, fit, predict on the block, then concatenate and score
     (Tasks 4.2-4.3).
   - **Ranking:** a cell's score is mean − std across its seeds of its track's primary metric (selection:
     quintile_spread after costs; timing: timing_excess after costs).
3. Run the tests; commit.

### Task 4.5: Gate check, factor report and dashboard

1. **Test:** `gate(cell_results)` applies the design's stage-1 gate (beats the baseline and its shuffle control
   on the seed mean, in at least 4 of 6 regimes including 2022bear or 2020crash, positive after costs) and returns
   a reason per failed condition.
2. **Implement:**
   - `report.md`: factor-effect tables per track and the gate table.
   - `dashboard.html`: one self-contained page (cells × metrics heatmap, factor effects, gate status), rebuilt
     after every 20 finished rows. Publish it with the Artifact tool once the grid starts.
3. Run the tests; commit.

---

## Phase 5: strategies for stage 2

### Task 5.1: MLForecast ranking mode and a rotation strategy (selection track)

**Files:**
- `packages/experts/ba2_experts/MLForecast.py`: new setting `rank_top_n` (0 = off).
- `packages/common/ba2_common/core/ml_forecast/store.py`: `read_session(model_key, session, horizon)` returns
  every symbol's row for one session.
- `testplatform/ba2test_launcher.py`: strategy `MLR`.
- Tests: `packages/experts/tests/test_ml_forecast_expert.py`, `packages/common/tests/test_ml_forecast_store.py`.

1. **Tests:**
   - With `rank_top_n=2`, a symbol ranked 1st or 2nd by score among the session's stored rows gets BUY, the 3rd
     gets HOLD.
   - On the open-positions pass, a held symbol that dropped out of the top N gets SELL.
   - The scores used are only from rows ≤ the cutoff.
2. **Implement:** MLR = entry on bullish + flat; exit when the expert turns bearish (left the top N) or
   `days_opened >= rebalance` (gene 5-20).
3. Run the tests; commit. This changes packages, so `PACKAGE_VERSION` gets bumped in Task 9.2.

### Task 5.2: Timing holding matched to the horizon

1. **Test:** for MLH, `--ml-hold-from-horizon` sets the `days_opened` gene to about H × 7 / 5 − 1, as a choice of
   that value ± 1.
2. Implement; run the tests; commit.

---

## Phase 6: pipeline fixes found by the inventory

Delegate this phase to a sonnet subagent in its own worktree (branch `fix/ml-pipeline-v2-cleanups`); merge it
afterwards.

| Task | Fix | Test |
|---|---|---|
| 6.1 | The wizard tsai path supports 3-class targets (`c_out` from the target's class count; assessment uses argmax/macro-F1 for k > 2) | A triple_barrier_atr job trains and scores |
| 6.2 | `trend_reversal` with donchian, adx or stochastic raises `TargetBuildError` instead of returning zeros (and the UI hides them); `unified_trend` is removed from `targets.ts` | |
| 6.3 | Dead options. The crossValidation option is removed from the UI; tsai `assess_model` computes balanced_accuracy; MSE and R² are added to the regression evaluation; the per-indicator timeframe is wired to `calculate_indicators_multi_timeframe` or hidden | One test per option |
| 6.4 | The wizard macro reads `fred_series.get_series_as_of` (first release) instead of the live revised series | The value is invisible before `realtime_start` |

## Phase 7: wizard options

### Task 7.1: New groups and targets in the wizard (subagent, worktree `feat/ml-v2-wizard`)

Add these to `DatasetWizard.tsx` / `PredictionTargetsPanel.tsx` and the backend builders, as with
`feat/custom-ml-ui`:
- panel feature options for the groups MTF, X, MAC, A, I and S;
- the targets rel_return, quintile, beats_universe, volatility and multistep.

Tests: payload round trip, builder calls, and `npx tsc -b`.

---

## Phase 8: run stage 1

### Task 8.1: Pre-flight

1. Run the whole ML test set:
   `python -m pytest tests/test_ml_*.py -q` in the backend, plus the packages' ml_forecast and expert tests. Expect
   all to pass.
2. Idle check on remote150. List python processes with their command lines (`Get-CimInstance Win32_Process`) and
   the listening ports.
   - Only the worker on :8100 may be present (BT confirmed no master uses it).
   - Report anything else to the owner and BT; do not stop it.
3. Smoke grid: 4 cells × 1 seed on 2 blocks. Expect results.jsonl and the dashboard to appear, with no errors.

### Task 8.2: Stage-1 configuration files

Write `C:\Users\basti\ba2-ml\mlv2\stage1_core.json`:

- **Selection core:** targets {rel_return, quintile, beats_universe, rank} × families {naive, linear, lgbm, xgb,
  catboost, tsai-inception, tsai-minirocket, darts-nhits (rel_return only)} × train_months {12, 24, 36, expanding}.
  Defaults: h = 5, groups T+M+X, daily inputs (tsai: intraday 1h panel, sampled bars).
- **Timing core:** targets {binary_direction, direction (terciles), binary_barrier, regression, quantile} ×
  the same families × the same training lengths, on the timing instruments plus MAG7.
- **Seeds:** 1, 2, 3.

### Task 8.3: Launch, monitor, ablate

1. **Launch:** `ba2-test ml-grid --config stage1_core.json --out mlv2/stage1 --gpu-slots 3 --cpu-slots 18`,
   in the background with a log.
2. **Monitor:** watch `results.jsonl`, and re-publish the dashboard artifact on each refresh.
3. **Ablations:** once the core finishes, write `stage1_ablate.json` around the top 5 cells per track:
   - each group added or removed (T, MTF, M, X, MAC, F, A, I, S);
   - horizons 1/3/10/20;
   - inputs 1h / 15m / daily;
   - losses.
4. **Report:** apply the gate (Task 4.5); write `mlv2/stage1/report.md`; message BT and the owner with the factor
   effects and the survivors (or the stop).

---

## Phase 9: wrap-up of the build

### Task 9.1: Code review

Use `superpowers:requesting-code-review` on the branch diff (Phases 0-7) before stage 1 starts, and fix the
findings.

### Task 9.2: Versions

- Bump the PACKAGE_VERSION of ba2_common and ba2_experts (Task 5.1) and set their minimums equal.
- Bump TEST_APP_VERSION.
- Run `python tools/check_package_versions.py --base origin/dev --allow-neutral-change`.
- Push the feature branch only. Nothing goes to dev without the owner's go.

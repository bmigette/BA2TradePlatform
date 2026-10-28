# ML exploration v2: design

*Status 2026-10-27: approved by the owner in a brainstorm. Executed on remote150 (RTX 4090) on branch
`feat/custom-ml-models`. It follows `2026-10-27-custom-ml-models-design.md`, which failed its controls.*

## 0. Why a second round

The first round (MAG7, 12-month training, 7 quarterly folds) found no robust edge. Report:
`CUSTOM_ML_REPORT_2026-10-27.md` on remote150.

- **The strategy layer faked an edge.** S1's winners used a +2% take-profit (the platform minimum) against a -10%
  stop. That bracket pays on mega-caps whatever stock the model picks.
- **There was one real, small signal: N-HiTS 5-session timing.** With hold-only exits, a time shuffle wiped it out
  (2025: +59% to about 0%; 2026 H1: +5.0% to -0.7%). It was gone in Jul-Oct 2026. Stock selection added nothing.

Five structural lessons drive this design:
1. **Too little data:** about 1,750 daily decisions, all from one bull market.
2. **Large selection noise:** a single retrain moved one score from 0.065 to 0.018.
3. **Exits can fake an edge:** fair exits and shuffle controls must be the default.
4. **The search and the expert disagree:** the expert GA chose stores with weaker signal. The expert's gates do not
   fit every target scale; the 1-session 3-class model never cleared the 0.5% min_edge floor.
5. **No clean holdout left:** 2026 has been looked at too often to serve as one.

## 1. Scope, data, holdouts

- **Two tracks on shared infrastructure.**
  - **Selection:** rank stocks against each other and hold the top few.
  - **Timing:** decide when to be in or out. The instruments are SPY, QQQ, IWM, DIA, SMH, the 11 sector ETFs and the
    MAG7 singly. TLT and GLD serve as cross-asset features. All of them are cached in daily and 5-minute bars.
- **Universe.**
  - Develop on NDQ30. **NDQ30 is a hindsight-selected list:** the thirty names are today's Nasdaq leaders, chosen
    in 2026 for having grown into them, so every NDQ30 result carries a survivor and winner bias that no
    point-in-time rule removes (it is in which names are studied, not in any row's value). Read an NDQ30 result as
    "does the signal exist at all", never as an expected return.
  - Scale survivors to large caps: each day's top ~300 by market cap and liquidity, from the cached screener store.
  - **Survivorship bias, accepted by the owner on 2026-10-28:**
    - The store is point-in-time in time but survivor-only in membership. TWTR, ATVI, SIVB, FRC, CELG, XLNX and VMW
      are absent.
    - It starts 2020-01, so the scale-up stage covers 2020-2026.
    - Every scale-up result is reported with this bias stated, since it flatters long strategies.
    - The rejected alternative: a dollar-volume universe from the 7,870-symbol price cache. It keeps some delisted
      names (ATVI, SIVB, FRC, VMW, PXD) but still misses others (TWTR, CELG, XLNX, SPLK).
- **History.**
  - Daily features and targets: 2016-2026.
  - Intraday features: 2019-2026 (5-minute bars exist for 93 of the NDX100 from 2019).
  - The 2020 crash and the 2022 bear market are inside the window.
- **Holdouts, locked before any run.**
  - **Locked symbols:** a fixed random third of NDQ30 (10 names) and a third of the large-cap universe. They are
    never used for training, search or selection. The lists are committed, and a guard test fails any run that
    touches them.
  - **Forward period:** the finalists paper-trade from 2026-10-13 for 2-3 months.
  - **2026-01..10 is ordinary walk-forward data,** never a verdict.
- **Splits.**
  - Walk-forward with quarterly prediction blocks from 2019: about 30 blocks. Deep models use half-year blocks.
  - Every training window is purged by the target horizon.
  - Training length is a grid factor: 12, 24 or 36 months, or expanding.
- **Regime tags.** Results are reported per regime: 2019, 2020 crash, 2021, 2022 bear, 2023-24, 2025-26.

## 2. Feature library

Features live in one point-in-time store per (symbol, session), plus intraday panels, built once from the cache.
Each group:
- is a switchable grid factor;
- has a documented visibility rule;
- has a cut test: a build from a cache truncated at an intraday bar end must match on every common row;
- gets a per-year coverage report before it enters the grid.

| Group | Contents | Source |
|---|---|---|
| T technical | The current panel set, plus the 13 wizard indicators in normalised form (ADX, SAR distance, Donchian position, OBV slope, pivot distance, ...) | Bars |
| MTF multi-timeframe | Daily and weekly indicators, joined only once their bar has ended | The existing safe aligner |
| X cross-sectional | Features as ranks or z-scores within the day's universe; momentum 12-1, Weinstein stage, relative volume, sector | Screener metric store |
| M market / cross-asset | SPY/QQQ/IWM, sector-ETF and own-sector relative strength, TLT/GLD returns, breadth (share of the universe above its 50-day average) | Bars |
| MAC macro | VIX, 10y and 3m yields, term spread, BAA10Y credit spread, CPI, payrolls, unemployment; first-release values only | FRED vintage cache (replaces the revised live macro) |
| F fundamentals | Earnings surprise and growth; balance-sheet and cash-flow ratios, visible after SEC acceptance | FMP cache |
| A analyst | Net upgrades over 30/90 days, price-target revisions, distance to the consensus target; visible the session after publication | FMP grades and price targets |
| I insider | Net insider buying over 90 days, with a coverage flag where FMP's history is truncated | FMP insider_v2 |
| S structure | Support/resistance distance, channel position, bars since a break of structure | market_conditions store |
| O options | IV level, IV rank, term slope, skew | Options cache; MAG7/SPY/QQQ from 2024 only, so a timing side experiment |

**Data rules (from earlier BA2 incidents; each gets a test or a coverage check):**
1. **Analyst targets.**
   - FMP's price-target *consensus* is windowed (about the last quarter), not a history; using it before that window
     is look-ahead.
   - Build the A group only from the dated per-analyst rows (`price_target.publishedDate`, `analyst_grades.date`),
     aggregated as of t.
   - Report the number of distinct analysts behind each value, so that one analyst cannot pass a "min analysts"
     style gate.
2. **Macro.** "First release" means vintage alignment: a value is visible from its `realtime_start` (ALFRED
   semantics), never "observation date + lag". DeterministicScorer once read prints 1-2 releases early that way.
3. **Options IV.**
   - The ThetaData history starts in 2020, and the store ends at 2026-09 expiries.
   - `open_interest` is dead, and spreads are constant in the parquet; the greeks are present.
   - So IV stays a timing side experiment and never enters the main grid.
4. **Prices.**
   - All prices come from the Alpaca-built OHLCV cache (the BT default since 2026-10-22), never FMP bars.
   - FMP has known split and spin-off defects: CRWD mixed basis, the HON and NVS spin-offs, META before 2022-06.
5. **Thin groups.**
   - Insider and earnings-drift-style data is thin on large caps.
   - Every group's per-year coverage on the universe is reported before it enters the grid.
   - A group under 70% coverage in a year is masked for that year, with a missing-flag feature, rather than filled.
6. **Point-in-time universe.** Screener-store universes are read through `metric_store.visible_scan_date` /
   `knowability.scan_cutoff_date`, never the raw scan dates.
7. **Machine time.** remote150 also hosts the goal2027atr lane and the exploration grid. Ask the owner before
   holding its cores for days; the grid runner takes a slot cap.

**Excluded, with reasons:**
- **Leak:** zigzag (hindsight pivots) and the live-snapshot fundamentals.
- **No publication date:** earnings estimates.
- **History too short:**
  - congress trades (capped at 100 rows);
  - news (26 symbols, uneven dates). It can return only if the news cache is extended.

## 3. Prediction types and models

**Targets (grid factor):**
- **Selection:**
  - forward return relative to the universe mean (regression);
  - top/bottom quintile classification;
  - LightGBM learning-to-rank (lambdarank);
  - binary "beats the universe".
- **Timing:**
  - binary up/down;
  - 3-class terciles;
  - triple barrier and binary take-profit-first (ATR barriers, k fitted per fold);
  - volatility-scaled regression;
  - quantile regression;
  - a volatility forecast, used for sizing only.
- **Both:** multistep (1, 3, 5, 10 and 20 sessions in one model).

Horizons are 1, 3, 5, 10 and 20 sessions.

**Model families (grid factor):**

| Family | Members | Note |
|---|---|---|
| Baselines | naive momentum, logistic or ridge regression | Always run; every model must beat them |
| Gradient boosting | LightGBM (main), XGBoost and CatBoost as a check | Installed, never used; cheap on CPU workers |
| tsai | InceptionTime, TST, LSTM-FCN, MiniRocket, PatchTST, TCN, ResNet | Intraday bar sequences |
| Darts | N-HiTS, N-BEATS, TCN | TFT only in stage-2 tuning (20-55 min per trial) |
| Chronos | Zero-shot | Comparison point only |

**Losses:**
- classes: CE, focal or weighted CE;
- regression: MSE or Huber;
- quantile models: pinball.

Class weighting replaces the 15% refusal for imbalanced long horizons. A fold with fewer than 50 examples of any
class is still refused.

## 4. Stage 1: signal-level screen

- **Grid.**
  - A core grid per track: target type × model family × training length, 3 seeds each.
  - Defaults for the core grid: horizon 5, feature groups T+M+X, daily decision bars.
  - Then ablations around the best core cells:
    - each feature group on or off;
    - horizons 1/3/10/20;
    - 1h vs 15m vs daily-only inputs;
    - losses.
- **Cost.**
  - Boosting and baselines run every quarterly block on CPU.
  - Deep models run half-year blocks and train on a few sampled intraday bars per session, at about 10-15 min per
    run.
  - Estimate: about 250 deep and 1,000+ cheap runs, about 3 days.
- **Scoring.** Out-of-sample walk-forward predictions, development symbols only.
  - **Selection:** daily rank IC and the top-minus-bottom quintile spread in volatility units.
  - **Timing:** the excess of "in" days over always-in (volatility-matched) and the Sharpe of an in/out rule.
  - Both are reported per regime, with overlap-corrected t-statistics.
  - **Controls on the same predictions:**
    - stock shuffle for selection;
    - time shuffle for timing;
    - naive momentum;
    - the linear baseline.
  - Calibration is reported, not optimised.
  - A configuration ranks by its mean minus one standard deviation across its 3 seeds.
- **Gate to stage 2.** A configuration passes when, averaged over its seeds:
  - it beats both the baseline and its shuffle control;
  - it does so in at least 4 of 6 regimes, including 2022 or the 2020 crash;
  - its spread or excess stays positive after 7 bps per trade.
  - At most 10 survivors per track go on.
- **Output.**
  - A factor-effect table per track: the mean effect of each factor level, with its spread.
  - A dashboard that fills in as the grid runs.
  - If nothing passes, the factor-effect report is the deliverable and the exploration stops.

## 5. Stages 2-4: from signal to verdict

**Stage 2: tuning and strategy test** (development symbols, 2019-2026).
1. **Tuning:** a short GA per survivor over a fixed hyperparameter space; fitness is the 3-seed mean.
2. **Store:** stitched predictions go to the forecast store (class-probability or decile rows).
3. **Expert walk-forward:**
   - Rolling yearly folds 2020 → 2026: 6 folds.
   - Costs: 4+3 bps, $10k cap.
   - **Timing track exits:** hold-only (MLH), with the holding time matched to the horizon. Fixed brackets in the
     style of S1's +2%/-10% are banned.
   - **Selection track exits:** a new rotation strategy that holds the top N, equal weight, with the rebalance
     cadence as a gene.
4. **Controls, always:**
   - stock and time shuffle stores;
   - naive momentum;
   - equal-weight universe buy-and-hold;
   - QQQ.

**Stage 3: scale-up.** Stage-2 passers are retrained on the point-in-time large-cap universe, with its own locked
third, and must repeat their stage-2 result there.

**Stage 4: verdict.**
1. The frozen finalists run once on the locked symbols over 2019-2026; nothing is changed after.
2. They then paper-trade forward from 2026-10-13 for 2-3 months, with daily store appends (the model server, ML
   forecast milestone 4, or a daily batch job).

**Success, all required, after costs:**
- It beats its track's shuffle control, naive momentum and the equal-weight universe on return over drawdown.
- It is positive in at least 4 of 6 yearly folds, including 2022.
- Its locked-symbol result is at least half of its development result.
- Its top 5 trades make under half of the profit (timing track: under two-thirds, given its low trade count).

**On failure at any gate:** report the factor-effect findings and stop. Never retune on a holdout.

## 6. Engineering and timeline

**Build (days 1-2):**
1. **Daily feature store**, 2016-2026, with the groups of section 2, cut tests and coverage reports. The intraday
   panel extends to 2019-2026 for NDQ30 and the timing instruments.
2. **Holdout lock:** the committed symbol lists and the guard test.
3. **Grid runner**, extending the `ml_model_search` job:
   - configuration list × seeds;
   - quarterly and half-year walk-forward;
   - regime tags;
   - per-track metrics and both shuffle controls;
   - the factor-effect report;
   - resume;
   - CPU/GPU split.
   - New families: LightGBM, XGBoost and CatBoost, the linear baselines, MiniRocket, PatchTST, TCN, ResNet and N-BEATS.
4. **New targets:** relative return, quintile rank, learning-to-rank, beats-universe, horizons 10 and 20, multistep,
   volatility.
5. **Rotation strategy** for the selection track.
6. **Fixes found by the pipeline inventory:**
   - the wizard's binary-only classification path, so 3-class targets work;
   - trend-reversal targets that silently return zeros (donchian, adx, stochastic);
   - dead options (cross-validation, the balanced-accuracy / MSE / R² fitness metrics, the per-indicator timeframe),
     each wired or removed;
   - FRED vintages replacing the revised live macro.
7. **Wizard options** for the new feature groups and targets.

**Machine time:**

| Days | Stage |
|---|---|
| 3-5 | Stage 1 |
| 6-7 | Stage 2 |
| 8-9 | Stage 3 |

Then the locked-symbol verdict, then 2-3 months of forward paper trading.

**Rules:**
- cached data only;
- feature branches only, nothing merged to dev without the owner's go;
- no `REQUIRED_PACKAGE_VERSIONS` raise or `TEST_APP_VERSION` bump pushed to dev while a master is mid-job;
- subagents on sonnet;
- report economics, not only fitness;
- inform BT at each gate.

**Deliverables:**
- this design;
- the grid dashboard;
- a report per stage with its factor-effect tables;
- a go/stop recommendation at each gate.

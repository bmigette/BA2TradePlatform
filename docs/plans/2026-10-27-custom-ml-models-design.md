# Custom ML models: design

*Status 2026-10-27: approved by the owner in a brainstorm. Test work: if it proves worth the effort, bigger models
and longer expert training ranges follow. Executed by the BTBigPC session on remote150 (RTX 4090).*

**Why.** The foundation-model pilot (TimesFM 3.0, Kronos-small; report `ML_PILOT_REPORT_2026-10-27.md` on remote150)
found no edge: a no-model control (each symbol's own trailing-return deciles) beat both out of sample. This design
trains our own models on cached data and feeds the best ones to the existing `MLForecast` expert, with the same
controls.

## 1. Datasets and targets

- **Universe:** MAG7 (AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA), plus SPY and QQQ as market context. MAG7 is chosen
  with hindsight, so every result is compared with equal-weight MAG7 buy-and-hold.
- **Bars:** H1 built from the cached Alpaca 5-minute data, regular session only (7 bars per day). M15 is a second
  dataset variant the search may try.
- **Samples:** one per H1 bar (about 12k a year for MAG7). The expert still decides **once a day**, at its
  decision time, from the last bars that have ended. Samples overlap in time, so the effective sample size is smaller.
- **Features, all from the cache, no news:**
  - technical: the test platform's indicator set on H1, plus daily context (distance from the 50/200-day averages,
    recent returns);
  - market (new): SPY and QQQ H1 returns, relative strength against QQQ;
  - fundamentals: FMP statements and ratios, each usable only after its filing date (point in time).
  - Forbidden: the existing "current fundamentals" option (today's values broadcast to every row) and anything
    computed from future bars. A test asserts it.
- **Targets** (genes):

  | Target | Definition | Balance |
  |---|---|---|
  | Triple barrier (primary) | TP of k x ATR before SL of m x ATR within N sessions: win / loss / timeout | k, m chosen on the training fold |
  | Direction | up / flat / down over 1, 3 or 5 sessions | thresholds = training-fold terciles |
  | Forward return (Darts) | regression with quantiles | n/a |

  The existing trend labels are excluded: several look at bars on both sides of a point.
- **Distribution check:** each dataset reports class shares per fold; a target whose smallest class is under 15% in
  any fold is refused, never silently rebalanced.
- **UI:** the market features, the new targets and the point-in-time fundamentals are options in the test-platform
  dataset-creation wizard, like the existing ones.

## 2. Training, search and validation

- **Folds:** 7 quarterly folds predicting 2025-01 .. 2026-10-12, each trained on the 12 months before it (training
  data from 2024-01). A gap of the target horizon separates training from prediction. Scalers and target thresholds
  are fitted on the training fold only.
- **Pooled or per-symbol:** one model per stock, or one MAG7 model with the stock as a feature (a gene).
- **Models:** tsai classifiers (InceptionTime, TST, ...) and Darts (TFT, N-HiTS) on the GPU, using the training code
  with the `fix/ml-dataset-pipeline` fixes.
- **Stage A, model search:** a new test-platform job type.
  - Genes: dataset variant (H1/M15, feature groups), target and its parameters, pooled or per-symbol, model family
    and architecture, lookback, main hyperparameters.
  - Trial: train every fold, stitch the predictions.
  - Fitness on the **2025** stitched predictions only: excess return of the top-confidence signals over a
    volatility-matched control on the same session, required to beat the naive momentum control, with a small
    calibration penalty. Never plain accuracy.
  - The **2026** folds are produced the same way and never shown to the search.
  - Budget: max 30 generations, early stop 5 at 1%. Estimated 1-3 min per trial on the 4090.
- **Provenance:** each fold model is saved with its feature list, scaler and settings, so every stored prediction
  can be rebuilt.

## 3. Expert, controls and success

- **Stage B:** the top 3-5 stage-A setups are written to the forecast store (`<cache>/ml_forecast/<model_key>/`),
  one model key each, with their stitched predictions. The store row format and the mapping gain the class-probability
  output type (refused today); a triple-barrier P(TP first) feeds `min_prob`. Backtest and live read the same store.
- **Expert GA:** `tools/run_walk_forward.py`, train 2025, test 2026-01..06 and 2026-07..10-12 (the pilot's windows).
  The `model_key` gene picks among the built keys.
- **Controls:** the naive momentum store with the same GA budget; forecasts shuffled across symbols within a session;
  equal-weight MAG7 and QQQ buy-and-hold.
- **Success, out of sample in 2026, all of:**
  - beats the naive control and equal-weight MAG7 on return over drawdown, after costs;
  - positive in most 3-month windows;
  - top-5 trades at most about half the profit, and no single stock carries it;
  - the shuffle control kills it.
- **On failure:** report and stop; no retuning on 2026. **On success:** bigger models and longer ranges, then the
  NDQ30 list; live needs quarterly retraining and the model server (ML forecast milestone 4).

## 4. Constraints

- Cached data only; refuse loudly on missing data.
- Build on branches from origin/dev plus `feat/ml-forecast-expert` and `fix/ml-dataset-pipeline`. Nothing that
  raises a `REQUIRED_PACKAGE_VERSIONS` minimum or bumps `TEST_APP_VERSION` is pushed to dev while a master is mid-job.
- Report economics (profit, CAR, max drawdown, trades, concentration), not only fitness.

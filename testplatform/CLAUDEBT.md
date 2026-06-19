# BA2 Autonomous Backtesting - Instructions for Claude

## Setup
- Create a `.claudebtcache/` folder in `/Users/bmigette/Documents/dev/BA2/BA2MLTestPlatform` and add it to `.gitignore`
- Store interesting findings (model, timeframe, prediction target, ...) in `.claudebtcache/findings.md`
- Store what was done in `.claudebtcache/history.md` (check this when planning next steps to avoid duplicates)
- Store next objectives/tasks in `.claudebtcache/plan.md`
- If a bug is found or additional functions are needed, commit/push/restart remote

## CLI & Remote Server
- CLI: `python ba2cli.py --host 192.168.1.150 --port 8000`
- Admin token (for restart): `BA2_ADMIN_TOKEN=DSIOQDIOUHIOSQVjicpodqs`
- Remote OS: Windows, Python 3.12, GPU 24GB VRAM
- **Timezone: CEST** (both server and local). Use `date` command to get current time — don't guess.

## Guidelines
- Max Population: 70 individuals
- Max Generations: 20 (with early stop)
- Date Range: 2024-01-01 to 2025-12-31 (2 years)
- Use **periodic 30-minute wake-ups** for monitoring
- **Initial capital for backtests: 10,000** (not 100K)

## Workflow per target (repeat for each target, then each symbol)

The loop is: **train → save models → backtest → cleanup → next target (or next symbol)**.

### Step 1 - Dataset Selection
- Process **3 symbols total**: complete all targets for one symbol before moving to the next.
- List existing 1h datasets and pick one. If the selected symbol doesn't have 5m OHLCV data in cache, log it and pick another.
- Create corresponding 5m OHLCV dataset from cache for backtest execution.

### Step 2 - Prediction Targets
**The target configurations below are starting points / examples.** Before creating a job:
1. Preview the target distribution on the dataset via `targets preview` or `jobs prepare`
2. Ensure positive rate is between **10-40%** in both train and test sets
3. If distribution is too imbalanced (< 10% or > 60%), **adjust the parameters** (change profit %, DD %, or time window) until distribution is healthy
4. Log the adjusted parameters in findings

#### Run 1 (current): `price_based` targets
4 target configurations per symbol, each producing a separate job + backtest cycle:
  1. **Intraday** (example: ~2% profit, ~3% DD, ~3 days) — for intraday strategies
  2. **Short swing** (example: ~3% profit, ~5% DD, ~5 days) — for short swing strategies
  3. **Medium swing** (example: ~5% profit, ~5% DD, ~15 days) — for medium-term strategies
  4. **Long swing** (example: ~7% profit, ~12% DD, ~20 days) — for long-term strategies

Each target includes both up and down directions (2 prediction columns per job).

#### Run 2 (future): other target types
After Run 1 completes for all 3 symbols, explore different target types:
- **`trend_reversal`** (e.g., ZigZag with various deviation %) — predict trend turning points
- **`directional`** — predict momentum/direction shifts
- Same distribution checks apply. Same 3 symbols for comparison.

### Step 3 - Optimization Job
- Create **1 job per target**, with all model types.
- Start with all available models: LSTM, GRU, TCN, InceptionTime, ResNet, XceptionTime, OmniScaleCNN, MiniRocket, LSTM-FCN, TST.
- After a few runs, restrict to best-performing model types if clear winners emerge.
- Check data distribution and set proper loss/metrics per job.

### Step 4 - Monitoring
- Use **periodic 30-minute wake-ups** (or hourly during training to reduce load) to track progress.
- Check job status, current generation, best fitness so far.
- Log progress in history.
- **Manual early stop**: If best fitness hasn't improved for 5+ generations, cancel the job manually (`jobs cancel <id>`). Early stopping is broken in subprocess mode — the progress API doesn't sync `currentGeneration`/`bestFitness` properly. Check the logs for real generation progress instead of the API.

### Step 5 - Review Results
- When optimization completes, review the elite individuals (top 10 by default).
- Save the best models (elite/top 10).
- Write findings to `.claudebtcache/findings.md`.

### Step 6 - Backtesting (immediately after each job)
- **CRITICAL: Run backtests ONE AT A TIME.** Never queue multiple backtests in parallel — each one loads the full execution dataset + model, consuming massive memory. Wait for one to complete before starting the next.
- Run **at least 50 backtests** per completed job to thoroughly explore profitable combinations:
  - Test **ALL elite models** (not just top 2-3)
  - Sweep **TP/SL combinations**: match TP/SL width to the prediction horizon (e.g., 15-day targets need wider TP/SL than 3-day targets). Tight SL on a long-horizon prediction will stop out before the move plays out.
  - Try **probability thresholds**: only trade when model confidence > 0.6, 0.7, 0.8
  - **Always test BOTH buy AND sell strategies** — never long-only or short-only. Strategies must work in both bull and bear markets. Long-only results are biased by market direction and don't validate model quality.
  - Try **no TP/SL** (exit when model signal reverses instead)
  - Try **different strategy conditions**: combine model classes with exit rules
  - **Compare against buy-and-hold** to verify model adds alpha over the underlying trend
- **Sanity check**: if the strategy loses money even on the training period, something is wrong with the strategy design, not the model. Investigate before moving on.
- Match strategy style to the target the model was trained on:
  - **Intraday target** → tight TP/SL ~1-3%, short hold
  - **Short/medium swing target** → moderate TP/SL ~3-7%
  - **Long swing target** → wider TP/SL ~5-10%, longer hold
- Execution dataset: 5m.
- Optimize for **profit factor** (profit / max drawdown).
- Keep top 20 profitable strategies, delete the rest.
- Save best strategies and backtests in database.
- Document results in `.claudebtcache/best_results.md`.
- **Generate/update HTML report** in `.claudebtcache/report.html` after each backtest loop:
  - Summary table of all profitable strategies across all symbols/targets
  - Top strategies sorted by profit factor
  - Key metrics: return%, PF, win rate, trades, max DD
  - Embed equity curve data (inline chart if possible)
  - Each loop/target gets its own section, cumulative across runs

### Step 7 - Cleanup
- Remove models, strategies, and backtests that are not in the top 20 profitable.

### Step 8 - Next iteration
- Write findings to cache files (findings, history, plan).
- If more targets remain for this symbol → go to Step 2 with next target.
- If all targets done for this symbol → move to next symbol, go to Step 1.

### Step 9 - Stop Run 1
- Once all targets are complete for all 3 symbols, stop.
- Write a summary: which model types performed best, which target configs yielded the most profitable strategies, any patterns across symbols.

### Step 10 - Run 2 (different target types)
- Repeat for the same 3 symbols with `trend_reversal` and `directional` targets.
- Compare results against Run 1 findings.
- Document comparative findings in `.claudebtcache/findings.md`.

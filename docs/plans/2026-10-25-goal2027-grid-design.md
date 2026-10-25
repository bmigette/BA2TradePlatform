# goal2027 stock grid: design

*Status 2026-10-25: design approved by the owner, section by section, in a brainstorm. Nothing is built yet.
Launch target: about 2027-01-10 on remote227.*

The goal2027 grid is the next baseline for the classic stock experts. It replaces the goal2020 grid and the deferred
ATR re-run ([atr_grid_2027_design.md](../strategy_research/atr_grid/atr_grid_2027_design.md)). It differs from
goal2020 in four ways:
- every cell runs a walk-forward chain over 2020-2026, not one in-sample GA;
- the sizing mode is a gene, not two matrices;
- the market gates are genes: the market-condition profile, plus a new SPY regime gate;
- entry time and manage time are separate genes.

Its results start a new baseline. Never compare its fitness values with goal2020, matrix3 or atr27 rows.

## 1. Grid shape

### 1.1 Cells (35)

The cells come from the atr27 strategy plan ([strategy_plan.json](../strategy_research/atr_grid/strategy_plan.json)).
A strategy is in the plan if it ranked in its expert's pooled goal2020 top 5 by fitness or by CAR.

| Expert | Bands | Strategies | Cells |
|---|---|---|---|
| FMPRating | large, mid, small | S1, S2, S6 | 9 |
| DeterministicScorer | large, mid, small | S1, S2, S6 | 9 |
| FMPEarningsDrift | mid, small | S1, S2, S3, S5 | 8 |
| FMPInsiderClusterBuy | mid, small | S1 | 2 |
| FMPSenateTraderWeight | Senate universe (2020-2026) | S1, S3, S5, S6 | 4 |
| FactorRanker | large, mid, small | none (no ATR or market genes) | 3 |

- FMPRating keeps its 2022 floor (`_EXPERT_MIN_START`). Its fold 1 trains on 2022-2023 and its jobs carry
  `-from2022`.
- The ML forecast expert ([2026-10-25-ml-forecast-expert-design.md](2026-10-25-ml-forecast-expert-design.md))
  joins as extra cells only if its pilot shows an edge.

### 1.2 Walk-forward chain, per cell

| Step | Train | Budget | Test |
|---|---|---|---|
| fold 1 | 2020-01-01 .. 2023-12-31 | cold start: population 200, at most 30 generations, early stop 5 at 1% | 2024 |
| fold 2 | 2020-01-01 .. 2024-12-31 | seeded with fold 1's final population, at most 15 generations | 2025 |
| fold 3 | 2020-01-01 .. 2025-12-31 | seeded with fold 2's final population, at most 15 generations | 2026 |
| refit | 2020-01-01 .. 2026-12-31 | seeded with fold 3's final population, at most 15 generations; passing cells only | none |

- **Seeding:** a seed never saw the next test year, so chain seeding adds no look-ahead.
- **Out-of-sample re-run:** each fold's distinct top N is re-run once on its test year. The engine saves those rows
  with the labels `WalkForward`, `wf-fold-k` and `OOS`.
- **Fold pass:** the fold passes if it trades, its out-of-sample return is above 0%, and its out-of-sample maximum
  drawdown is at most 1.5 times the in-sample one.
- **Cell pass:** at least 2 of 3 folds pass, and the 2026 fold is one of them, because 2026 is the only fully clean
  year.
- **Refit:** the refit winner is the deploy candidate. No out-of-sample data is left after the refit, so live
  forward testing is the next check.
- **Names:** `<band>-<expert>-<S>-goal2027-wf<k>` and `...-goal2027-refit`. All names are new, and every job
  resumes by name.

### 1.3 Genes carried by every S1-S7 cell

- Sizing mode: `risk_atr` or `notional` (§2.1).
- ATR stop, searched: the `atr-searched` RM block.
- Market profile: off, `ohlcv-v1`, `ta-structure-v1` or both, plus market exits (§2.2).
- SPY trend gate and SPY volatility gate, each with an off value (§2.3).
- Stop-loss loosening (`--search-sl-loosen`).
- Entry time and manage time (§2.4), plus the weekday genes.

FactorRanker keeps its own gene space. It takes no ATR, market or loosening genes, and it keeps a fixed time.

## 2. Code to build (in merge order)

Each item gets its own branch with a review. An item that can change GA results raises the package minimums, and it
merges only between jobs.

### 2.1 Sizing-mode gene

- Add the gene `model:sizing_mode` as a choice of `risk_atr` or `notional`. The `--sizing-mode` flag stays for older
  names.
- Decode-time neutralisation: genes that do nothing under the chosen mode decode to one canonical value, so the trial
  memo still recognises duplicates. Example: `atr_risk_budget_pct` under `notional`.
  - Audit every RM gene once for each mode. `max_virtual_equity_per_instrument_percent` is the main sizer under
    notional, but under risk_atr it is a ceiling that rarely binds. It stays searched in both modes.
- The deploy export writes the chosen `sizing_mode`.
- Tests: a genome with each value reproduces today's fixed-flag run bit for bit. A canonicalised genome hits the memo.

### 2.2 Market-profile gene

- Add `market:profile` as a choice of off, `ohlcv-v1`, `ta-structure-v1` or both. It replaces the on/off
  `market:enabled` in goal2027 names. Older names keep `market:enabled`.
- Every job pins both manifests. The leaf genes of each profile not picked decode to off, the same way
  `market:enabled=0` works today. Market exits follow the chosen profile.
- Tests: decoding is canonical (no memo misses), and an "off" genome equals an all-off run.

### 2.3 SPY regime gate for stock entries

- This reuses `ba2_common/core/benchmark_regime.py` and the `SpyTrendRegimeCondition` and `SpyVolRegimeCondition`
  conditions from `feat/spy-regime-gate`, which is first in the merge queue.
- Add the genes `cond:entry-spy-trend:mode` and `cond:entry-spy-vol:mode`, each with an off value, on S1-S7 entry
  rules only.
- The backtest calendar comes from the run's history source. Live trading reads FMP SPY closes, as built.
- Tests: the stock genes reach trials end to end. A gated run with no calendar is refused, as for options.

### 2.4 Separate entry and manage times

- Today one `schedule:time` gene drives both schedules. Replace it with `schedule:entry_time` and
  `schedule:manage_time` for goal2027 names, each using the same 7 values (09:35, 09:40, 09:45, 10:00, 12:00, 15:30,
  15:45).
- When both fire in the same minute, management runs before entry, in the backtest and in live trading. Exits then
  free cash for entries.
- Live trading currently runs open-positions jobs after enter-market jobs, so `JobManager` needs a fix and a parity
  test.
- The deploy export writes both times. The deploy tool still refuses a time that cannot finish before the close.

### 2.5 Walk-forward in the matrix driver

- Add `run_screener_capband_matrix.py --walk-forward`. It builds each cell's chain (§1.2) with the cell's own flags:
  spread and stress per band, the screener flags, the strategy plan, the name tokens, the time genes and the
  FMPRating floor.
  - Today `tools/run_walk_forward.py` wraps only a single `ba2-test optimize` call.
- Seeding hook: `initial_population_for_fold` returns the previous fold's final population, read from its stored GA
  state. Today the hook raises `NotImplementedError`.
- Add a pass option `--pass-require-fold <test-year>`.
- The refit step runs only for passing cells.
- Out-of-sample re-runs are local only, at about 14 GB each, so the master host needs that headroom.

### 2.6 Wrapper `tools/grid_goal2027.sh`

- `start`, `stop`, `status` and `resume`, as in goal2020, with the same stop order (driver last).
- The preflight checks the end date as well as the start: cache coverage to 2026-12-31, the metric store end, the
  manifest windows and the Senate shards. Today `check_window_coverage.py` checks only the start.
- It pins the paths: the Alpaca-built metric store, both manifests, the Senate universe and the exclusions file. The
  FMP-era store paths in `grid_goal2020.sh` and `grid_atr27.sh` are stale.
- The master runs on remote227 with local `--parallel` slots. Optional `--workers` can be added between jobs.

### 2.7 Rehearsal (November or December 2026)

- Run one FMPRating large cell and one small-cap cell end to end through `--walk-forward`, on today's data (which
  ends 2026-10-12), with the fold-3 test year cut at that date.
- It measures minutes per generation on remote227 and corrects the cost estimate in §4. It also exercises §3 steps
  4-6.
- It runs on remote227 once option stage 2 frees slots, or on remote150.

## 3. Data preparation (January 2027)

Every step runs through `tools/prepare_history.py` and [REPRODUCE-BACKTESTS.md](../REPRODUCE-BACKTESTS.md) §4. A new
§4 subsection, "goal2027 data", records the runs of record. No step is ad hoc.

1. **Universe (December, offline).** Start from `tools/screener_store_universe.txt` and add the 2026 US listings from
   FMP that pass the same listing filters. Review the list once. Symbols with too little history are screened out by
   the existing history rules (200 sessions for the EMA).
2. **New Alpaca stores (from 2027-01-01, since the end date cannot be in the future).** Ticker-mapping date
   2027-01-04, end 2026-12-31. This is a full re-download: about 4 days for 5-min bars and 1.5 h for daily, measured
   2026-10. It is a new store, because two mapping dates can never be mixed. Advance `DOWNLOAD_END` and
   `DOWNLOAD_ASOF` as a reviewed change.
3. **Split table, IPO dates, human split review, build, verify.** These end with the gate certificate. The known
   Alpaca outside-hours daily-close limitation (REPRODUCE §4.0) is accepted, not repaired.
4. **Metric store and daily panel** rebuilt to 2026-12-31 with `build-screener-metrics --ohlcv-history-source alpaca
   --daily-panel`, with the panel exclusions reviewed.
5. **Market-condition manifests** for the band union plus the new symbols, decision window 2020-01-02 .. 2026-12-31,
   with gap fill `previous` and the exclusions file. Also:
   - regenerate the Senate universe for 2020-2026 and rebuild its scoring shards;
   - warm the expert caches to 2026-12-31: FMP ratings and price targets, earnings, insider trades, statements and
     FRED (`ba2-test prewarm --end 2026-12-31`).
6. **Build locally, then push once to remote227.** Local stays the source of truth. Then check that the
   `/cache/manifest` count matches. Optional extra workers get the same push.
7. **Launch about 2027-01-10 to 2027-01-12.** The rehearsal will already have run steps 4-6 on the October data.

## 4. Cost, run order, monitoring

### 4.1 Cost (estimate, corrected by the rehearsal)

- **Assumptions:** 3.2 slot-minutes per 6-year FMPRating evaluation (August 2026), and these expert weights:

  | Expert | Weight |
  |---|---|
  | DeterministicScorer | about 4x |
  | FMPEarningsDrift | about 3.5x |
  | FMPSenateTraderWeight | about 8.6x |
  | FMPInsiderClusterBuy | about 1x |
  | FactorRanker | about 1x |

- **One FMPRating cell's chain:** about 50,000 evaluation-years, roughly 16 hours on 28 slots.
- **Whole grid:** the 35 cells weigh about 112 FMPRating cells, so roughly 10 weeks on remote227 alone. This could be
  off by 2x in either direction.
- **Levers:**
  - add remote150 as extra workers (about 25% faster);
  - early stopping usually ends around generation 17 to 20;
  - Senate is about 30% of the cost, so it runs last and can be cut.

### 4.2 Run order

Cells run one at a time with all slots. Each cell's chain runs to its verdict before the next cell starts.

1. FMPRating and DeterministicScorer in large and mid caps (the experts traded live).
2. FMPEarningsDrift and FMPInsiderClusterBuy in mid and small caps.
3. FMPRating and DeterministicScorer in small caps.
4. FactorRanker.
5. FMPSenateTraderWeight.

### 4.3 Monitoring and reporting

- `grid_goal2027.sh status` shows the cell, fold, generation and best fitness.
- Each finished fold writes the walk-forward report: in-sample vs out-of-sample, stitched out-of-sample,
  walk-forward efficiency and the pass verdict.
- `tools/report_grid_results.py` gains a goal2027 view: each cell's verdict, plus the refit winner's profit, CAR and
  maximum drawdown.

### 4.4 Deploy path

The refit winner goes through:
1. the concentration check: the top-1 and top-5 trades' share of P&L, from the persisted trades;
2. the `ForwardTest` label;
3. a forward test on dev (8080);
4. the owner's decision on prod.

## 5. Owner decisions (2026-10-25)

| Topic | Decision |
|---|---|
| Scope | the proven cells (strategy plan), FactorRanker and Senate |
| Walk-forward | chain-seeded, 15 generations for each later fold |
| Market gates | profile choice plus SPY regime gate |
| Times | separate entry and manage genes |
| Pass rule | out-of-sample return above 0 and drawdown at most 1.5x on at least 2 of 3 folds, 2026 required |
| Hosts | remote227 only, with optional extra workers; option stage 2 must be finished |
| Universe | extended with 2026 listings |

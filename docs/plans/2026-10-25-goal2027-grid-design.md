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

### 1.1 Cells (36)

The cells come from the atr27 strategy plan ([strategy_plan.json](../strategy_research/atr_grid/strategy_plan.json)).
A strategy is in the plan if it ranked in its expert's pooled goal2020 top 5 by fitness or by CAR.

| Expert | Bands | Strategies | Cells |
|---|---|---|---|
| FMPRating | large, mid, small | S1, S2, S6 | 9 |
| DeterministicScorer | large, mid, small | S1, S2, S6 | 9 |
| DeterministicScorer | xl | SQM | 1 |
| FMPEarningsDrift | mid, small | S1, S2, S3, S5 | 8 |
| FMPInsiderClusterBuy | mid, small | S1 | 2 |
| FMPSenateTraderWeight | Senate universe (2020-2026) | S1, S3, S5, S6 | 4 |
| FactorRanker | large, mid, small | none (no ATR or market genes) | 3 |

- FMPRating keeps its 2022 floor (`_EXPERT_MIN_START`). Its fold 1 trains on 2022-2023 and its jobs carry
  `-from2022`.
- **DeterministicScorer × SQM × xl** is the one cell outside the strategy plan. It is the exploration
  `quality_momentum` recipe as a GA cell (§2.8, evidence below). Every DeterministicScorer cell, this one
  included, also searches the DS factor genes (`--search-ds-factor-weights`, name token `dsfw`). Cells
  whose expert has an audited exploration dimension also carry `--search-exploration-ranges` (`explr`, §2.9).
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

DeterministicScorer cells add the factor genes of §2.8 (`dsfw`). The SQM cell carries the sizing, time and factor
genes but no market-profile or SPY-gate genes: SQM is not an S1-S7 strategy, and its market flags are refused.

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

### 2.8 DS factor genes, the SQM ruleset and the `xl` screen band

Evidence (exploration phase 1, exhaustive grid 2020-2025, $10,000, manifest `eb410451d4a3`; see
[exploration/README.md](../strategy_research/exploration/README.md)): the best recipe of the campaign is
DeterministicScorer on a liquid large-cap screen with technical/fundamental 70/30 (`tw_mom` 0.7, `tw_d200` 0.3,
`fw_quality` 0.6, `fw_piotroski` 0.4), `mom_lookback_days` 126, `theta_buy` 0.3, entry stop -12%, take-profit +20% and a
30-day time exit: fitness 3.16, 32.9%/yr, drawdown -32% (`quality_momentum` 70/30). The `analyst_targets` family
(analyst/technical 80/20, hold 60) reached fitness 2.46 and 24.6%/yr. The goal2027 DS genes (`w_technical`,
`w_fundamental`, `w_analyst` up to 0.4, `w_earnings`, `macro_mode`, the two thresholds, `k_stop`, `k_target`) cannot
express either, and no S1-S7 ruleset is "buy, fixed stop and target, time exit".

Owner decisions 2026-10-26: DS genes and the SQM rule: yes. PullbackReversion is NOT promoted (its results are not
worth it). The pullback DS blend and `etf_trend` are dropped.

**DS factor genes** (`ba2-test optimize --search-ds-factor-weights`; capband driver flag of the same name, which
requires `dsfw` in `--name-suffix`; DeterministicScorer jobs only, refused for any other expert):

| Gene | Range |
|---|---|
| `model:tw_mom`, `tw_d200`, `tw_rsi`, `tw_don` | 0.0-1.0, step 0.1 |
| `model:fw_quality`, `fw_piotroski`, `fw_value`, `fw_growth` | 0.0-1.0, step 0.1 |
| `model:mom_lookback_days` | choice 63, 126, 252 (that order is persisted) |
| `model:aw_targets`, `model:aw_grades` | 0.0-1.0, step 0.1 (the two analyst legs; DS divides by their sum) |
| `model:analyst_target_window_days` | 30-90, step 30 (the `analyst_targets` window) |
| `model:analyst_min_targets` | 3-5, step 2 (the `analyst_targets` minimum observations) |
| `model:w_analyst` | existing gene, ceiling widened 0.4 -> 0.8 under the flag only |

- **Zero-sum rule.** DS divides every section by the sum of its positive sub-weights, so only ratios matter. Two
  combinations have no honest reading and are canonicalised in the one decode path (`neutralise_ds_factor_genes`:
  GA, TOP-N persist, re-run, export, and the trial-memo key):
  - a section whose sub-weights are all 0 while its section weight is positive is switched OFF: section weight
    `w_technical`/`w_fundamental` -> 0.0, its sub-weights removed. Without this the technical section would score a
    literal 0.0 that still carries `w_technical` (a hidden dilution), and the fundamental section would be dropped as
    "missing";
  - a section weight of 0 makes its sub-weights dead (removed); `mom_lookback_days` is dead when the technical
    section is off or `tw_mom` is 0; the two analyst details are dead when `w_analyst` is 0.
  - the analyst legs `aw_targets`/`aw_grades` follow the same pattern: `w_analyst` 0 removes both legs and the two
    target details; both legs 0 with `w_analyst` > 0 switches the analyst section off (`w_analyst` -> 0.0, DS would
    otherwise drop it as missing); a zero `aw_targets` removes the target window and minimum.
  A genome with every section at 0 is left as today: it trades nothing and the trade-count gate disqualifies it.
- **Parity.** The deploy payload carries the canonical `w_technical`/`w_fundamental` (what the backtest ran), not the
  raw genome. A row whose optimization searched the genes but lost one is refused at export. Without the flag the gene
  space, the run settings and the run names are byte-identical.

**SQM ruleset** (`--strategy SQM`, DeterministicScorer only; also accepted by the capband driver and
`--strategy-plan`). Entry: buy when `bullish` AND `has_no_position` AND `days_since_last_close` > 1; at the fill
`adjust_stop_loss` and `adjust_take_profit` from `order_open_price`. Exit: close when `has_position` AND
`days_opened` > N. The same semantics as the exploration `time_exit()`.

| Gene | Range | Default (the recipe) |
|---|---|---|
| `entry:buy:a1:action_value` (stop %) | -14 to -6, step 2 | -12 |
| `entry:buy:a2:action_value` (target %) | 10-30, step 5 | 20 |
| `cond:xt:value` (time exit days) | 20-120, step 10 | 30 |

The gene names are the ones S6 uses for the same roles. No gene switches a part off. With every gene at its default the
ruleset equals the exploration `quality_momentum` recipe, compared on the live export (test). SQM takes no market
profile or SPY genes.

**`xl` screen band** (`ba2-test optimize --screener --screen-preset xl`). A fixed screen, not a searched cap band: the
exploration `new_idea_baseline` screen with no screener genes. The preset pins every selection key in the run's
`screener_opt.base_settings` (recorded as `screener_opt.screen_preset`); it is refused together with
`--screener-cap-band` or `--screener-base-json`, and without `--screener`.

```xl-screen
market_cap_min: 10e9
market_cap_max: 0
price_min: 10
volume_min: 500000
dollar_volume_min: 0
float_min: 10e6
relative_volume_min: 0
price_drop_pct: 0
price_drop_days: 5
weinstein_stage2_only: False
max_stocks: 50
```

Two deliberate differences from the exploration screen, both forced by live parity:
- `dollar_volume_min` is **0**, not 10e6. Live has no dollar-volume stage and the live-simulation gate refuses a
  positive value (`live_sim.check_settings`), so a floor could neither be simulated by a classic job nor deployed
  faithfully. With cap >= $10B, price >= $10 and volume >= 500,000 the floor should rarely bind, but that is NOT
  measured here: the exploration result (32.9%/yr) was produced with the floor.
- `price_drop_days: 5` is stated because the simulation refuses a screen that leaves a selection key implicit
  (`sort_metric` is the launcher's built-in `market_cap`).

The three cap bands (`SCREENER_CAP_BANDS`, `ba2_providers`) still only set a cap-gene range. The capband driver treats
`xl` as a preset band: `--bands xl` passes `--screen-preset xl` instead of `--screener-cap-band`, takes the large-cap
spread stress (4 bps), and drops SQM for every expert but DeterministicScorer, so the cell is
`--bands xl --strategies SQM --skip-experts <all others>`. Tests pin this block to the preset, the preset to the
exploration screen (apart from the floor above), and the export to the live screener keys.

### 2.9 Exploration ranges: what the families varied by hand vs what the cells search

Owner request 2026-10-26: every dimension exploration phase 1 varied by hand should be a GA gene in the matching
goal2027 cell, with the family's control on the lattice. Audit against the code (S1/S2/S3/S5/S6 templates in
`ba2test_launcher.py`, `_EXPERT_OPT`, the baselines in `tools/strategy_research/exploration/baselines_20260907.json`).
Status: covered = every exploration value and the control is on the searched lattice; range = the gene exists but
misses an exploration value or the control; missing = no gene.

| Family (cell expert, strategies) | Dimension | Exploration values (control) | Gene today and range | Status | Fix |
|---|---|---|---|---|---|
| mid_insider (InsiderClusterBuy, S1) | time exit | 60/90/120/210 (210) | `exit:s1_exit_time:enabled` + S1 day leaf 30-200 step 30 | range: 210 missing (max reached 180) | `explr`: 30-210 step 30 |
| mid_insider | insider lookback | 30/60/90/120 (120) | `model:lookback_days` 30-120 step 15 | covered | none |
| small_earnings, mid_earnings (EarningsDrift, S1) | time exit on/off | 60/90/120, off (small: off; mid: 150) | `exit:s1_exit_time:enabled` + 30-200 step 30 | covered (off = toggle) | none |
| EarningsDrift S2 | time exit on/off | 60/90/120, off | `exit:exit_time:enabled` + `cond:xt:value` 20-120 step 20 | range: 90 missing | `explr`: step 10 |
| EarningsDrift S3 | time exit on/off | 60/90/120, off | toggle + `cond:xt:value` 30-150 step 30 | covered | none |
| EarningsDrift S5 | time exit on/off | 60/90/120, off | none (S5 has no time exit) | missing | `explr`: toggled `exit_time` close + `cond:xt:value` 30-150 step 30 (default 90), before the floor stop |
| mid_earnings (EarningsDrift, S1) | first-tier target offset | -14/-12/-10/-8 (-14) | `entry:buy-1:a1:action_value` -20..10 step 2 | covered | none |
| mid_earnings (S2, S3, S5) | first-tier target offset | n/a | those strategies have no target-anchored entry TP | not applicable | none |
| mid_ds (DS, S1) | time exit | 15/20/25/30 (25) | toggle + S1 day leaf 30-200 step 30 | range: 15, 20, 25 missing | `explr`: 15-180 step 5 |
| mid_ds (DS, S2) | time exit | 15/20/25/30 (25) | toggle + `cond:xt:value` 20-120 step 20 | range: 15, 25, 30 missing | `explr`: 15-120 step 5 |
| mid_ds (DS, S6) | time exit | 15/20/25/30 (25) | `cond:xt:value` 10-30 step 5 (always on) | covered | none |
| mid_ds (DS, S1, S2, S6) | separate bearish exit on/off | on/off | `exit:s1_exit_signal:enabled`, `exit:exit_bearish:enabled` (all three) | covered | none |
| small_rating (FMPRating, S1) | time exit | 60/90/120, off | toggle + 30-200 step 30 | covered | none |
| small_rating (S2) | time exit | 60/90/120, off | toggle + `cond:xt:value` 20-120 step 20 | range: 90 missing | `explr`: step 10 |
| small_rating (S6) | time exit | 60/90/120, off | `cond:xt:value` 10-30, no toggle | not applicable | none: S6 is the quick-cycle strategy by design; the flag is refused for FMPRating S6 |
| small_rating (S1) | bracket original/near/wide, both tiers | tier TP/SL: near (-14, -4), wide (8, -18), original (-14, -4 and 8, -18) | per tier `entry:buy-k:a1:action_value` -20..10 and `a2` -20..-3, step 2, with `enabled` toggles | covered as values: each tier is searched independently, so the common near/wide brackets are reachable but not as one joint choice | none (a joint choice gene would shrink the space but is a different search) |
| small_rating (S2, S6) | bracket | n/a | no tiered entry bracket in S2; S6 uses an order-price TP/SL | not applicable | none |
| analyst_targets (DS, SQM cell) | hold | 15/30/60 | SQM `cond:xt:value` 20-120 step 10 | range: 15 missing | `explr`: SQM 15-120 step 5 |
| analyst_targets (DS) | target window | 30/60/90 (90) | none | missing | built in 2.8 (`dsfw`): `model:analyst_target_window_days` 30-90 step 30 |
| analyst_targets (DS) | minimum observations | 3/5 (3) | none | missing | built in 2.8 (`dsfw`): `model:analyst_min_targets` 3-5 step 2 |
| analyst_targets (DS) | analyst/technical 80/20 | w_analyst 0.8, w_technical 0.2 | `w_analyst` max 0.4 | range | built in 2.8 (`dsfw`): max 0.8 |
| analyst_targets (DS) | `aw_targets` 1.0 / `aw_grades` 0.0 | fixed by the family | DS defaults 0.5/0.5, not genes | missing | built (`dsfw`): `model:aw_targets`, `model:aw_grades` 0-1 step 0.1 |

What `--search-exploration-ranges` (name token `explr`) does, and nothing else:
- widens the time-exit threshold of FMPInsiderClusterBuy S1, DeterministicScorer S1/S2/SQM, FMPRating S2 and
  FMPEarningsDrift S2 to the lattices in the table (each template default sits on the new lattice; a test pins it);
- gives FMPEarningsDrift S5 a toggleable time exit placed with the closes, before the always-matching floor stop (first
  match wins, so the floor stop stays last). "Off" is the toggle, the unlimited-hold control;
- is defined only for the expert/strategy pairs above (the launcher refuses any other pair, and the capband driver
  forwards it only to those); requires `explr` in `--name-suffix`; folds into the job-name digest; without it every
  gene space and run name is unchanged.

The genes are ordinary rule genes (`cond:*:value`, `exit:*:enabled`), so the trial config, the TOP-N re-run, the
export and the deploy conversion need no new path. A test round-trips the S5 time exit through export and the live
conversion.

Open points: (1) the SQM cell takes no
market-profile genes, so the market-gate dimensions of the other cells do not apply to it.

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
- **Whole grid:** the 36 cells weigh about 116 FMPRating cells (the SQM cell adds one DeterministicScorer cell, about 4), so roughly 10 weeks on remote227 alone. This could be
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

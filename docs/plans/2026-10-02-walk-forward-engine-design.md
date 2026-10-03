# Walk-forward validation engine (design)

## Purpose
Validate any `ba2-test optimize` job (stock or option) out of sample. A GA winner is the best of
thousands of tries on one window; walk-forward asks whether that choice would have held up on
data the search never saw, by retraining on an expanding window and testing on the next one.

Code: `tools/run_walk_forward.py` (CLI, real runner) and
`testplatform/backend/app/services/walk_forward.py` (pure logic: folds, validation, aggregation,
verdict, report, orchestration over a `Runner` protocol). Tests:
`testplatform/backend/tests/test_walk_forward.py` (fake runner; nothing is optimized).

## Folds
Anchored, expanding. `--train-start 2020-01-01 --test-years 2024,2025,2026` gives train
2020-01-01..2023-12-31 / test 2024, train ..2024-12-31 / test 2025, train ..2025-12-31 / test 2026.
`--test-end` truncates the last test year. `--fold TS:TE:XS:XE` (repeatable) gives explicit folds.
`--embargo-days N` keeps N calendar days between train end and test start (year folds: the train
end moves back, the test year stays whole; explicit folds: the gap is validated). Refused loudly:
empty windows, test not after train end, folds out of order, overlapping test windows,
`--start/--end/--name` among the optimize args, a missing threshold, `--pass-min-folds` > folds.

## Per fold
1. Train: `ba2-test optimize <args> --start <train_start> --end <train_end> --name <prefix>-wf<k>`.
   Skipped when a StrategyOptimization of that name is already `completed` (the rule of
   `tools/run_options2_matrix.py`). `--skip-train` requires it to exist.
2. Select: the top `--top-n` behaviour-distinct genomes via `persist_distinct_topn.select_picks`
   (extracted from that tool's `main`: same function, same defaults and knobs).
3. Test: each genome is re-run once on the test window by
   `ba2test_launcher._persist_top_backtests(..., window=(start, end))`: the same `decode_params` +
   `_build_daily_trial_config` path that persists any TOP-N, with only the run window replaced
   (the screener hoisted state is derived from the replaced window). Row name
   `WF<k>-OOS-R<rank>-<train job>`, labels = the job's labels + `WalkForward`, `wf-fold-<k>`,
   `OOS`. A completed row of the same name, window and genome is reused on resume. Summary columns
   are read by query; blobs are never touched.
4. Record IS (GA record: fitness, return, CAR, max DD, trades) and OOS (return, CAR, max DD,
   trades). IS and OOS CAR are both `annualise(total_return, (end-start).days/365.25)`.

## Report (`reports/walk_forward/<prefix>/report.{json,md}`)
Per-fold IS/OOS table; per-rank stitched OOS (fold returns compounded, CAR over the summed test
years, deepest fold DD, total trades, IS CAR = mean of the folds' IS CARs); the rank-1-per-fold
stitch (what deploying each fold's winner would have returned). The stitched "worst DD" is the worst SINGLE-FOLD drawdown, not a chained one (fold equity curves are not read). `--pass-min-oos-return` is a per-fold TOTAL return, not annualised; fold lengths are printed next to it. The selection knobs used are written to the report; efficiency = OOS CAR / IS CAR per
genome and for stitches (n/a when IS CAR <= 0); the thresholds used.

## Verdict
A genome passes a fold iff OOS trades > 0, OOS return >= `--pass-min-oos-return`, and OOS |max DD|
<= `--pass-max-dd-mult` x IS |max DD|. Overall: at least `--pass-min-folds` fold WINNERS (rank 1)
pass. All three flags are required, no defaults. Zero OOS trades always fails. A missing metric
raises. `--only-fold` gives no overall verdict (reported as INCOMPLETE). Exit codes: 0
pass/partial/dry-run, 1 run failure, 2 refusal, 3 FAIL.

## Why not run_genome_once
`tools/run_genome_once.py` also supports a window override, but it passes only
`{"backtest_cfg": ...}` as hoisted state, so it runs the STATIC universe with no screener state and
does NOT reproduce the GA. Measured 2026-10-02: opt 375 rank 1 on H1 2026 gave 157 trades / -0.33% /
DD -5.1% there, against 61 trades / -0.27% / DD -2.5% through `_persist_top_backtests`, which is what
the GA does. The engine therefore re-runs through `_persist_top_backtests(window=...)`;
`run_genome_once` now prints a warning and says so in its docstring (behaviour unchanged).

## Out-of-sample rows are not in-sample rows
OOS Backtests carry the train optimization's id, status `completed` and the same genes. Consumers of
"an optimization's backtests" therefore exclude them: `persist_distinct_topn._existing_rows`
(window equal to the optimization's AND not an OOS row, so `--skip-already-persisted` cannot treat an
OOS row as the in-sample one), `robustness_label_topn`, `report_market_conditions.persisted_runs`,
`report_grid_results` (name pattern). The UI re-run (`rerun_handler._build_optimization_rerun_config`)
honours the row's own start/end, so re-running a `WF<k>-OOS-...` row re-runs its test window.
Helpers: `distinct_topn.is_oos_row`, `row_in_window`.

## Look-ahead and duplicate-name doors
The optimize args may not carry `--start/--end/--name`, `--warm-start-from` (imports another job's
population, which may have seen the test window), `--rerun` (a second optimization under the same
name) or `--submit` (returns before the job completes). A name shared by two optimizations, a
non-completed optimization or a non-completed OOS row of the expected name are refused up front with
the ids and what to do.

## Option holdout rail
`_assert_option_window_excludes_holdout` guards only the optimize CLI handlers (`cmd_optimize`,
`optimize-batch`). It is unchanged and not parametrised. Training stops at 2025-12-31 and the 2026
test is a re-run through `_persist_top_backtests`, which never consults the rail, so it is not hit.
A fold whose TRAIN window reaches 2026 for a pure-option strategy is still refused (the tool
pre-flights this with the launcher's own function, using `--strategy` from the optimize args, and
the launcher refuses it again at train time). Tests pin both.

## Seeding extension point (not implemented)
`walk_forward.initial_population_for_fold(fold) -> None`. When the seed population is defined it is
rendered into the fold's optimize command inside `train_command`; a non-None return raises
NotImplementedError until that rendering exists. There is no `--seed` flag.

## Cost notes
Training dominates (one full GA per fold; later folds are longer). Each OOS re-run is one trial,
~14 GB RAM and minutes; `--test-parallel` defaults to 1. Re-runs are local only, outside any
grid's memory governor (same warning as `persist_distinct_topn.py`; its `--min-free-gb` floor
applies). Folds run sequentially; a crash resumes from the last completed train job / OOS row.
Persisted OOS rows carry the train genome's `ga_fitness`; the GA-fitness fidelity gate is skipped
for them (different window).

## Running (examples, not run)
Stock:
```
python tools/run_walk_forward.py --prefix wf-fr-s2 --train-start 2020-01-01 \
  --test-years 2024,2025,2026 --test-end 2026-06-30 --top-n 3 \
  --pass-min-oos-return 0 --pass-max-dd-mult 1.5 --pass-min-folds 2 \
  -- --expert FMPRating --strategy S2 --universe AAPL,MSFT,NVDA --generations 30 --population 40
```
Option (train windows end by 2025-12-31; explicit folds, 90-day embargo for LEAPS):
```
python tools/run_walk_forward.py --prefix wf-leaps --top-n 2 --embargo-days 90 \
  --fold 2023-01-01:2024-09-30:2025-01-01:2025-12-31 \
  --fold 2023-01-01:2025-09-30:2026-01-01:2026-06-30 \
  --pass-min-oos-return 0 --pass-max-dd-mult 2 --pass-min-folds 1 --dry-run \
  -- --expert FMPRating --strategy O_LEAP --universe AAPL,MSFT --options-store parquet
```
Add `--dry-run` first to see the exact commands.

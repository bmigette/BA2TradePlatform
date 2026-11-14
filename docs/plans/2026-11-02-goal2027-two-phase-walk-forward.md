# goal2027: two-phase walk-forward (2020-2025 now, 2026 in January)

*Owner requests 2026-11-02 (two phases; Senate inside the grid). Amends [2026-10-25-goal2027-grid-design.md](2026-10-25-goal2027-grid-design.md)
sections 1.2, 2.5-2.7 and 4. Status: driver change built on `feat/goal2027-wf-defer`; wrapper not built.*

## Why it splits cleanly

Everything in a cell's chain except the 2026 test and the refit needs data only through 2025:

| Step | Train | Test | Phase | Data |
|---|---|---|---|---|
| wf1 | 2020-2023, cold, 30 gens | 2024 | **A (Nov-Dec 2026)** | current Alpaca store |
| wf2 | 2020-2024, seeded from wf1, 15 gens | 2025 | **A** | current store |
| wf3 train | 2020-2025, seeded from wf2, 15 gens | - | **A** | current store |
| wf3 test | - | 2026 | **B (January 2027)** | new store (asof 2027-01-04) |
| verdict, refit | 2020-2026, seeded from wf3, 15 gens, passing cells only | - | **B** | new store |

Phase A is roughly 70% of a cell's compute, so the cells done in phase A need only their 2026 test and
refit in January.

## Mechanism (built)

- `run_screener_capband_matrix.py --walk-forward --wf-defer-last-test --wf-data-end 2025-12-31`
  runs wf1 and wf2 whole and only TRAINS wf3. The cell is `PENDING`, its `cell.json` carries a `deferred`
  block, and no refit runs.
- January: the SAME command without `--wf-data-end`. Every train job is completed and the wf1/wf2 fold
  reports are stored, so only wf3's test, the verdict and the refit run.
- The cell name carries `defer` instead of the data end, so both runs share every job name. The
  flag must be on both runs. A data end INSIDE the last test year is refused under the flag (a partly
  clamped test or refit would share the full run's names).
- **Early elimination (default ON):** a cell stops as `FAIL` once it can no longer pass: a required year
  failed, or too few folds left to reach `--pass-min-folds`. Under the goal2027 rule (2 of 3, 2026
  required) a cell failing wf1 and wf2 never trains wf3. `--wf-run-all-folds` turns it off.
- Code: `wf_chain.build_cell_folds(defer_last_test=)`, `deferred_fold`, `cannot_pass`;
  `walk_forward.train_fold` (the train half of `run_fold`).

## Senate is a band of the grid (built)

Owner 2026-11-02: no separate Senate grid. `run_screener_capband_matrix.py --bands senate
--senate-universe-file <file>` runs the `FMPSenateTraderWeight` cells (strategy plan: S1, S3, S5, S6)
on the static disclosure-derived universe, with every grid flag (genes, budgets, walk-forward,
deferral) exactly as the other cells. Jobs are named `sen-<S><suffix>`; no screener tokens, no
`-sup1`/`-lds2`; the scalar stress reaches it (default widening 13 bps, the mid value). Senate runs on
the `senate` band only, and only Senate runs there. The walk-forward summary MERGES across driver
calls, so the wrapper's one-call-per-band loop (per-band spread) fills one grid summary; a rerun cell
is replaced in place. `tools/run_senate_matrix.py` stays for the old lanes only.

## Phase B reads January's data (built)

Two gaps found while integrating Senate, both affecting every cell, both fixed:

1. **Job names no longer carry data locations under `--wf-defer-last-test`.** The name digest masks
   the values of `--market-condition-manifest`, `--exclude-symbols`, `--screener-store` and `--universe`
   (`mask_data_tokens`), so January's new manifests, exclusions, store path and regenerated Senate
   universe still find phase A's jobs. Each job's row keeps the values it really ran on. Without the
   flag, a data change still means a new job, as before.
2. **Tests read the current command's data.** The out-of-sample re-run rebuilt its trial from the
   TRAIN job's stored config, only replacing the window. Under the flag, every fold's test now passes
   the current command to `_persist_top_backtests(data_rebase=)`, and `_rebase_run_data` replaces the
   data choices on the windowed block: a static universe (Senate) becomes the current one, a screener
   run keeps its stored superset and reads the current store, exclusions are the stored list PLUS the
   current one (never lifted), and manifests become the current digests, validated for coverage and
   window on the TEST window. The profile set must match the train job's; any mismatch is refused.
   Phase A's own tests are unaffected (current data = train data).

The refit is a fresh optimize from the current command, so it already used the new data.

**Rule for the boundary:** a train job must be COMPLETED before the January data replaces phase A's.
A wf3 interrupted mid-GA and resumed on the new data would mix stores inside one GA; stop at a fold
boundary (step 5 below) or delete that job and retrain it in January.

## Hosts

- **Master on this PC** (owner 2026-11-02), as the atr27 lane: `PARALLEL=0` (prod 8081, dev 8080 and
  8082 run here), run from a dedicated checkout (e.g. `BA2-goal2027-run`), never the main clone.
- **Workers are a setting:** `WORKERS=remote227` once stage 1.5 and option stage 2 free it; remote150
  can be added or swapped between cells. Changing workers never changes results (same code is enforced
  by the version check).
- Out-of-sample re-runs run on the master at about 14 GB each: `--wf-test-parallel 1` with the 20 GB
  free floor. This PC has 64 GB, shared with the live apps.

## Order and timeline

1. **Now:** merge the driver change at a job boundary (TEST bump; O_CBS is mid-job on remote227).
2. **`tools/grid_goal2027.sh` (built).** `setup` / `start` / `run` / `dry-run` / `status` / `stop`.
   `PHASE=A|B` is required (A adds `--wf-data-end 2025-12-31`; both pass `--wf-defer-last-test`).
   15 steps in the design run order, one driver call each (`STEPS=` runs a subset): fr/ds large, mid,
   ed/icb mid, small, fr/ds small, sqm-xl, FactorRanker x3, senate. Settings: `WORKERS` (default
   remote227), `PARALLEL` (0), `BA2_MAX_REMOTE_SLOTS` (28), `MC_OHLCV`/`MC_TA` required (no default:
   each phase has its own manifests), `MC_SEN_*` for the Senate step until one union manifest exists,
   `SENATE_UNIVERSE_FILE`, `EXCLUDE_FILE`, `WF_REPORT_DIR`. Preflight: the venv must import THIS
   checkout (refuses the main clone), metric store to the data end, 5-min coverage at the window start
   AND end (`check_window_coverage.py --end`, new). Run setup:
   `git -C ~/Documents/dev/BA2TradePlatform worktree add --detach ../BA2-goal2027-run <ref>`, then
   `bash tools/grid_goal2027.sh setup` there (uv venv, Python 3.12, editable packages + testplatform).
   Dry-run verified 2026-11-02 on fr-large, ds-large, sqm-xl, rank-large, senate (12 cells).
   **SPY regime gate for stocks (design 2.3): built 2026-11-02, in every classic step** (`-spyreg`).
   `--benchmark-regime-gates` now accepts S1-S7: `_append_equity_benchmark_regime_gates` appends
   `<kind>-spy-trend` / `<kind>-spy-vol` LAST to every entry tree (S1's tiers share one pair), never to
   an exit; independent of the market-profile gene; refused for SQM and for a strategy with no gate
   (bypass). Backtest calendar, trial config, live publish and export were already strategy-agnostic
   (tests added for the equity path). Real smoke 2026-11-02 (DS S2, 15 large caps, 2023-2025-06, scratch
   DB): gated trials trade (`off`/`not_calm` 55 trades) and `bear_rally`+`stressed` blocks every entry.
3. **First cell = rehearsal:** FMPRating large. Its minutes per generation set how far phase A gets.
4. **Phase A, design run order:** FMPRating + DeterministicScorer large/mid (traded live), then
   EarningsDrift + InsiderCluster mid/small, then the small caps, then FactorRanker.
5. **About 2026-12-20:** start no new cell; let the running one reach a fold boundary.
6. **January 1-9:** design section 3 data steps (re-download about 4 days, build, verify, metric store,
   manifests, Senate shards, prewarm, push to the workers).
7. **About January 10, phase B:** rerun every phase A cell without `--wf-data-end`, then full chains for
   the cells not reached, Senate (the `senate` band) last.

## Accepted trade-offs and rules

- **Two data stores per cell.** wf1-wf3 train on the October 2026 store; the 2026 test and the refit use
  the January store, whose split adjustments can be re-based by late-2026 splits. Seeds and tested
  genomes are parameters, so this is accepted rather than retraining wf3 in January.
- **Code freeze per cell:** a GA-affecting change (minimum raise) lands only between cells. If one lands
  between phase A and B, the 2026 tests run on newer code than the training; record it in the report.
- **Senate cells need Senate-covering market-condition manifests:** build the January manifests over
  the band union PLUS the Senate universe, so one manifest pair serves every cell.

## Open for later

- **Iterating every year:** the 2020-2026 refit is exactly a fold-4 training for a 2027 test. Naming
  and storing it as `-wf4` would make January 2028 a pure "test 2027, refit 2020-2027". The pass rule
  would then need a form such as "latest year required, 2 of the last 3". Decide before January 2027's
  refits run, since the name is fixed then.

## Master on remote227 and copy-back (owner 2026-11-02)

The master runs on remote227, away from Windows Update restarts. Its layout follows the stage-1/2
masters, per grid2020-remote227:
- its own checkout (e.g. `/home/debian/ba2-grid/repo-goal2027`), with the fleet venv and PYTHONPATH
  isolation;
- its own `BA2_HOME` with its own test DB; `common` is a symlink to the workers' cache;
- a `sudo systemd-run` transient unit with a `MemoryMax` cap.

`tools/grid_goal2027.sh` enforces all of this.

Results come back with `tools/merge_remote_test_db.py`:
1. Copy the remote `dl_forecasting.db` and the `reports/goal2027_wf` directory to the PC.
2. Run `--source <copy> --source-tag remote227-goal2027 --name-like '%goal2027%' --rewrite-reports <copied reports dir>`.

How the merge works:
- Every row gets `source id + a per-table offset` (the next multiple of 1000). The offsets and every
  mapped pair are recorded in the local DB, so the January copy adds only the new rows, under the same ids.
- Still-running jobs are skipped until a later copy.
- It runs in one transaction, and the source is attached read-only.

## Resume of an interrupted fold (built 2026-11-14, TEST 2026.11.0201)

`grid_goal2027.sh stop` kills the whole cgroup, so a fold's training job is left `running`. The
optimizer has long resumed from its name-keyed checkpoint, but the walk-forward driver refused any
non-completed row (cell ERROR, row deleted by hand). Now `RealRunner.run_train` relaunches such a job
with `optimize --resume-interrupted` when its runner is provably dead
(`app/services/optimization_resume.py`):

- **Liveness (`judge`).** The handler records each row's runner in `strategy_optimization_runners`
  (migration 033): host, pid, process start time, and an independent identity (Linux boot id plus the
  kernel start tick from `/proc/<pid>/stat`, and a hash of the command line), with the code versions
  and a random run token. **Dead** means the pid is gone, or that identity proves the pid was reused
  (other boot, other start tick, other command line). A pid that still exists with a start time that
  does not match, and no such proof, is **unknown** and refused: a clock step on a VM moves psutil's
  Linux start time, so a mismatch alone is not proof. A live runner, another host, a row with no runner
  record (started by older code), a failed or cancelled row and duplicate names are refused with the
  reason.
- **Same job.** The launcher (`_create_resumed_row`) compares the whole optimization config except
  `seededFrom`, `resumedFrom`, `backtest.backtest_id` and `parallelIndividuals` (window, data digests,
  universe and exclusions included). It also compares the chain-seed source the row really used
  (`seededFrom`), the metric, the decoded gene space, the strategy rules and the code versions (TEST
  version and package minimums; `BA2_RESUME_ACROSS_CODE_CHANGE=1` overrides only these, and the new row
  records both versions in `optimization_config.resumedFrom`). Any difference is refused. A phase-A wf3
  resumed on January's manifests is therefore refused, which enforces the boundary rule above.
- **Atomic claim.** The new row's INSERT and ONE guarded `UPDATE ... WHERE id=? AND name=? AND status IN
  ('running','pending')` (rename to `<name>~interrupted-<id>`, status `failed`) run in one transaction,
  and the UPDATE must change exactly one row. A second starter matches no row; it rolls back (its INSERT
  included) and is refused.
- **Checkpoint ownership.** Every checkpoint carries the writing row's id, its run token and the job
  identity hash (config incl. window and data digests, metric, gene space, rules). A resume uses only a
  checkpoint of the interrupted row or of a row it continues (id AND token match: SQLite reuses the id of
  a deleted max-id row), and refuses one of ours without a fingerprint, or with another fingerprint or
  identity. A checkpoint of ANY OTHER row means the interrupted run died before its first checkpoint.
  It is discarded with a log line and the run starts clean. Refusing would block a case whose correct
  answer is known: a clean start is exactly where that run was.
- **Plain launches** (no flag) still resume a same-name checkpoint, but only while the row that wrote
  it exists under that name. An orphaned checkpoint (row deleted by hand, or retired by a resume) or one
  of another job identity is discarded with a log line. Before this change it was resumed, silently,
  with only its 20 elites in `all_results`.
- **Resume.** All of the dead row's trial records are carried into `all_results` and the trial memo,
  so no finished trial runs again. With no checkpoint, or an exhausted one (killed after the last
  generation, before completing), the search is replayed from generation 0 against the memo. The draws
  are the same and no trial runs again (tested).
- **Identical to an uninterrupted run:** the population, the GA/global/numpy RNG states, history and
  the patience counter, the best genome, the final population (the chain seed), and the set of trials
  in `all_results` (so the top-N selection) are the same. Tests:
  `test_optimization_resume_handler.py` and `test_ga_resume_equals_uninterrupted.py`. Not identical: the
  order of `all_results` (parallel completion order is never deterministic), a trial running at the
  kill (re-run), a stalled trial (re-run), and the end-of-job TOP-N rows (re-run against the fidelity
  gate, because the final generation's buffered results died with the process).
- **Fixed on the way:** a PARTIAL checkpoint written by a generation's last trial was resumed as an
  end-of-generation one, which dropped that generation's record.
- **Other stages.** Re-run Backtest rows (TOP-N, out-of-sample) record their writer in
  `backtest_writers`. A half-written row is retired only when that writer is proven dead; a live or
  unknown writer, which can be an orphaned persist after only the driver was killed, is refused. If
  the TOP-N persist after training is cut off, the job is completed but `topn_done_at` is NULL. With
  its runner dead, the driver relaunches with the flag, which re-runs only the missing `TOP<rank>`
  rows with the uninterrupted ranking. Each cell runs under an OS file lock (`<report_dir>/<cell>/.lock`,
  `fcntl.flock` / `msvcrt.locking`, released by the OS when the holder dies; the file's content only
  names the holder). Fold reports, `cell.json` and the summary are written atomically.
- **Remote workers:** not tested. The master re-dispatches, and the memo means finished trials are
  not re-sent. A remote worker's trial still running for the killed master is not cancelled (nobody
  is left to cancel it), so it occupies a slot until it ends; its result is never read.

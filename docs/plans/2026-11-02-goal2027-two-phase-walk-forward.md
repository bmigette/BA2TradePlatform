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
- **Atomic claim.** The new row's INSERT, its runner record (with `resumed_from_id`) and ONE guarded
  `UPDATE ... WHERE id=? AND name=? AND status IN ('running','pending')` (rename to
  `<name>~interrupted-<id>`, status `failed`) run in one transaction, and the UPDATE must change exactly
  one row. A second starter matches no row; it rolls back (its INSERT included) and is refused. A start
  killed at any point after the claim leaves a `pending` row that has a runner and a link to the row it
  continues, so it is itself resumable. The handler then seeds from the newest row of the lineage that
  holds trial records. Every CLI launch (not `--submit`) registers the runner with the new row.
- **One run, one token.** The same process registering again (the handler after the launcher, or a
  task retry) keeps the token, so the row's own checkpoints stay its own. A different process is refused
  and the row is left untouched; it can only continue the row by claiming it. So is a row that was
  claimed or retired, even by the same process: a paused serve task resumed after its row was claimed
  refuses. A handler that returns records `ended_at` and `ended_as`; a kill never reaches that line.
- **Paused runs (decision).** A run that returned PAUSED in a process that still lives is not
  resumable elsewhere: `assess` and the plain launch refuse it as PAUSED. Its task can resume it there,
  or the operator cancels it. Once its process is dead it is resumable like any killed run.
- **Records never outlive their row.** Runner and writer records are bound to their row: an
  optimization's random `resumeRowKey` is stored in its config and on the record; a Backtest's creation
  time and name. A record that does not match is purged on read. Migration 033 also adds AFTER DELETE
  triggers that remove the record with its row, and purges records already orphaned. A hand DELETE
  followed by a new row that reuses the id (SQLite reuses a deleted max id) starts clean. That holds for
  the same pid or another, with or without the triggers (tested).
- **No two non-terminal rows of one name.** A launch without the flag (other grids, a hand launch)
  CLAIMS a dead running or pending row of its name, with the same identity check. It supersedes a
  `pending` row that never ran (no trial, no checkpoint, not a continuation), and refuses beside a
  live, paused or unprovable one. `--submit` refuses beside a dead one: the claim needs the process
  that runs the row. Terminal rows (completed, failed, cancelled) are not in the way, as before. The
  handler refuses to continue a checkpoint whose writer row is still non-terminal and unclaimed.
- **Abandoning a job: `ba2-test abandon-interrupted --id N | --name X | --backtest-id N --reason ...
  [--confirm-not-running]`.** It replaces hand DELETEs, which caused the confusion of 2026-11-14. The row
  is renamed `<name>~abandoned-<id>`, set `failed` with the reason and kept, its run is marked ended,
  and its own checkpoint is retired (payload kept). It is refused for a live or paused run, and needs
  `--confirm-not-running` for a run it cannot judge. It prints the cost: the finished trials are not
  reused, and the next start trains from generation 0. Every refusal message names this command and
  that cost.
- **Kill points before the first trial.** Between the claim and the handler: the `pending` row has its
  runner and link, so it is resumable (tested). In the handler before the first trial, the long wait of
  a remote-only job (0 local slots) is `DistributedEvaluator.start` (worker version check and cache
  push). The row is `running` with a runner and no trial or checkpoint, and the next start claims it
  and ends with the uninterrupted result (tested). A `--submit` row waiting in the serve queue has no
  runner until serve starts it: it is refused as unprovable (it may still run). Give it up with
  `--confirm-not-running` once that serve is known to be gone.
- **Strategy rows** are deleted when a claim is refused. A launcher killed between creating its
  Strategy row and the claim leaves an unreferenced Strategy row, as every killed launch always has:
  harmless, and not cleaned up.
- **Checkpoint ownership.** Every checkpoint carries the writing row's id, its run token and the job
  identity hash (config incl. window and data digests, metric, gene space, rules); id AND token must
  match, because SQLite reuses the id of a deleted max-id row. `_checkpoint_verdict` decides, and every
  cell is tested with the real handler in `tests/test_checkpoint_matrix.py`:

  | launch \ checkpoint  | none   | own                          | orphan (writer gone) | another LIVE run's |
  |----------------------|--------|------------------------------|----------------------|--------------------|
  | fresh start          | gen 0  | resume (retry of this row)   | retire, gen 0        | refuse, untouched  |
  | plain relaunch       | gen 0  | resume (the dead prior run)  | retire, gen 0        | refuse, untouched  |
  | --resume-interrupted | replay | resume (strict checks)       | retire, replay       | refuse, untouched  |

  Under `--resume-interrupted` a checkpoint of ours that has no fingerprint, or another fingerprint or
  identity, fails the job. A checkpoint of any other dead run means the interrupted run died before its
  first checkpoint, so the run starts clean; refusing would block a case whose correct answer is known.
  Before this change a plain launch resumed an orphaned checkpoint silently, with only its 20 elites in
  `all_results`.
- **Retire, never delete.** A checkpoint that is not used (orphaned, another identity or search, or
  exhausted) moves with its payload to a COMPLETED task row `<task_id>-r<8 hex>`. The new row records
  what, whose and why in `optimization_config.discardedCheckpoints`.
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
  names the holder). The lock belongs to the DRIVER process only: the fd is non-inheritable, the
  launcher runs as an exec'd subprocess and the re-run pools are spawn-based. Decision: an orphan the
  killed driver left (an optimize or persist still running) does NOT block the next driver at the cell
  level. Its own runner or writer record refuses exactly the resume that would touch its work (cell
  ERROR, "LIVE process"). That is precise, and it never blocks on a harmless orphan. Fold reports,
  `cell.json` and the summary are written atomically.
- **Schema.** Migration 033 converges from every starting state, idempotently: no table, the shape of
  its first version (missing columns ADDed, no table rewrite) or the current one. `init_db()` runs the
  same reconciliation after `create_all` (which never alters a table), so the CLI path ends at the same
  schema (tested).
- **Remote workers:** not tested. The master re-dispatches, and the memo means finished trials are
  not re-sent. A remote worker's trial still running for the killed master is not cancelled (nobody
  is left to cancel it), so it occupies a slot until it ends; its result is never read.

### Operator runbook: stop, start, refusals

**Stop / start.** `bash tools/grid_goal2027.sh stop` kills everything. `start`, with the same PHASE
and settings from the same checkout, continues where the grid stopped:

| Where the stop landed | What the next start does |
|---|---|
| A fold's training (any generation, also before the first trial) | Resumes the job. The dead row becomes `<name>~interrupted-<id>`, and a new row continues from the last checkpoint with every finished trial; nothing finished runs again |
| The TOP-N persist after training | Re-runs only the missing `TOP<rank>` rows |
| An out-of-sample test | Re-runs only the genomes whose result was not saved |
| Between folds / reports | Reuses the stored fold reports (written whole or not at all) |

**Refusals.** The cell goes to ERROR and the log line says which refusal. Never edit or delete rows by
hand.

| Message says | Meaning | Way out |
|---|---|---|
| `LIVE process` | Another process still runs that job (a second grid, an orphan) | Let it finish, or stop it (`grid_goal2027.sh stop`), then `start`: it resumes, nothing is lost |
| `PAUSED` | A serve task paused the job and that serve still runs | Resume or cancel it in the UI, or stop that serve; then `start` |
| `UNKNOWN` (start time differs, nothing proves reuse) | Cannot tell whether pid N is still the job | Check `ps -p N -o pid,lstart,args`; stop it if it is the job, then `start` |
| `another host` | The row belongs to another master | Resume it on that host |
| `no runner recorded` / `pending with no runner yet` | Started by older code, or a queued `--submit` task | Check the serve task queue first (UI: Backtesting -> History -> Running jobs) and cancel any queued task; then, if no process holds it, abandon it (below) |
| `is NOT the job this command describes` (lists the differences) | The config, gene space, data digests, rules or code changed since the job started | Run the original command (it resumes), use a new name, or abandon |
| `code app_version` / `required_package_versions` | The code changed under the job | See `BA2_RESUME_ACROSS_CODE_CHANGE` below, finish on the old checkout, or abandon |
| `writer is ALIVE / UNKNOWN` (backtest) | A live process may still be saving a re-run row | Let it finish or stop it; if no process writes it, abandon the backtest row |
| `failed` / `cancelled` | The job failed, and the failure may repeat | Read the error and fix the cause, then abandon to retrain |

**Abandon (the only supported way to give a job up):**

    $PY testplatform/ba2test_launcher.py abandon-interrupted --id <opt_id> --reason "<why>"
    $PY testplatform/ba2test_launcher.py abandon-interrupted --backtest-id <bt_id> --confirm-not-running --reason "<why>"

Add `--confirm-not-running` only after checking that no process, and no queued task, holds the job.
Running it twice is harmless. The cost: the job's finished trials are not reused, its checkpoint is
retired (payload kept in `task_queue`), and the next start trains from generation 0.

**`BA2_RESUME_ACROSS_CODE_CHANGE=1`** lifts only the code-version check; `start` passes it to the
systemd unit. It is acceptable only when the new code cannot change THIS job's trial results: a fix in
reporting or in the driver, a broker-only or GA-neutral package change (`ga_neutral_package_paths.py`),
or docs. Never use it for a change to the backtest engine, an expert, the fitness, the gene space, or
anything that raised a minimum in `required_package_versions.py`. The new row then records both
versions in `optimization_config.resumedFrom`.

### End-to-end test recipe (Linux, scratch BA2_HOME, local slots only)

The recipe runs one small job twice: once uninterrupted, and once killed with SIGKILL in the middle of
a generation and then restarted. Both must end with the same best genome and fitness, the same final
population and the same set of trials. It takes about 20-40 minutes. Paste the commands into a shell
on the master host (example: remote227).

```bash
# 1. a checkout of the branch and a SCRATCH home (its own DB; the cache is the shared one)
export REPO=/home/debian/ba2-grid/repo-wfresume            # git worktree at feat/wf-resume
export PY=/opt/ba2worker/ba2-venvs/test/bin/python
export BA2_HOME=/home/debian/ba2-scratch-wfresume
mkdir -p $BA2_HOME/test && ln -sfn /opt/ba2worker/Documents/ba2/common $BA2_HOME/common
export DB_FILE=$BA2_HOME/test/dl_forecasting.db DATABASE_URL=sqlite:///$BA2_HOME/test/dl_forecasting.db
export PYTHONPATH=$REPO/packages/common:$REPO/packages/providers:$REPO/packages/experts:$REPO/testplatform/backend:$REPO/testplatform
cd $REPO
$PY -c 'import app, ba2test_launcher; print(app.__file__, ba2test_launcher.__file__)'   # must be under $REPO

# 2. the FMP key, copied from the grid master's DB (never printed)
MASTER_DB=/home/debian/ba2-grid/home-goal2027/test/dl_forecasting.db
$PY -c 'from app.models.database import init_db; init_db()'
sqlite3 $DB_FILE "ATTACH '$MASTER_DB' AS m; INSERT OR REPLACE INTO appsetting SELECT * FROM m.appsetting WHERE key='FMP_API_KEY';"

# 3. the job (local slots only: no --workers); identical for both runs except the name
JOB="optimize --expert FMPRating --strategy S2 --universe AAPL,MSFT,NVDA,JPM,XOM \
  --start 2023-01-01 --end 2024-12-31 --population 16 --generations 4 --parallel 4 --seed 42 \
  --fitness consistent_annual_return --estimates-source fmp --save-top 2"

# 4. the uninterrupted reference run (foreground)
$PY testplatform/ba2test_launcher.py $JOB --name e2e-whole 2>&1 | tee e2e-whole.log

# 5. the same job as a transient unit, killed with SIGKILL in the middle of generation 1
sudo systemd-run --unit=wfresume-e2e -p User=$(id -un) --working-directory=$REPO \
  --setenv=PYTHONPATH=$PYTHONPATH --setenv=BA2_HOME=$BA2_HOME --setenv=DB_FILE=$DB_FILE \
  --setenv=DATABASE_URL=$DATABASE_URL --setenv=HOME=$HOME \
  $PY testplatform/ba2test_launcher.py $JOB --name e2e-cut
journalctl -u wfresume-e2e -f -n 0 | grep -m1 "Gen 0:"     # generation 0 is checkpointed
sleep 45                                                   # now inside generation 1
sudo systemctl kill -s KILL wfresume-e2e; sudo systemctl reset-failed wfresume-e2e

# 6. restart the SAME command. It must print "RESUMING interrupted e2e-cut": the plain launch claims
#    the dead row (the grid's driver passes --resume-interrupted, which behaves the same)
$PY testplatform/ba2test_launcher.py $JOB --name e2e-cut 2>&1 | tee e2e-cut.log
grep -E "RESUMING|continuing optimization|DISCARDING" e2e-cut.log

# 7. compare the two runs, and check that the dead row was kept
cat > /tmp/e2e_compare.py <<'PYEOF'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
def get(name):
    oid, st, fit, params, res = c.execute(
        "SELECT id, status, best_fitness, best_params, all_results FROM strategy_optimizations "
        "WHERE name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    pop = c.execute("SELECT population FROM strategy_optimization_populations "
                    "WHERE optimization_id=?", (oid,)).fetchone()
    return st, fit, json.loads(params), sorted(r["key"] for r in json.loads(res)), json.loads(pop[0])
w, k = get("e2e-whole"), get("e2e-cut")
for label, a, b in zip(("status", "best_fitness", "best_params", "trial keys", "final population"), w, k):
    print(f"{label:17s} {'SAME' if a == b else 'DIFFERENT'}")
print(c.execute("SELECT id, name, status FROM strategy_optimizations "
                "WHERE name LIKE 'e2e-cut%'").fetchall())
PYEOF
$PY /tmp/e2e_compare.py "$DB_FILE"
```

Expected: every line reads `SAME`, and there are two `e2e-cut` rows: `e2e-cut~interrupted-<id>`
(failed, kept) and `e2e-cut` (completed). If only the trial keys differ while the best result and the
population match, a trial stalled or crashed in one run only (an environment issue, not the resume);
look for `STALLED` or `trial errors` in the logs. Clean up with `rm -rf $BA2_HOME /tmp/e2e_compare.py`.

## Option walk-forward (built 2026-11-14, TEST 2026.11.0202; nothing launched yet)

`tools/run_options_matrix.py --walk-forward` turns every option job (expert x structure, and each
neutral arm) into a walk-forward CELL. It reuses the stock grid's engine unchanged:
`app.services.wf_chain` (`CellSpec`, `run_cells`, fold store, early elimination, deferral, refit)
with `tools/run_walk_forward.py`'s `RealRunner` (selection by `persist_distinct_topn`, out-of-sample
re-run by `ba2test_launcher._persist_top_backtests(window=)`). Only the driver side is new; the shared
`wf_optimize_args` moved to `tools/matrix_flags.py` (the stock driver re-exports it, same behaviour).
The plain mode (no `--walk-forward`) is unchanged: same names, digests and commands.

**Fold defaults (the stock phase-A layout).** `--wf-train-start 2020-01-01`, `--wf-test-years
2024,2025,2026`, `--wf-first-generations 30`, `--wf-next-generations 15`. ThetaData (floor about 8
years back) and the Alpha Vantage store (floor 2020-01-01, downloaded through 2025-12-31) both reach
2020, so the option data window does not force another layout. A store with a later floor (alpaca
2024-01-18, tastytrade 2022-10-01) is refused by the launcher at fold 1; give it `--wf-train-start`
and fewer `--wf-test-years`. No FMPRating floor is applied: the option driver has always run
FMPRating from 2020 (the stock grid floors it at 2022-01-01); pass `--wf-train-start 2022-01-01` for
an FMPRating-only run if wanted.

**The 2026 holdout rail is respected, not moved.** Before anything runs, the plan is refused when a
fold TRAINS into `_OPTION_HOLDOUT_START` (read from the launcher the jobs run) or when a passing cell
could REFIT in this run (no deferred last fold), since the refit trains to the last test end. The
out-of-sample TESTS may sit in 2026: they are single re-runs, which the rail deliberately does not
guard. So:
- **Phase A (now):** `--wf-defer-last-test --wf-data-end 2025-12-31`. wf1 and wf2 are trained and
  tested (2024, 2025), wf3 is trained on 2020-2025, cells end `PENDING`.
- **Phase B (January 2027):** the same command without `--wf-data-end`, ONLY after
  `_OPTION_HOLDOUT_START` has moved as a reviewed change; until then that command is refused with
  this explanation. The 2026 test, the verdict and the 2020-2026 refit then run.
- The option data for 2026 (Alpha Vantage/ThetaData chains, the gate store, the manifests) must
  exist before phase B; the driver does not check option-store coverage.

**Names.** `optwf-<expert>-<structure><spread tag><suffix><vendor tag>[-<arm>-rules1]-d<digest>`,
never an `optm-` name. The digest covers the cell's base optimize args (no window, name or
generation budget; `--labels`, `--parallel`, `--workers` excluded as in discovery names), the store
environment, the launcher path, option-only fixed settings and the stock grid's walk-forward spec
tokens (train start, test years, defer or data end, budgets, top-N, pass rule). Under
`--wf-defer-last-test` the data VALUES and LOCATIONS are masked (manifests, universe list,
`BA2_HOME`, parquet root, launcher path), so phase B rebuilds phase A's names, and the deferred tests
read the current command's data (`_rebase_run_data`, which rebases exactly the manifests and a
static universe). `--screener-gate-store` is NOT masked: the rebase does not replace it, so a test
would silently read the train job's gate store. A new gate-store path is a new cell; refresh the
store in place to keep the names.

**Trade-count gates (decision, conservative).** Nothing new and nothing tuned for options: the
selection floor stays `persist_distinct_topn`'s 30 in-sample round-trip rows (one per option LEG)
over the fold's train window (4-6 years), and a genome with ZERO out-of-sample trades fails its
fold, as for stocks. Evidence (test DB, 2026-11-14): 21 of 23 option `TOP1-optm-*` rows over
2020-2025 have more than 30 rows (median 294; the lowest are a pilot with 2 and O_LP with 9). A
structure that thin errors in fold 1 ("no eligible genome") rather than passing on a handful of
trades. `--wf-min-trade-rows N` overrides the floor (not part of the cell name; stored fold reports
re-evaluate). No minimum out-of-sample trade count beyond zero was added: that would be a change to
the shared verdict (`walk_forward.fail_reasons`) and to the stock grid as well.

**Out-of-sample path.** The test of a fold's genome is the GA's own trial assembly on the test window:
`select` takes the genome raw from the train job's `all_results`, `_persist_top_backtests(window=)`
decodes it against the job's stored strategy and builds the trial with `_build_daily_trial_config`.
`tests/test_option_wf_oos_rerun_genes.py` pins it for a launcher-built O_BULLCS with every searched
gene off its default: the re-run's trial config equals the GA's for that genome on the test window,
every gene (the `optsel` selection weights included) moves it, and the persisted out-of-sample row
carries the whole genome. A mutation that strips the `optsel` genes on that path fails two of the
tests. The legacy `rerun_handler` path (UI Re-run) is not used by the walk-forward.

**Launch (dry run first; same flags for phase A and B apart from `--wf-data-end`):**

```bash
python tools/run_options_matrix.py --profile discovery --experts DeterministicScorer --strategies O_BULLCS \
  --screener-gate-store <store> --max-stock-price 0 --estimates-source fmp \
  [--option-history-source alphavantage] [--fitness option_car_target_soft30] [--workers <name>] \
  --walk-forward --wf-defer-last-test --wf-data-end 2025-12-31 \
  --wf-top-n 3 --wf-report-dir reports/option_wf \
  --pass-min-oos-return 0 --pass-max-dd-mult 2.5 --pass-max-oos-dd 35 --pass-min-folds 2 \
  --dry-run
```

`--start`, `--end` and `--generations` are refused with `--walk-forward` (the chain sets them per
job); every other option of the driver (profile, store, gates, market conditions, fitness, capital)
rides on every fold. Reports: `<--wf-report-dir>/<cell>/{fold<k>.json,cell.json,report.md}` and the
grid `summary.{json,md}`. Resume, stop and refusals follow the runbook above (the runner is the same).

**Not covered.**
- The small-accounts chain (`tools/grid_option_small_accounts.sh`, unmerged) does not go through
  `run_options_matrix.py`: each job is `tools/option_style_job.sh`, which execs the launcher with
  `--strategy O_STYLE --style ... --seed-population <bundle>`. The option driver has no O_STYLE /
  style-config / seed-bundle support, and `--seed-population` is refused together with
  `--chain-seed-from` (so a seeded style job cannot be chained), and stage-1 seeds chosen on
  2020-2025 have seen the 2024 and 2025 test years (look-ahead for folds 1-2). Walk-forward for those
  jobs needs its own decision: unseeded folds, or seeds restricted to each fold's train window.
- Nothing was launched: no real optimization, no real out-of-sample re-run, no remote worker.

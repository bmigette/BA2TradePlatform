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

## Blocking for phase B (build in November, before January)

Found while integrating Senate; affects every cell, not only Senate:

1. **Job names carry data locations.** The name digest includes `--market-condition-manifest`
   digests, `--exclude-symbols`, and for Senate the universe list. January's new manifests, exclusions
   and the regenerated 2020-2026 Senate universe would give NEW names, so phase B would not find
   phase A's jobs. Fix: under `--wf-defer-last-test`, digest these tokens as placeholders; the real
   values stay on each job's row and are written into `cell.json` per phase.
2. **The out-of-sample re-run uses the TRAIN job's stored config** (`_persist_top_backtests` reads
   `optimization_config["backtest"]` and only replaces the window). wf3's 2026 test would therefore
   run on phase A's manifests, universe and exclusions. Fix: a deferred fold's test takes those data
   keys from the current command's config, everything else (genome, fixed settings, budgets) from the
   train job, and records both in the report.

The refit is a fresh optimize from the current command, so it already uses the new data.

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
2. **Build `tools/grid_goal2027.sh`** (design 2.6): start/stop/status/resume, `WORKERS`/`PARALLEL`
   settings, `PHASE_A=1` adds `--wf-defer-last-test --wf-data-end 2025-12-31`, preflight checks coverage
   to the data end (metric store, manifests, expert caches), pinned Alpaca-era paths.
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

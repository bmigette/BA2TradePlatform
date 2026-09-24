# Option discovery: soft total trade count

The September 20 continuation of Claude session **BT** implements its last requested
change: penalise fewer than 30 trades instead of rejecting sparse runs. Thirty means
completed structures across the entire backtest window, not thirty legs or thirty
trades per year. Fifteen structures receive a factor of 0.5; thirty or more receive 1.

Select `--fitness option_car_target_soft30`. This keeps the existing CAR > 35% and
CAR > drawdown targets, drawdown penalty, year consistency, profit caps and optional
robustness adjustment. It replaces only the annual count floor/ramp. Zero trades
and wipeout remain disqualified. Negative returns are not multiplied by a small
count factor, which would reward a thin losing book. Concentrated results can
still receive zero after robustness scoring, including books carried by five trades.

The metric requires the actual trade list when re-scoring stored results, so a
multi-leg option structure cannot be mistaken for several independent bets.

Existing fitness names and saved scores retain their behavior. Discovery includes
the chosen metric in its identity; checkpoint metadata additionally rejects a
switch between old and new objectives under the same name. A legacy checkpoint
without metric metadata cannot resume under `option_car_target_soft30`.

For the existing remote stage-1 campaign, retain its pinned OHLCV and structure
manifests and use:

```bash
export STAGE1_FITNESS=option_car_target_soft30
export STAGE1_SUFFIX=-st1soft30
bash tools/stage1_run.sh --experts DeterministicScorer
```

The campaign retains 16 structures, $20,000 starting equity, 2020–2025, population
200, 60 generations, patience 8, 24 local consumers, ThetaData options and the
existing screener gate. The 2026 holdout is excluded. Previous `-st1rob` results
and checkpoints must be retained under their original names.

Validation: 1,107 focused checks passed across the new objective, legacy objectives,
structure counting, discovery driver, checkpoint controls, metric catalog and golden
parity. One shell check initially selected unavailable WSL bash on Windows; it passed
when rerun with the installed Git Bash. Tests use isolated databases and fixtures.

Separate review finding: neutral recipes have a working low-confidence BUY/SELL
condition, but also an optional HOLD-only gate. Enabling the latter makes the entry
unreachable. The intended correction is to remove that conflict from new recipes,
not to broaden HOLD eligibility. This scoring change does not alter those recipes
or existing live/backtest trade decisions. See the September 19 commit review.

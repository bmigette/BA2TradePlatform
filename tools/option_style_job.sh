#!/usr/bin/env bash
# Stage-2 option STYLE job (O_STYLE x DeterministicScorer): ONE job, the launch every stage-2 run used.
#
#   bash tools/option_style_job.sh <style> <name-suffix> <seed bundle> [extra optimize args ...]
#
# What it pins (the stage-2 recipe; change one and the job is a different experiment, so give it a new suffix):
#   O_STYLE <style> from tools/option_styles.stage2.json, DeterministicScorer, 2020-01-02..2025-12-31 on the daily
#   clock, $20k initial capital + $20k equity cap, population 200 x 30 generations (patience 5, min gain 1%),
#   seed 42, fitness option_car_target_soft30, the ThetaData options store (OPTION_HISTORY_SOURCE below) with the
#   pow-2026-09-22 spread model,
#   Alpaca OHLCV history, SPY regime gates, the screener's LARGE band restricted (backtest only) to the
#   tools/options_universe_stage2.txt names, both market-condition profiles on the pinned digests below,
#   trials on one remote worker (--parallel 0), generation 0 seeded from <seed bundle>.
# Extra args go LAST, so they override the defaults above (argparse keeps the last value):
#   --fitness option_long --estimates-source fmp                 job C (style long)
#   --fitness option_long_dd40_50 --estimates-source fmp --initial-capital 10000 --equity-cap 10000   job L10 (long10k)
#                                                                 and job Burst (burst10k), same flags
#   --estimates-source fmp --initial-capital 10000 --equity-cap 10000   job 7 (cheap10k; default fitness, SCREEN below)
#   --seed-check-only                                            assemble + check the seeds, write nothing
#   --print-universe                                             (optimize flag) print the resolved universe
# `optimize` REFUSES a run that does not state --estimates-source (no default): jobs launched after
# 2026-11-07 must pass it in the extra args (fmp for every option style job so far).
#
# Host settings, all overridable by environment variables (the defaults are the remote227 values of the
# 2026-11-05..08 jobs):
#   REPO          the checkout the master runs from (a DETACHED checkout at the dev commit of record; never the
#                 fleet worker's own tree: a worker re-sync git-pulls it, never the owner PC's live clone)
#   PY            the interpreter: the fleet venv's python (no venv of its own on that host)
#   BA2_HOME      the master's own data home (own test DB: never share one DB between two masters);
#                 $BA2_HOME/common/cache must be the cache the workers share
#   WORKER        the registered remote worker's name (Settings -> Workers)
#   BA2_MAX_REMOTE_SLOTS  worker slots the master may use (default 24)
#   MC_OHLCV, MC_TA       the two market-condition manifest digests (default: the Alpaca build
#                 20261104T193527Z-5773e25c digests of record, docs/REPRODUCE-BACKTESTS.md 4.3)
#   SCREENER_STORE, SCREENER_PANEL   the metric store and daily-panel directory NAMES under
#                 $BA2_HOME/common/cache/screener (default metric_store_alpaca_20261022 and 03d21a4a2f176c69)
#   UFILE         the option universe file (default $REPO/tools/options_universe_stage2.txt): the --universe AND the
#                 --screener-universe-file restriction; '#' comments (whole-line or trailing) and blank lines skipped
#   OPTION_HISTORY_SOURCE  whose option history the job reads: thetadata (default: `--options-store thetadata`, the
#                 name and every flag exactly as the stage-2 jobs had them) or alphavantage (the Alpha Vantage store,
#                 docs/REPRODUCE-BACKTESTS.md 4.6: `--options-store alphavantage --option-history-source alphavantage`,
#                 BACKTEST_OPTIONS_STORE=alphavantage and an -optalphavantage name suffix, like the matrix drivers);
#                 the small-account chain sets alphavantage (owner 2026-11-12)
#   SCREEN        the screen (default "--screener-cap-band large": searched screener genes on the large band);
#                 job 6 passes "--screen-preset ndx30" (a FIXED screen: the 30 largest by market cap, no genes)
#                 and job 7 "--screen-preset cheap50" (FIXED: the 50 largest priced $10..$50 at each scan)
# The FMP key is read from the master's test DB (appsetting FMP_API_KEY) into the environment of the job; it is
# never written to a file, an argument or a log. Needs `refresh_fred_cache.py --check-rate-window` to pass
# (DGS3MO cached for the window) or the job does not start.
#
# Run it detached: a systemd transient unit (systemd-run --unit <name> -p MemoryMax=... ) or nohup + setsid, as
# tools/grid_goal2027.sh does for its `start`. Tools/grid_option_small_accounts.sh chains seven of these.
set -u
STYLE="$1"; SUFFIX="$2"; BUNDLE="$3"; shift 3
REPO="${REPO:-/home/debian/ba2-grid/repo}"
cd "$REPO" || exit 1
PY="${PY:-/opt/ba2worker/ba2-venvs/test/bin/python}"
export BA2_HOME="${BA2_HOME:-/home/debian/ba2-grid/home}"
export DB_FILE=$BA2_HOME/test/dl_forecasting.db
export DATABASE_URL="sqlite:///$DB_FILE"
FMP_API_KEY="$($PY -c 'import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("SELECT value_str FROM appsetting WHERE key=?", ("FMP_API_KEY",)).fetchone()
print(r[0] if r and r[0] else "")' "$DB_FILE")"
[ -n "$FMP_API_KEY" ] || { echo "no FMP key in $DB_FILE" >&2; exit 1; }
export FMP_API_KEY
OPTION_HISTORY_SOURCE="${OPTION_HISTORY_SOURCE:-thetadata}"
case "$OPTION_HISTORY_SOURCE" in
  thetadata)    OPT_STORE_ARGS="--options-store thetadata"; OPT_NAME_TAG="" ;;
  alphavantage) OPT_STORE_ARGS="--options-store alphavantage --option-history-source alphavantage"
                OPT_NAME_TAG="-optalphavantage" ;;
  *) echo "OPTION_HISTORY_SOURCE must be thetadata or alphavantage, got '$OPTION_HISTORY_SOURCE'" >&2; exit 2 ;;
esac
export BACKTEST_OPTIONS_STORE=$OPTION_HISTORY_SOURCE BT_MAX_TASKS_PER_CHILD=32 PYTHONUNBUFFERED=1
export BA2_MAX_REMOTE_SLOTS="${BA2_MAX_REMOTE_SLOTS:-24}"
export PYTHONPATH=$REPO/packages/common:$REPO/packages/providers:$REPO/packages/experts:$REPO/testplatform/backend:$REPO/testplatform
$PY tools/refresh_fred_cache.py --check-rate-window 2020-01-02 2025-12-31 || exit 1
CACHE=$BA2_HOME/common/cache
UFILE="${UFILE:-$REPO/tools/options_universe_stage2.txt}"
WORKER="${WORKER:-remote227}"
MC_OHLCV="${MC_OHLCV:-f0fe573aa4c92ca0f3a6cb88553c085f43b6dfe93b4994ab6c0a8d21e5db337a}"
MC_TA="${MC_TA:-e2ff85f617671c4b5ff8fde1e0375ecf7a4424b13ed9a12918c69df7624d1be4}"
SCREENER_STORE="${SCREENER_STORE:-metric_store_alpaca_20261022}"
SCREENER_PANEL="${SCREENER_PANEL:-03d21a4a2f176c69}"
SCREEN="${SCREEN:---screener-cap-band large}"
UNIVERSE=$(tr -d '\r' < "$UFILE" | sed 's/#.*//; s/[[:space:]]//g' | grep -v '^$' | paste -sd, -)
exec $PY testplatform/ba2test_launcher.py optimize --expert DeterministicScorer \
  --universe "$UNIVERSE" --strategy O_STYLE \
  --style-config $REPO/tools/option_styles.stage2.json --style "$STYLE" \
  --start 2020-01-02 --end 2025-12-31 --interval 1d \
  --population 200 --generations 30 --initial-capital 20000.0 --run-schedule daily \
  --name "optm-DeterministicScorer-O_STYLE-$STYLE-$SUFFIX$OPT_NAME_TAG" \
  --parallel 0 --worker "$WORKER" --seed 42 --elitism-percent 10.0 \
  $OPT_STORE_ARGS --option-spread-model pow-2026-09-22 \
  --fitness option_car_target_soft30 --early-stop 5 --early-stop-min-rel 0.01 --mutation-prob 0.3 \
  --ohlcv-history-source alpaca --profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 \
  --option-size-within-fill-volume --equity-cap 20000 --benchmark-regime-gates \
  --market-condition-profile ohlcv-v1,ta-structure-v1 \
  --market-condition-manifest "ohlcv-v1=$MC_OHLCV,ta-structure-v1=$MC_TA" \
  --screener --screener-store $CACHE/screener/$SCREENER_STORE \
  --screener-panel $CACHE/screener/daily_panel/$SCREENER_PANEL $SCREEN \
  --screener-universe-file "$UFILE" --screener-cadence-days 7 \
  --seed-population "$BUNDLE" "$@"

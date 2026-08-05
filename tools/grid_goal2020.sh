#!/usr/bin/env bash
# goal2020 — the full re-optimization on the FIXED engine, run as TWO sizing matrices.
#
# WHY THIS EXISTS. Every optimization before v2026.07.1002 was scored with DaysOpened and
# DaysSinceLastClose* INERT (they queried storage the backtest in-memory store does not serve, so
# they silently returned "nothing found"). 129 of 153 completed optimizations were built on a
# strategy declaring at least one of those conditions. Rather than re-run them individually, this
# is ONE clean sweep that becomes the new source of truth.
#
# WINDOW: 2020-01-01 -> 2025-12-31. Ends 31 Dec deliberately: consistent_annual_return buckets by
# CALENDAR year and merges a start/end stub shorter than 182.62 days into its neighbour, so a
# 30-June end silently produces an 18-month final bucket and biases the consistency factor down.
# This window yields six clean buckets (364/365/365/365/366/365d) and leaves 2026-H1 as a genuine
# OUT-OF-SAMPLE HOLDOUT, which no previous grid had.
#
# TWO MATRICES, NOT ONE GENE. sizing_mode (notional | risk_atr) has never been searched — it is
# pinned risk_atr for the classic experts. It stays pinned, run once per mode, because as a gene
# it would poison its own comparison: under notional the five ATR genes (risk_per_trade_pct,
# atr_multiplier, atr_period, min_stop_loss_pct, use_atr_stop) face no selection pressure and
# drift random, so a crossover flipping the mode judges it with unselected parameters and biases
# the result toward whichever mode happens to dominate the population. There is also a partial
# inertness no gating could fix: max_virtual_equity_per_instrument_percent is the PRIMARY sizer
# under notional but only a rarely-binding ceiling under risk_atr, so the two modes want
# different optima for the same gene and one population would fight itself.
#
# FactorRanker runs in the FIRST matrix ONLY. It declares bypasses_classic_rm = True, so it skips
# TradeRiskManagement entirely and never reads sizing_mode — running it twice would burn compute
# to produce byte-identical results.
#
# PREREQUISITE, NOT OPTIONAL: the screener metric_store must reach back to 2020. It currently
# starts ym=2022-01; without extending it, every screener-driven job silently begins in 2022 while
# the window claims 2020, and the two matrices are quietly measuring a different period than the
# one on the label. This script CHECKS and refuses to run.
#
# Resumable: the driver skips any job whose StrategyOptimization row is already `completed`, so
# killing and re-running continues where it left off. Matrices run SEQUENTIALLY — never
# concurrently; contention collapsed two earlier grid attempts and the box pages at 4 local
# Senate-class trials.
set -u
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python.exe
DRIVER=tools/run_screener_capband_matrix.py

START=2020-01-01
END=2025-12-31
FITNESS=consistent_annual_return
STORE="$HOME/Documents/ba2/common/cache/screener/metric_store"

# DISTRIBUTE BY DEFAULT. This is not a tuning preference -- it is the difference between ~2 days
# and ~2.5 weeks for 48 jobs, and a local-only run is SILENT about it: with no --workers the
# driver simply omits the flag, StrategyOptimization.worker_ids stays NULL, and
# strategy_optimization_handler keeps the local-only path with no warning. The only tell is the
# ABSENCE of a "DISTRIBUTED across N selected worker(s)" line in the log, which is easy to miss
# for hours. Defaulting it here means you must opt OUT (WORKERS= ./grid_goal2020.sh) rather than
# remember to opt in.
#
# The worker syncs by `git pull`, so the master's commit MUST be pushed before launching or every
# trial is retry-excluded for the whole run.
WORKERS="${WORKERS-remote150}"

COMMON=(--start "$START" --end "$END" --fitness "$FITNESS" --store "$STORE")
if [ -n "$WORKERS" ]; then
  COMMON+=(--workers "$WORKERS")
  echo "=== distributing to remote worker(s): $WORKERS  (+ 4 local)"
else
  echo "=== WORKERS empty -> LOCAL-ONLY run (roughly 2.5x slower)"
fi

echo "=================================================================="
echo "=== goal2020 : $START -> $END , fitness=$FITNESS"
echo "=== matrix 1 : risk_atr  (incl. FactorRanker)"
echo "=== matrix 2 : notional  (FactorRanker skipped — bypass expert)"
echo "=================================================================="

# --- prerequisite: does the metric store actually reach 2020? ---------------------------------
"$PY" - "$STORE" <<'EOF' || exit 1
import sys, pathlib
store = pathlib.Path(sys.argv[1])
if not store.exists():
    sys.exit(f"FATAL: metric store not found at {store}")
yms = sorted(p.name for p in store.glob("ym=*"))
if not yms:
    sys.exit(f"FATAL: no ym=* partitions under {store}")
first = yms[0].split("=", 1)[1]
print(f"metric store: {len(yms)} partitions, earliest {first}")
if first > "2020-01":
    sys.exit(
        f"FATAL: metric store starts {first}, but this grid claims a 2020-01-01 start.\n"
        f"       Screener-driven jobs would silently begin at {first} while every label says\n"
        f"       2020 . Extend the store first (see the metric_store task), then re-run.")
EOF

# --- transaction costs, PER CAP BAND ---------------------------------------------------------
# Until 2026-08-04 this grid ran with spread_bps=0 AND slippage=0 -- every fill at the ideal
# price. That is not a small optimism: it biases the SEARCH, because a cost-free book rewards
# exactly the genomes costs would punish (trade more often, take tighter targets). Both knobs
# already existed and worked (BacktestAccount._slip degrades MARKET/STOP fills,
# _limit_trigger_price widens LIMIT/TP triggers so a marginal TP can miss its window entirely);
# nothing ever passed them.
#
# WHERE THESE NUMBERS COME FROM -- and what they are not. We have no quote data, so the spread
# cannot be measured from the cache. A Corwin-Schultz (2012) high-low estimate over 2022-2025 was
# tried and REJECTED: it returns 76 bps for AAPL and 44 bps for SPY, whose true effective spreads
# are ~1 bp. On daily bars at this liquidity the estimator is dominated by intraday volatility,
# not spread, so its levels are meaningless here (its band ORDERING -- small > mid > large -- is
# the one thing it does get right, and it agrees with the values below).
#
# So these are ASSUMPTIONS from US equity market structure, not measurements: round-trip
# effective spread of a few bps for large caps, ~10 for mid, tens for small. They are deliberately
# on the conservative side. Treat any winner as conditional on them and run the Monte Carlo
# spread SWEEP (robustness.py's spread_sweep_bps) before trusting a genome -- that is what
# actually tells you whether an edge survives, rather than any single assumed number.
SPREAD_BPS_LARGE="${SPREAD_BPS_LARGE:-3}"
SPREAD_BPS_MID="${SPREAD_BPS_MID:-10}"
SPREAD_BPS_SMALL="${SPREAD_BPS_SMALL:-40}"

spread_for() {
  case "$1" in
    large) echo "$SPREAD_BPS_LARGE" ;;
    mid)   echo "$SPREAD_BPS_MID" ;;
    small) echo "$SPREAD_BPS_SMALL" ;;
    *)     echo 0 ;;
  esac
}

# One driver invocation per (mode, band): --spread-bps applies to EVERY job of an invocation, so
# a per-band value requires splitting what used to be one call per mode. Job names are unchanged,
# so the completed-job skip still resumes across this restructure.
run_matrix() {                      # $1=mode  $2=name-suffix  $3...=extra driver args
  local mode="$1" suffix="$2"; shift 2
  for band in large mid small; do
    local sp; sp="$(spread_for "$band")"
    echo; echo "=== $mode / $band  (spread ${sp} bps round-trip)  $(date)"
    "$PY" "$DRIVER" "${COMMON[@]}"       --sizing-mode "$mode"       --bands "$band"       --spread-bps "$sp"       --name-suffix="$suffix"       "$@"
    echo "=== $mode / $band  done rc=$? $(date)"
  done
}

# --- matrix 1: risk_atr ------------------------------------------------------------------------
# NOTE the `=` in --name-suffix=-... : the value starts with a dash, which argparse would
# otherwise read as another flag.
echo; echo "=== matrix 1/2  risk_atr  $(date)"
run_matrix risk_atr -goal2020-riskatr "$@"
echo "=== matrix 1/2  done $(date)"

# --- matrix 2: notional ------------------------------------------------------------------------
echo; echo "=== matrix 2/2  notional  $(date)"
run_matrix notional -goal2020-notional --skip-experts FactorRanker "$@"
echo "=== matrix 2/2  done $(date)"

# --- matrices 3 & 4: the S5/S6/S7 strategies -------------------------------------------------
# These are CLASSIC-expert strategies, not Senate ones, and they were run against FMPRating (S5 x6,
# S6 x5, S7 x6), FMPEarningsDrift and FMPInsiderClusterBuy in earlier grids. They were never in
# THIS driver's default (git: only ever S1,S2,S3,S4 -> S1,S2,S3), so without them goal2020 would
# not actually be re-optimizing everything that has been explored -- which is its whole purpose.
#
# It matters most for S7: it is a faithful replica of the archived 186.53% FMPRating S2-large
# winner (backtest #91 / opt 23, calmar 3.66, dd -11.52%). S5 is an S2/S3 hybrid derived from that
# same winner, S6 the high-frequency quick-cycle from the -tpsl S2-large run. Every prior result
# for all three is invalidated twice over: scored with the inert DaysOpened conditions AND on the
# pre-2026-08-04 CAR scale.
#
# Runs LAST, as separate matrices, so the S1/S2/S3 answer lands first and this is purely additive:
# +45 jobs (measured), i.e. roughly double the wall time. Set STRATEGIES_EXTRA= to skip.
STRATEGIES_EXTRA="${STRATEGIES_EXTRA-S5,S6,S7}"
if [ -n "$STRATEGIES_EXTRA" ]; then
  echo; echo "=== matrix 3/4  risk_atr  strategies=$STRATEGIES_EXTRA  $(date)"
  run_matrix risk_atr -goal2020-riskatr --strategies "$STRATEGIES_EXTRA" "$@"
  echo "=== matrix 3/4  done $(date)"

  echo; echo "=== matrix 4/4  notional  strategies=$STRATEGIES_EXTRA  $(date)"
  run_matrix notional -goal2020-notional --skip-experts FactorRanker --strategies "$STRATEGIES_EXTRA" "$@"
  echo "=== matrix 4/4  done $(date)"
fi

echo; echo "=== goal2020 COMPLETE $(date) ==="
echo "Compare with:  ba2-test report   (labels: goal2020-riskatr / goal2020-notional)"
echo "Worth checking beyond fitness: whether the two modes put"
echo "max_virtual_equity_per_instrument_percent in DIFFERENT places — that tells you if they are"
echo "genuinely different strategies or just re-parameterisations of the same one."

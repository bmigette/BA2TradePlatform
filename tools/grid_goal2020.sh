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

# OWN THE LOG (2026-08-09). This script only ever wrote to stdout, so the redirect belonged to
# whoever launched it. When that shell exits -- which it does when the launch came from a tool
# call -- the driver keeps running ORPHANED, holding a stdout handle nobody flushes, and Python
# block-buffers stdout when it is not a TTY. Observed on the small-band matrix: the job ran for
# 6+ hours while grid_goal2020.log's last write stayed frozen at the launch banner and NO file on
# disk was receiving the per-generation lines. The run was healthy; it was simply invisible.
# Owning the redirect here makes the log independent of how the script is invoked, and
# PYTHONUNBUFFERED stops the buffer from swallowing progress between flushes.
GRID_LOG="${GRID_LOG:-grid_goal2020.log}"
export PYTHONUNBUFFERED=1
exec > >(tee -a "$GRID_LOG") 2>&1

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

# Sweep orphaned multiprocessing.spawn pool workers from any PREVIOUS run before starting.
# On Windows those children are detached: killing a grid leaves them alive holding their full
# working set, and they match neither the driver nor the launcher in a process filter. Measured
# 2026-08-15: 40 orphans holding 41 GB on a 65 GB box, accumulated over a few kill/relaunch
# cycles -- they exhausted the box, failed a 2.24 MiB numpy allocation, and poisoned every memory
# measurement taken that evening (two "the fix did not hold" conclusions were drawn against a box
# that was mostly full of leftovers). Relaunching without this inherits the previous run's leak.
if command -v powershell >/dev/null 2>&1; then
  powershell -NoProfile -ExecutionPolicy Bypass -File tools/sweep_spawn_orphans.ps1 || true
fi

INTERVAL="${INTERVAL:-5min}"

# --- PREFLIGHT: does the cache cover THIS grid's window at THIS grid's interval? ---------------
# Added 2026-08-16 after a grid ran for days on a universe with no 5min bars before 2022: the jobs
# claimed 2020-2025 but ~75% of symbols could not be PRICED, therefore not traded, for the first
# two years -- silently, because preload treats "cache exists but has no rows in this sub-range"
# as a legitimate gap (recent IPO / holiday) rather than an error.
#
# SWITCHED to check_window_coverage.py (2026-08-26), matching grid_goal2020_matrix3.sh -- the
# original cache_health_check.py-based version here produced a false FATAL on a genuinely
# complete cache: its "INDICATOR WARMUP dependency" check demands 375 CALENDAR DAYS of history
# BEFORE $START for indicator lead-in, which nobody fetches when the download scope is stated as
# "$START -> $END" (verified directly: AA/AAP/AAON's 5min cache runs exactly 2020-01-02 ->
# 2026-06-30 -- a deliberate, COMPLETE download of the stated window; the tool's threshold, not
# the data, was wrong). It also exits 0 regardless of what it finds and is far too slow at 5min
# (~321s for 8 symbols measured -- see check_window_coverage.py's own header).
#
# UNIVERSE SCOPE: unlike matrix3 (one fixed --universe file), this grid is screener-driven with a
# DIFFERENT disjoint cap-band universe per job. check_window_coverage.py with --symbols omitted
# samples the WHOLE cache (7,048 5min / 18,731 1d files -- delisted tickers, ETFs, symbols no
# expert will ever touch) and measures something nobody cares about: verified 71%/63% cache-wide
# on this exact cache, matching matrix3's own comment ("72%/66% cache-wide versus 97%/95% on the
# Senate universe"). So each band's actual screened union is derived here the SAME way the real
# optimize run does it (ba2test_launcher.py's `_loosest` pattern): the union of every symbol
# `screened_symbol_union` could EVER select over [START, END] under the loosest end of every
# screener gene for that band (most-admitting thresholds + max_stocks at its ceiling) -- the
# correct superset a run actually touches, not the raw store or the whole cache.
echo
echo "=== PREFLIGHT: deriving each cap-band's screened universe (loosest gene bound)"
_UNIV_DIR="$(mktemp -d)"
"$PY" - "$STORE" "$START" "$END" "$_UNIV_DIR" <<'EOF'
import sys
from ba2_providers.screener import metric_store as ms

store, start, end, outdir = sys.argv[1:5]
store_df = ms.load_store(store)
if store_df.empty:
    sys.exit(f"FATAL: metric store empty at {store}")

# Mirrors _SCREENER_CAP_BANDS in ba2test_launcher.py -- keep the two in sync if the bands change.
BANDS = {
    "small": {"min": 5e7,  "cap_max": 2e9},
    "mid":   {"min": 2e9,  "cap_max": 1e10},
    "large": {"min": 1e10, "cap_max": None},
}
for band, b in BANDS.items():
    loosest = {
        "market_cap_min": b["min"],
        "relative_volume_min": 0.0,
        "price_drop_pct": 0.0,
        "weinstein_stage2_only": 0,
        "max_stocks": 50,   # screener_max_stocks's ceiling (_SCREENER_OPT)
    }
    if b["cap_max"] is not None:
        loosest["market_cap_max"] = b["cap_max"]
    union = ms.screened_symbol_union(store_df, start, end, loosest)
    path = f"{outdir}/{band}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(union))
    print(f"  {band}: {len(union)} symbol(s) ever screened-in -> {path}")
EOF
echo "=== PREFLIGHT cache coverage: window ${START} -> ${END}"
# Checks BOTH intervals (the 5min bars the engine PRICES with, and the 1d bars the indicators and
# the screener metric store read) for EACH band's own universe.
_cov_fail=0
for _band in small mid large; do
  _ufile="$_UNIV_DIR/${_band}.txt"
  for _iv in "$INTERVAL" 1d; do
    echo "--- coverage: band=${_band} interval=${_iv}"
    "$PY" tools/check_window_coverage.py --interval "$_iv" --start "$START" \
        --symbols "@$_ufile" --sample 150 --min-covered-pct 75 || _cov_fail=1
  done
done
if [ "$_cov_fail" = "1" ]; then
  echo "=== PREFLIGHT FAILED: the cache does not cover this window at this interval."
  echo "===   Backfill first, e.g.:"
  echo "===   ba2-test fetch-cache --provider fmp --timeframes ${INTERVAL} \\"
  echo "===       --start ${START} --end ${END} --symbols @<universe.txt> --workers 5"
  echo "===   Set GRID_SKIP_PREFLIGHT=1 to run anyway (results WILL be on a reduced universe)."
  [ "${GRID_SKIP_PREFLIGHT:-0}" = "1" ] || exit 1
  echo "=== GRID_SKIP_PREFLIGHT=1 -- continuing on a KNOWN-INCOMPLETE cache."
fi

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
# MEASURED 2026-08-17, no longer assumed. Source: Alpaca SIP historical quotes (full NBBO, not
# IEX), sampled at 11:30 ET on 8 trading days spanning both regimes -- 2022 (bear/high-vol) and
# 2024-25 -- across symbols these strategies actually trade, ~34k quotes total. Median relative
# top-of-book spread in bps:
#            2022     2024-25    p90 (both)   OLD ASSUMPTION
#   large    1.57      2.68         7.05          3   <- good
#   mid      7.23      9.01        22.16         10   <- good
#   small   13.75     16.78        45.31         40   <- 2.4x TOO HARSH
# 2022 came out TIGHTER than 2024-25, so the pessimistic-regime worry does not hold either.
# This retires the standing caveat that these were "assumptions from US equity market structure,
# not measurements" and that Corwin-Schultz was unusable (it returned 76bps for AAPL).
# Caveat kept: this is the QUOTED top-of-book spread. It ignores price improvement (favouring us)
# and depth beyond the top (against us on size) -- fine at our order sizes, not for size trading.
SPREAD_BPS_LARGE="${SPREAD_BPS_LARGE:-3}"
SPREAD_BPS_MID="${SPREAD_BPS_MID:-9}"
SPREAD_BPS_SMALL="${SPREAD_BPS_SMALL:-17}"

# Spread STRESS, expressed as a MULTIPLE of each band's own assumed spread rather than an
# absolute bps figure. 0 = off (default, and the pre-existing behaviour byte for byte).
# 1.0 = "also score every genome as if the spread were DOUBLE this band's assumption, and rank
# on the worse of the two", which directly answers the caveat three comments above: instead of
# treating a winner as conditional on an unmeasured number and checking it afterwards, the
# search itself refuses to select an edge that only exists at the assumed cost.
#
# A multiple, not per-band numbers, because the bands' spreads differ ~13x (3 vs 40). A single
# absolute stress would be 13x harsher on large than on small; and hand-written per-band values
# would silently desync the moment SPREAD_BPS_* changed. This cannot drift -- it is derived.
# 1.5 lands the widened spread on the MEASURED p90 in every band (widening/median is 1.33 large,
# 1.44 mid, 1.65 small), so "stressed" now means "survives the worst decile of real quoted
# spreads" rather than an invented multiple. Was 0 (off).
STRESS_SPREAD_MULT="${STRESS_SPREAD_MULT:-1.5}"

# Rank on the ROBUSTNESS-ADJUSTED fitness (concentration x monte-carlo x spread). Default ON:
# every band x strategy cell of the previous FMPRating matrix produced a concentrated winner
# (7 of 9 cells had NO result with a top-5 share under 40%), which is what an unpenalised search
# converges to. Set ROBUST_FITNESS=0 for a like-for-like comparison against a pre-2026-08-16 run.
ROBUST_FITNESS="${ROBUST_FITNESS:-1}"
robust_args=(); [ "$ROBUST_FITNESS" = "1" ] && robust_args=(--robust-fitness)

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
    local stress_args=()
    local st; st="$(awk -v s="$sp" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')"
    if awk -v v="$st" 'BEGIN{exit !(v>0)}'; then
      stress_args=(--stress-spread-bps "$st")
    fi
    echo; echo "=== $mode / $band  (spread ${sp} bps round-trip${st:+, stress +${st}})  $(date)"
    "$PY" "$DRIVER" "${COMMON[@]}"       --sizing-mode "$mode"       --bands "$band"       --spread-bps "$sp"       "${stress_args[@]}" "${robust_args[@]}"       --name-suffix="$suffix"       "$@"
    echo "=== $mode / $band  done rc=$? $(date)"
  done
}

# DeterministicScorer belongs to MATRIX 3 (grid_goal2020_matrix3.sh, PHASE B), not here. Its
# absence from these matrices was DOCUMENTED in that script's PHASE B header ("Skipped from
# matrices 1 and 2 via --skip-experts") but never actually wired: the driver's _CLASSIC list has
# included it since 2026-08-09 (310f669) and neither run_matrix call below excluded it. The result
# was 3 strategies x 3 bands x 2 matrices = 18 duplicate jobs shadowing matrix 3's own 18-job
# PHASE B under near-identical names (-goal2020-riskatr vs -goal2020-riskatr-ds).
# Keep this in sync with matrix 3: an expert owned by matrix 3 must be skipped in BOTH matrices
# here AND in the S5/S6/S7 pair below.
DS_SKIP=DeterministicScorer

# --- matrix 1: risk_atr ------------------------------------------------------------------------
# NOTE the `=` in --name-suffix=-... : the value starts with a dash, which argparse would
# otherwise read as another flag.
echo; echo "=== matrix 1/2  risk_atr  $(date)"
run_matrix risk_atr -goal2020-riskatr --skip-experts "$DS_SKIP" "$@"
echo "=== matrix 1/2  done $(date)"

# --- matrix 2: notional ------------------------------------------------------------------------
echo; echo "=== matrix 2/2  notional  $(date)"
run_matrix notional -goal2020-notional --skip-experts "FactorRanker,$DS_SKIP" "$@"
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
  run_matrix risk_atr -goal2020-riskatr --skip-experts "$DS_SKIP" --strategies "$STRATEGIES_EXTRA" "$@"
  echo "=== matrix 3/4  done $(date)"

  echo; echo "=== matrix 4/4  notional  strategies=$STRATEGIES_EXTRA  $(date)"
  run_matrix notional -goal2020-notional --skip-experts "FactorRanker,$DS_SKIP" --strategies "$STRATEGIES_EXTRA" "$@"
  echo "=== matrix 4/4  done $(date)"
fi

echo; echo "=== goal2020 COMPLETE $(date) ==="
echo "Compare with:  ba2-test report   (labels: goal2020-riskatr / goal2020-notional)"
echo "Worth checking beyond fitness: whether the two modes put"
echo "max_virtual_equity_per_instrument_percent in DIFFERENT places — that tells you if they are"
echo "genuinely different strategies or just re-parameterisations of the same one."

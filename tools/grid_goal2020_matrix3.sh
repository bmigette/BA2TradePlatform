#!/usr/bin/env bash
#
# goal2020 MATRIX 3 — the two experts matrices 1 and 2 never covered.
#
#   PHASE A  FMPSenateTraderWeight   own disclosure universe, no cap bands   14 jobs
#   PHASE B  DeterministicScorer     cap-band screener matrix                18 jobs
#
# Both on 2020-01-01 → 2025-12-31, both sizing modes, stress enabled.
#
# THE TWO PHASES HAVE DIFFERENT SHAPES AND THAT IS NOT INCIDENTAL. Senate is a
# basket-dispatch expert that picks its own symbols from Senate filings, so it has no `--bands`
# dimension and is driven straight through ba2test_launcher. DeterministicScorer declares
# can_recommend_instruments=False — it is a CLASSIC per-symbol expert and belongs to the
# cap-band screener matrix, so phase B calls run_screener_capband_matrix.py exactly as matrices
# 1 and 2 do. Running DeterministicScorer on Senate's universe would optimize a different
# strategy from the one that would ever be deployed.
#
# WHY THIS IS A SEPARATE SCRIPT, not a third run_matrix in tools/grid_goal2020.sh:
#
#   1. NEVER EDIT A RUNNING SHELL SCRIPT. bash reads a script lazily by byte offset, so adding
#      lines to grid_goal2020.sh while it is executing shifts every later offset and the running
#      shell resumes mid-line. That is not theoretical — it happened on 2026-08-12: matrix 1
#      finished cleanly at 04:04, then the wrapper died on `syntax error near unexpected token`
#      and matrix 2 never launched, idling the box for two hours.
#   2. Senate is NOT a cap-band expert. It picks its own symbols from Senate disclosures, so it
#      has no `--bands` dimension and must not be forced through the screener matrix — doing so
#      would optimize a different strategy from the one that is deployed.
#   3. Senate trials are ~11-12 GB each against ~2.5 GB for the classic experts. Matrix 1 peaked
#      at 95.6% system memory on the light experts alone, so PARALLEL is 2 here, not 4.
#
# BOTH SIZING MODES are searched (unlike FactorRanker, which is excluded from the notional
# matrix): Senate runs the classic per-symbol RM pipeline, so `sizing_mode` genuinely changes
# position sizing and the two modes are different strategies, not duplicates.
#
# WINDOW: 2020-01-01 → 2025-12-31, the same as every other goal2020 job. Senate disclosure data
# supports it — the congress_senate_trades cache runs continuously from 2013 with 864 rows in
# 2020, so there is no data floor like FMPRating's `-from2022`.
#
# BUT THE DATA IS THIN — CHECK THIS FIRST. Of 1,500 sampled symbol files, 1,056 were EMPTY
# (44% of symbols have no Senate trades at all) and the whole universe carries only ~700
# disclosures/year. `consistent_annual_return` DISQUALIFIES anything under 12 trades/year
# (_CAR_HARD_MIN_TRADES_PER_YEAR), returning LOW_TRADE_SENTINEL. If the post-filter set lands
# under that, jobs will disqualify themselves and the fitness column will be full of -1e8 —
# that is the expected signature of "not enough disclosures", not a bug. Read the first job's
# trade counts before letting all 14 run.
#
# SPREAD + STRESS: Senate names are mostly mid/large caps, so the mid-band assumption (10 bps)
# is the closest fit. STRESS_SPREAD_MULT defaults to 1.0 here — matrix 2 runs stressed, and a
# Senate result that cannot be compared against it on the same basis is worth much less.
#
# RUN AFTER MATRIX 2. Both grids on one box will exhaust memory.
#
#   nohup bash tools/grid_goal2020_matrix3.sh > grid_goal2020_matrix3.log 2>&1 &
#
set -u
cd "$(dirname "$0")/.."

PY=.venv/Scripts/python.exe
UNIVERSE_FILE="$HOME/Documents/ba2/senate_universe.csv"
if [ ! -f "$UNIVERSE_FILE" ]; then
  echo "FATAL: universe file not found: $UNIVERSE_FILE" >&2
  echo "It is built by tools/build_senate_universe.py — Senate needs its OWN universe, not a cap band." >&2
  exit 1
fi
UNI=$(cat "$UNIVERSE_FILE")

WORKERS="${WORKERS:-remote150}"
STRATEGIES="${STRATEGIES:-S1 S2 S3 S5 S6 S7}"
PARALLEL="${PARALLEL:-2}"          # 11-12 GB/trial — see header
SPREAD_BPS="${SPREAD_BPS:-10}"     # mid-band assumption; Senate names skew mid/large
STRESS_SPREAD_MULT="${STRESS_SPREAD_MULT:-1.0}"
POPULATION="${POPULATION:-40}"
GENERATIONS="${GENERATIONS:-8}"

STRESS_BPS=$(awk -v s="$SPREAD_BPS" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')
stress_args=()
if awk -v v="$STRESS_BPS" 'BEGIN{exit !(v>0)}'; then
  stress_args=(--stress-spread-bps "$STRESS_BPS")
fi

WORKER_ARGS=()
[ -n "$WORKERS" ] && WORKER_ARGS=(--workers "$WORKERS")

echo "=================================================================="
echo "=== goal2020 MATRIX 3 : PHASE A Senate (own universe) + PHASE B DeterministicScorer (cap bands)"
echo "=== window 2020-01-01 -> 2025-12-31 | spread ${SPREAD_BPS} bps, stress +${STRESS_BPS}"
echo "=== strategies: $STRATEGIES | parallel $PARALLEL | workers: ${WORKERS:-local-only}"
echo "=== universe: $(echo "$UNI" | tr ',' '\n' | wc -l) symbols"
echo "=================================================================="

rc_any=0
for MODE in risk_atr notional; do
  for S in $STRATEGIES; do
    NAME="sen-${S}-goal2020-${MODE}"
    echo
    echo "=== $NAME  start $(date)"
    "$PY" testplatform/ba2test_launcher.py optimize \
      --expert FMPSenateTraderWeight \
      --strategy "$S" \
      --universe "$UNI" \
      --start 2020-01-01 --end 2025-12-31 \
      --interval 5min \
      --fitness consistent_annual_return \
      --population "$POPULATION" --generations "$GENERATIONS" \
      --early-stop 4 --mutation-prob 0.3 \
      --parallel "$PARALLEL" --seed 42 \
      --sizing-mode "$MODE" \
      --initial-capital 10000 --commission 1.0 \
      --spread-bps "$SPREAD_BPS" "${stress_args[@]}" \
      --profit-cap-pct 2000 --profit-share-cap-pct 25 \
      "${WORKER_ARGS[@]}" \
      --labels "goal2020-senate,${S},${MODE}" \
      --name "$NAME"
    rc=$?
    echo "=== $NAME  done rc=$rc $(date)"
    [ $rc -ne 0 ] && rc_any=$rc
  done
done


# ---------------------------------------------------------------------------
# PHASE B — DeterministicScorer, cap-band matrix (the shape matrices 1/2 use).
# ---------------------------------------------------------------------------
# Skipped from matrices 1 and 2 via --skip-experts while its macro section was inert (every
# FRED input resolved to None, so mw_vix/mw_credit/mw_yield_curve/mw_sahm and the hard_riskoff
# cutoff were dead genes). That was fixed 2026-08-11 (real point-in-time FRED series + prewarm),
# so it now has a live regime composite and is worth searching for the first time.
DRIVER=tools/run_screener_capband_matrix.py
STORE="${STORE:-$HOME/Documents/ba2/common/cache/screener/metric_store}"
DS_PARALLEL="${DS_PARALLEL:-4}"      # light expert (~2.5 GB/trial), unlike Senate

ds_spread_for() { case "$1" in large) echo 3 ;; mid) echo 10 ;; small) echo 40 ;; *) echo 0 ;; esac; }

for MODE in risk_atr notional; do
  for band in large mid small; do
    sp="$(ds_spread_for "$band")"
    st=$(awk -v s="$sp" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')
    ds_stress=()
    awk -v v="$st" 'BEGIN{exit !(v>0)}' && ds_stress=(--stress-spread-bps "$st")
    echo
    echo "=== PHASE B  DeterministicScorer / $MODE / $band  (spread ${sp}, stress +${st})  $(date)"
    "$PY" "$DRIVER"       --start 2020-01-01 --end 2025-12-31       --fitness consistent_annual_return       --store "$STORE"       --sizing-mode "$MODE" --bands "$band"       --spread-bps "$sp" "${ds_stress[@]}"       --parallel "$DS_PARALLEL"       --name-suffix="-goal2020-${MODE}-ds"       --skip-experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker       "${WORKER_ARGS[@]}"
    rc=$?
    echo "=== PHASE B  $MODE / $band  done rc=$rc $(date)"
    [ $rc -ne 0 ] && rc_any=$rc
  done
done

echo
echo "=== MATRIX 3 COMPLETE rc=$rc_any $(date) ==="
exit $rc_any

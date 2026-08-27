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
# SPREAD + STRESS: Senate names are mostly mid/large caps, so the mid band is the closest fit --
# now 9 bps, the MEASURED median (Alpaca SIP historical quotes, 2026-08-17), not the old 10 bps
# assumption. STRESS_SPREAD_MULT 1.5 widens it to ~22 bps, the measured mid-band p90, so a genome
# must survive the worst decile of real quoted spreads.
#
# !!! MATRIX 3 FITNESS IS NOT COMPARABLE WITH MATRICES 1 AND 2 !!!
#
# Not by choice — by accident, then by decision. stress_spread_bps was dropped by
# _build_daily_trial_config's whitelist (fixed in b991003 on 2026-08-12), so matrices 1 and 2
# both ran UNSTRESSED despite the CLI, the wrapper and the driver all reporting otherwise.
# Matrix 3 starts after the fix, so its stress is real. A stressed score is a DIFFERENT SCALE:
# it is min(fitness at the modelled spread, fitness at double it), so a matrix 3 number is
# systematically lower than a matrix 1/2 number for the same quality of genome.
#
# Compare WITHIN a matrix. To rank a matrix 3 winner against a matrix 1/2 winner, re-score the
# older one through the same stress (tools/ post-hoc spread sweep — it re-scores a finished
# trade list in milliseconds, no re-optimization) rather than comparing the raw fitness columns.
# Set STRESS_SPREAD_MULT=0 if you would rather have comparability than robustness.
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
SPREAD_BPS="${SPREAD_BPS:-9}"      # MEASURED mid-band median (Alpaca SIP 2026-08-17); Senate skews mid/large
STRESS_SPREAD_MULT="${STRESS_SPREAD_MULT:-1.5}"   # 1.5 x median ~= the measured p90
POPULATION="${POPULATION:-40}"
GENERATIONS="${GENERATIONS:-8}"
# REMOTE POOL SLOTS. Mirrors ds_remote_slots_for below -- this is a property of the GRID RUN (a
# measured per-trial footprint at a given box size), not of the expert's Python class. Used to
# live as FMPSenateTraderWeight.max_remote_worker_slots; moved here 2026-08-27 so every
# memory-heavy expert's remote concurrency is set by the driver that actually knows the box and
# the trial shape, the same way DeterministicScorer's already was, instead of requiring a
# class-attribute edit (and a code deploy) per expert. Senate is ~11-12 GB/trial, same footprint
# that set PARALLEL=2 above; 3 remote slots leaves headroom on the worker the same way that
# local-parallel figure does on the master (see header comment history: 4 drove the sen5min3
# master to 99.5-99.7% memory).
SENATE_REMOTE_SLOTS="${SENATE_REMOTE_SLOTS:-3}"

STRESS_BPS=$(awk -v s="$SPREAD_BPS" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')

# Rank on the ROBUSTNESS-ADJUSTED fitness (concentration x monte-carlo x spread). Default ON.
# Evidence: of the 9 band x strategy cells of the previous FMPRating matrix, 7 contained NO
# result whose top-5 trades were under 40% of net P&L -- concentration is what an unpenalised
# search converges to, not a quirk of a few rows. Set ROBUST_FITNESS=0 for a like-for-like
# comparison against a pre-2026-08-16 run (scores are NOT comparable across this flag).
ROBUST_FITNESS="${ROBUST_FITNESS:-1}"
robust_args=(); [ "$ROBUST_FITNESS" = "1" ] && robust_args=(--robust-fitness)

stress_args=()
if awk -v v="$STRESS_BPS" 'BEGIN{exit !(v>0)}'; then
  stress_args=(--stress-spread-bps "$STRESS_BPS")
fi

WORKER_ARGS=()
[ -n "$WORKERS" ] && WORKER_ARGS=(--workers "$WORKERS")

START="${START:-2020-01-01}"
END="${END:-2025-12-31}"
INTERVAL="${INTERVAL:-5min}"

# --- PREFLIGHT: does the cache cover THIS grid's window at THIS grid's interval? ---------------
# Added 2026-08-16 after a grid ran for days on a universe with no 5min bars before 2022: the jobs
# claimed 2020-2025 but ~75% of symbols could not be PRICED, therefore not traded, for the first
# two years -- silently, because preload treats "cache exists but has no rows in this sub-range"
# as a legitimate gap (recent IPO / holiday) rather than an error.
#
# The cache health tool ALREADY had a period-coverage check; it was simply never asked the right
# question. Its defaults are --ohlcv-interval 1d --start 2022-01-01, and daily has deep history,
# so running it with defaults reports everything healthy. It MUST be given the job's OWN interval
# and start date -- which is the entire point of this block. (Its symbol sample was also the
# alphabetical head, now a seeded random sample, which is the other reason this slipped through.)
echo
echo "=== PREFLIGHT cache coverage: interval=${INTERVAL} window ${START} -> ${END}"
# Checks BOTH intervals: the 5min bars the engine PRICES with, and the 1d bars the indicators and
# the screener metric store read. A grid can be blocked by either, and 1d was never checked here.
#
# The bar is 75% of ELIGIBLE symbols, not ~100% of all of them. Two corrections behind that:
#   * eligible EXCLUDES securities that had not listed yet -- they cannot have data at any
#     interval and are not a cache defect. Counting them made this gate report 40% missing on a
#     healthy cache (the sample was SPAC units, preferreds and 2025-26 IPOs).
#   * of the symbols that DID trade in 2020, ~12% have no 5min history at FMP at all -- verified
#     symbol by symbol (AFBI, ORLA, USBC, LSAK, ONC all return 0 bars for Jan-2020). FMP's
#     intraday depth is liquidity-dependent; that data does not exist at any price.
# 75% therefore means "the cache holds essentially everything obtainable", while still failing
# hard on the real incident this gate exists for (2026-08-16: 75% of symbols had daily back to
# 2019 but no 5min before 2022 -- those ARE eligible, so that cache scores ~25% and fails).
#
# It samples THE UNIVERSE THIS GRID TRADES, not the whole cache. The cache holds 7,048 5min and
# 18,731 1d files -- delisted tickers, ETFs, symbols no expert will ever touch -- and scoring
# against all of them measured something nobody cares about: 72%/66% cache-wide versus 97%/95%
# on the Senate universe, from the same cache on the same day.
#
# cache_health_check.py is NOT used here: it re-reads every Date column month-by-month and did not
# finish in 10 minutes on this cache. A gate nobody can afford to run is a gate that gets skipped.
_cov_fail=0
for _iv in "$INTERVAL" 1d; do
  echo "--- coverage: interval=${_iv}"
  "$PY" tools/check_window_coverage.py --interval "$_iv" --start "$START"       --symbols "@$UNIVERSE_FILE" --sample 150 --min-covered-pct 75 || _cov_fail=1
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


echo "=================================================================="
echo "=== goal2020 MATRIX 3 : PHASE A Senate (own universe) + PHASE B DeterministicScorer (cap bands)"
echo "=== window 2020-01-01 -> 2025-12-31 | spread ${SPREAD_BPS} bps, stress +${STRESS_BPS}"
echo "=== strategies: $STRATEGIES | parallel $PARALLEL | workers: ${WORKERS:-local-only}"
echo "=== universe: $(echo "$UNI" | tr ',' '\n' | wc -l) symbols"
echo "=================================================================="

rc_any=0
# ---------------------------------------------------------------------------
# ORDER: PHASE B (DeterministicScorer) RUNS FIRST, ahead of PHASE A (Senate).
# ---------------------------------------------------------------------------
# DeterministicScorer has NEVER completed a run. The only prior signal is 18 trials at
# fitness 1.72 in the large band, plus two zero-trade samples in mid/small that came from a
# grid killed after 2 trials -- an insufficient sample, NOT evidence the expert is broken.
# Senate is known-good, so running the unknown FIRST makes its first generation observable
# within minutes instead of after Senate's 14 jobs: if it produces nothing, that is found
# early and cheaply rather than a day later.


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

# MEASURED medians (Alpaca SIP historical quotes, 2026-08-17) -- small was 40, i.e. 2.4x too harsh.
ds_spread_for() { case "$1" in large) echo 3 ;; mid) echo 9 ;; small) echo 17 ;; *) echo 0 ;; esac; }

# REMOTE POOL SLOTS PER BAND. remote150's daemon runs --workers 12 as its CEILING; the master
# sizes the pool per job via BA2_MAX_REMOTE_SLOTS, applied as the TIGHTEST of (env, the expert's
# own max_remote_worker_slots) so this can only ever reduce concurrency.
#
# Sized on MEASURED per-trial footprint, which is a property of the BAND, not the expert:
#   large  ~105 screened symbols, ~2.5-3.5 GB/trial  -> 12 ran fine for three jobs
#   mid    ~765 screened symbols, ~6 GB/trial        -> 12 starved a 65 GB box
#
# This used to be engagement-only and therefore did nothing for memory: pool children were
# spawned once at daemon start and stayed resident with their last working set whether or not
# anything was dispatched to them, so the master could shed 12 -> 1 and remote150 still sat at
# 0.5-2.6% free. Since 6bddce4 the master calls POST /pool/resize at pre-flight, so the number
# below is the number of children that actually EXIST for the job -- it is now a real memory
# lever. The 2026-08-19 starvation was mostly a master-side dispatcher leak (fe1cba3), not
# footprint, so these numbers are deliberately conservative until re-measured against a run
# with correct concurrency.
ds_remote_slots_for() { case "$1" in large) echo 12 ;; *) echo 6 ;; esac; }

# PHASE B runs TWICE, mirroring tools/grid_goal2020.sh. The S1/S2/S3 answer lands FIRST so the
# primary result is available early; S5/S6/S7 then run as a purely additive second pass.
#
# WHY S5/S6/S7 ARE HERE AT ALL (added 2026-08-18): they were never in this driver's default
# (git: only ever S1,S2,S3,S4 -> S1,S2,S3) because the cap-band driver predates them -- they
# were added later as data-driven refinements and only ever wired into the Senate loop and
# grid_goal2020.sh. grid_goal2020.sh then SKIPS DeterministicScorer (DS_SKIP) because matrix 3
# owns it -- so DS was the one expert getting no S5/S6/S7 pass anywhere. Nothing technical
# prevented it: every S-strategy is expert-agnostic (see _build_strategy), and the only
# expert-specific step, _clamp_confidence_genes, already knows DeterministicScorer.
ds_matrix() {                        # $@ = extra driver args (e.g. --strategies S5,S6,S7)
  for MODE in risk_atr notional; do
    for band in large mid small; do
      sp="$(ds_spread_for "$band")"
      st=$(awk -v s="$sp" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')
      ds_stress=()
      awk -v v="$st" 'BEGIN{exit !(v>0)}' && ds_stress=(--stress-spread-bps "$st")
      echo
      export BA2_MAX_REMOTE_SLOTS="$(ds_remote_slots_for "$band")"
      echo "=== PHASE B  DeterministicScorer / $MODE / $band  (spread ${sp}, stress +${st}, remote slots ${BA2_MAX_REMOTE_SLOTS}) $*  $(date)"
      "$PY" "$DRIVER"       --start 2020-01-01 --end 2025-12-31       --fitness consistent_annual_return       --store "$STORE"       --sizing-mode "$MODE" --bands "$band"       --spread-bps "$sp" "${ds_stress[@]}" "${robust_args[@]}"       --parallel "$DS_PARALLEL"       --name-suffix="-goal2020-${MODE}-ds"       --skip-experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker       "${WORKER_ARGS[@]}" "$@"
      rc=$?
      echo "=== PHASE B  $MODE / $band  done rc=$rc $(date)"
      [ $rc -ne 0 ] && rc_any=$rc
    done
  done
}

ds_matrix

# Set DS_STRATEGIES_EXTRA= to skip the additive pass.
DS_STRATEGIES_EXTRA="${DS_STRATEGIES_EXTRA-S5,S6,S7}"
if [ -n "$DS_STRATEGIES_EXTRA" ]; then
  echo; echo "=== PHASE B EXTRA  DeterministicScorer strategies=$DS_STRATEGIES_EXTRA  $(date)"
  ds_matrix --strategies "$DS_STRATEGIES_EXTRA"
fi


# Grid-driven remote concurrency cap for the memory-heavy expert about to run -- see
# SENATE_REMOTE_SLOTS above. Applied by strategy_optimization_handler as the TIGHTEST of
# (this env var, the worker's own reported capacity), same mechanism ds_matrix already uses.
export BA2_MAX_REMOTE_SLOTS="$SENATE_REMOTE_SLOTS"
for MODE in risk_atr notional; do
  for S in $STRATEGIES; do
    NAME="sen-${S}-goal2020-${MODE}"
    echo
    echo "=== $NAME  start $(date) (remote slots ${BA2_MAX_REMOTE_SLOTS})"
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
      --initial-capital 10000 \
      --spread-bps "$SPREAD_BPS" "${stress_args[@]}" "${robust_args[@]}" \
      --profit-cap-pct 2000 --profit-share-cap-pct 25 \
      "${WORKER_ARGS[@]}" \
      --labels "goal2020-senate,${S},${MODE}" \
      --name "$NAME"
    rc=$?
    echo "=== $NAME  done rc=$rc $(date)"
    [ $rc -ne 0 ] && rc_any=$rc
  done
done

echo
echo "=== MATRIX 3 COMPLETE rc=$rc_any $(date) ==="
exit $rc_any

#!/usr/bin/env bash
# goal2027atr -- the risk-ATR grid (design: docs/strategy_research/atr_grid/atr_grid_2027_design.md).
#
# PHASES (PHASE=...):
#   fr     FactorRanker, one job per cap band (large, mid, small). FactorRanker bypasses the
#          classic RM, so ATR, market gates and market exits do not apply to it (design §4.1).
#          Runnable now.
#   atr    The S1-S7 lane (per-expert strategy plan, ATR searched, market conditions togglable via
#          the market:enabled master gene; 28 equity jobs). Senate is a separate lane.
#   senate The FMPSenateTraderWeight lane (design §4/§7, 4 jobs: S1/S3/S5/S6 -- the strategy_plan.json
#          Senate row). Senate has no --bands dimension (its own disclosure-derived universe, not a
#          cap-band screener union -- see tools/run_senate_matrix.py's module docstring), so this
#          phase drives tools/run_senate_matrix.py directly rather than run_screener_capband_matrix.py.
#
# WINDOW: 2020-01-01 -> 2025-12-31, fit on six clean calendar years; 2026-H1 is the out-of-sample
# holdout (operator decision 2026-09-29, design D1). Same reasoning as grid_goal2020.sh's header.
#
# GA BUDGET (design §7): population clamp(4 x genes, 24, 120); 25 generations, 30 above 20 genes;
# early stop 5 generations without a >= 1% relative gain. FactorRanker has 14 genes (7 expert,
# 1 RM, 6 screener) -> population 56, 25 generations.
#
# FLEET: remote-only by default (PARALLEL=0): this box also runs prod/dev/8082. BA2_MAX_REMOTE_SLOTS
# caps the remote pool (the master resizes the worker pool per job from the measured peak child).
#
# Resumable: the driver skips any job whose StrategyOptimization row is `completed`.
set -u
cd "$(dirname "$0")/.."
# ABSOLUTE repo path (Windows form under Git Bash): the launcher resolves @file arguments against
# ITS OWN working directory, which is not this one -- a relative @docs/... path failed every
# atr job with FileNotFoundError on the first launch (2026-10-01).
REPO_ROOT="$(pwd -W 2>/dev/null || pwd)"

GRID_LOG="${GRID_LOG:-grid_atr27.log}"
export PYTHONUNBUFFERED=1
exec > >(tee -a "$GRID_LOG") 2>&1

PHASE="${PHASE:-fr}"
PY="${PY:-.venv/Scripts/python.exe}"
DRIVER=tools/run_screener_capband_matrix.py
START="${START:-2020-01-01}"
END="${END:-2025-12-31}"
FITNESS=consistent_annual_return
STORE="$HOME/Documents/ba2/common/cache/screener/metric_store"
WORKERS="${WORKERS-remote150}"
PARALLEL="${PARALLEL-0}"
export BA2_MAX_REMOTE_SLOTS="${BA2_MAX_REMOTE_SLOTS:-10}"
INTERVAL="${INTERVAL:-5min}"
BANDS="${BANDS:-large mid small}"
EARLY_STOP="${EARLY_STOP:-5}"
EARLY_STOP_MIN_REL="${EARLY_STOP_MIN_REL:-0.01}"
SPREAD_BPS_LARGE="${SPREAD_BPS_LARGE:-3}"
SPREAD_BPS_MID="${SPREAD_BPS_MID:-9}"
SPREAD_BPS_SMALL="${SPREAD_BPS_SMALL:-17}"
STRESS_SPREAD_MULT="${STRESS_SPREAD_MULT:-1.5}"

spread_for() {
  case "$1" in
    large) echo "$SPREAD_BPS_LARGE" ;;
    mid)   echo "$SPREAD_BPS_MID" ;;
    small) echo "$SPREAD_BPS_SMALL" ;;
    *)     echo 0 ;;
  esac
}

echo "=================================================================="
echo "=== goal2027atr  phase=$PHASE  $START -> $END  fitness=$FITNESS  $(date)"
echo "=== workers: ${WORKERS:-local-only}  local parallel: $PARALLEL  remote slots: $BA2_MAX_REMOTE_SLOTS"
echo "=== early stop: $EARLY_STOP generations @ min relative gain $EARLY_STOP_MIN_REL"
echo "=================================================================="

# The metric-store + cap-band screened-union preflight below is EQUITY-ONLY (fr/atr): Senate
# uses neither $STORE nor $BANDS (its own disclosure-derived universe -- see run_senate()'s own
# preflight further down), so it is skipped for PHASE=senate. This also keeps the Senate lane
# launchable on its own (design §7.2: "run Senate on its own lane in parallel") even when the
# equity metric store/cache isn't ready yet.
if [ "$PHASE" != "senate" ]; then
echo "=== PREFLIGHT: metric store reaches back to 2020-01"
"$PY" - "$STORE" <<'EOF' || exit 1
import sys, pathlib
store = pathlib.Path(sys.argv[1])
yms = sorted(p.name.split("=", 1)[1] for p in store.glob("ym=*"))
if not yms:
    sys.exit(f"FATAL: no ym=* partitions under {store}")
print(f"metric store: {len(yms)} partitions, {yms[0]} .. {yms[-1]}")
if yms[0] > "2020-01" or yms[-1] < "2025-12":
    sys.exit(f"FATAL: metric store {yms[0]}..{yms[-1]} does not span the 2020-01..2025-12 window")
EOF

echo "=== PREFLIGHT: each band's screened universe + cache coverage"
_UNIV_DIR="$(mktemp -d)"
"$PY" - "$STORE" "$START" "$END" "$_UNIV_DIR" <<'EOF' || exit 1
import sys
from ba2_providers.screener import metric_store as ms
store, start, end, outdir = sys.argv[1:5]
store_df = ms.load_store(store)
BANDS = {"small": (5e7, 2e9), "mid": (2e9, 1e10), "large": (1e10, None)}
for band, (lo, hi) in BANDS.items():
    loosest = {"market_cap_min": lo, "relative_volume_min": 0.0, "price_drop_pct": 0.0,
               "weinstein_stage2_only": 0, "max_stocks": 50}
    if hi is not None:
        loosest["market_cap_max"] = hi
    union = ms.screened_symbol_union(store_df, start, end, loosest)
    with open(f"{outdir}/{band}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(union))
    print(f"  {band}: {len(union)} symbol(s) ever screened-in")
EOF
_cov_fail=0
for _band in $BANDS; do
  for _iv in "$INTERVAL" 1d; do
    echo "--- coverage: band=${_band} interval=${_iv}"
    "$PY" tools/check_window_coverage.py --interval "$_iv" --start "$START" \
        --symbols "@$_UNIV_DIR/${_band}.txt" --sample 150 --min-covered-pct 75 || _cov_fail=1
  done
done
if [ "$_cov_fail" = "1" ]; then
  echo "=== PREFLIGHT FAILED: cache coverage. Set GRID_SKIP_PREFLIGHT=1 to run on a reduced universe."
  [ "${GRID_SKIP_PREFLIGHT:-0}" = "1" ] || exit 1
fi
fi  # [ "$PHASE" != "senate" ]

COMMON=(--start "$START" --end "$END" --fitness "$FITNESS" --store "$STORE" --interval "$INTERVAL"
        --early-stop "$EARLY_STOP" --early-stop-min-rel "$EARLY_STOP_MIN_REL"
        --robust-fitness --parallel "$PARALLEL")
[ -n "$WORKERS" ] && COMMON+=(--workers "$WORKERS")
# S1-S7 phases ONLY (treatment and all-off control alike): IAC's FMP spin-off split-basis drift
# fails the market-condition coverage check for every gated job
# (docs/strategy_research/atr_grid/excluded_symbols.txt). NOT for FactorRanker: it uses no
# market-condition data and runs on the full universe (operator 2026-09-29).
S17_EXCLUDE=(--exclude-symbols "@${REPO_ROOT}/docs/strategy_research/atr_grid/excluded_symbols.txt")

run_bands() {                      # $1=name-suffix  $2...=extra driver args
  local suffix="$1"; shift
  for band in $BANDS; do
    local sp st; sp="$(spread_for "$band")"
    st="$(awk -v s="$sp" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')"
    echo; echo "=== $PHASE / $band  (spread ${sp} bps, stress +${st})  $(date)"
    "$PY" "$DRIVER" "${COMMON[@]}" --bands "$band" --spread-bps "$sp" --stress-spread-bps "$st" \
        --name-suffix="$suffix" "$@"
    echo "=== $PHASE / $band  done rc=$? $(date)"
  done
}

# --- Senate lane (PHASE=senate): own driver, own universe, no --bands dimension --------------
SENATE_DRIVER=tools/run_senate_matrix.py
# docs/strategy_research/atr_grid/senate_universe_2020_2025.txt -- a FRESH derivation for THIS
# grid's 2020-2025 window (729 symbols; see that file's header for how it was built and its
# overlap with the equity S1-S7 snapshot union). NOT tools/senate_universe.txt, which was built
# for a different window. Senate's own preflight below refuses to start if it is missing.
SENATE_UNIVERSE_FILE="${SENATE_UNIVERSE_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/senate_universe_2020_2025.txt}"
# goal2020 matrix3's measured Senate settings (tools/grid_goal2020_matrix3.sh): 5min execution
# clock (TP/SL exits are NOT low-frequency even though disclosure ENTRIES are -- see that
# script's header), $10k initial capital, spread 9 bps (measured mid-band median -- Senate's
# universe spans all cap bands so this is one blended assumption, not per-band), stress x1.5.
SENATE_INTERVAL="${SENATE_INTERVAL:-5min}"
SENATE_CAPITAL="${SENATE_CAPITAL:-10000}"
SENATE_SPREAD_BPS="${SENATE_SPREAD_BPS:-9}"
SENATE_STRESS_BPS="$(awk -v s="$SENATE_SPREAD_BPS" -v m="$STRESS_SPREAD_MULT" 'BEGIN{printf "%.6g", s*m}')"
# IAC's FMP spin-off split-basis drift (S17_EXCLUDE above) does NOT apply here: IAC is not in the
# Senate universe (verified 2026-09-29 against senate_universe_2020_2025.txt's 729 symbols), so
# no --exclude-symbols is forwarded. Re-check this if SENATE_UNIVERSE_FILE is ever regenerated.
if grep -qx "IAC" "$SENATE_UNIVERSE_FILE" 2>/dev/null; then
  echo "FATAL: IAC is in $SENATE_UNIVERSE_FILE -- the split-basis drift that motivates" >&2
  echo "       S17_EXCLUDE now applies to Senate too; wire --exclude-symbols into" >&2
  echo "       tools/run_senate_matrix.py (not currently plumbed -- see its module docstring)" >&2
  echo "       and pass it here before launching PHASE=senate." >&2
  [ "$PHASE" = "senate" ] && exit 1
fi

run_senate() {                     # $@ = extra driver args
  if [ ! -f "$SENATE_UNIVERSE_FILE" ]; then
    echo "FATAL: Senate universe file not found: $SENATE_UNIVERSE_FILE" >&2
    exit 1
  fi
  # Published 2026-09-29 for the 727-symbol Senate universe (BRK.B / XSP removed, see its header).
  MC_SEN_OHLCV="${MC_SEN_OHLCV:-8cd4a4bc34f1cf3b072b8de064eb66244dd8a0011d1df21773c3f4ce071c69f1}"
  MC_SEN_TA="${MC_SEN_TA:-4eb35501180ecc51ff00ce60595caf0cc60247ef2f4fb43294c845aa7aae443d}"
  if [ -z "${MC_SEN_OHLCV:-}" ] || [ -z "${MC_SEN_TA:-}" ]; then
    echo "FATAL: PHASE=senate requires MC_SEN_OHLCV and MC_SEN_TA (the Senate market-condition" >&2
    echo "       snapshot digests). The equity snapshot (MC_OHLCV/MC_TA) does NOT cover the" >&2
    echo "       Senate universe -- design §5.3/§9 D7 -- and no Senate snapshot has been warmed" >&2
    echo "       yet. Run tools/warm_market_conditions.py plan/build/verify/prepare-host over" >&2
    echo "       $SENATE_UNIVERSE_FILE first, then set MC_SEN_OHLCV=<digest> MC_SEN_TA=<digest>." >&2
    exit 1
  fi
  echo
  echo "=== PREFLIGHT senate: cache coverage  interval=${SENATE_INTERVAL}/1d  window ${START} -> ${END}"
  local _sen_cov_fail=0
  for _iv in "$SENATE_INTERVAL" 1d; do
    echo "--- coverage: interval=${_iv}"
    "$PY" tools/check_window_coverage.py --interval "$_iv" --start "$START" \
        --symbols "@$SENATE_UNIVERSE_FILE" --sample 150 --min-covered-pct 75 || _sen_cov_fail=1
  done
  if [ "$_sen_cov_fail" = "1" ]; then
    echo "=== PREFLIGHT FAILED: Senate cache coverage. Set GRID_SKIP_PREFLIGHT=1 to run anyway."
    [ "${GRID_SKIP_PREFLIGHT:-0}" = "1" ] || exit 1
  fi
  echo
  echo "=== senate  (spread ${SENATE_SPREAD_BPS} bps, stress +${SENATE_STRESS_BPS})  $(date)"
  # BA2_MAX_REMOTE_SLOTS: left at the grid's top-level default (10, see line 37) -- NOT
  # hard-capped for Senate. The old ~11-12 GB/trial figure (grid_goal2020_matrix3.sh,
  # grid_senate_5min.sh) predates the shared-array cache (bar caches and derived data are now
  # memory-mapped and shared across GA workers -- project-shared-arrays-across-workers), so it is
  # no longer a reliable sizing basis. MEASURE the real per-child RSS on the FIRST Senate job of
  # this phase (the master's pre-flight pool-resize call / `POST /pool/resize` reports the
  # measured peak child, same as every other expert here); if it comes in high enough to threaten
  # the box, lower BA2_MAX_REMOTE_SLOTS for a re-run (env var, not a code change) -- e.g.
  # `BA2_MAX_REMOTE_SLOTS=4 PHASE=senate bash tools/grid_atr27.sh` continues the same (resumable)
  # jobs at a smaller pool.
  local sen_cmd=("$PY" "$SENATE_DRIVER" --start "$START" --end "$END" --fitness "$FITNESS"
      --interval "$SENATE_INTERVAL" --initial-capital "$SENATE_CAPITAL"
      --spread-bps "$SENATE_SPREAD_BPS" --stress-spread-bps "$SENATE_STRESS_BPS"
      --universe-file "$SENATE_UNIVERSE_FILE"
      --early-stop "$EARLY_STOP" --early-stop-min-rel "$EARLY_STOP_MIN_REL"
      --robust-fitness --parallel "$PARALLEL"
      --population 120 --generations 30
      --sizing-mode risk_atr --rm-toggle-policy atr-searched
      --market-condition-profile ohlcv-v1,ta-structure-v1
      --market-condition-manifest "ohlcv-v1=${MC_SEN_OHLCV},ta-structure-v1=${MC_SEN_TA}"
      --market-exit exit,stop,tp --search-sl-loosen
      --strategies S1,S3,S5,S6
      --name-suffix="${SENATE_SUFFIX:--atr27-riskatr}")
  [ -n "$WORKERS" ] && sen_cmd+=(--workers "$WORKERS")
  "${sen_cmd[@]}" "$@"
  echo "=== senate  done rc=$? $(date)"
}

case "$PHASE" in
  fr)
    # 14 genes -> population 56, 25 generations (design §7 rule).
    # FR_SUFFIX: a NEW suffix restarts the lane from scratch. Needed whenever a code change
    # invalidates results already scored under the old name, because a job with the SAME name
    # resumes its GA checkpoint (2026-09-29: -atr27-fr was scored with the ProtectiveStopError
    # trial crash and is restarted as -atr27-fr2 on BT's fix).
    #
    # --fr-top-n-below-pool (the "ranking inert" trap: the goal2027atr FactorRanker mid band
    # converged with top_n >= screener_max_stocks, so the factor weights did nothing) is DEFAULT
    # ON in the launcher as of 2026-09-29 -- no flag is forwarded here for it, and none is needed:
    # every job this lane launches gets the repair automatically. Set
    # FR_SUFFIX=-atr27-fr4 (a NEW suffix) for THIS reason when re-running the lane on the
    # default-on behaviour, so the repaired results are never confused with (or accidentally
    # resume the checkpoint of) the un-repaired -atr27-fr/-fr2/-fr3 rows scored before this
    # default existed. To reproduce the OLD (un-repaired) behaviour instead, pass
    # --no-fr-top-n-below-pool explicitly (not currently plumbed as an env switch here -- add
    # one only if a real need for it shows up; see run_screener_capband_matrix.py --help).
    #
    # BANDS (default "large mid small", set above): also applies here unchanged -- run_bands
    # loops `for band in $BANDS`, so e.g. `BANDS=mid PHASE=fr bash tools/grid_atr27.sh` runs
    # only the mid-band FactorRanker job.
    run_bands "${FR_SUFFIX:--atr27-fr}" \
        --skip-experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,DeterministicScorer,FMPSenateTraderWeight \
        --population 56 --generations 25
    ;;
  atr)
    # The S1-S7 ATR + market-condition lane: ONE GA per (expert, band, strategy) cell, no all-off
    # control GA (operator 2026-09-29). Market conditions are togglable by the GA through the
    # master gene market:enabled, ATR through use_atr_stop; winners are ablated afterwards with
    # tools/strategy_research/atr_grid/ablate_market.py.
    #   * strategies per expert: docs/strategy_research/atr_grid/strategy_plan.json (28 equity
    #     jobs; Senate runs as its own lane -- it needs its own snapshot and driver flags);
    #   * budget: every job has > 30 genes -> population 120, 30 generations (design §7),
    #     identical for every job (--no-budget-overrides);
    #   * sizing risk_atr only (D2); ATR searched (--rm-toggle-policy atr-searched);
    #   * snapshots pinned below (2,045 of 2,046 symbols; IAC excluded, see S17_EXCLUDE).
    MC_OHLCV="${MC_OHLCV:-d979c9bc59fcee65230c80f9aa4565c3dbd454db506324c38375c799ae868f5e}"
    MC_TA="${MC_TA:-97c7bc8c13b2838633769a2723dace9469100d3f16431009f1705f06a0f501e3}"
    run_bands "${ATR_SUFFIX:--atr27-riskatr}"         --skip-experts FactorRanker         --strategy-plan docs/strategy_research/atr_grid/strategy_plan.json         --no-budget-overrides --population 120 --generations 30         --sizing-mode risk_atr --rm-toggle-policy atr-searched         --market-condition-profile ohlcv-v1,ta-structure-v1         --market-condition-manifest "ohlcv-v1=${MC_OHLCV},ta-structure-v1=${MC_TA}"         --market-exit exit,stop,tp --search-sl-loosen         "${S17_EXCLUDE[@]}"
    ;;
  senate)
    # The Senate lane: 4 jobs (S1/S3/S5/S6), same GA budget/flags as PHASE=atr (population 120,
    # 30 generations, early-stop 5 @ 1%, robust fitness, risk_atr sizing, ATR searched, market
    # conditions on with the Senate-specific manifest digests). run_senate_matrix.py has no
    # per-strategy/population override logic (unlike run_screener_capband_matrix.py's S1-140/
    # S7-60x8 overrides), so there is no --no-budget-overrides flag to pass here -- --population
    # 120 --generations 30 already applies to every job as-is.
    #
    # MC_SEN_OHLCV / MC_SEN_TA: the Senate market-condition snapshot digests. MUST be set --
    # refused otherwise (run_senate below). The equity MC_OHLCV/MC_TA snapshot does not cover
    # the Senate universe (a different, disclosure-derived symbol set), so it cannot be reused.
    run_senate
    ;;
  *)
    echo "FATAL: unknown PHASE=$PHASE (known: fr, atr, senate)"; exit 2 ;;
esac

echo; echo "=== goal2027atr phase=$PHASE COMPLETE $(date)"
echo "Report: .venv/Scripts/python.exe tools/report_grid_results.py --like %atr27%"

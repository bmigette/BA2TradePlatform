#!/usr/bin/env bash
# goal2027atr -- the risk-ATR grid (design: docs/strategy_research/atr_grid/atr_grid_2027_design.md).
#
# PHASES (PHASE=...):
#   fr   FactorRanker, one job per cap band (large, mid, small). FactorRanker bypasses the classic
#        RM, so ATR, market gates and market exits do not apply to it (design §4.1). Runnable now.
#   atr  The S1-S7 lane (per-expert strategy plan, ATR searched, market conditions togglable via
#        the market:enabled master gene; 28 equity jobs). Senate is a separate lane.
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

COMMON=(--start "$START" --end "$END" --fitness "$FITNESS" --store "$STORE" --interval "$INTERVAL"
        --early-stop "$EARLY_STOP" --early-stop-min-rel "$EARLY_STOP_MIN_REL"
        --robust-fitness --parallel "$PARALLEL")
[ -n "$WORKERS" ] && COMMON+=(--workers "$WORKERS")
# S1-S7 phases ONLY (treatment and all-off control alike): IAC's FMP spin-off split-basis drift
# fails the market-condition coverage check for every gated job
# (docs/strategy_research/atr_grid/excluded_symbols.txt). NOT for FactorRanker: it uses no
# market-condition data and runs on the full universe (operator 2026-09-29).
S17_EXCLUDE=(--exclude-symbols "@docs/strategy_research/atr_grid/excluded_symbols.txt")

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

case "$PHASE" in
  fr)
    # 14 genes -> population 56, 25 generations (design §7 rule).
    # FR_SUFFIX: a NEW suffix restarts the lane from scratch. Needed whenever a code change
    # invalidates results already scored under the old name, because a job with the SAME name
    # resumes its GA checkpoint (2026-09-29: -atr27-fr was scored with the ProtectiveStopError
    # trial crash and is restarted as -atr27-fr2 on BT's fix).
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
  *)
    echo "FATAL: unknown PHASE=$PHASE (known: fr, atr)"; exit 2 ;;
esac

echo; echo "=== goal2027atr phase=$PHASE COMPLETE $(date)"
echo "Report: .venv/Scripts/python.exe tools/report_grid_results.py --like %atr27%"

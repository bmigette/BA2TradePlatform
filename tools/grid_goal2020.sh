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

COMMON=(--start "$START" --end "$END" --fitness "$FITNESS" --store "$STORE")

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

# --- matrix 1: risk_atr ------------------------------------------------------------------------
# NOTE the `=` in --name-suffix=-... : the value starts with a dash, which argparse would
# otherwise read as another flag.
echo; echo "=== matrix 1/2  risk_atr  $(date)"
"$PY" "$DRIVER" "${COMMON[@]}" \
  --sizing-mode risk_atr \
  --name-suffix=-goal2020-riskatr \
  "$@"
echo "=== matrix 1/2  done rc=$? $(date)"

# --- matrix 2: notional ------------------------------------------------------------------------
echo; echo "=== matrix 2/2  notional  $(date)"
"$PY" "$DRIVER" "${COMMON[@]}" \
  --sizing-mode notional \
  --name-suffix=-goal2020-notional \
  --skip-experts FactorRanker \
  "$@"
echo "=== matrix 2/2  done rc=$? $(date)"

echo; echo "=== goal2020 COMPLETE $(date) ==="
echo "Compare with:  ba2-test report   (labels: goal2020-riskatr / goal2020-notional)"
echo "Worth checking beyond fitness: whether the two modes put"
echo "max_virtual_equity_per_instrument_percent in DIFFERENT places — that tells you if they are"
echo "genuinely different strategies or just re-parameterisations of the same one."

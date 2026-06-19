#!/usr/bin/env bash
# Reproduce the Phase-1 strategy-optimization grid on any device.
#
# Runs ONE optimize-batch grid over {FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy}
# x {S1, S2, S3} + FactorRanker, on the 30-symbol NASDAQ universe, scored by Calmar,
# on the 5min fill clock. S1 strategies are imported from the tracked live rulesets in
# docs/live_rulesets/*.json; S2/S3 + FactorRanker are built in ba2test_launcher.py — so
# everything needed is in git. The ONLY external prerequisite is the 5min OHLCV cache,
# which this script pre-populates (with a warmup buffer) before launching.
#
# Usage:
#   scripts/run_phase1_grid.sh                      # default 1-year window (2023-01-01..2024-01-01)
#   START=2023-01-01 END=2026-01-01 scripts/run_phase1_grid.sh   # 3-year window
#   INTERVAL=15min scripts/run_phase1_grid.sh       # coarser/faster search clock
#
# Prereqs (once per device):
#   - venvs installed (install script) so `ba2-test` is on PATH, OR set BA2_TEST=/path/to/ba2-test.exe
#   - FMP_API_KEY (and other keys) in .env
#   - a serve backend running:  ba2-test serve --mode back   (this script will remind you if it's down)
set -euo pipefail

# ---- knobs (override via env) ---------------------------------------------------------
START="${START:-2023-01-01}"
END="${END:-2024-01-01}"
INTERVAL="${INTERVAL:-5min}"
POPULATION="${POPULATION:-40}"
GENERATIONS="${GENERATIONS:-8}"
PARALLEL="${PARALLEL:-6}"
FITNESS="${FITNESS:-calmar_ratio}"
EXPERTS="${EXPERTS:-FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker}"
STRATEGIES="${STRATEGIES:-S1,S2,S3}"
UNIVERSE="${UNIVERSE:-AAPL,MSFT,NVDA,AMZN,META,GOOGL,AVGO,TSLA,COST,NFLX,AMD,PEP,ADBE,CSCO,TMUS,INTC,QCOM,INTU,AMAT,TXN,AMGN,ISRG,BKNG,HON,VRTX,ADP,SBUX,GILD,MU,LRCX}"
BA2_TEST="${BA2_TEST:-ba2-test}"            # path to the ba2-test console entry point
API="${API:-http://localhost:8000}"        # serve backend URL

# Warmup buffer: experts/RM need lookback before START. Fetch ~6 months earlier so the
# as-of cache covers the auto-derived warmup window (FMPRating price-target window is up to
# 180d; classic-RM indicators add more). Cheap insurance against mid-run cache misses.
CACHE_START="$(python -c "import datetime,sys; d=datetime.date.fromisoformat('$START'); print(d-datetime.timedelta(days=210))" 2>/dev/null || echo "$START")"

echo ">> Phase-1 grid: experts=$EXPERTS strategies=$STRATEGIES interval=$INTERVAL window=$START..$END"
echo ">> universe: $(echo "$UNIVERSE" | tr ',' ' ' | wc -w) symbols"

# ---- 1. serve must be up (optimize-batch submits to its task queue and polls) ----------
if ! curl -fs --max-time 5 "$API/api/tasks?limit=1" >/dev/null 2>&1; then
  echo "!! serve backend not reachable at $API"
  echo "   start it first:  $BA2_TEST serve --mode back"
  exit 1
fi

# ---- 2. pre-cache the 5min OHLCV (idempotent: skips already-cached ranges) -------------
echo ">> pre-caching $INTERVAL OHLCV  $CACHE_START..$END"
"$BA2_TEST" fetch-cache \
  --symbols "$UNIVERSE" \
  --timeframes "$INTERVAL" \
  --start "$CACHE_START" --end "$END" \
  --provider fmp --workers 5

# ---- 2b. pre-warm the per-symbol FMP history disk cache (ratings/earnings/insider) so the
#          spawned GA workers read it from disk instead of each cold-fetching from FMP -------
echo ">> pre-warming FMP history cache for the experts"
"$BA2_TEST" prewarm --symbols "$UNIVERSE" --experts "$EXPERTS" --end "$END" --workers 5 || \
  echo "!! prewarm failed (non-fatal — workers will lazily cache); continuing"

# ---- 3. launch ONE grid (do NOT run two at once — duplicate grids corrupt the run) -----
echo ">> launching optimize-batch grid"
exec "$BA2_TEST" optimize-batch \
  --experts "$EXPERTS" \
  --strategies "$STRATEGIES" \
  --universe "$UNIVERSE" \
  --start "$START" --end "$END" \
  --fitness "$FITNESS" --interval "$INTERVAL" \
  --population "$POPULATION" --generations "$GENERATIONS" --parallel "$PARALLEL"

#!/usr/bin/env bash
# Options strategy grid: FMPRating x {the 10 option/equity strategies} over a 10
# mega-cap universe, on a DAILY analysis cadence + 1d fill clock (option cache bars
# are daily). Builds the offline options cache first (ba2-test fetch-options), then
# launches ONE optimize-batch grid. Mirrors run_phase1_grid.sh but for options.
#
# Usage:
#   scripts/run_options_grid.sh                       # most-recent ~3mo window
#   START=2024-03-01 END=2024-06-01 scripts/run_options_grid.sh
#
# Prereqs: venvs installed (ba2-test on PATH), keys in .env, an options-entitled
# Alpaca key for the cache build (account-3 "ba2New" in the live DB), serve backend up.
set -euo pipefail

START="${START:-}"          # empty => auto-detect a recent ~3mo window (set below)
END="${END:-}"
INTERVAL="${INTERVAL:-1d}"
POPULATION="${POPULATION:-12}"
GENERATIONS="${GENERATIONS:-4}"
PARALLEL="${PARALLEL:-4}"
FITNESS="${FITNESS:-total_return}"
EXPERTS="${EXPERTS:-FMPRating}"
STRATEGIES="${STRATEGIES:-O_LC,O_CC,O_VERT,O_STK,O_SSTG,O_SSTD,O_IC,O_JL,O_BF,O_RS}"
UNIVERSE="${UNIVERSE:-AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AVGO,AMD,NFLX}"
BA2_TEST="${BA2_TEST:-ba2-test}"
API="${API:-http://localhost:8000}"

# Auto-detect a recent ~3-month window ending ~last week (option daily bars settle
# with a short delay). Override with START/END.
if [[ -z "$END" ]]; then
  END="$(python -c "import datetime; print(datetime.date.today()-datetime.timedelta(days=7))")"
fi
if [[ -z "$START" ]]; then
  START="$(python -c "import datetime; d=datetime.date.fromisoformat('$END'); print(d-datetime.timedelta(days=92))")"
fi

echo ">> options grid: experts=$EXPERTS strategies=$STRATEGIES window=$START..$END interval=$INTERVAL"
echo ">> universe: $(echo "$UNIVERSE" | tr ',' ' ' | wc -w) symbols"

# serve must be up (optimize-batch submits to its task queue and polls).
if ! curl -fs --max-time 5 "$API/api/tasks?limit=1" >/dev/null 2>&1; then
  echo "!! serve backend not reachable at $API — start it: $BA2_TEST serve --mode back"; exit 1
fi

# 1. OHLCV cache (daily) for the underlier signals + 210d warmup for FMPRating.
CACHE_START="$(python -c "import datetime; d=datetime.date.fromisoformat('$START'); print(d-datetime.timedelta(days=210))")"
echo ">> pre-caching $INTERVAL OHLCV $CACHE_START..$END"
"$BA2_TEST" fetch-cache --symbols "$UNIVERSE" --timeframes "$INTERVAL" \
  --start "$CACHE_START" --end "$END" --provider fmp --workers 5

# 2. prewarm FMPRating history (ratings/targets) so GA workers read disk.
echo ">> pre-warming FMP history cache"
"$BA2_TEST" prewarm --symbols "$UNIVERSE" --experts "$EXPERTS" --end "$END" --workers 5 || \
  echo "!! prewarm failed (non-fatal); continuing"

# 3. build the offline OPTIONS cache (Alpaca; account-3 options-entitled key).
echo ">> building options cache $START..$END"
"$BA2_TEST" fetch-options --underlyings "$UNIVERSE" --start "$START" --end "$END"

# 4. launch ONE grid (daily cadence). Do NOT run two grids at once.
echo ">> launching optimize-batch options grid"
exec "$BA2_TEST" optimize-batch \
  --experts "$EXPERTS" --strategies "$STRATEGIES" --universe "$UNIVERSE" \
  --start "$START" --end "$END" --fitness "$FITNESS" --interval "$INTERVAL" \
  --run-schedule daily \
  --population "$POPULATION" --generations "$GENERATIONS" --parallel "$PARALLEL"

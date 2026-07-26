#!/usr/bin/env bash
# Make every NON-OPTION expert runnable from 2020-01-01, then pre-warm everything.
#
# Waits for the Senate 5min grid to finish first (it owns 4 local slots + remote150), then runs
# the EXISTING ba2-test tools in dependency order. No new tooling -- this is sequencing.
#
# WHY THIS ORDER -- each step feeds the next:
#
#   1. 1d OHLCV back to 2020.  NOT redundant with the 5min backfill already running:
#      build-screener-metrics reads OHLCV EXCLUSIVELY from 1d bars (confirmed by code trace in
#      docs/2026-07-16-test-account-spread-cache-audit-memo.md) -- 5min never touches
#      price_drop_pct / relative_volume / weinstein_stage / momentum / atr_*. Measured over the
#      ForwardTest union only 769/2409 (32%) of symbols have 1d back to 2020, so the metric
#      store cannot be extended without this. 1d is CHEAP compared with 5min: ~252 bars/yr
#      against ~19,656, and the daily endpoint is not subject to the ~624-bar intraday cap that
#      forces 8-day chunking.
#
#   2. Screener metric_store extended to 2020. Currently ym=2022-01..2026-06, so FactorRanker
#      and every screener-driven config silently gets a SHORTER window than the rest of a 2020
#      stress test. --market-cap-min 5e7 matches the 2026-07-16 rebuild (the documented
#      "loosest bound" that supports all 3 cap bands) so the extension keeps one consistent
#      semantic; a different floor would make pre/post-2022 rows incomparable.
#      NOTE: drop_days/cadence are BUILD-TIME and baked into the columns -- they must match the
#      existing store or the two halves mean different things. Defaults (drop-days 5,
#      cadence 7) are what the current store used.
#
#   3. Prewarm the per-symbol FMP history disk cache for every non-option expert. The Senate
#      5min A/B logged 331 prewarm misses across 146 tickers ("not pre-warmed (hermetic
#      backtest, 0 fetch)") -- each of those is a symbol the expert SKIPS, silently shrinking
#      the universe mid-run. --start matters for FMPSenateTraderWeight specifically: it
#      precomputes trader-skill scores per trading day instead of leaving them to lazy
#      per-trial computation.
#
# OPTIONS ARE DELIBERATELY EXCLUDED: the options cache is Alpaca-sourced and floors at
# 2024-01-18, so no amount of FMP data makes an option expert run from 2020. See
# docs/plans/2026-07-25-options-data-and-intraday-roadmap.md.
#
# FOR THE STRESS TEST THAT FOLLOWS THIS SCRIPT: write results to NEW Backtest rows with
# DISTINCT names -- never overwrite the originals. The 25 ForwardTest rows are the 2023-2026
# baseline and the only record of what each deployed config scored; a 2020-2026 re-run is a
# DIFFERENT experiment (longer window, gap-aware fills, costed options) and must sit beside
# them for comparison, not replace them. Note rerun_handler.py's optimization-derived path
# overwrites IN PLACE by design -- do not reuse it here. Follow rerun_senate_at_5min.py
# instead: build the config through the same decode_params -> _build_daily_trial_config path,
# then insert a new row (suggested name "<original>-2020", labels + "Stress2020").
#
# QUOTA: ~30GB available. The in-flight 5min backfill needs ~14GB of it; step 1 (1d) should be
# a small fraction of that, step 2 is cache-only for OHLCV but makes ONE live FMP call to
# enumerate the universe, step 3 is per-symbol history. Watch for real 403/'Limit Reach'
# (distinct from the harmless 429 backoff) -- see test_files/watch_fmp_datalimit_pause_fetch.py.
set -u
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python.exe
SC="$HOME/Documents/ba2"
START=2020-01-01
END=2026-06-30
GRID_LOG="${1:-}"

if [ -n "$GRID_LOG" ]; then
  echo "=== waiting for the Senate grid to finish ($GRID_LOG) ..."
  until grep -q "GRID COMPLETE" "$GRID_LOG" 2>/dev/null; do sleep 300; done
  echo "=== grid finished $(date); starting 2020 preparation"
fi

echo "=================================================================="
echo "=== STEP 1/3: 1d OHLCV back to $START   $(date)"
echo "=== (metric_store reads 1d ONLY; 5min does not feed it)"
echo "=================================================================="
"$PY" testplatform/ba2test_launcher.py fetch-cache \
  --symbols "@$SC/fwdtest_all_symbols.txt" \
  --timeframes 1d --start "$START" --end "$END" --workers 4
echo "=== step 1 rc=$? $(date)"

echo "=================================================================="
echo "=== STEP 2/3: extend screener metric_store to $START   $(date)"
echo "=================================================================="
"$PY" testplatform/ba2test_launcher.py build-screener-metrics \
  --store "$SC/common/cache/screener/metric_store" \
  --start "$START" --end "$END" \
  --market-cap-min 5e7 \
  --workers 4
echo "=== step 2 rc=$? $(date)"

echo "=================================================================="
echo "=== STEP 3/3: prewarm all non-option experts   $(date)"
echo "=================================================================="
"$PY" testplatform/ba2test_launcher.py prewarm \
  --symbols "@$SC/fwdtest_all_symbols.txt" \
  --experts FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker,FMPSenateTraderWeight \
  --start "$START" --end "$END" --workers 5
echo "=== step 3 rc=$? $(date)"
echo "=== 2020 PREPARATION COMPLETE $(date) ==="

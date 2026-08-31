#!/usr/bin/env bash
# Option grid STAGE 1 — isolated local run on babatest. Persistent home: /home/debian/ba2-grid
# (NOT /tmp — that is tmpfs-ish and died in the 2026-08-29 reboot).
# Isolation vs the running fleet (worker --port 8100, checkout /opt/ba2worker/BA2TradePlatform):
#   own BA2_HOME, own DB, own worktree code via --launcher, cgroup RAM+CPU caps.
#
# F4 (option-program-review-findings.md, 2026-08-30): next run uses 24 workers (operator,
# 2026-08-30). POP defaults to 140 (down from the design's spec'd 200, see docs/superpowers/
# specs/2026-08-27-option-ga-grid-design.md §8) -- that reduction is PROVISIONAL: pop may be
# reduced only if precision-neutral, and that has NOT been measured yet. Decision deferred to a
# pilot run that compares top-N stability at 140 vs 200 before trusting a 140-pop stage-1
# verdict. Override with POP=200 (or any value) to run the original spec while the pilot is
# pending.
set -u
cd /home/debian/ba2-grid/repo

FMP_KEY=$(/opt/ba2worker/ba2-venvs/test/bin/python -c "
import sqlite3
c = sqlite3.connect(\"/home/debian/ba2-grid/home/test/dl_forecasting.db\")
print(c.execute(\"SELECT value_str FROM appsetting WHERE key=?\", (\"FMP_API_KEY\",)).fetchone()[0])")

export BA2_HOME=/home/debian/ba2-grid/home
export DB_FILE=/home/debian/ba2-grid/home/test/dl_forecasting.db
export DATABASE_URL="sqlite:////home/debian/ba2-grid/home/test/dl_forecasting.db"
export FMP_API_KEY="$FMP_KEY"
export BACKTEST_OPTIONS_STORE=parquet
export PYTHONPATH=/home/debian/ba2-grid/repo/packages/common:/home/debian/ba2-grid/repo/packages/providers:/home/debian/ba2-grid/repo/packages/experts:/home/debian/ba2-grid/repo/testplatform/backend

# POP/GEN env overrides (F4, 2026-08-30). Defaults: POP 140 (provisional, see header note
# above), GEN 60 (unchanged from the design spec).
POP="${POP:-140}"
GEN="${GEN:-60}"

# Universe constraints (F4(a), grid design §6): the screener metric store attached PURELY as a
# GATE-ONLY per-bar entry gate (no universe switch, no screener:* genes -- see
# ba2test_launcher._screener_gate_opt_block). --max-stock-price is a SINGLE blanket cap and,
# passed alone, would cap EVERY structure at one price -- the review's "inert without the
# store" finding is really "the blanket cap is the wrong knob": the actual per-strategy caps
# the design calls for (O_CSP/O_JL/O_RS at $100, O_SSTD/O_SSTG at $300, everything else
# uncapped) are real `screener_gate_base` entries on those five `_OPTION_STRATS` members
# (ba2test_launcher.py, F4 2026-08-30) that WIN over the blanket default by design precedence.
# --max-stock-price 0 here disables that blanket default so every OTHER structure (all
# defined-risk: reserve is a function of wing width, not spot) stays uncapped, exactly as §6
# specifies.
#
# Prerequisite: the store must cover the options universe over the run window
# (tools/options_universe_top100.txt over 2023-01-01..2025-12-31). Build/extend it with:
#   ba2-test build-screener-metrics --start 2023-01-01 --end 2025-12-31 \
#     --market-cap-min 10000000000 --cadence-days 7
# (large-cap floor, weekly cadence -- matches the daily run-schedule's staleness tolerance
# noted in docs/superpowers/specs/2026-07-29-option-grid-max-stock-price-design.md).
SCREENER_STORE="${SCREENER_STORE:-${BA2_HOME}/common/cache/screener/metric_store}"
if [ ! -e "$SCREENER_STORE" ]; then
  echo "stage1_run.sh: screener metric store missing at $SCREENER_STORE -- build it first (see" >&2
  echo "the comment above this check in tools/stage1_run.sh), or set SCREENER_STORE to an" >&2
  echo "existing store. Refusing to launch stage 1 uncapped -- see F4(a) in" >&2
  echo "docs/superpowers/specs/2026-08-30-option-program-review-findings.md." >&2
  exit 1
fi

# 18 structures x 2 experts = 36 jobs; spec: gen 60, early-stop patience 8 (see POP above for
# population). Parquet (TastyTrade) store -> full spec window 2023-01-01..2025-12-31.
exec /opt/ba2worker/ba2-venvs/test/bin/python tools/run_options_matrix.py \
  --launcher /home/debian/ba2-grid/repo/testplatform/ba2test_launcher.py \
  --experts FMPRating,DeterministicScorer \
  --strategies O_LC,O_LP,O_VERT,O_BULLCS,O_BULLPS,O_BEARCS,O_BF,O_IC,O_JL,O_RS,O_SSTD,O_SSTG,O_CSP,O_STRD,O_STRG,O_CC,O_PP,O_WHEEL \
  --start 2023-01-01 --end 2025-12-31 \
  --population "$POP" --generations "$GEN" --early-stop 8 \
  --parallel 2 \
  --screener-gate-store "$SCREENER_STORE" --max-stock-price 0 \
  --name-suffix=-st1 \
  "$@"

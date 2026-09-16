#!/usr/bin/env bash
# Option grid STAGE 1 — isolated local run on babatest. Persistent home: /home/debian/ba2-grid
# (NOT /tmp — that is tmpfs-ish and died in the 2026-08-29 reboot).
# Isolation vs the running fleet (worker --port 8100, checkout /opt/ba2worker/BA2TradePlatform):
#   own BA2_HOME, own DB, own worktree code via --launcher, cgroup RAM+CPU caps.
#
# Discovery policy lives in run_options_matrix.py --profile discovery: 16 permitted
# structures x two experts, population 200, generations 60, patience 8. POP=140 remains
# available for an explicitly labelled stability pilot; equivalence is not established.
# Changed economic/search settings get a new job/checkpoint name. Old -st1 jobs are preserved.
#
# WINDOW + STORE (2026-09-14): the goal2020 window, 2020-01-01..2025-12-31, on the THETADATA
# store -- the only vendor whose history floor (2018-09-14) reaches 2020; tastytrade floors at
# 2022-10-01 and the alpaca sqlite at 2024-01-18 (backtest/options_store.py). The driver
# forwards --options-store to EVERY job explicitly (never left to the env), because a
# distributed trial carries no environment; BACKTEST_OPTIONS_STORE is still exported here
# because it is part of the discovery identity digest -- keep it equal to STAGE1_STORE.
# The store ACCEPTING the date does not prove the bars are there: check the ThetaData tree on
# THIS host covers tools/options_universe_top100.txt over the window before launching
# (local reference 2026-09-14: 98/98 symbols, expiries 2020-01-03..2026-09-11, 350 per symbol).
#
# PARALLELISM (2026-09-14). The FIRST attempts at 20 and 16 consumers were OOM-killed: the
# option reader held ~15.6 GB of private numpy per consumer (177.8M rows x ~88 B at the 2020
# window). Since commit c608ac05 the reader maps its arrays from a per-host DERIVED cache
# (`<CACHE_FOLDER>/_derived/...`, see docs/plans/2026-09-14-shared-arrays-across-workers.md):
# the columns are shared through the page cache once per host, and the private residue per
# consumer is the projections + the touched greeks rows (~1-3 GB). PARALLEL therefore starts
# at 24 and is TUNED FROM MEASUREMENT: watch the cgroup total and each child's PRIVATE bytes
# (`/proc/<pid>/smaps_rollup` Private_Clean+Private_Dirty -- RSS counts shared mapped pages
# and is misleading here), and raise/lower it between jobs. The fleet worker on this host must
# be IDLE (its pool parked at 1 slot) or the two will fight for RAM; check `free -g` first.
#
# PREWARM IS MANDATORY BEFORE A COLD LAUNCH (build transient ~2.3x the frame, ~7-8 GB for
# ThetaData TSLA; the store serialises builders per KEY only, so 24 cold consumers on
# different keys can OOM the host). From the repo root, same PYTHONPATH/BA2_HOME as below:
#   /opt/ba2worker/ba2-venvs/test/bin/python tools/build_shared_arrays.py #     --options-store thetadata --universe-file tools/options_universe_top100.txt #     --ohlcv-provider FMPOHLCVProvider --interval 1d --start 2020-01-01 --end 2025-12-31 #     --warmup-days 60 --jobs 4
# and run it TWICE: the second run must report 0 built / 98 opened before launching.
# BT_MAX_TASKS_PER_CHILD is raised from the handler default of 8: a recycle now costs a
# re-OPEN of mapped files (ms), not a re-parse, but the projections are still rebuilt.
set -euo pipefail
cd /home/debian/ba2-grid/repo

FMP_KEY=$(/opt/ba2worker/ba2-venvs/test/bin/python -c "
import sqlite3
c = sqlite3.connect(\"/home/debian/ba2-grid/home/test/dl_forecasting.db\")
print(c.execute(\"SELECT value_str FROM appsetting WHERE key=?\", (\"FMP_API_KEY\",)).fetchone()[0])")

export BA2_HOME=/home/debian/ba2-grid/home
export DB_FILE=/home/debian/ba2-grid/home/test/dl_forecasting.db
export DATABASE_URL="sqlite:////home/debian/ba2-grid/home/test/dl_forecasting.db"
export FMP_API_KEY="$FMP_KEY"
STAGE1_STORE="${STAGE1_STORE:-thetadata}"
export BACKTEST_OPTIONS_STORE="$STAGE1_STORE"
export BT_MAX_TASKS_PER_CHILD="${BT_MAX_TASKS_PER_CHILD:-32}"
export PYTHONPATH=/home/debian/ba2-grid/repo/packages/common:/home/debian/ba2-grid/repo/packages/providers:/home/debian/ba2-grid/repo/packages/experts:/home/debian/ba2-grid/repo/testplatform/backend

# Restore the approved search budget; reducing it requires the separate pilot evidence.
POP="${POP:-200}"
GEN="${GEN:-60}"
PARALLEL="${PARALLEL:-24}"

# Universe constraints (F4(a), grid design §6): the screener metric store attached PURELY as a
# GATE-ONLY per-bar entry gate (no universe switch, no screener:* genes -- see
# ba2test_launcher._screener_gate_opt_block). --max-stock-price is a SINGLE blanket cap and,
# passed alone, would cap EVERY structure at one price. Retain the current per-strategy
# caps: O_CSP/O_JL/O_RS and the inheriting O_WHEEL at $100; the other permitted singles
# have no extra spot cap here. O_SSTD/O_SSTG retain $300 caps in their builders but are
# excluded from search by the later risk decision. Actual sizing/assignment/volume rails
# still apply to every structure -- no spot cap does not mean every contract is affordable.
#
# Prerequisite: the store must cover the options universe over the run window
# (tools/options_universe_top100.txt over 2020-01-01..2025-12-31). Build/extend it with:
#   ba2-test build-screener-metrics --start 2020-01-01 --end 2025-12-31 \
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

# MARKET-CONDITION GATES (design 2026-09-15; plan Task 8). OFF unless MARKET_CONDITION_PROFILE
# names a registered profile (e.g. ohlcv-v1) -- with it unset this script is byte-for-byte the
# launch it has always been.
#
# The snapshot is prepared ONCE, here, before any job starts: plan (inventory + source preflight)
# -> build --cache-only (publish the manifest) -> verify (re-hash every object it references) ->
# prepare-host (map the arrays for this box). Every step must succeed; `set -e` plus the explicit
# messages below turn any failure into a refusal to launch rather than 32 jobs that each discover
# the same missing feature store. --cache-only is deliberate: a warmup that fetches while a grid
# waits is a surprise bill in provider calls and hours, so missing coverage is an actionable
# inventory item the operator resolves on purpose (re-fetch, or trim the universe).
#
# The published digest is then PINNED into every job (--market-condition-manifest). Without it the
# launcher refuses the run: each worker would otherwise compute 128-session indicators from
# whatever cache it happened to hold, with no two hosts provably agreeing.
MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-none}"
MARKET_CONDITION_MANIFEST="${MARKET_CONDITION_MANIFEST:-}"
MC_ARGS=()
if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then
  MC_PYTHON=/opt/ba2worker/ba2-venvs/test/bin/python
  MC_WARM=tools/warm_market_conditions.py
  MC_PLAN="${MC_PLAN:-/home/debian/ba2-grid/market_conditions_plan.json}"
  MC_UNIVERSE="${MC_UNIVERSE:-tools/options_universe_top100.txt}"
  MC_START="${STAGE1_START:-2020-01-01}"
  MC_END="${STAGE1_END:-2025-12-31}"
  MC_DRY=0
  case " $* " in *" --dry-run "*) MC_DRY=1 ;; esac
  if [ "$MC_DRY" = "1" ]; then
    echo "stage1_run.sh: market-condition warm step (profile $MARKET_CONDITION_PROFILE), run ONCE"
    echo "               before the matrix command below:"
    echo "  $MC_PYTHON $MC_WARM plan --profile $MARKET_CONDITION_PROFILE --universe-file $MC_UNIVERSE --start $MC_START --end $MC_END --out $MC_PLAN"
    echo "  $MC_PYTHON $MC_WARM build --plan $MC_PLAN --cache-only --print-digest   # -> MARKET_CONDITION_MANIFEST"
    echo "  $MC_PYTHON $MC_WARM verify --manifest \$MARKET_CONDITION_MANIFEST"
    echo "  $MC_PYTHON $MC_WARM prepare-host --manifest \$MARKET_CONDITION_MANIFEST --profile $MARKET_CONDITION_PROFILE"
    MARKET_CONDITION_MANIFEST="${MARKET_CONDITION_MANIFEST:-DIGEST-FROM-BUILD}"
  else
    "$MC_PYTHON" "$MC_WARM" plan --profile "$MARKET_CONDITION_PROFILE" \
      --universe-file "$MC_UNIVERSE" --start "$MC_START" --end "$MC_END" --out "$MC_PLAN" || {
      echo "stage1_run.sh: market-condition PLAN is actionable (missing coverage or a failed" >&2
      echo "source preflight) -- resolve it (re-fetch those symbols, or trim the universe) and" >&2
      echo "re-run. Refusing to launch a gated grid on an incomplete snapshot." >&2
      exit 1; }
    if [ -z "$MARKET_CONDITION_MANIFEST" ]; then
      MARKET_CONDITION_MANIFEST="$("$MC_PYTHON" "$MC_WARM" build --plan "$MC_PLAN" --cache-only \
        --print-digest)" || { echo "stage1_run.sh: market-condition BUILD failed" >&2; exit 1; }
    fi
    if [ -z "$MARKET_CONDITION_MANIFEST" ]; then
      echo "stage1_run.sh: market-condition build published no manifest" >&2
      exit 1
    fi
    "$MC_PYTHON" "$MC_WARM" verify --manifest "$MARKET_CONDITION_MANIFEST" || {
      echo "stage1_run.sh: market-condition VERIFY failed for $MARKET_CONDITION_MANIFEST" >&2
      exit 1; }
    "$MC_PYTHON" "$MC_WARM" prepare-host --manifest "$MARKET_CONDITION_MANIFEST" \
      --profile "$MARKET_CONDITION_PROFILE" || {
      echo "stage1_run.sh: market-condition PREPARE-HOST failed for $MARKET_CONDITION_MANIFEST" >&2
      exit 1; }
    echo "stage1_run.sh: market-condition profile $MARKET_CONDITION_PROFILE manifest $MARKET_CONDITION_MANIFEST prepared"
  fi
  MC_ARGS=(--market-condition-profile "$MARKET_CONDITION_PROFILE" \
           --market-condition-manifest "$MARKET_CONDITION_MANIFEST")
fi

# STAGE1_START/END allow explicit shorter pilots (a 2023 start prints LIMITED WINDOW and gets
# its own discovery identity). A dry-run (pass --dry-run) prints every resolved command.
exec /opt/ba2worker/ba2-venvs/test/bin/python tools/run_options_matrix.py \
  --profile discovery \
  --launcher /home/debian/ba2-grid/repo/testplatform/ba2test_launcher.py \
  --start "${STAGE1_START:-2020-01-01}" --end "${STAGE1_END:-2025-12-31}" \
  --options-store "$STAGE1_STORE" \
  --population "$POP" --generations "$GEN" --early-stop 8 \
  --parallel "$PARALLEL" \
  --screener-gate-store "$SCREENER_STORE" --max-stock-price 0 \
  --name-suffix=-st1 \
  ${MC_ARGS[@]+"${MC_ARGS[@]}"} \
  "$@"

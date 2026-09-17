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
# A PIN WITH THE PROFILE OFF IS A MISTAKE, NOT AN OMISSION (review 2026-09-16, F2 -- the same
# rule the matrix driver now applies to its flags). Exporting the digest and forgetting the
# profile used to launch the whole 32-job grid UNGATED, under the ordinary ungated job names,
# from an environment that says a snapshot is pinned -- and such a name can then be SKIPped
# against an existing ungated completion. Refused before anything is warmed or launched.
if [ "$MARKET_CONDITION_PROFILE" = "none" ] && [ -n "$MARKET_CONDITION_MANIFEST" ]; then
  echo "stage1_run.sh: MARKET_CONDITION_MANIFEST=$MARKET_CONDITION_MANIFEST is set but" >&2
  echo "MARKET_CONDITION_PROFILE is unset/none: nothing would read that snapshot and the grid" >&2
  echo "would run UNGATED under the ungated job names. Set MARKET_CONDITION_PROFILE, or unset" >&2
  echo "MARKET_CONDITION_MANIFEST." >&2
  exit 1
fi
if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then
  MC_PYTHON=/opt/ba2worker/ba2-venvs/test/bin/python
  MC_WARM=tools/warm_market_conditions.py
  MC_PLAN="${MC_PLAN:-/home/debian/ba2-grid/market_conditions_plan.json}"
  MC_UNIVERSE="${MC_UNIVERSE:-tools/options_universe_top100.txt}"
  MC_START="${STAGE1_START:-2020-01-01}"
  MC_END="${STAGE1_END:-2025-12-31}"
  MC_DRY=0
  case " $* " in *" --dry-run "*) MC_DRY=1 ;; esac
  # ONE SNAPSHOT PER PROFILE (Task 10): MARKET_CONDITION_PROFILE may be a comma list, and a
  # manifest names the single profile it was warmed for, so the whole plan/build/verify/
  # prepare-host sequence runs once per profile and the digests are passed on as profile=digest
  # pairs. Pre-setting MARKET_CONDITION_MANIFEST skips plan+build for the profiles it names --
  # and is then what the grid runs on, never built-and-discarded: a digest this script published
  # while the run used a different one would be a snapshot nobody compared. NOTE that skipping
  # `plan` also skips the SOURCE PREFLIGHT it runs (cache certification, the split-basis check):
  # a pre-set digest is a statement that those questions were answered when it was built, so
  # pass one only for a snapshot this same universe and window produced. verify + prepare-host
  # still run, so the digest is always re-hashed and mapped on this box before the grid starts.
  MC_PROFILES="$(echo "$MARKET_CONDITION_PROFILE" | tr ',' ' ')"
  MC_N=0
  for MC_P in $MC_PROFILES; do MC_N=$((MC_N + 1)); done
  MC_PRESET="$MARKET_CONDITION_MANIFEST"
  if [ -n "$MC_PRESET" ] && [ "$MC_N" -gt 1 ]; then
    # EVERY token, not just one of them: "ohlcv-v1=abc,deadbeef" carries an '=' and would
    # otherwise pass, and the bare second token would then be read as the digest of whichever
    # profile the loop reached last. Same rule the launcher applies to the flag.
    for MC_TOK in $(echo "$MC_PRESET" | tr ',' ' '); do
      case "$MC_TOK" in
        *=*) ;;
        *) echo "stage1_run.sh: MARKET_CONDITION_MANIFEST must be profile=digest pairs when more" >&2
           echo "than one profile is warmed ($MARKET_CONDITION_PROFILE); '$MC_TOK' is a bare" >&2
           echo "digest and cannot say which profile's snapshot it is." >&2
           exit 1 ;;
      esac
    done
  fi
  MC_PINS=""
  for MC_PROFILE in $MC_PROFILES; do
    MC_PLAN_P="${MC_PLAN%.json}.${MC_PROFILE}.json"
    MC_DIGEST=""
    for MC_TOK in $(echo "$MC_PRESET" | tr ',' ' '); do
      case "$MC_TOK" in
        "$MC_PROFILE="*) MC_DIGEST="${MC_TOK#*=}" ;;
        *=*) ;;
        *) MC_DIGEST="$MC_TOK" ;;
      esac
    done
    if [ -n "$MC_PRESET" ] && [ -z "$MC_DIGEST" ]; then
      echo "stage1_run.sh: MARKET_CONDITION_MANIFEST names no digest for profile $MC_PROFILE" >&2
      exit 1
    fi
    if [ "$MC_DRY" = "1" ]; then
      echo "stage1_run.sh: market-condition warm step (profile $MC_PROFILE), run ONCE"
      echo "               before the matrix command below:"
      echo "  $MC_PYTHON $MC_WARM plan --profile $MC_PROFILE --universe-file $MC_UNIVERSE --start $MC_START --end $MC_END --out $MC_PLAN_P"
      echo "  $MC_PYTHON $MC_WARM build --plan $MC_PLAN_P --cache-only --print-digest   # -> the $MC_PROFILE digest"
      echo "  $MC_PYTHON $MC_WARM verify --manifest <digest>"
      echo "  $MC_PYTHON $MC_WARM prepare-host --manifest <digest> --profile $MC_PROFILE"
      MC_DIGEST="${MC_DIGEST:-DIGEST-FROM-BUILD}"
    else
      if [ -z "$MC_DIGEST" ]; then
        "$MC_PYTHON" "$MC_WARM" plan --profile "$MC_PROFILE" \
          --universe-file "$MC_UNIVERSE" --start "$MC_START" --end "$MC_END" --out "$MC_PLAN_P" || {
          echo "stage1_run.sh: market-condition PLAN is actionable for $MC_PROFILE (missing" >&2
          echo "coverage or a failed source preflight) -- resolve it (re-fetch those symbols, or" >&2
          echo "trim the universe) and re-run. Refusing to launch a gated grid on an incomplete" >&2
          echo "snapshot." >&2
          exit 1; }
        MC_DIGEST="$("$MC_PYTHON" "$MC_WARM" build --plan "$MC_PLAN_P" --cache-only \
          --print-digest | tail -n 1)" || {
          echo "stage1_run.sh: market-condition BUILD failed for $MC_PROFILE" >&2; exit 1; }
        if [ -z "$MC_DIGEST" ]; then
          echo "stage1_run.sh: market-condition build published no manifest for $MC_PROFILE" >&2
          exit 1
        fi
      else
        echo "stage1_run.sh: market-condition profile $MC_PROFILE uses the pre-set manifest $MC_DIGEST (plan/build skipped)"
      fi
      "$MC_PYTHON" "$MC_WARM" verify --manifest "$MC_DIGEST" || {
        echo "stage1_run.sh: market-condition VERIFY failed for $MC_DIGEST ($MC_PROFILE)" >&2
        exit 1; }
      "$MC_PYTHON" "$MC_WARM" prepare-host --manifest "$MC_DIGEST" \
        --profile "$MC_PROFILE" || {
        echo "stage1_run.sh: market-condition PREPARE-HOST failed for $MC_DIGEST ($MC_PROFILE)" >&2
        exit 1; }
      echo "stage1_run.sh: market-condition profile $MC_PROFILE manifest $MC_DIGEST prepared"
    fi
    MC_PINS="${MC_PINS:+$MC_PINS,}$MC_PROFILE=$MC_DIGEST"
  done
  MARKET_CONDITION_MANIFEST="$MC_PINS"
  MC_ARGS=(--market-condition-profile "$MARKET_CONDITION_PROFILE" \
           --market-condition-manifest "$MARKET_CONDITION_MANIFEST")
fi

# FITNESS (2026-09-17). Unset -> run_options_matrix --profile discovery's own default,
# ``option_consistent_annual_return``, which is what every -st1 job so far ran under; with it
# unset this block is a no-op and the launch is byte-for-byte the one it has always been.
#
# Set STAGE1_FITNESS=option_car_over_risk for the OTHER option objective: ~50%/yr WITH a
# drawdown tolerance (annualized return / sqrt(max(dd,10%)), full credit to 40% dd then a
# (40/dd)^1.5 penalty). That is what to run when the default's 16x small-drawdown reward is
# producing low-return grinders -- as it did on the first gated stage-1 job, which converged on
# a 10.6%-CAR / 8.9%-DD genome (fitness 13.5, about 2.4x the score it gave a 50%-CAR / 30%-DD
# one) and was stopped for exactly that reason.
#
# CHANGING THE FITNESS REQUIRES A NEW SUFFIX, and is refused without one. Job names are the
# RESUME KEY: re-ranking a search and then resuming into checkpoints scored under the other
# metric silently mixes two objectives in one population, and the two metrics' scores are not
# comparable at all (the new one ranks a 50%/30% genome ABOVE a 25%/10% one; the default ranks
# them the other way round). Same rule as every other economic/search change here.
STAGE1_FITNESS="${STAGE1_FITNESS:-}"
STAGE1_SUFFIX="${STAGE1_SUFFIX:--st1}"
FITNESS_ARGS=()
if [ -n "$STAGE1_FITNESS" ]; then
  if [ "$STAGE1_SUFFIX" = "-st1" ]; then
    echo "stage1_run.sh: STAGE1_FITNESS=$STAGE1_FITNESS re-ranks the search, so it needs its own" >&2
    echo "job names -- STAGE1_SUFFIX is still the default '-st1' and those jobs are already" >&2
    echo "banked under option_consistent_annual_return. Set STAGE1_SUFFIX (e.g. -st1cor) so the" >&2
    echo "run cannot resume into checkpoints scored on a different objective." >&2
    exit 1
  fi
  FITNESS_ARGS=(--fitness "$STAGE1_FITNESS")
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
  --name-suffix="$STAGE1_SUFFIX" \
  ${FITNESS_ARGS[@]+"${FITNESS_ARGS[@]}"} \
  ${MC_ARGS[@]+"${MC_ARGS[@]}"} \
  "$@"

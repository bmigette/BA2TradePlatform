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
# PARALLELISM (2026-09-14, operator): 20 local consumers on babatest (32 cores / 251 GB).
# Measured 2026-09-02 on the 2024+ store: ~22 MB of cached chains per symbol per consumer,
# roughly x2-3 at a 2020 start (350 expiries per symbol vs 184) -> ~5-7 GB per consumer for
# the ~100-symbol universe, ~100-140 GB for 20. The fleet worker on this host must be IDLE
# (no goal2020 job) or the two will fight for RAM; check `free -g` before launching.
# BT_MAX_TASKS_PER_CHILD is raised from the handler default of 8: every pool recycle re-pays
# the cold chain load (~11 s/symbol at 2020), which was ~14% of job time on the 2024+ store
# at 8 individuals per child. RAM is not the constraint on this host, so hold the cache longer.
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
PARALLEL="${PARALLEL:-20}"

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
  "$@"

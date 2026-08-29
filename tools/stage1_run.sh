#!/usr/bin/env bash
# Option grid STAGE 1 — isolated local run on babatest. Persistent home: /home/debian/ba2-grid
# (NOT /tmp — that is tmpfs-ish and died in the 2026-08-29 reboot).
# Isolation vs the running fleet (worker --port 8100, checkout /opt/ba2worker/BA2TradePlatform):
#   own BA2_HOME, own DB, own worktree code via --launcher, cgroup RAM+CPU caps.
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

# 18 structures x 2 experts = 36 jobs; spec: pop 200, gen 60, early-stop patience 8.
# Parquet (TastyTrade) store -> full spec window 2023-01-01..2025-12-31.
exec /opt/ba2worker/ba2-venvs/test/bin/python tools/run_options_matrix.py \
  --launcher /home/debian/ba2-grid/repo/testplatform/ba2test_launcher.py \
  --experts FMPRating,DeterministicScorer \
  --strategies O_LC,O_LP,O_VERT,O_BULLCS,O_BULLPS,O_BEARCS,O_BF,O_IC,O_JL,O_RS,O_SSTD,O_SSTG,O_CSP,O_STRD,O_STRG,O_CC,O_PP,O_WHEEL \
  --start 2023-01-01 --end 2025-12-31 \
  --population 200 --generations 60 --early-stop 8 \
  --parallel 2 \
  --name-suffix=-st1 \
  "$@"

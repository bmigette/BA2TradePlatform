#!/usr/bin/env bash
# Build two venvs — one for the trade app (ba2-trade), one for the test app (ba2-test) —
# each with the common -> providers -> experts chain + that app's requirements.txt.
#   ./install.sh [--editable] [--ui] [--upgrade] [--trade-only|--test-only] \
#                [--branch dev|main] [--base PATH] [--python PY] [--no-db]
#     --editable/-e : install the sibling clones editable (default: git install over SSH @branch)
#     --ui          : include the experts [ui] extra (nicegui) in the chain
#     --upgrade     : pass --upgrade so an existing install is re-resolved/updated
#     --trade-only  : only build the trade venv ; --test-only : only build the test venv
#     --branch      : git branch to install from in non-editable mode (default: dev)
#     --base        : base folder for the venvs (default: $HOME). Venvs live OUTSIDE the git repos.
#     --no-db       : skip the DB step (copy old DB -> new location + run migrations)
# Venvs: <base>/ba2-venvs/{trade,test}. Uses uv (bootstrapped into each venv).
#
# DB step (after the venvs are built): for each platform, if the app's DB is NOT yet at its
# current (consolidated) location, copy a pre-existing OLD DB there (the OLD file is left in
# place as a backup), then apply that platform's migrations to the target. Paths are derived
# from BA2_HOME (default ~/Documents/ba2) and overridable via env:
#   BA2_OLD_TRADE_DB (default ~/Documents/ba2_trade_platform/db.sqlite)
#   BA2_OLD_TEST_DB  (default ~/Documents/ba2_ml_test_platform/dl_forecasting.db)
# Migrations are idempotent (alembic upgrade head / db_migrate runner) — safe to re-run.
set -euo pipefail
EDITABLE=0; UI=0; UPGRADE=0; TRADE_ONLY=0; TEST_ONLY=0; BRANCH="dev"; BASE=""; BASE_PY="${PYTHON:-python3}"; NO_DB=0
while [ $# -gt 0 ]; do case "$1" in
  --editable|-e) EDITABLE=1 ;; --ui) UI=1 ;; --upgrade) UPGRADE=1 ;;
  --trade-only) TRADE_ONLY=1 ;; --test-only) TEST_ONLY=1 ;; --no-db) NO_DB=1 ;;
  --branch) shift; BRANCH="$1" ;; --base) shift; BASE="$1" ;; --python) shift; BASE_PY="$1" ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;; esac; shift; done
OWNER="bmigette"
HERE="$(cd "$(dirname "$0")" && pwd)"               # the BA2TradePlatform monorepo root
BASE="${BASE:-$HOME}"
VENV_ROOT="$BASE/ba2-venvs"

# Interpreter for both venvs: Python 3.12 by default (the backend's pandas-ta requires
# >=3.12; the trade app runs on it too). Override with --python / $PYTHON.
if [ -n "${PYTHON:-}" ]; then BOTH_PY="$BASE_PY"; elif command -v python3.12 >/dev/null 2>&1; then BOTH_PY="python3.12"; else BOTH_PY="$BASE_PY"; fi
TRADE_PY="$BOTH_PY"; TEST_PY="$BOTH_PY"

UP=(); [ "$UPGRADE" = "1" ] && UP=(--upgrade)

install_chain() {  # $1=uv $2=vpy ; in-repo packages ONLY (self-contained monorepo, no external git)
  local UV="$1" VPY="$2"; local ef=(); [ "$EDITABLE" = "1" ] && ef=(-e)
  local common="$HERE/packages/common" prov="$HERE/packages/providers" exp="$HERE/packages/experts"
  [ "$UI" = "1" ] && exp="${exp}[ui]"
  "$UV" pip install --python "$VPY" --no-sources ${UP[@]+"${UP[@]}"} ${ef[@]+"${ef[@]}"} "$common"
  "$UV" pip install --python "$VPY" --no-sources ${UP[@]+"${UP[@]}"} ${ef[@]+"${ef[@]}"} "$prov"
  "$UV" pip install --python "$VPY" --no-sources ${UP[@]+"${UP[@]}"} ${ef[@]+"${ef[@]}"} "$exp"
}

install_reqs() {  # $1=uv $2=vpy $3=reqpath ; strip ba2trade-* (installed via the chain) to avoid conflicts
  local UV="$1" VPY="$2" REQ="$3"; [ -f "$REQ" ] || return 0
  # Drop any chain reference (`ba2trade-*`, git/path to the repos, `-e ../..`) — the chain
  # is installed explicitly above; keep everything else.
  local tmp; tmp="$(mktemp)"
  grep -v -i -E 'ba2trade-|BA2TradeCommon|BA2TradeProviders|BA2TradeExperts' "$REQ" > "$tmp" || true
  "$UV" pip install --python "$VPY" ${UP[@]+"${UP[@]}"} -r "$tmp"; rm -f "$tmp"
}

new_app_venv() {  # $1=venv $2=appdir $3=reqpath $4=torch_cpu(0/1) $5=verify_import $6=base_py
  local VENV="$1" APPDIR="$2" REQ="$3" TORCH="$4" VERIFY="$5" BPY="$6"
  echo ">> creating venv at $VENV (base: $BPY)"
  "$BPY" -m venv "$VENV"
  local VPY="$VENV/bin/python"; [ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"
  echo ">> bootstrapping pip + uv"
  "$VPY" -m pip install --upgrade pip uv >/dev/null 2>&1 || true
  local UV="$VENV/bin/uv"; [ -x "$UV" ] || UV="$VENV/Scripts/uv.exe"
  if [ "$TORCH" = "1" ]; then
    echo ">> installing CPU-only torch"
    "$UV" pip install --python "$VPY" torch --index-url https://download.pytorch.org/whl/cpu
  fi
  install_chain "$UV" "$VPY"
  install_reqs  "$UV" "$VPY" "$REQ"
  "$UV" pip install --python "$VPY" --no-sources --no-deps -e "$APPDIR"   # registers the console command
  echo ">> verifying $VENV"
  "$VPY" -c "import $VERIFY; print('ok')"
}

DO_TRADE=1; [ "$TEST_ONLY" = "1" ] && DO_TRADE=0
DO_TEST=1;  [ "$TRADE_ONLY" = "1" ] && DO_TEST=0

if [ "$DO_TRADE" = "1" ]; then
  echo "==== TRADE venv ===="
  new_app_venv "$VENV_ROOT/trade" "$HERE" \
               "$HERE/requirements.txt" 1 "ba2_common" "$TRADE_PY"
fi
if [ "$DO_TEST" = "1" ]; then
  echo "==== TEST venv ===="
  new_app_venv "$VENV_ROOT/test" "$HERE/testplatform" \
               "$HERE/testplatform/backend/requirements.txt" 0 "ba2_common" "$TEST_PY"
  # Test-platform frontend (Vite/React UI) deps — so `ba2-test serve` can start the UI.
  FE="$HERE/testplatform/frontend"
  if [ -f "$FE/package.json" ]; then
    if command -v npm >/dev/null 2>&1; then
      echo ">> installing test frontend deps (npm install in testplatform/frontend)"
      ( cd "$FE" && npm install ) || echo ">> npm install failed — run it manually in $FE"
    else
      echo ">> npm not found — skipping frontend deps (install Node.js, then 'npm install' in $FE)"
    fi
  fi
fi

# ---- DB step: copy a pre-existing OLD db to the app's current location (keep source as a
#      backup) + apply that platform's migrations to the target. Idempotent / safe to re-run.
BA2_HOME_DIR="${BA2_HOME:-$HOME/Documents/ba2}"
OLD_TRADE_DB="${BA2_OLD_TRADE_DB:-$HOME/Documents/ba2_trade_platform/db.sqlite}"
OLD_TEST_DB="${BA2_OLD_TEST_DB:-$HOME/Documents/ba2_ml_test_platform/dl_forecasting.db}"
NEW_TRADE_DB="$BA2_HOME_DIR/trade/db.sqlite"
NEW_TEST_DB="$BA2_HOME_DIR/test/dl_forecasting.db"

copy_db_if_needed() {  # $1=label $2=old $3=new — copies old->new only when new is absent
  local label="$1" old="$2" new="$3"
  mkdir -p "$(dirname "$new")"
  if [ -f "$new" ]; then
    echo ">> $label: target DB already at $new — keeping it (no copy)"
  elif [ -f "$old" ] && [ "$old" != "$new" ]; then
    echo ">> $label: copying $old -> $new (source left in place as backup)"
    cp -p "$old" "$new"
  else
    echo ">> $label: no old DB at $old — target created on first app run"
  fi
}

if [ "$NO_DB" != "1" ] && [ "$DO_TRADE" = "1" ]; then
  echo "==== TRADE DB ===="
  copy_db_if_needed "TRADE" "$OLD_TRADE_DB" "$NEW_TRADE_DB"
  if [ -f "$NEW_TRADE_DB" ]; then
    AL="$VENV_ROOT/trade/bin/alembic"; [ -x "$AL" ] || AL="$VENV_ROOT/trade/Scripts/alembic.exe"
    echo ">> migrating TRADE db -> head ($NEW_TRADE_DB)"
    ( cd "$HERE" && BA2_DB_FILE="$NEW_TRADE_DB" "$AL" upgrade head ) \
      && echo ">> TRADE migrations applied" || echo ">> TRADE migration FAILED (see above) — continuing"
  fi
fi
if [ "$NO_DB" != "1" ] && [ "$DO_TEST" = "1" ]; then
  echo "==== TEST DB ===="
  copy_db_if_needed "TEST" "$OLD_TEST_DB" "$NEW_TEST_DB"
  if [ -f "$NEW_TEST_DB" ]; then
    PYV="$VENV_ROOT/test/bin/python"; [ -x "$PYV" ] || PYV="$VENV_ROOT/test/Scripts/python.exe"
    echo ">> migrating TEST db ($NEW_TEST_DB)"
    ( cd "$HERE/testplatform/backend" && DATABASE_PATH="$NEW_TEST_DB" "$PYV" scripts/migrate_db.py ) \
      && echo ">> TEST migrations applied" || echo ">> TEST migration FAILED (see above) — continuing"
  fi
fi

echo ">> done."
[ "$DO_TRADE" = "1" ] && echo "   trade venv: $VENV_ROOT/trade   (ba2-trade)"
[ "$DO_TEST" = "1" ]  && echo "   test  venv: $VENV_ROOT/test    (ba2-test)"

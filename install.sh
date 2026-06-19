#!/usr/bin/env bash
# Build two venvs — one for the trade app (ba2-trade), one for the test app (ba2-test) —
# each with the common -> providers -> experts chain + that app's requirements.txt.
#   ./install.sh [--editable] [--ui] [--upgrade] [--trade-only|--test-only] \
#                [--branch dev|main] [--base PATH] [--python PY]
#     --editable/-e : install the sibling clones editable (default: git install over SSH @branch)
#     --ui          : include the experts [ui] extra (nicegui) in the chain
#     --upgrade     : pass --upgrade so an existing install is re-resolved/updated
#     --trade-only  : only build the trade venv ; --test-only : only build the test venv
#     --branch      : git branch to install from in non-editable mode (default: dev)
#     --base        : base folder for the venvs (default: $HOME). Venvs live OUTSIDE the git repos.
# Venvs: <base>/ba2-venvs/{trade,test}. Uses uv (bootstrapped into each venv).
set -euo pipefail
EDITABLE=0; UI=0; UPGRADE=0; TRADE_ONLY=0; TEST_ONLY=0; BRANCH="dev"; BASE=""; BASE_PY="${PYTHON:-python3}"
while [ $# -gt 0 ]; do case "$1" in
  --editable|-e) EDITABLE=1 ;; --ui) UI=1 ;; --upgrade) UPGRADE=1 ;;
  --trade-only) TRADE_ONLY=1 ;; --test-only) TEST_ONLY=1 ;;
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

install_chain() {  # $1=uv $2=vpy ; --no-sources so our explicit chain install beats the git pins
  local UV="$1" VPY="$2"; local ef=(); [ "$EDITABLE" = "1" ] && ef=(-e)
  local common prov exp
  if [ "$EDITABLE" = "1" ]; then
    common="$HERE/packages/common"; prov="$HERE/packages/providers"
    exp="$HERE/packages/experts"; [ "$UI" = "1" ] && exp="${exp}[ui]"
  else
    common="git+ssh://git@github.com/$OWNER/BA2TradeCommon.git@$BRANCH"
    prov="git+ssh://git@github.com/$OWNER/BA2TradeProviders.git@$BRANCH"
    if [ "$UI" = "1" ]; then exp="ba2trade-experts[ui] @ git+ssh://git@github.com/$OWNER/BA2TradeExperts.git@$BRANCH"
    else exp="git+ssh://git@github.com/$OWNER/BA2TradeExperts.git@$BRANCH"; fi
  fi
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
  new_app_venv "$VENV_ROOT/test" "$HERE/packages/testplatform" \
               "$HERE/packages/testplatform/backend/requirements.txt" 0 "ba2_common" "$TEST_PY"
fi

echo ">> done."
[ "$DO_TRADE" = "1" ] && echo "   trade venv: $VENV_ROOT/trade   (ba2-trade)"
[ "$DO_TEST" = "1" ]  && echo "   test  venv: $VENV_ROOT/test    (ba2-test)"

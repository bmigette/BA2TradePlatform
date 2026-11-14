#!/usr/bin/env bash
# goal2027 -- the walk-forward stock grid, Senate included (one grid, one summary).
#
#   Design:     docs/plans/2026-10-25-goal2027-grid-design.md
#   Two phases: docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md
#
#   PHASE=A bash tools/grid_goal2027.sh start      launch in the background (log: $GRID_LOG)
#   PHASE=A bash tools/grid_goal2027.sh run        the same, in the foreground
#   PHASE=A bash tools/grid_goal2027.sh dry-run    print every cell's chain; runs nothing
#           bash tools/grid_goal2027.sh status     alive?, current step, grid summary (read-only)
#           bash tools/grid_goal2027.sh stop [--dry-run]
#           bash tools/grid_goal2027.sh setup      create the run venv for THIS checkout (once)
#
# "Resume" is "start" again: every job resumes by name, completed folds are skipped.
#
# PHASES (PHASE is REQUIRED, never defaulted: running B on December's data, or A in January, is
# the mistake this refuses):
#   A  Nov-Dec 2026, data through 2025: wf1 + wf2 whole, wf3 TRAINED only (cell PENDING).
#   B  January 2027, data through 2026-12-31: wf3's 2026 test, the verdict, the refit; and full
#      chains for the cells phase A did not reach. --wf-defer-last-test is passed in BOTH phases:
#      it is part of the cell names, and it makes the tests read the current data.
#
# HOSTS (owner 2026-11-02): the master runs on remote227 (Linux), away from Windows Update restarts;
# the script also runs on Windows (Git Bash). Either way from a DEDICATED checkout + venv (`setup`):
# never the owner PC's main clone (the live apps import it), never the ba2-worker service's tree (a
# worker re-sync git-pulls it); FORBIDDEN_CHECKOUTS adds more. Local GA slots: PARALLEL (default 0).
# Remote workers are a setting: WORKERS=remote227 (default) or a comma list; change it between
# steps (stop, set, start). Out-of-sample re-runs run on the MASTER, one at a time (~14 GB each,
# 20 GB free floor). Results are copied back to the owner PC afterwards.
#
# STEPS (design 4.2 run order; STEPS="..." runs a subset, in the order given):
#   fr-large ds-large fr-mid ds-mid ed-mid icb-mid ed-small icb-small fr-small ds-small
#   sqm-xl rank-large rank-mid rank-small senate pb-large pb-mid pb-small
# (fr = FMPRating, ds = DeterministicScorer, ed = FMPEarningsDrift, icb = FMPInsiderClusterBuy,
#  sqm = DeterministicScorer SQM on the xl screen, rank = FactorRanker, senate = FMPSenateTraderWeight,
#  pb = PullbackReversion, long only, strategy S2: RSI-dip entries in an uptrend, exit_mode genes.)
# One driver call per step: names carry only the gene tokens that step really searches, and the
# per-band spread is a per-call flag. Every call adds to ONE summary under $WF_REPORT_DIR.
#
# GENES per kind (design 1.3):
#   classic (S1-S7, Senate too): sizing gene, ATR searched, SPY-stress regime overlay searched (rgov:
#            on/off + size/stop/TP scales), market profile gene + exits, SL loosen,
#            SPY regime entry gates (spy-trend, spy-vol), entry/manage time genes; DS PINS its score
#            weights (dspin; DS_WEIGHTS=search = the searched factor genes, dsfw); explr where the pair
#            has ranges.
#   sqm:     sizing and time genes, the DS score pin (or DS_WEIGHTS=search) + explr; no market, SPY, ATR
#            or SL-loosen genes (SQM's stop is its own gene and min_stop_loss_pct is pinned; its
#            market/SPY flags are refused).
#   rank:    FactorRanker is a bypass expert: no RM, market, SPY, sizing or schedule genes.
set -u
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -W 2>/dev/null || pwd)"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;; *) IS_WIN=0 ;; esac

CMD="${1:-}"; shift || true

GRID_LOG="${GRID_LOG:-grid_goal2027.log}"
# INTERPRETER + CODE ISOLATION. Windows: a dedicated venv (`setup`). remote227 (Linux): the FLEET venv
# (/opt/ba2worker/ba2-venvs/test, as the stage-1/2 masters; no uv, system python 3.13) and isolation by
# PYTHONPATH only. That venv's EDITABLE finders point at the fleet checkout, so without the PYTHONPATH
# below the master would silently run fleet code; check_venv asserts every module resolves HERE.
if [ "$IS_WIN" = "1" ]; then
  VENV="${VENV:-$HOME/ba2-venvs/goal2027}"; PY="${PY:-$VENV/Scripts/python.exe}"; _PSEP=";"
else
  VENV="${VENV:-/opt/ba2worker/ba2-venvs/test}"; PY="${PY:-$VENV/bin/python}"; _PSEP=":"
fi
export PYTHONPATH="${REPO_ROOT}/packages/common${_PSEP}${REPO_ROOT}/packages/providers${_PSEP}${REPO_ROOT}/packages/experts${_PSEP}${REPO_ROOT}/testplatform/backend${_PSEP}${REPO_ROOT}/testplatform${PYTHONPATH:+${_PSEP}$PYTHONPATH}"
# DATA HOME. remote227: REQUIRED and its OWN (own test DB; never share a DB between two masters),
# e.g. /home/debian/ba2-grid/home-goal2027 with common -> /opt/ba2worker/Documents/ba2/common (the
# workers' cache). The stage-2 master's home is forbidden. Windows: the default ~/Documents/ba2.
FORBIDDEN_HOMES="${FORBIDDEN_HOMES:-/home/debian/ba2-grid/home}"
MEMORY_MAX="${MEMORY_MAX:-80G}"     # remote227 transient unit's cgroup cap (box: 251 GB, 24 worker slots)
UNIT="${UNIT:-ba2-goal2027}"
DRIVER=tools/run_screener_capband_matrix.py
# DRAWDOWN PASS RULE (owner 2026-11-12, changed mid-grid): a fold winner passes when its test-year
# |max DD| is within 2.5 x its training |max DD| AND within 35%. The grid started on 1.5 x alone, which
# failed a +39% test year on a -15.9% drawdown because training had been -9.5%. The cells keep the
# names they had under 1.5 (--wf-name-pass-max-dd-mult): completed jobs resume, stored folds are re-judged.
PASS_DD_MULT="${PASS_DD_MULT:-2.5}"
PASS_MAX_OOS_DD="${PASS_MAX_OOS_DD:-35}"
WF_REPORT_DIR="${WF_REPORT_DIR:-${REPO_ROOT}/reports/goal2027_wf}"
TRAIN_START="${TRAIN_START:-2020-01-01}"
FITNESS="${FITNESS:-consistent_annual_return}"
INTERVAL="${INTERVAL:-5min}"
STORE="${STORE:-}"     # default: <ba2_common CACHE_FOLDER>/screener/metric_store (resolve_store)
PLAN_FILE="${PLAN_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/strategy_plan.json}"
EXCLUDE_FILE="${EXCLUDE_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/excluded_symbols.txt}"
SENATE_UNIVERSE_FILE="${SENATE_UNIVERSE_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/senate_universe_2020_2025.txt}"
WORKERS="${WORKERS-remote227}"
PARALLEL="${PARALLEL-0}"
export BA2_MAX_REMOTE_SLOTS="${BA2_MAX_REMOTE_SLOTS:-28}"
# Budget 2026-11-10 (design goal2027-pullback-downmarket "Grid budget"): population 200 -> 300 and later folds
# 15 -> 20 generations, because the pullback leaves and the down-trend scales add genes.
POPULATION="${POPULATION:-300}"
WF_FIRST_GENERATIONS="${WF_FIRST_GENERATIONS:-30}"
WF_NEXT_GENERATIONS="${WF_NEXT_GENERATIONS:-20}"
WF_TOP_N="${WF_TOP_N:-3}"
EARLY_STOP="${EARLY_STOP:-5}"
EARLY_STOP_MIN_REL="${EARLY_STOP_MIN_REL:-0.01}"
SUFFIX_BASE="${SUFFIX_BASE:--goal2027}"
# DS score weights: pin (default; name token dspin) or search (the original dsfw cells). See ds_weight_args.
DS_WEIGHTS="${DS_WEIGHTS:-pin}"
# LATE STEPS (owner: S8/S9 run after EVERY other cell so they can still be vetoed; S9 before S8, S8 last of all). sN-<expert>-<band>
# runs ONLY strategy SN (the matrix's --late-strategy: the plan's late_strategies section) for FMPRating (fr) and
# DeterministicScorer (ds). Their split scope and rsi+spy exits are implied by the key, so their names carry no
# -regsplit/-mx token; G27_REGIME_SPLIT is refused for them (the key already says the scope).
ALL_STEPS="fr-large ds-large fr-mid ds-mid ed-mid icb-mid ed-small icb-small fr-small ds-small sqm-xl rank-large rank-mid rank-small senate pb-large pb-mid pb-small s9-fr-large s9-ds-large s9-fr-mid s9-ds-mid s9-fr-small s9-ds-small s8-fr-large s8-ds-large s8-fr-mid s8-ds-mid s8-fr-small s8-ds-small"
STEPS="${STEPS:-$ALL_STEPS}"

# Spread = measured Alpaca SIP median per band; stress = the p90 - median widening (driver's
# _STRESS_BAND_DEFAULTS). xl is large caps; Senate spans every band (the mid values, as atr27).
spread_for() { case "$1" in large|xl) echo 3 ;; mid|senate) echo 9 ;; small) echo 17 ;; esac; }
stress_for() { case "$1" in large|xl) echo 4 ;; mid|senate) echo 13 ;; small) echo 28 ;; esac; }

expert_of() {
  case "$1" in
    fr) echo FMPRating ;; ds|sqm) echo DeterministicScorer ;; ed) echo FMPEarningsDrift ;;
    icb) echo FMPInsiderClusterBuy ;; rank) echo FactorRanker ;; senate) echo FMPSenateTraderWeight ;;
    pb) echo PullbackReversion ;;
  esac
}
ALL_EXPERTS="FMPRating DeterministicScorer FMPEarningsDrift FMPInsiderClusterBuy FactorRanker FMPSenateTraderWeight PullbackReversion"
skip_all_but() { local out=""; for e in $ALL_EXPERTS; do [ "$e" = "$1" ] || out="${out:+$out,}$e"; done; echo "$out"; }

data_end() { case "${PHASE:-}" in A) echo 2025-12-31 ;; B) echo "" ;; esac; }

# ------------------------------------------------------------------------------------------- setup
# Checkouts the master must never run from: the owner PC's main clone (the live apps import it), the
# ba2-worker service's tree (a re-sync git-pulls it), plus FORBIDDEN_CHECKOUTS (space-separated).
check_checkout() {
  local here bad="${FORBIDDEN_CHECKOUTS:-}" d wd
  here="$(cd "$REPO_ROOT" && pwd -P)"
  [ "$IS_WIN" = "1" ] && bad="$bad $HOME/Documents/dev/BA2TradePlatform"
  if command -v systemctl >/dev/null 2>&1; then
    wd="$(systemctl show -p WorkingDirectory --value ba2-worker 2>/dev/null)"
    [ -n "$wd" ] && bad="$bad $wd"
  fi
  case "$here/" in /opt/ba2worker/*)
    echo "FATAL: $REPO_ROOT is under /opt/ba2worker (the fleet worker's tree); the master needs its own checkout," >&2
    echo "  e.g. /home/debian/ba2-grid/repo-goal2027 (fetch from GitHub: the fleet origin lags)." >&2
    return 1 ;;
  esac
  for d in $bad; do
    [ -d "$d" ] || continue
    if [ "$(cd "$d" && pwd -P)" = "$here" ]; then
      echo "FATAL: $REPO_ROOT is a forbidden checkout for the master ($d: live apps or ba2-worker run from it)." >&2
      echo "  Create a run checkout: git worktree add --detach ../BA2-goal2027-run <ref>" >&2
      return 1
    fi
  done
}

cmd_setup() {
  check_checkout || exit 1
  if [ "$IS_WIN" = "0" ]; then
    # remote227: no venv of our own (no uv, python 3.13): the fleet venv + PYTHONPATH isolation.
    check_venv && check_home && echo "setup OK: $PY with PYTHONPATH -> $REPO_ROOT; BA2_HOME=$BA2_HOME"
    return
  fi
  local inst=(-e packages/common -e packages/providers -e packages/experts -e testplatform
              -r testplatform/backend/requirements.txt)
  if [ -x "$PY" ]; then
    echo "venv exists: $VENV"
  elif command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.12 || exit 1
  else
    python3.12 -m venv "$VENV" || exit 1
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install -p "$PY" "${inst[@]}" || exit 1
  else
    "$PY" -m pip install "${inst[@]}" || exit 1
  fi
  check_venv && echo "setup OK: $VENV -> $REPO_ROOT"
}

# remote227: the master's OWN data home (own test DB). Exports DB_FILE / DATABASE_URL like stage1_run.sh.
check_home() {
  [ "$IS_WIN" = "1" ] && return 0
  if [ -z "${BA2_HOME:-}" ]; then
    echo "FATAL: BA2_HOME is required on remote227 (the master's own home + test DB), e.g." >&2
    echo "  BA2_HOME=/home/debian/ba2-grid/home-goal2027 with common -> /opt/ba2worker/Documents/ba2/common" >&2
    return 1
  fi
  local h f; h="$(cd "$BA2_HOME" 2>/dev/null && pwd -P)" || { echo "FATAL: BA2_HOME $BA2_HOME does not exist" >&2; return 1; }
  for f in $FORBIDDEN_HOMES; do
    [ -d "$f" ] && [ "$(cd "$f" && pwd -P)" = "$h" ] && {
      echo "FATAL: BA2_HOME $BA2_HOME is another master's home (never share a DB between two masters)" >&2; return 1; }
  done
  [ -d "$BA2_HOME/common" ] || { echo "FATAL: $BA2_HOME/common missing (symlink it to the workers' cache)" >&2; return 1; }
  mkdir -p "$BA2_HOME/test"
  export BA2_HOME DB_FILE="$BA2_HOME/test/dl_forecasting.db" DATABASE_URL="sqlite:///$BA2_HOME/test/dl_forecasting.db"
}

# The FMP key from the master's own DB, exported for the children (never printed, never passed on a
# command line: systemd-run args are visible in ps and the unit's properties).
export_fmp_key() {
  [ -n "${FMP_API_KEY:-}" ] && return 0
  [ -f "${DB_FILE:-}" ] || return 0
  FMP_API_KEY="$("$PY" -c 'import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("SELECT value_str FROM appsetting WHERE key=?", ("FMP_API_KEY",)).fetchone()
print(r[0] if r and r[0] else "")' "$DB_FILE" 2>/dev/null)"
  [ -n "$FMP_API_KEY" ] && export FMP_API_KEY
}

# The metric store under the venv's own ba2_common cache root (BA2_HOME / CACHE_FOLDER), unless STORE is set.
resolve_store() {
  [ -n "$STORE" ] && return 0
  STORE="$("$PY" -c 'import os; from ba2_common.config import CACHE_FOLDER; print(os.path.join(CACHE_FOLDER, "screener", "metric_store"))')" || return 1
  echo "metric store: $STORE"
}

# The master must import THIS checkout's code: a venv pointing at another clone would run code the
# names and versions do not describe.
check_venv() {
  [ -x "$PY" ] || { echo "FATAL: no interpreter at $PY (run: bash tools/grid_goal2027.sh setup)" >&2; return 1; }
  "$PY" - "$REPO_ROOT" <<'EOF'
import os, sys
root = os.path.normcase(os.path.abspath(sys.argv[1]))
import ba2test_launcher, ba2_common, ba2_providers, ba2_experts, app
bad = [f"{m.__name__} ({m.__file__})" for m in (ba2test_launcher, ba2_common, ba2_providers, ba2_experts, app)
       if not os.path.normcase(os.path.abspath(m.__file__)).startswith(root)]
if bad:
    sys.exit(f"FATAL: {bad} import from outside {root} -- the interpreter runs another checkout's code")
print(f"venv imports this checkout: {root}")
EOF
}

# ------------------------------------------------------------------------------------------- preflight
preflight() {
  local end="$1"
  case "${PHASE:-}" in A|B) ;; *) echo "FATAL: PHASE must be A (Nov-Dec, data to 2025) or B (January, data to 2026)" >&2; exit 2 ;; esac
  # Analyst estimates source (2026-11-07): REQUIRED, never defaulted. alphavantage = point-in-time estimates
  # (needs the AV estimates namespace prewarmed); fmp = no historical estimates (estimate readers refuse in BT).
  case "${ESTIMATES_SOURCE:-}" in fmp|alphavantage) ;; *) echo "FATAL: ESTIMATES_SOURCE must be fmp or alphavantage" >&2; exit 2 ;; esac
  for s in $STEPS; do case " $ALL_STEPS " in *" $s "*) ;; *) echo "FATAL: unknown step $s (known: $ALL_STEPS)" >&2; exit 2 ;; esac; done
  ds_weight_args >/dev/null || exit 2
  check_checkout || exit 1
  check_home || exit 1
  check_venv || exit 1
  resolve_store || exit 1
  [ -n "${MC_OHLCV:-}" ] && [ -n "${MC_TA:-}" ] || {
    echo "FATAL: MC_OHLCV and MC_TA (market-condition manifest digests covering the cap bands to the data end)" >&2
    echo "       are required; there is no default, since each phase has its own data." >&2; exit 1; }
  for f in "$PLAN_FILE" "$EXCLUDE_FILE"; do [ -f "$f" ] || { echo "FATAL: missing $f" >&2; exit 1; }; done
  case " $STEPS " in *" senate "*)
    [ -f "$SENATE_UNIVERSE_FILE" ] || { echo "FATAL: missing Senate universe $SENATE_UNIVERSE_FILE" >&2; exit 1; } ;;
  esac
  [ "${GRID_SKIP_PREFLIGHT:-0}" = "1" ] && { echo "=== PREFLIGHT data checks SKIPPED (GRID_SKIP_PREFLIGHT=1)"; return 0; }
  local need_end="${end:-2026-12-31}"
  echo "=== PREFLIGHT: metric store spans ${TRAIN_START:0:7} .. ${need_end:0:7}"
  "$PY" - "$STORE" "${TRAIN_START:0:7}" "${need_end:0:7}" <<'EOF' || exit 1
import sys, pathlib
store, first, last = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
yms = sorted(p.name.split("=", 1)[1] for p in store.glob("ym=*"))
if not yms:
    sys.exit(f"FATAL: no ym=* partitions under {store}")
print(f"metric store: {len(yms)} partitions, {yms[0]} .. {yms[-1]}")
if yms[0] > first or yms[-1] < last:
    sys.exit(f"FATAL: metric store {yms[0]}..{yms[-1]} does not span {first}..{last}")
EOF
  # At the execution interval only: its check reads the DAILY file as the listing calendar (a late
  # first bar with a late daily start = not listed yet; an early last bar with an early daily end =
  # delisted), so it covers the daily files too. A check AT 1d has no such calendar and fails on
  # every post-2020 listing (atr27's "small/1d false positive").
  echo "=== PREFLIGHT: $INTERVAL cache coverage at the window start AND end ($TRAIN_START .. $need_end)"
  local fail=0
  "$PY" tools/check_window_coverage.py --interval "$INTERVAL" --start "$TRAIN_START" --end "$need_end" \
      --sample 150 --min-covered-pct 75 || fail=1
  case " $STEPS " in *" senate "*)
    "$PY" tools/check_window_coverage.py --interval "$INTERVAL" --start "$TRAIN_START" --end "$need_end" \
        --symbols "@$SENATE_UNIVERSE_FILE" --sample 150 --min-covered-pct 75 || fail=1 ;;
  esac
  if [ "$fail" = "1" ]; then
    echo "=== PREFLIGHT FAILED: cache coverage. GRID_SKIP_PREFLIGHT=1 runs anyway (state why in the log)."
    exit 1
  fi
}

# DeterministicScorer score weights (owner 2026-11-14; design 2.8 "Pinned score weights"): every DS step (ds-*,
# s9-ds-*, s8-ds-*, sqm-xl) PINS them by default -- momentum, 200-day trend and Piotroski in equal thirds plus a small
# grades-dominant analyst section -- because the GA-searched weights (dsfw) did not hold out of sample. Prints
# "<driver flag> <name token>". DS_WEIGHTS=search restores the searched cells (dsfw) and their names exactly.
ds_weight_args() {
  case "$DS_WEIGHTS" in
    pin)    echo "--pin-ds-score-weights -dspin" ;;
    search) echo "--search-ds-factor-weights -dsfw" ;;
    *) echo "FATAL: DS_WEIGHTS must be pin (default) or search, got '$DS_WEIGHTS'" >&2; return 2 ;;
  esac
}

# ------------------------------------------------------------------------------------------- one step
run_step() {                      # $1 = step id; $2 = "dry" for --dry-run
  local step="$1" dry="${2:-}" kind="${1%%-*}" band="${1#*-}" late=""
  [ "$step" = "senate" ] && { kind=senate; band=senate; }
  case "$step" in s8-*|s9-*)
    late="S${step:1:1}"; band="${step#*-}"; kind="${band%%-*}"; band="${band#*-}"
    [ -z "${G27_REGIME_SPLIT:-}" ] || { echo "FATAL: $step: G27_REGIME_SPLIT is set, but $late implies its own split scope" >&2; return 2; } ;;
  esac
  local expert; expert="$(expert_of "$kind")"
  local end; end="$(data_end)"
  local dsw
  local sp st; sp="$(spread_for "$band")"; st="$(stress_for "$band")"
  local args=(--bands "$band" --skip-experts "$(skip_all_but "$expert")"
      --start "$TRAIN_START" --fitness "$FITNESS" --interval "$INTERVAL" --store "$STORE"
      --spread-bps "$sp" --stress-spread-bps "$st"
      --population "$POPULATION" --no-budget-overrides
      --early-stop "$EARLY_STOP" --early-stop-min-rel "$EARLY_STOP_MIN_REL"
      --robust-fitness --parallel "$PARALLEL"
      --walk-forward --wf-defer-last-test --wf-train-start "$TRAIN_START" --wf-test-years 2024,2025,2026
      --wf-first-generations "$WF_FIRST_GENERATIONS" --wf-next-generations "$WF_NEXT_GENERATIONS"
      --wf-top-n "$WF_TOP_N" --wf-report-dir "$WF_REPORT_DIR" --wf-test-parallel 1
      --pass-min-oos-return 0 --pass-min-oos-return-exclusive
      --pass-max-dd-mult "$PASS_DD_MULT" --pass-max-oos-dd "$PASS_MAX_OOS_DD"
      --wf-name-pass-max-dd-mult 1.5
      --pass-min-folds 2 --pass-require-fold 2026)
  [ -n "$end" ] && args+=(--wf-data-end "$end")
  [ -n "$WORKERS" ] && args+=(--workers "$WORKERS")
  args+=(--estimates-source "$ESTIMATES_SOURCE")
  local suffix="$SUFFIX_BASE"
  local mc_o="$MC_OHLCV" mc_t="$MC_TA"
  if [ "$kind" = "senate" ]; then
    mc_o="${MC_SEN_OHLCV:-$MC_OHLCV}"; mc_t="${MC_SEN_TA:-$MC_TA}"
    args+=(--senate-universe-file "$SENATE_UNIVERSE_FILE")
  fi
  case "$kind" in
    fr|ds|ed|icb|senate|pb)
      args+=(--strategy-plan "$PLAN_FILE" --decision-times split --search-sizing-mode
             --rm-toggle-policy atr-regime-searched
             --market-condition-profile ohlcv-v1,ta-structure-v1
             --market-condition-manifest "ohlcv-v1=${mc_o},ta-structure-v1=${mc_t}"
             --search-market-profile --market-exit "${G27_MARKET_EXIT:-exit,stop,tp}" --search-sl-loosen
             --benchmark-regime-gates --pullback-gates --exclude-symbols "@$EXCLUDE_FILE")
      suffix="${suffix}-atr27-rgov-sizegene-mktprof-spyreg-pullback"
      # --market-exit kinds (exit,stop,tp,rsi,spy; rsi/spy add the RSI-recovery close and the SPY-below-200 stop).
      # G27_MARKET_EXIT set = that list for every strategy, its own name token. Unset = exit,stop,tp widened PER
      # STRATEGY by the matrix (--market-exit-per-strategy: S1/S2 +rsi, S6 +rsi,spy; S3/S5/... unchanged); a job whose
      # kinds differ from exit,stop,tp carries '-mx-<kinds>' in its name, so it never resumes an older job.
      if [ -n "${G27_MARKET_EXIT:-}" ]; then suffix="${suffix}-mx-${G27_MARKET_EXIT//,/_}"; else args+=(--market-exit-per-strategy); fi
      # S8/S9 (late steps): only that strategy, from the plan's late_strategies section; its split + rsi/spy are implied.
      if [ -n "$late" ]; then args+=(--late-strategy "$late"); fi
      # Optional: ONE ruleset with a SPY-above-200 section and a SPY-below-200 section (launcher --regime-split-entry).
      # G27_REGIME_SPLIT = context | entry | all. Unset = today's commands, names and labels unchanged.
      if [ -n "${G27_REGIME_SPLIT:-}" ]; then
        args+=(--regime-split-entry "$G27_REGIME_SPLIT"); suffix="${suffix}-regsplit-${G27_REGIME_SPLIT}"
      fi
      # PullbackReversion (long only) runs S2 on the cap bands; its entry is a daily dip, so the screener
      # cadence is daily and the entry scan runs every weekday (no weekday genes: _daily_entry in the launcher). The matrix runs it only when asked.
      if [ "$kind" = "pb" ]; then args+=(--pullback-strategies S2 --cadence-days 1); fi
      if [ "$kind" = "ds" ]; then
        dsw="$(ds_weight_args)" || return 2; args+=("${dsw% *}"); suffix="${suffix}${dsw#* }"
      fi
      if [ "$kind" != "senate" ] && [ "$kind" != "pb" ]; then args+=(--search-exploration-ranges); suffix="${suffix}-explr"; fi ;;
    sqm)
      dsw="$(ds_weight_args)" || return 2
      args+=(--strategies SQM --decision-times split --search-sizing-mode "${dsw% *}"
             --search-exploration-ranges --exclude-symbols "@$EXCLUDE_FILE")
      suffix="${suffix}-sizegene${dsw#* }-explr" ;;
    rank)
      : ;;   # bypass expert: no RM/market/sizing/schedule genes, full universe (atr27 rule)
  esac
  args+=(--name-suffix="$suffix")
  [ "$dry" = "dry" ] && args+=(--dry-run)
  echo; echo "=== step $step  ($expert / $band, spread $sp bps, stress +$st)  phase $PHASE  $(date)"
  "$PY" "$DRIVER" "${args[@]}"
  local rc=$?
  echo "=== step $step done rc=$rc $(date)"
  return $rc
}

cmd_run() {                        # $1 = "dry" for a dry run
  local end; end="$(data_end)"
  preflight "$end"
  export_fmp_key
  echo "=================================================================="
  echo "=== goal2027  phase $PHASE  train from $TRAIN_START  data end ${end:-2026-12-31 (full)}  $(date)"
  echo "=== checkout $REPO_ROOT ($(git rev-parse --short HEAD 2>/dev/null))  python $PY  BA2_HOME ${BA2_HOME:-(default)}"
  echo "=== workers: ${WORKERS:-local-only}  local parallel: $PARALLEL  remote slots: $BA2_MAX_REMOTE_SLOTS"
  echo "=== steps: $STEPS"
  echo "=== reports: $WF_REPORT_DIR"
  echo "=================================================================="
  local s failed=""
  for s in $STEPS; do run_step "$s" "${1:-}" || failed="$failed $s"; done
  echo; echo "=== goal2027 phase $PHASE COMPLETE $(date)${failed:+  (steps with errors:$failed)}"
  echo "Summary: $WF_REPORT_DIR/summary.md"
  [ -z "$failed" ]
}

# ------------------------------------------------------------------------------------------- control
# Process tiers, stopped in THIS order (grid_stop.sh's lesson: a killed driver lets its wrapper start
# the next step). Patterns are scoped to this grid: the wrapper script, drivers carrying the goal2027
# suffix, and processes of the goal2027 venv. The live apps (ba2-trade venv) never match.
ps_tiers() {                       # $1 = 1 to kill, 0 to list
  if [ "$IS_WIN" = "1" ]; then ps_tiers_win "$1"; else ps_tiers_linux "$1"; fi
}

# Linux (remote227 master): the same tiers from `ps`. Fork children of a GA pool carry their parent's
# command line, so the venv tier covers them; a killed parent's reparented children still match it.
ps_tiers_linux() {
  local kill="$1" venv_abs snap n pat pids
  venv_abs="$(cd "$VENV" 2>/dev/null && pwd -P || echo "$VENV")"
  snap="$(ps -eo pid=,args= | awk -v me="$$" '$1 != me')"
  for n in 1 2 3; do
    case $n in
      1) pat='bash .*tools/grid_goal2027\.sh run( |$)'; label="1. wrapper" ;;
      2) pat='run_screener_capband_matrix.*-goal2027'; label="2. matrix driver" ;;
      3) pat="$venv_abs/"; label="3. optimize / re-runs (goal2027 venv)" ;;
    esac
    if [ $n = 3 ]; then pids="$(printf '%s\n' "$snap" | grep -F -- "$pat" | awk '{print $1}')"
    else pids="$(printf '%s\n' "$snap" | grep -E -- "$pat" | awk '{print $1}')"; fi
    if [ -z "$pids" ]; then echo "$label: none"; continue; fi
    echo "$label: $(echo "$pids" | wc -l) process(es)"
    for p in $pids; do
      echo "    [$p] $(printf '%s\n' "$snap" | awk -v p="$p" '$1 == p {sub(/^ *[0-9]+ /, ""); print substr($0, 1, 90)}')"
    done
    if [ "$kill" = "1" ]; then kill $pids 2>/dev/null; sleep 3; kill -9 $pids 2>/dev/null; fi
  done
}

ps_tiers_win() {
  local venv_win; venv_win="$(cd "$VENV" 2>/dev/null && pwd -W 2>/dev/null || echo "$VENV")"
  powershell -NoProfile -Command "
\$kill = $1
\$venv = '${venv_win//\//\\}'
\$groups = @(
  @{ n='1. wrapper'; p={ \$_.Name -eq 'bash.exe' -and \$_.CommandLine -like '*grid_goal2027.sh*' -and \$_.CommandLine -notlike '* -c *' -and \$_.CommandLine -notlike '*grid_goal2027.sh* stop*' -and \$_.CommandLine -notlike '*grid_goal2027.sh* status*' } },
  @{ n='2. matrix driver'; p={ \$_.CommandLine -like '*run_screener_capband_matrix*' -and \$_.CommandLine -like '*-goal2027*' } },
  @{ n='3. optimize / re-runs (goal2027 venv)'; p={ \$_.CommandLine -like ('*' + \$venv + '*') } },
  @{ n='4. orphaned spawn-pool children'; p={ \$_.CommandLine -like '*--multiprocessing-fork*' -and \$null -eq (Get-Process -Id \$_.ParentProcessId -ErrorAction SilentlyContinue) } }
)
foreach (\$g in \$groups) {
  \$procs = @(Get-CimInstance Win32_Process | Where-Object \$g.p | Where-Object { \$_.Name -notin @('powershell.exe','pwsh.exe') -and \$_.ProcessId -ne \$PID })
  if (\$procs.Count -eq 0) { '{0}: none' -f \$g.n; continue }
  '{0}: {1} process(es)' -f \$g.n, \$procs.Count
  foreach (\$p in \$procs) {
    \$c = \$p.CommandLine; if (\$c.Length -gt 90) { \$c = \$c.Substring(0,90) }
    '    [{0}] {1}' -f \$p.ProcessId, \$c
    if (\$kill) { Stop-Process -Id \$p.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  if (\$kill) { Start-Sleep -Seconds 3 }
}
if (\$kill) { 'live platform (ba2-trade) processes: {0}  <- must still be > 0' -f @(Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*ba2-trade*' }).Count }
" 2>/dev/null
}

cmd_stop() {
  if [ "${1:-}" = "--dry-run" ]; then ps_tiers 0; echo "DRY RUN -- nothing was stopped."; return; fi
  if [ "$IS_WIN" = "0" ] && systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    echo "stopping unit $UNIT (the whole cgroup: wrapper, driver, optimize, re-runs)"
    sudo systemctl stop "$UNIT"
  fi
  ps_tiers 1
  echo
  echo "Stopped mid-job: that job resumes from its last completed generation on the next start (same"
  echo "name, same gene space). A wf3 interrupted in phase A must COMPLETE before January's data"
  echo "replaces phase A's, or be deleted and retrained (two-phase plan, boundary rule)."
}

cmd_status() {
  echo "=================== goal2027 status  $(date) ==================="
  if [ "$IS_WIN" = "0" ] && command -v systemctl >/dev/null 2>&1; then
    echo "unit $UNIT: $(systemctl is-active "$UNIT" 2>/dev/null)  (journalctl -u $UNIT)"
  fi
  ps_tiers 0
  echo
  if [ -f "$GRID_LOG" ]; then
    echo "--- current step: $(grep -E '^=== step .*\(' "$GRID_LOG" | tail -1)"
    echo "--- last progress:"
    grep -E 'fold [0-9]|Generation|gen [0-9]+|PENDING|PASS|FAIL|ERROR|eliminated' "$GRID_LOG" | tail -6
    [ "${1:-}" = "-v" ] && { echo "--- log tail:"; tail -15 "$GRID_LOG"; }
  else
    echo "no log at $GRID_LOG"
  fi
  echo
  if [ -f "$WF_REPORT_DIR/summary.md" ]; then cat "$WF_REPORT_DIR/summary.md"; else echo "no grid summary yet ($WF_REPORT_DIR)"; fi
}

case "$CMD" in
  setup)   cmd_setup ;;
  run)     export PYTHONUNBUFFERED=1; cmd_run 2>&1 | tee -a "$GRID_LOG"; exit "${PIPESTATUS[0]}" ;;
  dry-run) cmd_run dry ;;
  start)
    preflight "$(data_end)" || exit 1      # refuse here, in the operator's terminal, not in the log
    if [ "$IS_WIN" = "1" ]; then
      GRID_SKIP_PREFLIGHT=1 nohup bash tools/grid_goal2027.sh run >/dev/null 2>&1 &
    elif command -v systemd-run >/dev/null 2>&1; then
      # remote227: a transient unit (as the stage-1/2 masters) -- survives logout, cgroup RAM cap,
      # `journalctl -u`. The settings travel as --setenv; the FMP key does NOT (args and unit
      # properties are readable): `run` reads it from the master's own DB.
      sudo systemctl reset-failed "$UNIT" >/dev/null 2>&1
      local_env=()
      for v in PHASE ESTIMATES_SOURCE MC_OHLCV MC_TA MC_SEN_OHLCV MC_SEN_TA WORKERS PARALLEL BA2_MAX_REMOTE_SLOTS STEPS BA2_HOME                VENV PY STORE PLAN_FILE EXCLUDE_FILE SENATE_UNIVERSE_FILE WF_REPORT_DIR GRID_LOG POPULATION                WF_FIRST_GENERATIONS WF_NEXT_GENERATIONS WF_TOP_N EARLY_STOP EARLY_STOP_MIN_REL SUFFIX_BASE                TRAIN_START FITNESS INTERVAL FORBIDDEN_CHECKOUTS FORBIDDEN_HOMES DS_WEIGHTS; do
        [ -n "${!v:-}" ] && local_env+=("--setenv=$v=${!v}")
      done
      sudo systemd-run --unit="$UNIT" -p MemoryMax="$MEMORY_MAX" -p User="$(id -un)"           --working-directory="$REPO_ROOT" --setenv=GRID_SKIP_PREFLIGHT=1 --setenv=HOME="$HOME"           "${local_env[@]}" /bin/bash tools/grid_goal2027.sh run || exit 1
      echo "started unit $UNIT (MemoryMax $MEMORY_MAX); journalctl -u $UNIT; log: $REPO_ROOT/$GRID_LOG"
      exit 0
    else
      GRID_SKIP_PREFLIGHT=1 setsid nohup bash tools/grid_goal2027.sh run >/dev/null 2>&1 < /dev/null &
    fi
    echo "started (pid $!); log: $GRID_LOG   status: bash tools/grid_goal2027.sh status" ;;
  stop)    cmd_stop "$@" ;;
  status)  cmd_status "$@" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac

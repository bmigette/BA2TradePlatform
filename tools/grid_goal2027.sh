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
# HOSTS: the master runs on THIS PC from a DEDICATED checkout + venv (`setup`), never the main
# clone (the live apps import it). No local GA slots by default (PARALLEL=0: 8080/8081/8082 run
# here). Remote workers are a setting: WORKERS=remote227 (default), or remote150, or both
# ("remote227,remote150"); change it between steps (stop, set, start). Out-of-sample re-runs run
# HERE, one at a time (~14 GB each, 20 GB free floor).
#
# STEPS (design 4.2 run order; STEPS="..." runs a subset, in the order given):
#   fr-large ds-large fr-mid ds-mid ed-mid icb-mid ed-small icb-small fr-small ds-small
#   sqm-xl rank-large rank-mid rank-small senate
# (fr = FMPRating, ds = DeterministicScorer, ed = FMPEarningsDrift, icb = FMPInsiderClusterBuy,
#  sqm = DeterministicScorer SQM on the xl screen, rank = FactorRanker, senate = FMPSenateTraderWeight.)
# One driver call per step: names carry only the gene tokens that step really searches, and the
# per-band spread is a per-call flag. Every call adds to ONE summary under $WF_REPORT_DIR.
#
# GENES per kind (design 1.3):
#   classic (S1-S7, Senate too): sizing gene, ATR searched, market profile gene + exits, SL loosen,
#            SPY regime entry gates (spy-trend, spy-vol), entry/manage time genes; DS adds the factor
#            genes; explr where the pair has ranges.
#   sqm:     sizing, time and factor genes + explr; no market, SPY, ATR or SL-loosen genes (SQM's
#            stop is its own gene and min_stop_loss_pct is pinned; its market/SPY flags are refused).
#   rank:    FactorRanker is a bypass expert: no RM, market, SPY, sizing or schedule genes.
set -u
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -W 2>/dev/null || pwd)"

CMD="${1:-}"; shift || true

GRID_LOG="${GRID_LOG:-grid_goal2027.log}"
VENV="${VENV:-$HOME/ba2-venvs/goal2027}"
PY="${PY:-$VENV/Scripts/python.exe}"
DRIVER=tools/run_screener_capband_matrix.py
WF_REPORT_DIR="${WF_REPORT_DIR:-${REPO_ROOT}/reports/goal2027_wf}"
TRAIN_START="${TRAIN_START:-2020-01-01}"
FITNESS="${FITNESS:-consistent_annual_return}"
INTERVAL="${INTERVAL:-5min}"
STORE="${STORE:-$HOME/Documents/ba2/common/cache/screener/metric_store}"
PLAN_FILE="${PLAN_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/strategy_plan.json}"
EXCLUDE_FILE="${EXCLUDE_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/excluded_symbols.txt}"
SENATE_UNIVERSE_FILE="${SENATE_UNIVERSE_FILE:-${REPO_ROOT}/docs/strategy_research/atr_grid/senate_universe_2020_2025.txt}"
WORKERS="${WORKERS-remote227}"
PARALLEL="${PARALLEL-0}"
export BA2_MAX_REMOTE_SLOTS="${BA2_MAX_REMOTE_SLOTS:-28}"
POPULATION="${POPULATION:-200}"
WF_FIRST_GENERATIONS="${WF_FIRST_GENERATIONS:-30}"
WF_NEXT_GENERATIONS="${WF_NEXT_GENERATIONS:-15}"
WF_TOP_N="${WF_TOP_N:-3}"
EARLY_STOP="${EARLY_STOP:-5}"
EARLY_STOP_MIN_REL="${EARLY_STOP_MIN_REL:-0.01}"
SUFFIX_BASE="${SUFFIX_BASE:--goal2027}"
ALL_STEPS="fr-large ds-large fr-mid ds-mid ed-mid icb-mid ed-small icb-small fr-small ds-small sqm-xl rank-large rank-mid rank-small senate"
STEPS="${STEPS:-$ALL_STEPS}"

# Spread = measured Alpaca SIP median per band; stress = the p90 - median widening (driver's
# _STRESS_BAND_DEFAULTS). xl is large caps; Senate spans every band (the mid values, as atr27).
spread_for() { case "$1" in large|xl) echo 3 ;; mid|senate) echo 9 ;; small) echo 17 ;; esac; }
stress_for() { case "$1" in large|xl) echo 4 ;; mid|senate) echo 13 ;; small) echo 28 ;; esac; }

expert_of() {
  case "$1" in
    fr) echo FMPRating ;; ds|sqm) echo DeterministicScorer ;; ed) echo FMPEarningsDrift ;;
    icb) echo FMPInsiderClusterBuy ;; rank) echo FactorRanker ;; senate) echo FMPSenateTraderWeight ;;
  esac
}
ALL_EXPERTS="FMPRating DeterministicScorer FMPEarningsDrift FMPInsiderClusterBuy FactorRanker FMPSenateTraderWeight"
skip_all_but() { local out=""; for e in $ALL_EXPERTS; do [ "$e" = "$1" ] || out="${out:+$out,}$e"; done; echo "$out"; }

data_end() { case "${PHASE:-}" in A) echo 2025-12-31 ;; B) echo "" ;; esac; }

# ------------------------------------------------------------------------------------------- setup
cmd_setup() {
  if [ "$REPO_ROOT" = "$(cd "$HOME/Documents/dev/BA2TradePlatform" 2>/dev/null && (pwd -W 2>/dev/null || pwd))" ]; then
    echo "FATAL: this is the MAIN clone (the live apps import it). Create a run checkout first:" >&2
    echo "  git -C ~/Documents/dev/BA2TradePlatform worktree add --detach ../BA2-goal2027-run <ref>" >&2
    exit 1
  fi
  [ -x "$PY" ] && { echo "venv exists: $VENV"; } || uv venv "$VENV" --python 3.12 || exit 1
  uv pip install -p "$PY" -e packages/common -e packages/providers -e packages/experts -e testplatform \
      -r testplatform/backend/requirements.txt || exit 1
  check_venv && echo "setup OK: $VENV -> $REPO_ROOT"
}

# The master must import THIS checkout's code: a venv pointing at another clone would run code the
# names and versions do not describe.
check_venv() {
  [ -x "$PY" ] || { echo "FATAL: no interpreter at $PY (run: bash tools/grid_goal2027.sh setup)" >&2; return 1; }
  "$PY" - "$REPO_ROOT" <<'EOF'
import os, sys
root = os.path.normcase(os.path.abspath(sys.argv[1]))
import ba2test_launcher, ba2_common
bad = [m.__name__ for m in (ba2test_launcher, ba2_common)
       if not os.path.normcase(os.path.abspath(m.__file__)).startswith(root)]
if bad:
    sys.exit(f"FATAL: {bad} import from outside {root} -- the venv points at another checkout")
print(f"venv imports this checkout: {root}")
EOF
}

# ------------------------------------------------------------------------------------------- preflight
preflight() {
  local end="$1"
  case "${PHASE:-}" in A|B) ;; *) echo "FATAL: PHASE must be A (Nov-Dec, data to 2025) or B (January, data to 2026)" >&2; exit 2 ;; esac
  for s in $STEPS; do case " $ALL_STEPS " in *" $s "*) ;; *) echo "FATAL: unknown step $s (known: $ALL_STEPS)" >&2; exit 2 ;; esac; done
  check_venv || exit 1
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

# ------------------------------------------------------------------------------------------- one step
run_step() {                       # $1 = step id; $2 = "dry" for --dry-run
  local step="$1" dry="${2:-}" kind="${1%%-*}" band="${1#*-}"
  [ "$step" = "senate" ] && { kind=senate; band=senate; }
  local expert; expert="$(expert_of "$kind")"
  local end; end="$(data_end)"
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
      --pass-min-oos-return 0 --pass-min-oos-return-exclusive --pass-max-dd-mult 1.5
      --pass-min-folds 2 --pass-require-fold 2026)
  [ -n "$end" ] && args+=(--wf-data-end "$end")
  [ -n "$WORKERS" ] && args+=(--workers "$WORKERS")
  local suffix="$SUFFIX_BASE"
  local mc_o="$MC_OHLCV" mc_t="$MC_TA"
  if [ "$kind" = "senate" ]; then
    mc_o="${MC_SEN_OHLCV:-$MC_OHLCV}"; mc_t="${MC_SEN_TA:-$MC_TA}"
    args+=(--senate-universe-file "$SENATE_UNIVERSE_FILE")
  fi
  case "$kind" in
    fr|ds|ed|icb|senate)
      args+=(--strategy-plan "$PLAN_FILE" --decision-times split --search-sizing-mode
             --rm-toggle-policy atr-searched
             --market-condition-profile ohlcv-v1,ta-structure-v1
             --market-condition-manifest "ohlcv-v1=${mc_o},ta-structure-v1=${mc_t}"
             --search-market-profile --market-exit exit,stop,tp --search-sl-loosen
             --benchmark-regime-gates --exclude-symbols "@$EXCLUDE_FILE")
      suffix="${suffix}-atr27-sizegene-mktprof-spyreg"
      if [ "$kind" = "ds" ]; then args+=(--search-ds-factor-weights); suffix="${suffix}-dsfw"; fi
      if [ "$kind" != "senate" ]; then args+=(--search-exploration-ranges); suffix="${suffix}-explr"; fi ;;
    sqm)
      args+=(--strategies SQM --decision-times split --search-sizing-mode --search-ds-factor-weights
             --search-exploration-ranges --exclude-symbols "@$EXCLUDE_FILE")
      suffix="${suffix}-sizegene-dsfw-explr" ;;
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
  echo "=================================================================="
  echo "=== goal2027  phase $PHASE  train from $TRAIN_START  data end ${end:-2026-12-31 (full)}  $(date)"
  echo "=== checkout $REPO_ROOT ($(git rev-parse --short HEAD 2>/dev/null))  venv $VENV"
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
  ps_tiers 1
  echo
  echo "Stopped mid-job: that job resumes from its last completed generation on the next start (same"
  echo "name, same gene space). A wf3 interrupted in phase A must COMPLETE before January's data"
  echo "replaces phase A's, or be deleted and retrained (two-phase plan, boundary rule)."
}

cmd_status() {
  echo "=================== goal2027 status  $(date) ==================="
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
    GRID_SKIP_PREFLIGHT=1 nohup bash tools/grid_goal2027.sh run >/dev/null 2>&1 &
    echo "started (pid $!); log: $GRID_LOG   status: bash tools/grid_goal2027.sh status" ;;
  stop)    cmd_stop "$@" ;;
  status)  cmd_status "$@" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac

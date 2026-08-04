# Runbook — the goal2020 optimization grid

Everything needed to start, watch, stop and resume the grid without help. Run every command from
the repo root (`C:\Users\basti\Documents\dev\BA2TradePlatform`) in **Git Bash**.

Last verified 2026-08-04 against app_version 2026.08.1014.

---

## TL;DR

```bash
bash tools/grid_status.sh          # where is it?           (read-only, safe any time)
bash tools/grid_status.sh -v       #   + last 15 log lines

git push origin dev                # ALWAYS before launching — see "Before you launch"
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &

bash tools/grid_stop.sh --dry-run  # what would be killed
bash tools/grid_stop.sh            # stop it
```

Relaunching after a stop is the *same launch command* — completed jobs are skipped and an
interrupted job resumes at its last completed generation.

---

## 1. What this grid is for

Re-optimize every classic expert over **2020-01-01 → 2025-12-31** on the fixed engine, and make
the result the new source of truth. It exists because every optimization before v2026.07.1002
scored with `DaysOpened` / `DaysSinceLastClose*` **inert** — 129 of 153 completed runs were built
on a strategy declaring at least one of them.

**48 jobs, run strictly one at a time**, as two sizing matrices × three cap bands:

| matrix | sizing_mode | bands | experts |
|---|---|---|---|
| 1 | `risk_atr` | large, mid, small | FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy, FactorRanker |
| 2 | `notional` | large, mid, small | same, **minus FactorRanker** |

Why the split, and why not a `sizing_mode` gene: under `notional` the five ATR genes face no
selection pressure and drift randomly, so a crossover flipping the mode would judge it with
unselected parameters. `max_virtual_equity_per_instrument_percent` is also the *primary* sizer
under notional but a rarely-binding ceiling under risk_atr — one population would fight itself.
FactorRanker is `bypasses_classic_rm`, never reads `sizing_mode`, so running it twice would burn
compute for byte-identical results.

**Window.** Ends 31 Dec on purpose: `consistent_annual_return` buckets by calendar year and merges
a stub shorter than 182.62 days into its neighbour, so a 30-June end silently produces an
18-month final bucket. This gives six clean buckets and leaves **2026-H1 as an untouched
out-of-sample holdout** — note that nothing currently *scores* on it; that is still owed.

**Expected duration** ≈ 2 days at 4 local + 6 remote slots. Roughly 2.5× that local-only.

---

## 2. Before you launch — three things that have each cost hours

### a. Push first. Always.
`remote150` syncs by `git pull`. If the master's commit is not on `origin/dev`, the worker can
never reach that `app_version` and is retry-excluded **for the whole run**.

```bash
git status -sb | head -1        # must NOT say "ahead N"
git push origin dev
```

### b. Confirm it is actually distributed
`tools/grid_goal2020.sh` defaults `WORKERS=remote150` and prints its mode on line 1. The old
failure was silent: with no `--workers` the driver simply omits the flag, `worker_ids` stays NULL,
and the handler keeps the local path **with no warning** — the only tell is the *absence* of a
`DISTRIBUTED across` line. `grid_status.sh` now calls that out explicitly.

To run local-only on purpose: `WORKERS= nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &`

### c. Never edit the tree or bump `version.py` mid-run
Each job is a fresh subprocess reading the working tree, and the master snapshots its version at
job start. Edits land at the next job boundary and can desync master from worker.

The launch itself checks the one remaining prerequisite (the screener metric store must reach
`ym=2020-01`) and **refuses to start** otherwise.

---

## 3. Launching

```bash
git push origin dev
mv grid_goal2020.log grid_goal2020.log.$(date +%Y%m%d-%H%M) 2>/dev/null   # keep the old one
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &
```

Preview without running anything:

```bash
bash tools/grid_goal2020.sh --dry-run
```

### Knobs (environment variables)

| var | default | meaning |
|---|---|---|
| `WORKERS` | `remote150` | comma-separated remote worker names; empty = local-only |
| `SPREAD_BPS_LARGE` | `3` | round-trip spread, large band |
| `SPREAD_BPS_MID` | `10` | round-trip spread, mid band |
| `SPREAD_BPS_SMALL` | `40` | round-trip spread, small band |

The spread values are **assumptions from US equity market structure, not measurements** — we have
no quote data. A Corwin-Schultz high-low estimate was tried and rejected: it returns 76 bps for
AAPL and 44 bps for SPY, whose true spreads are ~1 bp. Any winner is conditional on these numbers;
the Monte Carlo spread sweep is what tells you whether an edge survives.

Anything after the script name is passed through to the driver, e.g.
`bash tools/grid_goal2020.sh --population 60`.

---

## 4. Monitoring

```bash
bash tools/grid_status.sh
```

A healthy run looks like:

```
RUN     script alive (1 bash, 2 driver)
DIST    distributed evaluator (opt 251): 4 local + 6 remote slot(s) across 1 worker(s)
SPREAD  (spread 3 bps round-trip)
JOB     [1/4] RUN  scr-large-FMPRating-S1-goal2020-riskatr-from2022
GEN     gen 3/8 ind 95/95
OPTS    1 row(s), 0 completed   (48 jobs total)
```

### What each line must show

| line | healthy | wrong |
|---|---|---|
| `RUN` | `script alive` | `PARTIAL` = the wrapper died; it will **not** advance to the next band. Stop and relaunch. |
| `DIST` | `4 local + 6 remote` | `!! LOCAL-ONLY` = the remote worker is not helping |
| `GEN` | advances every ~20-50 min | frozen for hours = investigate |
| `OPTS` | grows toward 48 completed | a `failed` row = read the log |

### Signals worth grepping

```bash
grep -E "DISTRIBUTED across|RESUMING|checkpoint" grid_goal2020.log   # good
grep -E "Traceback|FATAL|failed|retry-and-exclude|dead" grid_goal2020.log   # bad
```

**Normal and self-healing, do not intervene:** after a version bump the worker logs
`version X != master Y; updating + waiting...`, may briefly fail pre-flight with `WinError 10054`
(the pre-flight hit it mid-restart), then recovers with `worker remote150 recovered; re-admitted`.
Only worry if the recovery line never arrives.

**Memory.** The `gen N/M ind i/j` lines carry `master RSS` and system availability. ~2.5 GB per
local trial slot for LIGHT experts; on a 64 GB box, 4 local slots is the ceiling. Sustained
availability under ~5 GB means back off `--parallel`.

---

## 5. Stopping

```bash
bash tools/grid_stop.sh --dry-run     # inspect first
bash tools/grid_stop.sh
```

**Order matters, which is why this is a script.** The wrapper runs the driver as a child, one call
per (mode, band). Kill the driver first and the wrapper just moves on and starts the *next* band —
on 2026-08-04 that silently launched matrix 2 while the grid was believed stopped. The script goes
wrapper → driver → orphaned pool workers, with a pause between tiers.

The live trading platform is never touched: it runs from a different venv (`~/ba2-venvs/trade`).
The script prints the `ba2-trade` process count at the end — **it must still be > 0**.

Then retire the interrupted row:

```bash
.venv/Scripts/python.exe tools/grid_abandon.py <reason-slug>    # e.g. nocosts, localonly, outage
.venv/Scripts/python.exe tools/grid_abandon.py --list           # see all goal2020 rows
```

Rename rather than just cancel: two rows sharing a name is what broke the senate grid's resume on
2026-07-30 (the NOT_BEFORE guard is id-based, so a stale row sat above it and PASS 2 warm-started
from a pre-fix population). `completed` rows are left alone — those are real results.

---

## 6. Restarting / resuming

Same command as the launch. Two independent mechanisms carry work forward:

**Job level.** The driver skips any job whose `StrategyOptimization` row is `completed`, so a
finished job is never redone. Names are the identity, and are stable across the per-band
restructure.

**Generation level** (from 2026.08.1013). The GA checkpoints after every generation, so an
interrupted job resumes at its last completed generation instead of restarting from 1 — this did
not work at all before that version, which is what made an early abort cost 4h40m.

Resume is *refused*, and the job restarts cleanly from generation 0, when:

- the **gene space changed** (adding/removing a gene, or changing a range, population size or
  generation count). A checkpoint is chromosomes plus an RNG state, meaningless against a
  different space — so changing `_RM_OPT` deliberately invalidates every in-flight checkpoint.
- the job was **renamed** (`grid_abandon.py` does exactly this — deliberately).
- the checkpoint is **exhausted** (already at the final generation).

Known limitation: a resumed run's `all_results` restarts empty, so its row records only
post-resume trials. The search is intact (population, elites, best individual, both RNG states);
only the top-N candidate pool is thinner.

---

## 7. Reading the results

```bash
.venv/Scripts/python.exe tools/grid_abandon.py --list     # quick fitness-per-job view
ba2-test report                                            # full HTML report
```

Labels: `goal2020-riskatr` / `goal2020-notional`.

**Three things make this grid's numbers a new baseline, not a continuation:**

1. **CAR changed scale on 2026-08-04.** `dd_guard` went from a cliff (`1.0` below 20% drawdown) to
   a gradient (`20/max(dd,1)`). Never compare a fitness from before that date with one after.
   Rankings *within* a run are fine.
2. **Four regime-overlay genes were added**, so the space is wider than any previous grid at the
   same population. The overlay only acts on STRESSED benchmark bars — 2020 50%, 2021 3%,
   2022 69%, 2023 0%, 2024 10%, 2025 42%. A genome that enables it is betting on 2020/2022/2025.
3. **FMPRating starts 2022-01-01, not 2020**, and its jobs carry `-from2022` in the name. FMP's
   analyst price-target endpoint serves nothing before ~2021-04. Do not mix those rows with
   full-window rows in any per-year or consistency comparison.

**Before trusting or deploying any winner:**

- **Concentration check** — top-1 / top-5 trade share of net P&L from the persisted `trades` JSON.
  On the sen5min3 grid only S6 was clean; S1-S3/S5/S7 all rode never-exited winners.
- **Spread sweep** — the Monte Carlo robustness suite's `spread_sweep_bps`. The baseline spreads
  are assumptions; the sweep is what shows whether the edge survives them being wrong.
- **Ask whether the edge is just the regime gene** picking up 2022.

---

## 8. Files

| path | what |
|---|---|
| `tools/grid_goal2020.sh` | the run itself (window, fitness, per-band spread, both matrices) |
| `tools/run_screener_capband_matrix.py` | the driver — job list, resume-skip, per-expert start floors |
| `tools/grid_status.sh` | read-only status |
| `tools/grid_stop.sh` | ordered stop |
| `tools/grid_abandon.py` | retire interrupted rows |
| `grid_goal2020.log` | live log (repo root) |
| `~/Documents/ba2/test/dl_forecasting.db` | test-platform DB: optimizations, backtests, results |
| `~/Documents/ba2/common/cache/screener/metric_store` | screener metric store (must reach 2020-01) |

**Logs are not rotated.** A June `serve` run left a single 20 GB file. Check occasionally:

```bash
powershell -NoProfile -Command "Get-ChildItem *.log* | Sort-Object Length -Descending | Select-Object -First 5 @{n='MB';e={[math]::Round(\$_.Length/1MB,1)}},Name"
```

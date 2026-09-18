# Runbook — the goal2020 optimization grid

Everything needed to start, watch, stop and resume the grid without help. Run every command from
the repo root (`C:\Users\basti\Documents\dev\BA2TradePlatform`) in **Git Bash**.

Last verified 2026-08-04 against app_version 2026.08.1014. Job counts and the pace figure are
measured, not estimated (`--dry-run` per band; job 1 of the live run).

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

## Operator checklist (2026-09-03, options-grid2 closeout)

Standing items, also carried as header comments in the launchers
(`tools/run_options_matrix.py` for the option grids, `tools/run_screener_capband_matrix.py` for
the equity grids):

1. **Never merge into this checkout while a grid runs from it.** A long-lived master lazily
   imports new modules against the enums it loaded at start and dies at its persist phase
   (`AttributeError N_LOSS_PCT_OF_MAX_LOSS`, 2026-09-03). Merge at a job boundary: stop ONLY the
   wrapper bash shells (verify their cmdline), let the master finish, merge + ONE
   `TEST_APP_VERSION` bump, mark dangling `running` rows failed, relaunch from Git Bash
   (PowerShell `Start-Process nohup` resolves to WSL bash and fails). Lost TOP-N ranks:
   `tools/recover_missing_topn.py <worker> <opt>:<ranks>` (retry after the worker's version
   self-update, which forgets in-flight jobs).
2. **Live O_CC / O_WHEEL ExpertInstances deployed before 2026-09-03 must be re-exported and
   re-imported** (`tools/export_deploy_payload.py` / `tools/import_deploy_payload.py`, then
   `/api/reload`): the live lifecycle pass no longer closes the written call at the roll window;
   the ruleset's `cc_dte` rule owns that exit in both runtimes.
3. **Option grids: retarget to 2020-01-01 on the ThetaData store** after provider-parity pins,
   and do the option-cache optimization first (~3.6 s / ~22 MB per symbol cold on the 2024+
   store, ~x3 at 2020 → one parquet per symbol, higher `_MAX_TASKS_PER_CHILD` for option jobs,
   local slots sized from measured MB/symbol).
4. **`BT_BAR_CACHE_TRIALS=0`** re-preloads bars before every individual (matrix3 paid ~8 h);
   evaluate a non-zero value for the next equity launch (memory: the union of per-individual
   symbol sets is retained until recycle).
5. **Results baselines are split** — see `docs/` results-comparability note: BS mark fallback
   (options), the 683c7379 stress restatement (return / total_return / calmar fitness), O_CC and
   O_WHEEL after `cc_dte` + `wheel_stock_guard`. Never compare across a split.
6. **Agents' test suites compete with the grid for RAM**: targeted files, one pytest at a time.

---

## 1. What this grid is for

Re-optimize every classic expert over **2020-01-01 → 2025-12-31** on the fixed engine, and make
the result the new source of truth. It exists because every optimization before v2026.07.1002
scored with `DaysOpened` / `DaysSinceLastClose*` **inert** — 129 of 153 completed runs were built
on a strategy declaring at least one of them.

**45 jobs, run strictly one at a time**, as two sizing matrices × three cap bands:

| matrix | sizing_mode | bands | experts | jobs |
|---|---|---|---|---|
| 1 | `risk_atr` | large, mid, small | FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy, FactorRanker | 4 + 10 + 10 = **24** |
| 2 | `notional` | large, mid, small | same, **minus FactorRanker** | 3 + 9 + 9 = **21** |

The large band is small because FMPEarningsDrift and FMPInsiderClusterBuy are skipped there —
FMP has no large-cap insider data and the earnings-drift edge is a small/mid phenomenon.

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

**Expected duration — measured, and longer than you may have been told.** Job 1
(`FMPRating/S1/large`, the heaviest shape: FMPRating carries a population bonus and the large band
has the most symbols) ran **31.8 min/generation × 8 = ~4.2 h** at 4 local + 6 remote. At that pace
45 jobs is ~8 days; realistically **5-7 days**, since mid/small bands and the non-FMPRating
experts are lighter. Local-only is ~2.5× that.

An earlier "≈2 days" estimate in conversation was simply wrong — it is recorded here so the
number in your head matches the one the box will actually deliver. Plan around days, not hours,
and use `grid_status.sh` rather than waiting on it.

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
| `ROBUST_FITNESS` | `1` | rank on the robustness-adjusted fitness (concentration x monte-carlo x spread). `0` forwards `--no-robust-fitness` and ranks raw. |

The spread values are **assumptions from US equity market structure, not measurements** — we have
no quote data. A Corwin-Schultz high-low estimate was tried and rejected: it returns 76 bps for
AAPL and 44 bps for SPY, whose true spreads are ~1 bp. Any winner is conditional on these numbers;
the Monte Carlo spread sweep is what tells you whether an edge survives.

Anything after the script name is passed through to the driver. The ones you are most likely to
want:

| passthrough | default | when |
|---|---|---|
| `--population N` | 40 (+bonus for FMPRating) | wider search; costs time linearly |
| `--generations N` | 8 | ditto |
| `--parallel N` | 4 | LOCAL trial slots. **Lower it, never raise it**, on a 64 GB box — ~2.5 GB per slot for light experts, and Senate-class trials are ~11-12 GB. |
| `--bands a,b` | all three | re-run one band only |
| `--strategies S1,S3` | S1,S2,S3 | re-run specific strategies |

```bash
bash tools/grid_goal2020.sh --population 60
```

Changing `--population` or `--generations` changes the checkpoint fingerprint, so any in-flight
checkpoint is discarded and those jobs restart from generation 0. That is deliberate — see §6.

---

## 4. Monitoring

```bash
bash tools/grid_status.sh
```

A healthy run looks like:

```
RUN     script alive (2 bash, 3 driver)      <- counts vary; only 'script alive' matters
DIST    distributed evaluator (opt 251): 4 local + 6 remote slot(s) across 1 worker(s)
SPREAD  (spread 3 bps round-trip)
JOB     [1/4] RUN  scr-large-FMPRating-S1-goal2020-riskatr-from2022
GEN     gen 3/8 ind 95/95
OPTS    1 row(s), 0 completed   (45 jobs total)
```

### What each line must show

| line | healthy | wrong |
|---|---|---|
| `RUN` | `script alive` | `PARTIAL` = the wrapper died; it will **not** advance to the next band. Stop and relaunch. |
| `DIST` | `4 local + 6 remote` | `!! LOCAL-ONLY` = the remote worker is not helping |
| `GEN` | advances every ~20-50 min | frozen for hours = investigate |
| `OPTS` | grows toward 45 completed | a `failed` row = read the log |

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

### If a command dies with "FMP API key not configured"

The key lives in the **test-platform app-settings DB**, not the environment. `ba2-test` mirrors it
into the env at startup; a script that bypasses the launcher does not. Point `ba2_common` at the
test DB first, exactly as `ba2test_launcher._ensure_backend_on_path` does:

```python
from app.models.database import DATABASE_URL
from ba2_common.core import db as ba2_db
ba2_db.configure_db(DATABASE_URL.replace("sqlite:///", "", 1))
import os
from ba2_common.config import get_app_setting
os.environ["FMP_API_KEY"] = get_app_setting("FMP_API_KEY")
```

### Is the remote worker even up?

```bash
# The password lives in the workers table -- never hardcode it in a script or doc.
# NOTE the Windows-style path: Git Bash's $HOME is a POSIX path Windows Python cannot open.
DB='C:\Users\basti\Documents\ba2\test\dl_forecasting.db'
PW=$(.venv/Scripts/python.exe -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute(\"select password from workers where name='remote150'\").fetchone()[0])" "$DB")
curl -s -m 10 -H "Authorization: Bearer $PW" http://192.168.1.150:8100/health
```

Healthy looks like `{"ok":true,"capacity":6,"version":{"app_version":"...","git_commit":"..."}}`.
If `git_commit` is behind `origin/dev`, the worker will pull it at the next job's pre-flight —
that is expected, not a fault.

### A job ends up `failed`

The driver moves on to the next job; a `failed` row is NOT retried automatically and will be
re-attempted on the next launch (only `completed` is skipped). Read why first:

```bash
grep -B5 "strategy_optimization .* failed" grid_goal2020.log | tail -40
```

`0 successful trials — every backtest failed` almost always means a config problem affecting every
trial (a missing cache, a bad window), not a bad genome. Fix the cause, then relaunch: that job
starts fresh.

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

Resume is *refused and the job FAILS* (it does not restart, because silently restarting would
throw away work you may want) when:

- the **robustness setting changed**. Since 2026-09-17 the robustness-adjusted fitness is **ON by
  default** (`--robust-fitness`; `--no-robust-fitness` opts out, `ROBUST_FITNESS=0` in the grid
  scripts). It rescales the metric — concentration × monte-carlo × spread — so a population whose
  elites were scored raw and whose new individuals are scored robust carries two incomparable
  objectives at once, and the gene-space fingerprint cannot see it (the genes are identical).
  A checkpoint records the setting it was scored under; a mismatch raises, naming both values and
  the job name. A checkpoint written **before 2026-09-17 has no such key and is read as raw**,
  which is what those runs actually did — so every pre-flip checkpoint now refuses on resume.
  Two ways out: pass `--no-robust-fitness` (`ROBUST_FITNESS=0`) to match the checkpoint, or give
  the job a new name (`--name-suffix` / `STAGE1_SUFFIX`) and start fresh under the new objective.
  Scores either side of the setting are **not comparable** — never rank across it.

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

## 8. Recovery and edge cases

### There is no pause. Stop and relaunch instead.

The GA calls `is_task_paused(task_id)` every generation, but a CLI-launched grid passes the
literal task_id `"cli-optimize"` and **no `task_queue` row with that id has ever existed** — so
the check always returns False and the UI's pause button cannot touch a grid run. Verified:
`select count(*) from task_queue where task_id='cli-optimize'` → 0.

Since 2026.08.1013 this costs almost nothing: `grid_stop.sh` then relaunching resumes the
interrupted job at its last completed generation. Treat stop+relaunch as the pause.

### After a power cut, a crash, or a reboot

Nothing restarts itself. The grid does **not** survive a reboot, and the DB is left mid-flight.

```bash
bash tools/grid_status.sh                                  # 1. expect "STOPPED"
bash tools/grid_stop.sh --dry-run                          # 2. confirm no orphans survived
.venv/Scripts/python.exe tools/grid_abandon.py --list      # 3. any goal2020 row still "running"?
```

A row still marked `running` with no process behind it is the normal post-outage state — the
handler never got to write a terminal status. Retire it before relaunching:

```bash
.venv/Scripts/python.exe tools/grid_abandon.py outage
git push origin dev                                        # in case anything was committed since
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &
```

The relaunch resumes that job from its last checkpointed generation — the abandon step renames
the row, which frees the name, and the checkpoint is keyed on the *job name from the driver*, so
it is still found.

**Stale rows from OTHER grids are normal and are not yours to clean here.** As of 2026-08-04 the
DB still carries `sen5min-S5` ×3, `sen5min2-S1` ×2 and `diag-shardthrash` stuck at `running` from
earlier outages. `grid_abandon.py` deliberately only touches `%goal2020%`. Note the duplicated
names in that list — that is exactly the collision that broke the senate grid's resume, left in
place as a cautionary example.

### Orphaned pool workers

`spawn` pool children can outlive a killed master on Windows. `grid_stop.sh` sweeps them as its
third tier, but verify after any hard kill:

```bash
bash tools/grid_stop.sh --dry-run     # all three tiers should report "none"
```

Anything still listed is an orphan holding ~2.5 GB; re-run `grid_stop.sh` without `--dry-run`.

### Re-running one specific job

The driver only skips `completed` jobs, so the simplest re-run is to abandon that row and narrow
the invocation to the single job:

```bash
# example: just FMPRating S2 on the mid band, risk_atr
.venv/Scripts/python.exe tools/run_screener_capband_matrix.py   --start 2020-01-01 --end 2025-12-31 --fitness consistent_annual_return   --store "$HOME/Documents/ba2/common/cache/screener/metric_store"   --sizing-mode risk_atr --bands mid --strategies S2   --skip-experts FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker   --spread-bps 10 --workers remote150 --name-suffix=-goal2020-riskatr
```

The `--spread-bps` MUST match the band (3/10/40) or that row is not comparable with its matrix.

### Disk

The grid writes trial rows and Backtest blobs into the test DB, and reads a large cache. Current
footprint: cache 27 GB, test DB tree 24 GB, 741 GB free — not a concern today, but the test DB
grows with every persisted top-N. Logs are the thing that actually bites (§9).

### When it finishes

The wrapper prints `=== goal2020 COMPLETE`. Then:

```bash
bash tools/grid_status.sh          # OPTS should read 45 completed
ba2-test report                     # HTML summary
```

Go to §7 before trusting anything — the concentration check and the spread sweep are the two
that have actually changed conclusions in the past.

---

## 9. Files

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

## Market-condition feature store (design 2026-09-15)

The `ohlcv-v1` profile adds three entry gates per option structure (trend slope, ADX, realized-
volatility ratio), six searched genes in total per entry tree. `ta-structure-v1` adds five gates
and nine genes; both together add eight gates and fifteen genes. They read a **published snapshot**: a manifest
pinning immutable feature objects, built once before a run and mapped per host. **Nothing is
computed in a trial.** With no profile selected (the default) none of this exists — the launcher
emits no leaves, the seam installs nothing, and the run is byte-for-byte the one it has always
been (pinned by `tests/backtest/test_market_condition_all_off_matches_baseline.py`).

### Published snapshots — every universe symbol warmed, not every row usable

Pin these. Window 2020-01-02..2025-12-31 (decision dates; the feature sessions they read run
2019-12-31..2025-12-30), universe `tools/options_universe_top100.txt`:

| profile | digest | symbols the build warmed | symbols with recorded status exceptions |
|---|---|---|---|
| `ohlcv-v1` | `c9ba981fbae8726ec749eca4201c98399a4285046733d16cca6b112b8c8371df` | 98 of 98 | 6 |
| `ta-structure-v1` | `3c3020d05f9e24ded59272050e1f06193abc74e6c00e41e3168a1750bcc44385` | 98 of 98 | 98 |

Verified on publication: `identity_ok`, 7154 feature objects + 7358 raw shards re-hashed, none
corrupt or missing.

**"98/98" means NO SYMBOL WAS EXCLUDED FROM THE BUILD. It does not mean every row is a valid
observation** — and the manifest-level exclusion list being empty says only the first of those.
The per-symbol records say the second, and they are not empty (review 2026-09-16):

* **92 symbols are fully observed** for `ohlcv-v1`: a valid row on every session after their
  127-session warm-up prefix.
* **APP, ARM, GEV, PLTR, SNDK** list part-way through the window. Their rows before the listing
  are `missing_session` and the next 127 are `insufficient_history`. That is the truth about
  those years, not a defect: no download creates price history a symbol did not have, and no
  strategy could have traded them then either. The launch check accepts them.
* **SPCX had NO usable row anywhere in 2020–2025** — all 1,508 `missing_session`, because its
  daily cache starts 2026-06-12. **Removed from `tools/options_universe_top100.txt` on
  2026-09-16** (that file is a bare symbol-per-line list which three of its four readers parse by
  whitespace with no comment syntax, so the reason is recorded here instead). The universe is now
  97 symbols. Removing it does **not** invalidate the two digests: a manifest's identity is the
  hash of its own content, the universe file is not part of it, and the launcher's coverage check
  simply stops asking for that symbol. The published snapshots still carry SPCX's rows and are
  pinned unchanged. Ungated runs could not trade it either (no price data => no recommendation),
  so gated and ungated runs stay comparable.
* Per OHLCV field: 5,307 `missing_session` rows (1,508 of them SPCX) and 635
  `insufficient_history`. Do not add the three fields' counts as if they were separate sessions.
* `ta-structure-v1` adds legitimately undefined levels and states — resistance distance alone has
  15,494 `insufficient_history` rows. **An undefined pivot is not repaired by downloading more
  history.** Report usable coverage by field and eligible recommendation, never as "98/98 warmed".

See the [implementation review](../reports/strategy_research/market_conditions_review_2026-09-16.md).

> **History, 2026-09-16 — do not pin `1136d489…f5cb3`.** The first build of `ohlcv-v1` covered
> only 85 of the 98: thirteen symbols (`ASML BHP DELL GE HON IBM MRK NVS RTX SAN SCCO T WDC`)
> carried a split whose basis the cached prices could not settle, so the warmup refused to
> compute from them (`refetch_required`). The operator chose to re-fetch rather than trim the
> universe: `build --fetch-missing --concurrency 3` replaced each of the thirteen cache files
> wholesale — which is what clears a stale split basis; a patched file keeps it — in ~6 s and 13
> provider calls (DELL 2534 bars, the other twelve 3769 each). Both profiles were then published
> over the repaired cache; `ta-structure-v1` needed no provider calls at all (it reads the same
> repaired raw shards). The 85/98 digest is superseded.

**Why coverage is refused rather than tolerated.** The launcher refuses to dispatch a run whose
pinned manifest does not cover its `enabled_instruments`, and the seam refuses a GA trial on the
same condition. An uncovered symbol reads `missing_session` at every gate for the whole run, so
the genome that would have traded it scores as though its strategy simply did not fire there — a
feature-cache miss silently becoming a property of the fitness landscape.

**The run's SESSIONS are checked too (added 2026-09-16, review F1).** Symbol presence alone let a
snapshot warmed for one window be pinned on another: it carries every symbol and not one row the
run will read, so every gate reports `missing_session` on every decision date. The launcher now
validates the `(symbol, prior session)` rows the run's `start_date..end_date` actually requires,
once per (digest, universe, window), before dispatch — and refuses an out-of-range pin or an
unexplained hole as a **job configuration error**, never as a zero-trade fitness. It deliberately
does NOT refuse the legitimately undefined observations above (pre-listing rows, the warm-up
prefix, an unconfirmed pivot). If the refusal names a symbol with "no usable observation
anywhere", that symbol belongs out of the universe (as SPCX now is) or needs its source history
repaired. If a future universe change reintroduces uncovered symbols, the two ways forward are
the same: re-fetch
(`warm_market_conditions.py build --plan <plan> --fetch-missing`, a real provider bill — size it
with `plan` first), then re-publish and re-pin; or trim the universe to the covered set and pass
that file to the driver.

**Running the warm tools from a WORKTREE on Windows — two traps.** `PYTHONPATH` does not work: the
test venv's editable install puts a *meta-path finder* for `ba2_common` ahead of it, pointing at the
MAIN checkout, so the tool dies on `No module named ba2_common.core.market_condition_source`. Only a
`sys.path.insert` of the three `packages/*` dirs **before the first import** wins. And the tools read
the FMP key from the TEST database, so `DB_FILE` and `DATABASE_URL` must point at
`~/Documents/ba2/test/dl_forecasting.db` — without them every symbol fails preflight with
"FMP API key not configured", which reads like a data problem and is not.

### Warm the snapshot (once, on the master, before any job)

```bash
MARKET_CONDITION_PROFILE=ohlcv-v1 tools/stage1_run.sh --dry-run    # prints the four commands
```

`--dry-run` never fetches and never launches; it prints the exact warm sequence for the
resolved window and universe. Run those four, in order, on the master:

```bash
$PY tools/warm_market_conditions.py plan  --profile ohlcv-v1 \
      --universe-file tools/options_universe_top100.txt \
      --start 2020-01-01 --end 2025-12-31 --out market_conditions_plan.json
$PY tools/warm_market_conditions.py build --plan market_conditions_plan.json \
      --cache-only --print-digest          # stdout = the digest; the JSON report is on stderr
$PY tools/warm_market_conditions.py verify       --manifest <digest>
$PY tools/warm_market_conditions.py prepare-host --manifest <digest> --profile ohlcv-v1
```

* `plan` is an inventory plus a source preflight; a plan that reports missing coverage is
  **actionable**, not a warning to pass. `--cache-only` on `build` is deliberate: a warmup that
  fetches while a grid waits is a surprise provider bill measured in hours.
* `verify` re-hashes every object and raw shard the manifest references.
* `prepare-host` builds this box's mapped arrays. It is an **optimisation, not a correctness
  requirement** — the master runs `prepare_host` itself before dispatch — but a cold first job
  otherwise pays it.
* Then launch with the digest pinned:

```bash
MARKET_CONDITION_PROFILE=ohlcv-v1 MARKET_CONDITION_MANIFEST=<digest> tools/stage1_run.sh
```

`stage1_run.sh` does the whole sequence itself when `MARKET_CONDITION_PROFILE` is set and
`MARKET_CONDITION_MANIFEST` is not; it refuses to launch if any step fails.

**Remote workers prepare themselves.** `distributed_eval._preflight_worker` calls
`/market-conditions/prepare` after `push_cache`; a worker whose prepare fails is **excluded from
the run** rather than handed trials it would answer with no rows.

### Is a host ready?

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://<worker>:8100/health | jq .market_conditions.prepared
```

A list of digests. **A digest absent from it means "unready", never "run it anyway"** — the
master's pre-flight reads exactly this. `/market-conditions/prepare` is submit/poll like a trial
(it returns a `job_id`; the digest is admitted when the poll collects an `ok` report), because
verifying a season of objects is minutes of I/O and as a blocking handler was indistinguishable
from a hung worker.

### Retention, revocation and sweeping

* **There is no GC or compaction yet.** A future collector must walk **OBJECTS**, not only
  manifests: whole-object reuse preserves old `raw_shard_ref` values verbatim, so a raw shard is
  pinned by any manifest that transitively references it. Deleting by "manifests I still care
  about" would strand exactly the shards a reused object still points at.
* Progress records under `_derived/market_conditions_build` **expire after 24 h**.
* Readiness markers of a revoked digest become `.revoked` and are **pruned after 30 days**.
* **Revocation is scoped.** A corrupt cache push revokes only the digests whose verification
  failed; manifests outside the push's scope are never revoked by an unreadable file.
* A **`LAYOUT_VERSION` bump moves every mapped key**, so the old mapping directories are
  orphaned on every host at once. Collect them with `tools/build_shared_arrays.py --sweep`,
  which also drops dead readiness markers.
* `BA2_SHARED_ARRAYS=0` loads the same central rows into **private** arrays: identical values,
  still nothing recomputed. Use it to isolate a mapping problem from a data problem.

### Live

Set `market_condition_profile` on each expert to an empty string (off), `ohlcv-v1`,
`ta-structure-v1`, or `ohlcv-v1,ta-structure-v1`. The setting travels with the deploy payload.
Do **not** set `BA2_MARKET_CONDITION_PROFILE`: it is retired and a nonempty value is rejected.

For a reproducible pinned reader, the host still uses:

```
BA2_MARKET_CONDITION_MANIFEST=ohlcv-v1=<digest>,ta-structure-v1=<digest>
```

Pin every profile the box serves. The map is HOST-WIDE and the profile is PER EXPERT, so each
expert selects the subset it needs: with both profiles pinned, an expert on `ohlcv-v1` alone, one
on `ta-structure-v1` alone and one on both all resolve (fixed 2026-09-16, review F3; before that
only an expert naming every pinned profile could be built). A single-profile host may still use a
bare digest. An unregistered profile name, a profile pinned twice, a pin with no digest and
mixing the two shapes are all still refused. A profile the map does not pin is research mode for
that expert — reported once per decision pass, not silently computed.

`wire_all_seams` installs a dispatcher; readers and source certification are created lazily for
experts with a nonempty profile setting. An empty setting performs no feature reads. A profile
without a manifest computes from the local FMP cache and logs an error for each decision pass;
it is not the same pinned-input configuration as a GA trial. A historical 2020–2025 manifest
does not cover current live sessions: prepare a snapshot for the live decision dates before
pinning it, and renew it as dates advance.

* **A certification failure does not stop the platform.** Exits and protective-order handling
  must keep running, so an `UncertifiedSourceResolver` is installed instead: every gate resolves
  no context with the certification summary as its reason (one ERROR at install, one WARNING per
  field), so gated **entries** are refused loudly while everything else runs.
* **Coverage is checked against the live universe** — the union of the enabled instruments of
  every enabled expert instance whose enter-market ruleset carries a market leaf — when its
  resolver is built and at decision passes, with repeated comparisons cached. Each uncovered symbol gets **one ERROR naming the
  digest**, and its gates report `no_context` with that reason. An instance that picks its
  universe at analysis time (`EXPERT`/`DYNAMIC`/`SCREENER`) cannot be pre-checked and is reported
  as such.
* **Live refresh reports split-basis drift; it does not automatically replace history.** The
  automatic repair was reversed by operator decision. Inventory the live universe with
  `warm_market_conditions.py plan`, then use the explicit warmup `--fetch-missing` repair path
  when required. That path can call `force_full_refetch`; the earlier option-universe repair
  involved 13 symbols. Do not assume restarting the platform repairs the source cache.
* Market leaves are refused on open-positions / exit rulesets, and an unresolved mode gene is
  refused at export: live receives concrete conditions only.

### Reading the results

```bash
$PY tools/report_market_conditions.py --opt <id> [--top 5] [--coverage] [--out report.md]
$PY tools/report_market_conditions.py --like %ohlcv% --top 3
```

Per job: the versions **as persisted with the run**, the winning modes and thresholds per
structure, the gate counters (eligible recommendations reported separately from gate rejections,
and unknown input broken out by reason), submitted vs filled structures, per-year profit /
return / drawdown from the account engine, and the attribution of net P&L and top-1/top-5
concentration to explicit entry-state bins. `--coverage` adds the offline snapshot diagnostic,
which is the one to run on a **feature-off** winner: it says which symbols and sessions would
have been unknown, reading the manifest only.

A bin is not an account. The report never annualises a filtered subset of overlapping trades,
and neither should a summary of it.

### Performance

`testplatform/backend/tests_scripts/bench_market_conditions.py` (see
`reports/strategy_research/market_conditions_bench_2026-09-16.md` for the measured numbers and
the acceptance verdict). `--quick` runs the whole harness on a fabricated store in a second.

## remote227 (babatest) traps found 2026-09-15

* **logind `RemoveIPC`** deletes a non-system user's POSIX semaphores (`/dev/shm/sem.mp-*`) when
  that user's last login session ends. A GA unit running as `debian` under `systemd-run` then
  loses the semaphores its process pools created: children spawned later (a lazily-started
  slot, a recycle, a generation-boundary rebuild) die in `SemLock._rebuild` with
  `FileNotFoundError`, the executor reports "A process in the process pool was terminated
  abruptly", the job exits 1. Already-running children are unaffected, which is why it looks
  like a random mid-run death. Fix: `sudo loginctl enable-linger debian` and `RemoveIPC=no` in
  `/etc/systemd/logind.conf`. The "benign `sem_unlink FileNotFoundError` noise at pool recycle"
  seen on 2026-08-30 was this.
* **`RLIMIT_NOFILE`**: launch GA units with `-p LimitNOFILE=524288`; every memory-mapped derived
  array holds a descriptor (98 x 18 = 1764 > the 1024 default).
* **Ownership of the isolated home**: `/home/debian/ba2-grid/home` must be owned by `debian`
  (it was `ba2worker` 750, which blocked the derived cache). The fleet worker does not use it.
* Never `pgrep -f spawn_main` from an ssh command that contains the string (self-match killed the
  shell); use `pgrep -f multiprocessing.spawn`.

## remote227: updating the build and warming a snapshot (2026-09-16, all four found the hard way)

The 2026-09-16 gated launch hit four host-specific blockers in a row. None is in the code; all
four are in HOW you drive that box. In order:

**1. The mirror is not yours to write.** The grid clone's `origin` is `/opt/ba2worker/BA2TradePlatform`,
owned by `ba2worker`. `git fetch origin` inside it fails as `debian` with "insufficient permission
for adding an object to repository database" as soon as there are new objects — and the mirror's
local `dev` branch lags whatever it last pulled anyway. **Fetch GitHub directly into the grid
clone** (the box has outbound access):

```bash
cd /home/debian/ba2-grid/repo
git fetch https://github.com/bmigette/BA2TradePlatform.git dev:refs/remotes/gh/dev -f
git merge --ff-only gh/dev          # the grid branch carries no unique commits; check first:
                                    #   git log --oneline gh/dev..$(git rev-parse --abbrev-ref HEAD)
```
Do NOT "fix" this by updating the mirror: it is the fleet worker's own repository.

**2. Use the worker venv, and hand the tools a database.** The grid clone has no `.venv`, and the
system `python3` has no numpy:

```bash
PY=/opt/ba2worker/ba2-venvs/test/bin/python
export BA2_HOME=/home/debian/ba2-grid/home
export DB_FILE=/home/debian/ba2-grid/home/test/dl_forecasting.db
export DATABASE_URL="sqlite:////home/debian/ba2-grid/home/test/dl_forecasting.db"
export PYTHONPATH=/home/debian/ba2-grid/repo/packages/common:/home/debian/ba2-grid/repo/packages/providers:/home/debian/ba2-grid/repo/packages/experts:/home/debian/ba2-grid/repo/testplatform/backend
```
Without `DB_FILE`/`DATABASE_URL` the warm reads no FMP key and EVERY symbol fails preflight with
"FMP API key not configured" — which reads like a data problem and is not.

**3. The OHLCV cache is owned by `ba2worker`, and the repair needs to WRITE it.**
`/home/debian/ba2-grid/home/common/cache/FMPOHLCVProvider` was `drwxr-x---  ba2worker ba2worker`.
`debian` is in the `ba2worker` group, so the plan step READS fine and reports the 13 split-drifted
symbols — then `build --fetch-missing` fails every one of them with
`PermissionError: ... ASML_1d.parquet.tmp` and publishes NOTHING. Grant group write once, matching
the convention the parent cache directory already uses (`ba2worker:debian`, `drwxrwsr-x`):

```bash
D=/home/debian/ba2-grid/home/common/cache/FMPOHLCVProvider
sudo chmod g+ws "$D"
sudo find "$D" -maxdepth 1 -name '*.parquet' -exec chmod g+w {} +
```
Only that directory. The option stores are read-only to the warm and were left alone.

**4. A cache older than the re-fetch reach used to be unrepairable.** Fixed in TEST_APP 0046: the
request now starts at `min(15 years, the cache's first bar)`. Before that, T and WDC there (3777
bars from 2011-06-22) made a faithful 15-year answer look SHORT, and the C1 data-loss guard refused
the repair for ever. If you see `full re-fetch of X returned LESS history than the cache holds`,
check the build is 0046 or newer before suspecting the vendor.

**There is no `ba2-stage1` systemd unit on that host.** `systemctl is-active ba2-stage1` answers
`inactive` for a unit that does not exist, which reads like a stopped service. `systemctl show
ba2-stage1 -p FragmentPath` returns empty — that is the tell. Launch detached instead, from the
repo root, and never edit `tools/stage1_run.sh` while it runs:

```bash
cd /home/debian/ba2-grid/repo
export MARKET_CONDITION_PROFILE="ohlcv-v1,ta-structure-v1"
export MARKET_CONDITION_MANIFEST="ohlcv-v1=<digest>,ta-structure-v1=<digest>"
nohup bash tools/stage1_run.sh > /home/debian/ba2-grid/stage1_2020.log 2>&1 &
```

**Digests are per host and that is correct.** The snapshot identity is content-addressed over the
data actually warmed, so remote227's digests differ from a workstation's whenever the two price
caches differ. Each host warms and VERIFIES its own; never copy a digest between hosts and assume
it resolves.

**OWED (do not do while a grid runs):** fold blockers 2 and 3 into `tools/stage1_run.sh` as a
preflight — refuse with the exact `chmod` line when the provider cache is not writable, and refuse
when `DB_FILE`/`DATABASE_URL` are unset — so the next operator gets one refusal instead of four
investigations.

## Database backups (2026-09-15)

`tools/backup_dbs.py` copies the PROD trade DB, the DEV trade DB and the TEST/GA DB with SQLite's online-backup
API (safe while the platforms and a GA write), `quick_check`s the copy, deflates it to
`G:\Mon Driveackup\BA2\<prod|test>_<YYYY-MM-DD>.sqlite.zip`, and keeps the newest 7 per
database. Windows Task Scheduler task **`BA2 DB Backup`** runs it daily at 00:00 as the
interactive user (Google Drive's `G:` only exists in the logged-on session), 4 h limit, no
overlapping instances. Log: `G:\Mon Driveackup\BA2ackup.log`. Measured 2026-09-15: prod
411 MB -> 93 MB in 12 s. Manual run: `.venv\Scripts\python.exe toolsackup_dbs.py [--dry-run]`.

**Weekly remote pull (stage-1 isolated DB).** `tools/backup_remote_db.py` runs the same online
backup + `quick_check` + zip ON remote227 (python3 over one ssh session, `nice`d so the grid is
not disturbed), scp's it to `G:\Mon Driveackup\BA2
emote227-stage1_<YYYY-MM-DD>.sqlite.zip`,
deletes the remote copy and keeps the newest 4. Task **`BA2 Remote DB Backup`**, Sunday 01:00,
interactive user (needs the ssh key + G:). Stage-1 results live ONLY in that isolated DB
(`/home/debian/ba2-grid/home/test/dl_forecasting.db`); nothing syncs them to the local test DB.

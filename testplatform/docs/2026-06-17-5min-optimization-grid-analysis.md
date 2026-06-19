# 5-min Optimization Grid — Test & Performance Analysis (2026-06-17)

Purpose: document the tests run against the 5-min strategy-optimization grid, their
metrics, and the **performance bottleneck** — so the run can be evaluated / moved to a
faster machine. The grid is feasible and correct; it is just **CPU-bound and slow on this
laptop**.

---

## 1. Environment (the machine under test)

| | |
|---|---|
| CPU | 13th Gen Intel Core **i9-13900H** — **14 physical / 20 logical** cores |
| RAM | 63.7 GB |
| Test venv | `C:\Users\basti\ba2-venvs\test` (editable installs of ba2_common/providers/experts) |
| Test DB | `BA2TestPlatform\backend\dl_forecasting.db` (backtests / strategy_optimizations / task_queue) |
| OHLCV/data | FMP, 5min fully cached for the 30-symbol universe (as-of Parquet cache) |

**Grid config under test:** `optimize-batch` — experts
{FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy} × strategies {S1, S2, S3} +
FactorRanker (10 jobs) · 30 NASDAQ symbols · 2023-01-01 → 2026-01-01 (3 yr) ·
fitness = `calmar_ratio` · interval = `5min` · population 40 · generations 8 · `--parallel 6`.

Reproduce on any device: `scripts/run_phase1_grid.sh` (pre-cache → prewarm → one grid;
date window / interval / parallelism are env-overridable). S1 rulesets live in
`docs/live_rulesets/*.json`; S2/S3 + FactorRanker are built in `ba2test_launcher.py`.

---

## 2. Correctness tests (all passed)

### 2a. Metrics cadence-bug fix (Calmar/annualized return)
`backend/app/services/backtest/results.py` annualized over the **equity-point count**
(`(n_points-1)/252`). With a 5min fill clock (+ skip-flat-bars) the curve has tens of
thousands of irregularly-spaced points, so a 3-yr run looked like *hundreds of "years"* →
`annualized_return → ~0` and **`calmar → ~0.01` for every individual** (a flat, useless GA
fitness landscape). Fixed to annualize from the **actual calendar time** the curve spans
(`_years_spanned` / `_periods_per_year`); now correct & cadence-independent for daily, 5min
and skip-flat curves. Regression test added; 18 results-metrics tests pass.
Commit `74c4e97`.

### 2b. 1d vs 5min trade-count A/B (identical FMPRating config, 5 symbols, 2023)
Confirms switching the fill clock does **not** change the number of trades — entries are
driven by the (weekly) analysis cadence; the 5min clock only changes fill precision.

| metric | 1d | 5min |
|---|---|---|
| **trades** | **5** | **5** (MATCH) |
| total return % | 41.2 | 38.1 |
| annualized % | 41.9 | 38.7 |
| calmar | 5.91 | 2.38 |
| sharpe | 2.65 | 1.19 |
| volatility % | 13.6 | 31.5 |

Return/annret same order of magnitude; 5min volatility is higher (intraday sampling captures
more variance — expected). Calmar is **sane (2.38), not 0.01** → fix verified on real data.

### 2c. Stale-package fix (why the "caching" optimizations weren't active)
`ba2_providers` + `ba2_experts` were **non-editable (copied) installs** in the test venv, so
the pulled provider request-caching + `prewarm` code was inert (prewarm crashed on
`ImportError: _fmp_history_cache_dir`). Reinstalled both **editable** → source-linked like
`ba2_common`; `git pull` is now live with no reinstall. prewarm now warms the FMP history
disk cache (earnings/ratings/insider) for all 30 symbols before the GA pool spawns.

---

## 3. Existing persisted backtest metrics (kept; healthy)

These are prior **1d** runs (the buggy pre-fix 5min rows were deleted). They show the
strategies are sound when metrics are computed correctly:

| id | run | trades | return % | ann % | calmar | win % |
|---|---|---|---|---|---|---|
| 84 | TOP1 phase1-FMPRating-S1 | 49 | 175.0 | 40.4 | **3.19** | 87.8 |
| 85 | TOP2 phase1-FMPRating-S1 | 74 | 244.6 | 51.5 | 2.89 | 75.7 |
| 86 | TOP3 phase1-FMPRating-S1 | 80 | 246.7 | 51.8 | 2.88 | 76.3 |
| 87 | TOP4 phase1-FMPRating-S1 | 106 | 241.8 | 51.0 | 2.87 | 75.5 |
| 88 | TOP5 phase1-FMPRating-S1 | 79 | 246.1 | 51.7 | 2.86 | 76.0 |
| 83 | TOP1 FMPRating nasdaq30 | 222 | 143.9 | 34.9 | 2.25 | 27.5 |

(Calmar ~2.9, win-rate ~76% on FMPRating-S1 1d — the kind of result the 5min grid is trying
to reproduce/improve with precise intrabar fills.)

---

## 4. Performance analysis — the bottleneck

**Measured throughput (5min, 3yr, 30 sym, `--parallel 6`):**
- ~**5.0 min / individual** (steady state) → each backtest is ~**25–30 min** of single-core
  work; 6-way parallelism is what divides it down to ~5 min/individual.
- Two independent measurements agree (a 100-min window earlier, and a fresh 10-min probe
  post-caching: 0→2 individuals/600 s).

**The network/provider caching did NOT change this.** prewarm + request-caching is correct
and worth keeping, but the dominant cost is **CPU**, not network: the dense **5min
bar-by-bar TP/SL fill loop** runs over every open position on every 5min bar, and gen-1
random individuals open many positions → ~1–2 M position-bar evaluations per backtest over
3 yr × 30 symbols. Cost scales with how much the strategy is *in the market*, so it is
**data-dependent** (a sparse strategy like the bare-FMPRating A/B ran in seconds).

**Under-utilized cores.** `--parallel 6` on a **14-physical-core** CPU leaves ~8 cores idle.
Each worker is ~1 core, so raising `--parallel` toward the core count scales nearly linearly.

### ETA at current settings (3yr / 5min / parallel 6)
- Gen 1 (40 new individuals): ~**3 h**
- 8 generations: ~**16–24 h per job** (later gens partly faster: elitism + convergence to
  more-selective strategies = fewer position-bars)
- **10-job grid: several days**

---

## 5. Recommendations (to make it practical — combine as desired)

| Lever | Speedup | Trade-off |
|---|---|---|
| **`--parallel` = physical cores** (e.g. 12 here, more on a server) | ~2× on this box, more on a bigger one | none (RAM is ample at 64 GB) |
| **15min search clock** (`INTERVAL=15min`), validate winners at 5min | ~3× (⅓ the bars) | slightly coarser intrabar TP/SL during search only |
| **1-year window** for a first pass | ~3× | less out-of-sample; re-run promising configs on 3 yr |
| **Smaller universe** (~12 of 30 symbols), re-rank winners on full 30 | ~2.5× | phase-1 search on a subset |
| **Faster / many-core machine** (cloud VM, 32–64 vCPU) | ~5–10× | infra setup |

**Best single move for "test elsewhere":** run on a high-core-count machine and set
`PARALLEL=<physical cores>`; near-linear scaling means a 32-vCPU box turns the multi-day grid
into a few hours, all else equal. Stack with `INTERVAL=15min` for the search to shorten it
further, then confirm the top configs at 5min.

Example (32-core box, 15min search, 3-yr):
```
PARALLEL=32 INTERVAL=15min START=2023-01-01 END=2026-01-01 scripts/run_phase1_grid.sh
```

---

## 6. Status at time of writing
- Grid **running** (task 14 / strategy_optimization 41 = `phase1-FMPRating-S1`), Gen 1/8,
  on the corrected metrics + editable (caching-active) packages.
- Left running per request; this doc is for evaluating a move to faster hardware.

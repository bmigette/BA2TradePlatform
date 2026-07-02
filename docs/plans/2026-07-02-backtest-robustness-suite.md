# Backtest Robustness Suite (Monte Carlo + Schedule Perturbation) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the user select saved backtests in the UI and stress-test them two ways — trade-level Monte Carlo (resample/shuffle/drop-outliers: is the curve luck?) and schedule perturbation (re-run with shifted analysis day/time: is the edge an artifact of *when* we analyze?) — and expose every fitness/metric knob (fitness metric incl. `consistent_annual_return`, profit caps, trade scale) in the optimization UI.

**Architecture:** Pure-function Monte Carlo core over the persisted `Backtest.trades` JSON (no re-run, milliseconds); schedule perturbation reuses the existing `/rerun` reconstruction path but writes N variant rows (never in place) via the dedicated re-run task queue; one new `RobustnessRun` table links a parent backtest to its MC results + variant rows. UI adds multi-select + a Robustness dialog + a results panel (recharts box/percentile views) to `Backtesting.tsx`, and a fitness-options block backed by a new metrics-catalog endpoint so the UI can never drift from `strategy_fitness.py`.

**Tech Stack:** FastAPI + SQLAlchemy (testplatform backend), numpy (already a backend dep), React + recharts (frontend), existing `task_queue` re-run pool.

**Key existing anchors (verified):**
- `testplatform/backend/app/api/backtests.py:592` — `/{backtest_id}/rerun` re-executes a row's ORIGINAL config via `get_rerun_task_queue()`; the rerun task handler knows how to rebuild a config from a row (grep `rerun_backtest` in `app/services/task_queue.py` / handlers). Schedule variants clone this, overriding `run_schedule_override` / `manage_schedule_override` (same keys `daily_engine._entry_schedule/_manage_schedule` read).
- `Backtest` row carries `trades` (list of `{pnl, pnl_pct, entry_time, exit_time, exit_reason, ...}` — `pnl_pct` is % of account equity), `equity_curve`, `results` JSON (all metrics + `profit_cap_pct`/`profit_share_cap_pct`), `strategy_params`, `initial_capital`, `is_saved`.
- `app/services/strategy_fitness.py` — `compute_fitness(metric, results)`; `consistent_annual_return` lands there (in-flight agent).
- Migrations: `testplatform/backend/db_migrate/` numbered scripts; next free number **025**.
- Frontend: `testplatform/frontend/src/pages/Backtesting.tsx` (recharts imported at :82).
- Tests: `testplatform/backend/tests/` (pytest, test venv `C:\Users\basti\ba2-venvs\test\Scripts\python.exe`, run from `testplatform/backend`).

**Design decisions (locked with user):**
- MC methods: (a) bootstrap resample trades with replacement, (b) order shuffle (permutation — same total, different DD path), (c) drop-K best trades (K=1,2,3 — the "was it luck" test), (d) optional per-trade slippage jitter (±bp noise). Default 1000 paths, seeded (deterministic).
- MC equity model: apply each trade's `pnl_pct` (equity-relative) sequentially to a synthetic equity path — an approximation of overlapping positions, documented as such; good enough to rank robustness.
- Outputs per backtest: p5/p25/p50/p75/p95 bands for {annualized_return, max_drawdown, calmar}, `prob(ann_return >= 30%)`, `prob(dd <= -20%)`, drop-K return table, and the NEW `consistent_annual_return` fitness recomputed per path (ties robustness directly to the goal metric).
- Schedule variants: weekly-day shift (Mon..Fri) for entry schedule; time-of-day shift (09:30 → 10:30 / 12:30 / 15:00) for entry; manage-time shift optional v2 (YAGNI now). Each variant = one new Backtest row named `RBST-<variant>-<parent name>`, `optimization_id` NULL, linked via RobustnessRun.
- Concentration note: MC drop-K is the *proper* luck detector — trades are never avoided, only the ranking view changes (mirrors the profit-share-cap philosophy).

---

### Task 1: Monte Carlo core (pure functions)

**Files:**
- Create: `testplatform/backend/app/services/backtest/monte_carlo.py`
- Test: `testplatform/backend/tests/backtest/test_monte_carlo.py`

**Step 1: Write the failing tests**

```python
# tests/backtest/test_monte_carlo.py
import numpy as np
from app.services.backtest.monte_carlo import (
    equity_path_from_trade_pcts, mc_bootstrap, mc_shuffle, drop_k_best, summarize_paths,
)

def _trades(pcts):
    return [{"pnl_pct": p, "exit_time": f"2023-0{1+i%9}-15T00:00:00"} for i, p in enumerate(pcts)]

def test_equity_path_compounds_equity_relative_pcts():
    # +10% then -10% of ACCOUNT equity => 1.10 * 0.90 = 0.99
    path = equity_path_from_trade_pcts([10.0, -10.0], initial=10_000.0)
    assert abs(path[-1] - 9_900.0) < 1e-6

def test_shuffle_preserves_total_return_but_not_dd():
    pcts = [5.0, -3.0, 8.0, -6.0, 4.0] * 10
    r = mc_shuffle(pcts, initial=10_000.0, n_paths=200, seed=7)
    finals = {round(p["final_equity"], 4) for p in r}
    assert len(finals) == 1                     # permutation: same compounded total
    dds = {round(p["max_drawdown"], 2) for p in r}
    assert len(dds) > 1                          # ...but drawdown is path-dependent

def test_bootstrap_is_seeded_deterministic():
    pcts = [5.0, -3.0, 8.0]
    a = mc_bootstrap(pcts, initial=10_000.0, n_paths=50, seed=42)
    b = mc_bootstrap(pcts, initial=10_000.0, n_paths=50, seed=42)
    assert [p["final_equity"] for p in a] == [p["final_equity"] for p in b]

def test_drop_k_best_removes_top_profit_trades():
    trades = _trades([30.0, 2.0, -1.0, 5.0])
    out = drop_k_best(trades, k=1, initial=10_000.0, years=3.0)
    assert out["dropped"] == [30.0]
    assert out["annualized_return"] < 10.0       # the 30% trade carried it

def test_summarize_paths_percentiles_and_probs():
    paths = [{"annualized_return": r, "max_drawdown": -d, "calmar": 1.0}
             for r, d in [(10, 5), (20, 10), (30, 15), (40, 25), (50, 30)]]
    s = summarize_paths(paths, target_annual=30.0, dd_limit=20.0)
    assert s["annualized_return"]["p50"] == 30
    assert abs(s["prob_target_annual"] - 0.6) < 1e-9     # 30,40,50 of 5
    assert abs(s["prob_dd_breach"] - 0.4) < 1e-9         # -25,-30 of 5
```

**Step 2: Run to verify failure** — `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/backtest/test_monte_carlo.py -q` → ImportError.

**Step 3: Implement** `monte_carlo.py` — pure functions, no DB/IO:
- `equity_path_from_trade_pcts(pcts, initial) -> np.ndarray` (sequential compounding of equity-relative pcts; docstring states the overlapping-positions approximation).
- `_path_metrics(path, initial, years) -> {final_equity, annualized_return, max_drawdown, calmar}` (running-peak DD, geometric annualization — mirror `results.py` conventions).
- `mc_bootstrap(pcts, initial, n_paths, seed, years=3.0)` — resample len(pcts) with replacement per path (numpy Generator).
- `mc_shuffle(...)` — permutation per path.
- `mc_jitter(pcts, bp_sigma, ...)` — optional ± noise on each pct.
- `drop_k_best(trades, k, initial, years)` — deterministic; returns dropped pcts + metrics without them.
- `summarize_paths(paths, target_annual, dd_limit)` — percentile bands + probabilities.
- `run_monte_carlo(trades, initial, years, cfg) -> dict` — orchestrates methods per cfg `{methods:[...], n_paths, seed, drop_k:[1,2,3], jitter_bp}` and also recomputes `consistent_annual_return`-style consistency per path when `equity_curve` dates allow yearly bucketing (reuse the yearly-return helper from `strategy_fitness.py` once Task 0 of the fitness agent lands — import, don't duplicate).

**Step 4: Run tests** → PASS. **Step 5: Commit** `feat(robustness): monte-carlo core (bootstrap/shuffle/drop-K/jitter) over persisted trades`.

---

### Task 2: RobustnessRun model + migration 025

**Files:**
- Modify: `testplatform/backend/app/models/` (find the module defining `Backtest`; add `RobustnessRun` beside it)
- Create: `testplatform/backend/db_migrate/025_add_robustness_runs.py` (copy the 024 script's structure)
- Test: `testplatform/backend/tests/test_migration_025.py` (mirror `test_migration_022.py` style)

Columns: `id PK, backtest_id FK->backtests, kind ('monte_carlo'|'schedule'), params JSON, results JSON, variant_backtest_ids JSON, status ('pending'|'running'|'completed'|'failed'), error_message, created_at, completed_at`.

Steps: failing migration test (table exists + columns) → migration script + model → run test → commit `feat(robustness): RobustnessRun table (migration 025)`.

---

### Task 3: Robustness handler service

**Files:**
- Create: `testplatform/backend/app/services/robustness_handler.py`
- Test: `testplatform/backend/tests/test_robustness_handler.py`

Behavior:
- `run_monte_carlo_for_backtest(robustness_run_id)`: load parent Backtest row → parse `trades`/`initial_capital`/dates (years from start/end) → `monte_carlo.run_monte_carlo(...)` → write `results` JSON + status on the RobustnessRun. Fail-soft: exceptions → status failed + error_message.
- `launch_schedule_variants(robustness_run_id)`: rebuild the parent's config exactly like the `/rerun` handler does (extract that reconstruction into a shared helper `rebuild_config_for_backtest(bt)` in the rerun handler module and call it from BOTH — do not copy-paste), then per variant override `run_schedule_override` days/times, create a NEW pending Backtest row (`name=f"RBST-{variant}-{bt.name}"`, `engine_type='daily_expert'`, `is_saved=False`), queue each on `get_rerun_task_queue()` with the existing `daily_backtest` task type + config payload, record ids in `variant_backtest_ids`. A collector step (task or lazy on GET) marks the RobustnessRun completed when all variants reach terminal status and snapshots their headline metrics into `results.schedule_summary`.
- Tests with a seeded in-memory DB + a stub queue (assert: MC results persisted; variant rows created with correct schedule overrides; parent never mutated).

Commit: `feat(robustness): handler — MC over saved trades + schedule-variant re-runs via rerun queue`.

---

### Task 4: API endpoints

**Files:**
- Modify: `testplatform/backend/app/api/backtests.py` (append after `/whatif`, ~line 766)
- Test: `testplatform/backend/tests/test_robustness_api.py` (FastAPI TestClient, mirror `test_optimize_route.py`)

Endpoints:
- `POST /api/backtests/robustness` body `{backtest_ids: [int], monte_carlo: {enabled, n_paths=1000, seed=42, methods=["bootstrap","shuffle","drop_k"], drop_k=[1,2,3], jitter_bp=0}, schedule: {enabled, day_variants=true, time_variants=["10:30","12:30","15:00"]}}` → one RobustnessRun per (backtest, kind); MC runs queue on the rerun pool too (cheap but keeps the API snappy); returns run ids.
- `GET /api/backtests/robustness?backtest_id=` and `GET /api/backtests/robustness/{run_id}` → status + results (+ resolved variant rows' headline metrics for schedule kind).
- Guards: 404 unknown backtest; 400 non-`daily_expert` for schedule kind; 400 when `trades` empty for MC.

Commit: `feat(robustness): REST endpoints (launch + poll) for MC and schedule variants`.

---

### Task 5: Fitness-metrics catalog endpoint (UI single source of truth)

**Files:**
- Modify: `testplatform/backend/app/services/strategy_fitness.py` — add `METRICS_CATALOG`: list of `{key, label, description, supports_trade_scale, uses_adjusted_under_caps}` derived from `_FITNESS_KEYS` + specials (`max_drawdown`, `consistent_annual_return`), so a new metric added to the map without catalog metadata fails a unit test (drift guard).
- Modify: `testplatform/backend/app/api/strategies.py` (or `backtests.py` — pick whichever already serves optimization metadata; verify with grep `fitness` in `app/api/`) — `GET /api/optimization/fitness-options` returning the catalog + the cap/scale knob definitions `{profit_cap_pct: {default 2000}, profit_share_cap_pct: {default 25}, fitness_trade_scale: {default false}, fitness_trade_scale_cap: {default 100}}`.
- Test: catalog covers every `_FITNESS_KEYS` entry + specials; endpoint returns them.

Also verify the API optimize route accepts `fitness_metric` + the 4 knobs (the CLI does; if the serve route lacks any, add them to its request model and thread into the job config exactly as `ba2test_launcher` does — grep `profit_share_cap_pct` in `strategy_optimization_handler.py` for the config keys).

Commit: `feat(opt): fitness-metrics catalog endpoint + API optimize route accepts all fitness knobs`.

---

### Task 6: UI — robustness launch + results

**Files:**
- Modify: `testplatform/frontend/src/pages/Backtesting.tsx`
- Create: `testplatform/frontend/src/components/RobustnessDialog.tsx`, `testplatform/frontend/src/components/RobustnessResults.tsx`

Behavior:
- Backtest list: row checkboxes (saved/completed daily_expert rows only) + a "Robustness…" toolbar button (disabled unless ≥1 selected).
- `RobustnessDialog`: toggles + fields mirroring the POST body (MC methods, paths, seed, drop-K; schedule day/time variants); submit → POST → toast with run ids.
- `RobustnessResults` (per backtest, collapsible): MC section — percentile band table (p5/p50/p95 for ann-return / maxDD / calmar), `P(ann ≥ 30%)`, `P(dd ≤ −20%)` badges, drop-K table ("without best 1/2/3 trades: X%/yr"); recharts `ComposedChart` for the ann-return distribution (bar histogram) — reuse the page's existing recharts imports. Schedule section — small table: variant vs ann-return/calmar/dd, parent row highlighted, spread (max−min) badge.
- Poll `GET .../robustness?backtest_id=` while any run is pending (same polling pattern the page already uses for running backtests — grep `setInterval`/`useEffect` polling in the file and reuse).

Manual verification step (no frontend test infra): `ba2-test serve`, run an MC on a saved `-aggr` TOP row, screenshot table vs the agent-validated numbers.

Commit: `feat(ui): robustness dialog + results panels (MC bands, drop-K, schedule-variant spread)`.

---

### Task 7: UI — fitness options block

**Files:**
- Modify: `testplatform/frontend/src/pages/Backtesting.tsx` (optimization form section — locate the existing fitness/metric select via grep `fitness` in the file)

Behavior: replace any hardcoded metric list with the `GET /api/optimization/fitness-options` catalog (label + description tooltip); add inputs for `profit_cap_pct`, `profit_share_cap_pct`, `fitness_trade_scale` (checkbox + cap field, auto-disabled with a hint when the selected metric is `consistent_annual_return` — the catalog's `supports_trade_scale=false` drives this); thread all into the optimize POST.

Commit: `feat(ui): fitness metric catalog + profit-cap/trade-scale knobs in the optimization form`.

---

### Task 8: End-to-end validation + docs

- Backend: full `tests/backtest/` + new suites green (baseline 259+1 skip at plan time).
- Run MC on 3 real saved rows (`TOP1-scr-large-FMPRating-S4-aggr`, `TOP3-scr-mid-FMPRating-S2-aggr`, `TOP1-scr-large-FMPRating-S2-aggr`) — expected shape: S2-mid TOP3's drop-1 return collapses (its best trade ≈ 32% of equity) while S4-large's barely moves; include the three summaries in the PR/commit message body.
- Schedule variants on one row (S4-large TOP1): confirm 4 day-variant rows complete and the spread renders.
- Update `testplatform/docs/` with a short robustness-suite README section.

Commit: `docs(robustness): validation results on real -aggr rows + usage notes`.

---

## Sizing / sequencing notes

- Tasks 1–5 are backend-only and independent of the in-flight fitness agent EXCEPT the consistency-per-path reuse in Task 1 (soft dependency — implement behind a try-import until the fitness commit merges).
- Schedule-variant re-runs are full 3-year backtests (~minutes each on the rerun pool) — the UI must treat them as long-running (status polling), unlike MC (sub-second).
- YAGNI deliberately excluded: manage-schedule perturbation variants, distributed variant execution on remote150, MC over intraday marks (trade-level only), persistence of every MC path (summaries only).

# Backtest Robustness Suite (Monte Carlo + Schedule Perturbation)

Stress-test any saved backtest two ways: **is the equity curve luck?** (trade-level
Monte Carlo) and **is the edge an artifact of *when* we analyze?** (schedule
perturbation). Plan: `docs/plans/2026-07-02-backtest-robustness-suite.md`.

## Components

| Layer | Where |
|---|---|
| Monte Carlo core (pure fns) | `app/services/backtest/monte_carlo.py` |
| `RobustnessRun` table | `app/models/backtest.py` + migration `db_migrate/025_add_robustness_runs.py` |
| Handler (MC + schedule variants) | `app/services/robustness_handler.py` |
| REST API | `app/api/backtests.py` → `/api/backtests/robustness` |
| Fitness catalog | `app/services/strategy_fitness.py` `METRICS_CATALOG` + `GET /api/optimization/fitness-options` |
| UI | `frontend/src/components/RobustnessDialog.tsx`, `RobustnessResults.tsx`, wired into `Backtesting.tsx` |

## Monte Carlo (sub-second, no re-run)

Operates on the persisted `Backtest.trades` (`pnl_pct` = % of account equity),
compounding each trade's pct onto a synthetic equity path (documented
non-overlapping-positions approximation — good enough to *rank* robustness).

- **bootstrap** — resample trades with replacement (is the total return stable?)
- **shuffle** — permute order (same total, different drawdown path)
- **drop-K best** (K=1,2,3) — the "was it one lucky trade?" test
- **jitter** — optional ±bp slippage noise

Outputs per backtest: p5/p25/p50/p75/p95 bands for {annualized_return,
max_drawdown, calmar}, `P(ann ≥ target)`, `P(dd ≤ −limit)`, the drop-K table, and
(when trade dates allow) a `consistent_annual_return` consistency score. Seeded →
deterministic.

## Schedule perturbation (full re-runs, minutes each)

Re-runs the parent's ORIGINAL config with a shifted analysis schedule
(entry weekday Mon..Fri; entry time 09:30 → 10:30/12:30/15:00) via the existing
re-run task queue. Each variant is a NEW `Backtest` row `RBST-<variant>-<parent>`
(`is_saved=False`, `optimization_id=NULL`); **the parent is never mutated**. The
spread across variants shows whether the edge depends on *when* you analyze.

## API

- `POST /api/backtests/robustness` `{backtest_ids, monte_carlo:{...}, schedule:{...}}`
  → `{runs:[{backtest_id, kind, robustness_run_id, status}]}` (MC runs inline;
  schedule variants queue on the re-run pool).
- `GET /api/backtests/robustness?backtest_id=<id>` / `GET .../robustness/{run_id}`.
- Guards: 404 unknown backtest; 400 schedule on non-`daily_expert`; 400 MC with no trades.

## Validation (real saved rows, 3-year phase-1 windows)

| Row | Return | Trades | Bootstrap ann-return p5/p50/p95 | P(ann≥30%) | drop-1/2/3 | Verdict |
|---|---:|---:|---|---:|---|---|
| TOP1-…-S1 | +297% | 307 | 40 / 59 / 82 | 1.00 | 55/52/49 | **robust** (edge survives dropping best trades) |
| TOP5-…-S1 | +79% | 163 | 11 / 22 / 35 | 0.14 | 19/16/14 | moderate |
| TOP3-…-S1 | +31% | 348 | −3 / 10 / 26 | 0.02 | 8/6/5 | **fragile** (drop-K collapses; luck-heavy) |

The MC cleanly discriminates a robust curve (TOP1: high, tight bands, drop-K holds)
from a fragile one (TOP3: wide bands crossing zero, drop-K collapses).

## Tests

`tests/backtest/test_monte_carlo.py`, `tests/test_robustness_handler.py`,
`tests/test_robustness_api.py`, `tests/test_fitness_catalog.py`,
`tests/test_migration_025.py` (29 tests). Full `tests/backtest/` green (300).

## Known follow-ups

- The frontend `npm run build` currently fails on **pre-existing** TS errors in
  `src/lib/btExport.ts` + unrelated `Backtesting.tsx` lines (another in-flight
  session's WIP) — the robustness UI code itself compiles clean. Build turns green
  once those are resolved.
- MC uses trade-level (not intraday) marks and persists summaries only (not every
  path), by design.

# Backtest perf baseline — live↔backtest engine unification (Phase 0)

Plan: `docs/plans/2026-07-02-live-backtest-engine-unification.md`
Captured: 2026-07-07 · host: this macOS dev box · APP_VERSION at capture: 2026.07.869

## Why this baseline exists

The unification lifts the backtest driver loop onto a seam-parameterized
`ba2_common.core.trade_cycle` (Account/Clock/Data/Persistence/Schedule seams). Every seam adds
one layer of indirection to the **enter-decision path** — `TradeActionEvaluator.evaluate(...)
.execute(...)` — which runs once per (expert, symbol, analysis-bar). The plan caps the acceptable
backtest slowdown at **20–30%**. This doc is the concrete anchor that ceiling is measured against.

## The measured anchor — shared enter-decision path

Measured by replaying the golden fixture (live instance 13 = FMPRating, classic-RM, 62 recorded
recs) through the exact backtest enter path, timing-only (logging muted):

| metric | value |
|---|---|
| recs replayed / run | 62 |
| best run | 0.136 s |
| median run | 0.149 s |
| **per-decision (best)** | **2.19 ms** |
| per-decision (median) | 2.40 ms |
| throughput (best) | 457 decisions/s |

Per-decision cost covers: seed `ExpertRecommendation` → `TradeActionEvaluator.evaluate` (ruleset +
`TradeConditions` over 8 ordered tiers) → `execute(submit_to_broker=False)` (buy order + TP/SL
bracket adjust) → order read-back. This is the hot path the seam work touches.

**Ceiling for the unification:** after each phase, re-measure with the tool below. Per-decision
best must stay **≤ 2.19 ms × 1.30 = 2.85 ms** (30% hard cap; target ≤ 2.63 ms = +20%).

### Re-measure command

```bash
cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/perf_baseline.py
```

## Structural perf guarantees (call-count gates, timing-free)

Wall-clock asserts are flaky in CI, so the enforced regression gates count *calls*, not seconds.
These protect the seams that make the backtest fast (see the "5min perf" + "network-cache audit"
notes). Keep them green through every unification phase:

- `tests/backtest/test_backtest_perf_assertions.py`
  - `test_weekly_cadence_reduces_expert_evaluations` — cadence dedup (weekly ≤ daily/2 `analyze_as_of` calls).
  - `test_activity_logging_silenced_during_backtest` — 0 `ActivityLog` enqueues per run.
- `tests/backtest/test_option_run_perf.py::test_option_run_reuses_order_cache_no_per_bar_churn` —
  in-memory order cache invalidated only on EVENT bars (`invalidate_calls < total_bars`).

The perf seams these defend (must NOT be regressed by the seam refactor):
in-memory order cache (62× fewer DB queries), flat-bar skip, no-fill gating, cadence dedup
(78×/day→1×), columnar as-of price store (320× next_bar), hermetic FMP/OHLCV caches,
`frozen_ttl_cache`, activity/file logging disabled.

## Golden parity harness (the evidence channel this phase adds)

- `tools/capture_live_parity_fixture.py` → committed fixture
  `testplatform/backend/tests/backtest/fixtures/live_parity_inst13.json` (read-only capture; no
  live DB or network at test time).
- `app/services/backtest/parity_harness.py` — replays each recorded rec through the backtest
  enter path and pins the decision against the live outcome.
- `tests/backtest/test_parity_golden.py` — the CI assertion.

Current parity (instance 13, FMPRating):

```
POSITIVE (live-funded → BT must fire same side): 12/12 match
NEGATIVE (HOLD → BT must fire nothing):          13/13 match
MEASURED (BUY not funded live):                  20/37 fire in BT
```

- **12/12 + 13/13** = the backtest reproduces 100% of recorded live enter decisions on identical
  inputs (asserted — a regression here is a real shared-engine bug).
- **20/37** BUY recs pass the ruleset but were not funded live — the live *orchestration seam*
  (dedup / equity / capital allocation), i.e. exactly the gap Phases 1–3 close. Measured, not
  asserted, so the harness stays honest about shared-decision parity vs driver-loop divergence.

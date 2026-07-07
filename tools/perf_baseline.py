"""Re-measurable perf baseline for the shared enter-decision path (Phase 0 of
docs/plans/2026-07-02-live-backtest-engine-unification.md).

The unification adds seam indirection (Account/Clock/Data/Persistence) to the SAME per-decision
path this measures — TradeActionEvaluator.evaluate(...).execute(...) — so timing it gives the
concrete anchor for the plan's "<=20-30% BT perf hit" ceiling. Run it before AND after each
unification phase and compare against reports/perf_baseline_2026-07-02.md.

Wall-clock, so it is a TOOL, not a pytest assertion (the suite's perf gate is call-count based in
tests/backtest/test_backtest_perf_assertions.py — timing asserts are flaky in CI). This just prints
numbers for the human-committed baseline doc.

Usage:  cd testplatform/backend && ~/ba2-venvs/test/bin/python ../../tools/perf_baseline.py
"""
from __future__ import annotations

import logging
import os
import statistics
import sys
import time

# Make the testplatform backend importable regardless of cwd (a file script puts its OWN dir on
# sys.path, not the backend dir).
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def main() -> int:
    logging.disable(logging.CRITICAL)  # mute evaluator INFO churn for clean timing
    from app.services.backtest.parity_harness import default_fixture, run_parity

    fixture = default_fixture(13)
    run_parity(fixture)  # warm: imports + seam wiring + first-touch caches
    samples = []
    n = 0
    for _ in range(7):
        t = time.perf_counter()
        rep = run_parity(fixture)
        samples.append(time.perf_counter() - t)
        n = len(rep.rows)

    best = min(samples)
    median = statistics.median(samples)
    print(f"shared enter-decision path baseline (fixture inst13, {n} recs/run):")
    print(f"  runs (s):     {['%.3f' % s for s in samples]}")
    print(f"  best/median:  {best:.3f}s / {median:.3f}s")
    print(f"  per-decision: {best / n * 1000:.2f} ms (best)  |  {median / n * 1000:.2f} ms (median)")
    print(f"  throughput:   {n / best:.0f} decisions/s (best)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

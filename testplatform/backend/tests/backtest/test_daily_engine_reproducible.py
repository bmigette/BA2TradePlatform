"""Phase 2 Task 6 / GATE item 4: reproducibility (the optimizer prerequisite).

The design's contract for the engine layer: *same cache + same params + same seed =>
identical result*. Two independent ``handle_daily_backtest`` runs over the SAME hermetic
fixture cache, with identical payloads (same seed), must produce a BYTE-IDENTICAL equity
curve and IDENTICAL metrics. This is the determinism Phase 5 (the joint GA optimizer)
depends on — a non-deterministic engine would make fitness scores noisy and the search
meaningless.

Determinism comes from: a fixed (no-randomness) provider cache, ``random``/``numpy`` seeded
from ``config["seed"]`` before the loop, the date-ordered bar clock, and the deterministic
fill/sizing/metric math (no wall-clock, no set-iteration-order leakage into numbers).

Covers BOTH clean experts. Equity curves are compared for exact (==) equality, and every
metric column is compared exactly.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_engine_reproducible.py -v
"""
from __future__ import annotations

import pytest

from tests.backtest.fixtures.e2e_support import (
    earnings_drift_payload,
    ensure_host_schema,
    insider_cluster_payload,
    load_backtest,
    new_backtest_row,
    run_daily_backtest,
)


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    ensure_host_schema()
    yield


# The full metric column set that must match exactly across two identical runs.
_METRIC_COLUMNS = (
    "total_trades", "winning_trades", "losing_trades", "win_rate",
    "total_return", "annualized_return", "buy_hold_return",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio", "volatility",
    "max_drawdown", "avg_drawdown", "max_drawdown_duration",
    "profit_factor", "expectancy", "sqn", "avg_trade", "best_trade", "worst_trade",
    "avg_trade_duration", "exposure_time",
    "final_equity", "equity_peak",
)


def _run_twice(make_payload, seed: int):
    id_a = new_backtest_row("repro-a")
    id_b = new_backtest_row("repro-b")
    r1 = run_daily_backtest(make_payload(id_a, seed=seed), task_id="repro-a")
    r2 = run_daily_backtest(make_payload(id_b, seed=seed), task_id="repro-b")
    assert r1["status"] == "completed", r1.get("error")
    assert r2["status"] == "completed", r2.get("error")
    return load_backtest(id_a), load_backtest(id_b)


def _assert_identical(b1, b2):
    # GATE item 4: byte-identical equity curve (exact ==, list of {date, equity}).
    assert b1.equity_curve == b2.equity_curve
    assert b1.drawdown_curve == b2.drawdown_curve
    assert b1.trades == b2.trades

    # Identical metrics, column by column (exact equality — these are deterministic floats).
    for col in _METRIC_COLUMNS:
        v1, v2 = getattr(b1, col), getattr(b2, col)
        assert v1 == v2, f"metric {col} differs: {v1!r} != {v2!r}"


def test_earnings_drift_reproducible():
    """Two identical FMPEarningsDrift runs => byte-identical equity curve + identical metrics."""
    b1, b2 = _run_twice(earnings_drift_payload, seed=7)
    _assert_identical(b1, b2)
    # Sanity: the runs actually did something (not two identical empty runs).
    assert b1.equity_curve and len(b1.equity_curve) >= 1
    assert b1.total_trades >= 1


def test_insider_cluster_reproducible():
    """Two identical FMPInsiderClusterBuy runs => byte-identical equity curve + identical metrics."""
    b1, b2 = _run_twice(insider_cluster_payload, seed=7)
    _assert_identical(b1, b2)
    assert b1.total_trades >= 1


def test_reproducible_across_different_seed_same_data():
    """Same fixed cache + same params but DIFFERENT seeds still match here.

    The clean experts + notional sizing + the deterministic fixture make NO use of the RNG
    (no stochastic tie-breaking / sampling), so the seed does not perturb the result — a run
    is reproducible regardless of seed for this expert set. (The seed seam still exists for
    any future stochastic component; this asserts the current pipeline is seed-invariant.)
    """
    id_a = new_backtest_row("repro-seed-a")
    id_b = new_backtest_row("repro-seed-b")
    r1 = run_daily_backtest(earnings_drift_payload(id_a, seed=1), task_id="repro-s1")
    r2 = run_daily_backtest(earnings_drift_payload(id_b, seed=999), task_id="repro-s2")
    assert r1["status"] == "completed" and r2["status"] == "completed"
    b1, b2 = load_backtest(id_a), load_backtest(id_b)
    assert b1.equity_curve == b2.equity_curve
    assert b1.total_return == b2.total_return
    assert b1.sharpe_ratio == b2.sharpe_ratio

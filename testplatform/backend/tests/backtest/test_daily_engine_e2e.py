"""Phase 2 Task 6 / GATE item 3: end-to-end clean-expert daily backtests.

Drives the REAL ``handle_daily_backtest`` handler — the full packaged decision/order path
(``expert.analyze_as_of`` -> ``ExpertRecommendation`` -> ``TradeActionEvaluator`` enter
ruleset -> classic ``TradeRiskManagement`` notional sizing -> ``BacktestAccount.submit_order``
-> next-bar fill -> per-bar equity snapshot -> ``results.build_results`` -> persisted
``Backtest`` row) — against a FIXED, hermetic provider cache (``fixtures/hermetic_providers``).
No network, no FMP key, no wall-clock dependence.

Both CLEAN (no-LLM) experts are covered (the two the plan ships first):
  * FMPEarningsDrift     — a planted +20% EPS surprise (2024-01-15) -> a fresh-drift BUY.
  * FMPInsiderClusterBuy — a planted 3-insider, $300k open-market cluster (2024-01-12..18).

The fixture trades a Jan-19..Feb-23 window AFTER the planted signals, with the decision
windows (``max_days_since_report`` / ``lookback_days``) set wide so the signal stays live the
whole run -> AAPL opens once (the enter ruleset's HasNoPosition guard blocks re-entry) and is
held the rest of the window (a clean winning open position); MSFT (no signal) never trades.

GATE assertions (the plan's e2e contract): the stored row is ``completed`` with sane,
FINITE metrics (total_return / sharpe / max_drawdown / win_rate / profit_factor all non-NaN),
a non-empty equity curve, and the profit-factor cap honoured.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_engine_e2e.py -v
"""
from __future__ import annotations

import math

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
    """Ensure the host ``backtests`` table exists on the default engine."""
    ensure_host_schema()
    yield


def _assert_completed_with_sane_metrics(backtest_id: int):
    bt = load_backtest(backtest_id)
    assert bt.status == "completed", f"status={bt.status} error={bt.error_message}"

    # GATE item 3: every headline metric is non-None and FINITE (no NaN/Inf reaches the DB).
    for label, value in (
        ("total_return", bt.total_return),
        ("sharpe_ratio", bt.sharpe_ratio),
        ("max_drawdown", bt.max_drawdown),
        ("win_rate", bt.win_rate),
        ("profit_factor", bt.profit_factor),
    ):
        assert value is not None, f"{label} is None"
        assert math.isfinite(value), f"{label} is not finite: {value}"

    # The profit-factor cap is honoured (never an uncapped Inf).
    assert bt.profit_factor <= 999.99

    # A non-empty equity curve, one point per simulated trading bar (26 in the fixture window).
    assert bt.equity_curve, "equity_curve is empty"
    assert len(bt.equity_curve) >= 1
    # Each point carries an ISO date + a finite equity value.
    for pt in bt.equity_curve:
        assert "date" in pt and "equity" in pt
        assert math.isfinite(pt["equity"])

    # The drawdown curve mirrors the equity curve length (one point per bar).
    assert bt.drawdown_curve and len(bt.drawdown_curve) == len(bt.equity_curve)

    # final_equity / equity_peak are populated + finite.
    assert bt.final_equity is not None and math.isfinite(bt.final_equity)
    assert bt.equity_peak is not None and math.isfinite(bt.equity_peak)
    return bt


def test_earnings_drift_e2e_produces_completed_backtest():
    """FMPEarningsDrift over the fixed cache -> a completed Backtest row with sane metrics."""
    bt_id = new_backtest_row("e2e-earnings-drift")
    result = run_daily_backtest(earnings_drift_payload(bt_id, seed=42), task_id="e2e-ed")
    assert result["status"] == "completed", result.get("error")
    assert result["backtest_id"] == bt_id

    bt = _assert_completed_with_sane_metrics(bt_id)
    # The planted fresh-surprise BUY opens AAPL exactly once (re-entry blocked by the guard)
    # and the monotone-up fixture makes the held position a winner.
    assert bt.total_trades >= 1
    assert bt.total_return > 0.0, "the monotone-up fixture should yield a positive return"
    # The held position is in the money, so net liquidating value ends above the start cash.
    assert bt.final_equity > 100_000.0


def test_insider_cluster_e2e_produces_completed_backtest():
    """FMPInsiderClusterBuy over the fixed cache -> a completed Backtest row with sane metrics."""
    bt_id = new_backtest_row("e2e-insider-cluster")
    result = run_daily_backtest(insider_cluster_payload(bt_id, seed=42), task_id="e2e-ic")
    assert result["status"] == "completed", result.get("error")
    assert result["backtest_id"] == bt_id

    bt = _assert_completed_with_sane_metrics(bt_id)
    assert bt.total_trades >= 1
    assert bt.total_return > 0.0


def test_e2e_only_signalled_symbol_trades():
    """The universe is multi-asset (AAPL + MSFT) but ONLY the planted-signal symbol trades.

    MSFT has no earnings surprise / no insider cluster in the fixture, so it stays HOLD the
    whole run -> every filled trade is AAPL (proves per-symbol decisioning + the universe
    filter, not a blanket buy-everything).
    """
    bt_id = new_backtest_row("e2e-universe-filter")
    result = run_daily_backtest(earnings_drift_payload(bt_id, seed=42), task_id="e2e-uf")
    assert result["status"] == "completed", result.get("error")

    bt = load_backtest(bt_id)
    symbols_traded = {t["symbol"] for t in (bt.trades or [])}
    assert symbols_traded == {"AAPL"}, f"expected only AAPL to trade, got {symbols_traded}"

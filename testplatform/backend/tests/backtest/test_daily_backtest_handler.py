"""Phase 2 Task 5: ``daily_backtest`` handler + ``Backtest`` persistence (hermetic).

Proves the handler's control flow + persistence WITHOUT network/real-experts (that full
end-to-end run is Task 6's gate):
  * payload validation fails early on a missing required key (no-defaults rule);
  * ``_build_config`` assembles the account_settings + rejects a bad date / unknown expert;
  * a successful run (``run_daily_backtest`` monkeypatched to a known results blob) flips the
    ``Backtest`` row to ``completed`` and writes every metric column + the equity/drawdown/
    trades JSON blobs;
  * an engine failure flips the row to ``failed`` with an error_message;
  * the handler is registered on the TaskQueueService under ``daily_backtest``.

The ``Backtest`` RESULTS row lives in the host ``SessionLocal`` DB (created here on the default
engine, as the other backend tests do).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_backtest_handler.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.backtest import Backtest
from app.services.backtest import daily_backtest_handler as H


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    """Ensure the host ``backtests`` table exists on the default engine."""
    Base.metadata.create_all(bind=engine)
    yield


def _new_backtest_row(name="daily-test") -> int:
    db = SessionLocal()
    try:
        bt = Backtest(
            name=name,
            model_id=None,
            start_date=datetime(2024, 1, 2),
            end_date=datetime(2024, 1, 8),
            initial_capital=100_000.0,
            commission=1.0,
            slippage=0.0,
            status="pending",
        )
        db.add(bt)
        db.commit()
        db.refresh(bt)
        return bt.id
    finally:
        db.close()


def _payload(backtest_id: int, **over):
    p = {
        "backtest_id": backtest_id,
        "name": "daily-test",
        "enabled_instruments": ["AAPL"],
        "experts": ["FMPEarningsDrift"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_capital": 100_000.0,
        "commission": 1.0,
        "slippage": 0.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    p.update(over)
    return p


# A known results blob (the shape build_results emits) used to assert persistence.
_RESULTS = {
    "total_trades": 3, "winning_trades": 2, "losing_trades": 1, "win_rate": 66.67,
    "total_return": 5.0, "annualized_return": 12.3, "buy_hold_return": 0.0,
    "sharpe_ratio": 1.1, "sortino_ratio": 1.4, "calmar_ratio": 0.9, "volatility": 8.2,
    "max_drawdown": -4.55, "avg_drawdown": -2.1, "max_drawdown_duration": 1.0,
    "profit_factor": 4.0, "expectancy": 2.0, "sqn": 0.7, "avg_trade": 2.0,
    "best_trade": 5.0, "worst_trade": -2.0,
    "avg_trade_duration": 2.0, "exposure_time": 33.3,
    "final_equity": 105_000.0, "equity_peak": 110_000.0,
    "equity_curve": [{"date": "2024-01-02", "equity": 100_000.0},
                     {"date": "2024-01-04", "equity": 105_000.0}],
    "drawdown_curve": [{"date": "2024-01-02", "drawdown": 0.0},
                       {"date": "2024-01-04", "drawdown": -4.55}],
    "trades": [{"symbol": "AAPL", "entry_time": "2024-01-02", "exit_time": None,
                "direction": "buy", "entry_price": 100.0, "exit_price": 0.0,
                "size": 10.0, "pnl": 500.0, "pnl_pct": 5.0, "bars_held": 2,
                "exit_reason": "unknown"}],
}


# ---------------------------------------------------------------------------
# Validation + config
# ---------------------------------------------------------------------------
def test_missing_required_key_fails_early():
    p = _payload(123)
    del p["seed"]
    out = H.handle_daily_backtest("t-val", p)
    assert out["status"] == "failed"
    assert "seed" in out["error"]


def test_build_config_assembles_account_settings():
    cfg = H._build_config(_payload(1))
    assert cfg["account_settings"] == {
        "starting_cash": 100_000.0,
        "commission_per_trade": 1.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
        "spread_bps": 0.0,  # optional, defaults to 0.0 (exact no-op) when absent from payload
        # OPTION spread model (2026-07-25) — also optional and also 0.0-by-default, so an
        # existing payload reproduces pre-model fills bit-for-bit. The grid CLI passes real
        # values (--option-spread-pct defaults to 5.0 there); the handler itself never
        # invents one. Percent OF PREMIUM, not bps of price — see
        # BacktestAccount._option_half_spread for why options cannot share spread_bps.
        "option_spread_pct": 0.0,
        "option_spread_min_tick": 0.0,
        # Optional FIXED-notional equity ceiling (2026-08-25). None = OFF, which is what an
        # absent payload key means — never 0.0, which would make every position unaffordable.
        # See app/services/backtest/equity_cap.py.
        "equity_cap": None,
    }
    assert cfg["enabled_instruments"] == ["AAPL"]
    assert cfg["initial_capital"] == 100_000.0
    assert isinstance(cfg["start_date"], datetime)


def test_build_config_rejects_unknown_expert():
    with pytest.raises(ValueError, match="unsupported expert"):
        H._build_config(_payload(1, experts=["NotARealExpert"]))


def test_build_config_rejects_bad_date_order():
    with pytest.raises(ValueError, match="on or after"):
        H._build_config(_payload(1, start_date="2024-02-01", end_date="2024-01-01"))


# ---------------------------------------------------------------------------
# Entry-time TP/SL bracket (entry_rules) — Task 6. Replaces the deleted
# initial_tp_percent/initial_sl_percent/initial_tp_reference/initial_tp_ref scalar-field
# mechanism entirely: the bracket now rides on a rule list, the SAME shape exit_rules uses,
# forwarded unchanged (mirrors exit_rules' own forwarding immediately above in _build_config).
# ---------------------------------------------------------------------------
def test_build_config_forwards_entry_rules():
    """``entry_rules`` flows through to the engine config unchanged."""
    entry_rules = [
        {"id": "e_tp", "action_type": "adjust_take_profit",
         "reference_value": "expert_target_price", "action_value": 10.0},
    ]
    cfg = H._build_config(_payload(1, entry_rules=entry_rules))
    assert cfg["entry_rules"] == entry_rules


def test_build_config_entry_rules_absent_is_none():
    """No entry_rules supplied -> None (no entry-time bracket for this run)."""
    cfg = H._build_config(_payload(1))
    assert cfg.get("entry_rules") is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_persist_results_writes_all_columns():
    bt_id = _new_backtest_row("persist-test")
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        H._persist_results(db, bt, _RESULTS)
        db.commit()
        db.refresh(bt)

        assert bt.total_trades == 3
        assert bt.total_return == 5.0
        assert bt.sharpe_ratio == 1.1
        assert bt.max_drawdown == -4.55
        assert bt.profit_factor == 4.0
        assert bt.final_equity == 105_000.0
        assert bt.equity_peak == 110_000.0
        assert bt.win_rate == 66.67
        assert bt.equity_curve == _RESULTS["equity_curve"]
        assert bt.drawdown_curve == _RESULTS["drawdown_curve"]
        assert bt.trades == _RESULTS["trades"]
        # results blob carries the scalar metrics but NOT the curves/trades.
        assert "equity_curve" not in bt.results
        assert bt.results["total_return"] == 5.0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Full handler control flow (engine monkeypatched)
# ---------------------------------------------------------------------------
def test_handler_completed_persists_metrics(monkeypatch):
    bt_id = _new_backtest_row("complete-test")
    monkeypatch.setattr(H, "run_daily_backtest", lambda config, progress_cb=None: dict(_RESULTS))

    out = H.handle_daily_backtest("t-ok", _payload(bt_id))
    assert out["status"] == "completed"
    assert out["backtest_id"] == bt_id

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        assert bt.status == "completed"
        assert bt.started_at is not None
        assert bt.completed_at is not None
        assert bt.total_return == 5.0
        assert bt.equity_curve and len(bt.equity_curve) == 2
        import math
        for m in (bt.total_return, bt.sharpe_ratio, bt.max_drawdown, bt.win_rate, bt.profit_factor):
            assert m is not None and math.isfinite(m)
        assert bt.profit_factor <= 999.99
    finally:
        db.close()


def test_handler_engine_failure_marks_row_failed(monkeypatch):
    bt_id = _new_backtest_row("fail-test")

    def _boom(config, progress_cb=None):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(H, "run_daily_backtest", _boom)
    out = H.handle_daily_backtest("t-fail", _payload(bt_id))
    assert out["status"] == "failed"
    assert "engine exploded" in out["error"]

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        assert bt.status == "failed"
        assert "engine exploded" in (bt.error_message or "")
    finally:
        db.close()


def test_handler_missing_backtest_row():
    out = H.handle_daily_backtest("t-nope", _payload(999_999))
    assert out["status"] == "failed"
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_handler_registers_on_task_queue():
    from app.services.task_queue import TaskQueueService

    tq = TaskQueueService(max_workers=1)
    tq.register_handler("daily_backtest", H.handle_daily_backtest)
    assert "daily_backtest" in tq._handlers
    assert tq._handlers["daily_backtest"] is H.handle_daily_backtest

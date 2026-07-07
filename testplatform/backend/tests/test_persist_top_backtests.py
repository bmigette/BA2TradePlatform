"""Task 6 — top-N backtest sync: _persist_top_backtests must push each persisted TOP-N
Backtest row to synced remote workers via sync_client.push_backtest (not just create it).

``_persist_top_backtests`` (and its nested ``_persist_one``) import ``push_backtest`` and
``_persist_trial_worker`` via FUNCTION-LOCAL ``from ... import ...`` statements (matching the
existing style of that function's other ``app.services.*`` imports), executed at CALL time.
So monkeypatching must target the ORIGIN modules (``app.services.sync_client.push_backtest`` /
``app.services.strategy_optimization_handler._persist_trial_worker``) — patching attributes on
the loaded ``ba2test_launcher`` module itself (``mod.push_backtest``) would be a no-op (that
name is never bound at module scope) and would in fact raise AttributeError since the module
doesn't have that attribute in the first place.
"""
import importlib.util
import os
import sys

# Load the launcher module by path (it lives at testplatform/ba2test_launcher.py), same
# pattern as test_option_strategy_builders.py.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as _soh
from app.services import sync_client as _sync_client


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    """Ensure the host tables exist on the default engine (same pattern as
    test_strategy_optimization_handler.py's ``_host_db``) — SessionLocal()/engine here is the
    conftest-isolated throwaway sqlite, but its tables are never created unless something calls
    create_all; without this fixture every db.add/commit below fails with
    ``OperationalError: no such table``."""
    Base.metadata.create_all(bind=engine)
    yield


# A full results blob shape (mirrors tests/backtest/test_daily_backtest_handler.py's _RESULTS) —
# _persist_results reads several REQUIRED keys (total_trades, winning_trades, losing_trades,
# win_rate, total_return, sharpe_ratio, max_drawdown, profit_factor, avg_trade_duration,
# final_equity, equity_curve, drawdown_curve, trades) and KeyErrors on a thin dict.
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


def _seed_opt_for_top_n():
    db = SessionLocal()
    try:
        strat = Strategy(name="top-n-strat")
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="top-n-opt",
            fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={
                "backtest": {
                    # _build_daily_trial_config requires all of these (see
                    # tests/test_options_optimization_wiring.py's _backtest_cfg for the same
                    # minimal-required-fields template) — start/end/initial_capital ALONE
                    # (the plan draft's original guess) is not enough; it KeyErrors on
                    # backtest_id/experts/enabled_instruments/account_settings/warmup_days/seed.
                    "backtest_id": 1,
                    "start_date": "2024-01-01", "end_date": "2024-06-01",
                    "enabled_instruments": ["AAPL"],
                    "experts": [{"class": "FMPRating", "settings": {}}],
                    "initial_capital": 10000.0,
                    "account_settings": {"starting_cash": 10000.0},
                    "warmup_days": 30,
                    "seed": 42,
                }
            },
            all_results=[{"params": {}, "fitness": 1.23, "trades": 10}],
            best_params={}, best_fitness=1.23, status="completed",
        )
        db.add(opt); db.commit(); db.refresh(opt)
        return opt.id
    finally:
        db.close()


def test_persist_one_pushes_backtest_after_save(monkeypatch):
    opt_id = _seed_opt_for_top_n()
    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)

    assert persisted == 1
    assert len(calls) == 1
    assert calls[0] == "TOP1-top-n-opt"

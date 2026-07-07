"""Task 8 — CLI --save path: ``_persist_tracked`` must push a saved Backtest to synced
workers, and a ``--track``-only run (is_saved stays False) must NOT push.

``_persist_tracked`` calls ``push_backtest`` via a MODULE-LEVEL import
(``from app.services.sync_client import push_backtest`` at the top of
scripts/run_daily_backtest.py, per Task 8's wiring), so — unlike Task 6's
``_persist_top_backtests`` (which imports ``push_backtest`` function-locally and must be
patched on ``app.services.sync_client`` itself) — the patch target here is the attribute on
the LOADED ``run_daily_backtest`` module, ``script.push_backtest``. Patching
``app.services.sync_client.push_backtest`` instead would be a no-op: the name is already
bound in the script's own module globals at import time.

``_persist_tracked`` calls ``daily_backtest_handler._persist_results``, which reads ~15+
REQUIRED keys off the results blob (total_trades, winning_trades, losing_trades, win_rate,
total_return, sharpe_ratio, max_drawdown, profit_factor, avg_trade_duration, final_equity,
equity_curve, drawdown_curve, trades) and KeyErrors on a thin dict — so this uses the full
known-good ``_RESULTS`` fixture (same shape as
tests/backtest/test_daily_backtest_handler.py's module-level ``_RESULTS``), not a
two-key stub.
"""
import importlib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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


def _config(name: str) -> dict:
    # Mirrors the shape _build_real_config actually produces: start_date/end_date are
    # datetime OBJECTS (datetime.fromisoformat), not raw ISO strings — the Backtest model's
    # columns are SQLite DateTime, which reject plain strings.
    return {
        "name": name,
        "account_settings": {"commission_per_trade": 0.0, "slippage_bps": 0.0},
        "experts": [{"class": "FMPRating"}],
        "start_date": datetime.fromisoformat("2024-01-01"),
        "end_date": datetime.fromisoformat("2024-01-31"),
        "initial_capital": 10000.0,
    }


def test_persist_tracked_pushes_when_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli_save.sqlite'}")
    import app.models.database as dbmod
    importlib.reload(dbmod)
    # Reloading app.models.database builds a brand-new (EMPTY) Base/metadata -- Backtest and
    # every other model class stay bound to the ORIGINAL Base from whenever app.models was
    # first imported this session, so `_persist_tracked`'s own `init_db()` call (which does
    # `Base.metadata.create_all()` against the freshly reloaded, empty Base) is a no-op and
    # would leave this fresh sqlite file with no tables. Pre-create the schema through
    # `Backtest.metadata` instead -- it IS the original, fully-populated metadata (every model
    # shares one MetaData object), so this creates all tables against the new engine. Mirrors
    # `_ensure_tables`'s `Strategy.metadata.create_all(bind=engine)` in app/worker_server.py.
    from app.models.backtest import Backtest
    Backtest.metadata.create_all(bind=dbmod.engine)
    import scripts.run_daily_backtest as script

    calls = []
    monkeypatch.setattr(script, "push_backtest", lambda bt, db: calls.append(bt.is_saved))

    bt_id = script._persist_tracked(_config("cli-saved-run"), dict(_RESULTS), saved=True)

    assert calls == [True]
    assert bt_id is not None


def test_persist_tracked_does_not_push_when_not_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli_track_only.sqlite'}")
    import app.models.database as dbmod
    importlib.reload(dbmod)
    # See test_persist_tracked_pushes_when_saved for why this pre-create is required after
    # reloading app.models.database.
    from app.models.backtest import Backtest
    Backtest.metadata.create_all(bind=dbmod.engine)
    import scripts.run_daily_backtest as script

    calls = []
    monkeypatch.setattr(script, "push_backtest", lambda bt, db: calls.append(bt.is_saved))

    bt_id = script._persist_tracked(_config("cli-tracked-only-run"), dict(_RESULTS), saved=False)

    assert calls == []
    assert bt_id is not None

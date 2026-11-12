"""Backtest -> self-contained HTML export (renderer, API route, manual script)."""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _curves(n=30):
    start = datetime(2025, 1, 2)
    eq, dd, peak = [], [], 10000.0
    for i in range(n):
        v = 10000 + i * 40 - (300 if 12 <= i <= 15 else 0)
        peak = max(peak, v)
        d = (start + timedelta(days=i)).isoformat()
        eq.append({"date": d, "equity": float(v)})
        dd.append({"date": d, "drawdown": (v - peak) / peak * 100})
    return eq, dd


TRADES = [
    {"symbol": "AAPL", "direction": "buy", "size": 10, "entry_time": "2025-01-03T14:30:00", "exit_time": "2025-01-10T20:00:00",
     "entry_price": 100.0, "exit_price": 110.0, "pnl": 100.0, "pnl_pct": 1.0, "bars_held": 5, "exit_reason": "take_profit"},
    {"symbol": "MSFT", "direction": "sell", "size": 5, "entry_time": "2025-01-05T14:30:00", "exit_time": "2025-01-08T20:00:00",
     "entry_price": 200.0, "exit_price": 210.0, "pnl": -50.0, "pnl_pct": -0.5, "bars_held": 3, "exit_reason": "stop_loss"},
    {"symbol": "ACN", "underlying_symbol": "ACN", "contract_symbol": "ACN250620C00095000", "option_type": "call", "strike": 95.0,
     "expiry": "2025-06-20", "multiplier": 100, "direction": "buy", "size": 1, "transaction_id": 7,
     "entry_time": "2025-01-06T14:30:00", "exit_time": "2025-01-12T20:00:00", "entry_price": 8.0, "exit_price": 13.0,
     "pnl": 500.0, "pnl_pct": 5.0, "bars_held": 4, "exit_reason": "take_profit"},
    {"symbol": "ACN", "underlying_symbol": "ACN", "contract_symbol": "ACN250620C00105000", "option_type": "call", "strike": 105.0,
     "expiry": "2025-06-20", "multiplier": 100, "direction": "sell", "size": 1, "transaction_id": 7,
     "entry_time": "2025-01-06T14:30:00", "exit_time": "2025-01-12T20:00:00", "entry_price": 2.0, "exit_price": 3.0,
     "pnl": -100.0, "pnl_pct": -1.0, "bars_held": 4, "exit_reason": "take_profit"},
]

STRATEGY_PARAMS = {
    "strategyName": "demo-strat",
    "entryRules": [{"name": "Gate", "conditions": {"operator": "AND", "conditions": [{"field": "rating", "comparison": "gte", "value": 4}]},
                    "actions": [{"action_type": "buy"}, {"action_type": "adjust_take_profit", "action_value": 12.0, "reference_value": "order_open_price"},
                                {"action_type": "adjust_stop_loss", "action_value": -6.0, "reference_value": "order_open_price"}]}],
    "exitRules": [{"conditions": {"operator": "OR", "conditions": [{"field": "has_position"}]}, "actions": [{"action_type": "close"}]}],
    "exit:r1:a0:action_value": 3.5,
}


@pytest.fixture
def backtest(db):
    from app.models.backtest import Backtest

    eq, dd = _curves()
    bt = Backtest(
        name="html <demo>", expert_name="FMPRating", engine_type="daily_expert", status="completed",
        start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1), initial_capital=10000.0,
        trades=TRADES, equity_curve=eq, drawdown_curve=dd, strategy_params=STRATEGY_PARAMS,
        total_return=11.6, sharpe_ratio=1.87, max_drawdown=-2.5, win_rate=50.0, profit_factor=2.4,
        total_trades=4, best_trade=5.0, worst_trade=-1.0, description="my notes", labels=["alpha"],
        final_equity=11160.0, annualized_return=30.0,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


def _render(bt):
    from app.services.backtest.html_export import render_backtest_html
    return render_backtest_html(bt)


def test_contains_every_chart_table_and_metric(backtest):
    out = _render(backtest)
    for chart in ("c-equity", "c-dd", "c-cap", "c-yearly"):
        assert f'id="{chart}"' in out
    assert out.count("<svg") >= 4
    for tab in ("Equity Curve", "Drawdown", "Capital Used", "Trade List (4)", "Strategy", "Yearly Breakdown"):
        assert tab in out
    # headline metrics as the page formats them
    assert "+11.6%" in out and "1.87" in out and "-2.5%" in out and "50.0%" in out
    # header
    assert f"Backtest #{backtest.id}" in out and "FMPRating" in out and "2025-01-01" in out and "Exported:" in out
    assert "html &lt;demo&gt;" in out and "<demo>" not in out  # escaped
    assert "my notes" in out and "alpha" in out
    # trades: all rows, option structure folded into one parent + 2 legs
    assert out.count("<tbody data-pnl") == 3
    assert "2 legs" in out and "C 95.0" in out and "AAPL" in out and "MSFT" in out
    # strategy tab
    assert "demo-strat" in out and "+12%" in out and "-6%" in out and "Move take profit" in out
    assert "exit:r1:a0:action_value" in out
    # yearly + config + all-metrics tables
    assert "2025" in out and "Run configuration" in out and "All metrics" in out and "Annualized return" in out


def test_yearly_numbers_come_from_backend_function(backtest):
    from app.services.backtest.results import yearly_breakdown
    y = yearly_breakdown(backtest.equity_curve, backtest.drawdown_curve, backtest.trades)[0]
    assert f'{y["returnPct"]:+.1f}%' in _render(backtest)


def test_no_external_references(backtest):
    out = _render(backtest)
    assert not re.search(r'<script[^>]+src\s*=', out, re.I)
    assert not re.search(r'<link[^>]+href\s*=', out, re.I)
    assert not re.search(r'@import|url\(\s*["\']?https?:', out, re.I)
    assert not re.search(r'(src|href)\s*=\s*["\']?(https?:)?//', out, re.I)
    assert "fetch(" not in out and "XMLHttpRequest" not in out


def test_empty_run_renders(db):
    from app.models.backtest import Backtest
    bt = Backtest(name="empty", status="completed", engine_type="daily_expert",
                  start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1))
    db.add(bt)
    db.commit()
    out = _render(bt)
    assert "No trades." in out and "No equity curve" in out


def test_api_route_downloads_attachment(client, backtest):
    r = client.get(f"/api/backtests/{backtest.id}/export/html")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == f"attachment; filename=backtest-{backtest.id}.html"
    assert r.headers["content-type"].startswith("text/html")
    assert 'id="c-equity"' in r.text
    assert client.get("/api/backtests/99999999/export/html").status_code == 404


def test_api_route_refuses_running(client, db):
    from app.models.backtest import Backtest
    bt = Backtest(name="r", status="running", engine_type="daily_expert",
                  start_date=datetime(2025, 1, 1), end_date=datetime(2025, 2, 1))
    db.add(bt)
    db.commit()
    assert client.get(f"/api/backtests/{bt.id}/export/html").status_code == 409


def test_manual_script(tmp_path):
    """Seed a throwaway DB file, run the CLI against it, check output + that the DB is untouched."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.backtest import Backtest
    from app.models.database import Base
    import app.models  # noqa: F401

    dbf = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{dbf}")
    Base.metadata.create_all(eng)
    eq, dd = _curves()
    s = sessionmaker(bind=eng)()
    s.add(Backtest(name="cli", status="completed", engine_type="daily_expert", start_date=datetime(2025, 1, 1),
                   end_date=datetime(2025, 2, 1), trades=TRADES, equity_curve=eq, drawdown_curve=dd))
    s.commit()
    bid = s.query(Backtest).first().id
    s.close()
    eng.dispose()
    before = dbf.read_bytes()

    outdir = tmp_path / "out"
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "export_backtest_html.py"), str(bid), "--db", str(dbf),
                          "--out", str(outdir)], capture_output=True, text=True, cwd=str(tmp_path))
    assert res.returncode == 0, res.stderr
    target = outdir / f"backtest-{bid}.html"
    assert str(target) in res.stdout
    assert 'id="c-equity"' in target.read_text(encoding="utf-8")
    assert dbf.read_bytes() == before

    res = subprocess.run([sys.executable, str(ROOT / "tools" / "export_backtest_html.py"), "424242", "--db", str(dbf),
                          "--out", str(outdir)], capture_output=True, text=True, cwd=str(tmp_path))
    assert res.returncode == 1

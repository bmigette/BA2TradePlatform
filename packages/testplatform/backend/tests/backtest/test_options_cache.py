# backend/tests/backtest/test_options_cache.py
from datetime import date
from app.services.backtest.options_cache import OptionsHistoryCache, OptionsCacheMiss
import pytest

def test_write_then_read_chain_and_bar(tmp_path):
    db = str(tmp_path / "opt.db")
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [{
        "occ_symbol": "AAPL240315C00180000", "option_type": "call", "strike": 180.0,
        "expiry": "2024-03-15", "bid": 2.0, "ask": 2.1, "last": 2.05, "iv": 0.25,
        "delta": 0.5, "gamma": 0.01, "theta": -0.03, "vega": 0.1, "open_interest": 1000, "volume": 50,
    }])
    c.write_bar_rows([{
        "occ_symbol": "AAPL240315C00180000", "date": "2024-03-04", "open": 2.1, "high": 2.4,
        "low": 2.0, "close": 2.3, "volume": 120, "underlying": "AAPL",
        "option_type": "call", "strike": 180.0, "expiry": "2024-03-15",
    }])
    rows = c.read_chain("AAPL", "2024-03-01")
    assert len(rows) == 1 and rows[0]["occ_symbol"] == "AAPL240315C00180000"
    bar = c.read_bar("AAPL240315C00180000", "2024-03-04")
    assert bar["close"] == 2.3

def test_missing_chain_raises(tmp_path):
    c = OptionsHistoryCache(str(tmp_path / "opt.db"))
    with pytest.raises(OptionsCacheMiss):
        c.read_chain_or_miss("AAPL", "2024-03-01")

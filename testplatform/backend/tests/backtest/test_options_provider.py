# backend/tests/backtest/test_options_provider.py
from datetime import date
from app.services.backtest.options_cache import OptionsHistoryCache
from app.services.backtest.options_provider import HistoricalOptionsProvider
from ba2_common.core.types import OptionRight

def _seed(db):
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol":"AAPL240315C00180000","option_type":"call","strike":180.0,"expiry":"2024-03-15",
         "bid":2.0,"ask":2.1,"last":2.05,"iv":0.25,"delta":0.5,"gamma":0.01,"theta":-0.03,"vega":0.1,
         "open_interest":1000,"volume":50},
        {"occ_symbol":"AAPL240315P00180000","option_type":"put","strike":180.0,"expiry":"2024-03-15",
         "bid":1.8,"ask":1.9,"last":1.85,"iv":0.27,"delta":-0.5,"gamma":0.01,"theta":-0.03,"vega":0.1,
         "open_interest":900,"volume":40}])
    c.write_bar_rows([{"occ_symbol":"AAPL240315C00180000","date":"2024-03-05","open":2.1,"high":2.4,
        "low":2.0,"close":2.3,"volume":120,"underlying":"AAPL","option_type":"call","strike":180.0,
        "expiry":"2024-03-15"}])
    return c

def test_chain_filtered_by_type_and_asof_clamp(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    calls = p.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024,3,1),
                        expiry_max=date(2024,3,31), option_type=OptionRight.CALL)
    assert len(calls) == 1 and calls[0].option_type == OptionRight.CALL
    assert calls[0].delta == 0.5

def test_chain_before_any_snapshot_is_empty(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    assert p.get_chain("AAPL", date(2024,2,1), expiry_min=date(2024,3,1),
                       expiry_max=date(2024,3,31)) == []

def test_get_bar_asof(tmp_path):
    db = str(tmp_path / "opt.db"); _seed(db)
    p = HistoricalOptionsProvider(db)
    assert p.get_bar("AAPL240315C00180000", date(2024,3,5))["close"] == 2.3

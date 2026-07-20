# backend/tests/backtest/test_options_provider.py
from datetime import date
import app.services.backtest.options_provider as op
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


# ---------------------------------------------------------------------------
# 2026-07-20 fix: worker-process-level cache. HistoricalOptionsProvider is rebuilt fresh once
# per GA trial from the SAME shared cache file — before this fix each rebuild re-hit sqlite on
# every read. These regression tests assert the load-from-disk path only fires ONCE per
# (db_path, underlying)/(db_path, occ_symbol), even across many provider instances.
# ---------------------------------------------------------------------------
def test_second_provider_reuses_worker_cache_no_reload(monkeypatch, tmp_path):
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed(db)

    calls = {"chain": 0, "bar": 0}
    real_load_chain = op._load_chain_history
    real_load_bar = op._load_bar_history

    def counting_load_chain(db_path, underlying):
        calls["chain"] += 1
        return real_load_chain(db_path, underlying)

    def counting_load_bar(db_path, occ_symbol):
        calls["bar"] += 1
        return real_load_bar(db_path, occ_symbol)

    monkeypatch.setattr(op, "_load_chain_history", counting_load_chain)
    monkeypatch.setattr(op, "_load_bar_history", counting_load_bar)

    p1 = HistoricalOptionsProvider(db)
    p1.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024, 3, 1), expiry_max=date(2024, 3, 31))
    assert calls["chain"] == 1
    assert calls["bar"] >= 1  # loaded the as-of greeks bar history for each contract in the chain

    # A SECOND provider (mimics a fresh GA trial rebuilding HistoricalOptionsProvider from the
    # same cache_db) reading the SAME underlying/contracts must reuse the worker-level cache
    # rather than reload from disk.
    calls_before = dict(calls)
    p2 = HistoricalOptionsProvider(db)
    p2.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024, 3, 1), expiry_max=date(2024, 3, 31))
    p2.get_bar("AAPL240315C00180000", date(2024, 3, 5))
    p2.get_atm_iv("AAPL", date(2024, 3, 7))
    assert calls == calls_before, (
        "second provider re-hit the DB instead of reusing the worker-level cache"
    )


def test_clear_worker_options_cache_forces_reload(monkeypatch, tmp_path):
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed(db)

    calls = {"chain": 0}
    real_load_chain = op._load_chain_history

    def counting_load_chain(db_path, underlying):
        calls["chain"] += 1
        return real_load_chain(db_path, underlying)

    monkeypatch.setattr(op, "_load_chain_history", counting_load_chain)

    p = HistoricalOptionsProvider(db)
    p.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024, 3, 1), expiry_max=date(2024, 3, 31))
    assert calls["chain"] == 1

    op.clear_worker_options_cache()
    p.get_chain("AAPL", date(2024, 3, 7), expiry_min=date(2024, 3, 1), expiry_max=date(2024, 3, 31))
    assert calls["chain"] == 2, "clear_worker_options_cache() should force a fresh reload"


def test_chain_and_bar_cache_are_lru_bounded(monkeypatch, tmp_path):
    """Cache size never exceeds the configured cap -- a long-lived remote worker touching many
    underlyings/contracts across many jobs must not grow this cache unboundedly."""
    op.clear_worker_options_cache()
    monkeypatch.setattr(op, "_CHAIN_CACHE_MAX", 2)
    monkeypatch.setattr(op, "_BAR_CACHE_MAX", 2)

    db = str(tmp_path / "opt.db")
    c = OptionsHistoryCache(db)
    for i, underlying in enumerate(["AAA", "BBB", "CCC"]):
        occ = f"{underlying}240315C00180000"
        c.write_chain_rows(underlying, "2024-03-01", [{
            "occ_symbol": occ, "option_type": "call", "strike": 180.0, "expiry": "2024-03-15",
            "bid": 2.0, "ask": 2.1, "last": 2.05, "iv": 0.25, "delta": 0.5, "gamma": 0.01,
            "theta": -0.03, "vega": 0.1, "open_interest": 1000, "volume": 50,
        }])
        c.write_bar_rows([{
            "occ_symbol": occ, "date": "2024-03-04", "open": 2.1, "high": 2.4, "low": 2.0,
            "close": 2.3, "volume": 120, "underlying": underlying, "option_type": "call",
            "strike": 180.0, "expiry": "2024-03-15",
        }])

    p = HistoricalOptionsProvider(db)
    for underlying in ["AAA", "BBB", "CCC"]:
        p.get_chain(underlying, date(2024, 3, 7), expiry_min=date(2024, 3, 1), expiry_max=date(2024, 3, 31))

    assert len(op._WORKER_CHAIN_CACHE) == 2, "chain cache exceeded its configured LRU cap"
    assert len(op._WORKER_BAR_CACHE) == 2, "bar cache exceeded its configured LRU cap"
    # AAA was the least-recently-used entry (evicted first).
    assert (db, "AAA") not in op._WORKER_CHAIN_CACHE
    assert (db, "BBB") in op._WORKER_CHAIN_CACHE and (db, "CCC") in op._WORKER_CHAIN_CACHE

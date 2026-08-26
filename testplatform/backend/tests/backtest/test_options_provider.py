# backend/tests/backtest/test_options_provider.py
from datetime import date
import pytest
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


# ---------------------------------------------------------------------------
# 2026-07-22 bug B4: point-in-time quotes. The chain row's bid/ask/last are the cache
# build's START-DATE snapshot and go stale weeks into a backtest — when a per-date bar
# exists, quotes derive from the bar close (last = close; bid/ask = close ± half the
# snapshot's absolute spread). With no bar the chain-row snapshot is kept exactly.
# ---------------------------------------------------------------------------
def _seed_b4(db):
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol":"AAPL240419C00180000","option_type":"call","strike":180.0,"expiry":"2024-04-19",
         "bid":2.0,"ask":2.2,"last":2.1,"iv":0.25,"delta":0.5,"gamma":0.01,"theta":-0.03,"vega":0.1,
         "open_interest":1000,"volume":50},
        {"occ_symbol":"AAPL240419P00180000","option_type":"put","strike":180.0,"expiry":"2024-04-19",
         "bid":None,"ask":None,"last":None,"iv":0.27,"delta":-0.5,"gamma":0.01,"theta":-0.03,
         "vega":0.1,"open_interest":900,"volume":40}])
    c.write_bar_rows([{"occ_symbol":"AAPL240419C00180000","date":"2024-03-05","open":2.9,"high":3.2,
        "low":2.8,"close":3.0,"volume":120,"underlying":"AAPL","option_type":"call","strike":180.0,
        "expiry":"2024-04-19"},
        {"occ_symbol":"AAPL240419P00180000","date":"2024-03-05","open":1.4,"high":1.6,
        "low":1.3,"close":1.5,"volume":80,"underlying":"AAPL","option_type":"put","strike":180.0,
        "expiry":"2024-04-19"}])
    return c

def test_chain_quotes_derived_from_bar_close(tmp_path):
    """Bar close moved vs the start-date snapshot premium -> bid/ask/last come from the bar."""
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_b4(db)
    p = HistoricalOptionsProvider(db)
    chain = p.get_chain("AAPL", date(2024,3,7), expiry_min=date(2024,3,1),
                        expiry_max=date(2024,5,31), option_type=OptionRight.CALL)
    assert len(chain) == 1
    ct = chain[0]
    assert ct.last == 3.0                   # bar close, not the stale 2.1 snapshot
    assert ct.bid == pytest.approx(2.9)     # close - snapshot_spread/2 (spread 0.2 preserved)
    assert ct.ask == pytest.approx(3.1)     # close + snapshot_spread/2

def test_get_quote_synthesizes_same_spread_as_chain(tmp_path):
    """Entry actions read chain rows, close actions read quotes — both must agree (B4)."""
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_b4(db)
    p = HistoricalOptionsProvider(db)
    q = p.get_quote("AAPL240419C00180000", date(2024,3,5))
    assert q is not None
    assert q.last == 3.0
    assert q.bid == pytest.approx(2.9) and q.ask == pytest.approx(3.1)

def test_chain_quotes_keep_snapshot_when_no_bar(tmp_path):
    """No bar on/before the as-of date -> chain-row snapshot quotes kept exactly (B4)."""
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_b4(db)
    p = HistoricalOptionsProvider(db)
    chain = p.get_chain("AAPL", date(2024,3,3), expiry_min=date(2024,3,1),
                        expiry_max=date(2024,5,31), option_type=OptionRight.CALL)
    assert len(chain) == 1
    assert chain[0].bid == 2.0 and chain[0].ask == 2.2 and chain[0].last == 2.1

def test_chain_quotes_bar_close_only_when_snapshot_lacks_spread(tmp_path):
    """Chain row without bid/ask + a bar -> only last is set; spread never fabricated (B4)."""
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_b4(db)
    p = HistoricalOptionsProvider(db)
    chain = p.get_chain("AAPL", date(2024,3,7), expiry_min=date(2024,3,1),
                        expiry_max=date(2024,5,31), option_type=OptionRight.PUT)
    assert len(chain) == 1
    assert chain[0].last == 1.5 and chain[0].bid is None and chain[0].ask is None
    q = p.get_quote("AAPL240419P00180000", date(2024,3,5))
    assert q.last == 1.5 and q.bid is None and q.ask is None


# ---------------------------------------------------------------------------
# 2026-07-22 bug B7: get_atm_iv must return a NEAR-ATM contract's iv (mirroring live
# AlpacaAccount.get_atm_implied_volatility: nearest the money, 20-45 DTE), not the old
# chain-wide mean. Spot is unavailable in this layer, so |delta| nearest 0.50 among CALLS
# proxies at-the-money (documented in the provider docstring).
# ---------------------------------------------------------------------------
def _seed_skewed_chain(db):
    """Known skew: near-ATM call iv 0.30, wings 0.60/0.90, an out-of-window near-dated call
    at 0.05 and an in-window put at 0.99. The old chain mean (0.568) is far from 0.30."""
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        # near-ATM call, in the 20-45 DTE window (as_of 2024-03-01 -> window 03-21..04-15)
        {"occ_symbol":"AAPL240405C00100000","option_type":"call","strike":100.0,"expiry":"2024-04-05",
         "iv":0.30,"delta":0.52,"gamma":0.01,"theta":-0.03,"vega":0.1},
        # deep-ITM call (delta ~1) — skewed high iv
        {"occ_symbol":"AAPL240405C00050000","option_type":"call","strike":50.0,"expiry":"2024-04-05",
         "iv":0.60,"delta":0.99,"gamma":0.01,"theta":-0.03,"vega":0.1},
        # far-OTM call (delta ~0) — skewed high iv
        {"occ_symbol":"AAPL240405C00150000","option_type":"call","strike":150.0,"expiry":"2024-04-05",
         "iv":0.90,"delta":0.05,"gamma":0.01,"theta":-0.03,"vega":0.1},
        # near-ATM call OUTSIDE the DTE window (10 DTE) — must be excluded
        {"occ_symbol":"AAPL240311C00100000","option_type":"call","strike":100.0,"expiry":"2024-03-11",
         "iv":0.05,"delta":0.50,"gamma":0.01,"theta":-0.03,"vega":0.1},
        # near-ATM PUT in-window — excluded (calls only, for determinism)
        {"occ_symbol":"AAPL240405P00100000","option_type":"put","strike":100.0,"expiry":"2024-04-05",
         "iv":0.99,"delta":-0.48,"gamma":0.01,"theta":-0.03,"vega":0.1}])
    # The SAME skew on as-of-clamped BARS. Required since 2026-08-26 (OPT-C8): _compute_atm_iv
    # no longer falls back to the chain-snapshot row's iv/delta, because that row records no
    # trace of the date its IV was inverted from. Mirroring the values keeps these tests about
    # the B7 SELECTION rule (near-ATM call, DTE-windowed, calls only) rather than about the
    # source; the no-fallback property is pinned in test_atm_iv_no_lookahead.py.
    c.write_bar_rows([
        {"occ_symbol": occ, "date": "2024-03-01", "open": 3.0, "high": 3.1, "low": 2.9,
         "close": 3.0, "volume": 100, "underlying": "AAPL", "option_type": otype,
         "strike": strike, "expiry": expiry, "iv": iv, "delta": delta,
         "gamma": 0.01, "theta": -0.03, "vega": 0.1}
        for occ, otype, strike, expiry, iv, delta in (
            ("AAPL240405C00100000", "call", 100.0, "2024-04-05", 0.30, 0.52),
            ("AAPL240405C00050000", "call", 50.0, "2024-04-05", 0.60, 0.99),
            ("AAPL240405C00150000", "call", 150.0, "2024-04-05", 0.90, 0.05),
            ("AAPL240311C00100000", "call", 100.0, "2024-03-11", 0.05, 0.50),
            ("AAPL240405P00100000", "put", 100.0, "2024-04-05", 0.99, -0.48),
        )
    ])
    return c

def test_atm_iv_picks_near_atm_call_not_chain_mean(tmp_path):
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_skewed_chain(db)
    p = HistoricalOptionsProvider(db)
    assert p.get_atm_iv("AAPL", date(2024,3,1)) == pytest.approx(0.30)

def test_atm_iv_uses_asof_bar_greeks_overlay(tmp_path):
    """ATM selection reads the as-of-clamped bar's iv/delta when its own iv computed (same
    overlay rule as get_chain), so the value tracks iv changes across the backtest window."""
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed_skewed_chain(db)
    OptionsHistoryCache(db).write_bar_rows([
        {"occ_symbol":"AAPL240405C00100000","date":"2024-03-01","open":3.0,"high":3.1,"low":2.9,
         "close":3.0,"volume":100,"underlying":"AAPL","option_type":"call","strike":100.0,
         "expiry":"2024-04-05","iv":0.42,"delta":0.51,"gamma":0.01,"theta":-0.03,"vega":0.1}])
    p = HistoricalOptionsProvider(db)
    assert p.get_atm_iv("AAPL", date(2024,3,1)) == pytest.approx(0.42)

def test_atm_iv_none_when_no_in_window_contract(tmp_path):
    op.clear_worker_options_cache()
    db = str(tmp_path / "opt.db"); _seed(db)  # seeded expiry 2024-03-15 is only 8 DTE out
    p = HistoricalOptionsProvider(db)
    assert p.get_atm_iv("AAPL", date(2024,3,7)) is None

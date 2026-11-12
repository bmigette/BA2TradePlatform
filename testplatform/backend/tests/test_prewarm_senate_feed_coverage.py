"""Prewarm of FMPSenateTraderWeight must leave a hermetic backtest with ZERO misses.

Basket mode prices every ticker the disclosure FEED names (not just the universe) and open-position
management reads the per-symbol trades of each held one.  Everything here runs against the REAL
``fmp_history_disk_cached`` (temp cache dir) with fake network functions; no socket may open.
"""
from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timezone

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import requests  # noqa: E402

from ba2_providers import fmp_common  # noqa: E402
from ba2_providers.fmp_common import (  # noqa: E402
    fmp_history_disk_cached, frozen_ttl_cache, hermetic_fmp_history, persist_empty_sentinel,
    reset_hermetic_misses, hermetic_miss_symbols,
)

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2025, 12, 31, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    def _refuse(self, address):  # noqa: ANN001
        raise AssertionError(f"network attempt to {address!r}")
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    reset_hermetic_misses()
    yield
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    reset_hermetic_misses()


def _trade(sym, ds, name="Jane Doe", typ="Purchase", es=None):
    first, last = name.split()
    return {"symbol": sym, "disclosureDate": ds, "transactionDate": es or ds, "type": typ, "amount": "$1,001 - $15,000",
            "firstName": first, "lastName": last}


class _FakeSenate:
    """Stand-in for the expert the fetchers drive (no network): serves canned feed/per-symbol rows."""

    def __init__(self, feed, per_symbol, no_price=()):
        self.feed, self.per_symbol, self.no_price = feed, per_symbol, set(no_price)
        self._api_key = "k"
        self.price_calls = []

    @staticmethod
    def _normalize_ticker(s):
        return s.replace("/", "-").upper().strip() if s else ""

    @staticmethod
    def _trader_name(t):
        return f"{t.get('firstName', '')} {t.get('lastName', '')}".strip()

    def _fetch_senate_trades(self, symbol=None, full_history=False):
        if symbol is None:
            return [t for t in self.feed if t.get("chamber") == "senate"]
        return fmp_history_disk_cached("congress_senate_trades", symbol, lambda: [])

    def _fetch_house_trades(self, symbol=None, full_history=False):
        if symbol is None:
            return [t for t in self.feed if t.get("chamber") != "senate"]
        return fmp_history_disk_cached("congress_house_trades", symbol, lambda: self.per_symbol.get(symbol, []))

    def _fetch_trader_history(self, name):
        return fmp_history_disk_cached("congress_trader_history", name.replace(" ", "_"), lambda: [])

    @staticmethod
    def _calculate_trader_avg_hold_days(history):
        return {"avg_hold_days": None, "roundtrips": 0}

    def _get_price_at_date(self, sym, date):
        self.price_calls.append(sym)
        # a ticker FMP has no data for -> the fetch yields [] -> prewarm's empty sentinel
        fmp_history_disk_cached("historical_price_full", sym,
                                lambda: [] if sym in self.no_price else [{"date": "2020-01-02", "open": 1.0}])
        return 1.0


def _fetchers(fake, tmp_path):
    from app.services.prewarm_fetchers import PrewarmFetchers
    f = PrewarmFetchers(fmp_key="k", end_date=END, finnhub_key=None, estimates_source="alphavantage",
                        log=lambda m: None, start_date=START)
    f.senate_expert = fake
    f._warm = lambda *a, **k: None  # earnings namespace is covered elsewhere
    return f


def test_feed_referenced_symbols_uses_expert_normalisation_and_window(tmp_path):
    fake = _FakeSenate([], {})
    f = _fetchers(fake, tmp_path)
    trades = [_trade("BRK/B", "2021-03-01"), _trade("AMGEN", "2021-03-01"), _trade("OLD", "2010-01-01"),
              _trade("FUT", "2026-06-01"), _trade("", "2021-03-01")]
    assert f.feed_referenced_symbols(fake, trades) == ["AMGEN", "BRK-B"]


def test_run_prewarm_warms_out_of_universe_feed_tickers_and_sentinels_no_data_ones(tmp_path):
    from app.services.prewarm_fetchers import run_prewarm
    feed = [dict(_trade("INUNI", "2021-03-01"), chamber="senate"),
            dict(_trade("OUTSIDE", "2021-03-02"), chamber="house"),
            dict(_trade("9201", "2021-03-03"), chamber="house")]
    fake = _FakeSenate(feed, {}, no_price={"9201"})
    f = _fetchers(fake, tmp_path)
    summary = run_prewarm(f, ["FMPSenateTraderWeight"], ["INUNI"], 2, end=END)
    assert summary["errors"] == 0, summary["failures"]

    # the backtest side: hermetic, every dataset the expert reads for those tickers is present -> 0 misses
    with frozen_ttl_cache(), hermetic_fmp_history():
        for sym in ("INUNI", "OUTSIDE", "9201"):
            fake._get_price_at_date(sym, START)
            fake._fetch_senate_trades(sym)
            fake._fetch_house_trades(sym)
        assert fmp_history_disk_cached("historical_price_full", "9201", lambda: 1 / 0) == []   # known empty
        assert hermetic_miss_symbols() == set()


def test_run_prewarm_persists_the_trader_roster_for_the_backtest(tmp_path):
    from app.services.prewarm_fetchers import run_prewarm
    from ba2_experts.FMPSenateTraderWeight import CONGRESS_ROSTER_NAMESPACE, CONGRESS_ROSTER_KEY
    feed = [dict(_trade("A", "2021-03-01", name="Ann Lee"), chamber="senate"),
            dict(_trade("B", "2021-03-02", name="Bob Ray"), chamber="house")]
    fake = _FakeSenate(feed, {"A": [_trade("A", "2020-02-01", name="Old Member")]})
    f = _fetchers(fake, tmp_path)
    run_prewarm(f, ["FMPSenateTraderWeight"], ["A"], 1, end=END)
    with frozen_ttl_cache(), hermetic_fmp_history():
        roster = fmp_history_disk_cached(CONGRESS_ROSTER_NAMESPACE, CONGRESS_ROSTER_KEY, lambda: 1 / 0)
        assert roster == ["Ann Lee", "Bob Ray", "Old Member"]
        assert hermetic_miss_symbols() == set()


def test_a_failed_buy_symbol_price_is_reported_not_swallowed(tmp_path):
    from app.services.prewarm_fetchers import run_prewarm
    feed = [dict(_trade("A", "2021-03-01"), chamber="senate")]
    fake = _FakeSenate(feed, {})
    f = _fetchers(fake, tmp_path)

    def boom(sym, date):
        raise ValueError("Failed to fetch price history for A: 503")
    fake._get_price_at_date = boom
    summary = run_prewarm(f, ["FMPSenateTraderWeight"], ["A"], 1, end=END)
    assert summary["errors"] >= 1
    assert any("historical_price_full/A" in x or "FMPSenateTraderWeight/A" in x for x in summary["failures"])


def test_run_prewarm_warms_the_tickers_of_pre_cap_backfill_rows(tmp_path):
    """House-latest is page-capped (~2023-05): the backtest rebuilds older house disclosures from the roster's
    trader histories (``_pre_cap_backfill``) and prices their tickers, so the prewarm must warm those too --
    including traders only discovered through the per-symbol trades of a feed ticker (a second round)."""
    from app.services.prewarm_fetchers import run_prewarm
    histories = {"Old_Member": [_trade("PRECAP", "2021-05-01", name="Old Member"),
                                _trade("ANCIENT", "2015-01-01", name="Old Member"),
                                _trade("NEW", "2023-06-01", name="Old Member")]}

    class _WithHistories(_FakeSenate):
        def _fetch_trader_history(self, name):
            key = name.replace(" ", "_")
            return fmp_history_disk_cached("congress_trader_history", key, lambda: histories.get(key, []))

    feed = [dict(_trade("NEW", "2023-06-01", name="Ann Lee"), chamber="house")]
    fake = _WithHistories(feed, {"NEW": [_trade("NEW", "2023-06-01", name="Old Member")]})
    f = _fetchers(fake, tmp_path)
    summary = run_prewarm(f, ["FMPSenateTraderWeight"], ["NEW"], 1, end=END)
    assert summary["errors"] == 0, summary["failures"]
    assert f.senate_house_floor == "2023-06-01"
    assert f.pre_cap_referenced_symbols() == ["PRECAP"]   # ANCIENT: disclosed before [start - lookback, end]
    with frozen_ttl_cache(), hermetic_fmp_history():
        fake._get_price_at_date("PRECAP", START)
        fake._fetch_house_trades("PRECAP")
        assert hermetic_miss_symbols() == set()

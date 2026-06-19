"""Tests for FMPHistoricalScreenerProvider (Phase 3, Task 3).

All FMP access is monkeypatched — no live key / network is required (the test DBs
have no FMP key). The gate anchors here:
  - test_historical_normalised_shape_matches_live -> gate item 2 (identical dict shape)
  - test_historical_screen_applies_thresholds     -> Stage-1 threshold parity + survivorship
"""
from datetime import datetime, timezone

import importlib

import pytest

# NOTE: ``ba2_providers.screener`` re-exports the class name
# ``FMPHistoricalScreenerProvider`` at package level, which shadows the same-named
# submodule on attribute access. Use importlib to get the actual MODULE object (so
# monkeypatching module-level names like ``broad_universe``/``index_universe`` works).
H = importlib.import_module("ba2_providers.screener.FMPHistoricalScreenerProvider")

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)

# The 12-key contract that FMPScreenerProvider._normalise_result emits.
LIVE_KEYS = {
    "symbol", "company_name", "price", "volume", "market_cap", "sector",
    "industry", "exchange", "beta", "is_actively_trading", "country", "float_shares",
}


def _patch(monkeypatch, prov, *, universe, closes, mcaps, vols):
    """Patch the as-of metric helpers + the universe builder.

    ``mcaps`` maps symbol -> market_cap; the helper returns the (mcap, source) tuple
    the real ``_market_cap_at`` returns.
    """
    monkeypatch.setattr(prov, "_close_at", lambda s, a: closes.get(s))
    monkeypatch.setattr(
        prov, "_market_cap_at",
        lambda s, a, c: (mcaps.get(s), "shares_x_close" if mcaps.get(s) else "unavailable"),
    )
    monkeypatch.setattr(prov, "_avg_volume_at", lambda s, a, window=20: vols.get(s, 0.0))
    monkeypatch.setattr(H, "fetch_lifecycle_map", lambda: {})
    monkeypatch.setattr(H, "broad_universe", lambda a, lifecycle=None: universe)


def test_historical_screen_applies_thresholds(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["KEEP", "CHEAP", "SMALL", "DEAD"],
        closes={"KEEP": 50.0, "CHEAP": 5.0, "SMALL": 60.0, "DEAD": 40.0},
        mcaps={"KEEP": 5e9, "CHEAP": 9e9, "SMALL": 1e8, "DEAD": 3e9},
        vols={"KEEP": 1_000_000, "CHEAP": 2_000_000, "SMALL": 800_000, "DEAD": 700_000},
    )
    filters = {
        "price_min": 20.0,
        "market_cap_min": 1_000_000_000,
        "volume_min": 500_000,
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "limit": 10_000,
    }
    res = p.screen_stocks(filters, as_of=AS_OF)
    syms = {r["symbol"] for r in res}
    assert "KEEP" in syms       # passes all thresholds
    assert "DEAD" in syms       # survivorship: present on as_of (universe contains it)
    assert "CHEAP" not in syms  # price 5 < price_min 20
    assert "SMALL" not in syms  # mcap 1e8 < market_cap_min 1e9


def test_historical_volume_max_threshold(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["LOW", "HIGH"],
        closes={"LOW": 50.0, "HIGH": 50.0},
        mcaps={"LOW": 5e9, "HIGH": 5e9},
        vols={"LOW": 600_000, "HIGH": 9_000_000},
    )
    res = p.screen_stocks(
        {"price_min": 20.0, "volume_min": 500_000, "volume_max": 1_000_000}, as_of=AS_OF
    )
    syms = {r["symbol"] for r in res}
    assert "LOW" in syms
    assert "HIGH" not in syms   # 9M > volume_max 1M


def test_historical_market_cap_max_threshold(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["MID", "MEGA"],
        closes={"MID": 50.0, "MEGA": 50.0},
        mcaps={"MID": 5e9, "MEGA": 5e11},
        vols={"MID": 1_000_000, "MEGA": 1_000_000},
    )
    res = p.screen_stocks(
        {"market_cap_min": 1e9, "market_cap_max": 1e11}, as_of=AS_OF
    )
    syms = {r["symbol"] for r in res}
    assert "MID" in syms
    assert "MEGA" not in syms   # 5e11 > market_cap_max 1e11


def test_historical_limit_truncates(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["A", "B", "C", "D", "E"],
        closes={s: 50.0 for s in "ABCDE"},
        mcaps={s: 5e9 for s in "ABCDE"},
        vols={s: 1_000_000 for s in "ABCDE"},
    )
    res = p.screen_stocks({"limit": 2}, as_of=AS_OF)
    assert len(res) == 2


def test_historical_normalised_shape_matches_live():
    from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider
    live_keys = set(FMPScreenerProvider._normalise_result({}).keys())
    hist_keys = set(
        H.FMPHistoricalScreenerProvider._normalise("X", 1.0, 1.0, 1.0, None).keys()
    )
    assert hist_keys == live_keys        # identical 12-key dict shape
    assert hist_keys == LIVE_KEYS        # and exactly the documented contract


def test_historical_normalise_audit_metadata_off_contract():
    """Audit annotations (market_cap_source / float_approx) only appear when set,
    so the bare normalise() stays shape-equal to the live result."""
    bare = H.FMPHistoricalScreenerProvider._normalise("X", 1.0, 1.0, 1.0, None)
    assert "market_cap_source" not in bare
    assert "float_approx" not in bare
    annotated = H.FMPHistoricalScreenerProvider._normalise(
        "X", 1.0, 1.0, 1.0, None, market_cap_source="shares_x_close", float_approx=True
    )
    assert annotated["market_cap_source"] == "shares_x_close"
    assert annotated["float_approx"] is True
    # The 12 contract keys are still all present alongside the audit extras.
    assert LIVE_KEYS.issubset(annotated.keys())


def test_historical_screen_records_market_cap_source(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["KEEP"],
        closes={"KEEP": 50.0},
        mcaps={"KEEP": 5e9},
        vols={"KEEP": 1_000_000},
    )
    res = p.screen_stocks({"price_min": 20.0}, as_of=AS_OF)
    assert len(res) == 1
    assert res[0]["market_cap_source"] == "shares_x_close"
    assert res[0]["float_approx"] is True
    assert res[0]["float_shares"] is None     # documented backtest approximation


def test_historical_drops_symbols_without_price(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="broad")
    p.api_key = "x"
    _patch(
        monkeypatch, p,
        universe=["GOOD", "NOPRICE"],
        closes={"GOOD": 50.0},          # NOPRICE -> None (no as-of close)
        mcaps={"GOOD": 5e9, "NOPRICE": 5e9},
        vols={"GOOD": 1_000_000, "NOPRICE": 1_000_000},
    )
    res = p.screen_stocks({"price_min": 20.0}, as_of=AS_OF)
    syms = {r["symbol"] for r in res}
    assert "GOOD" in syms
    assert "NOPRICE" not in syms        # no reconstructable close -> excluded


def test_historical_index_mode_uses_index_universe(monkeypatch):
    p = H.FMPHistoricalScreenerProvider(universe_mode="sp500")
    p.api_key = "x"
    captured = {}

    def fake_index(idx, a):
        captured["index"] = idx
        return ["IDX1"]

    monkeypatch.setattr(H, "index_universe", fake_index)
    monkeypatch.setattr(p, "_close_at", lambda s, a: 50.0)
    monkeypatch.setattr(p, "_market_cap_at", lambda s, a, c: (5e9, "shares_x_close"))
    monkeypatch.setattr(p, "_avg_volume_at", lambda s, a, window=20: 1_000_000)
    res = p.screen_stocks({"price_min": 20.0}, as_of=AS_OF)
    assert captured["index"] == "sp500"
    assert {r["symbol"] for r in res} == {"IDX1"}


def test_historical_rejects_none_as_of():
    p = H.FMPHistoricalScreenerProvider()
    p.api_key = "x"
    with pytest.raises(ValueError):
        p.screen_stocks({}, as_of=None)


def test_historical_provider_name_and_registry():
    from ba2_providers import get_provider, SCREENER_PROVIDERS
    assert "fmp_historical" in SCREENER_PROVIDERS
    prov = get_provider("screener", "fmp_historical", universe_mode="nasdaq")
    assert prov.get_provider_name() == "fmp_historical"
    assert prov.universe_mode == "nasdaq"


def test_market_cap_at_prefers_historical_endpoint(monkeypatch):
    """When the FMP dated historical-market-cap endpoint returns a value, it wins."""
    p = H.FMPHistoricalScreenerProvider()
    p.api_key = "x"

    class _Resp:
        def json(self):
            return [{"date": "2020-06-30", "marketCap": 1.23e10}]

    import ba2_providers.fmp_common as fc
    monkeypatch.setattr(fc, "fmp_http_get", lambda *a, **k: _Resp())
    mc, src = p._market_cap_at("AAPL", AS_OF, close=100.0)
    assert mc == 1.23e10
    assert src == "historical_market_cap"


def test_market_cap_at_falls_back_to_shares_x_close(monkeypatch):
    """When the dated endpoint errors/empties, fall back to shares x close."""
    p = H.FMPHistoricalScreenerProvider()
    p.api_key = "x"

    import ba2_providers.fmp_common as fc

    def boom(*a, **k):
        raise RuntimeError("no endpoint")

    monkeypatch.setattr(fc, "fmp_http_get", boom)
    monkeypatch.setattr(p, "_shares_at", lambda s, a: 2_000_000.0)
    mc, src = p._market_cap_at("AAPL", AS_OF, close=50.0)
    assert mc == 100_000_000.0
    assert src == "shares_x_close"


def test_market_cap_at_unavailable(monkeypatch):
    """No dated endpoint and no shares -> unavailable."""
    p = H.FMPHistoricalScreenerProvider()
    p.api_key = "x"

    import ba2_providers.fmp_common as fc
    monkeypatch.setattr(fc, "fmp_http_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(p, "_shares_at", lambda s, a: None)
    mc, src = p._market_cap_at("AAPL", AS_OF, close=50.0)
    assert mc is None
    assert src == "unavailable"

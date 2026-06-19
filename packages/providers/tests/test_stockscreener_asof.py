"""StockScreener as_of re-anchor tests (Phase 3, Task 4).

Cover the THREE behaviours Task 4 adds, plus the live-path no-regression guards:

  1. Provider selection forks on as_of: as_of=<date> -> 'fmp_historical'
     (with universe_mode forwarded); as_of=None -> the configured live provider,
     and as_of stays None down the screen_stocks call.
  2. The two now()-based fetch windows re-anchor to as_of inside _fetch_history_bulk
     (to_date == as_of); the live (as_of=None) window still anchors on today.
  3. The historical RVOL path derives a quote-shaped map from as-of bars
     (_quotes_from_bars) instead of live /quote, and the per-candidate enrichment
     loop applies the SAME RVOL/float/volume_max filters over it.

All fetches are mocked/monkeypatched — no FMP key or network is required.
"""
from datetime import datetime, timezone

import ba2_providers
import ba2_providers.StockScreener as S

AS_OF = datetime(2020, 6, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 1. Provider selection fork
# ----------------------------------------------------------------------

def test_screener_selects_historical_provider_when_as_of_set(monkeypatch):
    captured = {}

    class FakeProv:
        def screen_stocks(self, filters, as_of=None):
            captured["as_of"] = as_of
            captured["filters"] = filters
            return []  # empty -> early return, skips enrichment

    def fake_get_provider(cat, name, **kw):
        captured["category"] = cat
        captured["name"] = name
        captured["kw"] = kw
        return FakeProv()

    monkeypatch.setattr(ba2_providers, "get_provider", fake_get_provider)

    sc = S.StockScreener({"universe_mode": "sp500"}, as_of=AS_OF)
    out = sc.screen()

    assert captured["category"] == "screener"
    assert captured["name"] == "fmp_historical"
    assert captured["kw"]["universe_mode"] == "sp500"
    assert captured["as_of"] == AS_OF
    assert out["results"] == []


def test_screener_live_path_unchanged(monkeypatch):
    captured = {}

    class FakeProv:
        def screen_stocks(self, filters, as_of=None):
            captured["as_of"] = as_of
            captured["name_seen"] = True
            return []

    def fake_get_provider(cat, name, **kw):
        captured["name"] = name
        captured["kw"] = kw
        return FakeProv()

    monkeypatch.setattr(ba2_providers, "get_provider", fake_get_provider)

    sc = S.StockScreener({"screener_provider": "fmp"})  # no as_of
    sc.screen()

    assert captured["name"] == "fmp"        # live path uses the configured provider
    assert captured["kw"] == {}             # no universe_mode forwarded on live path
    assert captured["as_of"] is None        # live path: as_of stays None


# ----------------------------------------------------------------------
# 2. now()-window re-anchor inside _fetch_history_bulk
# ----------------------------------------------------------------------

def test_fetch_history_bulk_anchors_on_as_of(monkeypatch):
    sc = S.StockScreener({}, as_of=AS_OF)
    captured = {}

    def fake_http(url, params=None, endpoint=None, timeout=None):
        captured["from"] = params.get("from")
        captured["to"] = params.get("to")

        class R:
            def json(self):
                return {"historicalStockList": []}

        return R()

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_http, raising=False)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "key", raising=False)

    sc._fetch_history_bulk(["AAA"], lookback_days=5)

    assert captured["to"] == "2020-06-30"   # anchored on as_of, not today
    # from_date = as_of - (lookback_days + 5) = 2020-06-30 - 10 days
    assert captured["from"] == "2020-06-20"


def test_fetch_history_bulk_live_window_is_today(monkeypatch):
    sc = S.StockScreener({})  # no as_of -> live
    captured = {}

    def fake_http(url, params=None, endpoint=None, timeout=None):
        captured["to"] = params.get("to")

        class R:
            def json(self):
                return {"historicalStockList": []}

        return R()

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_http, raising=False)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "key", raising=False)

    sc._fetch_history_bulk(["AAA"], lookback_days=5)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert captured["to"] == today          # live path still anchors on now()


# ----------------------------------------------------------------------
# 3. Historical RVOL path: quotes-from-bars + same downstream filters
# ----------------------------------------------------------------------

def test_quotes_from_bars_builds_quote_shape(monkeypatch):
    sc = S.StockScreener({}, as_of=AS_OF)
    # 5 bars, oldest-first; last bar volume = 3,000,000, avg of all = 1,800,000
    bars = [
        {"date": "2020-06-24", "close": 10.0, "volume": 1_000_000},
        {"date": "2020-06-25", "close": 11.0, "volume": 1_500_000},
        {"date": "2020-06-26", "close": 12.0, "volume": 2_000_000},
        {"date": "2020-06-29", "close": 13.0, "volume": 1_500_000},
        {"date": "2020-06-30", "close": 14.0, "volume": 3_000_000},
    ]
    monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days: {"AAA": bars})

    quotes = sc._quotes_from_bars(["AAA"], window=20)
    q = quotes["AAA"]
    assert q["volume"] == 3_000_000              # last (as-of) bar volume
    assert q["price"] == 14.0                    # last (as-of) close
    assert q["avgVolume"] == 1_800_000.0         # mean of the 5 in-window vols
    # marketCap/sharesFloat intentionally absent so reconstructed market_cap survives
    assert "marketCap" not in q
    assert "sharesFloat" not in q


def test_enrich_with_rvol_uses_bars_on_as_of_path(monkeypatch):
    sc = S.StockScreener(
        {"screener_relative_volume_min": 1.5, "screener_float_min": 0,
         "screener_volume_max": 0},
        as_of=AS_OF,
    )
    # Guard: the live quote path must NOT be touched on the as_of path.
    def boom(*a, **k):
        raise AssertionError("live _fetch_quotes_chunked called on as_of path")
    monkeypatch.setattr(sc, "_fetch_quotes_chunked", boom)

    # avgVolume = mean over the in-window bars; rvol = last_vol / avgVolume.
    # KEEP: bars 500k, 2M -> avg 1.25M -> rvol 2M/1.25M = 1.6 >= 1.5 (kept)
    # DROP: bars 1M, 1M   -> avg 1M    -> rvol 1M/1M    = 1.0 <  1.5 (dropped)
    bars_map = {
        "KEEP": [{"close": 50.0, "volume": 500_000}, {"close": 51.0, "volume": 2_000_000}],
        "DROP": [{"close": 30.0, "volume": 1_000_000}, {"close": 31.0, "volume": 1_000_000}],
    }
    monkeypatch.setattr(sc, "_fetch_history_bulk", lambda syms, lookback_days: bars_map)

    candidates = [
        {"symbol": "KEEP", "price": 51.0, "market_cap": 5e9},
        {"symbol": "DROP", "price": 31.0, "market_cap": 4e9},
    ]
    enriched, stats = sc._enrich_with_rvol(candidates, min_rvol=1.5)
    syms = {c["symbol"] for c in enriched}
    assert syms == {"KEEP"}
    assert stats["dropped_rvol"] == 1
    # market_cap reconstructed by the historical provider is preserved (no q_mcap override)
    assert enriched[0]["market_cap"] == 5e9
    assert enriched[0]["relative_volume"] == 1.6


def test_enrich_with_rvol_live_path_uses_quotes(monkeypatch):
    sc = S.StockScreener({"screener_relative_volume_min": 1.5})  # live, no as_of
    called = {"bars": False, "quotes": False}

    def fake_quotes(symbols, *a, **k):
        called["quotes"] = True
        return {"KEEP": {"volume": 2_000_000, "avgVolume": 1_000_000, "price": 51.0}}

    def boom_bars(*a, **k):
        called["bars"] = True
        raise AssertionError("_quotes_from_bars/_fetch_history_bulk called on live path")

    monkeypatch.setattr(sc, "_fetch_quotes_chunked", fake_quotes)
    monkeypatch.setattr(sc, "_quotes_from_bars", boom_bars)

    enriched, _ = sc._enrich_with_rvol([{"symbol": "KEEP", "price": 51.0}], min_rvol=1.5)
    assert called["quotes"] is True
    assert called["bars"] is False
    assert {c["symbol"] for c in enriched} == {"KEEP"}

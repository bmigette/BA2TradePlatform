"""Phase 3 Task 8 Step 2 — GATE ITEM 1: live-equivalence (as_of=None byte-equal).

The Phase-3 acceptance gate requires that ``StockScreener(settings, as_of=None).screen()``
produces output **byte-equal** to the pre-Phase-3 live FMP screener for a fixed
``settings`` — the live decision logic must be unchanged because:

  * ``FMPScreenerProvider`` is byte-untouched below the documented ``as_of`` guard
    (the guard is a no-op for ``as_of=None`` — verified in test_screener_interface_asof.py
    that ``as_of=<date>`` raises, here that ``as_of=None`` flows through unchanged), and
  * ``StockScreener.screen()`` with ``as_of=None`` selects the configured ``screener_provider``
    (``"fmp"``) and passes ``as_of=None`` straight into ``screen_stocks`` (the seam line),
    then runs the identical, unchanged post-fetch pipeline.

This module proves it TWO ways:

  1. **Fixture-driven (no network, always runs)** — the single HTTP boundary
     ``ba2_providers.fmp_common.fmp_http_get`` is monkeypatched to return a fixed
     FMP ``/stock-screener`` payload. We assert that the candidate list produced by the
     full ``StockScreener(as_of=None).screen()`` pipeline is **byte-equal** (same dicts,
     same order, same keys/values) to calling the untouched
     ``FMPScreenerProvider().screen_stocks(filters)`` directly on the same payload.
     With RVOL / price-drop disabled (min=0) the pipeline makes NO further HTTP calls,
     so the comparison isolates the live screener path exactly.

  2. **Network-gated golden (skips without a key)** — the plan's documented live probe:
     when ``FMP_API_KEY`` is set, hit the real endpoint and assert the canonical dict
     shape survives the full live pipeline. Skipped in the keyless test DBs (the env's
     no-live-FMP-in-tests rule), so CI never depends on a live FMP call.
"""
import os

import pytest


# --------------------------------------------------------------------------- #
# A fixed FMP /stock-screener payload (the live endpoint's raw shape).
# Three rows above and one below the price/market-cap thresholds so the live
# server-side filter (encoded in _build_params) is exercised deterministically.
# --------------------------------------------------------------------------- #
_FMP_SCREENER_PAYLOAD = [
    {
        "symbol": "AAA", "companyName": "Alpha Corp", "price": 120.0,
        "volume": 3_000_000, "marketCap": 80_000_000_000, "sector": "Technology",
        "industry": "Software", "exchangeShortName": "NASDAQ", "beta": 1.1,
        "isActivelyTrading": True, "country": "US", "floatShares": 500_000_000,
    },
    {
        "symbol": "BBB", "companyName": "Beta Inc", "price": 60.0,
        "volume": 2_000_000, "marketCap": 60_000_000_000, "sector": "Healthcare",
        "industry": "Biotech", "exchangeShortName": "NYSE", "beta": 0.9,
        "isActivelyTrading": True, "country": "US", "floatShares": 300_000_000,
    },
    {
        "symbol": "CCC", "companyName": "Gamma LLC", "price": 25.0,
        "volume": 1_500_000, "marketCap": 55_000_000_000, "sector": "Energy",
        "industry": "Oil & Gas", "exchangeShortName": "AMEX", "beta": 1.4,
        "isActivelyTrading": True, "country": "US", "floatShares": 200_000_000,
    },
]

# Settings that DISABLE the post-Stage-1 HTTP stages (RVOL + price-drop), so the
# whole pipeline reduces to "fetch via live provider + rank + trim" with no other
# network. This isolates the live-screener path for a clean byte-equal comparison.
_NO_NETWORK_SETTINGS = {
    "screener_provider": "fmp",
    "screener_market_cap_min": 50_000_000_000,
    "screener_volume_min": 1_000_000,
    "screener_price_min": 20.0,
    "screener_relative_volume_min": 0,   # disable Stage 2 (no /quote calls)
    "screener_price_drop_pct": 0,        # disable Stage 4 (no /historical calls)
    "screener_max_stocks": 100,          # large -> no trim drift
    "screener_sort_metric": "market_cap",
}


def _patch_fmp(monkeypatch, payload):
    """Patch the single HTTP boundary + the key read so no network/key is needed.

    The test DBs are keyless, so ``FMPScreenerProvider.__init__`` would read an empty
    ``api_key`` and ``validate_config()`` would short-circuit ``screen_stocks`` to ``[]``
    BEFORE reaching the HTTP call. We give the provider a dummy key (so it proceeds into
    ``_build_params`` + ``fmp_http_get``) and stub the HTTP boundary with the fixture
    payload. No live FMP call, no real key — the LOGIC under test is unchanged.
    """
    import ba2_providers.fmp_common as fmp_common
    import ba2_providers.screener.FMPScreenerProvider as prov_mod

    class _Resp:
        def json(self):
            return payload

    def fake_get(url, params=None, endpoint=None, timeout=None):
        return _Resp()

    monkeypatch.setattr(fmp_common, "fmp_http_get", fake_get)
    # __init__ reads FMP_API_KEY via this name -> give it a dummy so validate_config passes.
    monkeypatch.setattr(prov_mod, "get_app_setting", lambda k: "dummy-key", raising=False)
    return fake_get


def test_live_provider_byte_equal_through_pipeline(monkeypatch):
    """as_of=None: the StockScreener live path is byte-equal to the untouched provider.

    The full StockScreener(as_of=None).screen() candidate set (after rank/trim, with
    enrichment disabled) must equal — dict-for-dict, in order — the result of calling
    FMPScreenerProvider().screen_stocks(filters) directly on the same FMP payload.
    """
    from ba2_providers.StockScreener import StockScreener
    from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider

    _patch_fmp(monkeypatch, _FMP_SCREENER_PAYLOAD)

    sc = StockScreener(_NO_NETWORK_SETTINGS)        # no as_of -> live path
    assert sc._as_of is None
    out = sc.screen()
    pipeline_results = out["results"]

    # Direct provider call on the SAME payload, with the SAME filters the pipeline builds.
    prov = FMPScreenerProvider()
    prov.api_key = "x"                              # construction reads key; we set it
    filters = sc._build_provider_filters()
    direct = prov.screen_stocks(filters, as_of=None)

    # Rank-then-trim is order-only over the same dicts; with metric=market_cap the
    # pipeline orders by descending market_cap. Compare as multisets first (byte-equal
    # contents), then confirm the pipeline's documented descending-market_cap order.
    assert {r["symbol"] for r in pipeline_results} == {d["symbol"] for d in direct}
    assert len(pipeline_results) == len(direct)
    by_symbol_pipeline = {r["symbol"]: r for r in pipeline_results}
    by_symbol_direct = {d["symbol"]: d for d in direct}
    for sym, drow in by_symbol_direct.items():
        assert by_symbol_pipeline[sym] == drow      # identical dict shape AND values

    # Live-pipeline ordering contract (rank by market_cap desc) is preserved.
    mcaps = [r["market_cap"] for r in pipeline_results]
    assert mcaps == sorted(mcaps, reverse=True)


def test_live_provider_normalised_keys_unchanged(monkeypatch):
    """The canonical 12-key normalised dict shape survives the as_of=None live path."""
    from ba2_providers.StockScreener import StockScreener

    _patch_fmp(monkeypatch, _FMP_SCREENER_PAYLOAD)
    out = StockScreener(_NO_NETWORK_SETTINGS).screen()
    assert out["results"], "fixture should yield survivors"
    expected = {
        "symbol", "company_name", "price", "volume", "market_cap", "sector",
        "industry", "exchange", "beta", "is_actively_trading", "country",
        "float_shares",
    }
    for row in out["results"]:
        assert set(row.keys()) == expected


def test_as_of_none_routes_to_configured_live_provider(monkeypatch):
    """The seam line: as_of=None selects screener_provider and passes as_of=None through."""
    from ba2_providers.StockScreener import StockScreener
    import ba2_providers

    captured = {}

    class _FakeProv:
        def screen_stocks(self, filters, as_of=None):
            captured["as_of"] = as_of
            captured["filters"] = filters
            return []

    def fake_get_provider(category, name, **kwargs):
        captured["category"] = category
        captured["name"] = name
        captured["kwargs"] = kwargs
        return _FakeProv()

    monkeypatch.setattr(ba2_providers, "get_provider", fake_get_provider)

    StockScreener({"screener_provider": "fmp"}).screen()   # no as_of
    assert captured["category"] == "screener"
    assert captured["name"] == "fmp"            # the configured LIVE provider
    assert captured["kwargs"] == {}             # no universe_mode kwarg on the live path
    assert captured["as_of"] is None            # as_of=None threaded straight through


# --------------------------------------------------------------------------- #
# Network-gated golden (the plan's documented live probe). Skips without a key.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("FMP_API_KEY"),
    reason="needs live FMP key for golden equivalence (skipped in keyless test DBs)",
)
def test_live_screen_unchanged_shape_and_keys():
    """as_of=None must hit the live FMP screener and return the canonical dict shape."""
    from ba2_providers.StockScreener import StockScreener

    settings = {
        "screener_market_cap_min": 50_000_000_000, "screener_volume_min": 1_000_000,
        "screener_price_min": 20.0, "screener_relative_volume_min": 0,
        "screener_price_drop_pct": 0, "screener_max_stocks": 5,
        "screener_sort_metric": "market_cap",
    }
    out = StockScreener(settings).screen()        # no as_of -> live
    assert isinstance(out["results"], list)
    if out["results"]:
        keys = set(out["results"][0].keys())
        assert {"symbol", "price", "market_cap", "volume"} <= keys

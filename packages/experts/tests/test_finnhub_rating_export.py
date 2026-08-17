"""FinnHubRating.export_symbol_data: analyst recommendation-trends metric rows.

FinnHubRating's live (as_of=None) _gather fetches recommendation trends via a
direct Finnhub HTTP client (_fetch_recommendation_trends), not the providers
registry, so that fetcher is monkeypatched at the CLASS level (unbound, taking
self) for the duration of each test -- mirroring test_fmp_rating_export.py's
convention.

Unlike FMPRating, FinnHubRating's live current_price DOES flow through
self._get_current_price(symbol) (see the module docstring in FinnHubRating.py
and its _gather), which the bypass-constructed instance (see
ExpertDataExportInterface._bypass_instance) automatically resolves via
providers.price_at_date -- UNLESS something has already customized
_get_current_price on the class. These tests deliberately do NOT monkeypatch
_get_current_price and instead inject a fake OHLCV provider (same
FakeOHLCV/resolver shape as test_finnhub_rating_gather_process.py), proving
that automatic price-resolution wiring end-to-end (per the Task 6 brief's
nice-to-have).

FinnHubRating has exactly ONE Recommendation(...) construction site (verified
by reading _process in full) -- there is no skip path, so rec.skip is always
False and the base 'Recommendation' row from ExpertDataExportInterface is
always kept alongside the appended consensus rows.

SYMBOL360 always calls export_symbol_data with as_of=None (the live path); the
as_of/lookahead reconstruction path is exercised separately in
test_finnhub_rating_gather_process.py and is not re-tested here.
"""
import pandas as pd
import pytest

from ba2_experts.FinnHubRating import FinnHubRating

SETTINGS = {"buy_threshold": 4.5, "overweight_threshold": 3.5,
            "hold_threshold": 2.5, "underweight_threshold": 1.5}


class FakeOHLCV:
    """Same fixed-close fake used by test_finnhub_rating_gather_process.py --
    price resolves through providers.price_at_date(...).get_ohlcv_data, no
    network call."""
    def __init__(self, close: float = 123.0):
        self._close = close

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [self._close]})


def _providers_resolver(close: float = 123.0):
    ohlcv = FakeOHLCV(close)
    return lambda cat, name, **kw: ohlcv


def _patch_trends(monkeypatch, trends):
    monkeypatch.setattr(FinnHubRating, "_fetch_recommendation_trends",
                        lambda self, symbol: trends)


def test_export_symbol_data_buy_shows_consensus_rows(monkeypatch):
    trends = [{"period": "2026-06-01", "strongBuy": 10, "buy": 0, "hold": 0,
              "sell": 0, "strongSell": 0}]
    _patch_trends(monkeypatch, trends)
    result = FinnHubRating.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_providers_resolver())
    assert result.error is None, result.error
    assert result.overall_signal == "buy"
    assert result.skipped is False
    assert result.confidence == pytest.approx(100.0)

    labels = {m.label for m in result.metrics}
    assert "Recommendation" in labels
    assert "Consensus mean (1=Strong Sell .. 5=Strong Buy)" in labels
    assert "Analysts" in labels

    mean_row = next(m for m in result.metrics
                    if m.label == "Consensus mean (1=Strong Sell .. 5=Strong Buy)")
    assert mean_row.value == pytest.approx(5.0)
    assert mean_row.display == "5.00"

    total_row = next(m for m in result.metrics if m.label == "Analysts")
    assert total_row.value == 10
    assert total_row.display == "10"
    assert total_row.detail == "Period: 2026-06-01"


def test_export_symbol_data_sell_from_all_strong_sell(monkeypatch):
    trends = [{"period": "2026-06-01", "strongBuy": 0, "buy": 0, "hold": 0,
              "sell": 0, "strongSell": 10}]
    _patch_trends(monkeypatch, trends)
    result = FinnHubRating.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_providers_resolver())
    assert result.error is None, result.error
    assert result.overall_signal == "sell"

    mean_row = next(m for m in result.metrics
                    if m.label == "Consensus mean (1=Strong Sell .. 5=Strong Buy)")
    assert mean_row.value == pytest.approx(1.0)
    assert mean_row.display == "1.00"

    total_row = next(m for m in result.metrics if m.label == "Analysts")
    assert total_row.value == 10
    assert total_row.detail == "Period: 2026-06-01"


def test_export_symbol_data_no_trends_holds_with_na_consensus(monkeypatch):
    """Empty trends data -> _calculate_recommendation's 'no eligible period'
    fallback (HOLD, mean=None, total=0, period=None) -- FinnHubRating's actual
    'no data' behaviour, since it has no skip path. Must NOT surface as the
    base class's 'Skipped' row."""
    _patch_trends(monkeypatch, [])
    result = FinnHubRating.export_symbol_data(
        "AAPL", overrides=SETTINGS, providers_resolver=_providers_resolver())
    assert result.error is None, result.error
    assert result.skipped is False
    assert result.overall_signal == "hold"

    labels = {m.label for m in result.metrics}
    assert "Skipped" not in labels
    assert "Recommendation" in labels

    mean_row = next(m for m in result.metrics
                    if m.label == "Consensus mean (1=Strong Sell .. 5=Strong Buy)")
    assert mean_row.value is None
    assert mean_row.display == "n/a"

    total_row = next(m for m in result.metrics if m.label == "Analysts")
    assert total_row.value == 0
    assert total_row.display == "0"
    assert total_row.detail is None

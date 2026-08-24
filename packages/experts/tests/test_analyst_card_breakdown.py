""""We should have the ability to expand and see all analysts data."

The FMPRating / FinnHubRating cards already KNOW the per-bucket analyst counts
and the four price targets -- but they only ever surfaced them buried inside a
1.5 kB prose blob (rec.details) that the renderer flattened into one clipped
line. These tests pin the same numbers as STRUCTURED (label, value) rows, which
the card draws as a small table.

FinnHubRating additionally had to carry its bucket counts through: _process
recorded mean/total/period in raw_outputs and dropped `counts` on the floor, so
the breakdown was genuinely unrecoverable at render time.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_experts.FinnHubRating import FinnHubRating
from ba2_experts.FMPRating import FMPRating


# ============================== FMPRating =================================

_FMP_SETTINGS = {"profit_ratio": 1.0, "min_analysts": 10,
                 "target_price_type": "consensus"}
_CONSENSUS = {"targetConsensus": 130.0, "targetHigh": 160.0,
              "targetLow": 110.0, "targetMedian": 128.0}
_UPGRADE = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]
_NOW = datetime.now(timezone.utc)
_TARGET_HISTORY = [
    {"publishedDate": (_NOW - timedelta(days=d)).strftime("%Y-%m-%d"), "priceTarget": p}
    for d, p in ((10, 110.0), (11, 124.0), (12, 128.0), (13, 128.0), (14, 160.0))
]


@pytest.fixture()
def fmp_export(monkeypatch):
    monkeypatch.setattr(FMPRating, "_fetch_price_target_consensus",
                        lambda self, symbol: _CONSENSUS)
    monkeypatch.setattr(FMPRating, "_fetch_upgrade_downgrade",
                        lambda self, symbol: _UPGRADE)
    monkeypatch.setattr(FMPRating, "_fetch_price_target_history",
                        lambda self, symbol: _TARGET_HISTORY)
    monkeypatch.setattr(FMPRating, "_get_current_price", lambda self, symbol: 100.0)
    result = FMPRating.export_symbol_data("AAPL", overrides=_FMP_SETTINGS)
    assert result.error is None, result.error
    return result


def _row(export, label):
    return next(m for m in export.metrics if m.label == label)


def _tbl(metric):
    return dict(metric.detail_table or [])


def test_the_analyst_buckets_are_structured_rows_not_a_sentence(fmp_export):
    table = _tbl(_row(fmp_export, "Total analysts"))
    assert table["Strong Buy"] == "10"
    assert table["Buy"] == "5"
    assert table["Hold"] == "3"
    assert table["Sell"] == "1"
    assert table["Strong Sell"] == "1"


def test_the_bucket_counts_sum_to_the_displayed_total(fmp_export):
    row = _row(fmp_export, "Total analysts")
    table = _tbl(row)
    buckets = ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell")
    assert sum(int(table[b]) for b in buckets) == int(row.display)


def test_the_price_target_set_is_a_table_with_the_percent_from_current(fmp_export):
    table = _tbl(_row(fmp_export, "Consensus price target"))
    assert table["Consensus"].startswith("$130.00")
    assert "+30.0%" in table["Consensus"]
    assert table["High"].startswith("$160.00") and "+60.0%" in table["High"]
    assert table["Low"].startswith("$110.00") and "+10.0%" in table["Low"]
    assert table["Median"].startswith("$128.00") and "+28.0%" in table["Median"]
    assert table["Current price"] == "$100.00"


def test_a_missing_price_target_is_reported_as_n_a_not_as_zero(monkeypatch):
    monkeypatch.setattr(FMPRating, "_fetch_price_target_consensus",
                        lambda self, symbol: {"targetConsensus": 130.0,
                                              "targetLow": 110.0})
    monkeypatch.setattr(FMPRating, "_fetch_upgrade_downgrade",
                        lambda self, symbol: _UPGRADE)
    monkeypatch.setattr(FMPRating, "_fetch_price_target_history",
                        lambda self, symbol: _TARGET_HISTORY)
    monkeypatch.setattr(FMPRating, "_get_current_price", lambda self, symbol: 100.0)
    result = FMPRating.export_symbol_data("AAPL", overrides=_FMP_SETTINGS)
    assert result.error is None, result.error
    table = _tbl(_row(result, "Consensus price target"))
    assert table["High"] == "n/a"
    assert "$0.00" not in table["High"]


def test_the_long_derivation_is_still_carried_as_multi_line_steps(fmp_export):
    """The 4-step confidence derivation must survive intact -- the fix is
    where it is DRAWN, not that it is discarded."""
    rec_row = _row(fmp_export, "Recommendation")
    assert "Step 1" in rec_row.detail
    assert "Step 4" in rec_row.detail
    assert rec_row.detail.count("\n") > 10


# ============================= FinnHubRating ==============================

_FINNHUB_SETTINGS = {"buy_threshold": 4.5, "overweight_threshold": 3.5,
                     "hold_threshold": 2.5, "underweight_threshold": 1.5}
_TRENDS = [{"period": "2026-08-01", "strongBuy": 0, "buy": 7, "hold": 6,
            "sell": 3, "strongSell": 0}]


class _FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [110.35]})


@pytest.fixture()
def finnhub_export(monkeypatch):
    monkeypatch.setattr(FinnHubRating, "_fetch_recommendation_trends",
                        lambda self, symbol: _TRENDS)
    ohlcv = _FakeOHLCV()
    result = FinnHubRating.export_symbol_data(
        "AAPL", overrides=_FINNHUB_SETTINGS,
        providers_resolver=lambda cat, name, **kw: ohlcv)
    assert result.error is None, result.error
    return result


def test_finnhub_carries_its_bucket_counts_through_to_the_export(finnhub_export):
    """_process used to record mean/total/period and DROP counts, making the
    breakdown unrecoverable at render time."""
    assert finnhub_export.raw["counts"] == {"strongBuy": 0, "buy": 7, "hold": 6,
                                            "sell": 3, "strongSell": 0}


def test_finnhub_shows_the_buckets_as_a_table(finnhub_export):
    table = _tbl(_row(finnhub_export, "Analysts"))
    assert table["Strong Buy"] == "0"
    assert table["Buy"] == "7"
    assert table["Hold"] == "6"
    assert table["Sell"] == "3"
    assert table["Strong Sell"] == "0"


def test_finnhub_keeps_the_reporting_period_visible(finnhub_export):
    assert _tbl(_row(finnhub_export, "Analysts"))["Period"] == "2026-08-01"


def test_finnhub_bucket_counts_sum_to_the_displayed_total(finnhub_export):
    row = _row(finnhub_export, "Analysts")
    table = _tbl(row)
    buckets = ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell")
    assert sum(int(table[b]) for b in buckets) == int(row.display)


def test_finnhub_with_no_trends_reports_no_counts_rather_than_five_zeros(monkeypatch):
    """The no-data fallback really has NO buckets; five zeros would be a
    fabricated 'every analyst is neutral' reading."""
    monkeypatch.setattr(FinnHubRating, "_fetch_recommendation_trends",
                        lambda self, symbol: [])
    ohlcv = _FakeOHLCV()
    result = FinnHubRating.export_symbol_data(
        "AAPL", overrides=_FINNHUB_SETTINGS,
        providers_resolver=lambda cat, name, **kw: ohlcv)
    assert result.error is None, result.error
    row = _row(result, "Analysts")
    assert row.display == "0"
    assert not row.detail_table

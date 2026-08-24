"""FMPRating.export_symbol_data: analyst-consensus metric rows.

Fixtures (CONSENSUS/UPGRADE) are duplicated from test_fmp_rating_gather_process.py
rather than imported -- this mirrors test_deterministic_scorer_export.py's /
test_insider_cluster_export.py's convention of local fixtures per *_export.py file.

FMPRating's live (as_of=None) _gather reads FMP data via direct instance-method
fetchers (_fetch_price_target_consensus/_fetch_upgrade_downgrade/
_fetch_price_target_history), not the providers registry (see the module
docstring: "the FMP endpoints stay direct, not in the get_provider registry"),
and its current_price comes from _get_current_price (an account/DB lookup), not
an OHLCV provider. export_symbol_data bypass-constructs the instance internally,
so these are monkeypatched at the CLASS level (unbound, taking self) for the
duration of each test -- the providers_resolver injection point is unused here
since none of FMPRating's live-path fetchers go through it.

SYMBOL360 always calls export_symbol_data with as_of=None (the live path); the
as_of reconstruction path is exercised separately in
test_fmp_rating_gather_process.py and is not re-tested here.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_experts.FMPRating import FMPRating

SETTINGS = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus"}

# Consensus with upside above current (pinned current_price=100) -> BUY territory.
CONSENSUS = {"targetConsensus": 130.0, "targetHigh": 160.0,
             "targetLow": 110.0, "targetMedian": 128.0}

# buy_score = 10*2 + 5 = 25 ; sell_score = 1*2 + 1 = 3 ; hold_score = 3 -> BUY.
UPGRADE = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]

# Dates relative to "now" (not hardcoded) so the fixture stays inside the live
# path's real trailing-quarter window (_QUARTER_DAYS=90, measured against
# datetime.now(timezone.utc)) regardless of when the test runs -- absolute
# dates would age out of the window and silently flip these tests into the
# insufficient-price-targets skip path a few months out.
_NOW = datetime.now(timezone.utc)
PRICE_TARGET_HISTORY = [
    {"publishedDate": (_NOW - timedelta(days=10)).strftime("%Y-%m-%d"), "priceTarget": 110.0},
    {"publishedDate": (_NOW - timedelta(days=11)).strftime("%Y-%m-%d"), "priceTarget": 124.0},
    {"publishedDate": (_NOW - timedelta(days=12)).strftime("%Y-%m-%d"), "priceTarget": 128.0},
    {"publishedDate": (_NOW - timedelta(days=13)).strftime("%Y-%m-%d"), "priceTarget": 128.0},
    {"publishedDate": (_NOW - timedelta(days=14)).strftime("%Y-%m-%d"), "priceTarget": 160.0},
]


def _patch_live_fetchers(monkeypatch, consensus=CONSENSUS, upgrade=UPGRADE,
                         price_target_history=PRICE_TARGET_HISTORY, current_price=100.0):
    monkeypatch.setattr(FMPRating, "_fetch_price_target_consensus",
                        lambda self, symbol: consensus)
    monkeypatch.setattr(FMPRating, "_fetch_upgrade_downgrade",
                        lambda self, symbol: upgrade)
    monkeypatch.setattr(FMPRating, "_fetch_price_target_history",
                        lambda self, symbol: price_target_history)
    monkeypatch.setattr(FMPRating, "_get_current_price",
                        lambda self, symbol: current_price)


def test_export_symbol_data_buy_shows_analyst_consensus_rows(monkeypatch):
    _patch_live_fetchers(monkeypatch)
    result = FMPRating.export_symbol_data("AAPL", overrides=SETTINGS)
    assert result.error is None, result.error
    assert result.overall_signal == "buy"
    assert result.skipped is False

    labels = {m.label for m in result.metrics}
    assert "Recommendation" in labels
    assert "Analyst target upside" in labels
    assert "Consensus price target" in labels
    assert "Total analysts" in labels

    upside = next(m for m in result.metrics if m.label == "Analyst target upside")
    # current=100, target_price (consensus)=130 -> +30.0% upside.
    assert upside.value == pytest.approx(30.0)
    assert upside.display == "+30.0%"
    assert upside.signal == "buy"
    assert "consensus" in upside.detail

    consensus_row = next(m for m in result.metrics if m.label == "Consensus price target")
    assert consensus_row.value == 130.0
    assert consensus_row.display == "$130.00"

    total = next(m for m in result.metrics if m.label == "Total analysts")
    assert total.value == 20
    assert total.display == "20"
    # The bucket breakdown moved from a prose `detail` into structured
    # (label, value) rows the card draws as a small table -- see
    # test_analyst_card_breakdown.py.
    assert dict(total.detail_table) == {
        "Strong Buy": "10", "Buy": "5", "Hold": "3", "Sell": "1", "Strong Sell": "1"}


def test_export_symbol_data_sell_flips_upside_signal(monkeypatch):
    # Sell-dominated ratings, target below current -> SELL with negative upside.
    sell_upgrade = [{"strongBuy": 0, "buy": 1, "hold": 1, "sell": 5, "strongSell": 10}]
    sell_consensus = {"targetConsensus": 70.0, "targetHigh": 90.0,
                      "targetLow": 50.0, "targetMedian": 68.0}
    _patch_live_fetchers(monkeypatch, consensus=sell_consensus, upgrade=sell_upgrade)
    result = FMPRating.export_symbol_data("AAPL", overrides=SETTINGS)
    assert result.error is None, result.error
    assert result.overall_signal == "sell"

    upside = next(m for m in result.metrics if m.label == "Analyst target upside")
    assert upside.value == pytest.approx(-30.0)
    assert upside.signal == "sell"


def test_export_symbol_data_skip_no_coverage_has_no_extra_rows(monkeypatch):
    """No consensus data -> _process's first skip site (skip_reason='no consensus
    data'). Must surface as the single base 'Skipped' row with none of the
    analyst-consensus rows leaking through."""
    _patch_live_fetchers(monkeypatch, consensus=None)
    result = FMPRating.export_symbol_data("AAPL", overrides=SETTINGS)
    assert result.error is None, result.error
    assert result.skipped is True
    assert len(result.metrics) == 1
    assert result.metrics[0].label == "Skipped"
    assert result.metrics[0].detail == "no consensus data"

    labels = {m.label for m in result.metrics}
    assert "Analyst target upside" not in labels
    assert "Consensus price target" not in labels
    assert "Total analysts" not in labels


def test_export_symbol_data_skip_insufficient_price_targets_has_no_extra_rows(monkeypatch):
    """Fewer than min_price_targets_per_quarter (default 3) analyst targets
    behind the consensus -> _process's second skip site (skip_reason=
    'insufficient price targets'). Same single-row 'Skipped' contract. Dates
    are relative to "now" (not hardcoded) so the fixture stays inside the
    live path's real trailing-quarter window regardless of when the test runs."""
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    thin_targets = [{"publishedDate": recent.strftime("%Y-%m-%d"), "priceTarget": 130.0}]
    _patch_live_fetchers(monkeypatch, price_target_history=thin_targets)
    result = FMPRating.export_symbol_data("AAPL", overrides=SETTINGS)
    assert result.error is None, result.error
    assert result.skipped is True
    assert len(result.metrics) == 1
    assert result.metrics[0].label == "Skipped"
    assert result.metrics[0].detail == "insufficient price targets"

    labels = {m.label for m in result.metrics}
    assert "Analyst target upside" not in labels


def test_export_symbol_data_skip_insufficient_analysts_has_no_extra_rows(monkeypatch):
    """Below min_analysts -> _process's third skip site (skip_reason=
    'insufficient analysts'). Same single-row 'Skipped' contract."""
    thin_upgrade = [{"strongBuy": 2, "buy": 1, "hold": 1, "sell": 0, "strongSell": 0}]  # 4 < 10
    _patch_live_fetchers(monkeypatch, upgrade=thin_upgrade)
    result = FMPRating.export_symbol_data("AAPL", overrides=SETTINGS)
    assert result.error is None, result.error
    assert result.skipped is True
    assert len(result.metrics) == 1
    assert result.metrics[0].label == "Skipped"
    assert result.metrics[0].detail == "insufficient analysts"

    labels = {m.label for m in result.metrics}
    assert "Analyst target upside" not in labels

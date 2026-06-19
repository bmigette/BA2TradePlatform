"""Phase 1 Task 8: FinnHubRating _gather/_process split + as_of period selection.

Proves: _process is pure (no provider/API reads), the LOOKAHEAD BUG FIX (period
selection is no longer trends_data[0] but the latest period whose date <= as_of),
as_of=None is byte-identical to the old trends_data[0] behaviour, and analyze_as_of
runs the SAME _gather+_process as the live path (the golden-test logic-equality
contract). Finnhub stays a direct API (not in the get_provider registry); only the
current price is routed through the providers bundle.
"""
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FinnHubRating import FinnHubRating, consensus_from_counts
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)

# thresholds resolved as a plain settings dict (defaults), as run_analysis builds it
SETTINGS = {"buy_threshold": 4.5, "overweight_threshold": 3.5,
            "hold_threshold": 2.5, "underweight_threshold": 1.5}

# Finnhub returns periods NEWEST-FIRST. The newest period (2026-07-01) is in the
# FUTURE relative to NOW (2026-06-15) and would leak under the old trends_data[0].
# Each period has a distinct consensus so the SELECTED period is observable:
#   2026-07-01 -> all strongSell  -> SELL  (the lookahead leak we must NOT pick)
#   2026-06-01 -> all strongBuy   -> BUY   (the correct as_of pick)
#   2026-05-01 -> all hold        -> HOLD
TRENDS = [
    {"period": "2026-07-01", "strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 10},
    {"period": "2026-06-01", "strongBuy": 10, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0},
    {"period": "2026-05-01", "strongBuy": 0, "buy": 0, "hold": 10, "sell": 0, "strongSell": 0},
]


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [123.0]})


def _provider_resolver():
    ohlcv = FakeOHLCV()
    return lambda cat, name, **kw: ohlcv


def _expert(trends=TRENDS):
    e = FinnHubRating.__new__(FinnHubRating)
    e.id = 1
    e._gather_symbol = "AAPL"
    # Finnhub is a direct API; stub the trends fetch so _gather has no network.
    e._fetch_recommendation_trends = lambda symbol: trends
    return e


def test_select_period_asof_picks_latest_on_or_before():
    """LOOKAHEAD FIX: with as_of set, pick the latest period <= as_of, NOT trends[0]."""
    picked = FinnHubRating._select_period(TRENDS, NOW)
    assert picked is not None
    assert picked["period"] == "2026-06-01", "must skip the future 2026-07-01 leak"


def test_select_period_live_is_trends0():
    """as_of=None is byte-identical to the old trends_data[0] behaviour."""
    assert FinnHubRating._select_period(TRENDS, None) is TRENDS[0]


def test_process_asof_buy_from_selected_period():
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    # 2026-06-01 is all strongBuy -> BUY (NOT the future SELL period)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.current_price == 123.0
    assert rec.expected_profit_percent == 0.0   # FinnHub gives no price targets
    assert "Period: 2026-06-01" in rec.details
    assert rec.raw_outputs["period"] == "2026-06-01"
    assert rec.raw_outputs["type"] == "finnhub_rating_analysis"


def test_process_live_picks_trends0_sell():
    """With as_of=None, _process selects trends[0] (the 2026-07-01 SELL period)."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.signal == OrderRecommendation.SELL
    assert "Period: 2026-07-01" in rec.details


def test_process_no_eligible_period_holds():
    """as_of before EVERY period => no eligible period => HOLD with the no-data details."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()),
                       as_of=datetime(2026, 1, 1, tzinfo=timezone.utc))
    rec = e._process(bundle, SETTINGS, as_of=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.confidence == 0.0
    assert rec.details == "No recommendation data available"


def test_process_byte_equal_details_vs_direct_calculate():
    """_process details must be byte-equal to a direct _calculate_recommendation call
    on the same selected period (proves no detail-string drift in the refactor)."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=NOW)
    rec = e._process(bundle, SETTINGS, as_of=NOW)
    thresholds = {"buy": 4.5, "overweight": 3.5, "hold": 2.5, "underweight": 1.5}
    direct = e._calculate_recommendation(TRENDS, thresholds, as_of=NOW)
    assert rec.details == direct["details"]
    assert rec.signal == direct["signal"]
    assert round(direct["confidence"], 1) == rec.confidence


def test_analyze_as_of_equals_live_when_latest_is_on_or_before():
    """Golden contract: when trends[0] date <= as_of, analyze_as_of(now) ==
    _process(_gather(live, None)) on (signal, confidence, details)."""
    # A clean fixture whose newest period is on/before NOW so both paths select it.
    clean_trends = [
        {"period": "2026-06-01", "strongBuy": 10, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0},
        {"period": "2026-05-01", "strongBuy": 0, "buy": 0, "hold": 10, "sell": 0, "strongSell": 0},
    ]
    e = _expert(clean_trends)
    ctx = BacktestContext(
        providers=LiveProviderBundle(_provider_resolver()),
        settings=SETTINGS, as_of=NOW, extra={"symbol": "AAPL"})
    rec_asof = e.analyze_as_of(NOW, ctx)

    bundle_live = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec_live = e._process(bundle_live, SETTINGS, as_of=None)

    rec_asof.current_price = rec_live.current_price  # pin price source per golden harness
    assert rec_asof.almost_equals(rec_live)
    assert rec_asof.signal == OrderRecommendation.BUY
    assert rec_asof.details == rec_live.details


def test_consensus_from_counts_still_pure():
    """The shared pure helper is untouched (value parity guard)."""
    r = consensus_from_counts({"strongBuy": 10, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0})
    assert r["signal"] == OrderRecommendation.BUY
    assert r["mean"] == 5.0
    assert r["total"] == 10

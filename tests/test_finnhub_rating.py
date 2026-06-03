"""Tests for FinnHubRating's 5-grade consensus-mean mapping (pure function)."""
import pytest

from ba2_trade_platform.modules.experts.FinnHubRating import consensus_from_counts
from ba2_trade_platform.core.types import OrderRecommendation


class TestConsensusFromCounts:
    def test_strong_buy_consensus_maps_to_buy(self):
        # mean = (5*8 + 4*2)/10 = 4.8 -> BUY
        r = consensus_from_counts({"strongBuy": 8, "buy": 2})
        assert r["signal"] == OrderRecommendation.BUY
        assert r["confidence"] == pytest.approx(100.0)
        assert r["mean"] == pytest.approx(4.8)

    def test_buy_leaning_maps_to_overweight(self):
        # mean = (5*2 + 4*6 + 3*2)/10 = 4.0 -> OVERWEIGHT; agreement = (2+6)/10
        r = consensus_from_counts({"strongBuy": 2, "buy": 6, "hold": 2})
        assert r["signal"] == OrderRecommendation.OVERWEIGHT
        assert r["confidence"] == pytest.approx(80.0)

    def test_all_hold_maps_to_hold(self):
        r = consensus_from_counts({"hold": 10})
        assert r["signal"] == OrderRecommendation.HOLD
        assert r["confidence"] == pytest.approx(100.0)
        assert r["mean"] == pytest.approx(3.0)

    def test_sell_leaning_maps_to_underweight(self):
        # mean = (3*2 + 2*6 + 1*2)/10 = 2.0 -> UNDERWEIGHT; agreement = (6+2)/10
        r = consensus_from_counts({"hold": 2, "sell": 6, "strongSell": 2})
        assert r["signal"] == OrderRecommendation.UNDERWEIGHT
        assert r["confidence"] == pytest.approx(80.0)

    def test_strong_sell_consensus_maps_to_sell(self):
        # mean = (2*2 + 1*8)/10 = 1.2 -> SELL
        r = consensus_from_counts({"sell": 2, "strongSell": 8})
        assert r["signal"] == OrderRecommendation.SELL
        assert r["confidence"] == pytest.approx(100.0)

    def test_no_analysts_maps_to_hold_zero_confidence(self):
        r = consensus_from_counts({})
        assert r["signal"] == OrderRecommendation.HOLD
        assert r["confidence"] == 0.0
        assert r["total"] == 0

    def test_boundary_45_is_buy(self):
        # mean exactly 4.5 -> BUY (>= buy threshold)
        r = consensus_from_counts({"strongBuy": 5, "buy": 5})
        assert r["mean"] == pytest.approx(4.5)
        assert r["signal"] == OrderRecommendation.BUY

    def test_custom_thresholds_shift_grade(self):
        # mean 4.0; with buy threshold lowered to 4.0 it becomes BUY
        r = consensus_from_counts(
            {"strongBuy": 2, "buy": 6, "hold": 2},
            thresholds={"buy": 4.0, "overweight": 3.5, "hold": 2.5, "underweight": 1.5},
        )
        assert r["signal"] == OrderRecommendation.BUY

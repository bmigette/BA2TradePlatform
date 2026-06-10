"""EX-1 regression tests: FMPRating price-target boost must be direction-aware.

Bug: the boost was the signed % distance from price to target, added to
confidence regardless of signal. For SELL with price ABOVE the targets (the
strongest short thesis), that *reduced* confidence. The boost must be oriented
by signal: BUY adds upside, SELL adds downside, HOLD gets none.
"""
from unittest.mock import patch

from ba2_trade_platform.core.types import OrderRecommendation
from tests.factories import create_account_definition, create_expert_instance


def _make_expert():
    acct_def = create_account_definition()
    ei = create_expert_instance(account_id=acct_def.id, expert="FMPRating")
    with patch("ba2_trade_platform.modules.experts.FMPRating.get_app_setting", return_value="fake_key"), \
         patch("ba2_trade_platform.modules.experts.FMPRating.get_expert_logger"):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        expert = FMPRating(ei.id)
    return expert


def _calc(consensus, upgrade, current_price):
    return _make_expert()._calculate_recommendation(consensus, upgrade, current_price, 1.0, 3)


BEARISH = [{"strongBuy": 0, "buy": 0, "hold": 2, "sell": 5, "strongSell": 10}]
BULLISH = [{"strongBuy": 10, "buy": 5, "hold": 2, "sell": 0, "strongSell": 0}]


class TestSellBoostDirection:
    def test_sell_with_price_above_targets_increases_confidence(self):
        # current price ABOVE all targets => strong short thesis => boost must ADD.
        consensus = {"targetConsensus": 80.0, "targetHigh": 95.0, "targetLow": 60.0, "targetMedian": 78.0}
        r = _calc(consensus, BEARISH, current_price=100.0)
        assert r["signal"] == OrderRecommendation.SELL
        assert r["confidence"] > r["base_confidence"]

    def test_sell_more_downside_means_higher_confidence(self):
        far = _calc({"targetConsensus": 70.0, "targetHigh": 90.0, "targetLow": 55.0, "targetMedian": 68.0}, BEARISH, 100.0)
        near = _calc({"targetConsensus": 98.0, "targetHigh": 110.0, "targetLow": 95.0, "targetMedian": 97.0}, BEARISH, 100.0)
        assert far["signal"] == near["signal"] == OrderRecommendation.SELL
        assert far["confidence"] >= near["confidence"]
        if far["confidence"] < 100.0:  # strictly greater unless clamped
            assert far["confidence"] > near["confidence"]


class TestBuyBoostDirection:
    def test_buy_with_price_below_targets_increases_confidence(self):
        consensus = {"targetConsensus": 130.0, "targetHigh": 160.0, "targetLow": 115.0, "targetMedian": 128.0}
        r = _calc(consensus, BULLISH, current_price=100.0)
        assert r["signal"] == OrderRecommendation.BUY
        assert r["confidence"] > r["base_confidence"]

    def test_buy_more_upside_means_higher_confidence(self):
        far = _calc({"targetConsensus": 160.0, "targetHigh": 200.0, "targetLow": 140.0, "targetMedian": 158.0}, BULLISH, 100.0)
        near = _calc({"targetConsensus": 103.0, "targetHigh": 115.0, "targetLow": 101.0, "targetMedian": 102.0}, BULLISH, 100.0)
        assert far["signal"] == near["signal"] == OrderRecommendation.BUY
        if far["confidence"] < 100.0:
            assert far["confidence"] > near["confidence"]

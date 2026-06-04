"""Tests for FinnHubRating expert — 5-grade consensus mapping via the expert
instance (no API calls). Pure mapping logic is covered in tests/test_finnhub_rating.py."""
import pytest
from unittest.mock import patch
from ba2_trade_platform.core.types import OrderRecommendation
from tests.factories import create_account_definition, create_expert_instance


def _make_expert():
    """Create a FinnHubRating instance with mocked DB/API dependencies."""
    acct_def = create_account_definition()
    ei = create_expert_instance(account_id=acct_def.id, expert="FinnHubRating")

    with patch("ba2_trade_platform.modules.experts.FinnHubRating.get_setting", return_value="fake_key"), \
         patch("ba2_trade_platform.modules.experts.FinnHubRating.get_expert_logger"):
        from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
        expert = FinnHubRating(ei.id)
    return expert


class TestCalculateRecommendation:
    def _calc(self, trends):
        # thresholds=None -> default 5-grade buckets
        return _make_expert()._calculate_recommendation(trends)

    def test_strong_buy_consensus_is_buy(self):
        trends = [{"strongBuy": 15, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0, "period": "2024-01"}]
        result = self._calc(trends)
        assert result["signal"] == OrderRecommendation.BUY
        assert result["confidence"] > 50.0

    def test_buy_leaning_consensus_is_overweight(self):
        trends = [{"strongBuy": 2, "buy": 6, "hold": 2, "sell": 0, "strongSell": 0, "period": "2024-01"}]
        assert self._calc(trends)["signal"] == OrderRecommendation.OVERWEIGHT

    def test_strong_sell_consensus_is_sell(self):
        trends = [{"strongBuy": 0, "buy": 0, "hold": 1, "sell": 3, "strongSell": 15, "period": "2024-01"}]
        assert self._calc(trends)["signal"] == OrderRecommendation.SELL

    def test_sell_leaning_consensus_is_underweight(self):
        trends = [{"strongBuy": 0, "buy": 0, "hold": 2, "sell": 6, "strongSell": 2, "period": "2024-01"}]
        assert self._calc(trends)["signal"] == OrderRecommendation.UNDERWEIGHT

    def test_hold_signal_when_neutral(self):
        trends = [{"strongBuy": 1, "buy": 1, "hold": 20, "sell": 1, "strongSell": 1, "period": "2024-01"}]
        assert self._calc(trends)["signal"] == OrderRecommendation.HOLD

    def test_empty_trends_returns_hold(self):
        result = self._calc([])
        assert result["signal"] == OrderRecommendation.HOLD
        assert result["confidence"] == 0.0

    def test_none_trends_returns_hold(self):
        assert self._calc(None)["signal"] == OrderRecommendation.HOLD

    def test_confidence_is_percentage(self):
        trends = [{"strongBuy": 10, "buy": 5, "hold": 0, "sell": 0, "strongSell": 0, "period": "2024-01"}]
        assert 0.0 <= self._calc(trends)["confidence"] <= 100.0


class TestRatingThresholds:
    def test_get_rating_thresholds_returns_defaults(self):
        from ba2_trade_platform.modules.experts.FinnHubRating import DEFAULT_RATING_THRESHOLDS
        expert = _make_expert()
        thresholds = expert._get_rating_thresholds()
        assert thresholds == {k: float(v) for k, v in DEFAULT_RATING_THRESHOLDS.items()}


class TestSettingsDefinitions:
    def test_has_threshold_settings(self):
        from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
        defs = FinnHubRating.get_settings_definitions()
        assert "buy_threshold" in defs
        assert defs["buy_threshold"]["type"] == "float"
        assert "strong_factor" not in defs

    def test_description_not_empty(self):
        from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
        assert len(FinnHubRating.description()) > 0

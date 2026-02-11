"""Tests for FinnHubRating expert — pure calculation logic (no API calls)."""
import pytest
from unittest.mock import patch, MagicMock
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
    def _calc(self, trends, strong_factor=2.0):
        expert = _make_expert()
        return expert._calculate_recommendation(trends, strong_factor)

    def test_buy_signal_when_bullish(self):
        trends = [{"strongBuy": 10, "buy": 5, "hold": 2, "sell": 1, "strongSell": 0, "period": "2024-01"}]
        result = self._calc(trends)
        assert result["signal"] == OrderRecommendation.BUY
        assert result["confidence"] > 50.0

    def test_sell_signal_when_bearish(self):
        trends = [{"strongBuy": 0, "buy": 1, "hold": 2, "sell": 5, "strongSell": 10, "period": "2024-01"}]
        result = self._calc(trends)
        assert result["signal"] == OrderRecommendation.SELL

    def test_hold_signal_when_neutral(self):
        trends = [{"strongBuy": 1, "buy": 1, "hold": 20, "sell": 1, "strongSell": 1, "period": "2024-01"}]
        result = self._calc(trends)
        assert result["signal"] == OrderRecommendation.HOLD

    def test_empty_trends_returns_hold(self):
        result = self._calc([])
        assert result["signal"] == OrderRecommendation.HOLD
        assert result["confidence"] == 0.0

    def test_none_trends_returns_hold(self):
        result = self._calc(None)
        assert result["signal"] == OrderRecommendation.HOLD

    def test_strong_factor_affects_scores(self):
        trends = [{"strongBuy": 5, "buy": 0, "hold": 0, "sell": 0, "strongSell": 5, "period": "2024-01"}]
        # With default factor 2.0, buy_score = 10, sell_score = 10 -> HOLD
        result = self._calc(trends, strong_factor=2.0)
        assert result["buy_score"] == result["sell_score"]

        # With factor 3.0, still equal
        result2 = self._calc(trends, strong_factor=3.0)
        assert result2["buy_score"] == 15.0
        assert result2["sell_score"] == 15.0

    def test_confidence_is_percentage(self):
        trends = [{"strongBuy": 10, "buy": 5, "hold": 0, "sell": 0, "strongSell": 0, "period": "2024-01"}]
        result = self._calc(trends)
        assert 0.0 <= result["confidence"] <= 100.0

    def test_weighted_scores_calculated_correctly(self):
        trends = [{"strongBuy": 3, "buy": 4, "hold": 2, "sell": 1, "strongSell": 0, "period": "2024-01"}]
        result = self._calc(trends, strong_factor=2.0)
        assert result["buy_score"] == pytest.approx(3 * 2.0 + 4)  # 10.0
        assert result["sell_score"] == pytest.approx(0 * 2.0 + 1)  # 1.0
        assert result["hold_score"] == 2


class TestSettingsDefinitions:
    def test_has_strong_factor(self):
        from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
        defs = FinnHubRating.get_settings_definitions()
        assert "strong_factor" in defs
        assert defs["strong_factor"]["type"] == "float"

    def test_description_not_empty(self):
        from ba2_trade_platform.modules.experts.FinnHubRating import FinnHubRating
        assert len(FinnHubRating.description()) > 0

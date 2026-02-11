"""Tests for FMPRating expert — pure calculation logic (no API calls)."""
import pytest
from unittest.mock import patch, MagicMock
from ba2_trade_platform.core.types import OrderRecommendation
from tests.factories import create_account_definition, create_expert_instance


def _make_expert():
    """Create an FMPRating instance with mocked DB/API dependencies."""
    acct_def = create_account_definition()
    ei = create_expert_instance(account_id=acct_def.id, expert="FMPRating")

    with patch("ba2_trade_platform.modules.experts.FMPRating.get_app_setting", return_value="fake_key"), \
         patch("ba2_trade_platform.modules.experts.FMPRating.get_expert_logger"):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        expert = FMPRating(ei.id)
    return expert


class TestCalculateRecommendation:
    def _calc(self, consensus_data, upgrade_data, current_price=150.0,
              profit_ratio=1.0, min_analysts=3):
        expert = _make_expert()
        return expert._calculate_recommendation(
            consensus_data, upgrade_data, current_price, profit_ratio, min_analysts
        )

    def test_buy_signal_with_bullish_consensus(self):
        consensus = {
            "targetConsensus": 200.0, "targetHigh": 250.0,
            "targetLow": 180.0, "targetMedian": 195.0,
        }
        upgrade = [{"strongBuy": 10, "buy": 5, "hold": 2, "sell": 0, "strongSell": 0}]
        result = self._calc(consensus, upgrade)
        assert result["signal"] == OrderRecommendation.BUY

    def test_sell_signal_with_bearish_consensus(self):
        consensus = {
            "targetConsensus": 100.0, "targetHigh": 120.0,
            "targetLow": 80.0, "targetMedian": 95.0,
        }
        upgrade = [{"strongBuy": 0, "buy": 0, "hold": 2, "sell": 5, "strongSell": 10}]
        result = self._calc(consensus, upgrade)
        assert result["signal"] == OrderRecommendation.SELL

    def test_hold_when_no_consensus_data(self):
        result = self._calc(None, None)
        assert result["signal"] == OrderRecommendation.HOLD
        assert result["confidence"] == 0.0

    def test_hold_when_insufficient_analysts(self):
        consensus = {
            "targetConsensus": 200.0, "targetHigh": 250.0,
            "targetLow": 180.0, "targetMedian": 195.0,
        }
        upgrade = [{"strongBuy": 1, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}]
        result = self._calc(consensus, upgrade, min_analysts=5)
        assert result["signal"] == OrderRecommendation.HOLD
        assert result["confidence"] == 20.0  # Low confidence

    def test_expected_profit_positive_for_buy(self):
        consensus = {
            "targetConsensus": 200.0, "targetHigh": 250.0,
            "targetLow": 180.0, "targetMedian": 195.0,
        }
        upgrade = [{"strongBuy": 10, "buy": 5, "hold": 2, "sell": 0, "strongSell": 0}]
        result = self._calc(consensus, upgrade, current_price=150.0)
        assert result["expected_profit_percent"] > 0

    def test_confidence_clamped_to_0_100(self):
        consensus = {
            "targetConsensus": 500.0, "targetHigh": 1000.0,
            "targetLow": 400.0, "targetMedian": 450.0,
        }
        upgrade = [{"strongBuy": 50, "buy": 20, "hold": 0, "sell": 0, "strongSell": 0}]
        result = self._calc(consensus, upgrade, current_price=100.0)
        assert 0.0 <= result["confidence"] <= 100.0

    def test_analyst_count_from_upgrade_data(self):
        consensus = {
            "targetConsensus": 200.0, "targetHigh": 250.0,
            "targetLow": 180.0, "targetMedian": 195.0,
        }
        upgrade = [{"strongBuy": 5, "buy": 3, "hold": 2, "sell": 1, "strongSell": 0}]
        result = self._calc(consensus, upgrade)
        assert result["analyst_count"] == 11


class TestSettingsDefinitions:
    def test_has_profit_ratio(self):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        defs = FMPRating.get_settings_definitions()
        assert "profit_ratio" in defs

    def test_has_min_analysts(self):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        defs = FMPRating.get_settings_definitions()
        assert "min_analysts" in defs

    def test_has_target_price_type(self):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        defs = FMPRating.get_settings_definitions()
        assert "target_price_type" in defs

    def test_description_not_empty(self):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        assert len(FMPRating.description()) > 0

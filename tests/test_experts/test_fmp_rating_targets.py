"""Tests that FMPRating persists analyst price targets into ExpertRecommendation.data.

These targets (especially target_consensus) are read by the option
`consensus_target` strike-selection method, so they must survive into the
persisted recommendation under data["FMPRating"].

The test exercises the builder seam (_create_expert_recommendation) directly with
a synthetic recommendation_data dict, so NO external FMP API call is made.
"""
import pytest
from unittest.mock import patch

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertRecommendation
from ba2_trade_platform.core.types import OrderRecommendation
from tests.factories import create_account_definition, create_expert_instance


def _make_expert():
    """Create an FMPRating instance with mocked settings/logger (no network)."""
    acct_def = create_account_definition()
    ei = create_expert_instance(account_id=acct_def.id, expert="FMPRating")

    with patch("ba2_trade_platform.modules.experts.FMPRating.get_app_setting", return_value="fake_key"), \
         patch("ba2_trade_platform.modules.experts.FMPRating.get_expert_logger"):
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        expert = FMPRating(ei.id)
    return expert


def _recommendation_data():
    """A minimal recommendation_data dict as produced by _calculate_recommendation."""
    return {
        "signal": OrderRecommendation.BUY,
        "confidence": 72.5,
        "expected_profit_percent": 18.4,
        "details": "Synthetic FMP details for test.",
        "target_consensus": 200.0,
        "target_high": 250.0,
        "target_low": 180.0,
        "target_median": 195.0,
        "analyst_count": 12,
        "target_price": 200.0,
    }


class TestPersistAnalystTargets:
    def test_target_consensus_persisted_under_fmprating(self):
        expert = _make_expert()
        rec_id = expert._create_expert_recommendation(
            _recommendation_data(), symbol="AAPL",
            market_analysis_id=None, current_price=150.0,
        )

        rec = get_instance(ExpertRecommendation, rec_id)
        assert rec.data is not None, "ExpertRecommendation.data was not persisted"
        assert "FMPRating" in rec.data
        assert rec.data["FMPRating"]["target_consensus"] == 200.0

    def test_all_targets_persisted(self):
        expert = _make_expert()
        rec_id = expert._create_expert_recommendation(
            _recommendation_data(), symbol="AAPL",
            market_analysis_id=None, current_price=150.0,
        )

        rec = get_instance(ExpertRecommendation, rec_id)
        fmp = rec.data["FMPRating"]
        assert fmp["target_consensus"] == 200.0
        assert fmp["target_high"] == 250.0
        assert fmp["target_low"] == 180.0
        assert fmp["target_median"] == 195.0

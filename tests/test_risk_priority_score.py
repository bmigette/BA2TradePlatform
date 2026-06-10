"""RM-3 / EX-5: confidence-aware order prioritization scoring (pure function)."""
from ba2_trade_platform.core.TradeRiskManagement import compute_order_priority_score as score


class TestProfitWeightedByConfidence:
    def test_confidence_breaks_ties_in_favor_of_conviction(self):
        # 12% profit @ 40% conf (=4.8) should rank BELOW 10% profit @ 85% conf (=8.5)
        assert score(12.0, 40.0) < score(10.0, 85.0)

    def test_higher_profit_same_confidence_ranks_higher(self):
        assert score(30.0, 70.0) > score(10.0, 70.0)

    def test_higher_confidence_same_profit_ranks_higher(self):
        assert score(10.0, 90.0) > score(10.0, 50.0)


class TestZeroProfitExpertsNotStarved:
    def test_zero_profit_ranked_by_confidence(self):
        # FinnHub-style 0.0 profit: still ordered by conviction amongst themselves.
        assert score(0.0, 90.0) > score(0.0, 50.0)

    def test_none_profit_treated_as_zero(self):
        assert score(None, 80.0) == score(0.0, 80.0)

    def test_any_positive_profit_outranks_zero_profit(self):
        # Even a tiny positive expected profit beats the best zero-profit order.
        assert score(0.1, 10.0) > score(0.0, 100.0)

    def test_zero_profit_band_is_below_zero(self):
        # Reserved band [-1, 0): always below positive-profit orders.
        assert -1.0 <= score(0.0, 50.0) < 0.0


class TestEdgeCases:
    def test_none_confidence_treated_as_zero(self):
        assert score(10.0, None) == 10.0 * 0.0

    def test_both_none(self):
        assert score(None, None) == -1.0

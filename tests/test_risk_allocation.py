"""RM-4: WAITING transaction allocation estimate (pure function)."""
from ba2_trade_platform.core.TradeRiskManagement import estimate_transaction_allocation as alloc


class TestEstimateTransactionAllocation:
    def test_uses_open_price_when_present(self):
        assert alloc(10, 25.0, None) == 250.0

    def test_open_price_takes_precedence_over_fallback(self):
        assert alloc(10, 25.0, 99.0) == 250.0

    def test_falls_back_to_order_price_when_no_open_price(self):
        # WAITING transaction without open_price -> use pending order's limit/stop price.
        assert alloc(10, None, 30.0) == 300.0

    def test_zero_when_no_price_available(self):
        assert alloc(10, None, None) == 0.0

    def test_zero_when_no_quantity(self):
        assert alloc(0, 25.0, 30.0) == 0.0
        assert alloc(None, 25.0, 30.0) == 0.0

    def test_absolute_value_for_short_quantities(self):
        # quantity is always stored positive, but be defensive.
        assert alloc(-5, 20.0, None) == 100.0

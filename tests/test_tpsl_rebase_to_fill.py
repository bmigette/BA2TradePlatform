"""Tests for re-basing a pending TP/SL price off the parent order's actual fill.

When an entry is a market order, its TP/SL are computed at enter time off a
pre-fill reference price (the market order has no fill yet). If the fill comes
in different from that reference, the proportional distance must be preserved by
re-scaling against the actual fill. rebase_price_to_fill() is the pure helper.
"""
from ba2_trade_platform.core.TradeManager import rebase_price_to_fill


class TestRebasePriceToFill:
    def test_stop_rebases_to_same_pct_of_fill(self):
        # SL set at -6% off the pre-fill reference 10.79 -> 10.1426; the buy then
        # actually filled lower at 10.66. Re-based stop must be -6% of the FILL.
        assert rebase_price_to_fill(10.1426, 10.79, 10.66) == 10.0204

    def test_tp_rebases_proportionally_sign_agnostic(self):
        # A target ABOVE the reference stays proportionally above the fill.
        assert rebase_price_to_fill(11.0, 10.0, 10.5) == 11.55

    def test_noop_when_reference_equals_fill(self):
        assert rebase_price_to_fill(10.1426, 10.66, 10.66) == 10.1426

    def test_returns_unchanged_on_missing_inputs(self):
        assert rebase_price_to_fill(10.0, None, 10.5) == 10.0
        assert rebase_price_to_fill(10.0, 10.0, None) == 10.0
        assert rebase_price_to_fill(None, 10.0, 10.5) is None

    def test_returns_unchanged_on_nonpositive_reference(self):
        assert rebase_price_to_fill(10.0, 0, 10.5) == 10.0
        assert rebase_price_to_fill(10.0, -5.0, 10.5) == 10.0

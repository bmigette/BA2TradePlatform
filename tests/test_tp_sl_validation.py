"""Tests for TransactionHelper.validate_tp_sl_prices — the direction-aware
guard that rejects swapped/inverted manual TP/SL values in the UI before they
reach the broker (BRUN incident: long @29.06 with TP 27 / SL 34 -> Alpaca 42210000)."""

from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.types import OrderDirection

LONG = OrderDirection.BUY
SHORT = OrderDirection.SELL
V = TransactionHelper.validate_tp_sl_prices


class TestLong:
    def test_valid_long(self):
        ok, _ = V(LONG, tp_price=34.0, sl_price=27.0, reference_price=29.06)
        assert ok

    def test_swapped_long_rejected(self):
        # The exact BRUN mistake: TP and SL switched on a long.
        ok, msg = V(LONG, tp_price=27.0, sl_price=34.0, reference_price=29.06)
        assert not ok and "swapped" in msg

    def test_tp_below_price_rejected(self):
        ok, msg = V(LONG, tp_price=25.0, sl_price=20.0, reference_price=29.06)
        assert not ok and "Take-profit" in msg

    def test_sl_above_price_rejected(self):
        ok, msg = V(LONG, tp_price=40.0, sl_price=31.0, reference_price=29.06)
        assert not ok and "Stop-loss" in msg

    def test_tp_only_valid(self):
        assert V(LONG, tp_price=34.0, sl_price=None, reference_price=29.06)[0]

    def test_sl_only_invalid(self):
        ok, _ = V(LONG, tp_price=None, sl_price=34.0, reference_price=29.06)
        assert not ok


class TestShort:
    def test_valid_short(self):
        ok, _ = V(SHORT, tp_price=25.0, sl_price=34.0, reference_price=29.06)
        assert ok

    def test_swapped_short_rejected(self):
        ok, msg = V(SHORT, tp_price=34.0, sl_price=25.0, reference_price=29.06)
        assert not ok and "swapped" in msg

    def test_sl_below_price_rejected(self):
        ok, _ = V(SHORT, tp_price=20.0, sl_price=28.0, reference_price=29.06)
        assert not ok


class TestEdgeCases:
    def test_nothing_to_validate(self):
        assert V(LONG, None, None, reference_price=29.06) == (True, "")

    def test_no_reference_price_ordering_still_checked(self):
        ok, _ = V(LONG, tp_price=27.0, sl_price=34.0, reference_price=None)
        assert not ok
        assert V(LONG, tp_price=34.0, sl_price=27.0, reference_price=None)[0]

    def test_equal_tp_sl_rejected(self):
        ok, _ = V(LONG, tp_price=30.0, sl_price=30.0, reference_price=29.0)
        assert not ok

    def test_zero_values_treated_as_unset(self):
        # UI passes 0/'' for cleared fields — treated as not-being-set.
        assert V(LONG, tp_price=0, sl_price=0, reference_price=29.06)[0]

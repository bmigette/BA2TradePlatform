"""A cancel-and-replace OCO (e.g. a trailing-stop raise) must not be submitted
until the broker has ACTUALLY released the prior order's held qty. Our DB can
mark the prior order CANCELED before the broker frees the qty, so the replacement
gets rejected (Alpaca 40310000 "insufficient qty available") and the position is
left unprotected.

replacement_blocked_by_qty() is the pure decision used by the waiting-trigger
submit path: defer (keep WAITING_TRIGGER, retry next refresh) while the broker's
available qty is still short of what the replacement needs.
"""
from ba2_trade_platform.core.TradeManager import replacement_blocked_by_qty
from ba2_trade_platform.core.types import OrderStatus


class TestReplacementBlockedByQty:
    def test_blocks_when_qty_not_yet_released(self):
        # TEM: replacement needs 6, broker still shows 0 available -> wait.
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, 0.0, 6.0) is True

    def test_allows_when_qty_available(self):
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, 6.0, 6.0) is False
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, 10.0, 6.0) is False

    def test_partial_availability_still_blocks(self):
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, 5.0, 6.0) is True

    def test_only_applies_to_cancel_triggered_replacements(self):
        # A normal entry->TP/SL (triggered by FILLED) just got its shares; don't gate.
        assert replacement_blocked_by_qty(OrderStatus.FILLED, 0.0, 6.0) is False

    def test_unknown_availability_does_not_block(self):
        # Broker qty unknown (None) -> don't block (preserve prior behaviour).
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, None, 6.0) is False
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, 0.0, None) is False

    def test_accepts_string_trigger_value(self):
        # depends_order_status_trigger may arrive as the raw enum value string.
        assert replacement_blocked_by_qty("canceled", 0.0, 6.0) is True
        assert replacement_blocked_by_qty("CANCELED", 0.0, 6.0) is True
        assert replacement_blocked_by_qty("filled", 0.0, 6.0) is False

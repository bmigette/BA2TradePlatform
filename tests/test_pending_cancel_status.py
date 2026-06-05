"""cancel_order marks an order PENDING_CANCEL (not optimistically CANCELED); the
account refresh then advances it based on the broker's reported status.

OrderStatus.resolve_pending_cancel(broker_status) is that decision: adopt the
broker status once the order reaches a FINAL state (CANCELED confirmed, or it
completed before the cancel landed — e.g. FILLED/EXPIRED/REJECTED); otherwise
return None so the order stays PENDING_CANCEL and keeps waiting.
"""
from ba2_trade_platform.core.types import OrderStatus


class TestResolvePendingCancel:
    def test_confirmed_cancel(self):
        assert OrderStatus.resolve_pending_cancel(OrderStatus.CANCELED) == OrderStatus.CANCELED

    def test_raced_to_fill(self):
        # The order filled in the split second before the cancel landed.
        assert OrderStatus.resolve_pending_cancel(OrderStatus.FILLED) == OrderStatus.FILLED

    def test_other_terminal_states_adopted(self):
        for s in (OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.STOPPED,
                  OrderStatus.REPLACED):
            assert OrderStatus.resolve_pending_cancel(s) == s

    def test_still_working_or_cancelling_stays(self):
        for s in (OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.HELD,
                  OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_NEW,
                  OrderStatus.PARTIALLY_FILLED):
            assert OrderStatus.resolve_pending_cancel(s) is None

    def test_none_input(self):
        assert OrderStatus.resolve_pending_cancel(None) is None

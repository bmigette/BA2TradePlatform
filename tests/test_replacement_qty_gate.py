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
from ba2_trade_platform.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_trade_platform.core.types import OrderStatus
from tests.conftest import MockAccount


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


class TestGateIsNotAlpacaOnly:
    """I9: the guard was a PERMANENT NO-OP on every broker but Alpaca.

    ``get_available_position_quantity`` existed only on ``AlpacaAccount``, so
    TradeManager's ``except Exception: available_qty = None`` fired for every other
    account and ``replacement_blocked_by_qty(None)`` returned False -- the whole
    protection against dropping a stop to a 40310000 rejection was simply absent.
    """

    def test_the_seam_lives_on_the_interface_not_just_alpaca(self):
        assert hasattr(ReadOnlyAccountInterface, "get_available_position_quantity")

    def test_a_plain_broker_answers_the_gate_without_an_override(self):
        """MockAccount defines no override -- it must still get a real answer."""
        acct = MockAccount(1)
        acct._positions = [{"symbol": "AAPL", "qty": 10.0, "qty_available": 0.0}]

        available = acct.get_available_position_quantity("AAPL")

        assert available == 0.0
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, available, 6.0) is True

    def test_a_released_qty_lets_the_replacement_through(self):
        acct = MockAccount(1)
        acct._positions = [{"symbol": "AAPL", "qty": 10.0, "qty_available": 10.0}]

        available = acct.get_available_position_quantity("AAPL")

        assert available == 10.0
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, available, 6.0) is False

    def test_a_broker_that_cannot_answer_blocks_instead_of_submitting(self):
        """The DEFAULT for "cannot answer" is 0.0 (defer and retry next refresh),
        never None (submit blind). A fetch failure must not be read as "the qty is
        free" -- that is the direction that gets the order rejected and hard-ERRORed,
        silently dropping the position's protection."""
        acct = MockAccount(1)
        acct._positions = None  # positions fetch FAILED

        available = acct.get_available_position_quantity("AAPL")

        assert available == 0.0
        assert replacement_blocked_by_qty(OrderStatus.CANCELED, available, 6.0) is True

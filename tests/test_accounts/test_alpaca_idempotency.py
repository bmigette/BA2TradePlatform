"""
H1 idempotency guard: AlpacaAccount._submit_order_impl must not re-submit an order that
already carries a broker_order_id (i.e. was already sent to the broker). This is the
defense-in-depth half of the double-submission fix.
"""
from unittest.mock import MagicMock

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType


def _bare_account():
    """Construct an AlpacaAccount without running __init__ (no real broker connection)."""
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()  # non-None so _check_authentication() passes
    return acct


def test_submit_order_impl_skips_when_broker_order_id_already_set():
    acct = _bare_account()
    order = TradingOrder(
        id=42,
        account_id=1,
        symbol="BBAI",
        quantity=10.0,
        side=OrderDirection.SELL,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        broker_order_id="already-sent-abc123",
    )

    result = acct._submit_order_impl(order)

    # Returns the same order, untouched, and never calls the broker.
    assert result is order
    acct.client.submit_order.assert_not_called()

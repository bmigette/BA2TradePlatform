"""PennyMomentum entry: attribution flows through Transaction.expert_id, with no
ExpertRecommendation record created (consistency with the FactorRanker pattern)."""
from unittest.mock import MagicMock, patch

from sqlmodel import func, select

from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import ExpertRecommendation, TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus
from ba2_trade_platform.modules.experts.PennyMomentumTrader.trade_manager import PennyTradeManager
from tests.factories import create_account_definition, create_expert_instance


class _EntryAccount:
    """Account double that fills + persists submitted orders (no auto transaction)."""

    def __init__(self, price):
        self.price = price
        self.submitted = []

    def get_instrument_current_price(self, symbol):
        return self.price

    def submit_order(self, order, is_closing_order=False):
        from ba2_trade_platform.core.db import add_instance
        self.submitted.append((order, is_closing_order))
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        add_instance(order, expunge_after_flush=True)
        return order


def _make_manager(account):
    acct_def = create_account_definition()
    inst = create_expert_instance(account_id=acct_def.id, expert="PennyMomentumTrader")
    expert_stub = MagicMock()
    expert_stub.get_available_balance.return_value = 100_000.0
    expert_stub.get_virtual_balance.return_value = 100_000.0
    expert_stub.get_setting_with_interface_default.return_value = 100.0  # per-instrument cap %
    with patch("ba2_trade_platform.core.utils.get_account_instance_from_id", return_value=account), \
         patch("ba2_trade_platform.core.utils.get_expert_instance_from_id", return_value=expert_stub):
        mgr = PennyTradeManager(inst.id)
    return mgr, inst


def test_entry_attributes_via_transaction_without_recommendation():
    account = _EntryAccount(price=2.0)
    mgr, inst = _make_manager(account)

    order_id = mgr.execute_entry(
        symbol="BBAI", qty=100, confidence=80.0, catalyst="news",
        strategy="swing", limit_slippage_pct=0.0,
    )
    assert order_id is not None

    order = get_instance(TradingOrder, order_id)
    assert order.side == OrderDirection.BUY
    assert order.quantity == 100

    # No recommendation linkage...
    assert order.expert_recommendation_id is None
    # ...attribution flows through the pre-created transaction instead.
    assert order.transaction_id is not None
    trans = get_instance(Transaction, order.transaction_id)
    assert trans.expert_id == inst.id
    assert trans.symbol == "BBAI"

    # No ExpertRecommendation rows created for this expert.
    with get_db() as session:
        count = session.exec(
            select(func.count()).select_from(ExpertRecommendation)
            .where(ExpertRecommendation.instance_id == inst.id)
        ).one()
    assert count == 0

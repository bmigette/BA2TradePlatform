"""
Tests for PennyMomentum exit staging (wash-trade avoidance).

When an exit fires while the entry BUY is still working at the broker (e.g.
PARTIALLY_FILLED), submitting the opposing SELL immediately is rejected by Alpaca
as a wash trade. Instead the SELL is staged as a WAITING_TRIGGER order that depends
on the entry reaching FILLED, and every PennyMomentum exit is stamped
``fixed_quantity`` so its deliberate (possibly partial) size is never rewritten.
"""
from unittest.mock import MagicMock, patch

import pytest

from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.modules.experts.PennyMomentumTrader.trade_manager import (
    PennyTradeManager,
    find_open_entry_buy,
)
from tests.factories import (
    create_account_definition,
    create_expert_instance,
    create_transaction,
    create_trading_order,
)


# ---------------------------------------------------------------------------
# find_open_entry_buy helper
# ---------------------------------------------------------------------------

class TestFindOpenEntryBuy:
    def test_finds_partially_filled_entry(self):
        acct = create_account_definition()
        txn = create_transaction(symbol="BBAI", quantity=100.0)
        entry = create_trading_order(
            account_id=acct.id, symbol="BBAI", quantity=100.0,
            side=OrderDirection.BUY, status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=60.0, transaction_id=txn.id,
        )
        with get_db() as session:
            found = find_open_entry_buy(session, txn.id)
        assert found is not None and found.id == entry.id

    def test_ignores_fully_filled_entry(self):
        acct = create_account_definition()
        txn = create_transaction(symbol="BBAI", quantity=100.0)
        create_trading_order(
            account_id=acct.id, symbol="BBAI", quantity=100.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            filled_qty=100.0, transaction_id=txn.id,
        )
        with get_db() as session:
            assert find_open_entry_buy(session, txn.id) is None

    def test_ignores_dependent_sell_leg(self):
        """A TP/SL leg (depends_on_order set) is never treated as the entry buy."""
        acct = create_account_definition()
        txn = create_transaction(symbol="BBAI", quantity=100.0)
        entry = create_trading_order(
            account_id=acct.id, symbol="BBAI", quantity=100.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            filled_qty=100.0, transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct.id, symbol="BBAI", quantity=100.0,
            side=OrderDirection.BUY, status=OrderStatus.NEW,
            transaction_id=txn.id, depends_on_order=entry.id,
        )
        with get_db() as session:
            assert find_open_entry_buy(session, txn.id) is None


# ---------------------------------------------------------------------------
# execute_exit staging
# ---------------------------------------------------------------------------

class _CapturingAccount:
    """Account double that records submit_order calls and persists the order."""

    def __init__(self):
        self.submitted = []

    def submit_order(self, order, is_closing_order=False):
        from ba2_trade_platform.core.db import add_instance
        self.submitted.append(order)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        # expunge_after_flush keeps the instance usable after commit (the real
        # AccountInterface.submit_order likewise returns a usable order with an id).
        add_instance(order, expunge_after_flush=True)
        return order


def _make_trade_manager(account):
    acct_def = create_account_definition()
    inst = create_expert_instance(account_id=acct_def.id, expert="MockExpert")
    with patch(
        "ba2_trade_platform.core.utils.get_account_instance_from_id",
        return_value=account,
    ), patch(
        "ba2_trade_platform.core.utils.get_expert_instance_from_id",
        return_value=MagicMock(),
    ):
        mgr = PennyTradeManager(inst.id)
    return mgr, inst


def _open_sell_for(transaction_id):
    with get_db() as session:
        from sqlmodel import select
        return session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == transaction_id)
            .where(TradingOrder.side == OrderDirection.SELL)
        ).first()


def test_exit_stages_waiting_trigger_when_entry_still_open():
    account = _CapturingAccount()
    mgr, inst = _make_trade_manager(account)

    txn = create_transaction(
        symbol="BBAI", quantity=100.0, status=TransactionStatus.OPENED,
        expert_id=inst.id,
    )
    entry = create_trading_order(
        account_id=mgr.account_id, symbol="BBAI", quantity=100.0,
        side=OrderDirection.BUY, status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=60.0, transaction_id=txn.id,
    )

    ok = mgr.execute_exit("BBAI", exit_pct=50.0, reason="take profit tier 1 (50%)")

    assert ok is True
    # The SELL must NOT have been submitted to the broker (wash-trade avoidance).
    assert account.submitted == []
    sell = _open_sell_for(txn.id)
    assert sell is not None
    assert sell.status == OrderStatus.WAITING_TRIGGER
    assert sell.depends_on_order == entry.id
    assert sell.depends_order_status_trigger == OrderStatus.FILLED
    assert sell.quantity == 30.0  # 50% of the 60 filled shares
    assert sell.data and sell.data.get("fixed_quantity") is True


def test_exit_submits_immediately_when_entry_filled():
    account = _CapturingAccount()
    mgr, inst = _make_trade_manager(account)

    txn = create_transaction(
        symbol="BBAI", quantity=100.0, status=TransactionStatus.OPENED,
        expert_id=inst.id,
    )
    create_trading_order(
        account_id=mgr.account_id, symbol="BBAI", quantity=100.0,
        side=OrderDirection.BUY, status=OrderStatus.FILLED,
        filled_qty=100.0, transaction_id=txn.id,
    )

    ok = mgr.execute_exit("BBAI", exit_pct=50.0, reason="take profit tier 1 (50%)")

    assert ok is True
    # The SELL was submitted to the broker immediately (one submit_order call).
    assert len(account.submitted) == 1
    # Inspect the persisted order (re-fetched to avoid a detached instance).
    sell = _open_sell_for(txn.id)
    assert sell is not None
    assert sell.side == OrderDirection.SELL
    assert sell.quantity == 50.0
    assert sell.status == OrderStatus.FILLED  # not staged
    assert sell.depends_on_order is None
    # Even an immediately-submitted partial exit is flagged so it is never resized.
    assert sell.data and sell.data.get("fixed_quantity") is True

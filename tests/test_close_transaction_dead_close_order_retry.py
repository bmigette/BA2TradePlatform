"""A DEAD closing order (terminal and never filled) must be re-submitted, not reported
as "already exists".

2026-10-02 prod: three TastyTrade "Closing position for transaction N" market orders were
REJECTED by the broker. Calling close_transaction again returned
success=True / 'Close order already exists with status rejected' while the position stayed
open, because only ERROR was retried. NO SILENT FAILURE: REJECTED/CANCELED/EXPIRED close
orders take the same retry path as ERROR; FILLED/PARTIALLY_FILLED and still-live ones
keep the no-action behaviour.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_transaction, create_trading_order,
)
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

SYMBOL = "AAPL"
QTY = 10.0
T0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def _txn_with_entry(acct_id):
    txn = create_transaction(symbol=SYMBOL, quantity=QTY, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0)
    create_trading_order(
        account_id=acct_id, symbol=SYMBOL, quantity=QTY, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=QTY,
        transaction_id=txn.id, open_price=150.0, created_at=T0,
    )
    return txn


def _close_order(acct_id, txn, status, minutes=1):
    return create_trading_order(
        account_id=acct_id, symbol=SYMBOL, quantity=QTY, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=status, transaction_id=txn.id,
        comment=f"Closing position for transaction {txn.id}",
        created_at=T0 + timedelta(minutes=minutes),
    )


def _held_account(acct_id, monkeypatch):
    account = MockAccount(acct_id)
    account._positions = [{"symbol": SYMBOL, "qty": QTY}]
    submitted = []
    original = account.submit_order

    def _capture(order, is_closing_order=False):
        submitted.append(order)
        return original(order, is_closing_order=is_closing_order)

    monkeypatch.setattr(account, "submit_order", _capture)
    return account, submitted


@pytest.mark.parametrize("dead", [
    OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.ERROR,
])
def test_dead_close_order_is_resubmitted(dead, monkeypatch):
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    old = _close_order(acct.id, txn, dead)

    result = account.close_transaction(txn.id)

    assert len(submitted) == 1, "a fresh close order must be submitted"
    assert submitted[0].side == OrderDirection.SELL
    assert submitted[0].id != old.id
    assert result["success"] is True
    assert "already exists" not in result["message"]
    assert get_instance(Transaction, txn.id).status != TransactionStatus.CLOSED


def test_rejected_order_keeps_its_broker_status(monkeypatch):
    acct = create_account_definition()
    account, _ = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    old = _close_order(acct.id, txn, OrderStatus.REJECTED)

    account.close_transaction(txn.id)

    assert get_instance(TradingOrder, old.id).status == OrderStatus.REJECTED


def test_error_order_is_still_marked_canceled(monkeypatch):
    acct = create_account_definition()
    account, _ = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    old = _close_order(acct.id, txn, OrderStatus.ERROR)

    account.close_transaction(txn.id)

    assert get_instance(TradingOrder, old.id).status == OrderStatus.CANCELED


@pytest.mark.parametrize("status", [
    OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW,
    OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
])
def test_live_or_filled_close_order_means_no_action(status, monkeypatch):
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, status)

    result = account.close_transaction(txn.id)

    assert submitted == []
    assert result["success"] is True
    assert "already exists" in result["message"]


def test_newest_live_order_beats_older_dead_one(monkeypatch):
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.REJECTED, minutes=1)
    _close_order(acct.id, txn, OrderStatus.ACCEPTED, minutes=2)

    account.close_transaction(txn.id)

    assert submitted == []


def test_older_live_order_is_not_shadowed_by_newer_dead_one(monkeypatch):
    """A live close is working: re-submitting would double-close."""
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.ACCEPTED, minutes=1)
    _close_order(acct.id, txn, OrderStatus.REJECTED, minutes=2)

    account.close_transaction(txn.id)

    assert submitted == []


def test_two_dead_close_orders_retry_exactly_once(monkeypatch):
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.REJECTED, minutes=1)
    _close_order(acct.id, txn, OrderStatus.REJECTED, minutes=2)

    account.close_transaction(txn.id)

    assert len(submitted) == 1


def test_second_call_after_retry_sees_the_new_order(monkeypatch):
    """After the retry the new close order governs: a repeat call is a no-op."""
    acct = create_account_definition()
    account, submitted = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.REJECTED)

    account.close_transaction(txn.id)
    assert len(submitted) == 1
    # MockAccount.submit_order does not persist, so persist the fresh order the real
    # helper would have written: newer than the rejected one and broker-accepted.
    _close_order(acct.id, txn, OrderStatus.ACCEPTED, minutes=5)

    before = len(submitted)
    result = account.close_transaction(txn.id)

    assert len(submitted) == before
    assert "already exists" in result["message"]


def test_dead_close_order_never_closes_a_held_position(monkeypatch):
    """Item 3 pin: FILLED entry + dead close order must not satisfy 'all orders terminal'
    (FILLED is not terminal), so the transaction stays open while the broker holds it."""
    acct = create_account_definition()
    account, _ = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.REJECTED)

    account.close_transaction(txn.id)

    fresh = get_instance(Transaction, txn.id)
    assert fresh.status != TransactionStatus.CLOSED
    assert fresh.close_reason != "manual_close"


def test_failed_resubmit_is_loud(monkeypatch):
    """If the re-submit itself fails the result must say so, not report success."""
    acct = create_account_definition()
    account, _ = _held_account(acct.id, monkeypatch)
    txn = _txn_with_entry(acct.id)
    _close_order(acct.id, txn, OrderStatus.REJECTED)
    monkeypatch.setattr(
        account, "submit_close_order_for_transaction",
        lambda t, last=None: {"success": False, "close_order_id": None,
                              "message": "broker refused"})

    result = account.close_transaction(txn.id)

    assert result["success"] is False
    assert "broker refused" in result["message"]

"""Regression 2026-08-08: a take-profit leg attached to a MARKET entry was submitted with
quantity 0 and silently cancelled, leaving live positions with a stop and no upside exit.

Measured on PROD: FMPEarningsDrift positions WKC / GNTX / CSTL each had a SELL_LIMIT carrying a
real computed price (73.17 / 23.40 / 58.42) and a ``-TP-`` comment, quantity 0.0, status CANCELED.
Dev held 12 more of the same rows; it only looked healthy because its other positions use OCO,
which carries both legs inside one correctly-sized order.

Root cause: ``AccountInterface.submit_order``'s protective-leg quantity sync skipped any leg whose
parent was a MARKET order, on the reasoning "market orders are typically close orders". An ENTRY is
a MARKET order too, so entry-attached legs that arrived with no quantity never got one.

The genuine exception it was reaching for is a PARTIAL CLOSE, which is distinguishable by SIDE:

    entry BUY   -> protective SELL leg : OPPOSITE sides -> parent is the entry -> SYNC
    close SELL  -> new protective SELL : SAME side      -> partial close       -> KEEP own qty

Plus a belt-and-braces rail: a protective leg with no quantity is refused outright rather than
sent to the broker to be cancelled, because that silent cancellation is what hid this for weeks.
"""
import pytest

from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


class _StubAccount(AccountInterface):
    """Concrete AccountInterface whose broker call just records the order it was handed."""
    def __init__(self, account_id=1):
        self.id = account_id
        self.submitted = []

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        self.submitted.append(trading_order)
        trading_order.status = OrderStatus.NEW
        return trading_order

    def _get_instrument_current_price_impl(self, *a, **k): raise NotImplementedError
    def adjust_sl(self, *a, **k): raise NotImplementedError
    def adjust_tp(self, *a, **k): raise NotImplementedError
    def adjust_tp_sl(self, *a, **k): raise NotImplementedError
    def cancel_order(self, *a, **k): raise NotImplementedError
    def get_account_info(self, *a, **k): raise NotImplementedError
    def get_balance(self, *a, **k): raise NotImplementedError
    def get_balance_history(self, *a, **k): raise NotImplementedError
    def get_dividends(self, *a, **k): raise NotImplementedError
    def get_filled_trades(self, *a, **k): raise NotImplementedError
    def get_order(self, *a, **k): raise NotImplementedError
    def get_orders(self, *a, **k): raise NotImplementedError
    def get_positions(self, *a, **k): raise NotImplementedError
    def modify_order(self, *a, **k): raise NotImplementedError
    def refresh_orders(self, *a, **k): raise NotImplementedError
    def refresh_positions(self, *a, **k): raise NotImplementedError
    def symbols_exist(self, *a, **k): raise NotImplementedError


def _account(monkeypatch):
    """Stub account with validation / transaction plumbing / wash-trade gate neutralised —
    only the quantity-sync block is under test here."""
    acct = _StubAccount()
    monkeypatch.setattr(_StubAccount, "_validate_trading_order",
                        lambda self, o, is_closing_order=False: {"is_valid": True, "errors": []})
    monkeypatch.setattr(_StubAccount, "_handle_transaction_requirements", lambda self, o: None)
    monkeypatch.setattr(_StubAccount, "_is_washtrade_lock_candidate", lambda self, o: False)
    monkeypatch.setattr(_StubAccount, "_attach_protective_orders",
                        lambda self, *a, **k: None, raising=False)
    return acct


def _entry(symbol, qty, side=OrderDirection.BUY, order_type=OrderType.MARKET):
    txn = Transaction(symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                      status=TransactionStatus.OPENED, expert_id=1)
    txn_id = add_instance(txn)
    parent = TradingOrder(account_id=1, symbol=symbol, quantity=qty, side=side,
                          order_type=order_type, status=OrderStatus.FILLED,
                          transaction_id=txn_id)
    return add_instance(parent), txn_id


def test_tp_leg_on_market_entry_inherits_the_entry_quantity(monkeypatch):
    """THE REGRESSION. A SELL_LIMIT take-profit attached to a filled MARKET BUY entry must be
    sized to the entry, not left at whatever it was constructed with (0).

    Before the fix this hit the "parent is MARKET -> keep independent quantity" branch, kept 0,
    and was cancelled by the broker — exactly prod's WKC/GNTX/CSTL."""
    acct = _account(monkeypatch)
    parent_id, txn_id = _entry("WKC", 1.0)

    tp = TradingOrder(account_id=1, symbol="WKC", quantity=0.0, side=OrderDirection.SELL,
                      order_type=OrderType.SELL_LIMIT, status=OrderStatus.PENDING,
                      limit_price=73.17, depends_on_order=parent_id, transaction_id=txn_id)
    acct.submit_order(tp)

    assert acct.submitted, "the TP leg should have reached the broker"
    assert acct.submitted[0].quantity == 1.0, (
        "TP leg must inherit the MARKET entry's quantity; quantity 0 is silently cancelled")


def test_protective_leg_on_partial_close_keeps_its_own_quantity(monkeypatch):
    """The case the old MARKET check existed to protect: closing 4 of 5 shares creates a same-side
    MARKET SELL, and the replacement protective leg for the remaining 1 share must NOT be resized
    to 4. Side is what distinguishes it — same side as the leg means it is a close."""
    acct = _account(monkeypatch)
    close_id, txn_id = _entry("GNTX", 4.0, side=OrderDirection.SELL, order_type=OrderType.MARKET)

    sl = TradingOrder(account_id=1, symbol="GNTX", quantity=1.0, side=OrderDirection.SELL,
                      order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING,
                      stop_price=19.66, depends_on_order=close_id, transaction_id=txn_id)
    acct.submit_order(sl)

    assert acct.submitted[0].quantity == 1.0, (
        "a protective leg behind a same-side partial close keeps its own remaining-share quantity")


def test_zero_quantity_protective_leg_is_refused_not_silently_cancelled(monkeypatch):
    """The rail. If a protective leg still has no quantity after the sync (e.g. an unresolvable
    parent), refuse it loudly. Sending it means the broker cancels it and the position looks
    covered in the order list while actually being naked."""
    acct = _account(monkeypatch)

    orphan = TradingOrder(account_id=1, symbol="CSTL", quantity=0.0, side=OrderDirection.SELL,
                          order_type=OrderType.SELL_LIMIT, status=OrderStatus.PENDING,
                          limit_price=58.42, depends_on_order=999999)
    with pytest.raises(ValueError, match="quantity"):
        acct.submit_order(orphan)
    assert not acct.submitted, "a zero-quantity protective leg must never reach the broker"

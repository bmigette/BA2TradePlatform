"""Alpaca's exit-leg maintenance replaces PROTECTION only, sized on the POST-FILL position.

REVIEW FINDING C1 (2026-09-29). ``AlpacaAccount._adjust_tpsl_internal`` took every
non-terminal order on the transaction other than the first entry to be an exit leg -- and that
includes an order some other path just put to work on the SAME transaction: a FactorRanker
rebalance's market SELL (a trim or exit) or market BUY (an add). ``_handle_filled_entry_exit``
then marked the real protective leg CANCELED, staged a replacement sized on
``transaction.quantity`` that waited for the REBALANCE order to be CANCELED, sent the broker a
cancel for that rebalance order, and returned True:

* sell already filled -> the cancel fails, the staged stop is dropped when its parent turns out
  FILLED, and the remainder is left with NO stop while every log says the stop moved;
* sell still working  -> the trim is cancelled at the broker, i.e. reversed;
* add still working   -> the add is cancelled, and the stop is sized on the pre-add quantity.

The ADD case is reachable on the base code without any re-pricing pass: FactorRanker's
``_submit_buy`` attaches its stop through ``submit_order(sl_price=...)`` ->
``adjust_sl(..., source="initial_setup")`` right after the add is accepted.

These tests drive the REAL ``AlpacaAccount`` bracket maintenance on a real DB (the shape of
``test_live_entry_stop_reconciliation.test_real_alpaca_bracket_...``); only the broker calls
(``cancel_order`` / ``submit_order``) are recorded instead of sent. In every case no
non-protection order gets a cancel, and the position ends protected at its post-fill size.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.factories import create_account_definition, create_trading_order, create_transaction

T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
NEW_SL = 75.0


def _account(monkeypatch, definition):
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct = object.__new__(AlpacaAccount)
    acct.id = definition.id
    cancels, submits = [], []

    def fake_cancel(order_id):
        cancels.append(int(order_id))
        o = get_instance(TradingOrder, int(order_id))
        o.status = OrderStatus.PENDING_CANCEL
        update_instance(o)
        return True

    def fake_submit(order, **kwargs):
        o = get_instance(TradingOrder, order.id)
        o.status = OrderStatus.NEW
        o.broker_order_id = f"brk-{o.id}"
        update_instance(o)
        submits.append(o.id)
        return o

    monkeypatch.setattr(acct, "cancel_order", fake_cancel)
    monkeypatch.setattr(acct, "submit_order", fake_submit)
    monkeypatch.setattr(acct, "get_instrument_current_price", lambda *a, **k: 80.0)
    monkeypatch.setattr(acct, "_margin_enabled", lambda: False)
    return acct, cancels, submits


def _order(definition, txn, minutes, **kw):
    kw.setdefault("symbol", "AAPL")
    o = create_trading_order(account_id=definition.id, transaction_id=txn.id,
                             created_at=T0 + timedelta(minutes=minutes), **kw)
    return o


def _sl_comment(definition, txn, parent_id):
    from ba2_common.core.TransactionHelper import TransactionHelper
    return TransactionHelper.tpsl_comment("SL", definition.id, txn.id, parent_id)


def _held_long(definition, *, qty=10.0):
    """An OPENED long: entry BUY ``qty`` FILLED at 100."""
    txn = create_transaction(side=OrderDirection.BUY, quantity=qty, stop_loss=78.16,
                             open_price=100.0, status=TransactionStatus.OPENED)
    entry = _order(definition, txn, 0, side=OrderDirection.BUY, quantity=qty,
                   order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=qty,
                   open_price=100.0, broker_order_id="b-entry")
    return txn, entry


def _stops(txn_id):
    """Every non-terminal protective stop on the transaction: [(qty, stop, status, depends)]."""
    from ba2_common.core.TransactionHelper import TransactionHelper
    from ba2_common.core.trade_store import orders_where

    return sorted(
        (o.quantity, o.stop_price, o.status, o.depends_on_order,
         o.depends_order_status_trigger)
        for o in orders_where(transaction_id=txn_id)
        if o.status not in OrderStatus.get_terminal_statuses()
        and TransactionHelper.is_resting_protection(o))


def _trim_rows(definition, *, sell_filled: bool):
    """The state FactorRanker's ``_submit_sell`` leaves after trimming 10 -> 4: its old stop
    released (CANCELED), the market SELL 6, and ``_reprotect_remainder``'s 4-share leg waiting
    for that SELL to fill, at the released leg's price."""
    txn, entry = _held_long(definition)
    old = _order(definition, txn, 1, side=OrderDirection.SELL, quantity=10.0,
                 order_type=OrderType.SELL_STOP, status=OrderStatus.CANCELED, stop_price=78.16,
                 broker_order_id="b-oldsl", comment=_sl_comment(definition, txn, entry.id))
    sell = _order(definition, txn, 2, side=OrderDirection.SELL, quantity=6.0,
                  order_type=OrderType.MARKET,
                  status=OrderStatus.FILLED if sell_filled else OrderStatus.NEW,
                  filled_qty=6.0 if sell_filled else None, broker_order_id="b-sell",
                  comment="FactorRanker rebalance sell")
    reprotect = _order(definition, txn, 3, side=OrderDirection.SELL, quantity=4.0,
                       order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
                       stop_price=78.16, depends_on_order=sell.id,
                       depends_order_status_trigger=OrderStatus.FILLED,
                       comment=_sl_comment(definition, txn, sell.id))
    return txn, sell, reprotect, old


@pytest.mark.parametrize("sell_filled", [False, True], ids=["sell-working", "sell-filled"])
def test_a_trim_keeps_its_sell_and_ends_protected_at_the_remainder(monkeypatch, sell_filled):
    definition = create_account_definition()
    txn, sell, reprotect, _ = _trim_rows(definition, sell_filled=sell_filled)
    acct, cancels, submits = _account(monkeypatch, definition)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL,
                          source="factorranker_rebalance") is True

    assert sell.id not in cancels, "the rebalance SELL was cancelled"
    assert get_instance(TradingOrder, sell.id).status == sell.status
    # The remainder -- 4 shares, not the 10 transaction.quantity still says -- is protected at
    # the new price, at the broker now (the reserve fits: 6 selling + 4 stopped = 10 held).
    assert _stops(txn.id) == [(4.0, NEW_SL, OrderStatus.NEW, None, None)]
    assert len(submits) == 1


def test_a_full_exit_is_left_alone_and_the_adjust_says_it_did_nothing(monkeypatch):
    definition = create_account_definition()
    txn, entry = _held_long(definition)
    sell = _order(definition, txn, 2, side=OrderDirection.SELL, quantity=10.0,
                  order_type=OrderType.MARKET, status=OrderStatus.NEW, broker_order_id="b-sell",
                  comment="FactorRanker rebalance sell")
    acct, cancels, submits = _account(monkeypatch, definition)

    # Nothing will be left to protect once the working SELL fills: placing a stop would
    # reserve shares the exit needs, and "success" would be a lie.
    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL,
                          source="factorranker_rebalance") is False

    assert cancels == [] and submits == []
    assert get_instance(TradingOrder, sell.id).status == OrderStatus.NEW


def _add_rows(definition):
    """A held 10 with its live 10-share stop, and a FactorRanker add of 5 accepted, unfilled."""
    txn, entry = _held_long(definition)
    live_sl = _order(definition, txn, 1, side=OrderDirection.SELL, quantity=10.0,
                     order_type=OrderType.SELL_STOP, status=OrderStatus.NEW, stop_price=78.16,
                     broker_order_id="b-sl", comment=_sl_comment(definition, txn, entry.id))
    add = _order(definition, txn, 2, side=OrderDirection.BUY, quantity=5.0,
                 order_type=OrderType.MARKET, status=OrderStatus.NEW, broker_order_id="b-add",
                 comment="FactorRanker rebalance buy", data={"fixed_quantity": True})
    return txn, live_sl, add


@pytest.mark.parametrize("source", ["factorranker_rebalance", "initial_setup"])
def test_an_add_keeps_its_buy_and_the_stop_is_restaged_for_the_whole_position(monkeypatch, source):
    """``initial_setup`` is the base-code path: ``_submit_buy`` -> ``submit_order(sl_price)``
    -> Alpaca step 4b stamps ``transaction.stop_loss`` and calls ``adjust_sl``."""
    definition = create_account_definition()
    txn, live_sl, add = _add_rows(definition)
    if source == "initial_setup":
        t = get_instance(Transaction, txn.id)
        t.stop_loss = NEW_SL
        update_instance(t)
    acct, cancels, submits = _account(monkeypatch, definition)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL, source=source) is True

    # Only the protective leg is cancelled; the add is untouched.
    assert cancels == [live_sl.id]
    assert get_instance(TradingOrder, add.id).status == OrderStatus.NEW
    # The replacement covers the POST-fill position (10 + 5) and waits on the cancel of the
    # PROTECTION leg -- never on the add. (TradeManager's available-qty gate then holds it until
    # the broker has the 15 shares free.)
    assert _stops(txn.id) == [
        (10.0, 78.16, OrderStatus.PENDING_CANCEL, None, None),
        (15.0, NEW_SL, OrderStatus.WAITING_TRIGGER, live_sl.id, OrderStatus.CANCELED),
    ]
    assert submits == []


def test_an_unchanged_price_on_a_resized_position_is_not_skipped(monkeypatch):
    """The early "nothing to do" check compared prices only, so a stop at the right price for
    the WRONG size (10 of a position about to be 15) was left alone."""
    definition = create_account_definition()
    txn, live_sl, add = _add_rows(definition)
    acct, cancels, _ = _account(monkeypatch, definition)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), 78.16, source="initial_setup") is True

    assert cancels == [live_sl.id]
    assert (15.0, 78.16, OrderStatus.WAITING_TRIGGER, live_sl.id, OrderStatus.CANCELED) in _stops(txn.id)


def test_a_failed_protection_cancel_is_a_failure_not_a_success(monkeypatch):
    definition = create_account_definition()
    txn, live_sl, add = _add_rows(definition)
    acct, cancels, _ = _account(monkeypatch, definition)
    monkeypatch.setattr(acct, "cancel_order", lambda order_id: cancels.append(order_id) or False)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL,
                          source="factorranker_rebalance") is False
    # The staged replacement would wait forever on a cancel that never happened: withdrawn.
    assert _stops(txn.id) == [(10.0, 78.16, OrderStatus.NEW, None, None)]


def test_an_unmeasurable_fill_with_an_order_in_flight_is_refused(monkeypatch):
    """A FILLED row with no filled_qty makes the post-fill size unknowable; with an add in
    flight, the leg's size cannot be decided, so nothing is touched and the adjust fails."""
    definition = create_account_definition()
    txn, live_sl, add = _add_rows(definition)
    entry = [o for o in __import__("ba2_common.core.trade_store", fromlist=["x"])
             .orders_where(transaction_id=txn.id) if o.broker_order_id == "b-entry"][0]
    entry.filled_qty = None
    update_instance(entry)
    acct, cancels, submits = _account(monkeypatch, definition)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL,
                          source="factorranker_rebalance") is False
    assert cancels == [] and submits == []


def test_an_unmeasurable_fill_with_nothing_in_flight_keeps_the_historical_size(monkeypatch):
    definition = create_account_definition()
    txn, entry = _held_long(definition)
    entry.filled_qty = None
    update_instance(entry)
    live_sl = _order(definition, txn, 1, side=OrderDirection.SELL, quantity=10.0,
                     order_type=OrderType.SELL_STOP, status=OrderStatus.NEW, stop_price=78.16,
                     broker_order_id="b-sl", comment=_sl_comment(definition, txn, entry.id))
    acct, cancels, _ = _account(monkeypatch, definition)

    assert acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL, source="ruleset") is True
    assert cancels == [live_sl.id]
    assert (10.0, NEW_SL, OrderStatus.WAITING_TRIGGER, live_sl.id, OrderStatus.CANCELED) in _stops(txn.id)

"""A top-up sent as a complex order keeps its buy, and the position never loses its stop.

Dev 8080, 2026-11-16 15:11 UTC (FactorRanker expert 28, SOLS and VRT). A top-up buy of a name
that rests a sell stop is blocked by the wash-trade rule, so it goes out as an OTO (buy + stop
leg). ``AlpacaAccount`` step 4b then called ``adjust_sl``, and ``_handle_filled_entry_exit``
cancelled BOTH the position's old stop and the OTO's own leg about 100 ms after the submit.
Read from the broker afterwards: the buy and its leg carry cancel times 2 ms apart -- the
cancel of the leg cancels the unfilled buy. The replacement stop, sized for the position
AFTER the buy (11 for a position still of 10), was then deferred at every refresh, at INFO.
Result: no top-up, and no stop at the broker. 16 of FactorRanker's 21 top-ups sent this way
died like that.

The NEW-entry path has kept such a leg since 2026-10-29 (``_handle_unfilled_entry_exit``, RAPP
entry 1438); the top-up path never did. Now:

* while the add is in flight nothing is cancelled: the replacement waits for the add to FILL;
* when it fills, the trigger pass cancels the add's leg AND the old stop, and the replacement
  goes in once the broker has released the shares (its price is not re-based to the add's fill);
* when the add dies, the replacement is dropped, the old stop keeps working, and the
  transaction gets its level back;
* a staged replacement larger than the position can ever be is resized to the position, and
  one whose transaction holds nothing is cancelled, never sent.

Real ``AlpacaAccount`` exit maintenance and real ``TradeManager`` trigger pass on the test DB;
only the broker calls are recorded instead of sent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core import TradeManager as TM
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.factories import create_account_definition, create_trading_order, create_transaction
from tests.test_breached_stop_force_close import _LogRecorder

pytestmark = pytest.mark.usefixtures("reset_test_db")

T0 = datetime(2026, 11, 16, 15, 11, tzinfo=timezone.utc)
OLD_SL = 78.16
NEW_SL = 75.0


class _World:
    """The real ``AlpacaAccount`` with its broker calls recorded, wired into the trigger pass."""

    def __init__(self, monkeypatch, definition, available=10.0):
        from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
        import ba2_trade_platform.modules.accounts as accounts

        self.acct = object.__new__(AlpacaAccount)
        self.acct.id = definition.id
        self.cancels, self.submits = [], []
        self.available = available
        self.refuse_cancel = set()          # order ids whose cancel the broker refuses

        def fake_cancel(order_id):
            self.cancels.append(int(order_id))
            if int(order_id) in self.refuse_cancel:
                return False
            o = get_instance(TradingOrder, int(order_id))
            o.status = OrderStatus.PENDING_CANCEL
            update_instance(o)
            return True

        def fake_submit(order, **kwargs):
            o = get_instance(TradingOrder, order.id)
            o.quantity = order.quantity
            o.status = OrderStatus.NEW
            o.broker_order_id = f"brk-{o.id}"
            update_instance(o)
            self.submits.append((o.id, float(o.quantity)))
            return o

        monkeypatch.setattr(self.acct, "cancel_order", fake_cancel)
        monkeypatch.setattr(self.acct, "submit_order", fake_submit)
        monkeypatch.setattr(self.acct, "get_instrument_current_price", lambda *a, **k: 101.0)
        monkeypatch.setattr(self.acct, "get_available_position_quantity",
                            lambda symbol: self.available)
        monkeypatch.setattr(self.acct, "_margin_enabled", lambda: False)
        monkeypatch.setattr(accounts, "get_account_class",
                            lambda provider: (lambda _id: self.acct))

    def sweep(self):
        """The WAITING_TRIGGER pass of the account refresh (step 2)."""
        tm = TradeManager()
        rec = _LogRecorder()
        tm.logger = rec
        tm._check_all_waiting_trigger_orders()
        return rec


def _order(definition, txn, minutes, **kw):
    kw.setdefault("symbol", "AAPL")
    return create_trading_order(account_id=definition.id, transaction_id=txn.id,
                                created_at=T0 + timedelta(minutes=minutes), **kw)


def _sl_comment(definition, txn, parent_id):
    from ba2_common.core.TransactionHelper import TransactionHelper
    return TransactionHelper.tpsl_comment("SL", definition.id, txn.id, parent_id)


def _held_long(definition, *, stop_status=OrderStatus.NEW, txn_status=TransactionStatus.OPENED):
    """A long of 10 bought at 100 with its 10-share stop at ``OLD_SL``."""
    txn = create_transaction(side=OrderDirection.BUY, quantity=10.0, stop_loss=OLD_SL,
                             open_price=100.0, status=txn_status)
    entry = _order(definition, txn, 0, side=OrderDirection.BUY, quantity=10.0,
                   order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=10.0,
                   open_price=100.0, broker_order_id="b-entry")
    old = _order(definition, txn, 1, side=OrderDirection.SELL, quantity=10.0,
                 order_type=OrderType.SELL_STOP, status=stop_status, stop_price=OLD_SL,
                 broker_order_id="b-sl", comment=_sl_comment(definition, txn, entry.id))
    return txn, entry, old


def _complex_add(definition, txn, *, status=OrderStatus.NEW, filled=None,
                 leg_status=OrderStatus.HELD):
    """What the wash-trade escape leaves: the add BUY 5 sent as an OTO, and the stop leg
    Alpaca created for it (5 shares at the NEW level), adopted as a row."""
    add = _order(definition, txn, 2, side=OrderDirection.BUY, quantity=5.0,
                 order_type=OrderType.MARKET, status=status, filled_qty=filled,
                 broker_order_id="b-add", comment="FactorRanker rebalance buy",
                 data={"fixed_quantity": True,
                       "washtrade_complex_submit": {"blocker_order_type": "sell_stop"}})
    leg = _order(definition, txn, 3, side=OrderDirection.SELL, quantity=5.0,
                 order_type=OrderType.SELL_STOP, status=leg_status, stop_price=NEW_SL,
                 broker_order_id="b-leg", parent_order_id=add.id,
                 comment=f"1794841893-OCO-SL-[PARENT:{add.id}/BROKER:b-add]")
    return add, leg


def _step_4b(world, txn):
    """``AlpacaAccount._submit_order_impl`` step 4b for the add: stamp the level, adjust."""
    t = get_instance(Transaction, txn.id)
    t.stop_loss = NEW_SL
    update_instance(t)
    return world.acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL, source="initial_setup")


def _status(order):
    return get_instance(TradingOrder, order.id).status


def _staged(txn):
    """The one unsent replacement stop of the transaction."""
    from ba2_common.core.trade_store import orders_where
    rows = [o for o in orders_where(transaction_id=txn.id)
            if o.status == OrderStatus.WAITING_TRIGGER and o.order_type == OrderType.SELL_STOP]
    assert len(rows) == 1, [(o.id, o.status, o.quantity) for o in rows]
    return rows[0]


def _set(order, status, filled_qty=None, open_price=None):
    row = get_instance(TradingOrder, order.id)
    row.status = status
    if filled_qty is not None:
        row.filled_qty = filled_qty
    if open_price is not None:
        row.open_price = open_price
    update_instance(row)


# --------------------------------------------------------------------------- while in flight

def test_nothing_is_cancelled_while_the_add_is_in_flight(monkeypatch):
    """The incident: cancelling the add's own leg cancelled the add."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)

    assert _step_4b(world, txn) is True

    assert world.cancels == [], "a protection leg was cancelled under an unfilled add"
    assert _status(old) == OrderStatus.NEW and _status(leg) == OrderStatus.HELD
    assert _status(add) == OrderStatus.NEW
    staged = _staged(txn)
    # The whole position (10 + 5) at the new level, sent only once the add has FILLED.
    assert (staged.quantity, staged.stop_price) == (15.0, NEW_SL)
    assert (staged.depends_on_order, staged.depends_order_status_trigger) == (
        add.id, OrderStatus.FILLED)
    assert staged.data[TM.PROTECTION_AFTER_ADD_KEY] is True


def test_a_second_adjust_while_the_add_is_in_flight_stages_one_replacement(monkeypatch):
    definition = create_account_definition()
    txn, _entry, _old = _held_long(definition)
    _complex_add(definition, txn)
    world = _World(monkeypatch, definition)

    assert _step_4b(world, txn) is True
    assert world.acct.adjust_sl(get_instance(Transaction, txn.id), 74.0,
                                source="factorranker_rebalance") is True

    assert world.cancels == []
    assert _staged(txn).stop_price == 74.0


# --------------------------------------------------------------------------- the add fills

def test_when_the_add_fills_both_old_stops_are_replaced_by_one(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id
    world.sweep()                                   # add still in flight: the pass waits
    assert world.cancels == [] and world.submits == []

    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()

    # The add's leg and the position's old stop both hold shares the replacement needs.
    assert sorted(world.cancels) == sorted([old.id, leg.id])
    assert world.submits == [], "sent while the old stops still held the shares"
    staged = get_instance(TradingOrder, staged_id)
    assert staged.status == OrderStatus.WAITING_TRIGGER
    assert staged.depends_order_status_trigger == OrderStatus.CANCELED
    assert staged.depends_on_order in (old.id, leg.id)
    # A rule price for the whole position: it is not shifted by the add's own fill price.
    assert staged.stop_price == NEW_SL
    assert get_instance(Transaction, txn.id).stop_loss == NEW_SL

    _set(old, OrderStatus.CANCELED)
    _set(leg, OrderStatus.CANCELED)
    world.available = 15.0
    world.sweep()

    assert world.submits == [(staged_id, 15.0)]
    assert get_instance(TradingOrder, staged_id).stop_price == NEW_SL


def test_the_replacement_waits_until_every_old_stop_is_confirmed_cancelled(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()

    # Only the order it is chained on is confirmed; the other still holds its shares.
    chained_on = _staged(txn).depends_on_order
    _set(old if chained_on == old.id else leg, OrderStatus.CANCELED)
    world.available = 10.0 if chained_on == leg.id else 5.0
    world.sweep()

    assert world.submits == []
    staged = _staged(txn)
    assert (staged.quantity, staged.depends_order_status_trigger) == (15.0, OrderStatus.CANCELED)


def test_a_stop_that_is_the_leg_of_the_first_entry_is_replaced_too(monkeypatch):
    """The first entry went out as an OTO itself, so the position's stop is that entry's leg.
    Left in place at the add's fill, it kept 10 shares held and the replacement never fitted."""
    definition = create_account_definition()
    txn, entry, old = _held_long(definition)
    row = get_instance(TradingOrder, old.id)
    row.parent_order_id = entry.id
    update_instance(row)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    assert _step_4b(world, txn) is True
    assert world.cancels == []

    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()

    assert sorted(world.cancels) == sorted([old.id, leg.id])


def test_a_cancel_the_broker_refused_is_asked_again(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id
    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.refuse_cancel = {old.id}
    rec = world.sweep()
    assert any(str(old.id) in m for m in rec.error_msgs), rec.error_msgs
    assert _status(old) == OrderStatus.NEW

    _set(leg, OrderStatus.CANCELED)
    world.refuse_cancel, world.available, world.cancels = set(), 5.0, []
    world.sweep()                               # the old stop still holds 10 of the 15

    assert world.cancels == [old.id] and world.submits == []
    _set(old, OrderStatus.CANCELED)
    world.available = 15.0
    world.sweep()
    assert world.submits == [(staged_id, 15.0)]


def test_the_replacement_is_not_grown_for_a_second_buy_still_in_flight(monkeypatch):
    """15 are held; a further buy of 3 is working. A stop for 18 could never go in."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id
    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()
    _set(old, OrderStatus.CANCELED)
    _set(leg, OrderStatus.CANCELED)
    _order(definition, txn, 6, side=OrderDirection.BUY, quantity=3.0,
           order_type=OrderType.MARKET, status=OrderStatus.NEW, broker_order_id="b-add2",
           comment="FactorRanker rebalance buy", data={"fixed_quantity": True})
    world.available = 15.0
    world.sweep()

    assert world.submits == [(staged_id, 15.0)]


def test_a_replacement_dropped_after_the_add_filled_is_placed_again(monkeypatch):
    """The leg it waited on EXPIRED instead of being cancelled. The add did fill: this is
    not a dead add, and the old stops are already gone."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id
    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()
    _set(old, OrderStatus.EXPIRED)
    _set(leg, OrderStatus.EXPIRED)
    rec = world.sweep()

    assert get_instance(TradingOrder, staged_id).status == OrderStatus.CANCELED
    assert not any("ended unfilled" in m for m in rec.warning_msgs + rec.error_msgs)
    assert get_instance(Transaction, txn.id).stop_loss == NEW_SL
    from ba2_common.core.trade_store import orders_where
    live = [(o.quantity, o.stop_price) for o in orders_where(transaction_id=txn.id)
            if o.status == OrderStatus.NEW and o.order_type == OrderType.SELL_STOP]
    assert live == [(15.0, NEW_SL)]


def test_a_first_entry_still_filling_keeps_its_own_leg(monkeypatch):
    """RAPP 2026-10-29, on the executed branch: 6 of 10 filled when the level is changed."""
    definition = create_account_definition()
    txn = create_transaction(side=OrderDirection.BUY, quantity=10.0, stop_loss=OLD_SL,
                             open_price=100.0, status=TransactionStatus.OPENED)
    entry = _order(definition, txn, 0, side=OrderDirection.BUY, quantity=10.0,
                   order_type=OrderType.MARKET, status=OrderStatus.PARTIALLY_FILLED,
                   filled_qty=6.0, open_price=100.0, broker_order_id="b-entry")
    _order(definition, txn, 1, side=OrderDirection.SELL, quantity=10.0,
           order_type=OrderType.SELL_STOP, status=OrderStatus.HELD, stop_price=OLD_SL,
           broker_order_id="b-leg", parent_order_id=entry.id,
           comment=f"1794841893-OCO-SL-[PARENT:{entry.id}/BROKER:b-entry]")
    world = _World(monkeypatch, definition)

    assert world.acct.adjust_sl(get_instance(Transaction, txn.id), NEW_SL,
                                source="factorranker_rebalance") is True

    assert world.cancels == [], "the leg of an entry that is still filling was cancelled"
    staged = _staged(txn)
    assert (staged.quantity, staged.stop_price, staged.depends_on_order) == (10.0, NEW_SL, entry.id)


def test_an_add_to_a_position_without_a_stop_ends_protected_for_all_of_it(monkeypatch):
    """The blocker was another transaction's stop. The add's 5-share leg at the right price
    read as "already protected", which left the 10 shares held before with nothing."""
    definition = create_account_definition()
    txn, _entry, _old = _held_long(definition, stop_status=OrderStatus.CANCELED)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)

    assert _step_4b(world, txn) is True
    assert world.cancels == []
    staged_id = _staged(txn).id

    _set(add, OrderStatus.FILLED, filled_qty=5.0, open_price=101.0)
    _set(leg, OrderStatus.NEW)
    world.sweep()
    assert world.cancels == [leg.id]
    _set(leg, OrderStatus.CANCELED)
    world.available = 15.0
    world.sweep()

    assert world.submits == [(staged_id, 15.0)]


# --------------------------------------------------------------------------- the add dies

def test_a_dead_add_leaves_the_old_stop_working_and_restores_the_level(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id

    _set(add, OrderStatus.CANCELED, filled_qty=0.0)      # the broker killed the complex order
    _set(leg, OrderStatus.CANCELED)
    rec = world.sweep()

    assert get_instance(TradingOrder, staged_id).status == OrderStatus.CANCELED
    assert world.cancels == [] and world.submits == []
    assert _status(old) == OrderStatus.NEW
    # The stop at the broker is the old one: the transaction says so again.
    assert get_instance(Transaction, txn.id).stop_loss == OLD_SL
    assert any(str(add.id) in m and "keeps" in m for m in rec.warning_msgs), rec.warning_msgs


def test_a_dead_add_on_a_position_without_a_stop_gets_one_for_what_it_holds(monkeypatch):
    """The blocker was another transaction's stop; this one had no protection of its own."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition, stop_status=OrderStatus.CANCELED)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)

    _set(add, OrderStatus.CANCELED, filled_qty=0.0)
    _set(leg, OrderStatus.CANCELED)
    world.sweep()

    from ba2_common.core.trade_store import orders_where
    live = [(o.quantity, o.stop_price) for o in orders_where(transaction_id=txn.id)
            if o.status == OrderStatus.NEW and o.order_type == OrderType.SELL_STOP]
    assert live == [(10.0, NEW_SL)]


def test_an_add_cancelled_after_a_partial_fill_is_protected_at_what_it_holds(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition)
    add, leg = _complex_add(definition, txn)
    world = _World(monkeypatch, definition)
    _step_4b(world, txn)
    staged_id = _staged(txn).id

    _set(add, OrderStatus.CANCELED, filled_qty=2.0, open_price=101.0)
    _set(leg, OrderStatus.CANCELED)
    world.sweep()

    assert world.cancels == [old.id]
    _set(old, OrderStatus.CANCELED)
    world.available = 12.0
    world.sweep()

    # 10 held + the 2 that filled: not the 2 alone, and not the 15 it was staged at.
    assert world.submits == [(staged_id, 12.0)]


# --------------------------------------------------------------------------- stuck replacements

def _stuck_replacement(definition, txn, old, *, qty):
    """A replacement staged the old way: waiting on the cancel of ``old``, for ``qty``."""
    return _order(definition, txn, 5, side=OrderDirection.SELL, quantity=qty,
                  order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
                  stop_price=NEW_SL, depends_on_order=old.id,
                  depends_order_status_trigger=OrderStatus.CANCELED,
                  comment=_sl_comment(definition, txn, old.id) + f" (chained on cancel of {old.id})")


def test_a_replacement_sized_for_a_buy_that_died_is_resized_and_sent(monkeypatch):
    """SOLS order 2402: 11 shares wanted, 10 held, the add cancelled with nothing filled."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition, stop_status=OrderStatus.CANCELED)
    _complex_add(definition, txn, status=OrderStatus.CANCELED, filled=0.0,
                 leg_status=OrderStatus.CANCELED)
    stuck = _stuck_replacement(definition, txn, old, qty=15.0)
    world = _World(monkeypatch, definition, available=10.0)

    rec = world.sweep()

    assert world.submits == [(stuck.id, 10.0)]
    assert any(str(stuck.id) in m and "15" in m and "10" in m for m in rec.warning_msgs), \
        rec.warning_msgs


def test_a_replacement_waiting_for_a_buy_still_in_flight_is_left_alone(monkeypatch):
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition, stop_status=OrderStatus.CANCELED)
    _order(definition, txn, 2, side=OrderDirection.BUY, quantity=5.0,
           order_type=OrderType.MARKET, status=OrderStatus.NEW, broker_order_id="b-add",
           comment="FactorRanker rebalance buy", data={"fixed_quantity": True})
    stuck = _stuck_replacement(definition, txn, old, qty=15.0)
    world = _World(monkeypatch, definition, available=10.0)

    world.sweep()

    assert world.submits == []
    row = get_instance(TradingOrder, stuck.id)
    assert (row.status, row.quantity) == (OrderStatus.WAITING_TRIGGER, 15.0)


def test_a_remainder_leg_sized_across_transactions_is_sent_as_staged(monkeypatch):
    """FactorRanker's remainder leg waits on its sell's FILL and is sized over every
    transaction of the symbol: measured on its own transaction alone it would read as
    covering shares that are not there, and be cancelled."""
    definition = create_account_definition()
    txn, _entry, _old = _held_long(definition, stop_status=OrderStatus.CANCELED)
    sell = _order(definition, txn, 2, side=OrderDirection.SELL, quantity=15.0,
                  order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=15.0,
                  open_price=101.0, broker_order_id="b-sell", comment="FactorRanker rebalance sell")
    remainder = _order(definition, txn, 3, side=OrderDirection.SELL, quantity=5.0,
                       order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
                       stop_price=OLD_SL, depends_on_order=sell.id,
                       depends_order_status_trigger=OrderStatus.FILLED,
                       comment=_sl_comment(definition, txn, sell.id) + " FactorRanker protective leg")
    world = _World(monkeypatch, definition, available=1_000.0)

    world.sweep()

    assert world.submits == [(remainder.id, 5.0)]


def test_a_replacement_for_a_position_that_is_gone_is_never_sent(monkeypatch):
    """Its transaction holds nothing; the ACCOUNT still holds the symbol for another one."""
    definition = create_account_definition()
    txn, _entry, old = _held_long(definition, stop_status=OrderStatus.CANCELED,
                                  txn_status=TransactionStatus.CLOSED)
    _order(definition, txn, 2, side=OrderDirection.SELL, quantity=10.0,
           order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=10.0,
           broker_order_id="b-close", comment="Closing position")
    stuck = _stuck_replacement(definition, txn, old, qty=10.0)
    world = _World(monkeypatch, definition, available=1_000.0)

    world.sweep()

    assert world.submits == [], "a stop went out for shares this transaction does not hold"
    assert get_instance(TradingOrder, stuck.id).status == OrderStatus.CANCELED

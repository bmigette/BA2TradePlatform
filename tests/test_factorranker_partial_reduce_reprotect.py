"""A FactorRanker PARTIAL reduce leaves the remainder with a stop.

Dev 8080, every weekly rebalance from 2026-10-12 to 2026-11-16 (experts 27 and 28, 24 reduces):

    FactorRanker[27]: FAILED to re-protect MXL x13.0 after a partial reduce -- position is
    UNPROTECTED: Instance <TradingOrder ...> is not bound to a Session

``_reprotect_remainder`` saved the replacement stop with ``add_instance`` (which detaches the
row) and handed that detached object to ``account.submit_order``. Had it not raised, the stop
would have been SENT at once: before the reducing sell filled, and while the old stop still
held the shares.

The replacement is now written WAITING_TRIGGER on the reducing sell reaching FILLED, and the
platform's dependent-order pass sends it. These tests run the REAL manager against the test
database and the REAL ``TradeManager._check_all_waiting_trigger_orders``.

The stop keeps the released leg's PRICE. The dependent-order pass re-bases a stop to its
parent's fill, which is right when the parent is the ENTRY and wrong when it is a reducing
sell: 22 of 52 open dev FactorRanker transactions carry a stamped stop anchor, and each would
have had its stop moved by (trim price / anchor).
"""
import pytest

from ba2_common.core.tpsl_fill_rebase import stamp_anchor
from ba2_experts.FactorRanker import portfolio as P
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import create_account_definition
from tests.test_breached_stop_force_close import _orders_of, _set
from tests.test_factorranker_staged_rebalance_sell import (
    CAI_STOP, _Broker, _book, _cai_long_with_stop, _rebalance_sell, _sell_row, _sweep,
)

pytestmark = pytest.mark.usefixtures("reset_test_db")


def _replacement_stops(txn, old_stop):
    return [o for o in _orders_of(txn.id)
            if o.order_type == OrderType.SELL_STOP and o.id != old_stop.id]


def _reduce(monkeypatch, account, txn, errors):
    monkeypatch.setattr(P.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    return _rebalance_sell(monkeypatch, account, txn, qty=8.0, held=18.0)


def test_a_partial_reduce_stages_a_stop_for_the_remainder(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    errors = []

    _reduce(monkeypatch, account, txn, errors)

    assert not [e for e in errors if "UNPROTECTED" in e], errors
    assert account.sent == [], "nothing may reach the broker while the old stop holds the shares"
    sell = _sell_row(txn)
    [replacement] = _replacement_stops(txn, stop)
    assert replacement.quantity == pytest.approx(10.0)
    assert replacement.stop_price == pytest.approx(CAI_STOP)
    assert replacement.side == OrderDirection.SELL
    assert replacement.status == OrderStatus.WAITING_TRIGGER
    assert replacement.depends_on_order == sell.id
    assert replacement.depends_order_status_trigger == OrderStatus.FILLED
    assert not replacement.broker_order_id


def test_the_stop_goes_out_once_the_sell_has_filled_at_the_released_price(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    row = get_instance(Transaction, txn.id)
    row.meta_data = stamp_anchor(row.meta_data, stop=20.0)   # the stop was computed from 20.00
    update_instance(row)
    account = _Broker(acct_def.id, book=_book())
    errors = []
    _reduce(monkeypatch, account, txn, errors)

    _set(stop.id, OrderStatus.CANCELED, 0.0)       # the refresh confirms the cancel
    _sweep()
    assert account.sent == [(OrderDirection.SELL, pytest.approx(8.0), True)]
    [replacement] = _replacement_stops(txn, stop)
    assert replacement.status == OrderStatus.WAITING_TRIGGER, "sent before the sell filled"

    sell = get_instance(TradingOrder, _sell_row(txn).id)
    sell.status, sell.filled_qty, sell.open_price = OrderStatus.FILLED, 8.0, 25.0
    update_instance(sell)
    _sweep()

    assert [(s, q) for s, q, _closing in account.sent] == [
        (OrderDirection.SELL, pytest.approx(8.0)), (OrderDirection.SELL, pytest.approx(10.0))]
    [replacement] = _replacement_stops(txn, stop)
    assert replacement.broker_order_id
    # 25.00 * 17.1701 / 20.00 = 21.4626 is the stop re-based to the SELL's fill: not this one.
    assert replacement.stop_price == pytest.approx(CAI_STOP)
    assert get_instance(Transaction, txn.id).stop_loss == pytest.approx(CAI_STOP)
    assert not [e for e in errors if "UNPROTECTED" in e], errors


def test_a_full_exit_stages_no_stop(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())

    _rebalance_sell(monkeypatch, account, txn)     # all 18

    assert _replacement_stops(txn, stop) == []


def _staged_then_sent(monkeypatch, account, txn, stop, errors):
    """The reduce of 8 staged, its stop cancel confirmed, the sell at the broker."""
    _reduce(monkeypatch, account, txn, errors)
    _set(stop.id, OrderStatus.CANCELED, 0.0)
    _sweep()
    assert account.sent == [(OrderDirection.SELL, pytest.approx(8.0), True)]
    return get_instance(TradingOrder, _sell_row(txn).id)


def test_a_sell_cancelled_after_a_partial_fill_protects_what_is_really_left(monkeypatch):
    """3 of the 8 sold, then the broker cancelled the rest: 15 are held. The dependent-order pass
    sized a leg whose parent died part-filled at the parent's FILLED quantity -- right behind an
    entry (protect what was bought), wrong behind a reduce (3 = what LEFT)."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    sell = _staged_then_sent(monkeypatch, account, txn, stop, [])

    sell.status, sell.filled_qty, sell.open_price = OrderStatus.CANCELED, 3.0, 25.0
    update_instance(sell)
    _sweep()

    assert account.sent[-1][:2] == (OrderDirection.SELL, pytest.approx(15.0))
    [replacement] = _replacement_stops(txn, stop)
    assert replacement.quantity == pytest.approx(15.0)
    assert replacement.stop_price == pytest.approx(CAI_STOP)


def test_a_sell_that_dies_unfilled_puts_the_whole_stop_back(monkeypatch):
    """The old stop is cancelled and the reduce never traded: the waiting leg can never fire, and
    cancelling it alone left 18 shares with no stop and one WARNING."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    sell = _staged_then_sent(monkeypatch, account, txn, stop, [])

    sell.status, sell.filled_qty = OrderStatus.CANCELED, 0.0
    update_instance(sell)
    rec = _sweep()
    _sweep()                                       # a dead leg is not revisited

    [replacement] = _replacement_stops(txn, stop)
    assert replacement.status == OrderStatus.CANCELED
    assert account.adjusts == [(txn.id, None, pytest.approx(CAI_STOP), "refused_exit_reprotect")]
    assert any(str(txn.id) in m and "CAI" in m for m in rec.warning_msgs), rec.warning_msgs


def test_a_dead_reduce_whose_stop_cannot_be_put_back_is_reported_as_unprotected(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(), adjust_ok=False)
    sell = _staged_then_sent(monkeypatch, account, txn, stop, [])

    sell.status, sell.filled_qty = OrderStatus.CANCELED, 0.0
    update_instance(sell)
    rec = _sweep()

    assert any("UNPROTECTED" in m and str(txn.id) in m for m in rec.error_msgs), rec.error_msgs

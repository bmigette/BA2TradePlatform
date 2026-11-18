"""A FactorRanker rebalance sell waits for the broker to confirm the cancel of its own stop.

Dev 8080, 2026-11-16 14:45:10 UTC (experts 9 and 10, account 1): the stop was cancelled and
the market sell sent 270 ms later. Alpaca's cancel is asynchronous, the stop still held the
shares, and the sell was refused (40310000 insufficient qty, ``related_orders`` = our own
stops). 14 sells that minute; 13 positions stayed open with the stop gone.

``FactorPortfolioManager._submit_sell`` now writes the sell WAITING_TRIGGER on the stop
reaching CANCELED. These tests run the REAL manager against the test database and the REAL
``TradeManager._check_all_waiting_trigger_orders`` (the pass that submits it), with a broker
double whose cancel is a request (``PENDING_CANCEL``), as at Alpaca.

The sell carries ``STAGED_EXIT_KEY``. The pass then:
  * sizes it at the broker, like a staged full close (a stop that filled in the cancel
    window must not be sold a second time);
  * puts the stop back when the broker refuses the sell.
"""
import pytest

from ba2_experts.FactorRanker import portfolio as P
from ba2_trade_platform.core import TradeManager as TM
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)
from tests.test_breached_stop_force_close import _BrokerBook, _LogRecorder, _orders_of, _set

pytestmark = pytest.mark.usefixtures("reset_test_db")

CAI_STOP = 17.1701


class _Broker(_BrokerBook):
    """``_BrokerBook`` that records every ``adjust_tp_sl`` and can refuse a submission."""

    def __init__(self, account_id, *, book, refuse=None, adjust_ok=True):
        super().__init__(account_id, book=book)
        self._refuse, self._adjust_ok = refuse, adjust_ok
        self.adjusts = []

    def submit_order(self, order, is_closing_order=False):
        if self._refuse == "raises":
            raise RuntimeError("broker unreachable")
        if self._refuse == "rejected":
            # What AccountInterface.submit_order does with a broker rejection.
            row = get_instance(TradingOrder, order.id)
            row.status = OrderStatus.ERROR
            row.comment = f"{row.comment} | [insufficient_qty] 40310000"
            update_instance(row)
            return None
        return super().submit_order(order, is_closing_order=is_closing_order)

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        self.adjusts.append((transaction.id, new_tp_price, new_sl_price, source))
        return self._adjust_ok


def _cai_long_with_stop(acct_def, qty=18.0):
    """Transaction 560's shape: long 18 CAI, a FILLED entry and its resting stop."""
    txn = create_transaction(symbol="CAI", side=OrderDirection.BUY, quantity=qty,
                             status=TransactionStatus.OPENED, open_price=20.0,
                             stop_loss=CAI_STOP)
    entry = create_trading_order(
        account_id=acct_def.id, symbol="CAI", quantity=qty, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=qty,
        open_price=20.0, transaction_id=txn.id, broker_order_id="brk-entry")
    stop = create_trading_order(
        account_id=acct_def.id, symbol="CAI", quantity=qty, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, status=OrderStatus.ACCEPTED, filled_qty=0.0,
        stop_price=CAI_STOP, transaction_id=txn.id, depends_on_order=entry.id,
        depends_order_status_trigger=OrderStatus.FILLED, broker_order_id="brk-stop")
    return txn, entry, stop


def _book(stop_status=OrderStatus.CANCELED, stop_filled=0.0):
    return {"brk-entry": {"status": OrderStatus.FILLED, "filled_qty": 18.0},
            "brk-stop": {"status": stop_status, "filled_qty": stop_filled}}


def _rebalance_sell(monkeypatch, account, txn, qty=18.0, held=18.0):
    """The rebalance's sell for one name, through the real manager."""
    import ba2_trade_platform.modules.accounts as accounts
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: (lambda _id: account))
    mgr = P.FactorPortfolioManager.__new__(P.FactorPortfolioManager)
    mgr.expert_instance_id = 9
    mgr.account_id = account.id
    mgr.account = account
    return mgr._submit_sell("CAI", qty, [P._OpenedTxn(txn.id, 20.0, held)])


def _sweep():
    """The WAITING_TRIGGER pass of the account refresh (step 2)."""
    tm = TradeManager()
    rec = _LogRecorder()
    tm.logger = rec
    tm._check_all_waiting_trigger_orders()
    return rec


def _sell_row(txn):
    rows = [o for o in _orders_of(txn.id)
            if o.order_type == OrderType.MARKET and o.side == OrderDirection.SELL]
    assert len(rows) == 1, rows
    return rows[0]


def test_the_two_layers_use_the_same_marker():
    assert TM.STAGED_EXIT_KEY == P.STAGED_EXIT_KEY


def test_the_sell_reaches_the_broker_only_after_the_cancel_is_confirmed(monkeypatch):
    """The 2026-11-16 incident, end to end."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())

    _rebalance_sell(monkeypatch, account, txn)

    assert account.cancels == ["brk-stop"]
    assert account.sent == [], "sent while the stop still held the shares: the incident"
    sell = _sell_row(txn)
    assert sell.status == OrderStatus.WAITING_TRIGGER
    assert sell.depends_on_order == stop.id
    assert sell.depends_order_status_trigger == OrderStatus.CANCELED
    assert get_instance(TradingOrder, stop.id).status == OrderStatus.PENDING_CANCEL

    _sweep()                                   # the cancel is still only requested
    assert account.sent == []

    _set(stop.id, OrderStatus.CANCELED, 0.0)   # the account refresh confirms it
    _sweep()
    assert account.sent == [(OrderDirection.SELL, pytest.approx(18.0), True)]


def test_a_stop_that_filled_in_the_cancel_window_is_not_sold_again(monkeypatch):
    """The stop sold all 18 before the cancel reached it: sending the rebalance sell would
    sell 18 shares of another position of the account (or open a short)."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(OrderStatus.FILLED, 18.0))
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.FILLED, 18.0)

    rec = _sweep()
    _sweep()

    assert account.sent == []
    assert _sell_row(txn).status == OrderStatus.CLOSED
    assert any(str(txn.id) in m and "flat" in m.lower() for m in rec.warning_msgs)


def test_a_stop_that_partly_filled_shrinks_the_sell_to_what_is_left(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(OrderStatus.CANCELED, 5.0))
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.CANCELED, 5.0)

    _sweep()

    assert account.sent == [(OrderDirection.SELL, pytest.approx(13.0), True)]


@pytest.mark.parametrize("refusal", ["rejected", "raises"])
def test_a_refused_sell_puts_the_stop_back(monkeypatch, refusal):
    """The stop is cancelled and the sell is dead: the position must not ride unprotected
    until the next weekly rebalance. The transaction's own stop is re-placed."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(), refuse=refusal)
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.CANCELED, 0.0)

    rec = _sweep()
    _sweep()                                   # an ERROR sell is not revisited

    assert _sell_row(txn).status == OrderStatus.ERROR
    assert account.adjusts == [(txn.id, None, pytest.approx(CAI_STOP), "refused_exit_reprotect")]
    assert any(str(txn.id) in m and "CAI" in m and "stop" in m.lower()
               for m in rec.warning_msgs), rec.warning_msgs


def test_a_stop_that_cannot_be_put_back_is_reported_as_unprotected(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(), refuse="rejected", adjust_ok=False)
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.CANCELED, 0.0)

    rec = _sweep()

    assert len(account.adjusts) == 1
    assert any("UNPROTECTED" in m and str(txn.id) in m for m in rec.error_msgs), rec.error_msgs


def test_a_sell_that_waited_too_long_is_dropped_and_the_stop_put_back(monkeypatch):
    """A cancel the broker never completes: ``_reconcile_pending_cancel_orders`` gives the
    leg its working status back after a day, and the sell would then wait for ever -- until
    some later cancel of that stop fired a rebalance decision days old."""
    from datetime import datetime, timedelta, timezone
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    _rebalance_sell(monkeypatch, account, txn)
    sell = get_instance(TradingOrder, _sell_row(txn).id)
    sell.created_at = (datetime.now(timezone.utc)
                       - timedelta(hours=TM.STAGED_EXIT_MAX_WAIT_HOURS, minutes=1))
    update_instance(sell)

    rec = _sweep()                              # the leg is still PENDING_CANCEL
    _set(stop.id, OrderStatus.CANCELED, 0.0)    # ... and confirms only afterwards
    _sweep()

    assert account.sent == []
    assert _sell_row(txn).status == OrderStatus.CANCELED
    assert account.adjusts == [(txn.id, None, pytest.approx(CAI_STOP), "refused_exit_reprotect")]
    assert any(str(sell.id) in m and "waited" in m for m in rec.error_msgs), rec.error_msgs


def test_a_stop_that_ends_other_than_cancelled_drops_the_sell_and_reprotects(monkeypatch):
    """REJECTED / EXPIRED leg: the pass cancels the waiting sell. The stop is dead too, so
    the position is re-protected rather than left bare."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.EXPIRED, 0.0)

    _sweep()

    assert account.sent == []
    assert _sell_row(txn).status == OrderStatus.CANCELED
    assert [a[0] for a in account.adjusts] == [txn.id]


def test_a_sell_that_dies_before_it_has_an_account_is_reported_unprotected(monkeypatch):
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book())
    _rebalance_sell(monkeypatch, account, txn)
    _set(stop.id, OrderStatus.CANCELED, 0.0)
    import ba2_trade_platform.modules.accounts as accounts
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: None)

    rec = _sweep()

    assert _sell_row(txn).status == OrderStatus.ERROR
    assert any("UNPROTECTED" in m and str(txn.id) in m for m in rec.error_msgs), rec.error_msgs


def test_a_refused_classic_close_is_left_to_its_own_retry(monkeypatch):
    """Only a marked sell is re-protected here: a ``close_transaction`` close leaves its
    transaction CLOSING, which ``_retry_stuck_closes`` owns."""
    acct_def = create_account_definition()
    txn, _entry, stop = _cai_long_with_stop(acct_def)
    account = _Broker(acct_def.id, book=_book(), refuse="rejected")
    import ba2_trade_platform.modules.accounts as accounts
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: (lambda _id: account))
    _set(stop.id, OrderStatus.CANCELED, 0.0)
    create_trading_order(
        account_id=acct_def.id, symbol="CAI", quantity=18.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.WAITING_TRIGGER,
        transaction_id=txn.id, depends_on_order=stop.id,
        depends_order_status_trigger=OrderStatus.CANCELED,
        comment=f"Closing position for transaction {txn.id}")

    _sweep()

    assert _sell_row(txn).status == OrderStatus.ERROR
    assert account.adjusts == []

"""Tests for the WASHTRADE_LOCKED order gate and refresh promotion.

The gate lives in the broker-agnostic AccountInterface.submit_order. MockAccount
overrides submit_order, so the integration tests call the *base* method directly
(AccountInterface.submit_order(mock_account, ...)) to exercise the real gate while
using MockAccount's _submit_order_impl as the "broker".
"""
import pytest

from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, TransactionStatus
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
import ba2_trade_platform.modules.accounts as accounts_mod
from ba2_trade_platform.core.TradeManager import TradeManager


def _acct():
    return MockAccount(create_account_definition().id)


class TestWashtradeLockCandidate:
    def test_primary_market_order_is_candidate(self):
        acct = _acct()
        o = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                         side=OrderDirection.SELL, order_type=OrderType.MARKET,
                         status=OrderStatus.PENDING)
        assert acct._is_washtrade_lock_candidate(o) is True

    def test_dependent_protective_order_IS_candidate(self):
        """Dependent legs are subject to the lock too (changed 2026-08-04).

        They used to be exempt outright, on the theory that a protective leg is always an
        accepted bracket. True against its own parent; false against an unrelated order — and
        that gap silently killed 8 real SELL_STOPs on 2026-08-03 (they bypassed the lock, hit
        the broker, got 40310000, and were marked ERROR = terminal, never retried).

        BOTH leg shapes qualify — a dependent SELL_STOP (SL) and a dependent SELL_LIMIT (TP).
        A LIMIT can be REJECTED by an opposing stop even though it never CAUSES a rejection
        itself: verified against the live paper broker 2026-08-04, where a BUY LIMIT was refused
        40310000 by a standing SELL_STOP. Only the BLOCKING side stays restricted (see
        _WASHTRADE_BLOCKING_ORDER_TYPES).
        """
        acct = _acct()
        for leg_type in (OrderType.SELL_STOP, OrderType.SELL_LIMIT):
            leg = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                               side=OrderDirection.SELL, order_type=leg_type,
                               status=OrderStatus.PENDING, depends_on_order=123)
            assert acct._is_washtrade_lock_candidate(leg) is True, leg_type


class TestFindOpposingWorkingOrder:
    def test_finds_opposite_unfilled(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is not None

    def test_same_side_not_returned(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.SELL, status=OrderStatus.NEW)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is None

    def test_filled_not_returned(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.FILLED)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is None

    def test_partially_filled_counts(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.PARTIALLY_FILLED)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is not None

    def test_locked_order_not_counted(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.WASHTRADE_LOCKED)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is None

    def test_other_account_not_returned(self):
        acct, other = _acct(), _acct()
        create_trading_order(account_id=other.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is None

    def test_limit_order_not_counted(self):
        """Alpaca only wash-trade-blocks against market/stop orders; an opposing
        LIMIT order (e.g. a take-profit leg) must not lock."""
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.SELL, status=OrderStatus.NEW,
                             order_type=OrderType.SELL_LIMIT)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.BUY) is None

    def test_stop_limit_order_not_counted(self):
        """An opposing STOP-LIMIT order (e.g. a stop-loss leg) must not lock."""
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.SELL, status=OrderStatus.HELD,
                             order_type=OrderType.SELL_STOP_LIMIT)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.BUY) is None

    def test_stop_order_counted(self):
        """A plain STOP order (becomes a market order) does trigger a wash trade."""
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW,
                             order_type=OrderType.BUY_STOP)
        assert acct._find_opposing_working_order("AAPL", OrderDirection.SELL) is not None


class TestSubmitOrderGate:
    def test_locks_when_opposing_working_order_exists(self):
        acct = _acct()
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW)
        order = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.SELL, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING)
        result = AccountInterface.submit_order(acct, order, is_closing_order=True)
        # Gate should lock and skip the broker (_submit_order_impl sets FILLED)
        assert result.status == OrderStatus.WASHTRADE_LOCKED

    def test_submits_when_no_opposing_order(self):
        acct = _acct()
        order = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.SELL, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING)
        result = AccountInterface.submit_order(acct, order, is_closing_order=True)
        assert result.status == OrderStatus.FILLED

    def test_protective_leg_not_locked_by_its_OWN_parent(self):
        """A leg's own parent is a genuine bracket pair — it must never lock its own leg,
        or every TP/SL would deadlock behind the entry it protects."""
        acct = _acct()
        entry = create_trading_order(account_id=acct.id, symbol="AAPL",
                                     side=OrderDirection.BUY, status=OrderStatus.NEW)
        leg = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                           side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                           status=OrderStatus.PENDING, depends_on_order=entry.id,
                           depends_order_status_trigger=OrderStatus.FILLED,
                           stop_price=140.0, transaction_id=entry.transaction_id)
        result = AccountInterface.submit_order(acct, leg, is_closing_order=True)
        assert result.status != OrderStatus.WASHTRADE_LOCKED

    def test_protective_leg_IS_locked_by_an_UNRELATED_opposing_order(self):
        """REGRESSION (2026-08-03, order 587 / UBER tx 273).

        Several experts hold the same ticker in separate transactions while the broker nets
        them into ONE position, so a protective SELL_STOP routinely meets a DIFFERENT
        transaction's working BUY. Alpaca rejects that with 40310000 and the leg was marked
        ERROR — terminal — leaving 21 UBER shares with no stop at the broker at all.

        It must be locked and retried instead. The wait is bounded: a working order blocks only
        until it FILLS (the real blocker was a MARKET order), not until its position closes.
        """
        acct = _acct()
        # its own parent - already filled, not a blocker
        entry = create_trading_order(account_id=acct.id, symbol="AAPL",
                                     side=OrderDirection.BUY, status=OrderStatus.FILLED)
        # an UNRELATED expert's entry, still working on the same symbol
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW)
        leg = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                           side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                           status=OrderStatus.PENDING, depends_on_order=entry.id,
                           depends_order_status_trigger=OrderStatus.FILLED,
                           stop_price=140.0, transaction_id=entry.transaction_id)
        result = AccountInterface.submit_order(acct, leg, is_closing_order=True)
        assert result.status == OrderStatus.WASHTRADE_LOCKED


class _RecordingAccount(MockAccount):
    """Records how _submit_order_impl was called, and whether the adjust_* bracket block ran."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.impl_calls = []
        self.adjust_calls = []

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        self.impl_calls.append({'tp': tp_price, 'sl': sl_price, 'complex': use_complex_order})
        return super()._submit_order_impl(trading_order, tp_price=tp_price, sl_price=sl_price,
                                          is_closing_order=is_closing_order,
                                          use_complex_order=use_complex_order)

    def adjust_tp_sl(self, transaction, tp_price, sl_price, source=None):
        self.adjust_calls.append('tp_sl')

    def adjust_tp(self, transaction, tp_price, source=None):
        self.adjust_calls.append('tp')

    def adjust_sl(self, transaction, sl_price, source=None):
        self.adjust_calls.append('sl')


class TestComplexOrderEscape:
    """A blocked order with protective prices goes out as a COMPLEX order instead of locking.

    Alpaca exempts bracket/OTO/OCO from the wash-trade check — verified against the live paper
    API on 2026-08-05: an identical BUY was rejected 40310000 as MARKET and as marketable LIMIT,
    then ACCEPTED and FILLED with order_class=BRACKET against the very same blocker.
    See docs/WASHTRADE-LOCK.md.
    """

    def _blocked_order(self, acct):
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.SELL, status=OrderStatus.NEW,
                             order_type=OrderType.SELL_STOP)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        return TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                            side=OrderDirection.BUY, order_type=OrderType.MARKET,
                            status=OrderStatus.PENDING, transaction_id=txn.id)

    def test_blocked_with_tp_and_sl_submits_as_complex(self):
        acct = _RecordingAccount(create_account_definition().id)
        result = AccountInterface.submit_order(acct, self._blocked_order(acct),
                                               tp_price=200.0, sl_price=90.0)
        assert result.status != OrderStatus.WASHTRADE_LOCKED
        assert acct.impl_calls[-1]['complex'] is True

    def test_blocked_with_only_sl_submits_as_complex(self):
        """One leg is enough — the broker takes it as OTO."""
        acct = _RecordingAccount(create_account_definition().id)
        result = AccountInterface.submit_order(acct, self._blocked_order(acct), sl_price=90.0)
        assert result.status != OrderStatus.WASHTRADE_LOCKED
        assert acct.impl_calls[-1]['complex'] is True

    def test_blocked_with_no_protective_price_still_locks(self):
        """The fallback survives: with neither TP nor SL there is no complex order to form."""
        acct = _RecordingAccount(create_account_definition().id)
        result = AccountInterface.submit_order(acct, self._blocked_order(acct))
        assert result.status == OrderStatus.WASHTRADE_LOCKED
        assert acct.impl_calls == []  # never reached the broker

    def test_unblocked_order_is_not_made_complex(self):
        """REGRESSION GUARD: the uncontended path must be untouched by this feature.

        Without a blocker the order stays a plain order and the normal adjust_* bracket block
        still creates the protective legs.
        """
        acct = _RecordingAccount(create_account_definition().id)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        order = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.BUY, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING, transaction_id=txn.id)
        AccountInterface.submit_order(acct, order, tp_price=200.0, sl_price=90.0)
        assert acct.impl_calls[-1]['complex'] is False
        assert acct.adjust_calls == ['tp_sl']

    def test_complex_submission_does_not_also_call_adjust(self):
        """The broker built the legs; calling adjust_* too would place a DUPLICATE pair."""
        acct = _RecordingAccount(create_account_definition().id)
        AccountInterface.submit_order(acct, self._blocked_order(acct),
                                      tp_price=200.0, sl_price=90.0)
        assert acct.adjust_calls == []


class _PromotingAccount(MockAccount):
    """MockAccount whose submit_order records calls and persists FILLED.

    Signature matches the AccountInterface template (tp_price/sl_price/is_closing_order):
    the washtrade unlock path now re-threads the safeguard SL via ``sl_price=`` (a locked
    entry never had its protective leg created — the original submit early-returned at the
    lock), so the stub must accept the interface kwargs like a real account."""
    submitted = []

    def submit_order(self, order, tp_price=None, sl_price=None, is_closing_order=False):
        _PromotingAccount.submitted.append(order.id)
        order.status = OrderStatus.FILLED
        update_instance(order)
        return order


class TestRefreshPromotion:
    def test_promotes_when_symbol_clear(self, monkeypatch):
        _PromotingAccount.submitted = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _PromotingAccount)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED)
        locked = create_trading_order(account_id=acct_def.id, symbol="AAPL",
                                      side=OrderDirection.SELL, order_type=OrderType.MARKET,
                                      status=OrderStatus.WASHTRADE_LOCKED, transaction_id=txn.id)

        TradeManager()._check_all_washtrade_locked_orders()

        assert locked.id in _PromotingAccount.submitted
        assert get_instance(TradingOrder, locked.id).status == OrderStatus.FILLED

    def test_stays_locked_when_still_blocked(self, monkeypatch):
        _PromotingAccount.submitted = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _PromotingAccount)
        create_trading_order(account_id=acct_def.id, symbol="AAPL",
                             side=OrderDirection.BUY, status=OrderStatus.NEW)
        locked = create_trading_order(account_id=acct_def.id, symbol="AAPL",
                                      side=OrderDirection.SELL, order_type=OrderType.MARKET,
                                      status=OrderStatus.WASHTRADE_LOCKED)

        TradeManager()._check_all_washtrade_locked_orders()

        assert locked.id not in _PromotingAccount.submitted
        assert get_instance(TradingOrder, locked.id).status == OrderStatus.WASHTRADE_LOCKED


class TestLockExpiry:
    """A lock that outlives its signal is a deadlock, not a wait.

    Measured 2026-08-05: 13 entries stuck up to 9 days, every one of them blocked by a
    protective SELL_STOP guarding an OPEN position — a blocker that stays working at the broker
    for the life of that position and so can never clear on its own.
    """

    def _setup(self, monkeypatch, age_hours):
        from datetime import datetime, timedelta, timezone
        _PromotingAccount.submitted = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: _PromotingAccount)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        locked = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.WASHTRADE_LOCKED,
            transaction_id=txn.id,
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours))
        return acct_def, txn, locked

    def _blocker(self, acct_def):
        return create_trading_order(account_id=acct_def.id, symbol="AAPL",
                                    side=OrderDirection.SELL, status=OrderStatus.NEW,
                                    order_type=OrderType.SELL_STOP)

    def test_fresh_lock_is_left_alone(self, monkeypatch):
        acct_def, txn, locked = self._setup(monkeypatch, age_hours=2)
        self._blocker(acct_def)

        TradeManager()._check_all_washtrade_locked_orders()

        assert get_instance(TradingOrder, locked.id).status == OrderStatus.WASHTRADE_LOCKED

    def test_stale_lock_is_cancelled(self, monkeypatch):
        acct_def, txn, locked = self._setup(monkeypatch, age_hours=48)
        self._blocker(acct_def)

        TradeManager()._check_all_washtrade_locked_orders()

        assert get_instance(TradingOrder, locked.id).status == OrderStatus.CANCELED
        assert locked.id not in _PromotingAccount.submitted

    def test_stale_lock_fails_its_waiting_transaction(self, monkeypatch):
        from ba2_trade_platform.core.models import Transaction
        acct_def, txn, locked = self._setup(monkeypatch, age_hours=48)
        self._blocker(acct_def)

        TradeManager()._check_all_washtrade_locked_orders()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.FAILED

    def test_stale_lock_cancels_its_waiting_protective_leg(self, monkeypatch):
        """Otherwise the leg waits forever on a parent status that will never arrive."""
        acct_def, txn, locked = self._setup(monkeypatch, age_hours=48)
        self._blocker(acct_def)
        leg = create_trading_order(account_id=acct_def.id, symbol="AAPL",
                                   side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                                   status=OrderStatus.WAITING_TRIGGER, transaction_id=txn.id,
                                   depends_on_order=locked.id,
                                   depends_order_status_trigger=OrderStatus.FILLED)

        TradeManager()._check_all_washtrade_locked_orders()

        assert get_instance(TradingOrder, leg.id).status == OrderStatus.CANCELED

    def test_clearing_beats_expiry(self, monkeypatch):
        """A stale lock whose symbol is now CLEAR submits normally — it is not cancelled.

        Expiry is the give-up path for orders that are still blocked, not a blanket age cap.
        """
        acct_def, txn, locked = self._setup(monkeypatch, age_hours=48)
        # no blocker created

        TradeManager()._check_all_washtrade_locked_orders()

        assert locked.id in _PromotingAccount.submitted
        assert get_instance(TradingOrder, locked.id).status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# 2026-09-22 incident. An entry the BROKER killed unfilled left its transaction
# WAITING forever, its protective leg PENDING_CANCEL forever, and the wash-trade
# rejection that caused it unrecorded. See docs/WASHTRADE-LOCK.md.
# ---------------------------------------------------------------------------

import importlib
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError

from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.TradeManager import classify_waiting_entry
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_common.core.washtrade import (
    WASHTRADE_COMPLEX_SUBMIT_KEY, WASHTRADE_REJECTED_KEY,
    went_out_as_contended_complex, was_rejected_as_washtrade,
)

# NOT `import ...AlpacaAccount as alpaca_mod`: the accounts package re-exports the CLASS
# under that same attribute name, so the plain import binds the class and shadows the
# module whose constants these tests tune.
alpaca_mod = importlib.import_module("ba2_trade_platform.modules.accounts.AlpacaAccount")


@contextmanager
def capture_logs(*logger_names, level=logging.DEBUG):
    """Collect log records from the platform loggers.

    pytest's `caplog` cannot see these: both `ba2_trade_platform` and `ba2_common` set
    `propagate = False` (logger.py), so nothing reaches the root handler caplog installs.
    Attaching a collector to the named loggers is the only way to assert on them — and
    these assertions matter, because "refuse loudly" is a requirement here, not a detail.
    """
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    handler.setLevel(level)
    loggers = [logging.getLogger(name) for name in logger_names]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        if lg.level > level:
            lg.setLevel(level)
    try:
        yield records
    finally:
        for lg, lvl in zip(loggers, previous):
            lg.removeHandler(handler)
            lg.setLevel(lvl)


def _messages(records, min_level=logging.ERROR):
    return [r.getMessage() for r in records if r.levelno >= min_level]


class _Entry:
    """Minimal stand-in for the four fields classify_waiting_entry reads."""

    def __init__(self, status, filled_qty=None, open_price=None, broker_order_id=None):
        self.status = status
        self.filled_qty = filled_qty
        self.open_price = open_price
        self.broker_order_id = broker_order_id


class TestClassifyWaitingEntry:
    """The verdict that decides whether a WAITING transaction is given up on.

    The distinction that matters most is terminal-UNFILLED (give up) versus
    terminal-PARTIALLY-FILLED (a real position; never give up).
    """

    def test_no_entry_row_yet_waits(self):
        assert classify_waiting_entry([]) == "wait"

    def test_in_flight_entry_waits(self):
        assert classify_waiting_entry([_Entry(OrderStatus.NEW)]) == "wait"

    def test_partially_filled_and_still_working_waits(self):
        """PARTIALLY_FILLED is not terminal - it is progressing toward FILLED."""
        assert classify_waiting_entry(
            [_Entry(OrderStatus.PARTIALLY_FILLED, filled_qty=3.0)]) == "wait"

    def test_one_working_entry_holds_the_whole_verdict(self):
        assert classify_waiting_entry([
            _Entry(OrderStatus.CANCELED, filled_qty=0),
            _Entry(OrderStatus.PENDING),
        ]) == "wait"

    def test_terminal_and_unfilled_is_dead(self):
        assert classify_waiting_entry([_Entry(OrderStatus.CANCELED, filled_qty=0)]) == "dead"

    def test_terminal_with_null_filled_qty_and_no_broker_id_is_dead(self):
        """An order the broker never saw cannot have traded - a measured fact."""
        assert classify_waiting_entry([_Entry(OrderStatus.CANCELED)]) == "dead"

    def test_null_filled_qty_on_a_broker_touched_order_is_UNKNOWN_not_zero(self):
        """The row was never synced, so how much traded is genuinely unknown. Unknown
        is not zero, and a transaction is never failed on an unknown."""
        assert classify_waiting_entry(
            [_Entry(OrderStatus.CANCELED, broker_order_id="b-1")]) == "ambiguous"

    def test_rejected_and_expired_are_dead_too(self):
        for status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.ERROR):
            assert classify_waiting_entry([_Entry(status, filled_qty=0)]) == "dead", status

    def test_cancelled_after_a_PARTIAL_fill_is_never_dead(self):
        """A cancel that raced a fill left REAL shares; failing it forgets a position."""
        assert classify_waiting_entry(
            [_Entry(OrderStatus.CANCELED, filled_qty=4.0, open_price=101.0)]) == "filled"

    def test_fully_filled_is_not_dead(self):
        assert classify_waiting_entry(
            [_Entry(OrderStatus.FILLED, filled_qty=10.0, open_price=101.0)]) == "filled"

    def test_zero_qty_but_a_fill_price_is_ambiguous(self):
        """Two witnesses disagreeing is not a licence to act."""
        assert classify_waiting_entry(
            [_Entry(OrderStatus.CANCELED, filled_qty=0, open_price=101.0)]) == "ambiguous"


class _CancellingAccount(MockAccount):
    """Account double whose cancel_order behaves like the real broker path.

    Matches AlpacaAccount.cancel_order: takes an ORDER ID and sets PENDING_CANCEL
    (never an optimistic CANCELED) - the by-id reconciliation resolves it later.
    """

    cancelled = []

    def cancel_order(self, order_id):
        _CancellingAccount.cancelled.append(order_id)
        row = get_instance(TradingOrder, order_id)
        row.status = OrderStatus.PENDING_CANCEL
        update_instance(row)
        return True


class TestStrandedWaitingTransactions:
    """FIX 1 - a cancelled entry now fails its transaction.

    REGRESSION (dev 2026-09-22): txns 28/29 (SHOP, UNH) and 131/133 (NVDA, AVGO),
    all expert 2 / account 1. Each entry was accepted by Alpaca and CANCELED by it
    within ~100 ms, filled 0. `_fail_unsent_entry` - the only path to FAILED -
    refuses anything carrying a broker_order_id, so nothing failed them, and
    enter_market's safety check then retired each symbol for that expert permanently.
    """

    def _setup(self, monkeypatch, entry_status=OrderStatus.CANCELED, filled_qty=0.0,
               open_price=None, age_minutes=120.0, txn_status=TransactionStatus.WAITING,
               data=None):
        _CancellingAccount.cancelled = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class",
                            lambda provider: _CancellingAccount)
        txn = create_transaction(symbol="NVDA", side=OrderDirection.BUY, status=txn_status)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=entry_status, transaction_id=txn.id,
            filled_qty=filled_qty, open_price=open_price, broker_order_id="b-entry-1",
            data=data,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes))
        return acct_def, txn, entry

    def test_terminal_unfilled_entry_fails_its_transaction(self, monkeypatch):
        acct_def, txn, entry = self._setup(monkeypatch)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.FAILED

    def test_entry_status_is_left_exactly_as_the_broker_set_it(self, monkeypatch):
        """The give-up path fails the TRANSACTION; it never rewrites broker state."""
        acct_def, txn, entry = self._setup(monkeypatch)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(TradingOrder, entry.id).status == OrderStatus.CANCELED

    def test_leaves_no_non_terminal_child_behind(self, monkeypatch):
        """Unsent legs are cancelled here; legs working at the broker are cancelled
        AT the broker (-> PENDING_CANCEL), never written off locally."""
        acct_def, txn, entry = self._setup(monkeypatch)
        staged = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
            transaction_id=txn.id, depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED)
        working = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.NEW,
            transaction_id=txn.id, broker_order_id="b-leg-9")

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(TradingOrder, staged.id).status == OrderStatus.CANCELED
        assert working.id in _CancellingAccount.cancelled
        assert get_instance(TradingOrder, working.id).status == OrderStatus.PENDING_CANCEL

    def test_a_working_leg_is_never_written_off_locally(self, monkeypatch):
        """Writing CANCELED on a leg still live at the broker would hide a real order."""
        acct_def, txn, entry = self._setup(monkeypatch)
        working = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.NEW,
            transaction_id=txn.id, broker_order_id="b-leg-9")

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(TradingOrder, working.id).status != OrderStatus.CANCELED

    def test_PARTIALLY_filled_entry_is_NOT_failed(self, monkeypatch):
        """THE distinction. A cancel that raced a fill left real shares, so the
        transaction must survive or the platform forgets a position it owns."""
        acct_def, txn, entry = self._setup(monkeypatch, filled_qty=4.0, open_price=101.0)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.WAITING

    def test_partial_fill_leaves_its_protective_leg_alone(self, monkeypatch):
        acct_def, txn, entry = self._setup(monkeypatch, filled_qty=4.0, open_price=101.0)
        leg = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
            transaction_id=txn.id, depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(TradingOrder, leg.id).status == OrderStatus.WAITING_TRIGGER
        assert _CancellingAccount.cancelled == []

    def test_still_working_entry_is_not_failed(self, monkeypatch):
        acct_def, txn, entry = self._setup(monkeypatch, entry_status=OrderStatus.NEW)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.WAITING

    def test_ambiguous_fill_evidence_is_refused_loudly(self, monkeypatch):
        """filled_qty says nothing traded, open_price says something did. Refuse, and
        say so at ERROR - never decide a position's fate on a contradiction."""
        acct_def, txn, entry = self._setup(monkeypatch, filled_qty=0.0, open_price=101.0)

        with capture_logs("ba2_trade_platform") as records:
            TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.WAITING
        assert any("disagree" in m for m in _messages(records))

    def test_fresh_entry_is_inside_the_grace_period(self, monkeypatch):
        """A transaction and its entry are two separate writes; never judge between them."""
        acct_def, txn, entry = self._setup(monkeypatch, age_minutes=1.0)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.WAITING

    def test_opened_transaction_is_never_touched(self, monkeypatch):
        """An OPENED transaction has a real position behind it."""
        acct_def, txn, entry = self._setup(monkeypatch, txn_status=TransactionStatus.OPENED)

        TradeManager()._check_stranded_waiting_transactions()

        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED


class _FakeAlpacaOrder:
    """The handful of attributes alpaca_order_to_tradingorder reads."""

    def __init__(self, broker_id, status, filled_qty="0", filled_avg_price=None):
        self.id = broker_id
        self.client_order_id = None
        self.symbol = "NVDA"
        self.qty = "10"
        self.side = "sell"
        self.type = "stop"
        self.order_class = None
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.limit_price = None
        self.stop_price = 100.0
        self.created_at = datetime.now(timezone.utc)
        self.legs = None


class _FakeClient:
    """Stands in for alpaca's TradingClient, for get_order_by_id only.

    `asked` is the by-id CALL BUDGET under test: its length must equal exactly the
    number of PENDING_CANCEL orders the order listing did not already answer.
    """

    def __init__(self, answer=None, answers=None):
        self.answer = answer
        self.answers = answers or {}
        self.asked = []

    def get_order_by_id(self, order_id):
        self.asked.append(order_id)
        reply = self.answers.get(order_id, self.answer)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _alpaca_double(account_id, client):
    """An AlpacaAccount with no credentials and no DB lookups. Only the two methods
    under test are exercised, and both take their broker state from `client`."""
    acct = object.__new__(AlpacaAccount)
    acct.id = account_id
    acct.client = client
    return acct


class TestPendingCancelDirectLookup:
    """FIX 2 - PENDING_CANCEL resolved by a DIRECT by-id lookup.

    REGRESSION (dev 2026-09-22): four protective SELL_STOPs sat PENDING_CANCEL for up
    to 8 days. The promotion ran only over the paginated GetOrdersRequest listing, and
    an OCO leg is never in it (legs are metadata on a parent that was itself cancelled),
    while Step 4's "missing from the listing" sweep puts every OCO leg in its safe set.
    get_order_by_id answers instantly.
    """

    def _pending(self, age_hours=2.0, broker_order_id="b-leg-1", acct_def=None):
        acct_def = acct_def or create_account_definition()
        order = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING_CANCEL,
            broker_order_id=broker_order_id,
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours))
        return acct_def, order

    def test_broker_says_canceled_so_the_order_is_canceled(self):
        acct_def, order = self._pending()
        acct = _alpaca_double(acct_def.id,
                              _FakeClient(_FakeAlpacaOrder("b-leg-1", "canceled")))

        assert acct._reconcile_pending_cancel_orders(listed_broker_ids=()) == 1

        assert acct.client.asked == ["b-leg-1"]
        assert get_instance(TradingOrder, order.id).status == OrderStatus.CANCELED

    def test_broker_says_filled_so_the_race_is_recorded(self):
        """The cancel lost to a fill: resolve_pending_cancel hands back FILLED."""
        acct_def, order = self._pending()
        acct = _alpaca_double(acct_def.id, _FakeClient(
            _FakeAlpacaOrder("b-leg-1", "filled", filled_qty="10", filled_avg_price="99.5")))

        acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        row = get_instance(TradingOrder, order.id)
        assert row.status == OrderStatus.FILLED
        assert float(row.filled_qty) == 10.0

    def test_still_working_keeps_waiting_and_is_not_optimistically_canceled(self):
        """resolve_pending_cancel returns None while the broker still has it working -
        a dependent replacement must not fire on an invented CANCELED."""
        acct_def, order = self._pending()
        acct = _alpaca_double(acct_def.id, _FakeClient(_FakeAlpacaOrder("b-leg-1", "new")))

        assert acct._reconcile_pending_cancel_orders(listed_broker_ids=()) == 0

        assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL

    def test_a_wait_that_outlives_the_limit_adopts_the_brokers_own_status(self):
        """LAST RESORT. Past 24h with the broker still reporting it working, the cancel
        plainly never took: drop the local PENDING_CANCEL fiction and record what the
        broker actually says. Nothing is invented, and the endless wait ends."""
        acct_def, order = self._pending(age_hours=48.0)
        acct = _alpaca_double(acct_def.id, _FakeClient(_FakeAlpacaOrder("b-leg-1", "new")))

        acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert get_instance(TradingOrder, order.id).status == OrderStatus.NEW

    def test_broker_does_not_know_the_order_is_loud_and_changes_nothing(self):
        """A 40410000 is NOT a confirmation of cancellation - it is also what a
        wrong-account credential returns (docs/WASHTRADE-LOCK.md probe trap)."""
        acct_def, order = self._pending()
        acct = _alpaca_double(acct_def.id, _FakeClient(
            APIError('{"code":40410000,"message":"order not found"}')))

        with capture_logs("ba2_trade_platform") as records:
            acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL
        assert any("does NOT KNOW" in m for m in _messages(records))

    def test_broker_has_not_known_it_for_a_day_ends_the_wait(self):
        acct_def, order = self._pending(age_hours=48.0)
        acct = _alpaca_double(acct_def.id, _FakeClient(
            APIError('{"code":40410000,"message":"order not found"}')))

        with capture_logs("ba2_trade_platform") as records:
            acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert get_instance(TradingOrder, order.id).status == OrderStatus.CANCELED
        assert any("last resort" in m for m in _messages(records))

    def test_a_transport_failure_is_not_read_as_absent(self):
        """'I could not ask' and 'the broker says it never existed' are different facts."""
        acct_def, order = self._pending()
        acct = _alpaca_double(acct_def.id, _FakeClient(
            APIError('{"code":50010000,"message":"internal error"}')))

        acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL

    def test_get_broker_order_or_absent_separates_the_two(self):
        acct = _alpaca_double(1, _FakeClient(
            APIError('{"code":40410000,"message":"order not found"}')))
        assert acct._get_broker_order_or_absent("b-x") == (None, True)

        acct.client = _FakeClient(APIError('{"code":50010000,"message":"boom"}'))
        with pytest.raises(APIError):
            acct._get_broker_order_or_absent("b-x")

    def test_pending_cancel_without_a_broker_id_is_loud(self):
        acct_def = create_account_definition()
        create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING_CANCEL)
        acct = _alpaca_double(acct_def.id,
                              _FakeClient(_FakeAlpacaOrder("b-leg-1", "canceled")))

        with capture_logs("ba2_trade_platform") as records:
            acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert acct.client.asked == []
        assert any("NO broker_order_id" in m for m in _messages(records))


class TestPendingCancelLookupBudget:
    """THIS IS NOT A POLLER. These are the tests that keep it from becoming one.

    The by-id pass is narrow by construction: only PENDING_CANCEL orders, only inside
    refresh_orders, and only the ones the refresh's own listing did not already answer
    for free. A healthy account therefore pays NOTHING for this change.
    """

    def _pending(self, acct_def, broker_order_id, age_hours=2.0):
        return create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.PENDING_CANCEL,
            broker_order_id=broker_order_id,
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours))

    def test_a_healthy_account_issues_ZERO_by_id_calls(self):
        """No PENDING_CANCEL at all: not one extra API call. If this ever fails,
        something has started polling the broker per order."""
        acct_def = create_account_definition()
        create_trading_order(account_id=acct_def.id, symbol="NVDA",
                             side=OrderDirection.BUY, status=OrderStatus.FILLED,
                             broker_order_id="b-filled")
        create_trading_order(account_id=acct_def.id, symbol="NVDA",
                             side=OrderDirection.SELL, status=OrderStatus.NEW,
                             broker_order_id="b-working")
        acct = _alpaca_double(acct_def.id, _FakeClient(_FakeAlpacaOrder("x", "canceled")))

        acct._reconcile_pending_cancel_orders(
            listed_broker_ids={"b-filled", "b-working"})

        assert acct.client.asked == []

    def test_only_the_orders_the_listing_missed_are_asked_about(self):
        """The listing already answered the covered ones in the main refresh loop."""
        acct_def = create_account_definition()
        self._pending(acct_def, "b-listed-1")
        self._pending(acct_def, "b-listed-2")
        self._pending(acct_def, "b-missing-1")
        self._pending(acct_def, "b-missing-2")
        acct = _alpaca_double(acct_def.id, _FakeClient(answers={
            "b-missing-1": _FakeAlpacaOrder("b-missing-1", "canceled"),
            "b-missing-2": _FakeAlpacaOrder("b-missing-2", "canceled"),
        }))

        acct._reconcile_pending_cancel_orders(
            listed_broker_ids={"b-listed-1", "b-listed-2"})

        assert sorted(acct.client.asked) == ["b-missing-1", "b-missing-2"]

    def test_a_listed_order_is_left_for_the_main_loop(self):
        acct_def = create_account_definition()
        listed = self._pending(acct_def, "b-listed-1")
        acct = _alpaca_double(acct_def.id,
                              _FakeClient(_FakeAlpacaOrder("b-listed-1", "canceled")))

        acct._reconcile_pending_cancel_orders(listed_broker_ids={"b-listed-1"})

        assert acct.client.asked == []
        assert get_instance(TradingOrder, listed.id).status == OrderStatus.PENDING_CANCEL

    def test_the_cap_bounds_one_refresh_and_says_so(self, monkeypatch):
        """A pathological state degrades into 'slower to heal', never a flood."""
        monkeypatch.setattr(alpaca_mod, "_PENDING_CANCEL_LOOKUP_BUDGET", 3)
        acct_def = create_account_definition()
        for i in range(7):
            self._pending(acct_def, f"b-stuck-{i}", age_hours=float(i + 1))
        acct = _alpaca_double(acct_def.id, _FakeClient(_FakeAlpacaOrder("x", "new")))

        with capture_logs("ba2_trade_platform") as records:
            acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert len(acct.client.asked) == 3
        assert any("deferring 4" in m for m in _messages(records, logging.WARNING))

    def test_the_cap_takes_the_OLDEST_first_so_progress_is_guaranteed(self, monkeypatch):
        monkeypatch.setattr(alpaca_mod, "_PENDING_CANCEL_LOOKUP_BUDGET", 2)
        acct_def = create_account_definition()
        self._pending(acct_def, "b-young", age_hours=1.0)
        self._pending(acct_def, "b-middle", age_hours=50.0)
        self._pending(acct_def, "b-oldest", age_hours=200.0)
        acct = _alpaca_double(acct_def.id, _FakeClient(_FakeAlpacaOrder("x", "new")))

        acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert acct.client.asked == ["b-oldest", "b-middle"]

    def test_another_accounts_stuck_order_is_not_asked_about(self):
        """The 40410000 probe trap: an id from another Alpaca account answers 'not
        found' with this account's credentials, which looks exactly like a dead order."""
        mine = create_account_definition()
        theirs = create_account_definition()
        self._pending(theirs, "b-not-mine")
        acct = _alpaca_double(mine.id, _FakeClient(_FakeAlpacaOrder("x", "canceled")))

        acct._reconcile_pending_cancel_orders(listed_broker_ids=())

        assert acct.client.asked == []


class TestComplexOrderRejectionRecorded:
    """FIX 3 - the complex-order exemption is attempted, but no longer trusted blindly.

    Measured 2026-09-22: four contended BRACKET entries were accepted by Alpaca and
    cancelled by it within ~100 ms, unfilled, the blocker being another expert's
    resting protective SELL_STOP. The exemption does not hold for that blocker shape.
    Making such a stop a hard blocker again would only rebuild the 2026-08-05 deadlock,
    so the attempt stands - and its failure is recorded and handed to the give-up path.
    """

    def test_a_contended_complex_submission_is_stamped_on_the_order(self):
        acct = _RecordingAccount(create_account_definition().id)
        blocker = create_trading_order(account_id=acct.id, symbol="AAPL",
                                       side=OrderDirection.SELL, status=OrderStatus.NEW,
                                       order_type=OrderType.SELL_STOP)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        order = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.BUY, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING, transaction_id=txn.id)

        result = AccountInterface.submit_order(acct, order, tp_price=200.0, sl_price=90.0)

        assert went_out_as_contended_complex(result)
        persisted = get_instance(TradingOrder, result.id)
        assert persisted.data[WASHTRADE_COMPLEX_SUBMIT_KEY]["blocker_order_id"] == blocker.id

    def test_an_uncontended_submission_is_not_stamped(self):
        acct = _RecordingAccount(create_account_definition().id)
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        order = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=1.0,
                             side=OrderDirection.BUY, order_type=OrderType.MARKET,
                             status=OrderStatus.PENDING, transaction_id=txn.id)

        result = AccountInterface.submit_order(acct, order, tp_price=200.0, sl_price=90.0)

        assert not went_out_as_contended_complex(result)

    def test_broker_cancels_the_complex_order_so_it_is_recorded_and_handed_on(
            self, monkeypatch):
        """The whole incident, end to end: the exemption is taken, the broker kills the
        order unfilled anyway, and that is recognised as the wash-trade rejection it is."""
        _CancellingAccount.cancelled = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class",
                            lambda provider: _CancellingAccount)
        txn = create_transaction(symbol="NVDA", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.CANCELED,
            transaction_id=txn.id, filled_qty=0.0, broker_order_id="b-entry-1",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            data={WASHTRADE_COMPLEX_SUBMIT_KEY: {
                "submitted_at": "2026-09-21T13:42:56+00:00",
                "blocker_order_id": 263,
                "blocker_order_type": "sell_stop",
                "blocker_status": "new",
            }})

        with capture_logs("ba2_trade_platform") as records:
            TradeManager()._check_stranded_waiting_transactions()

        stamped = get_instance(TradingOrder, entry.id)
        assert was_rejected_as_washtrade(stamped)
        assert stamped.data[WASHTRADE_REJECTED_KEY]["blocker_order_id"] == 263
        assert stamped.data[WASHTRADE_REJECTED_KEY]["final_status"] == "canceled"
        assert any("WASH-TRADE REJECTION CONFIRMED" in m for m in _messages(records))
        # ...and handed to Fix 1's give-up path, which is what unblocks the symbol.
        assert get_instance(Transaction, txn.id).status == TransactionStatus.FAILED

    def test_an_unstamped_dead_entry_is_not_called_a_wash_trade_rejection(self, monkeypatch):
        """Every other way an entry can die must not be mislabelled as a wash trade."""
        _CancellingAccount.cancelled = []
        acct_def = create_account_definition()
        monkeypatch.setattr(accounts_mod, "get_account_class",
                            lambda provider: _CancellingAccount)
        txn = create_transaction(symbol="NVDA", side=OrderDirection.BUY,
                                 status=TransactionStatus.WAITING)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NVDA", side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.CANCELED,
            transaction_id=txn.id, filled_qty=0.0, broker_order_id="b-entry-1",
            created_at=datetime.now(timezone.utc) - timedelta(hours=1))

        TradeManager()._check_stranded_waiting_transactions()

        assert not was_rejected_as_washtrade(get_instance(TradingOrder, entry.id))
        assert get_instance(Transaction, txn.id).status == TransactionStatus.FAILED

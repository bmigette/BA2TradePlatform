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

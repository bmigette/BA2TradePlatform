"""Reconcile positions closed directly at the broker.

The order-driven refresh_transactions can only close a transaction when the
platform's OWN orders fill/balance/terminate. A position closed DIRECTLY at the
broker (outside the platform) leaves the entry order FILLED (not a terminal
status) and every protective SELL/OCO order CANCELED, with no filled sell — so
refresh_transactions never closes it and the trade shows "alive" forever (the
2026-06-29 prod incident: 10 Alcapa Live trades stuck OPENED after a manual
broker close). reconcile_externally_closed_transactions() closes such trades by
checking the broker's actual positions.
"""
from datetime import datetime, timezone, timedelta

import pytest

from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction, create_trading_order
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)


def _open_txn(acct_id, symbol="AMPX", qty=2.0, age_minutes=120, with_resting_order=False):
    """An OPENED long whose entry filled and whose protective orders are all canceled."""
    txn = create_transaction(symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=12.11)
    # Backdate open_date past the grace period.
    txn.open_date = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    update_instance(txn)
    create_trading_order(
        account_id=acct_id, symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=qty,
        transaction_id=txn.id,
    )
    create_trading_order(
        account_id=acct_id, symbol=symbol, quantity=qty, side=OrderDirection.SELL,
        order_type=OrderType.OCO, status=OrderStatus.CANCELED, filled_qty=0.0,
        transaction_id=txn.id,
    )
    if with_resting_order:
        create_trading_order(
            account_id=acct_id, symbol=symbol, quantity=qty, side=OrderDirection.SELL,
            order_type=OrderType.OCO, status=OrderStatus.WAITING_TRIGGER,
            transaction_id=txn.id,
        )
    return txn


class TestReconcileExternallyClosed:
    def test_closes_when_broker_flat_for_symbol(self):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []  # broker holds nothing
        txn = _open_txn(acct.id, "AMPX")

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 1
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "position_not_at_broker"

    def test_closes_stuck_closing_transaction_when_broker_flat(self):
        """2026-07-14 prod incident: a transaction can get stuck in CLOSING (the platform's
        own close attempt failed/canceled repeatedly) while the position was ALREADY closed
        directly at the broker in the meantime. An OPENED-only filter left it unreconciled
        for 5 days until a stale retry surfaced it via a broker error. CLOSING must be
        checked too, not just OPENED."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []  # broker holds nothing
        txn = _open_txn(acct.id, "AMPX")
        txn = get_instance(Transaction, txn.id)
        txn.status = TransactionStatus.CLOSING
        update_instance(txn)

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 1
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "position_not_at_broker"

    def test_keeps_when_broker_still_holds_position(self):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": 2.0}]
        txn = _open_txn(acct.id, "AMPX")

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_does_not_close_on_position_fetch_error(self, monkeypatch):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        txn = _open_txn(acct.id, "AMPX")

        def boom():
            raise RuntimeError("broker API down")
        monkeypatch.setattr(account, "get_positions", boom)

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 0  # never close on an API error
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_does_not_close_when_fetch_returns_none(self):
        """The 2026-07-03 prod incident: AlpacaAccount.get_positions() doesn't raise on a
        network/DNS failure, it CATCHES and returns a sentinel — so the "never close on a
        fetch failure" guard must trigger on that sentinel (None), not rely on an exception
        that never comes. A real empty book is `[]` and DOES reconcile; only `None` (fetch
        failed) must be a no-op. Regression test for the 8-transaction false mass-close during
        a local DNS outage where the broker held the positions the entire time."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = None  # fetch failed but returned normally (no exception)
        txn = _open_txn(acct.id, "AMPX")

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_skips_fresh_transaction_within_grace(self):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []
        txn = _open_txn(acct.id, "AMPX", age_minutes=1)  # just opened

        closed = account.reconcile_externally_closed_transactions(grace_period_minutes=5)

        assert closed == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_cancels_resting_orders_on_close(self):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []
        txn = _open_txn(acct.id, "AMPX", with_resting_order=True)

        account.reconcile_externally_closed_transactions()

        resting = [o for o in _orders_for(txn.id) if o.order_type == OrderType.OCO
                   and o.status == OrderStatus.CANCELED]
        # both the already-canceled OCO and the formerly-WAITING_TRIGGER one are CANCELED
        assert all(o.status == OrderStatus.CANCELED for o in _orders_for(txn.id)
                   if o.side == OrderDirection.SELL)

    def test_skips_option_transactions(self):
        """get_positions reports equities only; an option transaction must not be
        closed here (its lifecycle is reconciled separately)."""
        from ba2_trade_platform.core.types import AssetClass
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []  # no equity positions
        txn = create_transaction(symbol="AMPX", quantity=1.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=2.0)
        txn.open_date = datetime.now(timezone.utc) - timedelta(minutes=120)
        update_instance(txn)
        create_trading_order(
            account_id=acct.id, symbol="AMPX", quantity=1.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=1.0,
            transaction_id=txn.id, asset_class=AssetClass.OPTION,
        )

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_unreadable_position_qty_does_not_force_a_close(self):
        """THE DEFECT: an UNREADABLE per-position quantity dropped the symbol from
        ``broker_symbols``, so reconcile concluded the position was gone and
        force-closed the transaction -- cancelling its protective stops on the way out.

        ``abs(float(qty or 0))`` is what did it: the ``or 0`` turns None into a
        measured zero BEFORE the ``except (TypeError, ValueError)`` that was written
        for exactly this case can ever see it, so the guard was dead code. A broker
        that reports a position but not its size is telling us the position EXISTS."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": None}]
        txn = _open_txn(acct.id, "AMPX")

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 0, "an unreadable quantity is not a flat book"
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_unreadable_position_qty_is_logged_loudly(self, monkeypatch):
        """Silently keeping the position would just move the blindness. Say so."""
        import sys
        module = sys.modules["ba2_common.core.interfaces.ReadOnlyAccountInterface"]
        errors = []
        monkeypatch.setattr(module.logger, "error",
                            lambda msg, *a, **k: errors.append(str(msg)))
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": None}]
        _open_txn(acct.id, "AMPX")

        account.reconcile_externally_closed_transactions()

        assert any("AMPX" in m and "unreadable" in m.lower() for m in errors), errors

    def test_a_garbage_position_qty_also_keeps_the_symbol(self):
        """The ``except (TypeError, ValueError)`` branch the ``or 0`` was defeating:
        a non-numeric qty is unreadable too."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": "n/a"}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_a_missing_qty_key_also_keeps_the_symbol(self):
        """A position dict with no ``qty`` key at all: still a reported position."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX"}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_a_measured_zero_qty_still_reconciles(self):
        """THE INVERSE, and the whole point. A broker that reports qty EXACTLY 0 has
        MEASURED a flat position -- that is the signal reconcile exists to act on. The
        fix must not blunt it into 'unknown, keep it open forever'."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": 0.0}]
        txn = _open_txn(acct.id, "AMPX")

        closed = account.reconcile_externally_closed_transactions()

        assert closed == 1, "a measured zero position is a genuine close signal"
        assert get_instance(Transaction, txn.id).close_reason == "position_not_at_broker"

    def test_a_string_zero_qty_still_reconciles(self):
        """Alpaca reports quantities as decimal STRINGS; "0" is a measured zero."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": "0"}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 1
        assert get_instance(Transaction, txn.id).status == TransactionStatus.CLOSED

    def test_an_unreadable_qty_on_ANOTHER_symbol_does_not_shield_this_one(self):
        """Keeping an unreadable symbol must not accidentally keep every symbol."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "MSFT", "qty": None}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 1
        assert get_instance(Transaction, txn.id).status == TransactionStatus.CLOSED

    def test_only_affects_this_account(self):
        acct1 = create_account_definition(name="A1")
        acct2 = create_account_definition(name="A2")
        account1 = MockAccount(acct1.id)
        account1._positions = []
        txn2 = _open_txn(acct2.id, "AMPX")  # belongs to a DIFFERENT account

        closed = account1.reconcile_externally_closed_transactions()

        assert closed == 0
        assert get_instance(Transaction, txn2.id).status == TransactionStatus.OPENED


def _orders_for(txn_id):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    with get_db() as session:
        return list(session.exec(select(TradingOrder).where(TradingOrder.transaction_id == txn_id)).all())


class TestReconcileShortPositions:
    """A SHORT position at the broker reports a NEGATIVE quantity."""

    def test_a_short_broker_position_is_still_a_position(self):
        """`abs()` is load-bearing: without it a short reads as <= 0 and reconcile
        force-closes a live short, cancelling its stops."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": -2.0}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_a_short_reported_as_a_negative_string_is_also_a_position(self):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": "AMPX", "qty": "-2"}]
        txn = _open_txn(acct.id, "AMPX")

        assert account.reconcile_externally_closed_transactions() == 0
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

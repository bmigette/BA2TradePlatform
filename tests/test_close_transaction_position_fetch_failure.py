"""A positions-fetch OUTAGE must never be read as "the position is gone".

``get_positions()`` has a deliberate tri-state contract: a list of positions on
success, ``[]`` when the account is genuinely flat, and ``None`` when the FETCH
ITSELF FAILED (network/DNS/auth). See ``AlpacaAccount.get_positions`` and the
2026-07-03 prod incident, where a local DNS outage that swallowed to ``[]`` made
the reconciler mass-close 8 real open transactions.

``AccountInterface.close_transaction`` retries an ERRORed close order, and first
checks whether the broker still holds the position — if not, it force-closes the
transaction with ``close_reason="position_not_at_broker"`` instead of retrying.
That check used ``for pos in (broker_positions or [])``, which collapses the
``None`` sentinel into "broker holds nothing" — so a transient API outage
force-closed a REAL, still-open transaction in the DB.

The safe answer on ``None`` is the same one the ``except`` branch two lines below
already reaches for an exception, and the same one
``ReadOnlyAccountInterface.reconcile_externally_closed_transactions`` reaches
(``if positions is None: return 0``): do NOT conclude the position is absent —
proceed with the close-order retry.
"""
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


def _txn_with_errored_close_order(acct_id):
    """A filled long whose closing MARKET order is stuck in ERROR (the retry path)."""
    txn = create_transaction(symbol=SYMBOL, quantity=QTY, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0)
    create_trading_order(
        account_id=acct_id, symbol=SYMBOL, quantity=QTY, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=QTY,
        transaction_id=txn.id, open_price=150.0,
    )
    close_order = create_trading_order(
        account_id=acct_id, symbol=SYMBOL, quantity=QTY, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.ERROR,
        transaction_id=txn.id,
        comment=f"Closing position for transaction {txn.id}",
    )
    return txn, close_order


def _spy_submits(account, monkeypatch):
    """Record every order handed to submit_order (the retry path submits a FRESH
    close order; MockAccount.submit_order doesn't persist it, so it has no id)."""
    submitted = []
    original = account.submit_order

    def _capture(order, is_closing_order=False):
        submitted.append(order)
        return original(order, is_closing_order=is_closing_order)

    monkeypatch.setattr(account, "submit_order", _capture)
    return submitted


class TestCloseTransactionPositionFetchFailure:
    def test_fetch_failure_does_not_force_close_the_transaction(self):
        """THE BUG: get_positions() returned None (outage) and the transaction was
        force-closed as position_not_at_broker while the broker still held it."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = None  # fetch FAILED (returned normally, no exception)
        txn, _ = _txn_with_errored_close_order(acct.id)

        result = account.close_transaction(txn.id)

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status != TransactionStatus.CLOSED, (
            "A positions-fetch outage must NOT close a real open transaction"
        )
        assert fresh.close_reason != "position_not_at_broker"
        assert "no longer at broker" not in result["message"]

    def test_fetch_failure_still_retries_the_errored_close_order(self, monkeypatch):
        """Aborting the 'position is gone' conclusion must fall through to the retry,
        exactly as the exception branch already does — not silently do nothing."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = None
        txn, close_order = _txn_with_errored_close_order(acct.id)
        submitted = _spy_submits(account, monkeypatch)

        result = account.close_transaction(txn.id)

        assert result["success"] is True
        assert len(submitted) == 1                       # a FRESH close order went out
        assert submitted[0].side == OrderDirection.SELL
        assert submitted[0].order_type == OrderType.MARKET
        assert submitted[0].id != close_order.id
        assert get_instance(TradingOrder, close_order.id).status == OrderStatus.CANCELED

    def test_genuinely_flat_broker_still_force_closes(self):
        """`[]` is a REAL flat account and must keep force-closing — the fix must
        distinguish None from [], not disable the check."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = []  # broker genuinely holds nothing
        txn, close_order = _txn_with_errored_close_order(acct.id)

        result = account.close_transaction(txn.id)

        assert result["success"] is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_reason == "position_not_at_broker"
        assert get_instance(TradingOrder, close_order.id).status == OrderStatus.CANCELED

    def test_position_still_held_retries_instead_of_closing(self, monkeypatch):
        acct = create_account_definition()
        account = MockAccount(acct.id)
        account._positions = [{"symbol": SYMBOL, "qty": QTY}]
        txn, close_order = _txn_with_errored_close_order(acct.id)
        submitted = _spy_submits(account, monkeypatch)

        result = account.close_transaction(txn.id)

        assert result["success"] is True
        assert len(submitted) == 1
        assert get_instance(Transaction, txn.id).status != TransactionStatus.CLOSED

    def test_fetch_exception_still_retries(self, monkeypatch):
        """Pre-existing behaviour, pinned: an exception from get_positions() proceeds
        with the retry rather than concluding the position is gone."""
        acct = create_account_definition()
        account = MockAccount(acct.id)
        txn, close_order = _txn_with_errored_close_order(acct.id)
        submitted = _spy_submits(account, monkeypatch)

        def boom():
            raise RuntimeError("broker API down")
        monkeypatch.setattr(account, "get_positions", boom)

        result = account.close_transaction(txn.id)

        assert result["success"] is True
        assert len(submitted) == 1
        assert get_instance(Transaction, txn.id).status != TransactionStatus.CLOSED

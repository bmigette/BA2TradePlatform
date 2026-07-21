"""Tests for ReadOnlyAccountInterface.refresh_transactions and canceled partial fills.

A cancel-and-replace (TP/SL rebase) can race a live fill: the broker executes
part of the order before honoring the cancel, leaving the order CANCELED with
filled_qty > 0. refresh_transactions() recalculates each open transaction's
quantity from its orders on every call (it runs on every TradeManager cycle,
not just on a detected status change), so if it drops those genuinely-filled
shares it permanently re-inflates the transaction back to the pre-fill
quantity - even overwriting a manual correction made via the Overview UI.
This is the NNE "Quantity Mismatch: broker +8 / transactions +12" incident
that kept reappearing no matter how many times it was manually fixed
(2026-06-24).
"""
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction, create_trading_order
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import AssetClass, OrderDirection, OrderStatus, OrderType


class TestRefreshTransactionsCanceledPartialFill:
    def test_canceled_partial_fill_counted_in_recalculated_quantity(self):
        """A SELL order that partially filled (4/6) before being CANCELED must
        still reduce the recalculated transaction quantity - the 4 shares
        really traded at the broker."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=4.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 2.0

    def test_canceled_zero_fill_does_not_count(self):
        """A CANCELED order that never filled (filled_qty falsy) must not
        contribute - this keeps the never-filled / rejected-leg case
        behaving exactly as before."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=0.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 6.0


class TestRefreshTransactionsMultiLegOptionCombo:
    """A multi-leg option combo (e.g. call_butterfly) writes its "structures"
    quantity onto BOTH its PARENT order (asset_class OPTION, no contract_symbol)
    and each CHILD leg order (contract_symbol + parent_order_id set) - each leg
    independently carries that same structures count, scaled by its own ratio.
    refresh_transactions() must count the parent's filled_qty ONCE and ignore
    the legs entirely; summing every order unconditionally counted one combo
    fill event (1 parent + N legs) times, which - compounding every bar as the
    resulting bogus quantity inflated mark-to-market equity and therefore the
    next trade's position size - produced multi-trillion-scale runaway
    quantities in the options optimization grid (2026-07-21)."""

    def test_child_leg_fills_excluded_from_parent_quantity(self):
        """A child leg's filled_qty must not add onto the parent's - only the
        parent's own filled_qty is the transaction-level position size."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="WMT", quantity=5.0, side=OrderDirection.BUY)
        parent = create_trading_order(
            account_id=acct_def.id, symbol="WMT", quantity=5.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=5.0,
            transaction_id=txn.id, asset_class=AssetClass.OPTION,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="WMT240101C00050000", quantity=999.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=999.0, transaction_id=txn.id, asset_class=AssetClass.OPTION,
            contract_symbol="WMT240101C00050000", parent_order_id=parent.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 5.0

    def test_standalone_single_leg_option_order_still_counted(self):
        """A single-leg option order (carries contract_symbol but NO
        parent_order_id - it IS the fillable order, not a combo child) must
        still count normally; only actual combo children get excluded."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="WMT", quantity=5.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="WMT240101C00050000", quantity=5.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=5.0, transaction_id=txn.id, asset_class=AssetClass.OPTION,
            contract_symbol="WMT240101C00050000",
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 5.0


class TestHasPendingClosingOrder:
    """``ReadOnlyAccountInterface.has_pending_closing_order`` — shared by live
    (``TradeManager.process_open_positions_recommendations``) and the backtest engine
    (``DailyBacktestEngine._held_transactions``) — must report True only while a
    transaction's closing order is still WORKING (not yet filled/canceled), so callers
    managing open positions skip re-evaluating exit rules (and re-submitting a close) until
    it resolves. See testplatform/backend/tests/backtest/test_pending_close_guard.py for the
    backtest-side coverage of the same shared method."""

    def test_true_while_closing_order_still_working(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.NEW,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is True

    def test_false_once_closing_order_is_terminal(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is False

    def test_false_with_no_closing_order_submitted(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )

        assert account.has_pending_closing_order(txn.id) is False

    def test_dependent_tp_sl_order_is_not_treated_as_a_pending_close(self):
        """A dependent TP/SL leg (``depends_on_order`` set) is NOT a market-entry-level
        order, so it must not affect this check at all — a still-WORKING bracket order
        sitting at the broker is normal and must not suppress exit-rule management."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.NEW,
            transaction_id=txn.id, depends_on_order=entry.id,
        )

        assert account.has_pending_closing_order(txn.id) is False

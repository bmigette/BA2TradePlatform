"""Seam 1 — the equity adjust path and the allocation planner must not see options."""
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType,
)


def _record_broker_calls(account):
    """Wrap the double so the test can see what would have reached the broker.

    ``adjust_quantity_with_tpsl`` swallows every per-order cancel failure, so
    "the mock didn't change" is not evidence that nothing was attempted. Only
    the call log is.
    """
    submitted, canceled = [], []
    real_submit, real_cancel = account.submit_order, account.cancel_order

    def _submit(order, *a, **kw):
        submitted.append(order)
        return real_submit(order, *a, **kw)

    def _cancel(order, *a, **kw):
        canceled.append(order)
        return real_cancel(order, *a, **kw)

    account.submit_order = _submit
    account.cancel_order = _cancel
    return submitted, canceled


class TestAdjustQuantityRefusesOptions:
    def test_adjusting_an_option_transaction_is_refused(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-1.0)
        assert result["success"] is False
        assert "OPTION" in result["message"]

    def test_adjusting_an_equity_transaction_is_unaffected(self):
        from ba2_common.core.TransactionHelper import TransactionHelper
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        # Must not be the OPTION refusal. Any other outcome is not this test's business.
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-5.0)
        assert "OPTION" not in (result["message"] or "")

    def test_an_equity_trim_still_goes_through(self):
        """The guard must not cost the equity path the trim it does today."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-4.0)
        assert result["success"] is True, result["message"]
        assert result["orders_created"]
        assert get_instance(Transaction, txn.id).quantity == 6.0

    def test_the_refusal_writes_no_order_and_moves_nothing(self):
        """Caller-obeys. Without the guard this path does NOT merely build a bad
        order: it persists a MARKET row on the UNDERLYING sized in contracts,
        submits it (no TP/SL leg means nothing else ever would), writes the
        transaction down to the post-trim size, and returns success=True."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_common.core.trade_store import orders_where
        from ba2_trade_platform.core.models import Transaction
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, _canceled = _record_broker_calls(account)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION,
                                 take_profit=5.2, stop_loss=1.1)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=2.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=2.0, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        before = len(orders_where(transaction_id=txn.id))

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=-1.0)

        assert submitted == [], "an equity order reached the broker for an option"
        assert len(orders_where(transaction_id=txn.id)) == before
        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 2.0, "the option was written down without closing"
        assert fresh.take_profit == 5.2 and fresh.stop_loss == 1.1
        assert result["success"] is False
        assert result["orders_created"] == []

    def test_the_refusal_cancels_no_protective_leg(self):
        """Caller-obeys. The refusal lands before the existing option TP/SL legs
        are canceled, so a structure cannot be left naked by a rejected resize."""
        from ba2_common.core.TransactionHelper import TransactionHelper
        from ba2_common.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted, canceled = _record_broker_calls(account)
        txn = create_transaction(symbol="AAPL", quantity=2.0,
                                 asset_class=AssetClass.OPTION,
                                 take_profit=5.2, stop_loss=1.1)
        entry = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=2.0,
            transaction_id=txn.id, status=OrderStatus.FILLED, filled_qty=2.0,
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")
        leg = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=2.0,
            transaction_id=txn.id, side=OrderDirection.SELL,
            order_type=OrderType.OCO, status=OrderStatus.NEW,
            limit_price=5.2, stop_price=1.1, depends_on_order=entry.id,
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")

        result = TransactionHelper.adjust_quantity_with_tpsl(
            account=account, transaction=txn, qty_change=1.0)

        assert canceled == [], "an option's protective leg was canceled by the equity path"
        assert submitted == [], "an equity order reached the broker for an option"
        assert get_instance(TradingOrder, leg.id).status == OrderStatus.NEW
        assert result["success"] is False
        assert result["orders_canceled"] == []

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


def _open_equity_and_option(account_id, symbol="AAPL", shares=100.0, contracts=1.0):
    """A covered call: `shares` of `symbol` plus an option whose symbol IS `symbol`.

    Both transactions reach the account only through their TradingOrder, which is
    what ``_open_transaction_ids`` joins on, so each needs a filled order.
    """
    equity = create_transaction(symbol=symbol, quantity=shares)
    create_trading_order(account_id=account_id, symbol=symbol, quantity=shares,
                         transaction_id=equity.id, status=OrderStatus.FILLED,
                         filled_qty=shares)
    option = create_transaction(symbol=symbol, quantity=contracts,
                                side=OrderDirection.SELL,
                                asset_class=AssetClass.OPTION)
    create_trading_order(account_id=account_id, symbol=symbol, quantity=contracts,
                         transaction_id=option.id, side=OrderDirection.SELL,
                         status=OrderStatus.FILLED, filled_qty=contracts,
                         asset_class=AssetClass.OPTION,
                         contract_symbol="AAPL260116C00250000")
    return equity, option


class _FakeAccount:
    """Duck-typed broker for ``build_position_states``: positions and prices only."""

    def __init__(self, account_id, positions, prices):
        self.id = account_id
        self._positions = positions
        self._prices = prices

    def get_positions(self):
        return self._positions

    def get_instrument_current_price(self, symbols):
        return {s: self._prices[s] for s in symbols if s in self._prices}


class _FakePosition:
    def __init__(self, symbol, qty, cost_basis, market_value):
        self.symbol = symbol
        self.qty = qty
        self.cost_basis = cost_basis
        self.market_value = market_value
        self.side = None


class TestAllocationExcludesOptions:
    def test_an_option_transaction_is_not_in_the_allocation_plan(self):
        """An option txn's symbol is the UNDERLYING, so without an asset_class filter the
        allocator treats a covered call as a holding of the stock and can sell the cover."""
        from ba2_trade_platform.core.portfolio_allocation_service import _open_transaction_ids
        acct_def = create_account_definition()
        equity, option = _open_equity_and_option(acct_def.id)

        ids = _open_transaction_ids(acct_def.id, ["AAPL"]).get("AAPL", [])

        assert equity.id in ids
        assert option.id not in ids

    def test_the_equity_side_is_untouched(self):
        """An account holding only equities plans exactly as it did before the filter:
        every open/closing transaction, grouped by symbol, oldest first."""
        from ba2_trade_platform.core.portfolio_allocation_service import _open_transaction_ids
        from ba2_trade_platform.core.types import TransactionStatus
        acct_def = create_account_definition()
        first = create_transaction(symbol="AAPL", quantity=20.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=20.0,
                             transaction_id=first.id, status=OrderStatus.FILLED,
                             filled_qty=20.0)
        second = create_transaction(symbol="AAPL", quantity=10.0,
                                    status=TransactionStatus.CLOSING)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=second.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        other = create_transaction(symbol="MSFT", quantity=5.0)
        create_trading_order(account_id=acct_def.id, symbol="MSFT", quantity=5.0,
                             transaction_id=other.id, status=OrderStatus.FILLED,
                             filled_qty=5.0)

        assert _open_transaction_ids(acct_def.id, ["AAPL", "MSFT"]) == {
            "AAPL": sorted([first.id, second.id]),
            "MSFT": [other.id],
        }

    def test_the_option_never_reaches_the_state_the_close_loop_walks(self):
        """``_close_symbol`` closes every id in ``PositionState.transaction_ids``, so the
        seam only holds if the covered call is absent THERE, not merely in the query."""
        from ba2_trade_platform.core import portfolio_allocation_service as svc
        acct_def = create_account_definition()
        equity, option = _open_equity_and_option(acct_def.id)
        account = _FakeAccount(
            acct_def.id,
            positions=[_FakePosition("AAPL", 100.0, 15000.0, 16000.0)],
            prices={"AAPL": 160.0},
        )

        states = svc.build_position_states(account, ["AAPL"])

        assert states["AAPL"].transaction_ids == [equity.id]
        assert option.id not in states["AAPL"].transaction_ids

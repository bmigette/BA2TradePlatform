"""M2 fix (review 2026-07-18): TradeRiskManagement._update_orders_in_database and
_delete_unfunded_orders did raw session.get()/select() on Transaction/TradingOrder, which
silently find nothing when the backtest in-mem store is active (see trade_store.
IN_MEM_MODELS) -- the exact bug class the senate-basket-dispatch changeset already fixed
for TradeConditions/TradeRiskManagement._get_orders_with_recommendations, left unfixed here.
These tests prove the in-mem-store path actually FINDS the linked rows instead of silently
no-op'ing."""
from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeRiskManagement import TradeRiskManagement
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus


def _order(account_id=1, symbol="AAPL", txn_id=None, status=OrderStatus.NEW,
          depends_on_order=None):
    return TradingOrder(account_id=account_id, symbol=symbol, quantity=10.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=status,
                        transaction_id=txn_id, depends_on_order=depends_on_order)


def _txn(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=1):
    return Transaction(symbol=symbol, quantity=10.0, side=OrderDirection.BUY, status=status,
                       expert_id=expert_id)


def test_update_orders_in_database_finds_linked_transaction_in_mem(monkeypatch):
    """The transaction linked to an order must be FOUND (routed through get_instance), not
    silently dropped -- proven by TransactionHelper.adjust_qty actually being invoked with
    the real transaction object."""
    with ts.inmem_trades():
        txn = _txn()
        txn_id = add_instance(txn)
        order = _order(txn_id=txn_id)
        order_id = add_instance(order)
        got_order = ts.store_get(TradingOrder, order_id)
        got_order.quantity = 7.0

        calls = []

        def _fake_adjust_qty(transaction, new_quantity, session=None):
            calls.append((transaction.id, new_quantity))
            return True

        from ba2_common.core.TransactionHelper import TransactionHelper
        monkeypatch.setattr(TransactionHelper, "adjust_qty", staticmethod(_fake_adjust_qty))

        rm = TradeRiskManagement()
        updated, failed = rm._update_orders_in_database([got_order])
        assert calls == [(txn_id, 7.0)], "adjust_qty must be called with the REAL linked transaction"
        assert failed == 0
        assert updated == 1


def test_delete_unfunded_orders_deletes_linked_order_and_transaction_in_mem():
    """A linked (depends_on_order) TP/SL order AND the entry order's transaction must both
    be found and deleted -- not silently skipped because the raw select()/session.get()
    found nothing in the (unused) real SQLite table."""
    with ts.inmem_trades():
        txn = _txn()
        txn_id = add_instance(txn)
        entry_order = _order(txn_id=txn_id)
        entry_id = add_instance(entry_order)
        linked_order = _order(depends_on_order=entry_id)
        linked_id = add_instance(linked_order)

        got_entry = ts.store_get(TradingOrder, entry_id)
        got_entry.quantity = 0.0

        rm = TradeRiskManagement()
        rm._delete_unfunded_orders([got_entry])

        assert ts.store_get(TradingOrder, entry_id) is None, "the unfunded entry order must be deleted"
        assert ts.store_get(TradingOrder, linked_id) is None, "the linked TP/SL order must be deleted too"
        assert ts.store_get(Transaction, txn_id) is None, "the linked transaction must be deleted too"

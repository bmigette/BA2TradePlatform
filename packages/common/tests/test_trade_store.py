"""SQL-less in-memory trade store (backtest "dict trades"): the per-thread dict store + the named
accessors that replace the inline select(TradingOrder/Transaction) queries. Covers the flag gate +
every accessor filter + the transaction<->order join, on the in-memory (flag-ON) path."""
from ba2_common.core import trade_store as ts
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus


def _order(account_id=1, symbol="AAPL", txn_id=None, status=OrderStatus.NEW, broker_id=None):
    return TradingOrder(account_id=account_id, symbol=symbol, quantity=10.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=status,
                        transaction_id=txn_id, broker_order_id=broker_id)


def _txn(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=1):
    return Transaction(symbol=symbol, quantity=10.0, side=OrderDirection.BUY, status=status,
                       expert_id=expert_id)


def test_flag_off_by_default():
    assert ts.inmem_trades_active() is False


def test_add_assigns_ids_get_update_delete():
    with ts.inmem_trades():
        assert ts.inmem_trades_active() is True
        oid = ts.store_add(_order())
        assert oid == 1
        assert ts.store_add(_order()) == 2  # monotonic id counter
        got = ts.store_get(TradingOrder, 1)
        assert got is not None and got.id == 1
        got.status = OrderStatus.FILLED
        ts.store_update(got)
        assert ts.store_get(TradingOrder, 1).status == OrderStatus.FILLED
        assert ts.store_delete(got) is True
        assert ts.store_get(TradingOrder, 1) is None
    assert ts.inmem_trades_active() is False  # restored on exit


def test_reset_between_runs():
    with ts.inmem_trades():
        ts.store_add(_order())
    with ts.inmem_trades():
        # fresh store -> id counter restarts at 1 (no bleed across runs/trials)
        assert ts.store_add(_order()) == 1


def test_orders_where_filters():
    with ts.inmem_trades():
        ts.store_add(_order(account_id=1, txn_id=10, status=OrderStatus.NEW, broker_id="b1"))
        ts.store_add(_order(account_id=1, txn_id=10, status=OrderStatus.FILLED, broker_id="b2"))
        ts.store_add(_order(account_id=2, txn_id=11, status=OrderStatus.NEW, broker_id="b3"))
        assert len(ts.orders_where(account_id=1)) == 2
        assert len(ts.orders_where(transaction_id=10)) == 2
        assert len(ts.orders_where(account_id=1, statuses=[OrderStatus.FILLED])) == 1
        assert len(ts.orders_where(statuses=[OrderStatus.NEW])) == 2
        assert len(ts.orders_where(transaction_ids=[10, 11])) == 3
        assert ts.orders_where(broker_order_id="b3")[0].account_id == 2


def test_transactions_where_and_join():
    with ts.inmem_trades():
        t_open = ts.store_add(_txn(status=TransactionStatus.OPENED))
        t_closed = ts.store_add(_txn(status=TransactionStatus.CLOSED))
        ts.store_add(_order(txn_id=t_open, status=OrderStatus.FILLED))
        ts.store_add(_order(txn_id=t_closed, status=OrderStatus.NEW))
        assert len(ts.transactions_where(status=TransactionStatus.OPENED)) == 1
        assert len(ts.transactions_where(statuses=[TransactionStatus.OPENED,
                                                   TransactionStatus.CLOSED])) == 2
        # join: transactions that have a FILLED order -> only t_open
        joined = ts.transactions_with_orders(lambda o: o.status == OrderStatus.FILLED)
        assert [t.id for t in joined] == [t_open]

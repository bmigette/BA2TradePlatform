"""The in-memory trade store's EQUALITY INDEXES must never omit a matching row.

The store's named accessors used to scan every row and read SQLModel/SQLAlchemy instrumented
attributes on each one. They now start from a per-(model, field) index. An index that is not
maintained on some mutation does not return a STALE answer, it returns a WRONG one -- an order the
caller cannot see silently changes a trading decision -- so the property these tests pin is:

    for every mutation kind, the indexed accessor answers EXACTLY what a brute-force scan answers,
    same rows, same order.

They assert it the way the audit mode does (``_scan`` below is the pre-index filter, written out
in full so the comparison is against an independent implementation, not against the code under
test), and they exercise every way a row's indexed fields can change: insert, in-place field
mutation + persist, re-add under an existing id, and delete.
"""
import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus

_UNSET = ts._UNSET


def _order(account_id=1, symbol="AAPL", txn_id=None, status=OrderStatus.NEW, broker_id=None,
           depends_on=None, parent=None):
    return TradingOrder(account_id=account_id, symbol=symbol, quantity=10.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET, status=status,
                        transaction_id=txn_id, broker_order_id=broker_id,
                        depends_on_order=depends_on, parent_order_id=parent)


def _txn(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=1):
    return Transaction(symbol=symbol, quantity=10.0, side=OrderDirection.BUY, status=status,
                       expert_id=expert_id)


# -- the reference implementation: the exact pre-index full scan --------------------------------
def _scan_orders(**kw):
    sset = set(kw["statuses"]) if kw.get("statuses") is not None else None
    nset = set(kw["not_statuses"]) if kw.get("not_statuses") is not None else None
    tids = set(kw["transaction_ids"]) if kw.get("transaction_ids") is not None else None
    dep = kw.get("depends_on_order", _UNSET)
    out = []
    for o in ts.store_all(TradingOrder):
        if kw.get("account_id") is not None and o.account_id != kw["account_id"]:
            continue
        if kw.get("transaction_id") is not None and o.transaction_id != kw["transaction_id"]:
            continue
        if tids is not None and o.transaction_id not in tids:
            continue
        if sset is not None and o.status not in sset:
            continue
        if nset is not None and o.status in nset:
            continue
        if kw.get("broker_order_id") is not None and o.broker_order_id != kw["broker_order_id"]:
            continue
        if dep is not _UNSET and o.depends_on_order != dep:
            continue
        if kw.get("parent_order_id") is not None and o.parent_order_id != kw["parent_order_id"]:
            continue
        out.append(o)
    return out


def _scan_txns(**kw):
    sset = set(kw["statuses"]) if kw.get("statuses") is not None else None
    nset = set(kw["not_statuses"]) if kw.get("not_statuses") is not None else None
    xids = set(kw["exclude_ids"]) if kw.get("exclude_ids") else None
    out = []
    for t in ts.store_all(Transaction):
        if kw.get("status") is not None and t.status != kw["status"]:
            continue
        if sset is not None and t.status not in sset:
            continue
        if nset is not None and t.status in nset:
            continue
        if kw.get("expert_id") is not None and t.expert_id != kw["expert_id"]:
            continue
        if kw.get("symbol") is not None and t.symbol != kw["symbol"]:
            continue
        if xids is not None and t.id in xids:
            continue
        out.append(t)
    return out


#: Every filter shape the named accessors are actually called with (see the call sites in
#: AccountInterface / ReadOnlyAccountInterface / OptionsAccountInterface / TradeActions /
#: TradeConditions / TradeRiskManagement / models.py / backtest_account).
ORDER_QUERIES = (
    {"account_id": 1},
    {"account_id": 2},
    {"account_id": 1, "statuses": [OrderStatus.NEW, OrderStatus.ACCEPTED]},
    {"account_id": 1, "transaction_id": 1},
    {"transaction_id": 1},
    {"transaction_id": 2},
    {"transaction_ids": [1, 2]},
    {"statuses": [OrderStatus.PENDING]},
    {"statuses": [OrderStatus.FILLED]},
    {"not_statuses": [OrderStatus.FILLED]},
    {"broker_order_id": "B7"},
    {"depends_on_order": None},
    {"depends_on_order": 1},
    {"parent_order_id": 1},
    {"account_id": 1, "transaction_id": 1, "depends_on_order": None},
)

TXN_QUERIES = (
    {"status": TransactionStatus.OPENED},
    {"status": TransactionStatus.CLOSED},
    {"statuses": [TransactionStatus.OPENED, TransactionStatus.WAITING]},
    {"not_statuses": [TransactionStatus.CLOSED]},
    {"expert_id": 1},
    {"expert_id": 2},
    {"symbol": "AAPL"},
    {"symbol": "MSFT"},
    {"expert_id": 1, "status": TransactionStatus.OPENED},
    {"status": TransactionStatus.OPENED, "exclude_ids": [1]},
)


def _assert_agrees(label):
    """Every query shape: the indexed accessor == the brute-force scan, rows AND order."""
    for q in ORDER_QUERIES:
        got, exp = ts.orders_where(**q), _scan_orders(**q)
        assert [o.id for o in got] == [o.id for o in exp], (label, q)
        assert all(a is b for a, b in zip(got, exp)), (label, q)
    for q in TXN_QUERIES:
        got, exp = ts.transactions_where(**q), _scan_txns(**q)
        assert [t.id for t in got] == [t.id for t in exp], (label, q)


def _seed():
    """Two accounts, two transactions, parents + dependent legs, mixed statuses."""
    ts.store_add(_order(account_id=1, txn_id=1, status=OrderStatus.NEW))                 # 1
    ts.store_add(_order(account_id=1, txn_id=1, status=OrderStatus.PENDING, depends_on=1))  # 2
    ts.store_add(_order(account_id=2, txn_id=2, status=OrderStatus.ACCEPTED))            # 3
    ts.store_add(_order(account_id=1, txn_id=2, status=OrderStatus.FILLED, parent=1))    # 4
    ts.store_add(_order(account_id=2, txn_id=None, status=OrderStatus.NEW))              # 5
    ts.store_add(_txn(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=1))      # 1
    ts.store_add(_txn(symbol="MSFT", status=TransactionStatus.WAITING, expert_id=1))     # 2
    ts.store_add(_txn(symbol="AAPL", status=TransactionStatus.CLOSED, expert_id=2))      # 3


# -- one test per mutation kind -----------------------------------------------------------------
def test_index_agrees_after_insert():
    with ts.inmem_trades():
        _seed()
        _assert_agrees("insert")


def test_index_agrees_after_status_change():
    """The mutation the backtest does thousands of times: an order fills/cancels in place and is
    persisted through update_instance -> store_update."""
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 2)
        o.status = OrderStatus.FILLED
        ts.store_update(o)
        _assert_agrees("status")
        o.status = OrderStatus.CANCELED
        ts.store_update(o)
        _assert_agrees("status-again")


def test_index_agrees_after_status_change_written_as_a_raw_string():
    """A caller may assign the enum's VALUE (TradeActions does: ``status = OrderStatus.OPEN.value``);
    store_update coerces it, and the index must file it under the coerced enum."""
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 1)
        o.status = OrderStatus.OPEN.value
        ts.store_update(o)
        assert ts.store_get(TradingOrder, 1).status is OrderStatus.OPEN
        _assert_agrees("status-str")
        assert [x.id for x in ts.orders_where(statuses=[OrderStatus.OPEN])] == [1]


def test_index_agrees_after_broker_id_assignment():
    """_submit_order_impl stamps broker_order_id on an order ALREADY in the store."""
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 3)
        o.broker_order_id = "B7"
        ts.store_update(o)
        _assert_agrees("broker-id")
        assert [x.id for x in ts.orders_where(broker_order_id="B7")] == [3]


def test_index_agrees_after_transaction_link_change():
    """A link field (transaction_id / depends_on_order / parent_order_id) re-pointed in place."""
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 5)
        o.transaction_id = 1
        o.depends_on_order = 1
        o.parent_order_id = 1
        ts.store_update(o)
        _assert_agrees("relink")
        assert [x.id for x in ts.orders_where(transaction_id=1)] == [1, 2, 5]


def test_index_agrees_after_account_change():
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 3)
        o.account_id = 1
        ts.store_update(o)
        _assert_agrees("account")
        assert [x.id for x in ts.orders_where(account_id=1)] == [1, 2, 3, 4]


def test_index_agrees_after_transaction_status_and_symbol_change():
    with ts.inmem_trades():
        _seed()
        t = ts.store_get(Transaction, 2)
        t.status = TransactionStatus.OPENED
        t.symbol = "NVDA"
        t.expert_id = 2
        ts.store_update(t)
        _assert_agrees("txn-fields")


def test_index_agrees_after_delete():
    with ts.inmem_trades():
        _seed()
        ts.store_delete(ts.store_get(TradingOrder, 2))
        ts.store_delete(ts.store_get(Transaction, 1))
        _assert_agrees("delete")
        assert [x.id for x in ts.orders_where(transaction_id=1)] == [1]


def test_index_agrees_after_readd_under_an_existing_id():
    """A REPLACEMENT row under an id already in the store must not leave the old row's values in
    the buckets (that is the one way an index can over- AND under-report at once)."""
    with ts.inmem_trades():
        _seed()
        replacement = _order(account_id=2, txn_id=2, status=OrderStatus.CANCELED)
        replacement.id = 1
        ts.store_add(replacement)
        _assert_agrees("re-add")
        assert [x.id for x in ts.orders_where(account_id=1)] == [2, 4]
        assert ts.orders_where(statuses=[OrderStatus.NEW]) == [ts.store_get(TradingOrder, 5)]


def test_index_agrees_after_a_mutation_that_is_never_persisted():
    """A field changed in place WITHOUT store_update: the index cannot know, so the accessor must
    still not INVENT a row -- the predicate chain runs over the candidates and rejects it.

    This is the failure mode the index is designed to survive: a stale bucket over-includes (which
    the predicate fixes) rather than under-including. The row simply stops being findable under
    its NEW value until something persists it -- exactly what the pre-index scan would also do for
    a value it never sees, and what BT_TRADE_STORE_AUDIT=1 exists to catch in a real run."""
    with ts.inmem_trades():
        _seed()
        o = ts.store_get(TradingOrder, 1)
        o.status = OrderStatus.FILLED          # no store_update
        # Over-inclusion is filtered out by the predicate chain: id 1 is NOT returned as NEW.
        assert 1 not in [x.id for x in ts.orders_where(statuses=[OrderStatus.NEW])]
        assert 1 not in [x.id for x in ts.orders_where(statuses=[OrderStatus.FILLED])]
        # ...and the moment it IS persisted, both answers are right again.
        ts.store_update(o)
        _assert_agrees("unpersisted-then-persisted")
        assert 1 in [x.id for x in ts.orders_where(statuses=[OrderStatus.FILLED])]


def test_result_order_matches_insertion_order_after_status_churn():
    """Callers index into the result (``matches[0]``, ``orders[0]``), so the indexed answer must
    come back in the same order the scan produced: table-insertion order, even after rows have
    been moved between status buckets."""
    with ts.inmem_trades():
        _seed()
        for oid in (4, 1, 3):
            o = ts.store_get(TradingOrder, oid)
            o.status = OrderStatus.FILLED
            ts.store_update(o)
        assert [o.id for o in ts.orders_where(statuses=[OrderStatus.FILLED])] == [1, 3, 4]
        assert [o.id for o in ts.orders_where(account_id=1)] == [1, 2, 4]


def test_store_is_reset_between_runs():
    """A fresh ``inmem_trades()`` block must not see the previous run's index entries."""
    with ts.inmem_trades():
        _seed()
        assert len(ts.orders_where(account_id=1)) == 3
    with ts.inmem_trades():
        assert ts.orders_where(account_id=1) == []
        assert ts.transactions_where(status=TransactionStatus.OPENED) == []


def test_audit_mode_raises_when_the_index_is_corrupted():
    """The BT_TRADE_STORE_AUDIT guard must actually fail on a disagreement (a guard nobody has
    seen fire is a guard nobody knows works). The index is corrupted by hand here -- no production
    path can do this -- purely to prove the check is wired."""
    with ts.inmem_trades():
        _seed()
        store = ts._store()
        store._idx[TradingOrder]["account_id"][1].pop(2)     # hide order 2 from the index
        assert [o.id for o in ts.orders_where(account_id=1)] == [1, 4]   # silently wrong...
        ts._AUDIT = True
        try:
            with pytest.raises(ts.TradeStoreIndexError):
                ts.orders_where(account_id=1)                # ...and the audit refuses it
        finally:
            ts._AUDIT = False

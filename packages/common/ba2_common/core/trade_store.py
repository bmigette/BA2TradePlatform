"""SQL-less in-memory order/transaction store for the backtest ("dict trades").

The backtest runs on :memory: SQLite, so its cost is the SQLAlchemy ORM layer (compile/hydrate/
flush per op), not disk. Orders + transactions are the high-churn entities; keeping them in plain
Python dicts during a run — and only persisting at the end — removes that overhead.

Design (per the "sql-less, index trades, understand usecase" direction): NOT a SQL/Select
re-implementation. A tiny per-thread dict store + a handful of NAMED accessors matching the actual
query surface (orders by account/transaction/status/broker_id; transactions by status; the two
transaction<->order joins). Each accessor is DUAL-PATH:

  * flag ON  (backtest, inside ``inmem_trades()``) -> filter the in-memory dicts (no ORM), and
  * flag OFF (live / default)                      -> the exact SQLite query as before.

So call sites can adopt the accessors with behaviour UNCHANGED when the flag is off (live is never
affected), and the backtest gets the sql-less fast path when the flag is on. The flag + store are
THREAD-LOCAL, so parallel GA trials each get their own store; ``inmem_trades()`` (entered by
``backtest_trading_db``) resets it per run. The feature flag is propagated to remote GA workers so
distributed trials use the store too.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterable, List, Optional

from ba2_common.core.models import TradingOrder, Transaction

_tls = threading.local()

# Sentinel so accessors can distinguish "don't filter on this" from an explicit ``None`` filter
# (e.g. depends_on_order is None == "entry orders only", a real filter value).
_UNSET = object()


def inmem_trades_active() -> bool:
    """True iff the calling thread is inside an ``inmem_trades()`` block (backtest)."""
    return bool(getattr(_tls, "active", False))


_field_coerce_cache: dict = {}


def _coerce_spec(model) -> tuple:
    """(enum_fields, naive_dt_fields) for a model, cached. SQLModel ``table=True`` models DON'T
    validate on construction/assignment, and the codebase relies on the SQLite column types to
    normalise values on READ:
      * enum columns coerce a raw str -> the enum (e.g. TradeActions builds ``TradingOrder(side=
        "BUY")``), so ``o.side.value`` works; and
      * naive ``DateTime`` columns (``timezone=False``) STRIP tzinfo on round-trip (an aware
        ``datetime.now(utc)`` comes back naive), so downstream ``min()``/comparisons never mix
        aware & naive datetimes.
    The store must replicate BOTH so its objects are byte-faithful stand-ins for DB-hydrated rows."""
    cached = _field_coerce_cache.get(model)
    if cached is None:
        import sqlalchemy as _sa
        enums, naive_dt = {}, []
        for col in model.__table__.columns:
            if isinstance(col.type, _sa.Enum) and getattr(col.type, "enum_class", None) is not None:
                enums[col.name] = col.type.enum_class
            elif isinstance(col.type, _sa.DateTime) and not getattr(col.type, "timezone", False):
                naive_dt.append(col.name)
        cached = (enums, naive_dt)
        _field_coerce_cache[model] = cached
    return cached


def _coerce_enums(obj) -> None:
    """Normalise this object's enum + naive-datetime fields IN PLACE to match SQLite's read-time
    coercion (enum-by-value; strip tzinfo from aware datetimes). Idempotent; skips None and
    already-correct values. An invalid enum value raises exactly as a DB read would."""
    enums, naive_dt = _coerce_spec(type(obj))
    for name, enum_cls in enums.items():
        v = getattr(obj, name, None)
        if v is not None and not isinstance(v, enum_cls):
            setattr(obj, name, enum_cls(v))
    for name in naive_dt:
        v = getattr(obj, name, None)
        if v is not None and getattr(v, "tzinfo", None) is not None:
            setattr(obj, name, v.replace(tzinfo=None))


class _TradeStore:
    """Per-thread in-memory rows for the mapped models, keyed by id, with a monotonic id counter
    (mirrors SQLite autoincrement; exact id VALUES don't matter for economics — links are by the
    stored ids, round-trips pair by side/txn — only internal consistency matters)."""

    def __init__(self) -> None:
        self._rows: dict = {}       # {model: {id: obj}}
        self._counters: dict = {}   # {model: next_id}

    def _tbl(self, model) -> dict:
        return self._rows.setdefault(model, {})

    def add(self, obj) -> int:
        model = type(obj)
        if getattr(obj, "id", None) is None:
            self._counters[model] = self._counters.get(model, 0) + 1
            obj.id = self._counters[model]
        else:
            self._counters[model] = max(self._counters.get(model, 0), obj.id)
        _coerce_enums(obj)  # str enum fields -> enum, like a SQLite round-trip
        self._tbl(model)[obj.id] = obj
        return obj.id

    def get(self, model, obj_id):
        return self._tbl(model).get(obj_id)

    def update(self, obj) -> bool:
        # objects are stored by identity; a caller that mutated the stored object is already
        # reflected. Re-coerce enum fields (a mutation may have set a raw str) + re-index in case
        # its id was only just assigned.
        _coerce_enums(obj)
        self._tbl(type(obj))[obj.id] = obj
        return True

    def delete(self, obj) -> bool:
        return self._tbl(type(obj)).pop(getattr(obj, "id", None), None) is not None

    def all(self, model) -> List[Any]:
        return list(self._tbl(model).values())


def _store() -> _TradeStore:
    st = getattr(_tls, "store", None)
    if st is None:
        st = _TradeStore()
        _tls.store = st
    return st


@contextmanager
def inmem_trades():
    """Activate the sql-less order/transaction store for this thread + run (backtest only). Resets
    the store on entry so each run/trial starts clean; restores the prior state on exit."""
    prev_active = getattr(_tls, "active", False)
    prev_store = getattr(_tls, "store", None)
    _tls.active = True
    _tls.store = _TradeStore()
    try:
        yield _tls.store
    finally:
        _tls.active = prev_active
        _tls.store = prev_store


# Models whose access is routed to the in-memory store when the flag is active.
IN_MEM_MODELS = (TradingOrder, Transaction)


def is_inmem_model(model) -> bool:
    return model in IN_MEM_MODELS


# --- store CRUD (used by the db helpers when the flag is active) --------------------------------
def store_add(obj) -> int:
    return _store().add(obj)


def store_get(model, obj_id):
    return _store().get(model, obj_id)


def store_update(obj) -> bool:
    return _store().update(obj)


def store_delete(obj) -> bool:
    return _store().delete(obj)


def store_all(model) -> List[Any]:
    return _store().all(model)


def get_or_none(model, obj_id, session=None):
    """Fetch one row by id, or ``None`` if absent — the dual-path form of an inline
    ``session.get(Model, id)``. Flag-ON: the store (BT). Flag-OFF: ``session.get`` on the given
    session, else a fresh SQLite session (live semantics preserved). Unlike ``db.get_instance`` this
    NEVER raises on a miss, matching the inline sites that handle ``None``."""
    if obj_id is None:
        return None
    if inmem_trades_active() and is_inmem_model(model):
        return store_get(model, obj_id)
    if session is not None:
        return session.get(model, obj_id)
    from ba2_common.core.db import get_db
    from sqlmodel import Session
    with Session(get_db().bind) as s:
        return s.get(model, obj_id)


# --- named accessors (dual-path: in-mem filter when active, else the exact SQLite query) --------
def orders_where(*, account_id: Optional[int] = None, transaction_id: Optional[int] = None,
                 statuses: Optional[Iterable] = None, broker_order_id: Optional[str] = None,
                 transaction_ids: Optional[Iterable] = None,
                 not_statuses: Optional[Iterable] = None, depends_on_order: Any = _UNSET,
                 session=None) -> List[TradingOrder]:
    """TradingOrders matching the given equality/in filters (all AND-ed). Mirrors the
    ``select(TradingOrder).where(...)`` sites.

    ``session`` (flag-OFF only): reuse an existing SQLite session so a refactored inline site keeps
    its original transaction semantics (live is never on the flag path). ``depends_on_order`` may be
    ``None`` (entry orders) / a value (that parent) — omit it to not filter on it."""
    if inmem_trades_active():
        sset = set(statuses) if statuses is not None else None
        nset = set(not_statuses) if not_statuses is not None else None
        tids = set(transaction_ids) if transaction_ids is not None else None
        out = []
        for o in _store().all(TradingOrder):
            if account_id is not None and o.account_id != account_id:
                continue
            if transaction_id is not None and o.transaction_id != transaction_id:
                continue
            if tids is not None and o.transaction_id not in tids:
                continue
            if sset is not None and o.status not in sset:
                continue
            if nset is not None and o.status in nset:
                continue
            if broker_order_id is not None and o.broker_order_id != broker_order_id:
                continue
            if depends_on_order is not _UNSET and o.depends_on_order != depends_on_order:
                continue
            out.append(o)
        return out
    from sqlmodel import select, Session
    stmt = select(TradingOrder)
    if account_id is not None:
        stmt = stmt.where(TradingOrder.account_id == account_id)
    if transaction_id is not None:
        stmt = stmt.where(TradingOrder.transaction_id == transaction_id)
    if transaction_ids is not None:
        stmt = stmt.where(TradingOrder.transaction_id.in_(list(transaction_ids)))
    if statuses is not None:
        stmt = stmt.where(TradingOrder.status.in_(list(statuses)))
    if not_statuses is not None:
        stmt = stmt.where(TradingOrder.status.notin_(list(not_statuses)))
    if broker_order_id is not None:
        stmt = stmt.where(TradingOrder.broker_order_id == broker_order_id)
    if depends_on_order is not _UNSET:
        stmt = stmt.where(TradingOrder.depends_on_order == depends_on_order)
    if session is not None:
        return list(session.exec(stmt).all())
    from ba2_common.core.db import get_db
    with Session(get_db().bind) as s:
        return list(s.exec(stmt).all())


def transactions_where(*, status=None, statuses: Optional[Iterable] = None,
                       expert_id: Optional[int] = None, not_statuses: Optional[Iterable] = None,
                       symbol: Optional[str] = None, exclude_ids: Optional[Iterable] = None,
                       session=None) -> List[Transaction]:
    """Transactions matching status / expert / symbol filters. Mirrors ``select(Transaction)
    .where(...)``. ``exclude_ids`` mirrors ``Transaction.id.not_in(...)``. ``session`` (flag-OFF
    only) reuses an existing SQLite session (live semantics preserved)."""
    if inmem_trades_active():
        sset = set(statuses) if statuses is not None else None
        nset = set(not_statuses) if not_statuses is not None else None
        xids = set(exclude_ids) if exclude_ids else None
        out = []
        for t in _store().all(Transaction):
            if status is not None and t.status != status:
                continue
            if sset is not None and t.status not in sset:
                continue
            if nset is not None and t.status in nset:
                continue
            if expert_id is not None and t.expert_id != expert_id:
                continue
            if symbol is not None and t.symbol != symbol:
                continue
            if xids is not None and t.id in xids:
                continue
            out.append(t)
        return out
    from sqlmodel import select, Session
    stmt = select(Transaction)
    if status is not None:
        stmt = stmt.where(Transaction.status == status)
    if statuses is not None:
        stmt = stmt.where(Transaction.status.in_(list(statuses)))
    if not_statuses is not None:
        stmt = stmt.where(Transaction.status.notin_(list(not_statuses)))
    if expert_id is not None:
        stmt = stmt.where(Transaction.expert_id == expert_id)
    if symbol is not None:
        stmt = stmt.where(Transaction.symbol == symbol)
    if exclude_ids:
        stmt = stmt.where(Transaction.id.not_in(list(exclude_ids)))
    if session is not None:
        return list(session.exec(stmt).all())
    from ba2_common.core.db import get_db
    with Session(get_db().bind) as s:
        return list(s.exec(stmt).all())


def transactions_with_orders(order_predicate: Callable[[TradingOrder], bool],
                             txn_predicate: Optional[Callable[[Transaction], bool]] = None,
                             ) -> List[Transaction]:
    """Transactions that have >=1 TradingOrder satisfying ``order_predicate`` (and, if given, that
    themselves satisfy ``txn_predicate``) — the in-mem form of
    ``select(Transaction).join(TradingOrder).where(...).distinct()``. ONLY valid under the active
    flag; call sites keep their own SQLite join for the flag-off path."""
    txn_ids = {o.transaction_id for o in _store().all(TradingOrder)
               if o.transaction_id is not None and order_predicate(o)}
    return [t for t in _store().all(Transaction)
            if t.id in txn_ids and (txn_predicate is None or txn_predicate(t))]

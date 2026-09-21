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

import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterable, List, Optional

from ba2_common.core.models import (
    AccountDefinition, ExpertInstance, ExpertRecommendation, TradingOrder, Transaction,
)

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


# --- equality indexes over the in-memory rows --------------------------------------------------
#
# WHY. The sql-less store removed the DATABASE from the hot path but kept the SCAN: every named
# accessor below walked ``_store().all(Model)`` and read SQLModel/SQLAlchemy INSTRUMENTED
# attributes on every row to test the filters. Profiled on a heavy option genome (95 symbols, one
# year) that is 117,807 ``orders_where`` calls x N orders x ~7 descriptor reads -> 303M
# ``sqlalchemy.orm.attributes.__get__`` calls, ~98s of a 409s run. The bypass had made the lookup
# O(n) per call with an ORM descriptor tax per field.
#
# WHAT. A dict-of-dicts equality index per (model, field), maintained at the store's OWN mutation
# points (``add`` / ``update`` / ``delete``), so an accessor can start from the ids that CAN match
# instead of from every row.
#
# THE SAFETY PROPERTY THAT MAKES THIS SOUND. The index only ever NARROWS the candidate list; the
# accessor then runs its ORIGINAL predicate chain, unchanged, against each candidate. So an
# over-inclusive index (a stale bucket that still holds a row that no longer matches) cannot
# produce a wrong answer -- the predicate rejects it. The only way to get a WRONG answer is an
# index that OMITS a matching row, which is why:
#   * every indexed field is re-read from the object on ``update`` and the row is moved between
#     buckets when it changed (``_reindex``), and
#   * ``BT_TRADE_STORE_AUDIT=1`` makes every accessor ALSO run the full brute-force scan and raise
#     on any disagreement (used by the test suite and by a full real backtest).
#
# WHICH FIELDS. Exactly the ones the accessors below filter on by equality/membership, measured
# against the real call surface -- no speculative indexes (each one costs on every insert).
_INDEXED_FIELDS = {
    TradingOrder: ("account_id", "transaction_id", "status", "broker_order_id",
                   "depends_on_order", "parent_order_id"),
    Transaction: ("status", "expert_id", "symbol"),
}

#: Set BT_TRADE_STORE_AUDIT=1 to cross-check every indexed lookup against a brute-force scan.
#: Read once at import (a per-call env read would itself show up in the profile this exists for).
_AUDIT = os.environ.get("BT_TRADE_STORE_AUDIT", "").strip().lower() in ("1", "true", "yes")


class _TradeStore:
    """Per-thread in-memory rows for the mapped models, keyed by id, with a monotonic id counter
    (mirrors SQLite autoincrement; exact id VALUES don't matter for economics — links are by the
    stored ids, round-trips pair by side/txn — only internal consistency matters).

    Rows are additionally EQUALITY-INDEXED on the fields the named accessors filter by (see
    ``_INDEXED_FIELDS`` and the block above it)."""

    def __init__(self) -> None:
        self._rows: dict = {}       # {model: {id: obj}}
        self._counters: dict = {}   # {model: next_id}
        # {model: {field: {value: {id: None}}}} — the inner dict is an insertion-ORDERED set.
        self._idx: dict = {}
        # {model: {id: (value per indexed field)}} — what the index currently believes, so an
        # ``update`` can move a row out of the bucket it was filed under.
        self._shadow: dict = {}
        # {model: {id: ordinal}} — table insertion order, so a result assembled out of buckets can
        # be restored to the order a full scan would have produced (see ``_order_clean``).
        self._seq: dict = {}
        self._next_seq: dict = {}
        # {(model, field): bool} — True while no row has ever MOVED between that field's buckets,
        # i.e. the buckets are still in table-insertion order and a result read straight out of one
        # needs no sort. Flipped False the first time a value changes (status does this; the link
        # fields never do), after which results from that field are re-sorted by ``_seq``.
        self._order_clean: dict = {}

    def _tbl(self, model) -> dict:
        return self._rows.setdefault(model, {})

    # -- index maintenance ----------------------------------------------------------------
    def _index_insert(self, obj) -> None:
        model = type(obj)
        fields = _INDEXED_FIELDS.get(model)
        if not fields:
            return
        idx = self._idx.setdefault(model, {})
        vals = []
        for f in fields:
            v = getattr(obj, f, None)
            vals.append(v)
            idx.setdefault(f, {}).setdefault(v, {})[obj.id] = None
        self._shadow.setdefault(model, {})[obj.id] = tuple(vals)
        seq = self._seq.setdefault(model, {})
        if obj.id not in seq:
            n = self._next_seq.get(model, 0)
            self._next_seq[model] = n + 1
            seq[obj.id] = n

    def _index_drop(self, model, obj_id, *, keep_seq: bool = False) -> None:
        """Un-file a row. ``keep_seq`` is for a REPLACEMENT under the same id: the table dict
        keeps the original key's position, so the row must keep its table ordinal too."""
        fields = _INDEXED_FIELDS.get(model)
        if not fields:
            return
        old = self._shadow.get(model, {}).pop(obj_id, None)
        if old is None:
            return
        idx = self._idx.get(model, {})
        for f, v in zip(fields, old):
            bucket = idx.get(f, {}).get(v)
            if bucket is not None:
                bucket.pop(obj_id, None)
        if not keep_seq:
            self._seq.get(model, {}).pop(obj_id, None)

    def _reindex(self, obj) -> None:
        """Re-file ``obj`` under its CURRENT indexed values. Called from ``update`` (which the
        db helpers route every persist through), so a field a caller mutated in place lands in
        the right bucket."""
        model = type(obj)
        fields = _INDEXED_FIELDS.get(model)
        if not fields:
            return
        shadow = self._shadow.setdefault(model, {})
        old = shadow.get(obj.id)
        if old is None:
            self._index_insert(obj)
            return
        idx = self._idx.setdefault(model, {})
        new = []
        for pos, f in enumerate(fields):
            v = getattr(obj, f, None)
            new.append(v)
            prev = old[pos]
            if v is prev or v == prev:
                continue
            bucket = idx.setdefault(f, {})
            was = bucket.get(prev)
            if was is not None:
                was.pop(obj.id, None)
            bucket.setdefault(v, {})[obj.id] = None
            # The row was APPENDED to its new bucket, so that field's buckets are no longer in
            # table order; results taken from them must be re-sorted from here on.
            self._order_clean[(model, f)] = False
        shadow[obj.id] = tuple(new)

    def add(self, obj) -> int:
        model = type(obj)
        if getattr(obj, "id", None) is None:
            self._counters[model] = self._counters.get(model, 0) + 1
            obj.id = self._counters[model]
        else:
            self._counters[model] = max(self._counters.get(model, 0), obj.id)
        _coerce_enums(obj)  # str enum fields -> enum, like a SQLite round-trip
        if obj.id in self._tbl(model):
            # Re-adding an existing id REPLACES the row; drop the old one's index entries first
            # so it cannot linger in a bucket under a value the new object does not have. The
            # table dict keeps the key's ORIGINAL position while the re-filed row lands at the
            # end of its buckets, so every bucket for this model is now out of table order.
            self._index_drop(model, obj.id, keep_seq=True)
            for f in _INDEXED_FIELDS.get(model, ()):
                self._order_clean[(model, f)] = False
        self._tbl(model)[obj.id] = obj
        self._index_insert(obj)
        return obj.id

    def get(self, model, obj_id):
        return self._tbl(model).get(obj_id)

    def update(self, obj) -> bool:
        # objects are stored by identity; a caller that mutated the stored object is already
        # reflected. Re-coerce enum fields (a mutation may have set a raw str) + re-index in case
        # its id was only just assigned.
        _coerce_enums(obj)
        self._tbl(type(obj))[obj.id] = obj
        self._reindex(obj)
        return True

    def delete(self, obj) -> bool:
        obj_id = getattr(obj, "id", None)
        dropped = self._tbl(type(obj)).pop(obj_id, None) is not None
        if dropped:
            self._index_drop(type(obj), obj_id)
        return dropped

    def all(self, model) -> List[Any]:
        return list(self._tbl(model).values())

    # -- indexed narrowing ----------------------------------------------------------------
    def narrow(self, model, eq, membership) -> Optional[List[Any]]:
        """The rows that COULD satisfy these filters, in table order — or ``None`` when no index
        applies (the caller then scans everything, exactly as before).

        ``eq``: iterable of ``(field, value)`` equality filters. ``membership``: iterable of
        ``(field, value_set)`` "field IN set" filters. Only fields in ``_INDEXED_FIELDS`` are
        consulted; anything else is left to the caller's own predicate chain, which runs over the
        returned candidates regardless. The result is therefore allowed to be a SUPERSET of the
        true match set — it must never be a subset.
        """
        idx = self._idx.get(model)
        if not idx:
            return None
        best = None           # the smallest candidate id collection found
        needs_sort = False    # is that collection still in table order?
        for field, value in eq:
            bucket = idx.get(field)
            if bucket is None:
                continue
            ids = bucket.get(value)
            if ids is None:
                return []     # nothing was ever filed under this value -> no row can match
            if best is None or len(ids) < len(best):
                best = ids
                needs_sort = not self._order_clean.get((model, field), True)
        for field, values in membership:
            bucket = idx.get(field)
            if bucket is None or values is None:
                continue
            got: dict = {}
            for v in values:
                b = bucket.get(v)
                if b:
                    got.update(b)
            if best is None or len(got) < len(best):
                best = got
                # A union across >1 bucket has no order of its own; one bucket keeps its own.
                needs_sort = (len(values) > 1
                              or not self._order_clean.get((model, field), True))
        if best is None:
            return None
        rows = self._tbl(model)
        if needs_sort:
            seq = self._seq.get(model, {})
            ids: Any = sorted(best, key=lambda i: seq.get(i, i))
        else:
            ids = best
        return [rows[i] for i in ids if i in rows]


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
#
# ExpertInstance/AccountDefinition/ExpertRecommendation joined TradingOrder/Transaction here
# after profiling a full-length backtest showed ~40% of wall time in SQLAlchemy ORM overhead
# (session.get/_get_impl/load_on_pk_identity for ExpertInstance reads in the per-bar balance
# check, plus add_instance/flush/_emit_insert for ExpertRecommendation writes) even though the
# backing DB is already :memory: SQLite (no disk I/O) — the ORM compile/hydrate/flush cost alone
# dominates at 80K+ calls. All three are seeded fresh per-run (seed_account_definition/
# seed_expert_instance/daily_engine's recommendation persist) and only ever accessed by-PK
# within the backtest engine (no filtered SELECT), so they fit the exact same add-then-get
# pattern as orders/transactions. get_account_instance itself is already a pure in-memory dict
# lookup (BacktestInstanceResolver, testplatform/backend/app/services/backtest/seam_wiring.py)
# and never touches this store.
IN_MEM_MODELS = (TradingOrder, Transaction, ExpertInstance, AccountDefinition, ExpertRecommendation)


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


class TradeStoreIndexError(RuntimeError):
    """An indexed lookup disagreed with a brute-force scan (BT_TRADE_STORE_AUDIT=1 only).

    Raised, never logged-and-continued: a narrowed result that omits a row is a WRONG ANSWER --
    an order the caller does not see silently changes a trading decision -- so the audit refuses
    the run rather than letting it finish on numbers nobody can trust."""


def _audit(name: str, got: List[Any], expected: List[Any]) -> None:
    """Assert the indexed answer IS the full-scan answer (same rows, same order)."""
    if len(got) == len(expected) and all(a is b for a, b in zip(got, expected)):
        return
    gi = [getattr(o, "id", None) for o in got]
    ei = [getattr(o, "id", None) for o in expected]
    raise TradeStoreIndexError(
        f"{name}: indexed lookup returned ids {gi} but a full scan returns {ei} — the "
        f"equality index is missing or mis-ordering rows.")


# --- named accessors (dual-path: in-mem filter when active, else the exact SQLite query) --------
def orders_where(*, account_id: Optional[int] = None, transaction_id: Optional[int] = None,
                 statuses: Optional[Iterable] = None, broker_order_id: Optional[str] = None,
                 transaction_ids: Optional[Iterable] = None,
                 not_statuses: Optional[Iterable] = None, depends_on_order: Any = _UNSET,
                 parent_order_id: Optional[int] = None,
                 session=None) -> List[TradingOrder]:
    """TradingOrders matching the given equality/in filters (all AND-ed). Mirrors the
    ``select(TradingOrder).where(...)`` sites.

    ``session`` (flag-OFF only): reuse an existing SQLite session so a refactored inline site keeps
    its original transaction semantics (live is never on the flag path). ``depends_on_order`` may be
    ``None`` (entry orders) / a value (that parent) — omit it to not filter on it.
    ``parent_order_id`` selects the CHILD legs of a multi-leg (OCO/option-spread) parent."""
    if inmem_trades_active():
        sset = set(statuses) if statuses is not None else None
        nset = set(not_statuses) if not_statuses is not None else None
        tids = set(transaction_ids) if transaction_ids is not None else None
        st = _store()
        # Narrow through the equality indexes first; the predicate chain below is UNCHANGED and
        # still decides every row, so the index can only save work, never change the answer.
        rows = st.narrow(
            TradingOrder,
            (("account_id", account_id) if account_id is not None else (None, None),
             ("transaction_id", transaction_id) if transaction_id is not None else (None, None),
             ("broker_order_id", broker_order_id) if broker_order_id is not None else (None, None),
             ("parent_order_id", parent_order_id) if parent_order_id is not None else (None, None),
             ("depends_on_order", depends_on_order) if depends_on_order is not _UNSET
             else (None, None)),
            (("status", sset), ("transaction_id", tids)),
        )
        if rows is None:
            rows = st.all(TradingOrder)

        def _match(candidates):
            out = []
            for o in candidates:
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
                if parent_order_id is not None and o.parent_order_id != parent_order_id:
                    continue
                out.append(o)
            return out

        out = _match(rows)
        if _AUDIT:
            _audit("orders_where", out, _match(st.all(TradingOrder)))
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
    if parent_order_id is not None:
        stmt = stmt.where(TradingOrder.parent_order_id == parent_order_id)
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
        st = _store()
        rows = st.narrow(
            Transaction,
            (("status", status) if status is not None else (None, None),
             ("expert_id", expert_id) if expert_id is not None else (None, None),
             ("symbol", symbol) if symbol is not None else (None, None)),
            (("status", sset),),
        )
        if rows is None:
            rows = st.all(Transaction)

        def _match(candidates):
            out = []
            for t in candidates:
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

        out = _match(rows)
        if _AUDIT:
            _audit("transactions_where", out, _match(st.all(Transaction)))
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

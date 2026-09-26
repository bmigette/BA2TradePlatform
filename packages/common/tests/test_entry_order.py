"""The ONE entry-order read and the ONE duplicate-position gate, shared by live and backtest.

Review I1 of plan 2026-09-25 option-stage2-build, Task 8. The two runtimes used to answer both
questions inline, differently:

* "Which order opened the position the exits are evaluated against?" -- the backtest took the
  oldest OPENED transaction's first order with ``depends_on_order IS NULL`` (any status, any
  side, by (created_at, id)); live took the oldest OPENED-or-WAITING transaction by a raw
  ``open_date or created_at`` (a TypeError on mixed naive/aware rows) and then its oldest FILLED
  order on the transaction's side (no id tie-break). ``EntryTagCondition`` reads the member tag
  off that row, so a disagreement makes a style member's exits never fire in one runtime.
* "Does this expert already hold (or wait on) this symbol?" -- two copies of the same query.

Every test runs against BOTH stores the two runtimes read: SQLite (live, and file-backed
backtests) and the backtest's in-memory trade store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_db, update_instance
from ba2_common.core.entry_order import (
    EntryOrderUnresolved, entry_order_for_transaction, oldest_entry_order,
    oldest_open_transaction, positions_to_manage)
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.trade_cycle import open_or_waiting_position
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus)

T0 = datetime(2026, 3, 2, 14, 30)                       # naive, read as UTC
_ids = count(71_000)


@pytest.fixture(params=["sqlite", "inmem"])
def store(request):
    """``(mode, session)``: every row the test writes lands in that store."""
    if request.param == "inmem":
        with ts.inmem_trades():
            yield "inmem", None
    else:
        with get_db() as session:
            yield "sqlite", session


def _uniq():
    n = next(_ids)
    return n, f"EO{n}"


def _txn(expert_id, symbol, status=TransactionStatus.OPENED, *, open_date=T0, created_at=None,
         side=OrderDirection.BUY):
    t = Transaction(symbol=symbol, quantity=1.0, side=side, status=status, open_date=open_date,
                    created_at=created_at or open_date or T0, expert_id=expert_id,
                    open_price=10.0)
    add_instance(t, expunge_after_flush=True)
    return t


def _order(txn, *, status=OrderStatus.FILLED, side=None, created_at=T0, depends_on=None,
           parent=None, data=None, asset_class=AssetClass.EQUITY):
    o = TradingOrder(account_id=1, symbol=txn.symbol, quantity=1.0,
                     side=side or txn.side, order_type=OrderType.MARKET, status=status,
                     transaction_id=txn.id, depends_on_order=depends_on, parent_order_id=parent,
                     created_at=created_at, data=data, asset_class=asset_class)
    add_instance(o, expunge_after_flush=True)
    return o


# --------------------------------------------------------------------------------------------- #
# entry_order_for_transaction: WHICH row of a transaction is its entry
# --------------------------------------------------------------------------------------------- #
def test_the_entry_is_the_first_written_root_order_whatever_its_status(store):
    """A partly filled (then cancelled) entry is still THE entry -- the row the tag and the
    entry facts were written on -- even when a later root order on the same side FILLED (a
    scale-in). Old live picked the later FILLED row here."""
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    first = _order(t, status=OrderStatus.CANCELED, data={"entry_tag": 4})
    _order(t, status=OrderStatus.FILLED, created_at=T0 + timedelta(days=2))
    got = entry_order_for_transaction(t.id, session=session)
    assert got.id == first.id and got.data == {"entry_tag": 4}


def test_a_dependent_order_is_never_the_entry(store):
    """TP/SL legs and assignment call-away closes carry ``depends_on_order``: even an OLDER one
    (a clock skew) is never read as the entry."""
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    entry = _order(t, created_at=T0)
    _order(t, created_at=T0 - timedelta(hours=1), depends_on=entry.id,
           side=OrderDirection.SELL)
    assert entry_order_for_transaction(t.id, session=session).id == entry.id


def test_a_multi_leg_child_is_never_the_entry(store):
    """Leg children of an option combo have ``depends_on_order IS NULL`` too; the entry facts
    (and the member tag) live on the PARENT. Even a leg filed first with the same timestamp
    must not win, as it would on a bare (created_at, id) sort."""
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym, side=OrderDirection.SELL)
    leg = _order(t, side=OrderDirection.SELL, asset_class=AssetClass.OPTION)
    parent = _order(t, side=OrderDirection.SELL, asset_class=AssetClass.OPTION,
                    data={"entry_tag": 9})
    leg.parent_order_id = parent.id
    update_instance(leg)
    assert entry_order_for_transaction(t.id, session=session).id == parent.id


def test_equal_timestamps_break_on_id(store):
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    a = _order(t)
    _order(t)
    assert entry_order_for_transaction(t.id, session=session).id == a.id


def test_mixed_naive_and_aware_created_at_order_by_instant(store):
    """The later-written row carries the EARLIER instant, aware; the other is naive. A raw
    sort raises TypeError; the shared read compares instants."""
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    naive = _order(t, created_at=T0)
    aware = _order(t, created_at=(T0 - timedelta(minutes=5)).replace(tzinfo=timezone.utc))
    rows = ts.orders_where(transaction_id=t.id, session=session)
    for o in rows:                        # restore the shapes a store round trip may normalise
        o.created_at = (naive.created_at.replace(tzinfo=None) if o.id == naive.id
                        else (T0 - timedelta(minutes=5)).replace(tzinfo=timezone.utc))
    assert {o.created_at.tzinfo is None for o in rows} == {True, False}
    assert entry_order_for_transaction(t.id, session=session).id == aware.id


def test_no_root_order_reads_as_none(store):
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    assert entry_order_for_transaction(t.id, session=session) is None


# --------------------------------------------------------------------------------------------- #
# oldest_open_transaction / oldest_entry_order: WHOSE position the exits read
# --------------------------------------------------------------------------------------------- #
def _bare(ident, status, open_date=None, created_at=None):
    return Transaction(id=ident, symbol="X", quantity=1.0, side=OrderDirection.BUY,
                       status=status, open_date=open_date, created_at=created_at)


def test_only_an_OPENED_transaction_is_a_position_to_exit():
    """A WAITING transaction has not opened: even when it is OLDER it is never the one whose
    entry order the exits read (old live picked it, found no FILLED order, and every
    order-reading exit on the symbol went False)."""
    waiting = _bare(1, TransactionStatus.WAITING, open_date=T0)
    opened = _bare(2, TransactionStatus.OPENED, open_date=T0 + timedelta(days=1))
    for txns in ([waiting, opened], [opened, waiting]):
        assert oldest_open_transaction(txns) is opened
    assert oldest_open_transaction([waiting]) is None
    assert oldest_open_transaction([]) is None


def test_the_oldest_is_by_UTC_instant_across_naive_and_aware():
    naive = _bare(1, TransactionStatus.OPENED, open_date=T0)                      # 14:30 UTC
    aware = _bare(2, TransactionStatus.OPENED,
                  open_date=datetime(2026, 3, 2, 15, 0, tzinfo=timezone(timedelta(hours=2))))
    with pytest.raises(TypeError):                                    # the old live key
        min([naive, aware], key=lambda t: t.open_date or t.created_at)
    for txns in ([naive, aware], [aware, naive]):
        assert oldest_open_transaction(txns) is aware                 # 13:00 UTC


def test_open_date_falls_back_to_created_at_and_unknown_sorts_last():
    by_created = _bare(1, TransactionStatus.OPENED, created_at=T0 + timedelta(days=1))
    unknown = _bare(2, TransactionStatus.OPENED)
    later = _bare(3, TransactionStatus.OPENED, open_date=T0 + timedelta(days=2))
    assert oldest_open_transaction([unknown, later, by_created]) is by_created
    assert oldest_open_transaction([unknown, later]) is later


def test_equal_open_times_break_on_id_not_list_order():
    a = _bare(5, TransactionStatus.OPENED, open_date=T0)
    b = _bare(4, TransactionStatus.OPENED, open_date=T0)
    for txns in ([a, b], [b, a]):
        assert oldest_open_transaction(txns) is b


def test_oldest_entry_order_returns_the_tag_row_of_the_oldest_open_position(store):
    mode, session = store
    _, sym = _uniq()
    old = _txn(1, sym, open_date=T0)
    new = _txn(1, sym, open_date=T0 + timedelta(days=3))
    waiting = _txn(1, sym, TransactionStatus.WAITING, open_date=None,
                   created_at=T0 - timedelta(days=9))
    tag_a = _order(old, data={"entry_tag": 7})
    _order(new, data={"entry_tag": 3})
    _order(waiting, status=OrderStatus.ACCEPTED, data={"entry_tag": 3})
    assert oldest_entry_order([new, waiting, old], session=session).id == tag_a.id


def test_no_open_position_means_no_entry_order(store):
    mode, session = store
    _, sym = _uniq()
    waiting = _txn(1, sym, TransactionStatus.WAITING)
    _order(waiting, status=OrderStatus.ACCEPTED)
    assert oldest_entry_order([waiting], session=session) is None
    assert oldest_entry_order([], session=session) is None


def test_an_open_position_with_no_entry_order_is_refused_by_name(store):
    """Never a silent None: every order-reading exit (P&L, days opened, the member tag) would
    evaluate False for it and a style member's exits could never close it."""
    mode, session = store
    _, sym = _uniq()
    t = _txn(1, sym)
    with pytest.raises(EntryOrderUnresolved, match=rf"transaction {t.id} \({sym}\)"):
        oldest_entry_order([t], session=session)


# --------------------------------------------------------------------------------------------- #
# open_or_waiting_position: the one-position-per-expert-per-symbol entry gate
# --------------------------------------------------------------------------------------------- #
def test_the_dup_gate_counts_opened_and_waiting_per_expert_and_symbol(store):
    mode, session = store
    n, sym = _uniq()
    other_sym = sym + "Z"
    assert open_or_waiting_position(n, sym, session=session) is None
    _txn(n, sym, TransactionStatus.CLOSED)
    assert open_or_waiting_position(n, sym, session=session) is None     # a close re-arms
    waiting = _txn(n, sym, TransactionStatus.WAITING)
    opened = _txn(n, sym, TransactionStatus.OPENED)
    got = open_or_waiting_position(n, sym, session=session)
    assert got is not None and got.id == min(waiting.id, opened.id)
    assert open_or_waiting_position(n + 1, sym, session=session) is None  # other expert
    assert open_or_waiting_position(n, other_sym, session=session) is None
    _txn(n, other_sym, TransactionStatus.OPENED)
    assert open_or_waiting_position(n, other_sym, session=session) is not None


# --------------------------------------------------------------------------------------------- #
# positions_to_manage: the book an exit pass evaluates a symbol against
# --------------------------------------------------------------------------------------------- #
class _Pending:
    def __init__(self, *closing):
        self.closing = set(closing)

    def has_pending_closing_order(self, transaction_id):
        return transaction_id in self.closing


def test_the_managed_book_is_OPENED_minus_a_pending_close_in_the_given_order():
    a = _bare(3, TransactionStatus.OPENED, open_date=T0)
    waiting = _bare(1, TransactionStatus.WAITING, open_date=T0)
    closing = _bare(2, TransactionStatus.OPENED, open_date=T0)
    b = _bare(4, TransactionStatus.OPENED, open_date=T0)
    closed = _bare(5, TransactionStatus.CLOSED, open_date=T0)
    got = positions_to_manage([a, waiting, closing, b, closed], _Pending(2))
    assert [t.id for t in got] == [3, 4]


def test_a_WAITING_only_book_is_not_managed():
    """Filled at the broker but not promoted yet: not a position to exit (the caller skips the
    symbol rather than evaluating it with no entry order)."""
    assert positions_to_manage([_bare(1, TransactionStatus.WAITING, open_date=T0)],
                               _Pending()) == []

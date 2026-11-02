"""``has_pending_closing_order``: resting TP/SL protection is not a pending close.

THE LIVE DEFECT THIS PINS (2026-09-26). Alpaca writes an OCO's stop leg as its own row: a
``HELD`` ``SELL_STOP_LIMIT`` with ``parent_order_id`` -> the OCO and NO ``depends_on_order``.
The guard read every ``depends_on_order IS NULL`` row after the entry that was not terminal as
"a close is working", so that HELD leg made every OCO-protected position read "close pending"
for as long as its bracket rested. ``TradeManager.process_open_positions_recommendations``
then dropped the transaction, and its rule-based exits never ran: 13 of 23 expert positions on
prod, 30 of 114 on dev. The backtest never writes such rows (its TP/SL live on the
transaction), so live had silently diverged from it.

The books below are the prod rows, reshaped only in ids: transaction 203 (CELH) held entry
607 (FILLED), TP 608 / SL 609 (CANCELED, dependent), OCO 616 (NEW, chained on 609's cancel)
and leg 620 (HELD, parent 616).

The REAL ``ReadOnlyAccountInterface.has_pending_closing_order`` runs over the real store:
faking it would fake away the guard under test.
"""
from datetime import datetime, timedelta, timezone

import pytest


T0 = datetime(2026, 9, 10, 13, 35, 59, tzinfo=timezone.utc)


class _Account:
    """The real guard bound onto a bare account (the interface wants broker wiring)."""

    id = 1

    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    has_pending_closing_order = ReadOnlyAccountInterface.has_pending_closing_order


@pytest.fixture
def db(tmp_path):
    from ba2_common.core import db as _db
    _db.configure_db(str(tmp_path / "pending_close.sqlite"))
    _db.init_db()
    return _db


def _txn(db, symbol="CELH"):
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus
    return db.add_instance(Transaction(
        symbol=symbol, quantity=3, side=OrderDirection.BUY, status=TransactionStatus.OPENED,
        open_price=27.38, open_date=T0, take_profit=31.28, stop_loss=23.86, expert_id=1))


def _order(db, txn_id, *, side="SELL", order_type="MARKET", status="FILLED", at=T0,
           depends_on=None, trigger=None, parent=None, comment=None, data=None,
           symbol="CELH", **extra):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType
    order = TradingOrder(
        account_id=1, symbol=symbol, quantity=3, side=OrderDirection[side],
        order_type=OrderType[order_type], status=OrderStatus[status], transaction_id=txn_id,
        depends_on_order=depends_on,
        depends_order_status_trigger=OrderStatus[trigger] if trigger else None,
        parent_order_id=parent, comment=comment, data=data, created_at=at,
        filled_qty=3 if status == "FILLED" else 0, **extra)
    return db.add_instance(order)


def _celh_book(db):
    """Prod transaction 203: entry, its cancelled TP/SL, the chained OCO and its HELD leg."""
    txn = _txn(db)
    entry = _order(db, txn, side="BUY", status="FILLED", open_price=27.38,
                   comment="[ACC:1/TR:8/REC:1763]")
    _order(db, txn, order_type="SELL_LIMIT", status="CANCELED", depends_on=entry,
           trigger="FILLED", at=T0 + timedelta(seconds=1),
           comment=f"20260910133559-TP-[ACC:1/TR:{txn}/PORD:{entry}]",
           data={"tp_percent_target": 0.0, "tpsl_reference_price": 27.6})
    sl = _order(db, txn, order_type="SELL_STOP", status="CANCELED", depends_on=entry,
                trigger="FILLED", at=T0 + timedelta(seconds=2),
                comment=f"20260910133600-SL-[ACC:1/TR:{txn}/PORD:{entry}]")
    oco = _order(db, txn, order_type="OCO", status="NEW", depends_on=sl, trigger="CANCELED",
                 at=T0 + timedelta(hours=1, minutes=13), limit_price=31.28, stop_price=23.86,
                 comment=f"20260910144853-TPSL-[ACC:1/TR:{txn}/PORD:{entry}] (chained on cancel of {sl})")
    leg = _order(db, txn, order_type="SELL_STOP_LIMIT", status="HELD", parent=oco,
                 at=T0 + timedelta(hours=1, minutes=14), limit_price=23.74, stop_price=23.86,
                 comment=f"1789051776-OCO-SL-[PARENT:{oco}/BROKER:12e99ccb]")
    return txn, entry, oco, leg


def test_the_prod_celh_book_has_no_pending_close(db):
    txn, entry, _oco, leg = _celh_book(db)

    # The fixture really is the failing shape: a non-terminal ROOT row after the entry.
    from ba2_common.core.trade_store import orders_where
    roots = {o.id: o for o in orders_where(transaction_id=txn, depends_on_order=None)}
    assert set(roots) == {entry, leg} and roots[leg].status.name == "HELD"

    assert _Account().has_pending_closing_order(txn) is False


def test_a_root_oco_placed_on_a_filled_entry_is_protection(db):
    """``_create_broker_oco_order`` places the OCO with NO ``depends_on_order`` once the entry
    has filled (the TP/SL adjust path), and its legs follow as HELD/NEW children."""
    txn = _txn(db)
    entry = _order(db, txn, side="BUY", status="FILLED")
    oco = _order(db, txn, order_type="OCO", status="NEW", at=T0 + timedelta(days=1),
                 comment=f"20260911100000-TPSL-[ACC:1/TR:{txn}/PORD:{entry}]",
                 data={"tp_percent_target": 14.2, "sl_percent_target": 12.8})
    _order(db, txn, order_type="SELL_LIMIT", status="NEW", parent=oco,
           at=T0 + timedelta(days=1, seconds=1), comment=f"1789-OCO-TP-[PARENT:{oco}/BROKER:x]")
    _order(db, txn, order_type="SELL_STOP_LIMIT", status="HELD", parent=oco,
           at=T0 + timedelta(days=1, seconds=1), comment=f"1789-OCO-SL-[PARENT:{oco}/BROKER:x]")

    assert _Account().has_pending_closing_order(txn) is False


@pytest.mark.parametrize("comment,data", [
    ("20260709133017-SL-[ACC:1/TR:113/PORD:403]", {"sl_percent_target": 3.0}),
    ("20260709133017-SL-[ACC:1/TR:113/PORD:403]", None),          # older rows: comment only
    ("1780305214-OCO-SL-[PARENT:220/BROKER:7e75]", None),          # leg that lost its parent link
], ids=["comment+data", "comment-only", "orphan-oco-leg"])
def test_a_standalone_resting_stop_is_protection(db, comment, data):
    txn = _txn(db)
    _order(db, txn, side="BUY", status="FILLED")
    _order(db, txn, order_type="SELL_STOP", status="NEW", at=T0 + timedelta(days=2),
           comment=comment, data=data, stop_price=23.86)

    assert _Account().has_pending_closing_order(txn) is False


@pytest.mark.parametrize("status", ["PENDING", "NEW", "ACCEPTED", "PARTIALLY_FILLED", "HELD"])
def test_a_working_close_is_pending_even_behind_a_resting_oco(db, status):
    """The guard's job is unchanged: a submitted close that has not resolved blocks a second
    one, whatever protection is resting next to it."""
    txn, *_ = _celh_book(db)
    _order(db, txn, status=status, at=T0 + timedelta(days=3),
           comment=f"Closing position for transaction {txn}")

    assert _Account().has_pending_closing_order(txn) is True


@pytest.mark.parametrize("status", ["FILLED", "CANCELED", "REJECTED", "EXPIRED"])
def test_a_resolved_close_is_not_pending(db, status):
    txn, *_ = _celh_book(db)
    _order(db, txn, status=status, at=T0 + timedelta(days=3),
           comment=f"Closing position for transaction {txn}")

    assert _Account().has_pending_closing_order(txn) is False


def test_a_multileg_entry_with_legs_still_working_is_not_a_pending_close(db):
    """A structure's per-contract legs are children of its net-only parent: the parent carries
    the status. Entry legs that have not caught up must not read as a close."""
    from ba2_common.core.types import AssetClass
    txn = _txn(db, symbol="ACN")
    parent = _order(db, txn, symbol="ACN", side="SELL", order_type="SELL_LIMIT",
                    status="FILLED", asset_class=AssetClass.OPTION)
    for contract in ("ACN260717P00100000", "ACN260717P00095000"):
        _order(db, txn, symbol=contract, order_type="SELL_LIMIT", status="PENDING",
               parent=parent, at=T0 + timedelta(seconds=1), asset_class=AssetClass.OPTION)
    assert _Account().has_pending_closing_order(txn) is False

    # ...while a working multi-leg CLOSE is its parent, at the root.
    close = _order(db, txn, symbol="ACN", side="BUY", order_type="MARKET", status="PENDING",
                   at=T0 + timedelta(days=1), asset_class=AssetClass.OPTION)
    _order(db, txn, symbol="ACN260717P00100000", side="BUY", order_type="MARKET",
           status="PENDING", parent=close, at=T0 + timedelta(days=1, seconds=1),
           asset_class=AssetClass.OPTION)
    assert _Account().has_pending_closing_order(txn) is True


# --------------------------------------------------------------------- mixed timestamps
def _mem_order(oid, *, at, status="FILLED", side="SELL", order_type="MARKET", parent=None,
               comment=None):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType
    return TradingOrder(id=oid, account_id=1, symbol="CELH", quantity=3,
                        side=OrderDirection[side], order_type=OrderType[order_type],
                        status=OrderStatus[status], transaction_id=1, created_at=at,
                        parent_order_id=parent, comment=comment)


@pytest.mark.parametrize("entry_naive", [True, False], ids=["naive-entry", "aware-entry"])
def test_mixed_naive_and_aware_timestamps_sort_without_error(monkeypatch, entry_naive):
    """SQLite hands a column back naive or aware depending on how it was written; the old
    ``created_at or datetime.min.replace(tzinfo=utc)`` key raised TypeError on a mix."""
    import ba2_common.core.trade_store as trade_store

    entry_at = T0.replace(tzinfo=None) if entry_naive else T0
    close_at = T0 + timedelta(days=1)
    close_at = close_at if entry_naive else close_at.replace(tzinfo=None)
    book = [
        _mem_order(3, at=close_at, status="NEW", comment="Closing position for transaction 1"),
        _mem_order(1, side="BUY", at=entry_at),
        _mem_order(2, at=None, status="HELD", order_type="SELL_STOP_LIMIT", parent=9,
                   comment="1789-OCO-SL-[PARENT:9/BROKER:x]"),
    ]
    monkeypatch.setattr(trade_store, "orders_where", lambda **kw: list(book))
    assert _Account().has_pending_closing_order(1) is True

    book[0].status = book[0].status.__class__.FILLED
    assert _Account().has_pending_closing_order(1) is False


def test_the_entry_is_the_oldest_by_utc_instant_not_by_wall_text():
    """A naive entry and an aware close are compared as UTC instants, then by id."""
    from ba2_common.core.TransactionHelper import TransactionHelper

    entry = _mem_order(5, side="BUY", at=T0.replace(tzinfo=None))
    close = _mem_order(4, status="NEW", at=(T0 + timedelta(minutes=1)).astimezone(
        timezone(timedelta(hours=2))))
    assert [o.id for o in TransactionHelper.pending_closing_orders([close, entry])] == [4]


def test_protection_is_recognised_only_by_its_writers_marks():
    """A close's free text must not be mistaken for protection (that would re-open the
    2026-07-21 double-close runaway the guard exists for)."""
    from ba2_common.core.TransactionHelper import TransactionHelper as TH

    assert not TH.is_resting_protection(_mem_order(1, at=T0, comment="Closing position for transaction 7"))
    assert not TH.is_resting_protection(_mem_order(1, at=T0, comment="TP hit - SL moved, closing"))
    assert not TH.is_resting_protection(_mem_order(1, at=T0, comment="FactorRanker rebalance sell"))
    assert not TH.is_resting_protection(_mem_order(1, at=T0, comment=None))
    assert TH.is_resting_protection(_mem_order(1, at=T0, order_type="SELL_LIMIT",
                                               comment="20260910133559-TP-[ACC:1/TR:2/PORD:3]"))
    assert TH.is_resting_protection(_mem_order(1, at=T0, order_type="SELL_STOP",
                                               comment="20260910133559-SL-[ACC:1/TR:2/PORD:3]"))
    # A MARKET row is a close whatever its comment says (a breached stop re-sent as MARKET).
    assert not TH.is_resting_protection(_mem_order(1, at=T0, order_type="MARKET",
                                                   comment="20260910133559-SL-[ACC:1/TR:2/PORD:3]"))
    assert TH.is_resting_protection(_mem_order(1, at=T0, order_type="OCO"))
    assert TH.is_resting_protection(_mem_order(1, at=T0, parent=4))


# ----------------------------------------------------- a breached stop re-sent as MARKET
BREACHED = (" | [stop_through_market] stop price must be less than current price"
            " — auto-converted to MARKET (stop already breached)")


def test_a_breached_stop_resent_as_market_is_a_pending_close(db):
    """``AccountInterface._handle_order_submit_error`` turns a stop rejected as already
    breached into a MARKET order ON THE SAME ROW and keeps its ``<ts>-SL-[`` comment. That is
    a real full-size market sell: while it works, a second close must be refused."""
    txn = _txn(db)
    _order(db, txn, side="BUY", status="FILLED")
    _order(db, txn, order_type="MARKET", status="NEW", at=T0 + timedelta(days=2),
           comment="20260709133017-SL-[ACC:1/TR:113/PORD:403]" + BREACHED)

    assert _Account().has_pending_closing_order(txn) is True


def test_the_real_breach_conversion_turns_protection_into_a_pending_close(db):
    """Through the real ``_handle_order_submit_error``: the resting root stop is protection;
    the same row, converted to MARKET and working, is a pending close."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.interfaces.AccountInterface import AccountInterface
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import BrokerOrderErrorReason, OrderStatus, OrderType

    class _Converting(_Account):
        _STOP_ORDER_TYPES = AccountInterface._STOP_ORDER_TYPES
        _handle_order_submit_error = AccountInterface._handle_order_submit_error

        def _classify_order_error(self, exc):
            return BrokerOrderErrorReason.STOP_THROUGH_MARKET

        def _submit_order_impl(self, order, is_closing_order=False):
            order.status = OrderStatus.NEW          # the broker took the market sell
            db.update_instance(order)
            return order

    txn = _txn(db)
    _order(db, txn, side="BUY", status="FILLED")
    stop = _order(db, txn, order_type="SELL_STOP", status="PENDING", at=T0 + timedelta(days=2),
                  comment=f"20260709133017-SL-[ACC:1/TR:{txn}/PORD:1]", stop_price=23.86)
    account = _Converting()
    assert account.has_pending_closing_order(txn) is False

    account._handle_order_submit_error(get_instance(TradingOrder, stop),
                                       RuntimeError("stop price must be less than current price"))

    row = get_instance(TradingOrder, stop)
    assert row.order_type == OrderType.MARKET and "-SL-[" in row.comment
    assert account.has_pending_closing_order(txn) is True


def test_the_tpsl_comment_helper_stamps_the_mark_the_classifier_reads():
    from ba2_common.core.TransactionHelper import TransactionHelper as TH

    for kind in ("TP", "SL", "TPSL"):
        comment = TH.tpsl_comment(kind, 1, 2, 3, note="resized")
        assert comment.endswith(f"-{kind}-[ACC:1/TR:2/PORD:3] resized")
        assert TH.is_resting_protection(_mem_order(1, at=T0, order_type="SELL_STOP",
                                                   comment=comment))
    with pytest.raises(ValueError):
        TH.tpsl_comment("CLOSE", 1, 2, 3)


# ------------------------------------------- writers that used to stamp no protection mark
class _AddAccount:
    """The broker edge for ``adjust_quantity_with_tpsl``'s add-to-position branch."""

    id = 1

    def submit_order(self, order, **kw):
        from ba2_common.core.db import add_instance, get_instance
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import OrderStatus
        order.status = OrderStatus.NEW
        oid = add_instance(order)
        return get_instance(TradingOrder, oid)

    def cancel_order(self, order_id):
        return True


@pytest.mark.parametrize("tp,sl", [(31.0, None), (None, 24.0), (31.0, 24.0)],
                         ids=["tp-only", "sl-only", "oco"])
def test_add_to_position_protection_with_nothing_to_replace_is_protection(db, tp, sl):
    """With no existing TP/SL there is no order to chain on, so the new leg is written at the
    ROOT (no ``depends_on_order``). It must carry the TP/SL mark."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.TransactionHelper import TransactionHelper as TH
    from ba2_common.core.trade_store import orders_where

    txn_id = _txn(db)
    entry = _order(db, txn_id, side="BUY", status="FILLED")
    txn = get_instance(Transaction, txn_id)
    txn.take_profit = None
    txn.stop_loss = None
    db.update_instance(txn)

    result = TH.adjust_quantity_with_tpsl(_AddAccount(), get_instance(Transaction, txn_id), 2.0,
                                          tp_price=tp, sl_price=sl)
    assert result["success"], result["message"]

    legs = [o for o in orders_where(transaction_id=txn_id, depends_on_order=None)
            if o.id != entry and o.comment != "Add-to-position order"]
    assert len(legs) == 1
    assert TH.is_resting_protection(legs[0]), legs[0].comment

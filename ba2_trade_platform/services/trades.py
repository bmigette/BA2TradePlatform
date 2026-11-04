"""Platform trades (Transaction / TradingOrder) for the management API. Reads only in phase 2; the TP/SL,
close and cancel write functions join this module in phase 3."""
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlmodel import select

from ba2_common.core.trade_store import orders_where
from ba2_common.core.utils import calculate_transaction_pnl

from ..core.db import get_db
from ..core.models import ActivityLog, TradingOrder, Transaction
from ..core.types import OrderStatus, TransactionStatus
from . import open_pnl
from ._common import naive_utc, page, parse_enum, require_row, row_dict, utcnow_iso


def _transaction_dict(tx: Transaction) -> Dict[str, Any]:
    out = row_dict(tx)
    # A null P&L is always explained by ``pnl_status``:
    #   "realized"      -- closed, P&L computed (``realized_pnl``)
    #   "open_measured" -- OPENED / CLOSING, priced at the broker's position price (``open_pnl``, ``current_price``)
    #   "unmeasured"    -- closed (or open) but it cannot be computed: no close / open / current price. A gap, not a
    #                      zero; ``open_pnl_reason`` says why for an open row
    #   "open"          -- holds no position yet (WAITING): there is nothing to measure
    pnl = calculate_transaction_pnl(tx)
    out["realized_pnl"] = pnl
    out["open_pnl"] = None
    out["current_price"] = None
    if tx.status != TransactionStatus.CLOSED:
        out["pnl_status"] = "open"
    else:
        out["pnl_status"] = "realized" if pnl is not None else "unmeasured"
    return out


def _merge_open_pnl(items: list) -> bool:
    """Fill ``open_pnl`` / ``current_price`` / ``pnl_status`` of the OPENED and CLOSING rows in ``items`` from
    ``open_pnl.open_pnl``. True when any row was priced (the caller then stamps ``as_of``)."""
    priced = open_pnl.open_pnl([i["id"] for i in items if i["status"] in
                                (s.value for s in open_pnl.PRICED_STATUSES)])
    for item in items:
        if item["id"] in priced:
            item.update(priced[item["id"]])
    return bool(priced)


def list_transactions(*, account: Optional[int], expert: Optional[int], status: Optional[str],
                      symbol: Optional[str], from_: Optional[datetime], to: Optional[datetime],
                      limit: int, offset: int) -> Dict[str, Any]:
    stmt = select(Transaction)
    if account is not None:      # a transaction has no account column: it belongs to its orders' account
        stmt = stmt.where(Transaction.id.in_(
            select(TradingOrder.transaction_id).where(TradingOrder.account_id == account)))
    if expert is not None:
        stmt = stmt.where(Transaction.expert_id == expert)
    if status is not None:
        stmt = stmt.where(Transaction.status == parse_enum(TransactionStatus, status, "status"))
    if symbol is not None:
        stmt = stmt.where(Transaction.symbol == symbol.upper())
    if from_ is not None:
        stmt = stmt.where(Transaction.open_date >= naive_utc(from_))
    if to is not None:
        stmt = stmt.where(Transaction.open_date <= naive_utc(to))
    with get_db() as session:
        result = page(session, stmt.order_by(Transaction.id.desc()), limit=limit, offset=offset,
                      convert=_transaction_dict)
    # after the session is closed: pricing talks to the brokers
    if _merge_open_pnl(result["items"]):
        result["as_of"] = utcnow_iso()
    return result


def transaction_detail(transaction_id: int) -> Dict[str, Any]:
    with get_db() as session:
        tx = require_row(session, Transaction, transaction_id, "transaction")
        out = _transaction_dict(tx)
        orders = sorted(orders_where(transaction_id=transaction_id), key=lambda o: o.id)
        # Nothing about a transaction is logged before its first order (or its open date), so that moment bounds
        # the scan: json_extract cannot use an index, created_at can.
        starts = [o.created_at for o in orders if o.created_at is not None]
        if tx.open_date is not None:
            starts.append(tx.open_date)
        stmt = select(ActivityLog).where(func.json_extract(ActivityLog.data, "$.transaction_id") == transaction_id)
        if starts:
            stmt = stmt.where(ActivityLog.created_at >= naive_utc(min(starts)))
        activity = session.exec(stmt.order_by(ActivityLog.id.desc()).limit(200)).all()
    out["orders"] = [row_dict(o) for o in orders]
    out["activity"] = [row_dict(a) for a in activity]
    if _merge_open_pnl([out]):
        out["as_of"] = utcnow_iso()
    return out


def list_orders(*, account: Optional[int], status: Optional[str], transaction: Optional[int],
                limit: int, offset: int) -> Dict[str, Any]:
    """Platform orders (TradingOrder rows), including PENDING and WAITING_TRIGGER ones."""
    stmt = select(TradingOrder)
    if account is not None:
        stmt = stmt.where(TradingOrder.account_id == account)
    if status is not None:
        stmt = stmt.where(TradingOrder.status == parse_enum(OrderStatus, status, "status"))
    if transaction is not None:
        stmt = stmt.where(TradingOrder.transaction_id == transaction)
    with get_db() as session:
        return page(session, stmt.order_by(TradingOrder.id.desc()), limit=limit, offset=offset,
                    convert=row_dict)

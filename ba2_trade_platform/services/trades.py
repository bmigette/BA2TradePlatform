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
from ._common import naive_utc, page, parse_enum, require_row, row_dict


def _transaction_dict(tx: Transaction) -> Dict[str, Any]:
    out = row_dict(tx)
    # open P&L is a phase-4 stats figure; here a figure is only ever REALIZED, and a null is explained:
    #   "realized"   -- closed, P&L computed
    #   "unmeasured" -- closed but it cannot be computed (no close/open price): a gap, not a zero
    #   "open"       -- not closed yet
    pnl = calculate_transaction_pnl(tx)
    out["realized_pnl"] = pnl
    if tx.status != TransactionStatus.CLOSED:
        out["pnl_status"] = "open"
    else:
        out["pnl_status"] = "realized" if pnl is not None else "unmeasured"
    return out


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
        return page(session, stmt.order_by(Transaction.id.desc()), limit=limit, offset=offset,
                    convert=_transaction_dict)


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

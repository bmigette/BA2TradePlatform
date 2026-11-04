"""Platform trades (Transaction / TradingOrder): the reads of the management API (phase 2) and the writes the Live
Trades page and the API SHARE (phase 3: TP/SL, close, cancel). "One function, two callers": the page keeps only its
dialogs and notifications; every decision and every broker call is made here, so the two cannot disagree."""
import asyncio
import math
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlmodel import select

from ba2_common.core.trade_store import orders_where
from ba2_common.core.utils import calculate_transaction_pnl

from ..core.db import get_db, update_instance
from ..core.models import AccountDefinition, ActivityLog, TradingOrder, Transaction
from ..core.TransactionHelper import TransactionHelper
from ..core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ..core.utils import (close_transaction_with_logging, get_account_instance_from_id,
                          get_account_instance_from_transaction)
from ..logger import logger
from ..modules.accounts import get_account_class
from ._common import jsonable, naive_utc, page, parse_enum, require_row, row_dict
from ._locks import expert_write_lock
from .accounts import broker_write, map_broker_error
from .errors import BrokerRefused, Conflict, Invalid, NotFound


def _write_scope(expert_id: Optional[int], dry_run: bool):
    """A real write holds the expert's processing locks; a dry run writes nothing and holds none."""
    return nullcontext() if dry_run else expert_write_lock(expert_id)


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


# ======================================================================================================
# Writes shared with the Live Trades page: TP/SL
# ======================================================================================================

@dataclass
class TpSlStep:
    """The ONE account call a TP/SL change needs, with the page's wording for each outcome."""
    method: str                  # adjust_tp_sl | adjust_tp | adjust_sl | cancel_standing
    take_profit: Optional[float]
    stop_loss: Optional[float]
    ok: str
    failed: str
    error: str                   # prefix of the message when the call raised


@dataclass
class TpSlResult:
    step: Optional[TpSlStep]     # None: nothing changed, nothing was called
    success: bool
    message: str
    error: Optional[Exception]   # the exception the account call raised (already logged), if any


def tp_sl_context(transaction_id: int):
    """The transaction and the account object its TP/SL calls go to: the account of its FIRST order, built fresh
    from the provider class (as the dialog does). Raises NotFound/Invalid with the dialog's own wording."""
    with get_db() as session:
        txn = session.get(Transaction, transaction_id)
        if txn is None:
            raise NotFound("Transaction not found", {"id": transaction_id})
        order = session.exec(select(TradingOrder).where(TradingOrder.transaction_id == transaction_id)
                             .order_by(TradingOrder.created_at).limit(1)).first()
        if not order or not order.account_id:
            raise Invalid("No orders linked to this transaction or order account not found",
                          {"id": transaction_id})
        acc_def = session.get(AccountDefinition, order.account_id)
        if acc_def is None:
            raise NotFound("Account definition not found", {"id": order.account_id})
        provider, acc_id = acc_def.provider, acc_def.id
        session.expunge(txn)
    account_class = get_account_class(provider)
    if not account_class:
        raise Invalid(f"Account provider {provider} not found", {"provider": provider})
    return txn, account_class(acc_id)


def validate_tp_sl(account, txn: Transaction, tp_price: Optional[float], sl_price: Optional[float]) -> None:
    """Direction-aware sanity check BEFORE anything is touched: catches swapped values (TP below entry / SL above
    it on a long) which the broker would reject anyway (Alpaca 42210000) or fill instantly. The reference is the
    account's current price, the open price when that cannot be read (what the dialog has always done).
    Raises ``Invalid`` with the reason."""
    if not (tp_price or sl_price):
        return
    try:
        ref_price = account.get_instrument_current_price(txn.symbol)
    except Exception:
        ref_price = None
    ok, reason = TransactionHelper.validate_tp_sl_prices(
        txn.side, tp_price or None, sl_price or None, reference_price=ref_price or txn.open_price)
    if not ok:
        raise Invalid(reason, {"id": txn.id})


def plan_tp_sl(txn: Transaction, tp_value: Optional[float], sl_value: Optional[float]) -> Optional[TpSlStep]:
    """Pure: the account call that makes the broker side match the requested TP/SL, or None when neither side
    changed. ``tp_value`` / ``sl_value`` are the REQUESTED state (None = no leg); "changed" is compared against the
    transaction's stored values, deletions included. The wording is the dialog's."""
    tp_changed = tp_value != txn.take_profit
    sl_changed = sl_value != txn.stop_loss
    if tp_changed and sl_changed:
        if tp_value and sl_value:
            return TpSlStep("adjust_tp_sl", tp_value, sl_value, f"TP/SL updated to ${tp_value:.2f}/${sl_value:.2f}",
                            "Failed to update TP/SL", "Error updating TP/SL: ")
        if tp_value:
            return TpSlStep("adjust_tp", tp_value, None, f"TP updated to ${tp_value:.2f}, SL removed",
                            "Failed to update TP", "Error updating TP: ")
        if sl_value:
            return TpSlStep("adjust_sl", None, sl_value, f"SL updated to ${sl_value:.2f}, TP removed",
                            "Failed to update SL", "Error updating SL: ")
        return TpSlStep("cancel_standing", None, None, "TP/SL removed", "", "Error removing TP/SL: ")
    if tp_changed:
        if tp_value:
            return TpSlStep("adjust_tp", tp_value, None, f"Take Profit updated to ${tp_value:.2f}",
                            "Failed to update Take Profit", "Error updating TP: ")
        if sl_value:
            return TpSlStep("adjust_sl", None, sl_value, "Take Profit removed, Stop Loss kept",
                            "Failed to remove Take Profit", "Error removing TP: ")
        return TpSlStep("cancel_standing", None, None, "Take Profit removed", "", "Error removing TP: ")
    if sl_changed:
        if sl_value:
            return TpSlStep("adjust_sl", None, sl_value, f"Stop Loss updated to ${sl_value:.2f}",
                            "Failed to update Stop Loss", "Error updating SL: ")
        if tp_value:
            return TpSlStep("adjust_tp", tp_value, None, "Stop Loss removed, Take Profit kept",
                            "Failed to remove Stop Loss", "Error removing SL: ")
        return TpSlStep("cancel_standing", None, None, "Stop Loss removed", "", "Error removing SL: ")
    return None


def _standing_tpsl_orders(txn_id: int) -> List[TradingOrder]:
    """Non-terminal exit legs of a transaction (anything that is not the MARKET / limit entry)."""
    with get_db() as session:
        return list(session.exec(select(TradingOrder).where(
            TradingOrder.transaction_id == txn_id,
            TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
            TradingOrder.order_type.notin_([OrderType.MARKET, "limit"]))).all())


def _cancel_standing_tpsl(account, txn: Transaction) -> None:
    """Cancel every standing exit leg that has a broker id (a leg that fails to cancel is logged and skipped)."""
    for order in _standing_tpsl_orders(txn.id):
        if order.broker_order_id:
            try:
                account.cancel_order(order.id)
                logger.info(f"Cancelled order {order.id}")
            except Exception as e:
                logger.warning(f"Failed to cancel order {order.id}: {e}")


def apply_tp_sl(account, txn: Transaction, tp_price: Optional[float], sl_price: Optional[float], *,
                source: str) -> TpSlResult:
    """Make the transaction's TP/SL ``tp_price`` / ``sl_price`` (falsy = no leg), exactly as the Live Trades dialog
    does when the quantity is not edited: persist the transaction values first (they are the source of truth),
    then ask the account to sync its orders. A raised account call is logged and returned in ``error``; it is not
    re-raised (the page notifies it, the API maps it)."""
    tp_value = tp_price if tp_price else None
    sl_value = sl_price if sl_price else None
    step = plan_tp_sl(txn, tp_value, sl_value)
    if step is None:
        return TpSlResult(None, True, "", None)
    if tp_value != txn.take_profit:
        txn.take_profit = tp_value
    if sl_value != txn.stop_loss:
        txn.stop_loss = sl_value
    update_instance(txn)
    try:
        if step.method == "cancel_standing":
            _cancel_standing_tpsl(account, txn)
            success = True
        elif step.method == "adjust_tp_sl":
            success = account.adjust_tp_sl(txn, step.take_profit, step.stop_loss, source=source)
        elif step.method == "adjust_tp":
            success = account.adjust_tp(txn, step.take_profit, source=source)
        else:
            success = account.adjust_sl(txn, step.stop_loss, source=source)
    except Exception as e:
        logger.error(f"{step.error}{e}", exc_info=True)
        return TpSlResult(step, False, f"{step.error}{e}", e)
    return TpSlResult(step, bool(success), step.ok if success else step.failed, None)


@dataclass
class OverrideClear:
    symbol: str
    cleared: List[str]           # "TP" / "SL"


def clear_override_locks(transaction_id: int) -> OverrideClear:
    """Clear tp_manual_override / sl_manual_override so the next automation cycle may adjust TP and SL again.
    ``cleared`` is empty when neither side was locked (nothing is written)."""
    with get_db() as session:
        txn = session.get(Transaction, transaction_id)
        if txn is None:
            raise NotFound("Transaction not found", {"id": transaction_id})
        had_tp_lock, had_sl_lock = txn.tp_manual_override, txn.sl_manual_override
        cleared = (["TP"] if had_tp_lock else []) + (["SL"] if had_sl_lock else [])
        if cleared:
            txn.tp_manual_override = False
            txn.sl_manual_override = False
            update_instance(txn, session)
        symbol = txn.symbol
    if cleared:
        logger.info(f"Cleared manual override on transaction {transaction_id} for {symbol}: "
                    f"tp={had_tp_lock}, sl={had_sl_lock}")
    return OverrideClear(symbol, cleared)


# ------------------------------------------------------------------------------------------------------
# API entries
# ------------------------------------------------------------------------------------------------------

_NOT_ADJUSTABLE = (TransactionStatus.CLOSING, TransactionStatus.CLOSED, TransactionStatus.FAILED)


def _leg_dict(order: TradingOrder) -> Dict[str, Any]:
    return {"id": order.id, "broker_order_id": order.broker_order_id, "order_type": order.order_type,
            "status": order.status, "quantity": order.quantity, "limit_price": order.limit_price,
            "stop_price": order.stop_price}


def _account_id_of(account) -> Optional[int]:
    return account.id if hasattr(account, "id") else None


def _positive_price(name: str, value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise Invalid(f"{name} must be a positive number", {"field": name})
    return float(value)


def set_tp_sl(transaction_id: int, *, take_profit: Optional[float], stop_loss: Optional[float],
              lock: bool, dry_run: bool) -> Dict[str, Any]:
    """POST /transactions/{id}/tp-sl: the Live Trades dialog's TP/SL path (no quantity edit).

    The request names the sides to CHANGE; an omitted side keeps its stored value, and the RESULTING state is what
    is validated and applied -- exactly what the dialog submits. ``lock=True`` is the dialog's own call
    (``source="manual"``: the account sets the manual-override lock); otherwise ``source="api"``, and a side that is
    currently locked is REFUSED (409) instead of being silently dropped by the account's lock rule.
    ``dry_run`` validates, then reports the planned account call and the standing exit legs it may replace, and
    writes and calls nothing."""
    take_profit = _positive_price("take_profit", take_profit)
    stop_loss = _positive_price("stop_loss", stop_loss)
    if take_profit is None and stop_loss is None:
        raise Invalid("Give take_profit and/or stop_loss", {"fields": ["take_profit", "stop_loss"]})
    txn, account = tp_sl_context(transaction_id)
    with _write_scope(txn.expert_id, dry_run):
        txn, account = tp_sl_context(transaction_id)        # re-read under the lock
        if txn.status in _NOT_ADJUSTABLE:
            raise Conflict(f"Transaction {transaction_id} is {txn.status.value}: its TP/SL cannot be adjusted",
                           {"id": transaction_id, "status": txn.status.value})
        if not lock:
            locked = [name for name, wanted, flag in (("take_profit", take_profit, txn.tp_manual_override),
                                                      ("stop_loss", stop_loss, txn.sl_manual_override))
                      if wanted is not None and flag]
            if locked:
                raise Conflict(f"{' and '.join(locked)} is manually locked on transaction {transaction_id}: pass "
                               f"lock=true to override it, or unlock it first", {"id": transaction_id, "locked": locked})
        tp_value = take_profit if take_profit is not None else txn.take_profit
        sl_value = stop_loss if stop_loss is not None else txn.stop_loss
        validate_tp_sl(account, txn, tp_value, sl_value)
        source = "manual" if lock else "api"
        out: Dict[str, Any] = {"transaction_id": transaction_id, "dry_run": dry_run, "source": source,
                               "lock": bool(lock), "take_profit": tp_value or None, "stop_loss": sl_value or None}
        if dry_run:
            step = plan_tp_sl(txn, tp_value or None, sl_value or None)
            out.update({"changed": step is not None, "action": step.method if step else None,
                        "would_set_locks": (["TP"] if lock and take_profit is not None else [])
                        + (["SL"] if lock and stop_loss is not None else []),
                        "standing_legs": [_leg_dict(o) for o in _standing_tpsl_orders(transaction_id)],
                        "note": "the account decides which standing legs it replaces; they are listed, not simulated"})
            return jsonable(out)
        result = apply_tp_sl(account, txn, tp_value, sl_value, source=source)
        if result.step is not None and not result.success:
            _raise_for_failed(account, transaction_id, result)
        out.update({"changed": result.step is not None, "action": result.step.method if result.step else None,
                    "message": result.message,
                    "legs": [_leg_dict(o) for o in _standing_tpsl_orders(transaction_id)]})
        return jsonable(out)


def _raise_for_failed(account, transaction_id: int, result: TpSlResult) -> None:
    """A failed account call becomes the typed error; a raised exception keeps its class name only."""
    if result.error is not None:
        mapped = map_broker_error(result.error, _account_id_of(account) or 0, "the TP/SL change")
        if mapped is None:
            raise result.error
        raise mapped from result.error
    raise BrokerRefused(f"The broker did not accept the TP/SL change on transaction {transaction_id} "
                        f"({result.message})", {"id": transaction_id})


def unlock_tp_sl(transaction_id: int, *, dry_run: bool) -> Dict[str, Any]:
    """POST /transactions/{id}/tp-sl/unlock: the dialog's "Revert (re-enable automation)"."""
    with get_db() as session:
        txn = require_row(session, Transaction, transaction_id, "transaction")
        expert_id = txn.expert_id
        would = (["TP"] if txn.tp_manual_override else []) + (["SL"] if txn.sl_manual_override else [])
    if dry_run:
        return {"transaction_id": transaction_id, "dry_run": True, "cleared": would}
    with _write_scope(expert_id, dry_run):
        result = clear_override_locks(transaction_id)
    return {"transaction_id": transaction_id, "dry_run": False, "cleared": result.cleared}


# ======================================================================================================
# Close (shared with the Live Trades page: _close_position and _retry_close_position)
# ======================================================================================================

@dataclass
class PreparedClose:
    """What ``prepare_close`` decided. ``immediate`` is set when the close was completed on the spot (an orphaned
    transaction with no orders: marked closed, no broker involved); otherwise ``account`` runs the close."""
    transaction_id: int
    account: Any
    immediate: Optional[Dict[str, Any]]


def _close_guards(txn: Transaction, retry: bool) -> None:
    """The refusals, in the page's order. ``detail["severity"] == "warning"`` marks the two the page shows as a
    warning rather than an error."""
    detail = {"id": txn.id, "status": txn.status.value}
    if retry:
        if txn.status != TransactionStatus.CLOSING:
            raise Conflict("Transaction is not in CLOSING status", dict(detail, severity="warning"))
    elif txn.status == TransactionStatus.CLOSING:
        raise Conflict("Transaction is already being closed", dict(detail, severity="warning"))
    elif txn.status == TransactionStatus.CLOSED:
        raise Conflict(f"Transaction {txn.id} is already closed", detail)
    if txn.status == TransactionStatus.FAILED:
        raise Conflict("Cannot process FAILED transaction - transaction was previously marked as failed", detail)


def prepare_close(transaction_id: int, *, retry: bool = False) -> PreparedClose:
    """Guards, account lookup and the orphaned-transaction branch of "Close Position" (``retry=True``: the
    "Reset & Retry" button for a transaction stuck in CLOSING, which requires that status)."""
    with get_db() as session:
        txn = session.get(Transaction, transaction_id)
        if txn is None:
            raise NotFound("Transaction not found", {"id": transaction_id})
        session.expunge(txn)
    _close_guards(txn, retry)
    account = get_account_instance_from_transaction(transaction_id)
    if not account:
        # Transaction is orphaned (no orders found) - mark as closed with logging
        logger.warning(f"Transaction {transaction_id} has no orders - marking as closed")
        close_transaction_with_logging(
            txn,
            account_id=1,  # default account ID for orphaned transactions (the page has always done this)
            close_reason="orphaned_no_orders",
            additional_data={"note": "Transaction had no associated orders"}
        )
        update_instance(txn)
        return PreparedClose(transaction_id, None, {"success": True, "orphaned": True,
                                                    "message": "Transaction closed (was orphaned with no orders)"})
    return PreparedClose(transaction_id, account, None)


async def run_close(prepared: PreparedClose) -> Dict[str, Any]:
    """Run the close on the account (cancels unfilled orders, deletes WAITING_TRIGGER ones, submits or retries the
    closing order, refreshes orders and transactions) and return its result dict."""
    if prepared.immediate is not None:
        return prepared.immediate
    return await prepared.account.close_transaction_async(prepared.transaction_id)


def describe_close(transaction_id: int) -> Dict[str, Any]:
    """Dry run of a close: the guards, then the orders ``close_transaction`` would cancel or delete and the closing
    order it would submit. Reads the order store only -- no account object, no broker."""
    with get_db() as session:
        txn = require_row(session, Transaction, transaction_id, "transaction")
        session.expunge(txn)
    _close_guards(txn, False)
    orders = sorted(orders_where(transaction_id=transaction_id), key=lambda o: o.id)
    if not orders:
        return {"transaction_id": transaction_id, "dry_run": True, "orphaned": True, "would_cancel": [],
                "would_delete": [], "would_submit_close": None,
                "note": "no orders: the transaction would be marked closed without contacting the broker"}
    working = [o for o in orders if o.status not in OrderStatus.get_terminal_statuses()
               and o.status != OrderStatus.FILLED]
    return {"transaction_id": transaction_id, "dry_run": True, "orphaned": False,
            "would_cancel": [_leg_dict(o) for o in working if o.broker_order_id],
            "would_delete": [_leg_dict(o) for o in working if not o.broker_order_id
                             and o.status == OrderStatus.WAITING_TRIGGER],
            "would_submit_close": {"symbol": txn.symbol, "quantity": txn.quantity,
                                   "side": "SELL" if txn.side == OrderDirection.BUY else "BUY"},
            "note": "the account decides the details: an existing live closing order is left alone, a dead one is "
                    "retried; the listed orders are the ones that may be touched"}


def close_transaction(transaction_id: int, *, dry_run: bool) -> Dict[str, Any]:
    """POST /transactions/{id}/close: the page's "Close Position". Runs the account's async close on a private
    event loop (this function is called from a worker thread)."""
    if dry_run:
        return jsonable(describe_close(transaction_id))
    with get_db() as session:
        txn = require_row(session, Transaction, transaction_id, "transaction")
        expert_id = txn.expert_id
    with expert_write_lock(expert_id):
        prepared = prepare_close(transaction_id)
        try:
            result = asyncio.run(run_close(prepared))
        except Exception as e:
            mapped = map_broker_error(e, _account_id_of(prepared.account) or 0, "the close")
            if mapped is None:
                raise
            raise mapped from e
    if not result["success"]:
        raise BrokerRefused(result["message"], {"id": transaction_id})
    out = {"transaction_id": transaction_id, "dry_run": False, "success": True, "message": result["message"],
           "orphaned": "orphaned" in result and result["orphaned"]}
    for key in ("canceled_count", "deleted_count", "close_order_id"):
        if key in result:
            out[key] = result[key]
    return jsonable(out)


# ======================================================================================================
# Cancel one order
# ======================================================================================================

#: Statuses an order cannot be cancelled from: finished ones, a fill, and a cancel already on its way.
_NOT_CANCELLABLE = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED, OrderStatus.PENDING_CANCEL}


def _cancel_guards(order: TradingOrder) -> None:
    detail = {"id": order.id, "status": order.status.value}
    if order.status in _NOT_CANCELLABLE:
        raise Conflict(f"Order {order.id} is {order.status.value}: it cannot be cancelled", detail)
    if not order.broker_order_id:
        raise Conflict(f"Order {order.id} has not been sent to the broker (status {order.status.value}); "
                       f"there is nothing to cancel there. To drop a staged exit, close or adjust its transaction",
                       detail)


def cancel_order(order_id: int, *, dry_run: bool) -> Dict[str, Any]:
    """POST /orders/{id}/cancel: ask the broker to cancel one working order -- the call the TP/SL flows already
    make for a standing leg (``account.cancel_order(order.id)``). The account marks the order PENDING_CANCEL; the
    account refresh confirms CANCELED once the broker does. 409 on a finished order, a fill, a cancel already
    pending, or an order the broker never received."""
    with get_db() as session:
        order = require_row(session, TradingOrder, order_id, "order")
        expert_id = None
        if order.transaction_id is not None:
            txn = session.get(Transaction, order.transaction_id)
            expert_id = txn.expert_id if txn is not None else None
        session.expunge(order)
    _cancel_guards(order)
    if dry_run:
        return jsonable({"order_id": order_id, "dry_run": True, "would_cancel": _leg_dict(order)})
    with expert_write_lock(expert_id):
        with get_db() as session:
            order = require_row(session, TradingOrder, order_id, "order")
            session.expunge(order)
        _cancel_guards(order)                                  # re-checked under the lock
        account = get_account_instance_from_id(order.account_id)
        if account is None:
            raise NotFound(f"No account with id {order.account_id}", {"id": order.account_id})
        accepted = broker_write(order.account_id, "the order cancel", account.cancel_order, order.id)
    if not accepted:
        raise BrokerRefused(f"The broker did not accept the cancel of order {order_id}", {"id": order_id})
    with get_db() as session:
        status = require_row(session, TradingOrder, order_id, "order").status
    return jsonable({"order_id": order_id, "dry_run": False, "cancel_requested": True, "status": status,
                     "broker_order_id": order.broker_order_id})

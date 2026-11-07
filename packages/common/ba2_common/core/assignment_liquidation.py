"""Live liquidation of the stock a short-option assignment delivered (the "not held" half).

BACKTEST: ``BacktestAccount._book_assignment_share_leg`` queues the delivered lot and
``process_pending_assignment_liquidations`` sells exactly that lot at the NEXT bar's open. WHO is
sold is decided by ``assignment_hold.holds_assigned_stock`` (shared).

LIVE (this module). The reconciler books the delivered equity Transaction and, when the shared
decision says "not held", stamps it ``meta_data["assignment_liquidation"] = "pending"``
(``PENDING_META``). ``sweep_pending_liquidations`` runs on every refresh right after the option
activities are reconciled and closes each pending transaction through ``close_transaction`` -- the
same path every close rule uses: ONE MARKET order for the transaction's open quantity (exactly the
delivered shares: the lot is its own transaction, an unrelated holding is never touched). Alpaca
queues a DAY market order placed outside the session for the next open, which is the closest live
analogue of "the next bar's open".

Loudness: a refused or failed close (pledged cover, wash-trade rejection, broker error) logs an
ERROR and a FAILURE activity every sweep and the transaction STAYS pending -- the next sweep
retries (``close_transaction`` retires a dead close order and submits a fresh one), exactly the
backtest's "stays queued" idiom. Nothing is ever dropped silently.
"""
from typing import Any, Dict, List

from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus
from ba2_common.logger import logger

#: ``Transaction.meta_data`` key and its only value: the delivered lot awaits liquidation.
LIQUIDATION_META_KEY = "assignment_liquidation"
PENDING_META = "pending"


def pending_meta(meta_data: "dict | None" = None) -> dict:
    """``meta_data`` with the lot flagged for liquidation (a new dict, input untouched)."""
    return {**(meta_data or {}), LIQUIDATION_META_KEY: PENDING_META}


def _pending_transactions(account_id: int) -> List[Transaction]:
    with get_db() as session:
        rows = session.exec(
            select(Transaction)
            .where(Transaction.asset_class == AssetClass.EQUITY)
            .where(Transaction.side == OrderDirection.BUY)
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSING]))
        ).all()
        pending = [t for t in rows
                   if isinstance(t.meta_data, dict)
                   and t.meta_data.get(LIQUIDATION_META_KEY) == PENDING_META]
        owned = []
        for t in pending:
            acct = session.exec(
                select(TradingOrder.account_id).where(TradingOrder.transaction_id == t.id)
            ).first()
            if acct == account_id:
                owned.append(t)
        return owned


def sweep_pending_liquidations(account: Any) -> List[Dict[str, Any]]:
    """Close every pending assignment lot of ``account``; returns one result dict per lot.

    Idempotent: a lot whose close order is already live is a no-op success
    (``close_transaction`` leaves it alone), a dead one is retried, a CLOSED one leaves the
    sweep's query. Never raises: a failure is logged + recorded and the lot stays pending.
    """
    from ba2_common.core.db import log_activity
    from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
    from ba2_common.core.utils import log_close_order_activity

    results: List[Dict[str, Any]] = []
    try:
        pending = _pending_transactions(account.id)
    except Exception as e:
        logger.error(f"[Account {account.id}] assignment-liquidation sweep could not list its "
                     f"pending lots: {e}", exc_info=True)
        return results
    for txn in pending:
        try:
            res = account.close_transaction(txn.id)
        except Exception as e:
            res = {"success": False, "message": f"exception: {e}", "close_order_id": None}
            logger.error(f"[Account {account.id}] assignment liquidation of transaction "
                         f"{txn.id} ({txn.symbol} x {txn.quantity}) raised: {e}", exc_info=True)
        results.append({"transaction_id": txn.id, **res})
        if res["success"]:
            if res["close_order_id"] is not None:
                logger.warning(
                    f"[Account {account.id}] assignment_liquidation: submitted market SELL of "
                    f"{txn.quantity:g} x {txn.symbol} for transaction {txn.id} "
                    f"(close order {res['close_order_id']}); fills at the next open if the market "
                    f"is closed.")
                log_close_order_activity(
                    transaction=txn, account_id=account.id, success=True,
                    close_order_id=res["close_order_id"], quantity=float(txn.quantity),
                    side=OrderDirection.SELL)
            continue
        msg = (f"[Account {account.id}] assignment_liquidation of {txn.quantity:g} x {txn.symbol} "
               f"(transaction {txn.id}, expert {txn.expert_id}) FAILED: {res['message']} -- the "
               f"stock is still held at the broker and will be retried on the next refresh.")
        logger.error(msg)
        try:
            log_activity(severity=ActivityLogSeverity.FAILURE,
                         activity_type=ActivityLogType.TRANSACTION_CLOSED, description=msg,
                         data={"transaction_id": txn.id, "symbol": txn.symbol,
                               "error": res["message"], "assignment_liquidation": True},
                         source_account_id=account.id, source_expert_id=txn.expert_id)
        except Exception as e:
            logger.error(f"could not record the assignment-liquidation failure activity: {e}")
    return results

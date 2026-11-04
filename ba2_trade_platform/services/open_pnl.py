"""Open (unrealized) P&L of open transactions for ``GET /transactions`` and ``/transactions/{id}``.

THE PRICE SOURCE is the one the Overview and Live Trades use: the BROKER's position price for what the account
holds, the account price feed for the rest (``position_prices.held_position_prices``). The P&L of an equity
transaction is ``TransactionHelper.calculate_pnl`` (Live Trades' formula); an option structure goes through the Options
tab's rule (``FloatingPLCalculator._option_transaction_pl`` -> ``open_structure_pnl``), priced at the broker's position
marks.

ONE ``get_positions`` per account per request. A row whose price cannot be had is ``unmeasured`` with a reason -- never
a 0, and never a failure of the whole list: one broker being down marks only that account's open rows.

Pure read: the DB session is closed before the first broker call; nothing is written, synced or remembered."""
from collections import defaultdict
from typing import Any, Dict, Iterable, List

from sqlmodel import select

from ba2_common.core.TransactionHelper import TransactionHelper

from ..core.db import get_db
from ..core.models import TradingOrder, Transaction
from ..core.types import AssetClass, TransactionStatus
from ..core.utils import get_account_instance_from_id
from ..logger import logger
from .accounts import broker_read
from .errors import ServiceError
from .floating_pl import FloatingPLCalculator
from .position_prices import broker_position_prices_from, held_position_prices

#: Statuses that hold a position at the broker and so have an open P&L.
PRICED_STATUSES = (TransactionStatus.OPENED, TransactionStatus.CLOSING)


def _unmeasured(reason: str) -> Dict[str, Any]:
    return {"pnl_status": "unmeasured", "open_pnl": None, "current_price": None, "open_pnl_reason": reason}


def _account_of(session, tx_ids: List[int]) -> Dict[int, int]:
    """Transaction id -> the account of its first order (how Live Trades finds it)."""
    out: Dict[int, int] = {}
    rows = session.exec(select(TradingOrder.transaction_id, TradingOrder.account_id)
                        .where(TradingOrder.transaction_id.in_(tx_ids)).order_by(TradingOrder.id)).all()
    for tid, acc in rows:
        if acc is not None and tid not in out:
            out[tid] = acc
    return out


def open_pnl(transaction_ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
    """``{transaction_id: fields}`` for the OPENED / CLOSING transactions among ``transaction_ids`` (any other id is
    absent). ``fields``: ``pnl_status`` (``open_measured`` | ``unmeasured``), ``open_pnl`` (``{amount, percent}``,
    equity only has ``percent``), ``current_price`` and, when unmeasured, ``open_pnl_reason``."""
    ids = list(transaction_ids)
    if not ids:
        return {}
    with get_db() as session:
        txs = session.exec(select(Transaction).where(Transaction.id.in_(ids))
                           .where(Transaction.status.in_(PRICED_STATUSES))).all()
        if not txs:
            return {}
        account_of = _account_of(session, [t.id for t in txs])
        orders_of: Dict[int, List[TradingOrder]] = defaultdict(list)
        option_ids = [t.id for t in txs if t.asset_class == AssetClass.OPTION]
        if option_ids:
            for o in session.exec(select(TradingOrder).where(TradingOrder.transaction_id.in_(option_ids))
                                  .order_by(TradingOrder.created_at)).all():
                orders_of[o.transaction_id].append(o)
        # plain copies: the session is closed before any broker call, and the option rule wants real rows
        session.expunge_all()
    by_account: Dict[int, List[Transaction]] = defaultdict(list)
    out: Dict[int, Dict[str, Any]] = {}
    for tx in txs:
        if tx.id not in account_of:
            out[tx.id] = _unmeasured("the transaction has no order, so its account is unknown")
        else:
            by_account[account_of[tx.id]].append(tx)
    for account_id, account_txs in by_account.items():
        try:
            out.update(_for_account(account_id, account_txs, orders_of))
        except ServiceError as e:
            logger.warning(f"API: open P&L of account {account_id} unavailable: {e.code}: {e.message}")
            out.update({tx.id: _unmeasured(f"{e.code}: {e.message}") for tx in account_txs})
    return out


def _for_account(account_id: int, txs: List[Transaction], orders_of) -> Dict[int, Dict[str, Any]]:
    account = broker_read(account_id, "the account connection", get_account_instance_from_id, account_id)
    if account is None:
        return {tx.id: _unmeasured(f"account {account_id} cannot be instantiated") for tx in txs}
    equity = [tx for tx in txs if tx.asset_class != AssetClass.OPTION]
    options = [tx for tx in txs if tx.asset_class == AssetClass.OPTION]
    out: Dict[int, Dict[str, Any]] = {}
    if equity:
        prices = broker_read(account_id, "the position prices", held_position_prices, account, account_id,
                             sorted({tx.symbol for tx in equity}))
        for tx in equity:
            price = prices[tx.symbol] if tx.symbol in prices else None
            if price is None or price <= 0:
                out[tx.id] = _unmeasured(f"no price for {tx.symbol}")
                continue
            pnl = TransactionHelper.calculate_pnl(tx, price)
            if pnl is None:
                out[tx.id] = _unmeasured("the transaction has no open price or quantity")
                continue
            out[tx.id] = {"pnl_status": "open_measured", "open_pnl": pnl, "current_price": price}
    if options:
        positions = broker_read(account_id, "the positions fetch", account.get_positions)
        if positions is None:
            out.update({tx.id: _unmeasured("the broker positions fetch failed") for tx in options})
        else:
            marks = broker_position_prices_from(positions)
            quotes: Dict = {}
            for tx in options:
                amount = broker_read(account_id, "the option structure P&L", FloatingPLCalculator._option_transaction_pl,
                                     tx, orders_of[tx.id], account, account_id, quotes, marks)
                out[tx.id] = ({"pnl_status": "open_measured", "open_pnl": {"amount": amount}, "current_price": None}
                              if amount is not None else _unmeasured("the option structure is not priced"))
    return out

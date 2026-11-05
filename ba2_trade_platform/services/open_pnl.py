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
from ._positions_cache import read_positions, share_positions
from ..core.option_pnl_display import (
    UNAVAILABLE_FLAT, UNAVAILABLE_NO_FILLS, open_structure_pnl, quote_caching_account)
from .position_prices import broker_position_prices_from, held_position_prices

#: Statuses that hold a position at the broker and so have an open P&L.
PRICED_STATUSES = (TransactionStatus.OPENED, TransactionStatus.CLOSING)


def _unmeasured(reason: str) -> Dict[str, Any]:
    return {"pnl_status": "unmeasured", "open_pnl": None, "current_price": None, "price_source": None,
            "open_pnl_reason": reason}


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
    with share_positions():                   # one get_positions per account, kept 15 s (see _positions_cache)
        for account_id, account_txs in by_account.items():
            try:
                out.update(_for_account(account_id, account_txs, orders_of))
            except Exception as e:   # noqa: BLE001 -- deliberate: a list must not 500 because ONE broker is down or
                # answers oddly; that account's rows are marked unmeasured with the error CLASS (never its text).
                logger.warning(f"API: open P&L of account {account_id} unavailable: {type(e).__name__}: {e}")
                code = e.code if isinstance(e, ServiceError) else type(e).__name__
                out.update({tx.id: _unmeasured(f"{code}: account {account_id} could not be priced") for tx in account_txs})
    return out


def _option_amount(account, account_id: int, tx, orders, quotes, marks):
    """``(amount | None, reason | None)``. A structure with no fills or already flat is NOT a measured zero here: it
    holds nothing, so there is no open P&L to report."""
    priced = open_structure_pnl(quote_caching_account(account, quotes, account_id, marks), tx, orders)
    if priced.available:
        return priced.amount, None
    if priced.reason in (UNAVAILABLE_NO_FILLS, UNAVAILABLE_FLAT):
        return None, f"the option structure holds nothing ({priced.reason})"
    return None, f"the option structure is not priced ({priced.reason})"


def _for_account(account_id: int, txs: List[Transaction], orders_of) -> Dict[int, Dict[str, Any]]:
    account = broker_read(account_id, "the account connection", get_account_instance_from_id, account_id)
    if account is None:
        return {tx.id: _unmeasured(f"account {account_id} cannot be instantiated") for tx in txs}
    equity = [tx for tx in txs if tx.asset_class != AssetClass.OPTION]
    options = [tx for tx in txs if tx.asset_class == AssetClass.OPTION]
    positions = broker_read(account_id, "the positions fetch", read_positions, account, account_id)
    out: Dict[int, Dict[str, Any]] = {}
    if equity:
        sources: Dict[str, str] = {}
        prices = broker_read(account_id, "the position prices", held_position_prices, account, account_id,
                             sorted({tx.symbol for tx in equity}), positions, sources)
        for tx in equity:
            price = prices[tx.symbol] if tx.symbol in prices else None
            if price is None or price <= 0:
                out[tx.id] = _unmeasured(f"no price for {tx.symbol}")
                continue
            pnl = TransactionHelper.calculate_pnl(tx, price)
            if pnl is None:
                out[tx.id] = _unmeasured("the transaction has no open price or quantity")
                continue
            out[tx.id] = {"pnl_status": "open_measured", "open_pnl": pnl, "current_price": price,
                          "price_source": sources[tx.symbol]}
    if options:
        if positions is None:
            out.update({tx.id: _unmeasured("the broker positions fetch failed") for tx in options})
        else:
            marks = broker_position_prices_from(positions)
            quotes: Dict = {}
            for tx in options:
                amount, reason = broker_read(account_id, "the option structure P&L", _option_amount, account,
                                             account_id, tx, orders_of[tx.id], quotes, marks)
                out[tx.id] = ({"pnl_status": "open_measured", "open_pnl": {"amount": amount}, "current_price": None,
                               "price_source": "broker"} if amount is not None else _unmeasured(reason))
    return out

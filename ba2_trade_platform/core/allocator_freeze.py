"""Allocator freeze: the operator's per-symbol "leave this position exactly as it is" (store functions).

LIVE-ONLY and in-tree (see ``allocator_protection_models``). A frozen symbol is NOT excluded: it stays
in its labels and in the base, but a rebalance places no order for it and takes its CURRENT value out
of the money the other members share. The maths is ``portfolio_allocation.compute_allocation
(frozen_symbols=)``; ``frozen_symbols`` is the single read every consumer uses. Freezing never touches
protective TP/SL orders.
"""
from datetime import datetime as DateTime, timezone
from typing import Dict, Optional, Set

from sqlmodel import select

from ..logger import logger
from .allocator_protection_models import AllocatorFrozen
from .db import get_db


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _now() -> DateTime:
    return DateTime.now(timezone.utc).replace(tzinfo=None)


def get_frozen(account_id: int) -> Dict[str, AllocatorFrozen]:
    """``{SYMBOL: row}`` for the account. DB only; ``{}`` when nothing is frozen."""
    with get_db() as session:
        rows = list(session.exec(select(AllocatorFrozen).where(
            AllocatorFrozen.account_id == int(account_id))).all())
        session.expunge_all()
    return {r.symbol: r for r in rows}


def frozen_symbols(account_id: int) -> Set[str]:
    return set(get_frozen(account_id))


def freeze_symbol(account_id: int, symbol: str, *, note: Optional[str] = None) -> AllocatorFrozen:
    """Freeze ``symbol``. Idempotent: an already frozen symbol keeps its original ``since`` (and takes a
    new note only when one is given). Does NOT cancel or change protective TP/SL orders."""
    symbol = _norm(symbol)
    if not symbol:
        raise ValueError("a symbol is required")
    with get_db() as session:
        row = session.exec(select(AllocatorFrozen).where(
            AllocatorFrozen.account_id == int(account_id),
            AllocatorFrozen.symbol == symbol)).first()
        if row is None:
            row = AllocatorFrozen(account_id=int(account_id), symbol=symbol, since=_now(), note=note)
            session.add(row)
        elif note is not None:
            row.note = note
        session.commit()
        session.refresh(row)
        session.expunge(row)
    logger.info(f"allocator: {symbol} frozen for account {account_id}{f' ({note})' if note else ''}")
    return row


def unfreeze_symbol(account_id: int, symbol: str) -> bool:
    """Unfreeze ``symbol``. Returns False when it was not frozen."""
    symbol = _norm(symbol)
    with get_db() as session:
        row = session.exec(select(AllocatorFrozen).where(
            AllocatorFrozen.account_id == int(account_id),
            AllocatorFrozen.symbol == symbol)).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    logger.info(f"allocator: {symbol} unfrozen for account {account_id}")
    return True

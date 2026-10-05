"""Allocator exclusions and the audit trail of automatic weight changes (store functions).

LIVE-ONLY and in-tree (see ``allocator_protection_models``). Two small concerns that share a
table module because both are "decisions about a holding that the allocation maths must respect":

* EXCLUSION -- the ONE mechanism that keeps a symbol out of allocation: the operator's manual
  "disable". ``excluded_symbols`` is the single read every consumer uses (the page's label maths,
  the dry-run inputs, and the boundary in ``run_allocation``).
* WEIGHT CHANGES -- when a protective TP/SL order fills, the symbol's stored share of its label is
  reduced in proportion to the protected quantity that left; the freed share is NOT spread to the
  other symbols (it stays unallocated). ``reduce_symbol_weights`` does the write and the audit
  row in one place.
"""
from datetime import datetime as DateTime, timezone
from typing import Dict, Iterable, List, Optional, Set

from sqlmodel import select

from ..logger import logger
from .allocator_protection_models import (
    EXCLUDED_DISABLED, AllocatorExclusion, AllocatorWeightChange,
)
from .db import get_db
from .models import PortfolioAllocationSymbol


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _now() -> DateTime:
    return DateTime.now(timezone.utc).replace(tzinfo=None)


# =========================================================================================
# exclusion
# =========================================================================================

def get_exclusions(account_id: int) -> Dict[str, AllocatorExclusion]:
    """``{SYMBOL: exclusion}`` for the account. DB only; ``{}`` when nothing is excluded."""
    with get_db() as session:
        rows = list(session.exec(select(AllocatorExclusion).where(
            AllocatorExclusion.account_id == int(account_id))).all())
        session.expunge_all()
    return {r.symbol: r for r in rows}


def excluded_symbols(account_id: int) -> Set[str]:
    return set(get_exclusions(account_id))


def exclude_symbol(account_id: int, symbol: str, *, note: Optional[str] = None) -> AllocatorExclusion:
    """Exclude ``symbol`` from allocation. Idempotent: an already excluded symbol keeps its
    original ``since`` (and takes a new note only when one is given).

    Does NOT cancel or change protective TP/SL orders: they stay exactly as they are.
    """
    symbol = _norm(symbol)
    if not symbol:
        raise ValueError("a symbol is required")
    with get_db() as session:
        row = session.exec(select(AllocatorExclusion).where(
            AllocatorExclusion.account_id == int(account_id),
            AllocatorExclusion.symbol == symbol)).first()
        if row is None:
            row = AllocatorExclusion(account_id=int(account_id), symbol=symbol,
                                     excluded_reason=EXCLUDED_DISABLED, since=_now(), note=note)
            session.add(row)
        elif note is not None:
            row.note = note
        session.commit()
        session.refresh(row)
        session.expunge(row)
    logger.info(f"allocator: {symbol} excluded from allocation for account {account_id}"
                f"{f' ({note})' if note else ''}")
    return row


def include_symbol(account_id: int, symbol: str) -> bool:
    """Include ``symbol`` again. Returns False when it was not excluded."""
    symbol = _norm(symbol)
    with get_db() as session:
        row = session.exec(select(AllocatorExclusion).where(
            AllocatorExclusion.account_id == int(account_id),
            AllocatorExclusion.symbol == symbol)).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    logger.info(f"allocator: {symbol} included in allocation again for account {account_id}")
    return True


def split_included(symbols: Iterable[str], excluded: Iterable[str]) -> List[str]:
    """``symbols`` without the excluded ones, order and duplicates as given. Pure."""
    gone = {_norm(s) for s in excluded}
    return [s for s in symbols if _norm(s) not in gone]


# =========================================================================================
# automatic weight changes (protective fills)
# =========================================================================================

def reduce_symbol_weights(account_id: int, symbol: str, factor: float, *, reason: str,
                          detail: str, implicit: Optional[Dict[str, float]] = None
                          ) -> List[AllocatorWeightChange]:
    """Multiply the STORED weight of ``symbol`` by ``factor`` in every label that has one, and
    write one audit row per change. Returns the audit rows (``[]`` when no label stores a weight
    for it: a symbol on the derived default -- its actual share -- follows its holdings by itself).

    ``factor`` is ``remaining protected qty / protected qty before the fill`` in 0..1 (0 when the
    protection exited the position). The freed share is deliberately NOT given to any other
    symbol. ``previous_weight_pct`` is untouched: it is "what the last run went out with".
    """
    symbol = _norm(symbol)
    if not 0.0 <= factor <= 1.0:
        raise ValueError(f"weight factor {factor!r} is outside 0..1")
    changes: List[AllocatorWeightChange] = []
    with get_db() as session:
        rows = list(session.exec(select(PortfolioAllocationSymbol).where(
            PortfolioAllocationSymbol.account_id == int(account_id),
            PortfolioAllocationSymbol.symbol == symbol)).all())
        # A label with NO stored row for the symbol runs on the derived default (its actual share).
        # ``implicit`` carries that share per label: an EXPLICIT row is written with share x factor, so
        # the reduction applies and a stopped-out symbol is not bought back (operator, 2026-10-05).
        stored_labels = {r.label for r in rows}
        for label, share in (implicit or {}).items():
            if label not in stored_labels and factor < 1.0:
                row = PortfolioAllocationSymbol(account_id=int(account_id), label=label,
                                                symbol=symbol, weight_pct=float(share))
                session.add(row)
                rows.append(row)
        for row in rows:
            before = float(row.weight_pct or 0.0)
            after = round(before * factor, 6)
            if abs(after - before) < 1e-9:
                continue
            row.weight_pct = after
            change = AllocatorWeightChange(
                account_id=int(account_id), label=row.label, symbol=symbol, reason=reason,
                before_pct=before, after_pct=after, detail=detail, created_at=_now())
            session.add(change)
            changes.append(change)
        session.commit()
        for change in changes:
            session.refresh(change)
        session.expunge_all()
    for change in changes:
        logger.warning(f"allocator: {symbol} weight in '{change.label}' {change.before_pct:g}% -> "
                       f"{change.after_pct:g}% ({reason}: {detail}); the freed share stays unallocated")
    return changes


def get_weight_changes(account_id: int, *, symbol: Optional[str] = None,
                       label: Optional[str] = None) -> List[AllocatorWeightChange]:
    """The audit trail, newest first."""
    with get_db() as session:
        statement = select(AllocatorWeightChange).where(
            AllocatorWeightChange.account_id == int(account_id))
        if symbol:
            statement = statement.where(AllocatorWeightChange.symbol == _norm(symbol))
        if label:
            statement = statement.where(AllocatorWeightChange.label == label)
        rows = list(session.exec(statement.order_by(
            AllocatorWeightChange.created_at.desc(), AllocatorWeightChange.id.desc())).all())
        session.expunge_all()
    return rows


def latest_weight_change_by_symbol(account_id: int) -> Dict[str, AllocatorWeightChange]:
    """``{SYMBOL: its most recent automatic weight change}`` -- the row's small note."""
    out: Dict[str, AllocatorWeightChange] = {}
    for change in get_weight_changes(account_id):
        out.setdefault(change.symbol, change)
    return out


def labels_with_weight_changes(account_id: int) -> Set[str]:
    """Labels that have ever had an automatic weight change: only for them is a total below 100%
    reported as "freed from TP/SL fills" (a hand-typed 60% is not)."""
    return {c.label for c in get_weight_changes(account_id)}

"""Portfolio allocation persistence: every read and write of the five allocation tables.

Pure DB code -- it never talks to a broker and never touches NiceGUI. The only
thing it borrows from the allocation ENGINE
(``ba2_common.core.portfolio_allocation``) is the two ``VALUATION_MODE_*``
constants, so that the page, the store and the engine cannot disagree on the
spelling of a mode. The UI calls these helpers; the engine receives the plain
values they produce.

Two rules the callers depend on:

* A ``portfolio_allocation_label`` row's EXISTENCE is the "this label is managed"
  flag -- deleting the row unmanages the label.
* ``portfolio_allocation_symbol`` rows are created LAZILY. A symbol with no row
  takes the even-split default, so ``get_symbol_weights()`` returns a computed
  weight for every symbol you ask about and never an empty dict.
"""
from __future__ import annotations

from datetime import date as Date, datetime as DateTime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)
from ba2_common.logger import logger


# ---------------------------------------------------------------------------
# Managed labels
# ---------------------------------------------------------------------------

def get_managed_labels(account_id: int) -> List[PortfolioAllocationLabel]:
    """Every managed label of an account, in display order (sort_order, then name).

    Returns ``[]`` when the account manages nothing -- a legitimate empty state
    (nothing configured yet), not an error.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
            .order_by(PortfolioAllocationLabel.sort_order, PortfolioAllocationLabel.label)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows


def set_managed_label(account_id: int, label: str, *,
                      target_pct: Optional[float] = None,
                      sort_order: Optional[int] = None,
                      comment: Optional[str] = None) -> PortfolioAllocationLabel:
    """Create the managed-label row, or update only the fields you pass.

    ``None`` for a field means LEAVE IT UNCHANGED, so the page can save a comment
    without disturbing the percentage. Pass ``""`` to clear a comment.

    Raises:
        ValueError: when ``label`` is blank -- a nameless managed label is
        unreachable from the UI and would collide with the next blank one.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("set_managed_label requires a non-empty label")
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        if row is None:
            row = PortfolioAllocationLabel(account_id=account_id, label=label)
            session.add(row)
        if target_pct is not None:
            row.target_pct = float(target_pct)
        if sort_order is not None:
            row.sort_order = int(sort_order)
        if comment is not None:
            row.comment = comment
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove_managed_label(account_id: int, label: str) -> bool:
    """Unmanage a label: delete its row AND every symbol-weight row underneath it.

    Returns True when a label row was deleted, False when the label was not
    managed in the first place.
    """
    label = (label or "").strip()
    if not label:
        return False
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        symbol_rows = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
            )
        ).all()
        found = row is not None
        removed_symbols = len(symbol_rows)
        for symbol_row in symbol_rows:
            session.delete(symbol_row)
        if row is not None:
            session.delete(row)
        session.commit()
    if not found:
        return False
    logger.info(f"Unmanaged allocation label '{label}' for account {account_id} "
                f"({removed_symbols} symbol weight row(s) removed)")
    return True

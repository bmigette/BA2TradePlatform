"""The allocator's "rebalance due" reminder: its per-account interval and the last completed rebalance run (store).

LIVE-ONLY and in-tree (see ``allocator_protection_models``). The due decision itself is the pure
``ui/utils/rebalance_reminder.py``. Nothing here places, schedules or pre-accepts an order: the reminder points at
the page's existing Review and Submit flow.

"LAST COMPLETED REBALANCE RUN" = the newest ``PortfolioAllocationRun`` with ``mode == REBALANCE`` that created at
least one order (``order_ids`` non-empty). A run row is written at SUBMIT time, before any order, so a row without an
order id is a submission that never got an order out; an INVEST_LABEL run spends on one label and is not a rebalance.
A run whose orders are still working (income not yet consumed) DOES count: its orders went out when it was submitted.
"""
from datetime import datetime as DateTime, timezone
from typing import Optional

from sqlmodel import select

from ..logger import logger
from .allocator_protection_models import AllocatorRebalanceReminder
from .db import get_db
from .models import PortfolioAllocationRun
from .portfolio_allocation import ALLOCATION_MODE_REBALANCE

#: The interval when the operator never set one (the owner's choice: a quarterly reminder).
DEFAULT_REMINDER_MONTHS = 3
MAX_REMINDER_MONTHS = 36


def validate_reminder_months(months) -> int:
    """``months`` as an int in 1..MAX_REMINDER_MONTHS; ``ValueError`` (with the reason) otherwise."""
    if isinstance(months, bool) or months is None:
        raise ValueError(f"The reminder interval must be a whole number of months, not {months!r}")
    try:
        as_float = float(months)
    except (TypeError, ValueError):
        raise ValueError(f"The reminder interval must be a whole number of months, not {months!r}") from None
    if not as_float.is_integer() or not 1 <= as_float <= MAX_REMINDER_MONTHS:
        raise ValueError(f"The reminder interval must be a whole number of months from 1 to {MAX_REMINDER_MONTHS}, "
                         f"not {months!r}")
    return int(as_float)


def get_reminder_months(account_id: int) -> int:
    """The account's interval; ``DEFAULT_REMINDER_MONTHS`` when it never set one. DB read only, never creates a row."""
    with get_db() as session:
        row = session.exec(select(AllocatorRebalanceReminder).where(
            AllocatorRebalanceReminder.account_id == int(account_id))).first()
        return DEFAULT_REMINDER_MONTHS if row is None else int(row.interval_months)


def set_reminder_months(account_id: int, months) -> int:
    """Store the account's interval (validated first: nothing invalid is ever written). Returns the stored value."""
    value = validate_reminder_months(months)
    with get_db() as session:
        row = session.exec(select(AllocatorRebalanceReminder).where(
            AllocatorRebalanceReminder.account_id == int(account_id))).first()
        if row is None:
            row = AllocatorRebalanceReminder(account_id=int(account_id), interval_months=value)
        else:
            row.interval_months = value
            row.updated_at = DateTime.now(timezone.utc)
        session.add(row)
        session.commit()
    logger.info(f"allocator: rebalance reminder for account {account_id} set to every {value} month(s)")
    return value


def last_rebalance_run_at(account_id: int) -> Optional[DateTime]:
    """``created_at`` (UTC) of the account's last completed rebalance run (see the module docstring); ``None`` when
    there is none. Read-only."""
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationRun.created_at, PortfolioAllocationRun.order_ids)
            .where(PortfolioAllocationRun.account_id == int(account_id),
                   PortfolioAllocationRun.mode == ALLOCATION_MODE_REBALANCE)
            .order_by(PortfolioAllocationRun.created_at.desc(), PortfolioAllocationRun.id.desc())
        ).all()
    for created_at, order_ids in rows:
        if order_ids:
            return created_at
    return None

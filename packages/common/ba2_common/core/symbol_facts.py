"""Persist and read back the broker's per-symbol facts for one account.

``MarginInfo`` is what the broker says; ``AccountSymbolFacts`` is that answer stored
so the UI can show it without a REST round-trip on every page load. This module is the
only translation between the two, so the tri-state contract is enforced in one place.

THE TRI-STATE RULE, restated because it is what this module exists to protect: every
flag is ``True`` / ``False`` / ``None``, where ``None`` means "the broker did not say".
A MISSING ROW carries the same meaning as a row of ``None``s, so a reader never has to
distinguish "never fetched" from "fetched and unknown" -- both are absence of an answer,
and neither may be read as a refusal. Nothing here coerces with ``bool()``.
"""
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from sqlmodel import select

from ba2_common.core.account_types import MarginInfo, leverage_of  # noqa: F401 — re-exported
from ba2_common.core.db import get_db
from ba2_common.core.models import AccountSymbolFacts
from ba2_common.logger import logger

#: Fields copied verbatim between MarginInfo and AccountSymbolFacts. Listed once so a
#: field added to one side and forgotten on the other is a visible omission here rather
#: than a value that silently stops being stored.
_CARRIED = (
    'fractionable', 'marginable', 'tradable', 'bp_factor',
    'initial_margin_rate', 'maintenance_margin_rate',
    'min_order_size', 'min_trade_increment', 'min_fractional_notional', 'source',
)


def save_symbol_facts(account_id: int, info: Dict[str, MarginInfo]) -> int:
    """Upsert one broker answer per symbol. Returns the number of rows written.

    Symbols ABSENT from ``info`` are left alone rather than deleted: the broker
    omitting a symbol from one basket means "no answer this time", and wiping the last
    known answer would turn a transient lookup failure into permanent unknown.
    """
    if not info:
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    try:
        with get_db() as session:
            existing = {
                row.symbol: row
                for row in session.exec(
                    select(AccountSymbolFacts).where(AccountSymbolFacts.account_id == account_id)
                ).all()
            }
            for raw_symbol, m in info.items():
                symbol = (raw_symbol or '').strip().upper()
                if not symbol or m is None:
                    continue
                row = existing.get(symbol) or AccountSymbolFacts(account_id=account_id, symbol=symbol)
                for name in _CARRIED:
                    setattr(row, name, getattr(m, name, None))
                row.fetched_at = now
                session.add(row)
                written += 1
            session.commit()
    except Exception as e:  # noqa: BLE001 -- caching must never break the caller
        logger.error(f"Saving symbol facts for account {account_id} failed: {e}", exc_info=True)
        return 0
    return written


def load_symbol_facts(account_id: int,
                      symbols: Optional[Iterable[str]] = None) -> Dict[str, AccountSymbolFacts]:
    """``{symbol: row}`` for this account. ``symbols=None`` loads every stored row.

    A symbol with no row is simply missing from the dict -- see the tri-state rule
    above. Detached from the session so callers can read it after the block closes.
    """
    wanted: Optional[List[str]] = None
    if symbols is not None:
        wanted = sorted({(s or '').strip().upper() for s in symbols if (s or '').strip()})
        if not wanted:
            return {}
    try:
        with get_db() as session:
            stmt = select(AccountSymbolFacts).where(AccountSymbolFacts.account_id == account_id)
            if wanted is not None:
                stmt = stmt.where(AccountSymbolFacts.symbol.in_(wanted))  # type: ignore[attr-defined]
            rows = session.exec(stmt).all()
            for row in rows:
                session.expunge(row)
            return {row.symbol: row for row in rows}
    except Exception as e:  # noqa: BLE001 -- a cache read must never break the page
        logger.error(f"Loading symbol facts for account {account_id} failed: {e}", exc_info=True)
        return {}

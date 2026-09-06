"""Read and write the cached per-symbol market stats (yield, 1Y/3Y total return).

The provider (`ba2_providers.symbol_info`) is the source; this is the durable cache in
front of it, so a page render costs no REST call and a process restart does not throw
the answers away.

NULL MEANS UNKNOWN, NEVER ZERO. A fund that pays no dividend has
``dividend_yield_pct = 0.0`` -- a measured fact. A symbol whose fetch failed has
``None`` and an ``error``. Collapsing the two would render every unfetched symbol as a
non-payer, which is the one mistake this whole panel's design exists to prevent.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import SymbolMarketStats
from ba2_common.logger import logger

#: How old a row may be before a refresh bothers to re-ask. A dividend yield and a
#: trailing return move on the scale of days, not minutes, and the provider's own
#: cache is 24h -- so re-fetching more often than this buys nothing and spends the
#: FMP rate limit that the live platform's other callers share.
STALE_AFTER = timedelta(hours=12)


def load_symbol_stats(symbols: Optional[Iterable[str]] = None) -> Dict[str, SymbolMarketStats]:
    """``{symbol: row}``. ``symbols=None`` loads every stored row.

    A symbol with no row is simply missing from the dict -- "never fetched" and
    "fetched and unknown" are both absence of an answer to the caller.
    """
    wanted: Optional[List[str]] = None
    if symbols is not None:
        wanted = sorted({(s or '').strip().upper() for s in symbols if (s or '').strip()})
        if not wanted:
            return {}
    try:
        with get_db() as session:
            stmt = select(SymbolMarketStats)
            if wanted is not None:
                stmt = stmt.where(SymbolMarketStats.symbol.in_(wanted))  # type: ignore[attr-defined]
            rows = session.exec(stmt).all()
            for row in rows:
                session.expunge(row)
            return {row.symbol: row for row in rows}
    except Exception as e:  # noqa: BLE001 -- a cache read must never break a page
        logger.error(f"Loading symbol market stats failed: {e}", exc_info=True)
        return {}


def stale_symbols(symbols: Iterable[str], *, now: Optional[datetime] = None) -> List[str]:
    """Which of ``symbols`` are missing or older than :data:`STALE_AFTER`.

    Returned in a stable sorted order so a caller that refreshes in batches makes the
    same progress each run instead of re-shuffling the queue.
    """
    now = now or datetime.now(timezone.utc)
    known = load_symbol_stats(symbols)
    out: List[str] = []
    for raw in symbols:
        symbol = (raw or '').strip().upper()
        if not symbol:
            continue
        row = known.get(symbol)
        if row is None:
            out.append(symbol)
            continue
        fetched = row.fetched_at
        # A naive timestamp came from SQLite, which stores no zone; it was written as
        # UTC, so read it back as UTC rather than as local time.
        if fetched is not None and fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if fetched is None or (now - fetched) > STALE_AFTER:
            out.append(symbol)
    return sorted(set(out))


def save_symbol_stats(rows: Dict[str, Dict]) -> int:
    """Upsert ``{symbol: {dividend_yield_pct, total_return_1y_pct, ...}}``.

    A symbol absent from ``rows`` is left alone: the caller not asking about it is not
    evidence about it. Returns the number of rows written.
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    try:
        with get_db() as session:
            existing = {r.symbol: r for r in session.exec(select(SymbolMarketStats)).all()}
            for raw_symbol, values in rows.items():
                symbol = (raw_symbol or '').strip().upper()
                if not symbol:
                    continue
                row = existing.get(symbol) or SymbolMarketStats(symbol=symbol)
                row.dividend_yield_pct = values.get('dividend_yield_pct')
                row.total_return_1y_pct = values.get('total_return_1y_pct')
                row.total_return_3y_pct = values.get('total_return_3y_pct')
                row.company_name = values.get('company_name')
                row.error = values.get('error')
                row.fetched_at = now
                session.add(row)
                written += 1
            session.commit()
    except Exception as e:  # noqa: BLE001 -- caching must never break the caller
        logger.error(f"Saving symbol market stats failed: {e}", exc_info=True)
        return 0
    return written

"""Survivorship-free historical universe construction for the as-of screener.

Two modes, both terminating in a plain ``list[str]`` of symbols tradable on a date:
  - broad: ``available-traded/list`` UNION ``delisted-companies``, filtered per date
           by the symbol's ``[ipoDate, delistedDate]`` lifecycle window.
  - index-scoped (``sp500`` / ``nasdaq``): dated constituents replayed from the
           historical add/remove change log walked backward from today to the as-of
           date.

All fetches go through :func:`ba2_providers.fmp_common.fmp_http_get` (retry + FMP
200-status-error-dict handling). The full lists are bounded and small (~10-12k
symbols, ~tens of change events) so we fetch once and slice in memory — the universe
is computed once per backtest range.

This is the input the historical screener (:class:`FMPHistoricalScreenerProvider`)
reconstructs as-of metrics for. The FMP screener endpoint itself is point-in-time
(current listings only) — this module is what makes the backtest universe
survivorship-free (FMP_BACKTEST_FEASIBILITY.md survivorship warning #1).

FMP field names are taken from the documented endpoint shapes (and the Task-2 Step-1
live probe where available); ``_field`` helpers fall back across the documented
aliases so a minor FMP rename does not break reconstruction:
  - ``available-traded/list``  -> symbol, name, exchange, ipoDate (ipoDate may be
        absent on active rows -> ipo treated as -inf so the symbol is always eligible).
  - ``delisted-companies`` (paginated) -> symbol, companyName, exchange, ipoDate,
        delistedDate.
  - ``{index}_constituent`` (current)  -> symbol.
  - ``historical/{index}_constituent`` (change log, newest-first) -> date (or
        dateAdded), symbol || addedSecurity (the added ticker), removedTicker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from ba2_common.config import get_app_setting
from ba2_common.logger import logger

# Module-level import so tests can monkeypatch ``universe.fmp_http_get`` and so the
# retry/200-error-dict handling is applied to every universe fetch.
from ba2_providers.fmp_common import fmp_http_get, FMPError  # noqa: F401  (FMPError re-exported)

_BASE = "https://financialmodelingprep.com/api/v3"

# FMP delisted-companies page size; a short final page signals the last page.
_DELISTED_PAGE_SIZE = 100


def _api_key() -> str:
    k = get_app_setting("FMP_API_KEY")
    if not k:
        raise ValueError(
            "FMP_API_KEY not configured — required for universe reconstruction"
        )
    return k


def _as_utc(dt: datetime) -> datetime:
    """Coerce ``dt`` to an aware UTC datetime so it compares against the aware-UTC
    dates ``_parse_date`` produces. A naive ``as_of`` (the common caller shape — the
    backtest / CLI pass naive datetimes) is assumed to already be UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    """Parse an FMP ``YYYY-MM-DD`` (or longer) date string to an aware UTC datetime.

    Returns ``None`` for empty / unparseable input so callers can treat a missing
    ipoDate as ``-inf`` (always eligible) and a missing delistedDate as ``+inf``
    (still active).
    """
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_lifecycle_map() -> Dict[str, Tuple[Optional[datetime], Optional[datetime]]]:
    """Return ``{symbol: (ipo_date, delisted_date)}`` merging active + delisted lists.

    Active symbols have ``delisted_date=None`` (treated as +inf). Delisted symbols
    carry both an ipoDate and a delistedDate. Field names per the documented FMP
    shapes: active list ``symbol``/``ipoDate``; delisted list
    ``symbol``/``ipoDate``/``delistedDate``.

    The delisted list takes precedence over the active list on symbol collisions so
    a re-listed-then-delisted ticker keeps its delistedDate window.
    """
    key = _api_key()
    lifecycle: Dict[str, Tuple[Optional[datetime], Optional[datetime]]] = {}

    # Active (currently traded) — delisted_date = None => still active (+inf).
    resp = fmp_http_get(
        f"{_BASE}/available-traded/list",
        params={"apikey": key},
        endpoint="available-traded",
        timeout=30,
    )
    for row in (resp.json() or []):
        if isinstance(row, dict) and row.get("symbol"):
            lifecycle[str(row["symbol"]).upper()] = (_parse_date(row.get("ipoDate")), None)

    # Delisted (paginated, newest pages first). Overwrites active entries so a
    # symbol present in both lists keeps its delistedDate window.
    page = 0
    while True:
        r = fmp_http_get(
            f"{_BASE}/delisted-companies",
            params={"apikey": key, "page": page},
            endpoint="delisted-companies",
            timeout=30,
        )
        rows = r.json() or []
        if not rows or not isinstance(rows, list):
            break
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                lifecycle[str(row["symbol"]).upper()] = (
                    _parse_date(row.get("ipoDate")),
                    _parse_date(row.get("delistedDate")),
                )
        if len(rows) < _DELISTED_PAGE_SIZE:  # short page => last page reached
            break
        page += 1

    logger.info(f"universe: lifecycle map built for {len(lifecycle)} symbols")
    return lifecycle


def broad_universe(
    as_of: datetime,
    lifecycle: Optional[Dict[str, Tuple[Optional[datetime], Optional[datetime]]]] = None,
) -> List[str]:
    """Symbols tradable on ``as_of``: ``ipoDate <= as_of <= (delistedDate or +inf)``.

    A symbol with no ipoDate is treated as having always existed (eligible). A symbol
    with no delistedDate is treated as still active (eligible through the present).
    This is the survivorship-free set: a name that delisted *after* ``as_of`` but
    *before* today is still present for ``as_of``.
    """
    as_of = _as_utc(as_of)
    lifecycle = lifecycle if lifecycle is not None else fetch_lifecycle_map()
    out: List[str] = []
    for sym, (ipo, delisted) in lifecycle.items():
        if ipo is not None and ipo > as_of:
            continue  # not yet public on as_of
        if delisted is not None and delisted < as_of:
            continue  # already delisted before as_of
        out.append(sym)
    out.sort()
    logger.info(f"universe(broad): {len(out)} symbols tradable on {as_of.date()}")
    return out


def index_universe(index: str, as_of: datetime) -> List[str]:
    """Reconstruct dated index membership by replaying the historical change log.

    ``index``: ``'sp500'`` or ``'nasdaq'``. Start from the CURRENT constituent set,
    then walk the dated add/remove events backward from today to ``as_of``, inverting
    each event that happened strictly AFTER ``as_of``:
      - an added ticker after ``as_of`` -> the symbol was NOT a member on ``as_of`` -> remove
      - a removed ticker after ``as_of`` -> the symbol WAS a member on ``as_of``   -> add back

    Events on or before ``as_of`` are already reflected in the membership as of
    ``as_of`` and are left untouched. Field names per the documented FMP shapes
    (``date``/``dateAdded``, ``symbol``/``addedSecurity``, ``removedTicker``).
    """
    if index not in ("sp500", "nasdaq"):
        raise ValueError(f"index_universe: unsupported index {index!r} (use 'sp500' or 'nasdaq')")

    as_of = _as_utc(as_of)
    key = _api_key()
    cur_url = f"{_BASE}/{index}_constituent"
    hist_url = f"{_BASE}/historical/{index}_constituent"

    current = {
        str(row["symbol"]).upper()
        for row in (
            fmp_http_get(
                cur_url,
                params={"apikey": key},
                endpoint=f"{index}_constituent",
                timeout=30,
            ).json()
            or []
        )
        if isinstance(row, dict) and row.get("symbol")
    }

    members: Set[str] = set(current)
    changes = (
        fmp_http_get(
            hist_url,
            params={"apikey": key},
            endpoint=f"{index}_historical",
            timeout=30,
        ).json()
        or []
    )
    for ev in changes:  # FMP returns newest-first; order is irrelevant to the replay
        if not isinstance(ev, dict):
            continue
        ev_date = _parse_date(ev.get("date") or ev.get("dateAdded"))
        if ev_date is None or ev_date <= as_of:
            continue  # only invert events strictly AFTER as_of
        added = str(ev.get("symbol") or ev.get("addedSecurity") or "").upper()
        removed = str(ev.get("removedTicker") or "").upper()
        if added:
            members.discard(added)  # added after as_of -> not a member then
        if removed:
            members.add(removed)  # removed after as_of -> a member then
    logger.info(f"universe({index}): {len(members)} members on {as_of.date()}")
    return sorted(members)

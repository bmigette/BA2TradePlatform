"""Cache-management API. Brand-new router.

No /api/cache existed before this phase (providers.router is commented out in
app/main.py). This router exposes per-type disk usage + drill-down over every
cache root tracked by app.services.cache_manager, plus deletion endpoints
(clean-all / by-type / by-date) that are .tmp-aware and lock-safe.

DESTRUCTIVE guard: ``DELETE /api/cache`` (clean-all) skips dataset CSVs +
trained_models; those clear only via an explicit ``DELETE /api/cache/datasets``
or ``DELETE /api/cache/models``.

``/options/*`` (the option-chain viewer) is the one READ-ONLY family here. It never
deletes and never writes: see app.services.option_cache_reader for the read-only sqlite
handles and the availability contract. It is mounted on this router rather than a new one
because it is the same cache surface the rest of this file manages, and the frontend
already talks to ``${API_BASE}/cache``. The declared ordering below matters: FastAPI
matches in registration order, so every ``/options/...`` GET is declared before the
``DELETE /{cache_type}`` catch-all, and the GET/DELETE method split keeps them disjoint
regardless.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import cache_manager
from app.services import option_cache_reader as ocr

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_before(before: Optional[str]) -> Optional[datetime]:
    """Parse an optional YYYY-MM-DD cutoff into a UTC datetime, else 400."""
    if not before:
        return None
    try:
        return datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="before must be YYYY-MM-DD")


@router.get("/usage")
def cache_usage():
    """Per-type disk usage (bytes, file count, oldest/newest mtime, destructive flag, TTL).

    Plain ``def``, not ``async def`` -- deliberately, and load-bearing. ``get_usage()``
    is a synchronous ``Path.rglob("*")`` + ``stat()`` walk over every tracked cache root,
    including the as_of provider caches and the options store, which together can hold
    six-figure file counts. An ``async def`` handler runs directly on uvicorn's single
    event-loop thread with no implicit offload, so that walk — worse under the disk
    contention a live GA grid run adds — blocked EVERY request on the whole API, not just
    this one: /health included, for minutes, until the process was killed (2026-08-27).
    A plain ``def`` is what FastAPI/Starlette route to its worker threadpool automatically
    (see ``list_screener_stores`` in api/backtests.py for the same idiom already in use),
    so a slow scan now only occupies its own thread. This does not make the scan fast; it
    stops one slow scan from taking the rest of the platform down with it.
    """
    return {"types": cache_manager.get_usage()}


@router.get("/usage/{cache_type}")
def cache_drill_down(cache_type: str):
    """Per-item breakdown for one cache type."""
    try:
        return {"type": cache_type, "items": cache_manager.drill_down(cache_type)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cache type: {cache_type}")


# ---------------------------------------------------------------------------
# Option-chain viewer (READ-ONLY). Declared before the /{cache_type} catch-all.
# ---------------------------------------------------------------------------
@router.get("/options/stores")
def option_stores():
    """Which option stores exist, and what each can honestly report.

    A store that is absent (the TastyTrade parquet store usually is — the download runs
    on another machine) is reported with ``present: false`` and a reason, not omitted and
    not faked.
    """
    return {"stores": ocr.stores()}


@router.get("/options/symbols")
def option_symbols(
    q: str = Query(..., min_length=1, description="Symbol prefix, e.g. 'AAP'"),
    limit: int = Query(50, ge=1, le=500),
):
    """Prefix search. A prefix is REQUIRED: enumerating every underlying in the 63 M-row
    bar table is a ~1.4 s full index scan, while a prefix range seek is single-digit ms."""
    return ocr.search_symbols(q, limit=limit)


@router.get("/options/dates")
def option_dates(symbol: str = Query(..., min_length=1)):
    """The as-of dates that actually hold rows for this symbol, per store.

    The picker is populated from this. The legacy chain table holds three snapshot dates
    in the entire file, so a free calendar over it would miss on almost every click.
    """
    return ocr.available_dates(symbol)


@router.get("/options/chain")
def option_chain(
    symbol: str = Query(..., min_length=1),
    as_of: str = Query(..., description="YYYY-MM-DD, from /options/dates"),
    store: str = Query(..., description="alpaca-chain | alpaca-bars | tastytrade-parquet"),
    spot: Optional[float] = Query(None, gt=0, description="Underlying price, for computed greeks"),
    rate: float = Query(0.0, ge=-0.1, le=1.0, description="Risk-free rate for the greek model"),
    dividend_yield: float = Query(0.0, ge=0.0, le=1.0),
):
    """One chain: expiries as groups, strikes down the middle, calls and puts either side.

    404 when the symbol, the date or the store has nothing — with a message naming what
    IS available, because "empty" and "you asked for the wrong date" look identical
    otherwise.
    """
    try:
        return ocr.chain(symbol, as_of, store, spot=spot, rate=rate,
                         dividend_yield=dividend_yield)
    except ocr.ChainUnavailable as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.delete("")
def clear_all_caches(
    before: Optional[str] = Query(None, description="Only delete entries older than YYYY-MM-DD"),
):
    """Clean all NON-destructive cache types. datasets + trained_models are excluded."""
    return cache_manager.clear_all(before=_parse_before(before))


@router.delete("/{cache_type}")
def clear_cache_type(
    cache_type: str,
    before: Optional[str] = Query(None, description="Only delete entries older than YYYY-MM-DD"),
    symbol: Optional[str] = Query(None),
    interval: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    ticker: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
):
    """Clean one cache type (incl. the destructive datasets/models when named explicitly),
    optionally filtered by date and granular keys."""
    try:
        return cache_manager.clear_type(
            cache_type,
            before=_parse_before(before),
            symbol=symbol,
            interval=interval,
            provider=provider,
            ticker=ticker,
            task_id=task_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cache type: {cache_type}")

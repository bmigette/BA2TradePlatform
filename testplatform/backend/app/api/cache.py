"""Cache-management API. Brand-new router.

No /api/cache existed before this phase (providers.router is commented out in
app/main.py). This router exposes per-type disk usage + drill-down over every
cache root tracked by app.services.cache_manager, plus deletion endpoints
(clean-all / by-type / by-date) that are .tmp-aware and lock-safe.

DESTRUCTIVE guard: ``DELETE /api/cache`` (clean-all) skips dataset CSVs +
trained_models; those clear only via an explicit ``DELETE /api/cache/datasets``
or ``DELETE /api/cache/models``.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import cache_manager

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
async def cache_usage():
    """Per-type disk usage (bytes, file count, oldest/newest mtime, destructive flag, TTL)."""
    return {"types": cache_manager.get_usage()}


@router.get("/usage/{cache_type}")
async def cache_drill_down(cache_type: str):
    """Per-item breakdown for one cache type."""
    try:
        return {"type": cache_type, "items": cache_manager.drill_down(cache_type)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown cache type: {cache_type}")


@router.delete("")
async def clear_all_caches(
    before: Optional[str] = Query(None, description="Only delete entries older than YYYY-MM-DD"),
):
    """Clean all NON-destructive cache types. datasets + trained_models are excluded."""
    return cache_manager.clear_all(before=_parse_before(before))


@router.delete("/{cache_type}")
async def clear_cache_type(
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

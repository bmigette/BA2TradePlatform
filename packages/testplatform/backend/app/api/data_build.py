"""Data-build API endpoints.

Background, task-queue-backed endpoints that let the React UI drive the same data-build commands
the ``ba2-test`` CLI exposes (ba2test_launcher). Each endpoint validates fail-early, enqueues a
task, and returns immediately with ``{task_id}`` (or a list for the multi-symbol OHLCV build) —
it NEVER blocks the request. Poll progress/status via ``GET /api/tasks/{task_id}``.

Routing:
  * build-ohlcv          -> ``ohlcv_cache_fetch`` tasks on the dedicated OHLCV queue (one task
                            PER symbol, mirroring the CLI's per-symbol loop; that queue already
                            has its handler registered in main.py).
  * build-screener-metrics / build-options / prewarm -> their handlers on the MAIN task queue
    (registered in main.py); these are single tasks.
"""
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.task_queue import get_task_queue, get_ohlcv_task_queue

logger = logging.getLogger(__name__)

router = APIRouter()


class BuildOhlcvRequest(BaseModel):
    """Build/extend the per-symbol OHLCV cache (mirrors CLI fetch-cache).

    One ``ohlcv_cache_fetch`` task is enqueued per symbol on the OHLCV queue."""
    symbols: List[str]
    timeframes: List[str]
    start: str                               # ISO start date
    end: str                                 # ISO end date
    provider: Optional[str] = "fmp"          # ohlcv provider (default fmp)
    workers: Optional[int] = 5               # per-task executor_workers


class BuildScreenerMetricsRequest(BaseModel):
    """Build/extend the screener METRIC store (parquet) — mirrors CLI build-screener-metrics."""
    store: Optional[str] = None              # parquet metric-store dir (default: ba2_common SCREENER_STORE_DIR)
    start: str
    end: str
    market_cap_min: float                    # LOOSEST cap bound (shortlist superset)
    price_min: Optional[float] = 0.0
    volume_min: Optional[float] = 0.0
    cadence_days: Optional[int] = 7          # scan cadence in days (default 7 = weekly)
    drop_days: Optional[int] = 1


class BuildOptionsRequest(BaseModel):
    """Build the offline options cache from Alpaca — mirrors CLI fetch-options."""
    underlyings: List[str]
    start: str                               # ISO (>= 2024-02-01)
    end: str
    cache_db: Optional[str] = None           # options-history SQLite cache (default: ba2_common OPTIONS_CACHE_DB)
    feed: Optional[str] = "indicative"


class PrewarmRequest(BaseModel):
    """Pre-build the per-symbol FMP-history disk cache — mirrors CLI prewarm."""
    symbols: List[str]
    experts: Optional[List[str]] = None      # default: the 3 disk-cached history experts
    workers: Optional[int] = 5
    end: Optional[str] = None                # ISO end date (default now)


@router.post("/build-ohlcv")
async def build_ohlcv(req: BuildOhlcvRequest):
    """Enqueue one ``ohlcv_cache_fetch`` task per symbol on the OHLCV queue.

    Returns the list of created task ids (one per symbol)."""
    symbols = [s.strip().upper() for s in (req.symbols or []) if s and s.strip()]
    timeframes = [t.strip() for t in (req.timeframes or []) if t and t.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols must be non-empty")
    if not timeframes:
        raise HTTPException(status_code=400, detail="timeframes must be non-empty")

    queue = get_ohlcv_task_queue()
    tasks = []
    for sym in symbols:
        task_id = queue.queue_task(
            task_type="ohlcv_cache_fetch",
            name=f"OHLCV cache: {sym}",
            payload={
                "provider": req.provider or "fmp",
                "symbol": sym,
                "timeframes": timeframes,
                "start_date": req.start,
                "end_date": req.end,
                "executor_workers": int(req.workers or 5),
            },
            description=f"Build OHLCV cache for {sym} ({', '.join(timeframes)})",
        )
        tasks.append({"symbol": sym, "task_id": task_id})

    logger.info(f"build-ohlcv enqueued {len(tasks)} OHLCV cache tasks")
    return {"tasks": tasks, "count": len(tasks)}


@router.post("/build-screener-metrics")
async def build_screener_metrics(req: BuildScreenerMetricsRequest):
    """Enqueue a ``build_screener_metrics`` task on the main queue. Returns {task_id}.

    ``store`` defaults to the shared ba2_common screener store dir (trade bucket)
    when omitted — nothing is cached inside the repo."""
    from ba2_common.config import SCREENER_STORE_DIR
    store = req.store or SCREENER_STORE_DIR
    task_id = get_task_queue().queue_task(
        task_type="build_screener_metrics",
        name=f"Build screener metrics: {store}",
        payload={
            "store": store,
            "start": req.start,
            "end": req.end,
            "market_cap_min": req.market_cap_min,
            "price_min": req.price_min if req.price_min is not None else 0.0,
            "volume_min": req.volume_min if req.volume_min is not None else 0.0,
            "cadence_days": req.cadence_days if req.cadence_days is not None else 7,
            "drop_days": req.drop_days if req.drop_days is not None else 1,
        },
        description=f"Build screener metric store {store} ({req.start}..{req.end})",
        timeout_seconds=24 * 3600,  # store builds can take many minutes
    )
    logger.info(f"build-screener-metrics enqueued task {task_id}")
    return {"task_id": task_id}


@router.post("/build-options")
async def build_options(req: BuildOptionsRequest):
    """Enqueue a ``build_options`` task on the main queue. Returns {task_id}."""
    underlyings = [s.strip().upper() for s in (req.underlyings or []) if s and s.strip()]
    if not underlyings:
        raise HTTPException(status_code=400, detail="underlyings must be non-empty")
    from ba2_common.config import OPTIONS_CACHE_DB
    cache_db = req.cache_db or OPTIONS_CACHE_DB
    task_id = get_task_queue().queue_task(
        task_type="build_options",
        name=f"Build options cache: {cache_db}",
        payload={
            "underlyings": underlyings,
            "start": req.start,
            "end": req.end,
            "cache_db": cache_db,
            "feed": req.feed or "indicative",
        },
        description=f"Build options cache {cache_db} for {len(underlyings)} underlyings",
        timeout_seconds=24 * 3600,
    )
    logger.info(f"build-options enqueued task {task_id}")
    return {"task_id": task_id}


@router.post("/prewarm")
async def prewarm(req: PrewarmRequest):
    """Enqueue a ``prewarm`` task on the main queue. Returns {task_id}."""
    symbols = [s.strip().upper() for s in (req.symbols or []) if s and s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols must be non-empty")
    payload = {"symbols": symbols, "workers": int(req.workers or 5)}
    if req.experts is not None:
        payload["experts"] = req.experts
    if req.end is not None:
        payload["end"] = req.end
    task_id = get_task_queue().queue_task(
        task_type="prewarm",
        name=f"Prewarm FMP history: {len(symbols)} symbols",
        payload=payload,
        description=f"Pre-warm FMP history disk cache for {len(symbols)} symbols",
        timeout_seconds=24 * 3600,
    )
    logger.info(f"prewarm enqueued task {task_id}")
    return {"task_id": task_id}

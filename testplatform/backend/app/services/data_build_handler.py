"""Task handlers for the data-build endpoints (``app/api/data_build.py``).

These mirror the headless ``ba2-test`` build commands (ba2test_launcher) but run as background
tasks on the task queue so the React UI can drive them without blocking the request:

  * ``build_screener_metrics`` — wraps ``ba2_providers.screener.metric_store.build_store``
    (CLI ``_cmd_build_screener_metrics``).
  * ``build_options``         — wraps ``app.services.backtest.fetch_options.build_cache``
    (CLI ``_cmd_fetch_options``).
  * ``prewarm``               — wraps the per-symbol FMP-history disk-cache pre-warm
    (CLI ``_cmd_prewarm``).

Contract matches the other handlers (``handle_daily_backtest`` etc.):
``handler(task_id: str, payload: dict) -> result dict``; a returned ``{'status':'failed',...}``
marks the task failed. Required payload keys are validated fail-early (no-defaults rule,
backend/CLAUDE.md). The OHLCV build is NOT here — it reuses the existing
``ohlcv_cache_fetch`` handler on the dedicated OHLCV queue (one task per symbol).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _resolve_fmp_key() -> str:
    """Resolve the FMP API key the same way the CLI / providers do (env, then app-settings DB)."""
    key = os.getenv("FMP_API_KEY")
    if not key:
        try:
            from ba2_common.config import get_app_setting

            key = get_app_setting("FMP_API_KEY")
        except Exception:  # noqa: BLE001
            key = None
    return key


def handle_build_screener_metrics(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build/extend the screener METRIC store (parquet) from the as-of OHLCV cache.

    Mirrors ``ba2test_launcher._cmd_build_screener_metrics``: derives a latest-filing-ish shares
    map from the FMP screener rows (marketCap / price), wires the as-of OHLCV cache accessor, and
    calls ``metric_store.build_store``. Required payload keys: store, start, end, market_cap_min.
    """
    # Default the store dir to the shared ba2_common screener store (trade bucket)
    # when omitted — nothing is cached inside the repo. Still overridable.
    if payload.get("store") is None:
        try:
            from ba2_common.config import SCREENER_STORE_DIR
            payload = {**payload, "store": SCREENER_STORE_DIR}
        except Exception:  # noqa: BLE001
            pass
    for key in ("store", "start", "end", "market_cap_min"):
        if payload.get(key) is None:
            return {"status": "failed", "error": f"payload.{key} is required"}

    try:
        import os as _os
        # Ensure the (possibly nested, trade-bucket) store dir exists.
        _os.makedirs(payload["store"], exist_ok=True)
        import app.models  # noqa: F401 — register ORM models on Base
        import pandas as _pd
        from datetime import datetime as _dt
        from app.models.database import init_db
        from ba2_providers.screener import metric_store as ms
        from ba2_providers.cache.cached_get import ohlcv_get
        from ba2_providers import get_provider

        init_db()
        api_key = _resolve_fmp_key()
        if not api_key:
            return {"status": "failed", "error": "FMP_API_KEY not configured"}

        # Shares map derived once from the screener rows (marketCap / price) — same as the CLI.
        shares_by_sym: Dict[str, float] = {}
        for r in ms._fetch_screener_rows(api_key):
            sym = r.get("symbol")
            cap = r.get("marketCap") or 0
            px = r.get("price") or 0
            if sym and cap > 0 and px > 0:
                shares_by_sym[sym] = cap / px

        prov = get_provider("ohlcv", "fmp")

        def _ohlcv(sym, end):
            df = ohlcv_get(prov, sym, as_of=_dt.fromisoformat(end), lookback=4000)
            if df is None or len(df) == 0:
                return df
            idx = _pd.to_datetime(df["Date"])
            if idx.dt.tz is not None:
                idx = idx.dt.tz_localize(None)
            return df.set_index(idx).sort_index()

        def _shares(sym):
            return shares_by_sym.get(sym)

        summary = ms.build_store(
            payload["store"],
            api_key,
            payload["start"],
            payload["end"],
            market_cap_min=float(payload["market_cap_min"]),
            price_min=float(payload.get("price_min", 0.0)),
            volume_min=float(payload.get("volume_min", 0.0)),
            ohlcv_get=_ohlcv,
            shares_get=_shares,
            cadence_days=int(payload.get("cadence_days", 7)),
            drop_days=int(payload.get("drop_days", 1)),
        )
        logger.info(f"build-screener-metrics task {task_id}: {summary}")
        return {"status": "completed", "summary": summary}
    except Exception as e:  # noqa: BLE001 — surface as a failed task, don't crash the worker
        logger.error(f"build-screener-metrics task {task_id} failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}


def handle_build_options(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the offline options cache from Alpaca.

    Mirrors ``ba2test_launcher._cmd_fetch_options``. Required payload keys: underlyings (list of
    symbols), start, end (ISO, start >= 2024-02-01), cache_db. Optional: feed (default
    "indicative").
    """
    # Default the options cache DB to the shared ba2_common path (common bucket)
    # when omitted — nothing is cached inside the repo. Still overridable.
    if payload.get("cache_db") is None:
        try:
            from ba2_common.config import OPTIONS_CACHE_DB
            payload = {**payload, "cache_db": OPTIONS_CACHE_DB}
        except Exception:  # noqa: BLE001
            pass
    for key in ("underlyings", "start", "end", "cache_db"):
        if payload.get(key) is None:
            return {"status": "failed", "error": f"payload.{key} is required"}

    try:
        import os as _os
        # Ensure the (possibly nested, common-bucket) options cache parent dir exists.
        _parent = _os.path.dirname(str(payload["cache_db"]))
        if _parent:
            _os.makedirs(_parent, exist_ok=True)
        from app.services.backtest import fetch_options
        from datetime import date

        underlyings = payload["underlyings"]
        if isinstance(underlyings, str):
            underlyings = [s.strip() for s in underlyings.split(",") if s.strip()]
        underlyings = [str(s).strip().upper() for s in underlyings if str(s).strip()]
        if not underlyings:
            return {"status": "failed", "error": "payload.underlyings must be non-empty"}

        result = fetch_options.build_cache(
            payload["cache_db"],
            underlyings,
            date.fromisoformat(str(payload["start"])[:10]),
            date.fromisoformat(str(payload["end"])[:10]),
            payload.get("feed", "indicative"),
        )
        logger.info(f"build-options task {task_id}: {result}")
        return {"status": "completed", "result": result}
    except Exception as e:  # noqa: BLE001
        logger.error(f"build-options task {task_id} failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}


def handle_prewarm(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-build the per-symbol FMP-history disk cache for the grid experts.

    Mirrors ``ba2test_launcher._cmd_prewarm``: runs each (expert, symbol) cached fetch inside
    ``frozen_ttl_cache()`` (which engages the backtest-only disk cache) across a thread pool.
    Required payload keys: symbols (list). Optional: experts (list; default the 3 disk-cached
    history experts), workers (default 5), end (ISO; default now).
    """
    if payload.get("symbols") is None:
        return {"status": "failed", "error": "payload.symbols is required"}

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import timezone as _tz
        from ba2_providers.fmp_common import frozen_ttl_cache

        key = _resolve_fmp_key()
        if not key:
            return {"status": "failed", "error": "FMP_API_KEY not configured"}

        symbols = payload["symbols"]
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not symbols:
            return {"status": "failed", "error": "payload.symbols must be non-empty"}

        experts = payload.get("experts") or ["FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy"]
        if isinstance(experts, str):
            experts = [e.strip() for e in experts.split(",") if e.strip()]
        workers = int(payload.get("workers", 5))

        end_raw = payload.get("end")
        if end_raw:
            end_date = datetime.fromisoformat(str(end_raw))
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=_tz.utc)
        else:
            end_date = datetime.now(_tz.utc)

        from ba2_experts.FMPRating import (
            fetch_grades_historical_cached,
            fetch_price_target_history_cached,
            fetch_analyst_grades_cached,
        )
        from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
            FMPCompanyDetailsProvider,
        )
        from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider

        _details_provider = {"p": None}
        _insider_provider = {"p": None}

        def _do_fmprating(sym: str) -> None:
            fetch_grades_historical_cached(key, sym)
            fetch_price_target_history_cached(key, sym)
            fetch_analyst_grades_cached(key, sym)   # dated individual grades (rating-recency)

        def _do_earnings_drift(sym: str) -> None:
            if _details_provider["p"] is None:
                _details_provider["p"] = FMPCompanyDetailsProvider()
            _details_provider["p"].get_past_earnings(
                sym, frequency="quarterly", end_date=end_date,
                lookback_periods=8, format_type="dict",
            )

        def _do_insider(sym: str) -> None:
            if _insider_provider["p"] is None:
                _insider_provider["p"] = FMPInsiderProvider()
            _insider_provider["p"].get_insider_transactions(
                sym, end_date=end_date, lookback_days=400, as_of=end_date,
                format_type="dict",
            )

        fetchers = {
            "FMPRating": _do_fmprating,
            "FMPEarningsDrift": _do_earnings_drift,
            "FMPInsiderClusterBuy": _do_insider,
        }

        work = []
        skipped = []
        for expert in experts:
            fetcher = fetchers.get(expert)
            if fetcher is None:
                skipped.append(expert)
                continue
            for sym in symbols:
                work.append((expert, sym, fetcher))

        if not work:
            return {
                "status": "completed",
                "summary": {"cached": {}, "errors": 0, "skipped": skipped,
                            "note": "no disk-cached experts to pre-warm"},
            }

        counts: Dict[str, int] = {}
        errors = 0
        with frozen_ttl_cache():
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futures = {ex.submit(fn, sym): (expert, sym) for (expert, sym, fn) in work}
                for fut in as_completed(futures):
                    expert, sym = futures[fut]
                    try:
                        fut.result()
                        counts[expert] = counts.get(expert, 0) + 1
                    except Exception as e:  # noqa: BLE001 — one bad symbol must not abort
                        errors += 1
                        logger.warning(f"prewarm {expert}/{sym} failed: {e}")

        summary = {"cached": counts, "errors": errors, "skipped": skipped,
                   "symbols": len(symbols)}
        logger.info(f"prewarm task {task_id}: {summary}")
        return {"status": "completed", "summary": summary}
    except Exception as e:  # noqa: BLE001
        logger.error(f"prewarm task {task_id} failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}

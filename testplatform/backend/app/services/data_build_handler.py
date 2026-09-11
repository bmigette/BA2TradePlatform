"""Task handlers for the data-build endpoints (``app/api/data_build.py``).

These mirror the headless ``ba2-test`` build commands (ba2test_launcher) but run as background
tasks on the task queue so the React UI can drive them without blocking the request:

  * ``build_screener_metrics`` — wraps ``ba2_providers.screener.metric_store.build_store``
    (CLI ``_cmd_build_screener_metrics``).
  * ``build_options``         — wraps ``app.services.backtest.fetch_options.build_cache``
    (CLI ``_cmd_fetch_options``).
  * ``prewarm``               — wraps ``app.services.prewarm_fetchers.run_prewarm``, the
    SAME per-symbol FMP-history disk-cache pre-warm the CLI ``_cmd_prewarm`` runs.

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


def _resolve_fred_key() -> str:
    """Re-export: the ONE resolver now lives in ``prewarm_fetchers`` beside the run it
    serves, so the CLI reaches it too (it never warmed FRED at all while this was here).
    Kept as a name because existing callers/tests import it from this module."""
    from app.services.prewarm_fetchers import resolve_fred_key
    return resolve_fred_key()


def _prewarm_fred(max_age_hours: float = 24.0) -> Dict[str, Any]:
    """Re-export of ``prewarm_fetchers.prewarm_fred`` (see :func:`_resolve_fred_key`)."""
    from app.services.prewarm_fetchers import prewarm_fred
    return prewarm_fred(max_age_hours, log=logger.info)


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
    symbols), start, end (ISO, start >= 2024-01-18), cache_db. Optional: feed (default
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

    Argument parsing and reporting only: the run itself is
    ``app.services.prewarm_fetchers.run_prewarm``, the same call ``ba2-test prewarm`` makes, so
    the two entry points cannot drift. They had drifted badly. This handler knew 3 of the 7
    experts, warmed no per-symbol data at all for DeterministicScorer (only FRED), and -- worst
    -- entered ``frozen_ttl_cache()`` on the SUBMITTING thread only. That flag is thread-local,
    so every pool worker ran un-frozen: ``fmp_history_disk_cached`` took its live passthrough
    branch, the fetches went out over the network, and NOT ONE cache file was written, while the
    task reported success (2026-09-10 live-replay readiness audit, "Two prewarm tooling gaps").

    Required payload keys: symbols (list). Optional: experts (list; default the 3 core
    rating/signal experts), workers (default 5), end (ISO; default now).
    """
    if payload.get("symbols") is None:
        return {"status": "failed", "error": "payload.symbols is required"}

    try:
        from datetime import timezone as _tz

        from app.services.prewarm_fetchers import (
            PrewarmConfigError, PrewarmFetchers, resolve_keys, run_prewarm,
        )

        symbols = payload["symbols"]
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not symbols:
            return {"status": "failed", "error": "payload.symbols must be non-empty"}

        experts = payload.get("experts") or ["FMPRating", "FMPEarningsDrift",
                                             "FMPInsiderClusterBuy"]
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

        # DeterministicScorer's macro series are economy-wide, so they are refreshed once here
        # rather than entering the per-symbol work list (its per-symbol financial histories DO
        # enter it, through the shared fetcher table, same as the CLI).
        fred_summary = None
        if "DeterministicScorer" in experts:
            fred_summary = _prewarm_fred(float(payload.get("fred_max_age_hours", 24.0)))
            logger.info(f"prewarm task {task_id}: FRED {fred_summary}")

        keys = resolve_keys()
        try:
            fetchers = PrewarmFetchers(fmp_key=keys["fmp"], end_date=end_date,
                                       finnhub_key=keys["finnhub"], log=logger.info)
            summary = run_prewarm(fetchers, experts, symbols, workers, end=end_date)
        except PrewarmConfigError as e:
            # A configuration gap, not a data gap: it would repeat for every remaining symbol,
            # so the whole task fails instead of reporting a partial warm as success.
            logger.error(f"prewarm task {task_id} refused: {e}", exc_info=True)
            return {"status": "failed", "error": str(e)}

        summary["fred"] = fred_summary
        logger.info(f"prewarm task {task_id}: {summary}")
        return {"status": "completed", "summary": summary}
    except Exception as e:  # noqa: BLE001
        logger.error(f"prewarm task {task_id} failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}

"""
Reusable Stock Screener Module

Provides a configurable screen/enrich/rank pipeline for finding stocks
matching user-defined criteria. Designed to be consumed by any expert
(e.g. PennyMomentumTrader, SwingTrader) via composition.

Pipeline stages:
    1. Screen  – call screener provider with basic filters
    2. Enrich  – batch-fetch FMP quotes for RVOL + client-side filters
    3. Rank    – sort by chosen metric
    4. Filter  – bulk price-drop check on ranked list, stop at N
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ba2_common.config import get_app_setting
from ba2_common.logger import logger
from ba2_common.core.replay.context import capture_aware_submit
from ba2_common.core.replay.observe import observe_provider
from ba2_providers.fmp_common import FMPError


#: ONE window per symbol per day, wide enough for every pass a screen makes.
#:
#: WHY A SINGLE WIDE WINDOW instead of the window each caller asks for. A screen calls
#: _fetch_history_bulk three times with three different lookbacks -- volume/RVOL
#: (``window + 10``, ~35d), the price-drop filter (``screener_price_drop_days``, a
#: per-INSTANCE setting) and Weinstein (250d) -- and the cache key carried the exact
#: from/to dates, so the same symbol was fetched once per pass, and again for every
#: instance whose price-drop setting differed. Prod on 2026-09-08 ran 10 screens over
#: universes of 1177/878/641/640/459/304/116/26/18 symbols: roughly 4,200 symbol-history
#: fetches in a day, none of which could reuse each other.
#:
#: 250 (Weinstein) is the widest any pass needs today; 400 leaves room for a longer
#: filter without re-fragmenting the cache, and the extra bars cost one payload rather
#: than one request per pass.
SCREENER_HISTORY_WINDOW_DAYS = 400

#: LIVE ONLY. When more than this fraction of the stage-2 candidates still have no price history
#: after one re-fetch, and a volume bound / RVOL needs that history, the screen raises
#: ScreenerDataError instead of returning a silently smaller list. 2026-10: four straight Monday
#: FactorRanker screens returned 0 candidates and nothing said whether the filters or an FMP
#: failure emptied the list.
SCREENER_DATA_FAILURE_MAX_FRACTION = 0.10

#: Opt-in (env var, or the app setting of the same name): a directory that receives the full
#: ``last_diagnostics`` of every LIVE screen as one JSON file. Default off.
SCREENER_DIAG_DIR_ENV = "BA2_SCREENER_DIAG_DIR"
_DIAG_SYMBOLS_SHOWN = 8       # symbols named in the FMP DATA LOSS warning
_LOG_LIST_CAP = 50            # symbols per stage listed in the INFO log (full list: diagnostics)
_LOG_ROWS_SHOWN = 10          # raw vendor rows echoed in the INFO log
_SENSITIVE_NAME_PARTS = ("apikey", "api_key", "secret", "token", "password")
_REDACTED = "***REDACTED***"


def _redact(obj: Any, secrets: List[str]) -> Any:
    """Deep copy of ``obj`` that can never carry a credential: values under a key whose name
    looks like one are replaced, and any string containing a known secret value is scrubbed."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(part in str(k).lower() for part in _SENSITIVE_NAME_PARTS):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v, secrets)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for sec in secrets:
            if sec and sec in obj:
                obj = obj.replace(sec, _REDACTED)
        return obj
    return obj


def _new_history_stats() -> Dict[str, Any]:
    return {
        "calls": 0, "symbols_requested": 0, "chunks": 0, "chunks_failed": 0,
        "failed_symbols": [], "ok_symbols": set(), "requests": 0, "retried_requests": 0,
        "rate_limited_responses": 0, "no_bars_symbols": 0,
    }


class ScreenerDataError(FMPError):
    """The live screen could not fetch enough market data to produce a trustworthy result.

    Raised (live, ``as_of is None``, only) when:
      * the vendor screener request fails, returns a non-list body, or FMP_API_KEY is missing
        (stage 1: an outage must not read as "0 candidates");
      * the bulk float table cannot be fetched while a float bound is set;
      * stage 2 finds NO price history for ANY candidate (whatever the bounds), or, when a volume
        bound / RVOL needs history, more than ``SCREENER_DATA_FAILURE_MAX_FRACTION`` of the
        candidates have none after one re-fetch;
      * the price-drop or Weinstein stage finds no price history for any candidate.
    So an empty list from a live screen means "the filters matched nothing", not "FMP failed".
    Never raised on the as_of (backtest) path.
    """


class StockScreener:
    """
    Configurable stock screener with screen/enrich/rank pipeline.

    Settings keys (all have defaults):
        screener_provider          str   "fmp"
        screener_market_cap_min    int   1_000_000_000
        screener_market_cap_max    int   0  (0 = disabled)
        screener_volume_min        int   500_000
        screener_volume_max        int   0  (0 = disabled)
        screener_float_min         int   10_000_000
        screener_float_max         int   0  (0 = disabled)
        screener_price_min         float 20.0
        screener_price_max         float 0  (0 = disabled)
        screener_relative_volume_min float 1.05 (0 = disabled)
        screener_price_drop_pct    float 15.0 (0 = disabled)
        screener_price_drop_days   int   1
        screener_max_stocks        int   10
        screener_sort_metric       str   "market_cap"
    """

    # Default values for every setting key
    _DEFAULTS: Dict[str, Any] = {
        "screener_provider": "fmp",
        "screener_market_cap_min": 1_000_000_000,
        "screener_market_cap_max": 0,
        "screener_volume_min": 500_000,
        "screener_volume_max": 0,
        "screener_float_min": 10_000_000,
        "screener_float_max": 0,
        "screener_price_min": 20.0,
        "screener_price_max": 0,
        "screener_relative_volume_min": 1.05,
        "screener_price_drop_pct": 15.0,
        "screener_price_drop_days": 1,
        "screener_max_stocks": 10,
        "screener_sort_metric": "market_cap",
        # Weinstein Stage 2 filter (0 = off): keep only stocks in an advancing
        # stage (price above a rising 30-week SMA).
        "screener_weinstein_stage2_only": 0,
        # Historical universe mode (only consulted on the as_of/reconstructed path):
        # 'broad' = available-traded UNION delisted, 'sp500' / 'nasdaq' = dated
        # index constituents. Ignored entirely on the live (as_of=None) path.
        "universe_mode": "broad",
    }

    # Metrics that can be used for ranking
    _VALID_SORT_METRICS = {
        "market_cap",
        "volume",
        "float_shares",
        "relative_volume",
        "composite",
        "price_drop_pct",
    }

    def __init__(
        self,
        settings: Dict[str, Any],
        progress_callback=None,
        as_of: Optional[datetime] = None,
    ):
        """
        Initialise the screener from a settings dict.

        Missing keys fall back to class-level defaults.

        Args:
            settings: Dict of screener settings.
            progress_callback: Optional callable(step: str, value: float) called at
                each pipeline stage.  ``value`` is in [0, 1].
            as_of: Point-in-time anchor. ``None`` (default) = live screen via the
                configured ``screener_provider`` (today's listings, byte-identical to
                the pre-Phase-3 behaviour). A ``<date>`` selects the reconstructed
                historical path: the ``fmp_historical`` provider builds a
                survivorship-free universe for ``as_of`` and the two ``now()``-based
                fetch windows below are re-anchored to ``as_of`` so RVOL / Weinstein /
                price-drop read bars truncated to ``as_of``. The post-fetch filter
                LOGIC never forks — only its data inputs swap live<->as-of.
        """
        self._progress_callback = progress_callback
        # None => live; <date> => reconstructed historical screen.
        self._as_of = as_of
        #: LIVE ONLY (empty on a backtest): per-stage evidence of the last screen(). Read-only
        #: recording -- nothing here feeds back into selection or the returned dict.
        self.last_diagnostics: Dict[str, Any] = {}
        self._hist = _new_history_stats()
        self._quote_chunks_failed = 0
        self._diag: Optional[Dict[str, Any]] = None
        self._settings: Dict[str, Any] = {}
        for key, default in self._DEFAULTS.items():
            raw = settings.get(key)
            if raw is None:
                self._settings[key] = default
            else:
                # Coerce to the same type as the default
                try:
                    self._settings[key] = type(default)(raw)
                except (ValueError, TypeError):
                    logger.warning(
                        f"StockScreener: invalid value for {key}={raw!r}, "
                        f"using default {default!r}"
                    )
                    self._settings[key] = default

        sort_metric = self._settings["screener_sort_metric"]
        if sort_metric not in self._VALID_SORT_METRICS:
            logger.warning(
                f"StockScreener: unknown sort metric '{sort_metric}', "
                f"falling back to 'market_cap'"
            )
            self._settings["screener_sort_metric"] = "market_cap"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _report_progress(self, step: str, value: float) -> None:
        """Fire the progress callback if one was provided."""
        if self._progress_callback:
            try:
                self._progress_callback(step, value)
            except Exception:
                pass

    @observe_provider(
        "screener", "screen",
        identity=lambda a: {
            "as_of": a["self"]._as_of,
            "filters": a["self"]._settings,
        },
    )
    def screen(self) -> Dict[str, Any]:
        """
        Execute the full screen/enrich/rank pipeline.

        Returns:
            Dict with keys:
                results: Sorted list of stock dicts, length <= screener_max_stocks.
                stats: Dict of per-filter drop counts and totals.
        """
        self._hist = _new_history_stats()
        self._quote_chunks_failed = 0
        self.last_diagnostics = {}
        self._diag = None
        if self._as_of is not None:
            return self._screen_impl()       # backtest: no recording of any kind
        self._diag = {
            "schema": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "settings": dict(self._settings),
            "stages": {}, "stage_symbols": {}, "candidates": {}, "price_drop": [],
            "history_windows": {}, "final": [],
        }
        self.last_diagnostics = self._diag
        try:
            return self._screen_impl()
        except Exception as e:
            self._diag["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._emit_diagnostics()

    def _screen_impl(self) -> Dict[str, Any]:
        from ba2_providers import get_provider

        stats: Dict[str, int] = {}
        diag = self._diag          # None on a backtest -> every recording below is skipped

        # --- Provider selection: the ONE fork (fetch source, not filter logic) ---
        # as_of=None -> the configured live provider (unchanged); as_of=<date> ->
        # the survivorship-free 'fmp_historical' provider for the universe_mode.
        if self._as_of is None:
            provider_name = self._settings["screener_provider"]
            screener = get_provider("screener", provider_name)
        else:
            provider_name = "fmp_historical"
            screener = get_provider(
                "screener", "fmp_historical",
                universe_mode=self._settings["universe_mode"],
            )

        # --- Stage 1: basic screen via provider ---
        filters = self._build_provider_filters()
        logger.info(
            f"StockScreener: stage 1 — screening via '{provider_name}' "
            f"(as_of={self._as_of}) with filters: {filters}"
        )
        self._report_progress("Fetching candidates from screener...", 0.05)
        t_call = datetime.now(timezone.utc)
        candidates = screener.screen_stocks(filters, as_of=self._as_of)
        stats["screener_candidates"] = len(candidates)
        live = self._as_of is None
        if diag is not None:
            self._record_vendor_call(screener, provider_name, filters, candidates, t_call)
            self._record_stage("provider", candidates)
        if live:
            # every stage-accounting key always exists on a live run (0 = the stage dropped none),
            # including on the early "no candidates" returns
            stats.update(dropped_rvol=0, dropped_float=0, dropped_volume_min=0,
                         dropped_volume_max=0, dropped_no_history=0)
        logger.info(
            f"StockScreener: stage 1 done — {len(candidates)} candidates returned"
        )

        if not candidates:
            self._report_progress("No candidates found.", 1.0)
            return {"results": [], "stats": stats}

        # --- Stage 1b: share-float bounds (live only) ---
        # The vendor screener has no float parameter, so the bound is ours: the vendor's bulk
        # shares-float table (one cached call); an unknown float passes (as in the metric store).
        # as_of (historical) screens keep their documented "float is approximate" behaviour.
        float_min = self._settings["screener_float_min"]
        float_max = self._settings["screener_float_max"]
        if live and (float_min > 0 or float_max > 0):
            from ba2_providers.screener.float_filter import filter_by_float
            candidates, f_stats = filter_by_float(candidates, float_min, float_max)
            stats.update(f_stats)
            self._record_stage("float", candidates)
            logger.info(
                f"StockScreener: float filter [{float_min or '-'}, {float_max or '-'}] — "
                f"{len(candidates)} candidates left ({f_stats['float_unknown']} with unknown float passed)"
            )
            if not candidates:
                self._report_progress("No candidates after float filter.", 1.0)
                return {"results": [], "stats": stats}

        # --- Stage 2: volume / RVOL enrichment + live refresh + client-side filters ---
        # LIVE: ALWAYS runs. rvol_min == 0 only turns the RVOL *filter* off; the stage's other
        # effects (average-volume floor/ceiling, and the live price + market-cap refresh that the
        # rank key and the price-drop test read) must not depend on it.
        # AS_OF (backtest): unchanged -- runs only when rvol_min > 0 (it fetches history over
        # HTTP, which a hermetic run forbids).
        rvol_min = self._settings["screener_relative_volume_min"]
        if live or rvol_min > 0:
            logger.info(
                f"StockScreener: stage 2 — volume/RVOL enrichment on {len(candidates)} candidates "
                f"(min RVOL={rvol_min})"
            )
            self._report_progress(
                f"Fetching live prices for {len(candidates)} candidates (RVOL)...", 0.2
            )
            float_dropped = stats.get("dropped_float", 0)
            candidates, enrich_stats = self._enrich_with_rvol(candidates, rvol_min)
            stats.update(enrich_stats)
            if live:
                stats["dropped_float"] += float_dropped   # the float stage's drops, not the enrich's 0
                # "relative_volume" = what entered stage 2 minus the RVOL and no-history drops
                # (the volume floor/ceiling and float drops are added back); "volume_filters" =
                # the true survivors of stage 2.
                self._record_stage("relative_volume", None, count=(
                    len(candidates) + enrich_stats["dropped_float"]
                    + enrich_stats["dropped_volume_min"] + enrich_stats["dropped_volume_max"]))
                self._record_stage("volume_filters", candidates)
            logger.info(
                f"StockScreener: stage 2 done — {len(candidates)} candidates after the volume filters"
            )

        if not candidates:
            self._report_progress("No candidates after volume filters.", 1.0)
            return {"results": [], "stats": stats}

        # --- Stage 2.5: Weinstein Stage 2 filter (optional) ---
        if self._settings.get("screener_weinstein_stage2_only"):
            logger.info(
                f"StockScreener: Weinstein filter — keeping only Stage 2 of "
                f"{len(candidates)} candidates"
            )
            self._report_progress(
                f"Checking Weinstein stage for {len(candidates)} candidates...", 0.55
            )
            candidates, w_stats = self._filter_by_weinstein_stage2(candidates)
            stats.update(w_stats)
            self._record_stage("weinstein", candidates)
            logger.info(
                f"StockScreener: Weinstein filter done — {len(candidates)} in Stage 2"
            )
            if not candidates:
                self._report_progress("No candidates in Weinstein Stage 2.", 1.0)
                return {"results": [], "stats": stats}

        metric = self._settings["screener_sort_metric"]
        max_stocks = self._settings["screener_max_stocks"]
        drop_pct = self._settings["screener_price_drop_pct"]
        drop_days = self._settings["screener_price_drop_days"]

        if metric == "price_drop_pct":
            # Ranking by price drop: fetch history for ALL candidates, sort by drop descending.
            logger.info(
                f"StockScreener: stage 3/4 — price-drop ranking on {len(candidates)} candidates"
            )
            self._report_progress(
                f"Fetching price history for {len(candidates)} candidates (sort by drop)...", 0.7
            )
            result, drop_stats = self._filter_by_price_drop(
                candidates,
                min_drop_pct=drop_pct if drop_pct > 0 else 0,
                max_results=len(candidates),  # fetch all — trim after sorting
            )
            stats.update(drop_stats)
            self._record_stage("price_drop", result)
            result = sorted(result, key=lambda c: c.get("price_drop_pct") or 0, reverse=True)
            result = result[:max_stocks]
            logger.info(
                f"StockScreener: stage 3/4 done — {len(result)} stocks ranked by price_drop_pct"
            )
        else:
            # --- Stage 3: rank ---
            logger.info(
                f"StockScreener: stage 3 — ranking {len(candidates)} candidates by {metric}"
            )
            self._report_progress("Ranking candidates...", 0.7)
            ranked = self._rank(candidates)
            logger.info(f"StockScreener: stage 3 done")

            # --- Stage 4: price-drop filter ---
            if drop_pct > 0 and drop_days > 0:
                logger.info(
                    f"StockScreener: stage 4 — price-drop filter on {len(ranked)} candidates "
                    f"(>={drop_pct}% over {drop_days}d, target {max_stocks} stocks)"
                )
                self._report_progress(
                    f"Checking price history (>={drop_pct}% drop over {drop_days}d)...", 0.8
                )
                result, drop_stats = self._filter_by_price_drop(ranked, drop_pct, max_stocks)
                stats.update(drop_stats)
                self._record_stage("price_drop", result)
                logger.info(
                    f"StockScreener: stage 4 done — {len(result)} stocks passed price-drop filter"
                )
            else:
                result = ranked[:max_stocks]

        stats["final_count"] = len(result)
        self._record_final(result)
        self._report_progress(f"Done — {len(result)} stock(s) matched.", 1.0)
        logger.info(
            f"StockScreener: pipeline complete — {len(result)} stocks "
            f"(sorted by {self._settings['screener_sort_metric']})"
        )
        self._log_live_selection(result)
        return {"results": result, "stats": stats}

    # ------------------------------------------------------------------
    # LIVE-ONLY diagnostics (read-only recording; never alters selection or results)
    # ------------------------------------------------------------------

    def _record_stage(self, name: str, rows: Optional[List[Dict[str, Any]]],
                      count: Optional[int] = None) -> None:
        d = self._diag
        if d is None:
            return
        if rows is not None:
            d["stages"][name] = len(rows)
            d["stage_symbols"][name] = [str(r.get("symbol")) for r in rows]
        else:
            d["stages"][name] = count

    def _record_vendor_call(self, screener, provider_name, filters, rows, called_at) -> None:
        d = self._diag
        try:
            build = getattr(screener, "_build_params", None)
            http_params = build(filters) if callable(build) else None
        except Exception as e:      # recording only: never let it break a screen
            http_params = {"unavailable": f"{type(e).__name__}: {e}"}
        d["vendor_request"] = {
            "provider": provider_name, "filters": dict(filters), "http_params": http_params,
            "called_at": called_at.isoformat(),
            "returned_at": datetime.now(timezone.utc).isoformat(), "rows": len(rows),
        }
        d["vendor_rows"] = [
            {"symbol": r.get("symbol"), "price": r.get("price"),
             "marketCap": r.get("market_cap"), "volume": r.get("volume")} for r in rows
        ]

    def _record_final(self, result: List[Dict[str, Any]]) -> None:
        d = self._diag
        if d is None:
            return
        d["stages"]["final"] = len(result)
        d["stage_symbols"]["final"] = [str(r.get("symbol")) for r in result]
        metric = self._settings["screener_sort_metric"]
        key = self._sort_key_fn()
        d["final"] = [
            {"rank": i + 1, "symbol": r.get("symbol"), "rank_metric": metric,
             "rank_key": key(r)} for i, r in enumerate(result)
        ]

    @staticmethod
    def _secrets() -> List[str]:
        out = []
        try:
            k = get_app_setting("FMP_API_KEY")
            if k and isinstance(k, str):
                out.append(k)
        except Exception:
            pass
        return out

    def _emit_diagnostics(self) -> None:
        """LIVE ONLY. Fold the FMP failure counters into ``last_diagnostics``, redact it, log the
        compact INFO/WARNING lines and (opt-in) dump it as JSON."""
        d = self._diag
        if d is None:
            return
        h = self._hist
        failed = sorted(set(h["failed_symbols"]) - h["ok_symbols"])
        d["finished_at"] = datetime.now(timezone.utc).isoformat()
        d["fmp"] = {
            "history_calls": h["calls"], "history_chunks": h["chunks"],
            "history_chunks_failed": h["chunks_failed"], "history_requests": h["requests"],
            "history_retried_requests": h["retried_requests"],
            "history_rate_limited_responses": h["rate_limited_responses"],
            "quote_chunks_failed": self._quote_chunks_failed,
            "symbols_no_bars": h["no_bars_symbols"],
        }
        d["dropped_data_fetch_failure"] = len(failed)
        d["dropped_data_fetch_failure_symbols"] = failed
        # Redact the stored copy too: anything that reaches last_diagnostics or a file is clean.
        redacted = _redact(d, self._secrets())
        d.clear()
        d.update(redacted)

        stages = d["stages"]
        order = ("provider", "float", "relative_volume", "volume_filters", "weinstein",
                 "price_drop", "final")
        parts = " -> ".join(f"{k}={stages[k]}" for k in order if k in stages)
        f = d["fmp"]
        logger.info(
            f"StockScreener LIVE STAGES: {parts or 'no stage reached'} | FMP history "
            f"chunks_failed={f['history_chunks_failed']}/{f['history_chunks']} "
            f"requests={f['history_requests']} retried={f['history_retried_requests']} "
            f"rate_limited_429/5xx={f['history_rate_limited_responses']} "
            f"quote_chunks_failed={f['quote_chunks_failed']} "
            f"symbols_no_bars={f['symbols_no_bars']} dropped_for_data={len(failed)}"
        )
        if failed or f["quote_chunks_failed"]:
            logger.warning(
                f"StockScreener: FMP DATA LOSS -- {len(failed)} symbol(s) dropped because their "
                f"history/volume could not be fetched (NOT because they failed a filter), first "
                f"{min(len(failed), _DIAG_SYMBOLS_SHOWN)}: {failed[:_DIAG_SYMBOLS_SHOWN]}; "
                f"{f['quote_chunks_failed']} live-quote chunk(s) failed (price/market cap fell "
                f"back to the last bar close)"
            )
        req = d.get("vendor_request")
        if req:
            rows = d.get("vendor_rows", [])
            logger.info(
                f"StockScreener DIAG vendor: provider={req['provider']} called_at={req['called_at']} "
                f"params={json.dumps(req['http_params'], default=str, sort_keys=True)} "
                f"rows={len(rows)} first{_LOG_ROWS_SHOWN}="
                f"{json.dumps(rows[:_LOG_ROWS_SHOWN], default=str)}"
            )
        for name in order:
            syms = d["stage_symbols"].get(name)
            if syms is not None:
                more = f" (+{len(syms) - _LOG_LIST_CAP} more)" if len(syms) > _LOG_LIST_CAP else ""
                logger.info(
                    f"StockScreener DIAG stage {name}: n={len(syms)} "
                    f"symbols={syms[:_LOG_LIST_CAP]}{more}"
                )
        if d["candidates"]:
            logger.info(
                f"StockScreener DIAG candidates after stage 2: n={len(d['candidates'])} "
                f"first{_LOG_ROWS_SHOWN}="
                f"{json.dumps(list(d['candidates'].values())[:_LOG_ROWS_SHOWN], default=str)}"
            )
        if d["price_drop"]:
            logger.info(
                f"StockScreener DIAG price-drop walk: n={len(d['price_drop'])} "
                f"first{_LOG_ROWS_SHOWN}={json.dumps(d['price_drop'][:_LOG_ROWS_SHOWN], default=str)}"
            )
        if d["final"]:
            logger.info(
                f"StockScreener DIAG final order (rank key = {d['final'][0]['rank_metric']}): "
                f"{[(r['symbol'], r['rank_key']) for r in d['final'][:_LOG_LIST_CAP]]}"
            )
        self._dump_diagnostics(d)

    def _dump_diagnostics(self, d: Dict[str, Any]) -> None:
        """Opt-in (``BA2_SCREENER_DIAG_DIR``): write the redacted diagnostics atomically."""
        target = os.environ.get(SCREENER_DIAG_DIR_ENV)
        if not target:
            return
        try:
            os.makedirs(target, exist_ok=True)
            name = (f"screener_diag_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                    f"_{os.getpid()}.json")
            final = os.path.join(target, name)
            tmp = final + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, default=str, indent=1)
            os.replace(tmp, final)
            logger.info(f"StockScreener DIAG written: {final}")
        except OSError as e:
            logger.warning(f"StockScreener: could not write diagnostics to {target!r}: {e}")

    def _log_live_selection(self, result: List[Dict[str, Any]]) -> None:
        """LIVE-ONLY audit trail of WHICH symbols were selected, and under which thresholds.

        Live and backtest resolve the universe from different sources — live screens here,
        backtests screen the prebuilt metric_store — and nothing recorded the live side, so five
        weeks of live trading left no evidence to compare against. The live logs said only
        "creating SCREENER job", never its output, which is why the 2026-08-06 investigation
        could not settle whether the two paths agree on the same date.

        GRID SAFETY: gated on ``self._as_of is None``. A backtest ALWAYS passes an as_of, so this
        is structurally incapable of firing there — it is not merely "cheap enough". One line per
        live screen, no extra API calls.
        """
        if self._as_of is not None:
            return
        g = self._settings.get
        picked = ",".join(str(r.get("symbol") or "?") for r in result)
        logger.info(
            f"StockScreener LIVE SELECTION: {len(result)} symbols "
            f"[cap>={g('screener_market_cap_min')} rvol>={g('screener_relative_volume_min')} "
            f"stage2only={g('screener_weinstein_stage2_only')} "
            f"drop>={g('screener_price_drop_pct')}/{g('screener_price_drop_days')}d "
            f"max={g('screener_max_stocks')}] -> {picked}"
        )

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_quotes_chunked(
        symbols: List[str], chunk_size: int = 50, max_workers: int = 5,
        failed_chunks: Optional[List[int]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Batch-fetch FMP full quotes in parallel chunks with backoff retry.

        Args:
            symbols: List of ticker symbols to fetch.
            chunk_size: Number of symbols per API call (max 50 for FMP).
            max_workers: Number of parallel HTTP workers.

        Returns:
            Dict mapping uppercase symbol -> quote dict from FMP.
            ``failed_chunks``, when given, receives the index of every chunk that failed.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ba2_providers.fmp_common import fmp_http_get, FMPError

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            logger.warning("StockScreener: FMP_API_KEY not configured, skipping quote fetch")
            return {}

        total = len(symbols)
        chunks = [symbols[i: i + chunk_size] for i in range(0, total, chunk_size)]
        total_chunks = len(chunks)
        log_every = max(1, total_chunks // 5)  # log ~5 times across the run

        result: Dict[str, Dict[str, Any]] = {}
        result_lock = threading.Lock()
        completed_count = 0

        from ba2_providers.fmp_common import fmp_live_cached, _FMP_LIVE_QUOTE_TTL_S

        def fetch_chunk(chunk_idx: int, chunk: List[str]):
            joined = ",".join(chunk)
            try:
                # Shared across screener instances: the quote payload depends only on the
                # symbols, never on this instance's thresholds (those filter it afterwards).
                resp = fmp_live_cached(
                    f"screener:quote:{joined}",
                    lambda: fmp_http_get(
                        f"https://financialmodelingprep.com/api/v3/quote/{joined}",
                        params={"apikey": api_key},
                        endpoint="quote",
                        timeout=15,
                    ),
                    ttl_seconds=_FMP_LIVE_QUOTE_TTL_S,
                )
                data = resp.json()
                if isinstance(data, list):
                    return {
                        (item.get("symbol") or "").upper(): item
                        for item in data
                        if (item.get("symbol") or "").upper()
                    }
            except FMPError as e:
                logger.warning(f"StockScreener: quote chunk {chunk_idx + 1}/{total_chunks} failed after retries: {e}")
                if failed_chunks is not None:
                    failed_chunks.append(chunk_idx)
            except Exception as e:
                logger.warning(f"StockScreener: quote chunk {chunk_idx + 1}/{total_chunks} failed: {e}")
                if failed_chunks is not None:
                    failed_chunks.append(chunk_idx)
            return {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # capture_aware_submit: plain executor.submit for a worker with no
            # capture context; with one active it copies the caller's context into
            # the task so a tap inside the fan-out still records (spec step 2).
            futures = {capture_aware_submit(executor, fetch_chunk, i, chunk): i
                       for i, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                items = future.result()
                with result_lock:
                    result.update(items)
                    completed_count += 1
                    if completed_count % log_every == 0 or completed_count == total_chunks:
                        logger.info(
                            f"StockScreener: quote fetch — "
                            f"{completed_count}/{total_chunks} chunks done "
                            f"({len(result)}/{total} symbols fetched)"
                        )

        logger.debug(f"StockScreener: fetched FMP quotes for {len(result)}/{total} symbols")
        return result

    def _fetch_history_bulk(
        self,
        symbols: List[str],
        lookback_days: int,
        chunk_size: int = 5,
        max_workers: int = 8,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Batch-fetch daily OHLCV from FMP in parallel chunks with backoff retry.

        FMP's /historical-price-full/SYM1,SYM2 endpoint supports
        comma-separated symbols. We chunk to avoid URL-length limits.

        The fetch window is anchored on ``self._as_of`` when set (point-in-time
        reconstruction) and on ``datetime.now()`` otherwise (live). This is an
        instance method (not a staticmethod) so it can read ``self._as_of``; the
        two existing callers already invoke it as ``self._fetch_history_bulk(...)``,
        so no caller change is needed. The ``as_of=None`` path is byte-identical to
        the previous behaviour.

        Returns:
            Dict mapping uppercase symbol -> list of bar dicts
            (oldest-first), each with keys: date, open, high, low, close, volume.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ba2_providers.fmp_common import (
            FMP_LIVE_CACHE_MISS, FMPError, fmp_http_get, fmp_live_cache_enabled,
            fmp_live_cache_get, fmp_live_cache_put,
        )

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            logger.warning("StockScreener: FMP_API_KEY not configured")
            return {}

        # Re-anchor the window on as_of for the reconstructed path; live path uses now.
        anchor = self._as_of or datetime.now(timezone.utc)
        # FETCH WIDE, SERVE NARROW. The cache holds one window per symbol per day
        # (SCREENER_HISTORY_WINDOW_DAYS) and every caller is sliced back to the lookback
        # it asked for, so what each pass SEES is unchanged while what the screen FETCHES
        # collapses from once-per-pass-per-instance to once-per-symbol-per-day.
        # A caller asking for MORE than the shared window keeps its own wider key rather
        # than being served short.
        # LIVE ONLY. In a frozen backtest the memo is inert, so a wider window would be
        # extra bytes bought for nothing -- and a grid mid-run must keep fetching exactly
        # what it fetched before.
        window_days = (max(int(lookback_days), SCREENER_HISTORY_WINDOW_DAYS)
                       if fmp_live_cache_enabled() else int(lookback_days))
        from_date = (anchor - timedelta(days=window_days + 5)).strftime("%Y-%m-%d")
        to_date = anchor.strftime("%Y-%m-%d")
        params_base = {"apikey": api_key, "from": from_date, "to": to_date}
        cutoff = (anchor - timedelta(days=int(lookback_days) + 5)).strftime("%Y-%m-%d")

        def _cache_key(sym: str) -> str:
            return f"screener:ohlcv:{sym.upper()}:{from_date}:{to_date}"

        # WARM FIRST, then batch only what is missing. Asking through fmp_live_cached
        # per symbol would fetch the misses one at a time and turn a cold screen into
        # 1,177 HTTP calls; peeking keeps the chunked endpoint for the cold path.
        result: Dict[str, List[Dict[str, Any]]] = {}
        missing: List[str] = []
        for sym in symbols:
            hit = fmp_live_cache_get(_cache_key(sym))
            if hit is FMP_LIVE_CACHE_MISS:
                missing.append(sym)
            else:
                result[sym.upper()] = hit
        if result:
            logger.info(f"StockScreener: history cache hit for {len(result)}/{len(symbols)} "
                        f"symbol(s); fetching {len(missing)}")
        symbols = missing
        requested_n = len(symbols)
        live = self._as_of is None

        chunks = [symbols[i: i + chunk_size] for i in range(0, len(symbols), chunk_size)]
        total_chunks = len(chunks)
        log_every = max(1, total_chunks // 5)  # log ~5 times across the run

        result_lock = threading.Lock()
        completed_count = 0
        # LIVE-only per-chunk outcomes (written by the worker, keyed by the chunk's symbols).
        outcomes: Dict[tuple, Dict[str, Any]] = {}

        def fetch_chunk(chunk: List[str]):
            joined = ",".join(chunk)
            outcome = {"attempts": 0, "limited": 0, "failed": False}
            if live:
                outcomes[tuple(chunk)] = outcome

            def counting_getter(*a, **kw):
                # Counts every HTTP attempt fmp_http_get makes for this chunk so a retried or
                # rate-limited request is visible. requests.get is resolved at call time.
                import requests
                outcome["attempts"] += 1
                r = requests.get(*a, **kw)
                if getattr(r, "status_code", None) in (429, 500, 502, 503, 504):
                    outcome["limited"] += 1
                return r

            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{joined}"
            try:
                # The bars depend only on (symbols, from, to) -- all three are in the key, so a
                # different as_of or lookback never reuses the wrong window. The thresholds that
                # differ per instance (RVOL / Weinstein / price-drop) are computed FROM this
                # payload afterwards, so every screener instance wants the identical response.
                # The chunk is no longer the cache key -- each symbol in it is cached
                # on its own below, so a differently-composed or differently-ordered
                # chunk still reuses every symbol it shares with an earlier one.
                extra = {"getter": counting_getter} if live else {}
                resp = fmp_http_get(url, params=params_base,
                                    endpoint="historical-price-full", timeout=15, **extra)
                data = resp.json()
            except FMPError as e:
                logger.warning(f"StockScreener: OHLCV chunk failed after retries: {e}")
                outcome["failed"] = True
                return {}
            except Exception as e:
                logger.warning(f"StockScreener: OHLCV chunk failed: {e}")
                outcome["failed"] = True
                return {}

            # Single symbol → {"symbol": ..., "historical": [...]}
            # Multi symbol → {"historicalStockList": [{...}, ...]}
            stock_list = data.get("historicalStockList", [data] if "historical" in data else [])
            chunk_result = {}
            for entry in stock_list:
                sym = (entry.get("symbol") or "").upper()
                bars = entry.get("historical", [])
                # FMP returns newest-first; reverse to oldest-first
                chunk_result[sym] = list(reversed(bars))
                fmp_live_cache_put(_cache_key(sym), chunk_result[sym])
            # A symbol the response omitted entirely is cached as EMPTY: the provider
            # has nothing for it, and re-asking on every pass of every screen is the
            # most expensive way to learn that. It expires with everything else.
            for sym in chunk:
                if sym.upper() not in chunk_result:
                    fmp_live_cache_put(_cache_key(sym), [])
            return chunk_result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {capture_aware_submit(executor, fetch_chunk, chunk): i
                       for i, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                chunk_result = future.result()
                with result_lock:
                    result.update(chunk_result)
                    completed_count += 1
                    if completed_count % log_every == 0 or completed_count == total_chunks:
                        logger.info(
                            f"StockScreener: history fetch — "
                            f"{completed_count}/{total_chunks} chunks done "
                            f"({len(result)}/{len(symbols)} symbols fetched)"
                        )

        if live:
            self._record_history_outcomes(chunks, outcomes, result, requested_n)

        logger.debug(
            f"StockScreener: bulk OHLCV fetched {len(result)}/{len(symbols)} "
            f"symbols ({from_date} to {to_date})"
        )
        # SERVE NARROW. The cache holds the wide window; every caller gets exactly the
        # lookback it asked for, so no pass sees bars it did not request and the RVOL
        # and price-drop windows are the ones they always were.
        if window_days != int(lookback_days):
            result = {sym: [b for b in bars if (b.get("date") or "") >= cutoff]
                      for sym, bars in result.items()}
        return result

    def _record_history_outcomes(self, chunks, outcomes, result, requested_n) -> None:
        """LIVE ONLY, counters only (the refusal thresholds are BT's, in ``_require_history``)."""
        h = self._hist
        failed_chunks = 0
        for chunk in chunks:
            o = outcomes[tuple(chunk)]
            h["requests"] += o["attempts"]
            h["retried_requests"] += max(0, o["attempts"] - 1)
            h["rate_limited_responses"] += o["limited"]
            if o["failed"]:
                failed_chunks += 1
                h["failed_symbols"].extend(s.upper() for s in chunk)
            else:
                h["no_bars_symbols"] += sum(1 for s in chunk if not result.get(s.upper()))
        h["ok_symbols"].update(s for s, bars in result.items() if bars)
        h["calls"] += 1
        h["symbols_requested"] += requested_n
        h["chunks"] += len(chunks)
        h["chunks_failed"] += failed_chunks

    def _quotes_from_bars(
        self, symbols: List[str], window: int = 20
    ) -> Dict[str, Dict[str, Any]]:
        """Build a quote-shaped map (volume/avgVolume/price) from daily bars — the SOLE
        volume/RVOL source for both live and backtest (see ``_enrich_with_rvol``).

        FMP's real-time ``/quote`` "volume" is today's volume-SO-FAR, which is not a fair
        stand-in for a full trading day the way ``avgVolume`` (a full-day average) assumes
        — it is near-zero right at the open and only becomes comparable late in the
        session. Synthesizing the same three keys from daily bars, truncated to
        ``self._as_of`` (via the as_of-anchored :meth:`_fetch_history_bulk`; ``as_of=None``
        anchors on "now", i.e. the live path), gives a stable, time-of-day-independent
        signal that is IDENTICAL whether backtesting or live:

          - ``volume``    = the last COMPLETE session's volume (as of ``self._as_of`` for a
            backtest; yesterday's for live),
          - ``avgVolume`` = trailing mean volume over ~``window`` sessions ending there
            (the point-in-time analogue of FMP's rolling avgVolume),
          - ``price``     = that last bar's close (superseded by a live quote's price when
            ``_enrich_with_rvol`` has one — see there).

        FMP's ``/historical-price-full`` (unlike ``/quote``) was assumed to have no partial
        "today" bar to leak -- WRONG, confirmed empirically: queried during market hours it
        DOES return an in-progress bar dated today, with only the volume traded so far (the
        exact same "cold start" problem this function exists to avoid, one level deeper). A
        bar dated on the anchor's own calendar day is therefore explicitly dropped before
        picking ``bars[-1]``, so "last" always means the last FULLY COMPLETE session.

        ``marketCap`` / ``sharesFloat`` are intentionally omitted so the downstream loop
        only overwrites them from a live quote (the ``q_mcap``/``q_float`` updates only
        fire when the key is present) — this function never has an opinion on them.
        """
        # window + a small buffer for the lookback window passed to _fetch_history_bulk
        history_map = self._fetch_history_bulk(symbols, lookback_days=window + 10)
        anchor_date = (self._as_of or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        quotes: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            bars = history_map.get(sym.upper()) or history_map.get(sym) or []
            today_bar = bool(bars) and bars[-1].get("date") == anchor_date
            if self._diag is not None and bars:
                self._diag["history_windows"][sym.upper()] = {
                    "bars": len(bars), "first_bar_date": bars[0].get("date"),
                    "last_bar_date": bars[-1].get("date"),
                    "today_bar_present": today_bar,
                    "today_bar_close": bars[-1].get("close") if today_bar else None,
                    "anchor_date": anchor_date,
                }
            if today_bar:
                bars = bars[:-1]  # drop today's in-progress bar (partial volume)
            if not bars:
                continue
            last = bars[-1]
            last_vol = last.get("volume") or 0
            window_bars = bars[-window:] if window < len(bars) else bars
            vols = [b.get("volume") for b in window_bars if b.get("volume") is not None]
            avg_vol = round(sum(vols) / len(vols), 2) if vols else 0.0
            quotes[sym.upper()] = {
                "symbol": sym.upper(),
                "volume": last_vol,
                "avgVolume": avg_vol,
                "price": last.get("close"),
            }
        logger.debug(
            f"StockScreener: built {len(quotes)}/{len(symbols)} as-of quotes from bars "
            f"(as_of={self._as_of})"
        )
        return quotes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_provider_filters(self) -> Dict[str, Any]:
        """
        Build the filter dict to pass to the screener provider.

        Values of 0 mean "disabled" and are omitted.
        """
        filters: Dict[str, Any] = {}

        # Price
        price_min = self._settings["screener_price_min"]
        if price_min > 0:
            filters["price_min"] = price_min

        price_max = self._settings["screener_price_max"]
        if price_max > 0:
            filters["price_max"] = price_max

        # Volume floor/ceiling. LIVE: NOT sent to the vendor -- its ``volumeMoreThan`` tests the
        # CURRENT session's volume so far (near zero at the 09:30 open), not an average; both are
        # applied in ``_enrich_with_rvol`` on the average volume of the last finished sessions.
        # AS_OF: unchanged -- FMPHistoricalScreenerProvider applies ``volume_min`` itself.
        if self._as_of is not None:
            volume_min = self._settings["screener_volume_min"]
            if volume_min > 0:
                filters["volume_min"] = volume_min

        # Market cap
        mcap_min = self._settings["screener_market_cap_min"]
        if mcap_min > 0:
            filters["market_cap_min"] = mcap_min

        mcap_max = self._settings["screener_market_cap_max"]
        if mcap_max > 0:
            filters["market_cap_max"] = mcap_max

        # Float bounds are NOT sent to the vendor either (its screener has no float parameter);
        # LIVE ``screen`` applies them as stage 1b from the bulk float table. AS_OF: unchanged.
        if self._as_of is not None:
            float_max = self._settings["screener_float_max"]
            if float_max > 0:
                filters["float_max"] = float_max

        # Restrict to US exchanges — all BA2 broker accounts are US (Alpaca), so
        # foreign listings (e.g. *.TO Toronto) are never tradable. US-listed ADRs
        # (TSM, ASML, NVS, ...) trade on NASDAQ/NYSE and are kept.
        filters["exchanges"] = ["NASDAQ", "NYSE", "AMEX"]

        # Request a high limit to avoid FMP's default cap of 1000
        filters["limit"] = 10_000

        return filters

    def _require_history(
        self, symbols: List[str], quotes_map: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """LIVE: make a total (or large) history failure impossible to miss.

        If more than ``SCREENER_DATA_FAILURE_MAX_FRACTION`` of ``symbols`` have no bars, fetch
        those once more (failed chunks are not cached, so this is a real second attempt) and
        recount. If NO symbol has bars, raise: every later stage (price drop, Weinstein) reads the
        same bars, so an all-failed fetch must not flow on as "nothing passed the filters".
        """
        missing = [s for s in symbols if s not in quotes_map]
        if missing and len(missing) / len(symbols) > SCREENER_DATA_FAILURE_MAX_FRACTION:
            logger.warning(
                f"StockScreener: no price history for {len(missing)}/{len(symbols)} candidates "
                f"(limit {SCREENER_DATA_FAILURE_MAX_FRACTION:.0%}); re-fetching them once"
            )
            quotes_map = {**quotes_map, **self._quotes_from_bars(missing)}
            missing = [s for s in symbols if s not in quotes_map]
        if symbols and len(missing) == len(symbols):
            raise ScreenerDataError(
                f"StockScreener: no price history for any of the {len(symbols)} candidates "
                f"(first 8: {symbols[:8]}); FMP history failed or was rate limited."
            )
        return quotes_map

    def _require_any_history(self, history_map: Dict[str, List[Dict[str, Any]]], symbols: List[str],
                             stage: str) -> None:
        """LIVE: the price-drop / Weinstein stages must not run on a total history failure."""
        if self._as_of is None and symbols and not any(
                history_map.get(s.upper()) or history_map.get(s) for s in symbols):
            raise ScreenerDataError(
                f"StockScreener: {stage} stage found no price history for any of the "
                f"{len(symbols)} candidates (first 8: {symbols[:8]}); FMP history failed."
            )

    def _enrich_with_rvol(
        self,
        candidates: List[Dict[str, Any]],
        min_rvol: float,
    ) -> tuple:
        """
        Enrich candidates with volume/RVOL + a live price/market-cap refresh, and apply the
        client-side volume filters the vendor screener cannot express:

          * RVOL:       rvol = last finished session volume / avg_volume; dropped when
                        ``min_rvol > 0 and rvol < min_rvol`` (``min_rvol == 0`` = filter off);
          * volume_min: dropped when ``avg_volume < screener_volume_min`` (0 = off; no bars = dropped);
          * volume_max: dropped when ``avg_volume > screener_volume_max`` (0 = off).

        ``avg_volume`` = mean volume of the last <= 20 FINISHED sessions (including the last
        one), from the daily bars (``_quotes_from_bars``). The stage always runs: the live
        price/market-cap refresh happens whatever ``min_rvol`` is.
        """
        all_symbols = [
            c["symbol"].upper()
            for c in candidates
            if c.get("symbol")
        ]
        if not all_symbols:
            return [], {"dropped_rvol": 0, "dropped_float": 0, "dropped_volume_max": 0,
                        **({"dropped_volume_min": 0, "dropped_no_history": 0} if self._as_of is None else {})}

        # volume/avgVolume/RVOL ALWAYS come from daily bars (the last COMPLETE trading
        # session's full-day volume vs. a trailing 20-day average) — live and backtest
        # alike. This is what the backtest was optimized against, and it's the only
        # correct choice: FMP's real-time /quote "volume" is today's volume-SO-FAR, which
        # resets to ~0 at the open and only becomes comparable to a full-day average once
        # most of the session has elapsed. A scan running at/near market open would divide
        # by a near-zero numerator and reject nearly every candidate on RVOL regardless of
        # what's actually happening in the stock — confirmed live on 2026-07-13: the 09:30
        # ET scan (fixed to fire at the correct time that same day) returned 0/133
        # candidates after the RVOL filter. Bars give a stable, time-of-day-independent
        # signal that is IDENTICAL whether as_of is set (backtest) or None (live) — see
        # _fetch_history_bulk's anchor logic.
        quotes_map = self._quotes_from_bars(all_symbols)
        if self._as_of is None:
            quotes_map = self._require_history(all_symbols, quotes_map)

        # Price / market cap ARE legitimately live-sensitive (a stock's price is a real,
        # meaningful number the instant the market opens — nothing to "warm up" the way
        # cumulative volume does) and float is a near-static company attribute, so still
        # refresh those three from the real-time quote when not backtesting.
        if self._as_of is None:
            failed_quote_chunks: List[int] = []
            live_quotes_map = self._fetch_quotes_chunked(
                all_symbols, failed_chunks=failed_quote_chunks)
            self._quote_chunks_failed += len(failed_quote_chunks)
        else:
            live_quotes_map = {}

        live = self._as_of is None
        volume_min = self._settings["screener_volume_min"]
        volume_max = self._settings["screener_volume_max"]
        float_min = self._settings["screener_float_min"]   # as_of path only (see below)
        needs_bars = min_rvol > 0 or volume_min > 0 or volume_max > 0

        dropped_rvol = 0
        dropped_float = 0
        dropped_volume_min = 0
        dropped_volume_max = 0
        no_history: List[str] = []

        enriched: List[Dict[str, Any]] = []
        for c in candidates:
            sym = (c.get("symbol") or "").upper()
            # LIVE: a symbol with no finished-session bars cannot be checked against ANY volume
            # bound. Drop it under its own label (never "low volume": a failed history chunk would
            # otherwise empty a screen under a misleading reason) -- see the loud check below.
            if live and needs_bars and sym not in quotes_map:
                no_history.append(sym)
                continue
            quote = quotes_map.get(sym, {})
            live_quote = live_quotes_map.get(sym, {})

            # Update volume from the daily-bar-derived quote
            volume = quote.get("volume") or c.get("volume") or 0
            avg_vol = quote.get("avgVolume", 0) or 0
            rvol = round(volume / avg_vol, 2) if avg_vol > 0 else 0.0

            c["volume"] = volume
            c["avg_volume"] = avg_vol
            c["relative_volume"] = rvol

            # Update price from the LIVE quote when available (else the bar-derived close)
            q_price = live_quote.get("price") or quote.get("price")
            price_source = None
            if q_price and q_price > 0:
                c["price"] = q_price
                price_source = "live_quote" if live_quote.get("price") else "bar_close"

            # Update market_cap from the live quote if available
            q_mcap = live_quote.get("marketCap")
            if q_mcap and q_mcap > 0:
                c["market_cap"] = q_mcap

            # (Float is NOT refreshed here: /quote has no float field. LIVE: stage 1b owns it.)

            # --- Client-side filters ---

            if min_rvol > 0 and rvol < min_rvol:
                logger.debug(f"StockScreener: dropping {sym} — RVOL {rvol} < {min_rvol}")
                dropped_rvol += 1
                continue

            if live:
                # Average-volume floor / ceiling (mean of the last <= 20 finished sessions).
                if volume_min > 0 and avg_vol < volume_min:
                    logger.debug(
                        f"StockScreener: dropping {sym} — avg volume {avg_vol:,.0f} < {volume_min:,}"
                    )
                    dropped_volume_min += 1
                    continue
                if volume_max > 0 and avg_vol > volume_max:
                    logger.debug(
                        f"StockScreener: dropping {sym} — avg volume {avg_vol:,.0f} > {volume_max:,}"
                    )
                    dropped_volume_max += 1
                    continue
            else:
                # AS_OF path: byte-for-byte the pre-2026-10-08 behaviour.
                # float_min: 0 means data unavailable, don't filter those out
                if float_min > 0:
                    stock_float = c.get("float_shares") or 0
                    if stock_float > 0 and stock_float < float_min:
                        logger.debug(
                            f"StockScreener: dropping {sym} — float {stock_float:,} < {float_min:,}"
                        )
                        dropped_float += 1
                        continue

                if volume_max > 0:
                    if volume > volume_max:
                        logger.debug(
                            f"StockScreener: dropping {sym} — volume {volume:,} > {volume_max:,}"
                        )
                        dropped_volume_max += 1
                        continue

            enriched.append(c)
            if self._diag is not None:
                self._diag["candidates"][sym] = {
                    "symbol": sym, "price": c.get("price"), "price_source": price_source,
                    "market_cap": c.get("market_cap"),
                    "market_cap_source": "live_quote" if (live_quote.get("marketCap") or 0) > 0
                    else "vendor_row",
                    "quote_timestamp": live_quote.get("timestamp"),
                    "avg_volume": avg_vol, "volume": volume, "relative_volume": rvol,
                    "history": self._diag["history_windows"].get(sym),
                }

        stats = {
            "dropped_rvol": dropped_rvol,
            "dropped_float": dropped_float,
            "dropped_volume_max": dropped_volume_max,
        }
        if live:
            stats["dropped_volume_min"] = dropped_volume_min
            stats["dropped_no_history"] = len(no_history)
            # Observable headroom of the rule below, for EVERY live screen (the screener does not
            # know its instance; the band + thresholds identify it in the log).
            logger.info(
                f"StockScreener: dropped_no_history={len(no_history)}/{len(all_symbols)} "
                f"(limit {SCREENER_DATA_FAILURE_MAX_FRACTION:.0%}; needs_bars={needs_bars}; "
                f"cap {self._settings['screener_market_cap_min']}-{self._settings['screener_market_cap_max']} "
                f"rvol>={min_rvol} vol>={volume_min}/<={volume_max})"
            )
            if no_history and len(no_history) / len(all_symbols) > SCREENER_DATA_FAILURE_MAX_FRACTION:
                raise ScreenerDataError(
                    f"StockScreener: no price history for {len(no_history)}/{len(all_symbols)} "
                    f"candidate(s) (limit {SCREENER_DATA_FAILURE_MAX_FRACTION:.0%}); first 8: "
                    f"{no_history[:8]}. FMP history likely failed or was rate limited; refusing to "
                    f"return a silently smaller list."
                )
        return enriched, stats

    def _filter_by_price_drop(
        self,
        candidates: List[Dict[str, Any]],
        min_drop_pct: float,
        max_results: int,
    ) -> tuple:
        """
        Pre-fetch price history for all candidates in one parallel batch,
        then filter in memory for stocks with a sufficient recent price drop.

        Args:
            candidates: Ranked candidates (best first for early-stop).
            min_drop_pct: Minimum percentage drop required (0 = accept all).
            max_results: Stop collecting once this many stocks pass.

        Returns:
            Tuple of (filtered list, stats dict).
        """
        lookback_days = self._settings["screener_price_drop_days"]
        total = len(candidates)

        all_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
        history_map = self._fetch_history_bulk(all_symbols, lookback_days)
        self._require_any_history(history_map, all_symbols, "price-drop")
        logger.info(
            f"StockScreener: history fetched for {len(history_map)}/{total} symbols — filtering..."
        )

        passed: List[Dict[str, Any]] = []
        dropped_price_drop = 0
        checked = 0

        for c in candidates:
            if len(passed) >= max_results:
                break

            symbol = (c.get("symbol") or "").upper()
            bars = history_map.get(symbol, [])

            if not bars:
                logger.debug(f"StockScreener: no bars for {symbol}")
                if self._diag is not None:
                    self._diag["price_drop"].append({"symbol": symbol, "no_bars": True})
                continue

            checked += 1

            # Find peak price over the lookback window
            lookback_bars = bars[-lookback_days:] if lookback_days < len(bars) else bars
            peak_price = max(
                max(b.get("high") or 0, b.get("low") or 0)
                for b in lookback_bars
            )

            # Use live price from quote enrichment, fall back to last bar's close
            current_price = c.get("price") or bars[-1].get("close")

            if peak_price <= 0 or current_price is None:
                if self._diag is not None:
                    self._diag["price_drop"].append({
                        "symbol": symbol, "peak": peak_price, "current_price": current_price,
                        "skipped": "no usable peak/price"})
                continue

            drop_pct = round(((peak_price - current_price) / peak_price) * 100, 2)
            c["price_drop_pct"] = drop_pct
            if self._diag is not None:
                self._diag["price_drop"].append({
                    "symbol": symbol, "peak": peak_price, "current_price": current_price,
                    "price_source": "candidate" if c.get("price") else "last_bar_close",
                    "drop_pct": drop_pct, "min_drop_pct": min_drop_pct,
                    "lookback_bars": len(lookback_bars), "passed": drop_pct >= min_drop_pct})

            if drop_pct >= min_drop_pct:
                passed.append(c)
            else:
                dropped_price_drop += 1
                logger.debug(
                    f"StockScreener: dropping {symbol} — price drop {drop_pct}% < {min_drop_pct}%"
                )

        logger.info(
            f"StockScreener: price-drop filter checked {checked}/{total} symbols, "
            f"{len(passed)} passed"
        )
        stats = {
            "price_drop_checked": checked,
            "dropped_price_drop": dropped_price_drop,
        }
        return passed, stats

    def _filter_by_weinstein_stage2(self, candidates: List[Dict[str, Any]]) -> tuple:
        """Keep only candidates in Weinstein Stage 2 (price above a rising 30-week SMA).

        Fetches ~220 calendar days of daily history (enough for the 150-day /
        30-week SMA plus slope lookback) in one parallel batch, then classifies
        each symbol. Annotates survivors with weinstein_stage / weinstein_slope_pct.
        """
        from ba2_common.core.weinstein import classify_weinstein_stage

        # 30-week SMA (150 sessions) + 4-week slope (20) -> ~170 sessions -> ~240 cal days.
        lookback_days = 250
        all_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
        history_map = self._fetch_history_bulk(all_symbols, lookback_days)
        self._require_any_history(history_map, all_symbols, "Weinstein")

        passed: List[Dict[str, Any]] = []
        checked = 0
        dropped = 0
        for c in candidates:
            symbol = (c.get("symbol") or "").upper()
            bars = history_map.get(symbol, [])
            if not bars:
                continue
            checked += 1
            closes = [b.get("close") for b in bars if b.get("close") is not None]
            res = classify_weinstein_stage(closes)
            if res.get("stage") == 2:
                c["weinstein_stage"] = 2
                c["weinstein_slope_pct"] = res.get("slope_pct")
                passed.append(c)
            else:
                dropped += 1
                logger.debug(
                    f"StockScreener: {symbol} not Stage 2 "
                    f"(stage={res.get('stage')}, {res.get('reason','')})"
                )

        logger.info(
            f"StockScreener: Weinstein checked {checked}/{len(candidates)} symbols, "
            f"{len(passed)} in Stage 2"
        )
        return passed, {"weinstein_checked": checked, "weinstein_dropped": dropped,
                        "weinstein_stage2": len(passed)}

    def _rank(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Sort candidates by the configured metric (descending).

        Supported metrics:
            market_cap, volume, float_shares, relative_volume, composite.

        Composite = market_cap * volume * float_shares (normalised via
        product so larger values rank higher).
        """
        return sorted(candidates, key=self._sort_key_fn(), reverse=True)

    def _sort_key_fn(self):
        """The rank key of the configured metric (shared by ``_rank`` and the diagnostics)."""
        metric = self._settings["screener_sort_metric"]

        if metric == "composite":
            def sort_key(c: Dict[str, Any]) -> float:
                mcap = c.get("market_cap") or 0
                vol = c.get("volume") or 0
                flt = c.get("float_shares") or 1
                return mcap * vol * flt
        elif metric == "price_drop_pct":
            def sort_key(c: Dict[str, Any]) -> float:
                return c.get("price_drop_pct") or 0
        else:
            def sort_key(c: Dict[str, Any]) -> float:
                val = c.get(metric)
                if val is None:
                    return 0
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return 0

        return sort_key

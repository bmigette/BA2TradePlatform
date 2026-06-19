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

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ba2_common.config import get_app_setting
from ba2_common.logger import logger


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

    def screen(self) -> Dict[str, Any]:
        """
        Execute the full screen/enrich/rank pipeline.

        Returns:
            Dict with keys:
                results: Sorted list of stock dicts, length <= screener_max_stocks.
                stats: Dict of per-filter drop counts and totals.
        """
        from ba2_providers import get_provider

        stats: Dict[str, int] = {}

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
        candidates = screener.screen_stocks(filters, as_of=self._as_of)
        stats["screener_candidates"] = len(candidates)
        logger.info(
            f"StockScreener: stage 1 done — {len(candidates)} candidates returned"
        )

        if not candidates:
            self._report_progress("No candidates found.", 1.0)
            return {"results": [], "stats": stats}

        # --- Stage 2: RVOL enrichment + client-side filters ---
        rvol_min = self._settings["screener_relative_volume_min"]
        if rvol_min > 0:
            logger.info(
                f"StockScreener: stage 2 — RVOL enrichment on {len(candidates)} candidates "
                f"(min RVOL={rvol_min})"
            )
            self._report_progress(
                f"Fetching live prices for {len(candidates)} candidates (RVOL)...", 0.2
            )
            candidates, enrich_stats = self._enrich_with_rvol(candidates, rvol_min)
            stats.update(enrich_stats)
            logger.info(
                f"StockScreener: stage 2 done — {len(candidates)} candidates after RVOL filter"
            )

        if not candidates:
            self._report_progress("No candidates after RVOL filter.", 1.0)
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
                logger.info(
                    f"StockScreener: stage 4 done — {len(result)} stocks passed price-drop filter"
                )
            else:
                result = ranked[:max_stocks]

        stats["final_count"] = len(result)
        self._report_progress(f"Done — {len(result)} stock(s) matched.", 1.0)
        logger.info(
            f"StockScreener: pipeline complete — {len(result)} stocks "
            f"(sorted by {self._settings['screener_sort_metric']})"
        )
        return {"results": result, "stats": stats}

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_quotes_chunked(
        symbols: List[str], chunk_size: int = 50, max_workers: int = 5
    ) -> Dict[str, Dict[str, Any]]:
        """
        Batch-fetch FMP full quotes in parallel chunks with backoff retry.

        Args:
            symbols: List of ticker symbols to fetch.
            chunk_size: Number of symbols per API call (max 50 for FMP).
            max_workers: Number of parallel HTTP workers.

        Returns:
            Dict mapping uppercase symbol -> quote dict from FMP.
        """
        import threading
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

        def fetch_chunk(chunk_idx: int, chunk: List[str]):
            joined = ",".join(chunk)
            try:
                resp = fmp_http_get(
                    f"https://financialmodelingprep.com/api/v3/quote/{joined}",
                    params={"apikey": api_key},
                    endpoint="quote",
                    timeout=15,
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
            except Exception as e:
                logger.warning(f"StockScreener: quote chunk {chunk_idx + 1}/{total_chunks} failed: {e}")
            return {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_chunk, i, chunk): i for i, chunk in enumerate(chunks)}
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
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ba2_providers.fmp_common import fmp_http_get, FMPError

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            logger.warning("StockScreener: FMP_API_KEY not configured")
            return {}

        # Re-anchor the window on as_of for the reconstructed path; live path uses now.
        anchor = self._as_of or datetime.now(timezone.utc)
        from_date = (anchor - timedelta(days=lookback_days + 5)).strftime("%Y-%m-%d")
        to_date = anchor.strftime("%Y-%m-%d")
        params_base = {"apikey": api_key, "from": from_date, "to": to_date}

        chunks = [symbols[i: i + chunk_size] for i in range(0, len(symbols), chunk_size)]
        total_chunks = len(chunks)
        log_every = max(1, total_chunks // 5)  # log ~5 times across the run

        result: Dict[str, List[Dict[str, Any]]] = {}
        result_lock = threading.Lock()
        completed_count = 0

        def fetch_chunk(chunk: List[str]):
            joined = ",".join(chunk)
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{joined}"
            try:
                resp = fmp_http_get(url, params=params_base, endpoint="historical-price-full", timeout=15)
                data = resp.json()
            except FMPError as e:
                logger.warning(f"StockScreener: OHLCV chunk failed after retries: {e}")
                return {}
            except Exception as e:
                logger.warning(f"StockScreener: OHLCV chunk failed: {e}")
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
            return chunk_result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_chunk, chunk): i for i, chunk in enumerate(chunks)}
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

        logger.debug(
            f"StockScreener: bulk OHLCV fetched {len(result)}/{len(symbols)} "
            f"symbols ({from_date} to {to_date})"
        )
        return result

    def _quotes_from_bars(
        self, symbols: List[str], window: int = 20
    ) -> Dict[str, Dict[str, Any]]:
        """Build a quote-shaped map from as-of bars for the historical RVOL path.

        The live RVOL path reads ``volume`` / ``avgVolume`` / ``price`` off the FMP
        ``/quote`` payload. ``/quote`` has no temporal parameter, so for the as_of
        reconstruction we synthesize the same three keys from daily bars truncated to
        ``self._as_of`` (via the as_of-anchored :meth:`_fetch_history_bulk`):

          - ``volume``    = the as-of (last) bar's volume,
          - ``avgVolume`` = trailing mean volume over ~``window`` sessions ending at
            ``as_of`` (the point-in-time analogue of FMP's rolling avgVolume),
          - ``price``     = the as-of (last) bar's close.

        ``marketCap`` / ``sharesFloat`` are intentionally omitted so the downstream
        loop keeps the reconstructed market_cap (the q_mcap update only fires when the
        key is present). This makes the historical RVOL fully point-in-time while the
        per-candidate enrichment loop reads the SAME keys as the live path — the LOGIC
        never forks, only the data source.
        """
        # window + a small buffer for the lookback window passed to _fetch_history_bulk
        history_map = self._fetch_history_bulk(symbols, lookback_days=window + 10)
        quotes: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            bars = history_map.get(sym.upper()) or history_map.get(sym) or []
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

        # Volume
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

        # Float max (the provider supports float_max but not float_min)
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

    def _enrich_with_rvol(
        self,
        candidates: List[Dict[str, Any]],
        min_rvol: float,
    ) -> tuple:
        """
        Enrich candidates with FMP quote data, compute relative volume,
        and apply client-side filters that the screener API does not support
        (float_min, volume_max).
        """
        all_symbols = [
            c["symbol"].upper()
            for c in candidates
            if c.get("symbol")
        ]
        if not all_symbols:
            return [], {"dropped_rvol": 0, "dropped_float": 0, "dropped_volume_max": 0}

        # RVOL source: live FMP quotes on the live path (UNCHANGED); on the as_of
        # path the live /quote endpoint has no temporal equivalent, so derive a
        # quote-shaped map from as-of bars (last-bar volume vs. trailing average) —
        # fully point-in-time. The per-candidate loop below reads from this map by
        # the SAME keys (volume/avgVolume/price), so its logic never forks.
        if self._as_of is not None:
            quotes_map = self._quotes_from_bars(all_symbols)
        else:
            quotes_map = self._fetch_quotes_chunked(all_symbols)

        float_min = self._settings["screener_float_min"]
        volume_max = self._settings["screener_volume_max"]

        dropped_rvol = 0
        dropped_float = 0
        dropped_volume_max = 0

        enriched: List[Dict[str, Any]] = []
        for c in candidates:
            sym = (c.get("symbol") or "").upper()
            quote = quotes_map.get(sym, {})

            # Update volume from quote
            volume = quote.get("volume") or c.get("volume") or 0
            avg_vol = quote.get("avgVolume", 0) or 0
            rvol = round(volume / avg_vol, 2) if avg_vol > 0 else 0.0

            c["volume"] = volume
            c["avg_volume"] = avg_vol
            c["relative_volume"] = rvol

            # Update price from quote if available
            q_price = quote.get("price")
            if q_price and q_price > 0:
                c["price"] = q_price

            # Update market_cap from quote if available
            q_mcap = quote.get("marketCap")
            if q_mcap and q_mcap > 0:
                c["market_cap"] = q_mcap

            # Update float_shares from quote if available
            q_float = quote.get("sharesFloat")
            if q_float and q_float > 0:
                c["float_shares"] = q_float

            # --- Client-side filters ---

            if rvol < min_rvol:
                logger.debug(f"StockScreener: dropping {sym} — RVOL {rvol} < {min_rvol}")
                dropped_rvol += 1
                continue

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

        stats = {
            "dropped_rvol": dropped_rvol,
            "dropped_float": dropped_float,
            "dropped_volume_max": dropped_volume_max,
        }
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
                continue

            drop_pct = round(((peak_price - current_price) / peak_price) * 100, 2)
            c["price_drop_pct"] = drop_pct

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
        metric = self._settings["screener_sort_metric"]

        if metric == "composite":
            def sort_key(c: Dict[str, Any]) -> float:
                mcap = c.get("market_cap") or 0
                vol = c.get("volume") or 0
                flt = c.get("float_shares") or 1
                return mcap * vol * flt
        else:
            def sort_key(c: Dict[str, Any]) -> float:
                val = c.get(metric)
                if val is None:
                    return 0
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return 0

        return sorted(candidates, key=sort_key, reverse=True)

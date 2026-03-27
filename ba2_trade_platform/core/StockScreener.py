"""
Reusable Stock Screener Module

Provides a configurable screen/enrich/rank pipeline for finding stocks
matching user-defined criteria. Designed to be consumed by any expert
(e.g. PennyMomentumTrader, SwingTrader) via composition.

Pipeline stages:
    1. Screen  – call screener provider with basic filters
    2. Enrich  – batch-fetch FMP quotes for RVOL + client-side filters
    3. Filter  – price-drop lookback filter via OHLCV provider
    4. Rank    – sort by chosen metric and return top N
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import get_app_setting
from ..logger import logger


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
        screener_relative_volume_min float 1.5  (0 = disabled)
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
        "screener_relative_volume_min": 1.5,
        "screener_price_drop_pct": 15.0,
        "screener_price_drop_days": 1,
        "screener_max_stocks": 10,
        "screener_sort_metric": "market_cap",
    }

    # Metrics that can be used for ranking
    _VALID_SORT_METRICS = {
        "market_cap",
        "volume",
        "float_shares",
        "relative_volume",
        "composite",
    }

    def __init__(self, settings: Dict[str, Any]):
        """
        Initialise the screener from a settings dict.

        Missing keys fall back to class-level defaults.

        Args:
            settings: Dict of screener settings.
        """
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

    def screen(self) -> List[Dict[str, Any]]:
        """
        Execute the full screen/enrich/rank pipeline.

        Returns:
            Sorted list of stock dicts, length <= screener_max_stocks.
            Each dict contains at minimum: symbol, company_name, price,
            volume, market_cap, sector, industry, exchange, float_shares.
            If RVOL enrichment ran, also: avg_volume, relative_volume.
            If price-drop filter ran, also: price_drop_pct.
        """
        from ..modules.dataproviders import get_provider

        provider_name = self._settings["screener_provider"]
        screener = get_provider("screener", provider_name)

        # --- Stage 1: basic screen via provider ---
        filters = self._build_provider_filters()
        logger.info(
            f"StockScreener: screening via '{provider_name}' with filters: {filters}"
        )
        candidates = screener.screen_stocks(filters)
        logger.info(
            f"StockScreener: screener returned {len(candidates)} candidates"
        )

        if not candidates:
            return []

        # --- Stage 2: RVOL enrichment + client-side filters ---
        rvol_min = self._settings["screener_relative_volume_min"]
        if rvol_min > 0:
            candidates = self._enrich_with_rvol(candidates, rvol_min)
            logger.info(
                f"StockScreener: {len(candidates)} candidates after RVOL "
                f"enrichment (min RVOL={rvol_min})"
            )

        if not candidates:
            return []

        # --- Stage 3: price-drop filter ---
        drop_pct = self._settings["screener_price_drop_pct"]
        drop_days = self._settings["screener_price_drop_days"]
        if drop_pct > 0 and drop_days > 0:
            candidates = self._filter_by_price_drop(candidates, drop_pct)
            logger.info(
                f"StockScreener: {len(candidates)} candidates after "
                f"price-drop filter (>={drop_pct}% over "
                f"{self._settings['screener_price_drop_days']}d)"
            )

        if not candidates:
            return []

        # --- Stage 4: rank and trim ---
        ranked = self._rank(candidates)
        max_stocks = self._settings["screener_max_stocks"]
        result = ranked[:max_stocks]
        logger.info(
            f"StockScreener: returning top {len(result)} stocks "
            f"(sorted by {self._settings['screener_sort_metric']})"
        )
        return result

    # ------------------------------------------------------------------
    # Static helper – extracted from PennyMomentumTrader for reuse
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_quotes_chunked(
        symbols: List[str], chunk_size: int = 50
    ) -> Dict[str, Dict[str, Any]]:
        """
        Batch-fetch FMP full quotes in chunks.

        Args:
            symbols: List of ticker symbols to fetch.
            chunk_size: Number of symbols per API call (max 50 for FMP).

        Returns:
            Dict mapping uppercase symbol -> quote dict from FMP.
        """
        import fmpsdk

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            logger.warning("StockScreener: FMP_API_KEY not configured, skipping quote fetch")
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i: i + chunk_size]
            try:
                data = fmpsdk.quote(apikey=api_key, symbol=chunk)
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        if sym:
                            result[sym] = item
            except Exception as e:
                logger.warning(
                    f"StockScreener: FMP quote chunk {i}-{i + len(chunk)} "
                    f"failed: {e}"
                )
        logger.debug(
            f"StockScreener: fetched FMP quotes for "
            f"{len(result)}/{len(symbols)} symbols"
        )
        return result

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

        return filters

    def _enrich_with_rvol(
        self,
        candidates: List[Dict[str, Any]],
        min_rvol: float,
    ) -> List[Dict[str, Any]]:
        """
        Enrich candidates with FMP quote data, compute relative volume,
        and apply client-side filters that the screener API does not support
        (float_min, volume_max).

        Args:
            candidates: Raw screener results.
            min_rvol: Minimum relative volume threshold (> 0).

        Returns:
            Filtered and enriched list of candidates.
        """
        all_symbols = [
            c["symbol"].upper()
            for c in candidates
            if c.get("symbol")
        ]
        if not all_symbols:
            return []

        quotes_map = self._fetch_quotes_chunked(all_symbols)

        float_min = self._settings["screener_float_min"]
        volume_max = self._settings["screener_volume_max"]

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

            # Filter: minimum RVOL
            if rvol < min_rvol:
                logger.debug(
                    f"StockScreener: dropping {sym} — RVOL {rvol} < {min_rvol}"
                )
                continue

            # Filter: float_min (not supported by FMP screener API)
            if float_min > 0:
                stock_float = c.get("float_shares") or 0
                if stock_float < float_min:
                    logger.debug(
                        f"StockScreener: dropping {sym} — float "
                        f"{stock_float:,} < {float_min:,}"
                    )
                    continue

            # Filter: volume_max (not supported by FMP screener API)
            if volume_max > 0:
                if volume > volume_max:
                    logger.debug(
                        f"StockScreener: dropping {sym} — volume "
                        f"{volume:,} > {volume_max:,}"
                    )
                    continue

            enriched.append(c)

        return enriched

    def _filter_by_price_drop(
        self,
        candidates: List[Dict[str, Any]],
        min_drop_pct: float,
    ) -> List[Dict[str, Any]]:
        """
        Keep only candidates whose price dropped >= min_drop_pct over
        the configured lookback period.

        Args:
            candidates: Enriched screener results.
            min_drop_pct: Minimum percentage drop required.

        Returns:
            Filtered list with price_drop_pct annotated on each dict.
        """
        from ..modules.dataproviders import get_provider

        lookback_days = self._settings["screener_price_drop_days"]
        ohlcv_provider = get_provider("ohlcv", "fmp")
        now = datetime.now(timezone.utc)

        passed: List[Dict[str, Any]] = []
        for c in candidates:
            symbol = c.get("symbol")
            if not symbol:
                continue

            try:
                result = ohlcv_provider.get_ohlcv_data_formatted(
                    symbol=symbol,
                    end_date=now,
                    lookback_days=lookback_days + 5,  # extra margin for weekends/holidays
                    interval="1d",
                    format_type="dict",
                )
                bars = result.get("data", []) if isinstance(result, dict) else []
            except Exception as e:
                logger.warning(
                    f"StockScreener: OHLCV fetch failed for {symbol}: {e}"
                )
                continue

            if len(bars) < 2:
                logger.debug(
                    f"StockScreener: not enough OHLCV bars for {symbol} "
                    f"({len(bars)} bars)"
                )
                continue

            # Use the close from lookback_days ago vs. latest close
            # bars are sorted oldest-first from the provider
            lookback_idx = max(0, len(bars) - 1 - lookback_days)
            old_close = bars[lookback_idx].get("close")
            new_close = bars[-1].get("close")

            if old_close is None or new_close is None:
                logger.debug(
                    f"StockScreener: missing close prices for {symbol}"
                )
                continue
            if old_close <= 0:
                continue

            drop_pct = round(((old_close - new_close) / old_close) * 100, 2)
            c["price_drop_pct"] = drop_pct

            if drop_pct >= min_drop_pct:
                passed.append(c)
            else:
                logger.debug(
                    f"StockScreener: dropping {symbol} — price drop "
                    f"{drop_pct}% < {min_drop_pct}%"
                )

        return passed

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

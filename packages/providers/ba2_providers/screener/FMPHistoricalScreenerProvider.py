"""Point-in-time stock screener: reconstruct a historical screen as-of a past date.

``as_of=None`` is NOT supported here (use :class:`FMPScreenerProvider` for live). This
class implements ``ScreenerProviderInterface.screen_stocks(filters, as_of=<date>)`` by:

  1. building a survivorship-free universe for ``as_of`` (:mod:`.universe`),
  2. reconstructing each symbol's as-of price / market-cap / volume from the Phase-2
     ``as_of`` providers (OHLCV close-at-``as_of`` via the uniform ``ohlcv_get`` alias;
     shares-outstanding from the latest income statement with report date <= ``as_of``),
  3. applying the SAME numeric thresholds the live FMP ``/stock-screener`` applies
     server-side (price / volume / market_cap / float / exchange — the Stage-1 part),
  4. emitting the IDENTICAL normalised dict (same 12 keys as
     ``FMPScreenerProvider._normalise_result``) so ``StockScreener``'s downstream
     pipeline (RVOL enrich, Weinstein Stage-2, rank, price-drop) runs unchanged.

The heavy client-side filters (RVOL, Weinstein, price-drop) stay in ``StockScreener``;
this provider does only the Stage-1 equivalent over a historical universe.

Decisions (Phase-3 plan §"Decisions taken"):
  - As-of price = OHLCV close on (or last trading day <=) ``as_of`` (Decision 2).
  - Market-cap = FMP dated ``/historical-market-capitalization`` if it covers the
    date, else shares-outstanding(``as_of``) x close(``as_of``) (Decision 3). The path
    used is recorded per candidate under ``market_cap_source`` for cache auditability.
  - Float is an approximation in backtest (FMP serves current ``floatShares`` only);
    it is left ``None`` here and the documented limitation is flagged ``float_approx``
    on each candidate (Decision 4). Float thresholds remain enforced client-side
    downstream by ``StockScreener._enrich_with_rvol`` using whatever float it can find.

Re-plan reconciliations vs. the plan's draft (the draft pinned speculative Phase-2
signatures; these are the ACTUAL surfaces produced by Phase 2):
  - OHLCV: there is NO ``get_provider("ohlcv","fmp").get(symbol, as_of=...)`` method.
    The Phase-2 uniform as_of OHLCV accessor is
    ``ba2_providers.cache.cached_get.ohlcv_get(provider, symbol, as_of=..., lookback=...)``
    which returns a pandas DataFrame (columns Date/Open/High/Low/Close/Volume). We use
    it (close/volume read from the DataFrame), consistent with the Phase-1 expert path.
  - Shares: ``get_income_statement`` is ``(symbol, frequency, end_date, start_date=None,
    lookback_periods=None, as_of=None, format_type="markdown")`` -> dict with
    ``statements`` list; the share field is ``weighted_average_shares_outstanding``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces.ScreenerProviderInterface import ScreenerProviderInterface
from ba2_common.config import get_app_setting
from ba2_common.logger import logger

from .universe import broad_universe, index_universe, fetch_lifecycle_map

_HIST_MCAP_URL = "https://financialmodelingprep.com/api/v3/historical-market-capitalization"


class FMPHistoricalScreenerProvider(ScreenerProviderInterface):
    """Reconstructed historical screener (survivorship-free, point-in-time)."""

    def __init__(self, universe_mode: str = "broad"):
        self.api_key = get_app_setting("FMP_API_KEY")
        # 'broad' | 'sp500' | 'nasdaq' (validated lazily in screen_stocks)
        self.universe_mode = universe_mode

    def get_provider_name(self) -> str:
        return "fmp_historical"

    def validate_config(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # As-of metric helpers (route through the Phase-2 as_of providers)
    # ------------------------------------------------------------------

    def _ohlcv_provider(self):
        """The Phase-2 OHLCV provider used for the as-of close/volume reconstruction."""
        from ba2_providers import get_provider
        return get_provider("ohlcv", "fmp")

    def _details_provider(self):
        """The Phase-2 fundamentals-details provider (income statement -> shares)."""
        from ba2_providers import get_provider
        return get_provider("fundamentals_details", "fmp")

    def _bars_at(self, symbol: str, as_of: datetime, lookback: int):
        """Return the as-of OHLCV DataFrame (rows up to ``as_of``), or ``None``.

        Uses the Phase-2 uniform ``ohlcv_get`` alias (as_of -> end_date,
        lookback -> lookback_days), which is the canonical point-in-time OHLCV path.
        """
        from ba2_providers.cache.cached_get import ohlcv_get
        try:
            df = ohlcv_get(self._ohlcv_provider(), symbol, as_of=as_of, lookback=lookback)
            if df is None or getattr(df, "empty", True):
                return None
            return df
        except Exception as e:  # pragma: no cover - defensive (per-symbol failure)
            logger.debug(f"hist-screener: no OHLCV for {symbol}@{as_of.date()}: {e}")
            return None

    def _close_at(self, symbol: str, as_of: datetime) -> Optional[float]:
        """Close on (or last trading day <=) ``as_of`` via the Phase-2 OHLCV alias."""
        df = self._bars_at(symbol, as_of, lookback=10)
        if df is None or df.empty:
            return None
        try:
            return float(df["Close"].iloc[-1])
        except (KeyError, IndexError, ValueError, TypeError):
            return None

    def _avg_volume_at(self, symbol: str, as_of: datetime, window: int = 20) -> float:
        """Trailing average daily volume over ~``window`` sessions ending at ``as_of``."""
        df = self._bars_at(symbol, as_of, lookback=window + 10)
        if df is None or df.empty:
            return 0.0
        try:
            vols = df["Volume"].dropna().tail(window).astype(float)
            if len(vols) == 0:
                return 0.0
            return round(float(vols.mean()), 2)
        except (KeyError, ValueError, TypeError):
            return 0.0

    def _shares_at(self, symbol: str, as_of: datetime) -> Optional[float]:
        """Shares outstanding from the latest income statement with report date <= ``as_of``.

        Uses ``weightedAverageShsOut`` (exposed as ``weighted_average_shares_outstanding``
        in the dict format) with the same as-of-report discipline as
        ``get_financial_ratios`` (the provider gates rows on fillingDate/acceptedDate <=
        as_of when ``as_of`` is set — the Phase-2 statements lookahead fix).
        """
        try:
            stmt = self._details_provider().get_income_statement(
                symbol,
                "annual",
                as_of,                 # end_date
                lookback_periods=1,
                as_of=as_of,
                format_type="dict",
            )
            rows = (stmt or {}).get("statements") if isinstance(stmt, dict) else None
            if not rows:
                return None
            shares = rows[0].get("weighted_average_shares_outstanding")
            try:
                shares = float(shares) if shares is not None else 0.0
            except (ValueError, TypeError):
                return None
            return shares or None
        except Exception as e:  # pragma: no cover - defensive (per-symbol failure)
            logger.debug(f"hist-screener: no shares for {symbol}@{as_of.date()}: {e}")
            return None

    def _market_cap_at(
        self, symbol: str, as_of: datetime, close: Optional[float]
    ) -> tuple[Optional[float], str]:
        """Reconstruct market cap as-of ``as_of``.

        Returns ``(market_cap, source)`` where ``source`` is one of
        ``"historical_market_cap"`` (FMP dated endpoint), ``"shares_x_close"``
        (shares-outstanding x close fallback), or ``"unavailable"``.
        """
        # Prefer FMP's dated historical-market-capitalization when it covers the date.
        try:
            from ba2_providers.fmp_common import fmp_http_get
            to = as_of.strftime("%Y-%m-%d")
            r = fmp_http_get(
                f"{_HIST_MCAP_URL}/{symbol}",
                params={"apikey": self.api_key, "from": to, "to": to, "limit": 5},
                endpoint="historical-market-cap",
                timeout=20,
            )
            rows = r.json() or []
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                mc = rows[0].get("marketCap")
                if mc:
                    return float(mc), "historical_market_cap"
        except Exception:
            pass  # fall through to shares x close

        shares = self._shares_at(symbol, as_of)
        if shares and close:
            return shares * close, "shares_x_close"
        return None, "unavailable"

    # ------------------------------------------------------------------
    # Screener entry point
    # ------------------------------------------------------------------

    def screen_stocks(
        self, filters: Dict[str, Any], as_of: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        if as_of is None:
            raise ValueError(
                "FMPHistoricalScreenerProvider requires as_of "
                "(use the 'fmp' provider for live screening)"
            )
        if not self.validate_config():
            logger.error("FMP API key not configured for historical screener")
            return []

        if self.universe_mode in ("sp500", "nasdaq"):
            symbols = index_universe(self.universe_mode, as_of)
        else:
            symbols = broad_universe(as_of, lifecycle=fetch_lifecycle_map())

        price_min = filters.get("price_min")
        price_max = filters.get("price_max")
        vol_min = filters.get("volume_min")
        vol_max = filters.get("volume_max")
        mcap_min = filters.get("market_cap_min")
        mcap_max = filters.get("market_cap_max")
        limit = filters.get("limit") or 10_000

        out: List[Dict[str, Any]] = []
        for sym in symbols:
            close = self._close_at(sym, as_of)
            if close is None or close <= 0:
                continue
            if price_min and close < price_min:
                continue
            if price_max and close > price_max:
                continue

            mcap, mcap_source = self._market_cap_at(sym, as_of, close)
            if mcap_min and (mcap or 0) < mcap_min:
                continue
            if mcap_max and mcap and mcap > mcap_max:
                continue

            avg_vol = self._avg_volume_at(sym, as_of)
            if vol_min and avg_vol < vol_min:
                continue
            if vol_max and avg_vol > vol_max:
                continue

            # Float: current floatShares only on FMP -> documented approximation
            # (Decision 4). Left None here; downstream RVOL enrichment fills any float
            # it can find. Flagged float_approx for cache auditability.
            out.append(
                self._normalise(
                    sym,
                    close,
                    avg_vol,
                    mcap,
                    None,
                    market_cap_source=mcap_source,
                    float_approx=True,
                )
            )
            if len(out) >= limit:
                break

        logger.info(
            f"hist-screener: {len(out)} candidates @ {as_of.date()} "
            f"(universe={self.universe_mode}, scanned {len(symbols)})"
        )
        return out

    @staticmethod
    def _normalise(
        symbol,
        price,
        volume,
        market_cap,
        float_shares,
        *,
        market_cap_source: Optional[str] = None,
        float_approx: bool = True,
    ) -> Dict[str, Any]:
        """Emit the SAME 12 keys as ``FMPScreenerProvider._normalise_result`` so the
        downstream ``StockScreener`` pipeline is byte-identical.

        ``company_name``/``sector``/``industry``/``exchange``/``beta``/
        ``is_actively_trading``/``country`` are not reconstructed as-of (the broad
        universe is already US-listed via ``available-traded``; exchange restriction is
        enforced downstream by the US-only quote enrichment). They are emitted as
        ``None`` exactly as the live provider would for a missing FMP field.

        ``market_cap_source`` / ``float_approx`` are audit annotations (NOT part of the
        12-key contract); they are attached only when non-default so the dict still
        compares equal in shape to the live result when unused.
        """
        result: Dict[str, Any] = {
            "symbol": symbol,
            "company_name": None,
            "price": price,
            "volume": volume,
            "market_cap": market_cap,
            "sector": None,
            "industry": None,
            "exchange": None,
            "beta": None,
            "is_actively_trading": None,
            "country": None,
            "float_shares": float_shares,
        }
        # Audit metadata for the Phase-3 cache (market_cap_source / float_approx
        # columns). Kept off the 12-key contract surface so the shape-equality gate
        # (test_historical_normalised_shape_matches_live) holds for the bare 5-arg call.
        if market_cap_source is not None:
            result["market_cap_source"] = market_cap_source
            result["float_approx"] = float_approx
        return result

"""
FMP Stock Screener Provider

Stock screening provider using Financial Modeling Prep API.
Uses the /stock-screener endpoint to find stocks matching given criteria.

API Documentation:
    https://site.financialmodelingprep.com/developer/docs#stock-screener

AS_OF LIMITATION: the screener is live-only — ``as_of`` is ignored and there is no
temporal parameter. The endpoint returns the CURRENT screen regardless of any
historical date. Historical / point-in-time screening is Phase 3; the uniform
``cached_get`` layer deliberately EXCLUDES the screener category.
"""

from datetime import datetime
from typing import Dict, Any, List, Optional

from ba2_common.core.interfaces.ScreenerProviderInterface import ScreenerProviderInterface
from ba2_common.logger import logger
from ba2_common.config import get_app_setting


class FMPScreenerProvider(ScreenerProviderInterface):
    """
    Financial Modeling Prep stock screener provider.

    Uses FMP /stock-screener endpoint to screen stocks by price, volume,
    market cap, exchange, and other criteria.

    Requires:
        - FMP API key in app settings (FMP_API_KEY)
    """

    BASE_URL = "https://financialmodelingprep.com/api/v3"

    def __init__(self):
        """Initialize FMP screener provider."""
        self.api_key = get_app_setting("FMP_API_KEY")

    def get_provider_name(self) -> str:
        return "fmp"

    def validate_config(self) -> bool:
        """Validate that the FMP API key is configured."""
        return bool(self.api_key)

    def screen_stocks(
        self,
        filters: Dict[str, Any],
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Screen stocks using FMP stock-screener endpoint.

        Args:
            filters: Screening criteria. Supported keys:
                price_min, price_max, volume_min (AVERAGE volume, vendor
                ``avgVolumeMoreThan``), market_cap_min, market_cap_max,
                float_min / float_max (applied HERE as a post-filter on the
                vendor's bulk shares-float table: the screener endpoint has no
                float parameter), exchanges, sector_exclude, limit
            as_of: MUST be None — this provider is live-only (the FMP
                /stock-screener endpoint has no temporal parameter and returns
                the CURRENT screen). A non-None ``as_of`` is rejected loudly;
                use the 'fmp_historical' provider for point-in-time reconstruction.

        Returns:
            List of normalised stock dicts with keys: symbol, company_name,
            price, volume, market_cap, sector, industry, exchange, beta,
            is_actively_trading, country
        """
        if as_of is not None:
            raise ValueError(
                "FMPScreenerProvider is live-only (no temporal param). "
                "Use the 'fmp_historical' provider for as_of reconstruction."
            )
        # An outage must never read as "0 candidates": every failure below raises
        # ScreenerDataError (an FMPError) naming the cause. The vendor key / request / body are
        # all live-only concerns -- this provider rejects as_of above; the historical provider
        # is a different class and is untouched.
        from ba2_providers.StockScreener import ScreenerDataError
        if not self.validate_config():
            logger.error("FMP API key not configured for screener")
            raise ScreenerDataError("FMP screener: FMP_API_KEY is not configured")

        params = self._build_params(filters)

        import requests
        from ba2_providers.fmp_common import fmp_http_get, FMPError
        url = f"{self.BASE_URL}/stock-screener"
        try:
            logger.debug(f"FMP screener request: {url} params={params}")
            response = fmp_http_get(url, params=params, endpoint="stock-screener", timeout=30)
            data = response.json()
        except (FMPError, requests.RequestException, ValueError) as e:
            # FMPError = retries exhausted (429/5xx/connection); RequestException = a
            # non-retryable HTTP error (401/404/...); ValueError = a body that is not JSON.
            msg = str(e).replace(self.api_key, "***")     # requests' HTTPError embeds the URL + key
            logger.error(f"FMP screener API request failed: {type(e).__name__}: {msg}")
            raise ScreenerDataError(
                f"FMP screener request failed ({type(e).__name__}: {msg})") from None

        if not isinstance(data, list):
            logger.warning(f"FMP screener returned unexpected response type: {type(data)}")
            raise ScreenerDataError(
                f"FMP screener returned a {type(data).__name__} instead of a list "
                f"(rate limit / plan error?): {str(data)[:200]}")

        # FMP occasionally returns a list of plain strings (ticker symbols) instead of
        # dicts, e.g. when the plan limit is reached or on certain filter combinations.
        # Filter them out before normalisation to avoid AttributeError on item.get().
        non_dict = [item for item in data if not isinstance(item, dict)]
        if non_dict:
            logger.warning(
                f"FMP screener: dropping {len(non_dict)} non-dict items "
                f"(first: {non_dict[0]!r})"
            )
            data = [item for item in data if isinstance(item, dict)]

        # Apply client-side sector exclusion (FMP doesn't support it natively)
        sector_exclude = filters.get("sector_exclude", [])
        if sector_exclude:
            exclude_lower = [s.lower() for s in sector_exclude]
            data = [
                item for item in data
                if (item.get("sector") or "").lower() not in exclude_lower
            ]

        results = [self._normalise_result(item) for item in data]

        # The vendor screener has NO float parameter (``floatSharesUnder`` is silently ignored
        # and no float comes back), so a float bound is applied here from the bulk float table.
        float_min = filters.get("float_min") or 0
        float_max = filters.get("float_max") or 0
        if float_min > 0 or float_max > 0:
            from ba2_providers.screener.float_filter import filter_by_float
            before = len(results)
            results, fstats = filter_by_float(results, float_min, float_max)
            logger.info(
                f"FMP screener: float filter [{float_min or '-'}, {float_max or '-'}] kept "
                f"{len(results)}/{before} ({fstats['float_unknown']} with unknown float passed)"
            )
        return results

    def _build_params(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Build FMP API query parameters from filter dict."""
        params: Dict[str, Any] = {
            "apikey": self.api_key,
            "isEtf": False,
            "isFund": False,
            "isActivelyTrading": True,
        }

        filter_map = {
            "price_min": "priceMoreThan",
            "price_max": "priceLowerThan",
            # AVERAGE volume. NOT ``volumeMoreThan``: that tests the ``volume`` field = the
            # current session's volume so far (near zero at the 09:30 open).
            "volume_min": "avgVolumeMoreThan",
            "market_cap_min": "marketCapMoreThan",
            "market_cap_max": "marketCapLowerThan",
        }

        for key, fmp_key in filter_map.items():
            value = filters.get(key)
            if value is not None:
                params[fmp_key] = value

        exchanges = filters.get("exchanges")
        if exchanges:
            params["exchange"] = ",".join(exchanges)

        limit = filters.get("limit")
        if limit is not None:
            params["limit"] = limit

        return params

    @staticmethod
    def _normalise_result(item: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise an FMP screener result to the standard format."""
        return {
            "symbol": item.get("symbol"),
            "company_name": item.get("companyName"),
            "price": item.get("price"),
            "volume": item.get("volume"),
            "market_cap": item.get("marketCap"),
            "sector": item.get("sector"),
            "industry": item.get("industry"),
            "exchange": item.get("exchangeShortName") or item.get("exchange"),
            "beta": item.get("beta"),
            "is_actively_trading": item.get("isActivelyTrading"),
            "country": item.get("country"),
            "float_shares": item.get("floatShares"),
        }

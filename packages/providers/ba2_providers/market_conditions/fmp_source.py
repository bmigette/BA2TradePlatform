"""The FMP-backed ``WarmupSource``: every fetch goes through the existing provider cache path.

* ``fetch_daily`` -> ``FMPOHLCVProvider.get_ohlcv_data(..., end_date=None)`` -- the LATEST read,
  which cold-fills an absent file and tops up a stale tail through ``_refresh_parquet_if_stale``
  (whose split-basis drift repair therefore runs too). ``max_cache_age_hours=0`` so a file touched
  recently but still missing the required tail is refreshed.
* ``force_full_refetch`` -> ``MarketDataProviderInterface.force_full_refetch`` (replace, marker).
* ``split_calendar`` -> FMP ``/api/v3/historical-price-full/stock_split/{symbol}`` via
  ``symbol_info.fetch_splits`` + ``parse_splits``, disk-cached per symbol like other FMP history
  payloads (``fmp_history_disk_cached``, namespace ``mc_stock_split``, one-day max age), so a
  repeat warmup costs no request.

Counters are the FMP request meter's ``warm`` purpose (``fmp_common.get_purpose_stats``): every
call here runs under ``fmp_purpose(PURPOSE_WARM)``, so ``calls``/``bytes`` are the wire truth, not
an estimate. The provider writes under ``native_cache.CACHE_FOLDER``; a different ``cache_root``
is a configuration error (the warmup would fetch into one tree and read another).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import List, Optional

from ba2_common.core.split_basis import CalendarSplit

SPLIT_CALENDAR_NAMESPACE = "mc_stock_split"
SPLIT_CALENDAR_MAX_AGE_DAYS = 1.0


class FMPWarmupSource:
    def __init__(self, cache_root: str, provider: Optional[object] = None):
        from ba2_common.core import native_cache

        live = os.path.normcase(os.path.abspath(native_cache.CACHE_FOLDER))
        if os.path.normcase(os.path.abspath(cache_root)) != live:
            from ba2_providers.market_conditions.warmup import WarmupConfigError
            raise WarmupConfigError(
                f"cache root {cache_root} is not the provider cache {native_cache.CACHE_FOLDER}; "
                "set CACHE_FOLDER so the provider fetches into the tree the warmup reads")
        self.cache_root = cache_root
        self._provider = provider

    @property
    def provider(self):
        if self._provider is None:
            from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
            self._provider = FMPOHLCVProvider()
        return self._provider

    @staticmethod
    def _warm_stats():
        from ba2_providers.fmp_common import PURPOSE_WARM, get_purpose_stats
        return get_purpose_stats().get(PURPOSE_WARM, {"requests": 0, "bytes": 0})

    @property
    def calls(self) -> int:
        return int(self._warm_stats()["requests"])

    @property
    def bytes(self) -> int:
        return int(self._warm_stats()["bytes"])

    def split_calendar(self, symbol: str) -> List[CalendarSplit]:
        from ba2_providers import symbol_info
        from ba2_providers.fmp_common import PURPOSE_WARM, fmp_history_disk_cached, fmp_purpose, frozen_ttl_cache

        sym = symbol.upper()
        api_key = self.provider.api_key
        with fmp_purpose(PURPOSE_WARM), frozen_ttl_cache():
            payload = fmp_history_disk_cached(
                SPLIT_CALENDAR_NAMESPACE, sym, lambda: symbol_info.fetch_splits(api_key, sym),
                max_age_days=SPLIT_CALENDAR_MAX_AGE_DAYS, retain=False)
        return [CalendarSplit(e.date, float(e.ratio)) for e in symbol_info.parse_splits(payload) if e.ratio]

    def fetch_daily(self, symbol: str, start: date, end: date) -> None:
        from ba2_providers.fmp_common import PURPOSE_WARM, fmp_purpose

        with fmp_purpose(PURPOSE_WARM):
            self.provider.get_ohlcv_data(symbol.upper(), start_date=datetime(start.year, start.month, start.day),
                                         end_date=None, interval="1d", max_cache_age_hours=0)

    def force_full_refetch(self, symbol: str) -> None:
        from ba2_providers.fmp_common import PURPOSE_WARM, fmp_purpose

        with fmp_purpose(PURPOSE_WARM):
            self.provider.force_full_refetch(symbol.upper(), "1d")

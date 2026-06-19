"""Re-export shim: the native as_of cache substrate now lives in ba2_common.

The OHLCV read-through (``MarketDataProviderInterface.get_ohlcv_data``) lives in
ba2_common and ba2_common may not import ba2_providers, so the parquet/SQLite
substrate was MOVED to ``ba2_common.core.native_cache``. This module re-exports it
unchanged so existing ``from ba2_providers.cache import native_cache`` importers
(and ``ba2_providers.cache.native_cache.<name>``) stay green.

NOTE for tests that rebind the cache root (``CACHE_FOLDER`` / ``_CACHE_ROOT``):
the live values are module-level names READ inside the source module
(``ba2_common.core.native_cache``). Rebinding them HERE alone has no effect —
rebind them on the source module (``ba2_common.core.native_cache``). The providers
conftest does exactly that.
"""
from ba2_common.core import native_cache as _src
from ba2_common.core.native_cache import (  # noqa: F401
    STATS,
    reset_stats,
    upsert_event_rows,
    read_event_rows,
    timeseries_path,
    read_timeseries,
    write_timeseries,
    CACHE_FOLDER,
    _CACHE_ROOT,
    _as_utc,
)

# Pull in everything else (private helpers, locks) so attribute access via this
# shim mirrors the source module exactly.
globals().update({k: v for k, v in vars(_src).items() if not k.startswith("__")})

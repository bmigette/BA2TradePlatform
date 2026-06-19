"""Native as_of cache for ba2_providers.

Two stores, both keyed on EFFECTIVE date (when the datum became public) so that a
fixed (symbol, as_of) replays deterministically with no lookahead:

- ``native_cache`` parquet time-series store (ohlcv, indicators)
- ``native_cache`` ProviderCache(SQLite) event store (insider, fundamentals, news)

The DB index table (``ProviderCache``) is a ba2_common model registered by
``db.init_db()``; the parquet/JSON blobs live under ``CACHE_FOLDER``. The host owns
the migration (Amendment A4) — there is no migrator in this package.
"""
from . import native_cache  # noqa: F401

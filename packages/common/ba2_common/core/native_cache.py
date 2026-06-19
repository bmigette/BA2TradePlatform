"""Native as_of cache: parquet for time-series, ProviderCache(SQLite) for events.

This substrate lives in ba2_common (NOT ba2_providers) because the OHLCV
read-through (``MarketDataProviderInterface.get_ohlcv_data``) lives in ba2_common
and ba2_common may not import ba2_providers. It depends only on the ba2_common
ProviderCache model + config/db/logger + pandas/pyarrow + stdlib. ``ba2_providers``
re-exports this module (ba2_providers/cache/native_cache.py) so existing
``from ba2_providers.cache import native_cache`` importers stay green.

read path (mirrors the proven BA2TestPlatform range-coverage logic, but keyed on
effective_date instead of fetch time):
  validate dates -> look up rows with effective_date<=as_of within the value_date
  window -> on a coverage miss the per-category get() (Task 4) calls the injected
  fetch_impl, then upsert_event_rows dedupes by payload_hash before re-filtering.

Historical as_of (< today - settle_lag) is immutable, so cached rows never expire.

The CACHE_FOLDER constant is read at import; tests rebind ``_CACHE_ROOT`` (and
``ba2_common.config.CACHE_FOLDER``) to a temp dir via the providers conftest so a
test never touches the real ~/Documents/.../cache tree.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ba2_common.config import CACHE_FOLDER
from ba2_common.core.db import get_db
from ba2_common.core.provider_cache_model import ProviderCache
from ba2_common.logger import logger

_CACHE_ROOT = os.path.join(CACHE_FOLDER, "datasets", "cache")
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# ---- cache-hit / miss / fetch counters (Task 11 assertions) ------------------
class _Stats:
    hits = 0
    misses = 0
    fetches = 0


STATS = _Stats()


def reset_stats() -> None:
    STATS.hits = STATS.misses = STATS.fetches = 0


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _spill_path(data_type: str, provider: str, h: str) -> str:
    d = os.path.join(_CACHE_ROOT, data_type, provider)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{h}.json")


# ---- event store (ProviderCache / SQLite) ------------------------------------
def upsert_event_rows(provider: str, data_type: str, symbol: str,
                      rows: List[dict], value_date_fn: Callable[[dict], Optional[datetime]],
                      effective_date_fn: Callable[[dict], Optional[datetime]],
                      frequency: Optional[str] = None, spill_threshold: int = 4000) -> int:
    """Dedupe-upsert event rows into ProviderCache. Returns count newly written.

    Rows whose value_date or effective_date cannot be resolved are skipped. Dedupe
    is by (provider, data_type, symbol, payload_hash); a second write of an
    identical row is a no-op. Large payloads spill to a JSON file on disk.
    """
    from sqlmodel import select
    written = 0
    with _lock_for(f"{provider}:{data_type}:{symbol}"):
        session = get_db()
        try:
            for row in rows:
                vd, ed = value_date_fn(row), effective_date_fn(row)
                if vd is None or ed is None:
                    continue
                h = _payload_hash(row)
                exists = session.exec(
                    select(ProviderCache).where(
                        ProviderCache.provider == provider,
                        ProviderCache.data_type == data_type,
                        ProviderCache.symbol == symbol,
                        ProviderCache.payload_hash == h,
                    )
                ).first()
                if exists:
                    continue
                raw = json.dumps(row, default=str)
                cfp = None
                if len(raw) > spill_threshold:
                    cfp = _spill_path(data_type, provider, h)
                    with open(cfp, "w") as f:
                        f.write(raw)
                    raw = None
                session.add(ProviderCache(
                    provider=provider, data_type=data_type, symbol=symbol, frequency=frequency,
                    value_date=vd, effective_date=ed, payload_hash=h,
                    content_file_path=cfp, raw_json=raw, fetched_at=datetime.now(timezone.utc)))
                written += 1
            session.commit()
        finally:
            session.close()
    return written


def read_event_rows(provider: str, data_type: str, symbol: str,
                    as_of: Optional[datetime], value_from: Optional[datetime] = None) -> List[dict]:
    """Return cached rows with effective_date<=as_of (no-lookahead) within the value
    window, newest value_date first. as_of=None => no effective_date ceiling (live
    latest). Bumps STATS.hits when rows are returned, STATS.misses on empty.
    """
    session = get_db()
    try:
        from sqlmodel import select
        stmt = select(ProviderCache).where(
            ProviderCache.provider == provider,
            ProviderCache.data_type == data_type,
            ProviderCache.symbol == symbol)
        if as_of is not None:
            stmt = stmt.where(ProviderCache.effective_date <= as_of)
        if value_from is not None:
            stmt = stmt.where(ProviderCache.value_date >= value_from)
        stmt = stmt.order_by(ProviderCache.value_date.desc())
        rows = session.exec(stmt).all()
    finally:
        session.close()
    out = []
    for r in rows:
        raw = r.raw_json
        if raw is None and r.content_file_path and os.path.exists(r.content_file_path):
            with open(r.content_file_path) as f:
                raw = f.read()
        if raw:
            out.append(json.loads(raw))
    if out:
        STATS.hits += 1
    else:
        STATS.misses += 1
    return out


# ---- parquet time-series (ohlcv, indicators) ---------------------------------
# Interval spellings are aliased across providers/callers (FMP writes the long form
# "5min"/"1hour"/"daily"; the backtester + types.OHLCVInterval use the short form
# "5m"/"1h"/"1d"). The parquet filename used to be built from the raw string, so a
# cache written as "<SYM>_5min.parquet" was a MISS for a "5m" read — a silent, hard
# backtest failure. We canonicalise to the SHORT form for new writes and fall back to
# every known alias spelling on read, so existing long-form caches keep resolving.
_INTERVAL_ALIASES: Dict[str, List[str]] = {
    "1m": ["1m", "1min"],
    "5m": ["5m", "5min"],
    "15m": ["15m", "15min"],
    "30m": ["30m", "30min"],
    "1h": ["1h", "1hour"],
    "4h": ["4h", "4hour"],
    "1d": ["1d", "1day", "daily"],
    "1wk": ["1wk", "1w", "1week", "weekly"],
    "1mo": ["1mo", "1month", "monthly"],
}
# spelling (any alias, lowercased) -> canonical short form
_INTERVAL_CANONICAL: Dict[str, str] = {
    alias: canon for canon, aliases in _INTERVAL_ALIASES.items() for alias in aliases
}


def normalize_interval(interval: str) -> str:
    """Map any known interval spelling to its canonical short form (``5min`` -> ``5m``).

    Unknown spellings pass through lowercased unchanged (no silent data loss; an
    unrecognised interval just keys its own file as before)."""
    return _INTERVAL_CANONICAL.get(interval.lower(), interval.lower())


def timeseries_path(provider: str, symbol: str, interval: str) -> str:
    """Build the parquet path for a (provider, symbol, interval), using the interval VERBATIM.

    Writes keep the caller's spelling (so the existing ``<SYM>_5min.parquet`` convention and the
    migration are unchanged — no duplicate ``_5m``/``_5min`` files). Reads that must tolerate
    alias spellings use ``find_timeseries_path`` instead (it resolves ``5m`` to an on-disk
    ``_5min`` file)."""
    d = os.path.join(CACHE_FOLDER, provider)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol.upper()}_{interval}.parquet")


def find_timeseries_path(provider: str, symbol: str, interval: str) -> Optional[str]:
    """Return the first EXISTING parquet path for any alias spelling of ``interval``.

    Prefers the canonical short-form path; falls back to legacy spellings (e.g. the
    FMP long-form ``<SYM>_5min.parquet``) so caches written before normalisation
    still resolve. None if no spelling exists on disk. Use this (not
    ``timeseries_path``) for any existence check / read — ``timeseries_path`` only
    builds the canonical write path and will MISS a legacy-spelled file on disk."""
    d = os.path.join(CACHE_FOLDER, provider)
    canon = normalize_interval(interval)
    for spelling in _INTERVAL_ALIASES.get(canon, [canon]):
        p = os.path.join(d, f"{symbol.upper()}_{spelling}.parquet")
        if os.path.exists(p):
            return p
    return None


def read_timeseries(provider: str, symbol: str, interval: str,
                    as_of: Optional[datetime]):
    """Read a parquet time-series sliced to effective_date<=as_of. None on miss.

    Bumps STATS.hits on a cache hit, STATS.misses when the parquet file is absent.
    Resolves any known interval-alias spelling on disk (canonical + legacy).
    """
    import pandas as pd
    path = find_timeseries_path(provider, symbol, interval)
    if path is None:
        STATS.misses += 1
        return None
    df = pd.read_parquet(path)
    if as_of is not None and "effective_date" in df.columns:
        eff = pd.to_datetime(df["effective_date"], utc=True)
        df = df[eff <= _as_utc(as_of)]
    STATS.hits += 1
    return df


def write_timeseries(provider: str, symbol: str, interval: str, df) -> None:
    """Atomic temp+rename parquet write. df MUST carry an effective_date column
    (for OHLCV effective_date == bar Date)."""
    path = timeseries_path(provider, symbol, interval)
    with _lock_for(path):
        tmp = path + ".tmp"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)


def _as_utc(dt: datetime):
    """Normalize a datetime to a tz-aware UTC pandas Timestamp for comparisons."""
    import pandas as pd
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

"""Backend-local OHLCV cache-extend layer.

The shared ``ba2_providers`` OHLCV providers expose the public fetch contract
(``get_data`` / ``get_ohlcv_data`` / ``_get_ohlcv_data_impl``) but NOT the
backend's parquet-backed, gap-filling on-disk cache layer (``extend_ohlcv_cache``,
``_get_cache_file`` and friends). That layer used to live on
``dataproviders.base.MarketDataProviderInterface`` and is consumed by
``app.services.ohlcv_cache_handler`` (the OHLCV cache-fetch background task) and
covered by ``tests/test_ohlcv_cache.py``.

When the local ``dataproviders/`` tree was deleted in favour of the shared
packages, that cache layer had nowhere to live, so it is preserved here verbatim
as a mixin. ``wrap_with_cache`` layers it onto a shared provider instance so the
cache handler keeps working unchanged; ``OHLCVCacheProviderBase`` is the abstract
base the cache tests subclass (same shape the local base had).

UNIFIED CACHE (2026-06): this layer no longer keeps a SEPARATE store. Its reads/writes now target
the SAME native parquet cache the rest of the system uses —
``<CACHE_FOLDER>/<ProviderClassName>/<SYMBOL>_<interval>.parquet`` (ba2_common ``native_cache``,
schema carries an ``effective_date`` column) — so a bulk ``fetch-cache`` download is immediately
usable by the backtest/live/experts with NO second cache and no migration. ``extend_ohlcv_cache``
keeps its gap-fill / head-tail-extension behaviour; only its storage target changed. Legacy ``.csv``
caches are still read transparently and migrated to Parquet on the next write.
"""

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional
import logging
import threading

import pandas as pd

from ba2_common.core import native_cache

logger = logging.getLogger(__name__)

# Shared OHLCV disk-cache root. Lives under the shared ba2_common cache
# (default ~/Documents/ba2/common/cache/ohlcv) so NOTHING is cached inside the
# repo. ``CACHE_FOLDER`` env still wins (via ba2_common.config). Resolved
# defensively so the module still imports without ba2_common.
try:
    from ba2_common.config import CACHE_FOLDER as _COMMON_CACHE_FOLDER
    DEFAULT_OHLCV_CACHE_DIR = Path(_COMMON_CACHE_FOLDER) / "ohlcv"
except Exception:  # pragma: no cover
    DEFAULT_OHLCV_CACHE_DIR = Path("datasets/cache/ohlcv")

class OHLCVCacheMixin:
    """Parquet-backed, gap-filling OHLCV disk-cache for a market-data provider.

    Requires the host class to provide ``get_provider_name() -> str`` and
    ``_get_ohlcv_data_impl(symbol, start_date, end_date, interval) -> DataFrame``
    (both satisfied by the shared ba2_providers OHLCV providers). ``cache_folder``
    defaults to the legacy local-tree location and may be overridden per instance.
    """

    # Default cache root: shared ba2_common cache (common/cache/ohlcv), NOT the
    # repo/CWD. Overridable per instance.
    cache_folder: Path = DEFAULT_OHLCV_CACHE_DIR
    cache_max_age_hours: int = 24

    def _ensure_cache_folder(self) -> Path:
        folder = Path(self.cache_folder)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _get_cache_file(self, symbol: str, interval: str) -> Path:
        """Return the canonical NATIVE cache file path (Parquet), creating the dir if needed.

        UNIFIED CACHE: this used to return a separate backend layout
        (``CACHE_FOLDER/ohlcv/<get_provider_name()>/...``); it now points at the SAME native
        parquet cache the rest of the system reads/writes —
        ``CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet`` (ba2_common ``native_cache``).
        When ``wrap_with_cache`` binds this mixin onto a real provider, ``type(self).__name__`` is
        the provider class name (e.g. ``FMPOHLCVProvider``) — exactly the ``native_cache`` key used
        by ``MarketDataProviderInterface.get_ohlcv_data``. Legacy ``.csv`` caches are still read
        transparently (see ``_existing_cache_file``) and migrated to Parquet on next write.
        """
        return Path(native_cache.timeseries_path(type(self).__name__, symbol, interval))

    def _existing_cache_file(self, symbol: str, interval: str) -> Optional[Path]:
        """The on-disk cache file to READ: Parquet if present, else legacy CSV, else None."""
        pq = self._get_cache_file(symbol, interval)
        if pq.exists():
            return pq
        csv = pq.with_suffix(".csv")
        return csv if csv.exists() else None

    def _read_cache_df(self, path: Path) -> pd.DataFrame:
        """Read a cache file by suffix (Parquet preferred; legacy CSV still supported)."""
        return pd.read_csv(path) if path.suffix == ".csv" else pd.read_parquet(path)

    def _write_cache_df(self, df: pd.DataFrame, symbol: str, interval: str) -> Path:
        """Write ``df`` to the canonical native Parquet cache and remove any legacy CSV sibling.

        UNIFIED CACHE: delegates to ``native_cache.write_timeseries`` — the SAME atomic
        (temp+rename) + per-path-locked writer ``MarketDataProviderInterface._write_ohlcv_parquet``
        uses — so a ``fetch-cache`` download lands in the ONE cache the backtest/live/experts read.
        Stamps ``effective_date == Date`` (OHLCV is public on its bar date -> no lookahead),
        matching ``_write_ohlcv_parquet``; ``native_cache.write_timeseries`` requires the column.
        """
        out = df.copy()
        out["Date"] = pd.to_datetime(out["Date"])
        out["effective_date"] = out["Date"]
        native_cache.write_timeseries(type(self).__name__, symbol, interval, out)
        path = self._get_cache_file(symbol, interval)
        legacy = path.with_suffix(".csv")
        if legacy.exists():
            try:
                legacy.unlink()
            except OSError:
                pass
        return path

    def extend_ohlcv_cache(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d',
        progress_callback: Optional[Callable[[float, str], None]] = None,
        executor_workers: int = 5,
    ) -> pd.DataFrame:
        """
        Fetch and cache OHLCV data for the requested range using a two-phase approach.

        Phase 1 - Gap filling (parallel, 5 workers):
            Scans existing cache for internal gaps > 5 calendar days that overlap
            the requested range, then fetches all gaps concurrently.

        Phase 2 - Range extension (sequential):
            Fetches any head/tail portions that fall outside the existing cached range.

        Args:
            symbol: Ticker symbol
            start_date: Desired range start (inclusive)
            end_date: Desired range end (inclusive)
            interval: Data interval ('1d', '1h', etc.)
            progress_callback: Optional callable(pct, message) for progress reporting.

        Returns:
            Full merged DataFrame (existing + newly fetched), saved to cache.
        """

        def _report(pct: float, msg: str) -> None:
            logger.info(msg)
            if progress_callback:
                progress_callback(pct, msg)

        def _to_naive_ts(dt: datetime) -> pd.Timestamp:
            ts = pd.Timestamp(dt)
            if ts.tzinfo is not None:
                ts = ts.tz_convert('UTC').tz_localize(None)
            return ts

        start_ts = _to_naive_ts(start_date)
        end_ts = _to_naive_ts(end_date)

        # ------------------------------------------------------------------ #
        # Load existing cache (Parquet, or a legacy CSV - migrated on write)   #
        # ------------------------------------------------------------------ #
        existing = pd.DataFrame()
        read_file = self._existing_cache_file(symbol, interval)
        if read_file is not None:
            try:
                existing = self._read_cache_df(read_file)
                existing['Date'] = pd.to_datetime(existing['Date']).dt.tz_localize(None)
            except Exception as e:
                logger.warning(f"Could not read existing cache {read_file}: {e}")
                existing = pd.DataFrame()

        if existing.empty:
            # No cache - fetch the full requested range in one shot
            _report(5.0, f"{symbol}/{interval}: No cache, fetching full range "
                         f"{start_date.date()} -> {end_date.date()}")
            new_data = self._get_ohlcv_data_impl(symbol, start_date, end_date, interval)
            if not new_data.empty:
                new_data['Date'] = pd.to_datetime(new_data['Date']).dt.tz_localize(None)
                cache_file = self._write_cache_df(new_data, symbol, interval)
                logger.info(f"Saved {len(new_data)} rows to {cache_file}")
            _report(100.0, f"{symbol}/{interval}: Done - {len(new_data)} rows")
            return new_data

        existing = existing.sort_values('Date').reset_index(drop=True)

        # ------------------------------------------------------------------ #
        # Phase 1 - Identify internal gaps within the requested range          #
        # ------------------------------------------------------------------ #
        gap_threshold = pd.Timedelta('5 days')
        diffs = existing['Date'].diff()
        gaps_to_fill: List[tuple] = []
        for idx in diffs[diffs > gap_threshold].index:
            gap_start_dt = existing.loc[idx - 1, 'Date']
            gap_end_dt = existing.loc[idx, 'Date']
            # Only fill gaps that overlap with [start_ts, end_ts]
            if gap_end_dt > start_ts and gap_start_dt < end_ts:
                gaps_to_fill.append((gap_start_dt.to_pydatetime(), gap_end_dt.to_pydatetime()))

        _report(5.0, f"{symbol}/{interval}: Found {len(gaps_to_fill)} gap(s) to fill")

        # ------------------------------------------------------------------ #
        # Phase 1 - Fetch all gaps concurrently (max 5 workers)               #
        # ------------------------------------------------------------------ #
        gap_pieces: List[pd.DataFrame] = []
        if gaps_to_fill:
            completed_gaps = [0]
            total_gaps = len(gaps_to_fill)
            gap_lock = threading.Lock()

            def fetch_gap(gs: datetime, ge: datetime) -> pd.DataFrame:
                try:
                    data = self._get_ohlcv_data_impl(symbol, gs, ge, interval)
                    if not data.empty:
                        data['Date'] = pd.to_datetime(data['Date']).dt.tz_localize(None)
                        logger.info(f"  Gap {gs.date()}->{ge.date()}: {len(data)} bars")
                        return data
                except Exception as exc:
                    logger.warning(f"  Failed to fill gap {gs.date()}->{ge.date()}: {exc}")
                return pd.DataFrame()

            with ThreadPoolExecutor(max_workers=executor_workers) as executor:
                future_to_gap = {
                    executor.submit(fetch_gap, gs, ge): (gs, ge)
                    for gs, ge in gaps_to_fill
                }
                for future in as_completed(future_to_gap):
                    data = future.result()
                    if not data.empty:
                        gap_pieces.append(data)
                    with gap_lock:
                        completed_gaps[0] += 1
                        pct = 5.0 + (completed_gaps[0] / total_gaps) * 75.0
                        _report(pct, f"{symbol}/{interval}: Filled {completed_gaps[0]}/{total_gaps} gaps")

        # Merge existing + gap fills
        all_pieces = [existing] + gap_pieces
        merged = (
            pd.concat(all_pieces, ignore_index=True)
              .drop_duplicates(subset=['Date'])
              .sort_values('Date')
              .reset_index(drop=True)
        )
        cache_min = merged['Date'].min()
        cache_max = merged['Date'].max()

        # ------------------------------------------------------------------ #
        # Phase 2 - Extend head/tail to cover the full requested range        #
        # ------------------------------------------------------------------ #
        extension_pieces = [merged]

        if start_ts < cache_min:
            _report(82.0, f"{symbol}/{interval}: Extending left: "
                          f"{start_date.date()} -> {cache_min.date()}")
            left = self._get_ohlcv_data_impl(
                symbol, start_date, cache_min.to_pydatetime(), interval
            )
            if not left.empty:
                left['Date'] = pd.to_datetime(left['Date']).dt.tz_localize(None)
                extension_pieces.append(left)
            _report(90.0, f"{symbol}/{interval}: Left extension done")

        if end_ts > cache_max:
            _report(92.0, f"{symbol}/{interval}: Extending right: "
                          f"{cache_max.date()} -> {end_date.date()}")
            right = self._get_ohlcv_data_impl(
                symbol, cache_max.to_pydatetime(), end_date, interval
            )
            if not right.empty:
                right['Date'] = pd.to_datetime(right['Date']).dt.tz_localize(None)
                extension_pieces.append(right)
            _report(98.0, f"{symbol}/{interval}: Right extension done")

        final = (
            pd.concat(extension_pieces, ignore_index=True)
              .drop_duplicates(subset=['Date'])
              .sort_values('Date')
              .reset_index(drop=True)
        )

        if not final.empty:
            cache_file = self._write_cache_df(final, symbol, interval)
            logger.info(f"Saved {len(final)} rows to {cache_file}")

        _report(100.0, f"{symbol}/{interval}: Done - {len(final)} rows total")
        # The native cache (and thus a re-read of `existing`) carries an effective_date column;
        # callers expect the public Date+OHLCV shape, so drop it (mirrors get_ohlcv_data). It was
        # already (re)stamped on write by _write_cache_df.
        return final.drop(columns=["effective_date"], errors="ignore")


class OHLCVCacheProviderBase(OHLCVCacheMixin, ABC):
    """Abstract base combining the OHLCV disk-cache layer with the provider fetch
    contract. Subclasses implement the data fetch + identity; the cache methods
    come from :class:`OHLCVCacheMixin`.

    This mirrors the shape of the deleted
    ``dataproviders.base.MarketDataProviderInterface`` so existing cache tests can
    subclass it and override ``_get_ohlcv_data_impl`` / ``get_provider_name``.
    """

    def __init__(self):
        self.cache_folder = DEFAULT_OHLCV_CACHE_DIR
        self.cache_folder.mkdir(parents=True, exist_ok=True)
        self.cache_max_age_hours = 24

    @abstractmethod
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d',
    ) -> pd.DataFrame:
        """Fetch raw OHLCV data (columns: Date, Open, High, Low, Close, Volume)."""
        raise NotImplementedError

    @abstractmethod
    def get_provider_name(self) -> str:
        """Short provider identity used for the per-provider cache subdirectory."""
        raise NotImplementedError


# Mixin methods bound onto wrapped instances. (Functions only — the mixin's
# class-level data attributes cache_folder/cache_max_age_hours are set per
# instance below.)
_MIXIN_METHODS = (
    "_ensure_cache_folder",
    "_get_cache_file",
    "_existing_cache_file",
    "_read_cache_df",
    "_write_cache_df",
    "extend_ohlcv_cache",
)


def wrap_with_cache(shared_provider):
    """Augment a shared ``ba2_providers`` OHLCV provider with the backend OHLCV
    disk-cache layer, in place.

    The returned object IS the shared provider (its ``_get_ohlcv_data_impl`` /
    ``get_ohlcv_data`` / ``get_data`` / ``get_provider_name`` and class identity are
    unchanged, so ``isinstance(prov, MarketDataProviderInterface)`` still holds) but
    additionally exposes ``extend_ohlcv_cache`` / ``_get_cache_file`` so the backend
    OHLCV cache-fetch task can use it.

    Implemented by binding the :class:`OHLCVCacheMixin` methods onto the instance
    (``types.MethodType``) rather than re-classing it — re-classing fails with a
    layout TypeError for C-extension-backed provider classes, and we must not
    modify the shared package. The disk cache is pointed at the backend's
    CWD-relative location so ``_get_cache_file`` / ``extend_ohlcv_cache`` use the
    legacy path the cache tool and cache tests expect.
    """
    import types

    for name in _MIXIN_METHODS:
        func = getattr(OHLCVCacheMixin, name)
        setattr(shared_provider, name, types.MethodType(func, shared_provider))
    shared_provider.cache_folder = DEFAULT_OHLCV_CACHE_DIR
    shared_provider.cache_max_age_hours = 24
    return shared_provider

"""
MarketDataProvider Interface

Abstract base class for market data providers with built-in caching strategy.
All data providers should extend this class and implement the fetch methods.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, time, timezone
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd
import os
import threading
from ba2_common.core.types import MarketDataPoint
from ba2_common.logger import logger
from ba2_common import config
from ba2_common.core.provider_utils import log_provider_call, validate_date_range
from ba2_common.core.interfaces.DataProviderInterface import DataProviderInterface
from ba2_common.core import ohlcv_final_bars
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.replay.schemas import ReplayStatus

#: MAINTENANCE SWITCH -- MUST NOT BE SET IN A LIVE APP'S ENVIRONMENT. It exists for the data-refresh
#: tools that run while backtests of the old history are in flight (``tools/plan_daily_extension.py``,
#: the repair/extension scripts). A live app that carries it would REFUSE the top-up of a split name
#: instead of repairing its history; ``main.initialize_system`` logs one WARNING when it is set.
#: ``=0`` makes the verified daily top-up REFUSE (instead of REPLACE the history) when a split since
#: the last cached bar calls for a full re-fetch: an additive-only refresh. Unset / ``1``: unchanged.
#: Additive-only means: no cached bar is replaced by a vendor bar and no split replaces the history.
#: ONE exception, stated here and in the refusal text: a newest bar the file's mtime PROVES was written
#: before its session was final (``_unproven_tail_days``) is still healed -- it is a snapshot by
#: construction, not history. Only "0", "1" or unset are accepted (anything else raises).
FULL_REFETCH_ENV = "BA2_OHLCV_TOPUP_FULL_REFETCH"


def live_environment_warnings() -> List[str]:
    """Warnings for maintenance switches that are set in a LIVE app's environment (one per switch)."""
    value = os.environ.get(FULL_REFETCH_ENV)
    if value is None:
        return []
    return [f"{FULL_REFETCH_ENV}={value!r} is set in this LIVE app's environment: it is a maintenance "
            f"switch for the data-refresh tools (a split name's top-up is refused instead of repaired). "
            f"Unset it."]

#: Live memo TTL of today's FORMING daily bar. After it a LATEST read during the open session fetches
#: again. FMP load: one call per symbol per TTL at most, and only for symbols somebody reads. The live
#: set is ~505 symbols (instances 7-12) and experts read once per cycle; if EVERY symbol were read
#: continuously all session, 10 min = 39 windows x 505 = ~19.7k calls (15 min: ~13k), against ~6.7k
#: calls/day of normal use; a realistic day (the 09:30 cycle + occasional UI reads) is ~600-1,000.
FORMING_BAR_TTL_S = 600.0


def _mono() -> float:
    """Monotonic seconds; a function so tests can move it."""
    import time as _t
    return _t.monotonic()

# Intraday interval spellings, SHORT and provider long form (FMP writes "5min"/"1hour").
# Single source of truth: the cache-freshness branch in get_ohlcv_data and its
# cache-fill-range branch must agree on what counts as intraday. They were two separate
# literal tuples, and the drift (one listing only short forms) already caused a wasteful
# ~15-year fetch for long-form spellings.
_INTRADAY_INTERVALS = (
    '1m', '5m', '15m', '30m', '1h', '4h',
    '1min', '5min', '15min', '30min', '1hour', '4hour',
)


# --------------------------------------------------------------------------- #
# Replay capture (spec step 2): OHLCV provenance.
#
# The parquet store counts its own hits and misses, so "did this call fetch?" is
# already measured -- reading the delta costs two integer reads and issues no
# request of its own ("never issue a duplicate fetch just to fill metadata").
#
# The counters are process-global, so a CONCURRENT read in another thread can
# move them under us. That makes the delta ambiguous, never wrong-but-confident:
# anything other than exactly one hit or one miss is recorded as ``unknown``.
# --------------------------------------------------------------------------- #
def ohlcv_identity(args):
    """What makes an OHLCV response what it is: provider, symbol, window, interval.

    Named (not an inline lambda) so the offline replay tape can build the SAME
    identity to look a recorded frame up by it; two copies of this dict would
    drift and turn a real match into a silent miss.
    """
    return {
        "provider": type(args["self"]).__name__,
        "symbol": args["symbol"],
        "interval": args["interval"],
        "start_date": args["start_date"],
        "end_date": args["end_date"],
        "lookback_days": args["lookback_days"],
        "use_cache": args["use_cache"],
    }


def _ohlcv_cache_counters():
    from ba2_common.core import native_cache
    return (native_cache.STATS.hits, native_cache.STATS.misses)


def _ohlcv_provenance(args, before):
    from ba2_common.core import native_cache

    if not args["use_cache"]:
        # Caching disabled: the frame can only have come from the source.
        return ReplayStatus.PROVENANCE_NETWORK
    if before is None:
        return ReplayStatus.PROVENANCE_UNKNOWN
    hits = native_cache.STATS.hits - before[0]
    misses = native_cache.STATS.misses - before[1]
    if hits == 1 and misses == 0:
        return ReplayStatus.PROVENANCE_DISK_CACHE
    if misses >= 1 and hits == 0:
        return ReplayStatus.PROVENANCE_NETWORK
    return ReplayStatus.PROVENANCE_UNKNOWN


class MarketDataProviderInterface(DataProviderInterface):
    """
    Abstract base class for market data providers.

    Provides a standardized interface for fetching historical market data
    with built-in caching capabilities.

    Subclasses must implement:
        - _fetch_data_from_source(): Fetch data from the actual data source
    """

    # Class-level lock dictionary for per-file locking (shared across all instances)
    _cache_locks: Dict[str, threading.Lock] = {}
    _cache_locks_lock = threading.Lock()  # Lock to protect the locks dictionary


    def __init__(self):
        """
        Initialize the market data provider.
        
        Caching is automatically configured using:
        - CACHE_FOLDER from config module
        - Provider class name for organizing cache files
        """
        # Use CACHE_FOLDER from config + class name for provider-specific subfolder
        self.cache_folder = os.path.join(config.CACHE_FOLDER, self.__class__.__name__)
        os.makedirs(self.cache_folder, exist_ok=True)
        logger.debug(f"{self.__class__.__name__} initialized with cache folder: {self.cache_folder}")
    
    @classmethod
    def _get_cache_lock(cls, cache_file: str) -> threading.Lock:
        """
        Get or create a lock for a specific cache file.
        
        Args:
            cache_file: Path to the cache file
        
        Returns:
            Lock object for this cache file
        """
        with cls._cache_locks_lock:
            if cache_file not in cls._cache_locks:
                cls._cache_locks[cache_file] = threading.Lock()
            return cls._cache_locks[cache_file]
    
    @staticmethod
    def normalize_time_to_interval(dt: datetime, interval: str) -> datetime:
        """
        Normalize (floor) a datetime to the given interval.
        
        This ensures that timestamps align to interval boundaries for proper time series.
        
        Examples:
            - 15:54:00 with interval '15m' -> 15:45:00
            - 15:54:00 with interval '1h' -> 15:00:00
            - 15:54:00 with interval '4h' -> 12:00:00 (4h blocks start at midnight: 0h, 4h, 8h, 12h, 16h, 20h)
            - 15:54:00 with interval '1d' -> 00:00:00 (start of day)
        
        Args:
            dt: Datetime to normalize
            interval: Interval string ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1wk', '1mo')
        
        Returns:
            Normalized datetime floored to the interval boundary
        """
        # Parse interval to extract number and unit
        interval = interval.lower().strip()
        
        # Extract numeric value and unit
        if interval.endswith('m'):  # Minutes
            minutes = int(interval[:-1])
            # Floor to the minute interval
            total_minutes = dt.hour * 60 + dt.minute
            floored_minutes = (total_minutes // minutes) * minutes
            floored_hour = floored_minutes // 60
            floored_minute = floored_minutes % 60
            return dt.replace(hour=floored_hour, minute=floored_minute, second=0, microsecond=0)
        
        elif interval.endswith('h'):  # Hours
            hours = int(interval[:-1])
            # Floor to the hour interval (counting from midnight)
            floored_hour = (dt.hour // hours) * hours
            return dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)
        
        elif interval.endswith('d') or interval == '1d':  # Days
            # Floor to start of day
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        
        elif interval.endswith('wk'):  # Weeks
            # Floor to start of week (Monday)
            days_since_monday = dt.weekday()  # Monday is 0, Sunday is 6
            start_of_week = dt - timedelta(days=days_since_monday)
            return start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        
        elif interval.endswith('mo'):  # Months
            # Floor to start of month
            return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        else:
            # Unknown interval, return as-is with seconds/microseconds zeroed
            logger.warning(f"Unknown interval format '{interval}', returning time with seconds zeroed")
            return dt.replace(second=0, microsecond=0)
    
    @staticmethod
    def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        Clean and harden an OHLCV DataFrame against malformed data.

        Handles:
        - Coerces Date column to datetime, dropping unparseable rows
        - Coerces OHLCV columns to numeric, replacing failures with NaN
        - Drops rows where Close is missing (essential for analysis)
        - Forward-fills then back-fills remaining price gaps
        """
        if df.empty:
            return df

        # Coerce Date to datetime
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
            before_len = len(df)
            df = df.dropna(subset=['Date'])
            dropped = before_len - len(df)
            if dropped > 0:
                logger.warning(f"Dropped {dropped} rows with invalid dates")

        # Coerce price/volume columns to numeric
        numeric_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Drop rows missing Close (essential)
        if 'Close' in df.columns:
            before_len = len(df)
            df = df.dropna(subset=['Close'])
            dropped = before_len - len(df)
            if dropped > 0:
                logger.warning(f"Dropped {dropped} rows with missing Close price")

        # Forward-fill then back-fill remaining price gaps
        price_cols = [c for c in ['Open', 'High', 'Low', 'Close'] if c in df.columns]
        if price_cols:
            df[price_cols] = df[price_cols].ffill().bfill()

        # Fill missing volume with 0
        if 'Volume' in df.columns:
            df['Volume'] = df['Volume'].fillna(0)

        return df.reset_index(drop=True)

    @abstractmethod
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from the actual data source (e.g., API, database).
        
        This is an internal implementation method that must be implemented by subclasses.
        External code should call get_ohlcv_data() instead.
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval ('1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo')
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
            Date should be datetime or datetime index
        
        Raises:
            Exception: If data fetching fails
        """
        pass
    
    def _get_cache_file_path(self, symbol: str, interval: str) -> str:
        """
        Get the cache file path for a given symbol and interval.
        
        Args:
            symbol: Ticker symbol
            interval: Data interval
        
        Returns:
            Full path to the cache file
        """
        filename = f"{symbol.upper()}_{interval}.csv"
        return os.path.join(self.cache_folder, filename)
    
    def _is_cache_valid(self, cache_file: str, max_age_hours: int = 24) -> bool:
        """
        Check if cache file exists and is not too old.
        
        Args:
            cache_file: Path to cache file
            max_age_hours: Maximum age of cache in hours (default: 24)
        
        Returns:
            True if cache is valid, False otherwise
        """
        if not os.path.exists(cache_file):
            return False
        
        # Check file age
        file_modified_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        age = datetime.now() - file_modified_time
        
        is_valid = age < timedelta(hours=max_age_hours)
        
        if not is_valid:
            logger.debug(f"Cache file {cache_file} is too old ({age.total_seconds()/3600:.1f} hours)")
        
        return is_valid
    
    def _load_cache(self, cache_file: str, delete_if_corrupted: bool = True) -> Optional[pd.DataFrame]:
        """
        Load data from cache file with thread safety.
        
        Args:
            cache_file: Path to cache file
            delete_if_corrupted: If True, delete corrupted cache files (default: True)
        
        Returns:
            DataFrame if successful, None if failed or corrupted
        """
        lock = self._get_cache_lock(cache_file)
        
        with lock:
            try:
                if not os.path.exists(cache_file):
                    return None
                    
                # Check file size to avoid reading empty/corrupted files
                if os.path.getsize(cache_file) == 0:
                    logger.warning(f"Cache file is empty, deleting: {cache_file}")
                    self._delete_cache_file(cache_file)
                    return None
                
                df = pd.read_csv(cache_file, on_bad_lines="skip")

                # Validate that the DataFrame has expected columns
                if df.empty or 'Date' not in df.columns:
                    logger.warning(f"Cache file has no valid data/columns, deleting: {cache_file}")
                    if delete_if_corrupted:
                        self._delete_cache_file(cache_file)
                    return None

                # Clean and harden against malformed data
                df = self._clean_dataframe(df)
                # logger.debug(f"Loaded {len(df)} records from cache: {cache_file}")
                return df
            except Exception as e:
                logger.error(f"Failed to load cache file {cache_file}: {e}", exc_info=True)
                if delete_if_corrupted:
                    logger.warning(f"Deleting corrupted cache file: {cache_file}")
                    self._delete_cache_file(cache_file)
                return None
    
    def _delete_cache_file(self, cache_file: str) -> bool:
        """
        Delete a cache file safely.
        
        Args:
            cache_file: Path to cache file to delete
        
        Returns:
            True if deleted successfully, False otherwise
        """
        try:
            if os.path.exists(cache_file):
                os.remove(cache_file)
                logger.info(f"Deleted cache file: {cache_file}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete cache file {cache_file}: {e}")
            return False
    
    def _save_final_bars_cache(self, data: pd.DataFrame, symbol: str, interval: str,
                               cache_file: str) -> bool:
        """``_save_cache`` for an OHLCV frame of the legacy per-class CSV cache (``get_data``): the
        same rule as the parquet store (``ba2_common.core.ohlcv_final_bars``) -- an unfinished bar is
        served to the caller from memory but never written."""
        keep, dropped = ohlcv_final_bars.drop_unfinished_bars(data, symbol, interval)
        if dropped:
            logger.info(f"{symbol} ({interval}): not persisting {len(dropped)} unfinished bar(s) "
                        f"{dropped} to {cache_file}")
            if keep.empty:
                return False
        return self._save_cache(keep, cache_file)

    def _save_cache(self, data: pd.DataFrame, cache_file: str) -> bool:
        """
        Save data to cache file with thread safety.
        
        Uses atomic write pattern: writes to temp file first, then renames.
        This prevents other threads from reading partially written files.
        
        Args:
            data: DataFrame to cache
            cache_file: Path to cache file
        
        Returns:
            True if successful, False otherwise
        """
        lock = self._get_cache_lock(cache_file)
        
        with lock:
            try:
                # Use atomic write: write to temp file, then rename
                temp_file = cache_file + ".tmp"
                data.to_csv(temp_file, index=False)
                
                # Atomic rename (on most systems)
                if os.path.exists(cache_file):
                    os.remove(cache_file)
                os.rename(temp_file, cache_file)
                
                logger.debug(f"Saved {len(data)} records to cache: {cache_file}")
                return True
            except Exception as e:
                logger.error(f"Failed to save cache file {cache_file}: {e}", exc_info=True)
                # Clean up temp file if it exists
                temp_file = cache_file + ".tmp"
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
                return False
    
    def _dataframe_to_datapoints(
        self,
        df: pd.DataFrame,
        symbol: str,
        interval: str
    ) -> List[MarketDataPoint]:
        """
        Convert DataFrame to list of MarketDataPoint objects.
        
        Args:
            df: DataFrame with OHLCV data
            symbol: Ticker symbol
            interval: Data interval
        
        Returns:
            List of MarketDataPoint objects
        """
        datapoints = []
        
        for _, row in df.iterrows():
            try:
                datapoint = MarketDataPoint(
                    symbol=symbol.upper(),
                    timestamp=pd.to_datetime(row['Date']),
                    open=float(row['Open']),
                    high=float(row['High']),
                    low=float(row['Low']),
                    close=float(row['Close']),
                    volume=float(row['Volume']),
                    interval=interval
                )
                datapoints.append(datapoint)
            except Exception as e:
                logger.error(f"Failed to convert row to MarketDataPoint: {e}", exc_info=True)
                continue
        
        return datapoints

    @staticmethod
    def _interval_to_timedelta(interval: str) -> timedelta:
        """Convert an interval string (e.g. '5m', '1h') to a timedelta."""
        interval = interval.lower().strip()
        if interval.endswith('m'):
            return timedelta(minutes=int(interval[:-1]))
        if interval.endswith('h'):
            return timedelta(hours=int(interval[:-1]))
        if interval.endswith('d'):
            return timedelta(days=int(interval[:-1]))
        return timedelta(hours=1)  # fallback

    def _refresh_intraday_if_stale(
        self,
        df: pd.DataFrame,
        symbol: str,
        interval: str,
        cache_file: str,
    ) -> pd.DataFrame:
        """
        For intraday data loaded from disk cache: if the last cached bar is from
        today but older than one interval, fetch only the missing bars, append
        them, update the cache, and return the merged DataFrame.

        This avoids re-fetching the full history on every monitoring tick while
        still keeping intraday candles current.
        """
        df['Date'] = pd.to_datetime(df['Date'])
        last_bar_dt = df['Date'].iloc[-1]

        # Normalise to a plain datetime for comparison
        last_bar_naive = (
            last_bar_dt.to_pydatetime().replace(tzinfo=None)
            if hasattr(last_bar_dt, 'to_pydatetime')
            else last_bar_dt
        )
        if last_bar_naive.tzinfo is not None:
            last_bar_naive = last_bar_naive.replace(tzinfo=None)

        now = datetime.now()
        today = now.date()

        # Only do incremental fetch if the last bar is from today
        if last_bar_naive.date() != today:
            return df

        interval_td = self._interval_to_timedelta(interval)
        expected_latest = self.normalize_time_to_interval(now, interval)

        # Cache is already up to date
        if last_bar_naive >= expected_latest:
            return df

        # Fetch only the missing slice
        fetch_start = last_bar_naive + interval_td
        fetch_end = now + interval_td  # slightly beyond to capture the current bar

        logger.debug(
            f"Intraday cache stale for {symbol} ({interval}): "
            f"last={last_bar_naive.strftime('%H:%M')}, expected>={expected_latest.strftime('%H:%M')}; "
            f"fetching from {fetch_start.strftime('%H:%M')}"
        )
        try:
            new_df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)
            if new_df is not None and not new_df.empty:
                new_df['Date'] = pd.to_datetime(new_df['Date'])
                df = pd.concat([df, new_df], ignore_index=True)
                df = (
                    df.drop_duplicates(subset=['Date'])
                    .sort_values('Date')
                    .reset_index(drop=True)
                )
                self._save_final_bars_cache(df, symbol, interval, cache_file)
                logger.debug(
                    f"Appended {len(new_df)} new bar(s) to {symbol} ({interval}) cache"
                )
        except Exception as e:
            logger.warning(
                f"Failed to refresh intraday cache for {symbol} ({interval}): {e}"
            )

        return df

    # ---- unfinished bars: served to live from memory, never persisted ---------------------
    #: ``(provider, SYMBOL, canonical interval) -> (monotonic time, unfinished rows)``: the forming
    #: bar(s) the vendor returned at the last fetch. ``ba2_common.core.ohlcv_final_bars`` keeps them
    #: OFF the disk; live (``get_ohlcv_data`` for a LATEST request) still sees them, from here, for
    #: as long as they are unfinished. Process-local on purpose: a restart simply asks the vendor again.
    _UNFINISHED_MEMO: dict = {}
    _UNFINISHED_LOCK = threading.Lock()

    @staticmethod
    def _unfinished_key(provider_name: str, symbol: str, interval: str) -> tuple:
        from ba2_common.core.native_cache import normalize_interval
        return (provider_name, str(symbol).upper(), normalize_interval(interval))

    def _remember_unfinished_bars(self, frame: Optional[pd.DataFrame], symbol: str, interval: str,
                                  provider_name: str, *, record_empty: bool = False) -> None:
        """Keep the unfinished bars of a freshly FETCHED frame for the live overlay (never the disk).

        Call it only for a frame that has PASSED the top-up verdict (or that has nothing cached to
        disagree with): a refused vendor answer must never reach a live caller. ``record_empty``
        also records "asked now, no unfinished bar" so the memo throttles the next fetch."""
        if frame is None:
            return
        unfinished = (frame.iloc[0:0] if frame.empty else
                      ohlcv_final_bars.split_unfinished(frame, symbol, interval)[1])
        if unfinished.empty and not record_empty:
            return
        with type(self)._UNFINISHED_LOCK:
            type(self)._UNFINISHED_MEMO[self._unfinished_key(provider_name, symbol, interval)] = (
                _mono(), unfinished.drop(columns=['effective_date'], errors='ignore').copy())

    def _remember_failed_fetch(self, symbol: str, interval: str, provider_name: str) -> None:
        """Overlay ON only: record that the vendor WAS asked now and gave nothing usable, so
        ``_fetch_age`` throttles the next in-session read (one call per ``FORMING_BAR_TTL_S``) instead of
        re-firing the call on every read during an outage / a 429 storm. Memoises "no unfinished bar"."""
        if not ohlcv_final_bars.live_overlay_enabled():
            return
        key = self._unfinished_key(provider_name, symbol, interval)
        with type(self)._UNFINISHED_LOCK:
            previous = type(self)._UNFINISHED_MEMO.get(key)
            # keep the forming bar an EARLIER good fetch brought (it is still shown); only the clock moves
            kept = previous[1] if previous is not None else pd.DataFrame()
            type(self)._UNFINISHED_MEMO[key] = (_mono(), kept)

    def _forget_unfinished_bars(self, symbol: str, interval: str, provider_name: str) -> None:
        with type(self)._UNFINISHED_LOCK:
            type(self)._UNFINISHED_MEMO.pop(self._unfinished_key(provider_name, symbol, interval), None)

    def _fetch_age(self, symbol: str, interval: str, provider_name: str) -> Optional[float]:
        """Seconds since the last accepted vendor fetch memoized for this key, else ``None``."""
        with type(self)._UNFINISHED_LOCK:
            entry = type(self)._UNFINISHED_MEMO.get(self._unfinished_key(provider_name, symbol, interval))
        if entry is None:
            return None
        if not entry[1].empty and ohlcv_final_bars.split_unfinished(entry[1], symbol, interval)[1].empty:
            return None        # the bar the memo was holding has become final: the memo is obsolete
        return _mono() - entry[0]

    def _refused_now(self, symbol: str, interval: str, provider_name: str) -> bool:
        """True while a refused top-up is memoized for this key: every read must keep raising."""
        memo = type(self)._TOPUP_REFUSED.get((provider_name, str(symbol).upper(), interval))
        import time as _time
        return memo is not None and _time.monotonic() - memo[0] < self.TOPUP_REFUSAL_MEMO_S

    def _live_unfinished_bars(self, symbol: str, interval: str, provider_name: str):
        """``(age_s, rows)`` of the remembered bars that are STILL unfinished now, else ``None``
        (and a memo whose bars have become final is forgotten: the disk top-up supplies them)."""
        key = self._unfinished_key(provider_name, symbol, interval)
        with type(self)._UNFINISHED_LOCK:
            entry = type(self)._UNFINISHED_MEMO.get(key)
        if entry is None or entry[1].empty:
            return None
        _, still = ohlcv_final_bars.split_unfinished(entry[1], symbol, interval)
        if still.empty:
            with type(self)._UNFINISHED_LOCK:
                if type(self)._UNFINISHED_MEMO.get(key) is entry:
                    type(self)._UNFINISHED_MEMO.pop(key, None)
            return None
        return _mono() - entry[0], still

    def _overlay_unfinished_bars(self, df: pd.DataFrame, symbol: str, interval: str,
                                 provider_name: str) -> pd.DataFrame:
        """Append the remembered unfinished bar(s) NEWER than ``df``'s last bar to the frame RETURNED
        to a live caller. The disk copy is untouched."""
        if (not ohlcv_final_bars.live_overlay_enabled() or df is None or df.empty
                or self._refused_now(symbol, interval, provider_name)):
            return df
        live = self._live_unfinished_bars(symbol, interval, provider_name)
        if live is None:
            return df
        rows = live[1].copy()
        rows['Date'] = self._match_tz(pd.to_datetime(rows['Date']), pd.to_datetime(df['Date']))
        rows = rows[np.asarray(rows['Date'] > pd.to_datetime(df['Date']).max())]
        if rows.empty:
            return df
        if 'effective_date' in df.columns:
            rows['effective_date'] = rows['Date']
        rows = rows[[c for c in df.columns if c in rows.columns]]
        return pd.concat([df, rows], ignore_index=True)

    #: ``(provider class, interval) -> [symbols missing in the current burst, last bar date of the first]``.
    #: The "session open, no forming bar" report is ONE aggregated WARNING per burst, not one ERROR per symbol.
    _FORMING_MISSING_WINDOW: dict = {}
    _FORMING_MISSING_FLUSH_S = 5.0
    _FORMING_MISSING_LOCK = threading.Lock()

    def _note_forming_missing(self, symbol: str, interval: str, day: Any, frame: pd.DataFrame) -> None:
        cls = type(self)
        key = (cls.__name__, interval)
        last_bar = pd.Timestamp(frame['Date'].max()).date()
        with cls._FORMING_MISSING_LOCK:
            entry = cls._FORMING_MISSING_WINDOW.get(key)
            start_timer = entry is None
            if entry is None:
                entry = cls._FORMING_MISSING_WINDOW[key] = [set(), last_bar]
            entry[0].add(str(symbol).upper())

        def flush():
            with cls._FORMING_MISSING_LOCK:
                done = cls._FORMING_MISSING_WINDOW.pop(key, None)
            if done:
                syms = sorted(done[0])
                logger.warning(
                    f"{cls.__name__} ({interval}): the {day} session is open or settling but no forming bar "
                    f"could be obtained for {len(syms)} symbol(s) (e.g. {', '.join(syms[:5])}); their frames "
                    f"end at an OLDER session (e.g. {done[1]}). A live caller pricing off the last close "
                    f"would use it; every shipped expert prices from the account quote.")

        if start_timer:
            if cls._FORMING_MISSING_FLUSH_S <= 0:
                flush()
            else:
                t = threading.Timer(cls._FORMING_MISSING_FLUSH_S, flush)
                t.daemon = True
                t.start()

    def _stamp_forming_status(self, frame: pd.DataFrame, symbol: str, interval: str) -> None:
        """Mark a LATEST daily frame for a live caller (``ohlcv_final_bars.forming_bar_status`` /
        ``last_bar_is_today``). During an open or settling session a last row older than today means
        the forming bar could NOT be obtained: logged at ERROR (once per symbol per day) and
        ``attrs['ohlcv_forming_bar'] == 'missing'`` -- never a silent stale 'latest' bar."""
        status = "not_expected"
        if ohlcv_final_bars.live_overlay_enabled() and not frame.empty:
            now = datetime.now(timezone.utc)
            day = ohlcv_final_bars.forming_session_day(symbol, now)
            if day is not None:
                if ohlcv_final_bars.last_bar_is_today(frame, symbol, now):
                    status = "present"
                else:
                    status = "missing"
                    # ONE aggregated WARNING per burst (a pass over a basket would otherwise log one ERROR
                    # per symbol): the first miss of a burst starts a short timer, every symbol missing
                    # meanwhile joins the set, and the timer logs the count once.
                    self._note_forming_missing(symbol, interval, day, frame)
        frame.attrs[ohlcv_final_bars.FORMING_STATUS_ATTR] = status

    # ---- native parquet as_of store helpers (get_ohlcv_data) -----------------
    def _write_ohlcv_parquet(self, df: pd.DataFrame, provider_name: str,
                             symbol: str, interval: str) -> None:
        """MERGE a cleaned OHLCV frame into the parquet as_of store, stamping each
        bar's effective_date == its Date (OHLCV becomes public on its bar date, so
        a read sliced to effective_date<=as_of is no-lookahead).

        MERGE, not replace. This used to overwrite the file with just the frame it was
        handed, and the intraday cache-fill range is clamped to the CALLER's window —
        so one narrow miss destroyed a wide cache (e.g. a 1-day 5-minute fill wiping a
        3.5-year series). The truncated cache then made the next read for any other
        window miss too, and each miss re-downloaded and re-truncated: a self-sustaining
        re-fetch loop. Rows already present are kept (dedupe on Date, last write wins),
        so a re-write of the same range is a no-op.
        """
        from ba2_common.core import native_cache
        out = df.copy()
        out['Date'] = pd.to_datetime(out['Date'])
        out['effective_date'] = out['Date']

        existing = None
        path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        if path is not None:
            try:
                existing = pd.read_parquet(path)
            except Exception as e:
                # A corrupt cache must not block the refill; fall back to writing the
                # fresh frame alone (the pre-merge behaviour) rather than raising.
                logger.warning(f"Could not read {path} to merge, overwriting: {e}")
                existing = None

        if existing is not None and len(existing):
            existing = existing.copy()
            existing['Date'] = pd.to_datetime(existing['Date'])
            if 'effective_date' not in existing.columns:
                existing['effective_date'] = existing['Date']
            # Align the NEW rows' tz-awareness to the CACHE's convention before concat:
            # mixing aware and naive Dates yields an object column that later
            # pd.to_datetime calls reject. Matching the existing file (rather than
            # forcing one convention) keeps already-written caches byte-identical.
            out['Date'] = self._match_tz(out['Date'], existing['Date'])
            out['effective_date'] = out['Date']
            merged = pd.concat([existing, out], ignore_index=True)
            merged = (merged.drop_duplicates(subset=['Date'], keep='last')
                            .sort_values('Date')
                            .reset_index(drop=True))
            out = merged

        native_cache.write_timeseries(provider_name, symbol, interval, out)

    def _refresh_parquet_if_stale(
        self,
        df: pd.DataFrame,
        symbol: str,
        interval: str,
        provider_name: str,
    ) -> pd.DataFrame:
        """Incremental top-up of the parquet as_of store, for ANY interval.

        Fetches only the bars after the last cached one, appends them (stamping
        effective_date == Date), rewrites the parquet file, and returns the merged frame.
        Callers must only invoke this for a LATEST/live request — a pinned historical
        ``end_date`` is immutable and must never re-fetch (see ``_is_latest_request``).

        History (2026-07-28): this used to bail out unless the last cached bar was from
        TODAY (``if last_bar.date() != now.date(): return df``), so it could only extend a
        cache that was already current and could never catch up after a weekend, holiday
        or outage. Combined with it being called for intraday intervals only, daily caches
        were effectively write-once: every held symbol on the dev box had a last daily bar
        13-139 days old. The date-equality guard is gone; the ``expected_latest`` check
        below still prevents a redundant re-fetch inside the same bar.
        """
        df = df.copy()
        df['Date'] = pd.to_datetime(df['Date'])
        last_bar_dt = df['Date'].iloc[-1]
        last_bar_naive = (
            last_bar_dt.to_pydatetime().replace(tzinfo=None)
            if hasattr(last_bar_dt, 'to_pydatetime')
            else last_bar_dt
        )
        if last_bar_naive.tzinfo is not None:
            last_bar_naive = last_bar_naive.replace(tzinfo=None)

        now = datetime.now()
        interval_td = self._interval_to_timedelta(interval)
        expected_latest = self.normalize_time_to_interval(now, interval)
        if last_bar_naive >= expected_latest:
            return df
        if interval in _INTRADAY_INTERVALS:
            # The persisted last bar is now always the previous FINAL bar (a forming bar is never
            # written), so ``last_bar >= expected_latest`` no longer holds inside a bar: the memo of
            # the last fetch stands in for it, for one bar interval.
            length = ohlcv_final_bars.interval_length(interval)
            age = self._fetch_age(symbol, interval, provider_name)
            if age is not None and length is not None and age < length.total_seconds():
                return df

        fetch_start = last_bar_naive + interval_td
        fetch_end = now + interval_td
        if interval not in _INTRADAY_INTERVALS:
            # DAILY-or-longer: never append blindly across a split (APH 2026-09-28). See
            # _verified_tail_topup and ba2_common.core.ohlcv_topup_guard.
            df, action = self._verified_tail_topup(df, symbol, interval, provider_name, fetch_end)
            if action == "append":
                from ba2_common.core import native_cache
                try:
                    native_cache.write_timeseries(provider_name, symbol, interval, df)
                except OSError as e:   # the old top-up's policy: the verified frame is still served
                    logger.warning(f"Failed to refresh parquet cache for {symbol} ({interval}): {e}")
            return self._report_split_basis_drift(df, symbol, interval, provider_name)
        try:
            new_df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)
            if new_df is not None and not new_df.empty:
                new_df = self._clean_dataframe(new_df)
                new_df['Date'] = pd.to_datetime(new_df['Date'])
                # A forming bar is returned to the caller below but NEVER written (native_cache
                # drops it): the top-up resumes at last_bar + interval, so a persisted forming bar
                # would be frozen. Remember it for the live overlay.
                self._remember_unfinished_bars(new_df, symbol, interval, provider_name, record_empty=True)
                # Align the fetched bars' tz-awareness to the CACHE's convention before
                # concat. The provider may return tz-aware timestamps while the parquet
                # holds naive ones (FMP daily does exactly this); concatenating the two
                # yields an object column of mixed aware/naive values, and the
                # pd.to_datetime() in get_ohlcv_data then raises "Tz-aware datetime cannot
                # be converted to datetime64 unless utc=True". Matching the EXISTING cache
                # (rather than forcing one global convention) keeps already-working intraday
                # caches byte-identical instead of shifting their stored instants.
                new_df['Date'] = self._match_tz(new_df['Date'], df['Date'])
                new_df['effective_date'] = new_df['Date']
                if 'effective_date' not in df.columns:
                    df['effective_date'] = df['Date']
                df = pd.concat([df, new_df], ignore_index=True)
                df = (df.drop_duplicates(subset=['Date'])
                        .sort_values('Date')
                        .reset_index(drop=True))
                from ba2_common.core import native_cache
                native_cache.write_timeseries(provider_name, symbol, interval, df)
        except Exception as e:
            logger.warning(
                f"Failed to refresh parquet cache for {symbol} ({interval}): {e}"
            )
        return self._report_split_basis_drift(df, symbol, interval, provider_name)

    # ---- verified daily top-up (APH 2026-09-28) ---------------------------------------------
    #: ``(provider, SYMBOL, interval) -> (monotonic time, message)`` of a refused top-up. A refusal
    #: is re-raised from here for ``TOPUP_REFUSAL_MEMO_S`` instead of asking the vendor again on
    #: every read of the symbol; it is raised (loud) every time either way.
    _TOPUP_REFUSED: dict = {}
    TOPUP_REFUSAL_MEMO_S = 600.0

    def _topup_split_calendar(self, symbol: str, interval: str):
        """``(splits, failed)`` for the top-up guard: the provider's split calendar minus the rows
        an operator excluded (``split_basis_overrides`` ``exclude_calendar_event``), or ``None``
        when the provider has none; ``failed`` when it has one that could not be read now."""
        try:
            splits = self._split_calendar(symbol, interval)
        except Exception as e:  # noqa: BLE001 -- reported, and the guard then refuses a split-sized step
            logger.warning(f"Split calendar unavailable for {symbol} ({interval}); the top-up is "
                           f"verified on the overlap bars alone: {e}")
            return None, True
        if not splits:
            return splits, False
        from ba2_common.core import split_basis_overrides as ovr
        excluded = {e.event_date for e in ovr.overrides_for(symbol)
                    if e.kind == ovr.KIND_EXCLUDE_CALENDAR_EVENT}
        return [s for s in splits if s.date not in excluded], False

    def _unproven_tail_days(self, df: pd.DataFrame, symbol: str, interval: str,
                            provider_name: str) -> list:
        """``[newest cached day]`` when the file's mtime PROVES that bar was captured while its
        session was still open or settling (``ohlcv_final_bars.written_before_final``: the last write
        fell inside the bar's own trading window, nothing wrote the file since), else ``[]``.

        Only the newest bar can be proven this way: a later write of the file moved the mtime past
        every older bar's window. (Older stuck snapshots are the repair tool's job.) Never returns
        every row of ``df``: the guard needs one cached bar to anchor on."""
        from ba2_common.core import native_cache
        from ba2_common.core import ohlcv_topup_guard as guard
        path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        if path is None or len(df) < 2:
            return []
        try:
            written = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        except OSError:
            return []
        newest = guard.day_index(df['Date']).max()
        if ohlcv_final_bars.written_before_final(symbol, newest.date(), written):
            return [newest]
        return []

    def _verified_tail_topup(self, df: pd.DataFrame, symbol: str, interval: str,
                             provider_name: str, fetch_end: datetime, *,
                             raise_fetch_errors: bool = False):
        """Top ``df`` (a cached DAILY history) up to ``fetch_end`` without ever mixing split bases.

        Returns ``(frame, action)``:

        * ``"unchanged"`` -- nothing new (or the vendor could not be reached: logged, cached frame
          served, exactly the old top-up's failure policy -- unless ``raise_fetch_errors``, for a
          caller that reports its own failures);
        * ``"append"``    -- the vendor agrees with the last ``TOPUP_OVERLAP_BARS`` cached bars, so
          its newer bars are appended (and cached provisional snapshots among the compared bars
          replaced by the vendor's final bars). NOT written: the caller writes ``frame``;
        * ``"replaced"``  -- the vendor re-based its history (a split) or the new bars cross a
          calendar split: :meth:`force_full_refetch` REPLACED the cached history (written, marker
          included, the refuse-shorter check applied), after checking the replacement reproduces
          the vendor answer that triggered it. Logged at WARNING.

        Anything else raises ``OHLCVTopUpRefused`` and writes NOTHING: an overlap that disagrees
        with no split to explain it, a vendor that has not adjusted its pre-split bars yet, a
        split-sized step while the calendar is unreadable, or a failed replacement. Shared by the
        live refresh (``_refresh_parquet_if_stale``) and the test platform's ``fetch-cache``
        (``extend_ohlcv_cache``), the two writers that extend a cached history the backtests read.
        """
        import time as _time
        from ba2_common.core import ohlcv_topup_guard as guard

        switch = os.environ.get(FULL_REFETCH_ENV)
        if switch not in (None, "0", "1"):
            raise ValueError(f"{FULL_REFETCH_ENV} must be '0', '1' or unset, got {switch!r}")
        additive_only = switch == "0"

        key = (provider_name, str(symbol).upper(), interval)
        memo = type(self)._TOPUP_REFUSED.get(key)
        if memo is not None:
            if _time.monotonic() - memo[0] < self.TOPUP_REFUSAL_MEMO_S:
                raise guard.OHLCVTopUpRefused(
                    f"{memo[1]} [refused {int(_time.monotonic() - memo[0])}s ago; the vendor is "
                    f"asked again after {int(self.TOPUP_REFUSAL_MEMO_S)}s]")
            type(self)._TOPUP_REFUSED.pop(key, None)

        def refuse(msg: str, cause: Optional[BaseException] = None):
            full = (f"{provider_name} {symbol} ({interval}): top-up REFUSED, cache left untouched "
                    f"-- {msg}")
            type(self)._TOPUP_REFUSED[key] = (_time.monotonic(), full)
            self._forget_unfinished_bars(symbol, interval, provider_name)   # a refused answer is never served
            logger.error(full)
            if cause is not None:
                raise guard.OHLCVTopUpRefused(full) from cause
            raise guard.OHLCVTopUpRefused(full)

        df = df.copy()
        df['Date'] = pd.to_datetime(df['Date'])
        original = df
        # SELF-HEAL of a file written by the code that persisted forming bars: a cached newest bar
        # whose session was NOT final when the file was last written is a snapshot by construction
        # (mtime proof; nothing wrote the file since). It is taken out of the comparison and the
        # vendor's bar for that day, final now, replaces it. A bar the mtime cannot incriminate
        # (written after its session settled, or older than the newest bars) is left to the guard.
        healed = self._unproven_tail_days(df, symbol, interval, provider_name)
        if healed:
            logger.warning(f"{provider_name} {symbol} ({interval}): the cached bar(s) "
                           f"{[d.date().isoformat() for d in healed]} were written before their session "
                           f"was final (file mtime); replacing them with the vendor's final bar(s)")
            df = df[~np.asarray(guard.day_index(df['Date']).isin(healed))].reset_index(drop=True)
        # Every "nothing to write" exit returns the frame AS CACHED: a healed (dropped) bar is still on
        # disk, and the live frame must not lose it because the vendor had nothing to replace it with.
        nothing = original
        days = guard.day_index(df['Date'])
        tail_days = days[-guard.TOPUP_OVERLAP_BARS:]
        probe_start = tail_days[0].to_pydatetime()
        try:
            probe = self._get_ohlcv_data_impl(symbol, probe_start, fetch_end, interval)
        except Exception as e:  # noqa: BLE001 -- the old top-up's policy: serve the cache, say so
            # ATTEMPTED: memoized so a 429 storm does not re-fire the call on every read (overlay ON only)
            self._remember_failed_fetch(symbol, interval, provider_name)
            if raise_fetch_errors:
                raise
            logger.warning(f"Failed to refresh parquet cache for {symbol} ({interval}): {e}")
            return nothing, "unchanged"
        if probe is None or probe.empty:
            self._remember_failed_fetch(symbol, interval, provider_name)
            return nothing, "unchanged"
        probe = self._clean_dataframe(probe.copy())
        if probe.empty:
            self._remember_failed_fetch(symbol, interval, provider_name)
            return nothing, "unchanged"
        probe['Date'] = self._match_tz(pd.to_datetime(probe['Date']), df['Date'])
        # THE RULE (ohlcv_final_bars): the vendor's forming bar is never history. It is kept out of
        # the comparison, the merge and the write, so the verdict is computed on final bars only, and
        # it reaches live (memo) ONLY after the verdict accepted the vendor's basis (below).
        probe, forming = ohlcv_final_bars.split_unfinished(probe, symbol, interval)
        if probe.empty:
            # only the forming bar came back: nothing to compare it with, so it is NOT served
            logger.warning(f"{provider_name} {symbol} ({interval}): the vendor returned only an "
                           f"unfinished bar; it cannot be verified against the cache and is not used")
            self._remember_failed_fetch(symbol, interval, provider_name)
            return nothing, "unchanged"
        if healed and not np.asarray(guard.day_index(probe['Date']).isin(healed)).any():
            return nothing, "unchanged"            # no final bar to replace the healed one with
        probe_days = guard.day_index(probe['Date'])

        splits, calendar_failed = self._topup_split_calendar(symbol, interval)
        verdict = guard.verify_topup(df.tail(guard.TOPUP_OVERLAP_BARS), probe, splits,
                                     calendar_failed=calendar_failed, symbol=symbol)

        if verdict.appendable:
            last_day = days.max()
            fresh = probe[np.asarray(probe_days > last_day)].copy()
            prov = {pd.Timestamp(d) for d in verdict.provisional_days}
            if additive_only:
                prov = set()   # additive-only run: no cached bar is ever replaced, only appended after
            replace = probe[np.asarray(probe_days.isin(prov))].copy()
            self._remember_unfinished_bars(forming, symbol, interval, provider_name, record_empty=True)
            if fresh.empty and replace.empty:
                return (df, "append") if healed else (nothing, "unchanged")
            if 'effective_date' not in df.columns:
                df['effective_date'] = df['Date']
            if not replace.empty:
                df = df[~np.asarray(days.isin(prov))]
                logger.info(f"{provider_name} {symbol} ({interval}): replaced {len(replace)} cached "
                            f"provisional bar(s) {sorted(d.date().isoformat() for d in prov)} "
                            f"(snapshots taken before the session closed) with the vendor's final "
                            f"bars")
            new_df = pd.concat([replace, fresh], ignore_index=True)
            new_df['effective_date'] = new_df['Date']
            df = pd.concat([df, new_df], ignore_index=True)
            df = (df.drop_duplicates(subset=['Date'])
                    .sort_values('Date')
                    .reset_index(drop=True))
            return df, "append"

        if verdict.needs_full_refetch:
            if additive_only:
                # ADDITIVE-ONLY run (a data refresh while backtests of the old history are in
                # flight): a split since the last cached bar would REPLACE the whole history,
                # rescaling every old price. Deferred, loudly, and nothing is written.
                refuse(f"{verdict.reason}; the full re-fetch that would replace the cached history is "
                       f"DEFERRED ({FULL_REFETCH_ENV}=0, additive-only run: no cached bar is replaced and no "
                       f"history is rebuilt; the one exception is a newest bar the file mtime proves was "
                       f"written before its session was final, which is still healed)")
            logger.warning(
                f"{provider_name} {symbol} ({interval}): FULL RE-FETCH instead of a top-up -- "
                f"{verdict.reason}. Replacing the cached history (last cached bar "
                f"{days.max().date()}).")
            cached_days = [d.date() for d in tail_days]
            try:
                out = self.force_full_refetch(
                    symbol, interval, provider_name=provider_name,
                    verify=lambda fresh: guard.verify_replacement(probe, fresh, cached_days,
                                                                  symbol=symbol))
            except Exception as e:  # noqa: BLE001 -- ANY failure: the old basis must not be extended
                refuse(f"{verdict.reason}; the full re-fetch that must replace it failed "
                       f"({type(e).__name__}: {e})", cause=e)
            self._remember_unfinished_bars(forming, symbol, interval, provider_name, record_empty=True)
            return out, "replaced"

        refuse(verdict.reason)

    # ---- split-basis drift (market-condition source contract, plan Task 6) ----------
    #: Providers whose daily history is delivered split-adjusted AS OF THE FETCH set this, so a
    #: cold full fill records a full-fetch marker (``ba2_common.core.split_basis``) and a later
    #: split check knows the pre-split bars were fetched after the split.
    WRITES_FULL_FETCH_MARKER = False

    def _split_calendar(self, symbol: str, interval: str):
        """The provider's split calendar for ``symbol`` as ``[CalendarSplit]``, or ``None`` when
        this provider has none (no drift check is possible then). Overridden by providers whose
        appended cache can drift across a split (FMP)."""
        return None

    #: ``(provider, symbol, interval)`` already reported as split-basis suspect in this process.
    #: The check runs on every daily top-up, and the answer only changes when the cache is
    #: repaired -- one WARNING per symbol per process, not one per refresh.
    _SPLIT_BASIS_REPORTED: set = set()

    def _report_split_basis_drift(self, df: pd.DataFrame, symbol: str, interval: str,
                                  provider_name: str) -> pd.DataFrame:
        """REPORT (never repair) a cached history that is not verifiably on one split basis.

        The top-up used to APPEND bars after the last cached one blindly, so a symbol that split
        after its file was first fetched holds unadjusted pre-split bars next to adjusted
        post-split bars -- a fake 2x/4x/10x move every reader (the market-condition gates
        included) would take as real. Since 2026-09-28 (APH) the daily top-up itself refuses to
        write such a file (:meth:`_verified_tail_topup`); this report covers the files damaged
        before that, and anything the overlap check cannot see. Daily-or-longer intervals only. A split-calendar failure is logged and the refresh
        result is served unchanged.

        IT ONLY REPORTS, deliberately. This runs inside the LIVE analysis pass, on every stale
        daily cache, for every expert -- gated or not, since the refresh path knows nothing about
        the market-condition feature. An automatic repair here would mean an unbounded,
        unrate-limited 15-year re-fetch per symbol inside a decision pass, and on the FIRST
        refresh after this feature ships NO file carries a full-fetch marker, so every symbol
        with a historical split factor below ``MIN_DETECTABLE_FACTOR`` (3-for-2, 5-for-4, ...)
        classifies ``undetectable`` and would trigger one. The repair belongs to a tool an
        operator runs on purpose: ``tools/warm_market_conditions.py plan`` reports exactly these
        symbols and its ``--fetch-missing`` path calls :meth:`force_full_refetch` for them.

        The warmup preflight refuses a suspect symbol loudly, so a GATED strategy cannot quietly
        trade on drifted prices; an ungated one keeps the behaviour it has always had, plus this
        warning."""
        if interval in _INTRADAY_INTERVALS or df is None or df.empty:
            return df
        try:
            splits = self._split_calendar(symbol, interval)
        except Exception as e:
            logger.warning(f"Split calendar unavailable for {symbol} ({interval}); split-basis drift "
                           f"not checked on this refresh: {e}")
            return df
        if not splits:
            return df
        from ba2_common.core import native_cache
        from ba2_common.core.split_basis import (
            REFETCH_VERDICTS, check_split_basis, needs_full_refetch, read_full_fetch_marker,
        )
        path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        dates = pd.to_datetime(df['Date'])
        if getattr(dates.dt, 'tz', None) is not None:
            dates = dates.dt.tz_localize(None)
        days = dates.dt.normalize().to_numpy(dtype='datetime64[ns]')
        checks = check_split_basis(days, df['Open'], df['High'], df['Low'], df['Close'], splits,
                                   symbol=symbol, marker=read_full_fetch_marker(path))
        if not needs_full_refetch(checks):
            return df
        key = (provider_name, str(symbol).upper(), interval)
        if key not in type(self)._SPLIT_BASIS_REPORTED:
            type(self)._SPLIT_BASIS_REPORTED.add(key)
            bad = [c.to_dict() for c in checks if c.verdict in REFETCH_VERDICTS]
            logger.warning(
                f"{provider_name} {symbol} ({interval}): cached history is not verifiably on one "
                f"split basis ({bad}). NOT repaired here -- a full re-fetch inside a live pass is "
                f"unbounded. Run tools/warm_market_conditions.py plan (and --fetch-missing) to "
                f"repair {symbol}; until then a market-condition warmup refuses it.")
        return df

    def force_full_refetch(self, symbol: str, interval: str = '1d',
                           provider_name: Optional[str] = None, *,
                           verify=None) -> pd.DataFrame:
        """REPLACE (never merge) the cached daily history of ``symbol`` with a fresh full-history
        fetch -- the same 15-year window as the cold fill -- and record the full-fetch marker.

        Merging would keep the stale pre-split bars, which is exactly what this repairs.

        REFUSES a replacement that would LOSE history: shorter than what is cached, or starting
        later. ``empty`` alone was the only guard, and a short-but-non-empty answer -- a capped
        plan or endpoint, a partial payload, a vendor-shortened history -- would overwrite fifteen
        years of daily bars irrecoverably in the cache the live platform, every backtest and every
        warmed snapshot read. Worse, the marker written afterwards makes every later
        ``check_split_basis`` return ``refetched`` and skip the check, so the loss would hide
        itself. The marker is therefore written only after a replacement that passed this.

        ``verify`` (optional) is called with the replacement frame after those checks and before
        anything is written; it raises to refuse the replacement (the verified top-up uses it to
        demand that the re-fetch reproduces the vendor answer that triggered it).

        Raises:
            RuntimeError: the fetch returned nothing, or returned less than the cache already
                holds, or ``verify`` refused it (the existing file is left untouched in every
                case)."""
        from ba2_common.core import native_cache
        from ba2_common.core.split_basis import write_full_fetch_marker

        provider_name = provider_name or type(self).__name__
        now = datetime.now()
        existing_path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        # ASK FOR AT LEAST WHAT THE CACHE ALREADY HOLDS. The default reach is 15 years, but a
        # cache that starts EARLIER than that would make any faithful answer look short, and
        # `_refuse_shorter_replacement` would refuse the repair for ever -- observed on the grid
        # host, where T/WDC hold 3777 bars from 2011-06-22 while a 15-year request can only
        # return 3769 from 2011-09-20. Widening the request is the fix; relaxing the guard would
        # trade a false refusal for the silent truncation the guard exists to prevent.
        start = now - timedelta(days=365 * 15)
        if existing_path is not None:
            try:
                first = pd.to_datetime(pd.read_parquet(existing_path, columns=['Date'])['Date']).min()
                first = first.tz_localize(None) if getattr(first, 'tzinfo', None) else first
                start = min(start, first.to_pydatetime())
            except Exception as e:  # noqa: BLE001 -- unreadable here is not fatal: the guard
                # below re-reads the file and refuses rather than overwriting what it cannot check.
                logger.warning(f"Could not read {existing_path} to size the re-fetch window: {e}")
        fresh = self._get_ohlcv_data_impl(symbol, start, now, interval)
        if fresh is None or fresh.empty:
            raise RuntimeError(f"full re-fetch of {symbol} ({interval}) returned no bars")
        out = self._clean_dataframe(fresh.copy())
        out['Date'] = pd.to_datetime(out['Date'])
        if existing_path is not None:
            try:
                existing_dates = pd.to_datetime(pd.read_parquet(existing_path, columns=['Date'])['Date'])
                out['Date'] = self._match_tz(out['Date'], existing_dates)
            except Exception as e:
                logger.warning(f"Could not read {existing_path} to match its timezone convention: {e}")
        out = out.drop_duplicates(subset=['Date'], keep='last').sort_values('Date').reset_index(drop=True)
        out['effective_date'] = out['Date']
        # THE RULE (ohlcv_final_bars): a split-triggered re-fetch DURING a session returns today's
        # forming bar too. It is kept for live, and out of the replacement, the verification, the
        # write and the marker (whose last_bar must be the last FINAL session).
        out, forming = ohlcv_final_bars.split_unfinished(out, symbol, interval)
        if out.empty:
            raise RuntimeError(f"full re-fetch of {symbol} ({interval}) returned only unfinished bars")
        out = out.reset_index(drop=True)
        self._refuse_shorter_replacement(out, existing_path, symbol, interval, provider_name)
        if verify is not None:
            verify(out)
        native_cache.write_timeseries(provider_name, symbol, interval, out)
        path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        write_full_fetch_marker(path, first_bar=pd.Timestamp(out['Date'].iloc[0]).date(),
                                last_bar=pd.Timestamp(out['Date'].iloc[-1]).date(), rows=len(out))
        logger.info(f"{provider_name} {symbol} ({interval}): replaced the cache with {len(out)} "
                    f"freshly fetched bars")
        self._remember_unfinished_bars(forming, symbol, interval, provider_name)   # only after a verified, written replacement
        return out

    @staticmethod
    def _refuse_shorter_replacement(out: pd.DataFrame, existing_path: Optional[str], symbol: str,
                                    interval: str, provider_name: str) -> None:
        """Refuse a full-refetch replacement that holds LESS history than the file it replaces.

        Compared on both axes, because either alone can be satisfied by a bad answer: the ROW
        COUNT (a capped response) and the FIRST BAR (a vendor that shortened its history window).
        A file that cannot be read is not treated as "no history" -- that would turn the one
        failure this guards into a silent pass -- so an unreadable existing file refuses too.
        """
        if existing_path is None:
            return                      # nothing cached yet: a cold fill cannot lose anything
        try:
            existing = pd.read_parquet(existing_path, columns=['Date'])
        except Exception as e:
            raise RuntimeError(
                f"full re-fetch of {symbol} ({interval}): the existing cache {existing_path} "
                f"could not be read to check the replacement against it ({e}). Refusing to "
                f"overwrite it -- a replacement that cannot be compared is not a repair.") from e
        # an unfinished bar a pre-fix writer left on the disk is not history the replacement must keep
        existing, _ = ohlcv_final_bars.split_unfinished(existing, symbol, interval)
        if existing.empty:
            return
        old_rows = len(existing)
        old_first = pd.Timestamp(pd.to_datetime(existing['Date']).min()).date()
        new_rows = len(out)
        new_first = pd.Timestamp(pd.to_datetime(out['Date']).min()).date()
        if new_rows >= old_rows and new_first <= old_first:
            return
        raise RuntimeError(
            f"full re-fetch of {symbol} ({interval}) returned LESS history than the cache holds "
            f"({new_rows} rows from {new_first}, cached {old_rows} rows from {old_first}) -- "
            f"refusing to replace {provider_name}'s cache and lose the difference. This is what "
            f"a capped plan, a partial payload or a shortened vendor window looks like; the "
            f"cache file and its split-basis marker are left untouched.")

    def _record_cold_full_fetch(self, provider_name: str, symbol: str, interval: str) -> None:
        """A cold daily fill of an ABSENT file is a full-history fetch: record the marker."""
        from ba2_common.core import native_cache
        from ba2_common.core.split_basis import write_full_fetch_marker
        path = native_cache.find_timeseries_path(provider_name, symbol, interval)
        if path is None:
            logger.info(f"{provider_name} {symbol} ({interval}): no file was written by the cold fill "
                        f"(only unfinished bars came back); no full-fetch marker recorded")
            return
        try:
            dates = pd.to_datetime(pd.read_parquet(path, columns=['Date'])['Date'])
            if len(dates):
                write_full_fetch_marker(path, first_bar=pd.Timestamp(dates.iloc[0]).date(),
                                        last_bar=pd.Timestamp(dates.iloc[-1]).date(), rows=len(dates))
        except Exception as e:
            logger.warning(f"Could not record the full-fetch marker for {path}: {e}")

    @staticmethod
    def _match_tz(new_dates: 'pd.Series', existing_dates: 'pd.Series') -> 'pd.Series':
        """Return ``new_dates`` with the same tz-awareness as ``existing_dates``.

        Aware -> naive converts through UTC first so the instant is preserved rather than
        the wall-clock reading being reinterpreted. Naive -> aware localizes to the
        existing series' tz. Used when merging freshly fetched bars into a cached frame,
        where the two sides can disagree (see the caller).
        """
        new_dates = pd.to_datetime(new_dates)
        existing_tz = getattr(existing_dates.dt, 'tz', None) if len(existing_dates) else None
        new_tz = getattr(new_dates.dt, 'tz', None)
        if existing_tz is None and new_tz is not None:
            return new_dates.dt.tz_convert('UTC').dt.tz_localize(None)
        if existing_tz is not None and new_tz is None:
            return new_dates.dt.tz_localize(existing_tz)
        if existing_tz is not None and new_tz is not None and str(new_tz) != str(existing_tz):
            return new_dates.dt.tz_convert(existing_tz)
        return new_dates

    @staticmethod
    def _is_latest_request(requested_end: Optional[datetime]) -> bool:
        """True when the caller is asking for the LATEST data (live), not a pinned past as_of.

        Two live spellings must both count: ``end_date=None`` (validate_date_range defaults
        it to now) and an explicit "now" — ``ba2_providers.cache.cached_get.ohlcv_get`` does
        ``end = as_of or datetime.now(timezone.utc)``, so live arrives as an explicit
        tz-aware now. A backtest pins a real historical ``end_date`` and must be excluded so
        its reads stay byte-identical (immutable history, no re-fetch).

        The 1-hour tolerance covers clock/timezone skew between a naive ``datetime.now()``
        and a tz-aware "now" without admitting a genuine backtest date.
        """
        if requested_end is None:
            return True
        end = requested_end
        if end.tzinfo is not None:
            end = end.astimezone(timezone.utc).replace(tzinfo=None)
        return end >= datetime.utcnow() - timedelta(hours=1)

    def get_data(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        interval: str = '1d',
        use_cache: bool = True,
        max_cache_age_hours: int = 24,
        lookback_days: int = 30
    ) -> List[MarketDataPoint]:
        """
        Get market data with smart caching and optional date range.
        
        This is the main public method to fetch data. It handles:
        1. Calculate missing date parameters using lookback_days
        2. Normalize start/end dates to interval boundaries
        3. Cache validation (check if cache exists and is recent)
        4. Loading from cache if valid
        5. Fetching from source if cache invalid
        6. Filtering to requested date range
        7. Converting to MarketDataPoint objects
        
        OPTIONAL DATE LOGIC:
        - If both start_date and end_date are provided: uses them as-is
        - If end_date is None: defaults to current date
        - If start_date is None: defaults to end_date - lookback_days
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data (optional, will be floored to interval boundary).
                If None, defaults to end_date - lookback_days
            end_date: End date for data (optional). If None, defaults to current date
            interval: Data interval ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1wk', '1mo')
            use_cache: Whether to use caching (default: True)
            max_cache_age_hours: Maximum age of cache in hours (default: 24)
            lookback_days: Days to look back if dates not provided (default: 30)
        
        Returns:
            List of MarketDataPoint objects for the requested date range
        
        Raises:
            Exception: If data fetching fails
        """
        # Validate and normalize dates with intelligent optional handling
        start_date, end_date = validate_date_range(start_date, end_date, lookback_days)

        # Normalize start_date to interval boundary for proper time series alignment
        normalized_start = self.normalize_time_to_interval(start_date, interval)

        logger.info(f"Getting data for {symbol} from {normalized_start.date()} to {end_date.date()}, interval={interval}")

        cache_file = self._get_cache_file_path(symbol, interval)
        df = None

        # Try to load from cache if enabled
        if use_cache and self._is_cache_valid(cache_file, max_cache_age_hours):
            df = self._load_cache(cache_file)

        # Fetch from source if cache invalid or disabled
        if df is None:
            logger.info(f"Fetching data from source for {symbol}")
            
            # Determine fetch range based on interval (Yahoo Finance limits)
            # Intraday data (1m, 5m, 15m, 30m, 1h, 4h): max 730 days
            # Daily data (1d, 1wk, 1mo): can go back 15 years
            if interval in ['1m', '5m', '15m', '30m', '1h', '4h']:
                # Intraday data limited to ~2 years (730 days)
                fetch_start = normalized_start - timedelta(days=729)  # Use 729 to be safe
            else:
                # Daily/weekly/monthly data can go back 15 years
                fetch_start = normalized_start - timedelta(days=365 * 3)
            
            fetch_end = end_date if end_date else datetime.now()
            
            df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)

            if df is None or df.empty:
                raise Exception(f"Failed to fetch data for {symbol}")

            # Clean and harden against malformed data
            df = self._clean_dataframe(df)

            # Save to cache
            if use_cache:
                self._save_final_bars_cache(df, symbol, interval, cache_file)

        # Ensure Date column is datetime
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])

        # Make start_date and end_date timezone-aware if df['Date'] is timezone-aware
        filter_start = normalized_start
        filter_end = end_date
        if hasattr(df['Date'], 'dt') and df['Date'].dt.tz is not None:
            # DataFrame has timezone-aware dates, convert filter dates to match
            from datetime import timezone as tz
            if normalized_start.tzinfo is None:
                filter_start = normalized_start.replace(tzinfo=tz.utc)
            if end_date.tzinfo is None:
                filter_end = end_date.replace(tzinfo=tz.utc)

        # Filter to requested date range (using normalized start)
        mask = (df['Date'] >= filter_start) & (df['Date'] <= filter_end)
        filtered_df = df[mask].copy()

        logger.info(f"Filtered to {len(filtered_df)} records in date range")

        # Convert to MarketDataPoint objects
        datapoints = self._dataframe_to_datapoints(filtered_df, symbol, interval)

        logger.info(f"Returning {len(datapoints)} MarketDataPoint objects")

        return datapoints

    @observe_provider(
        "market_data", "get_ohlcv_data",
        identity=ohlcv_identity,
        before=lambda a: _ohlcv_cache_counters(),
        provenance=lambda a, result, before: _ohlcv_provenance(a, before),
    )
    def get_ohlcv_data(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        interval: str = '1d',
        use_cache: bool = True,
        max_cache_age_hours: int = 24,
        lookback_days: int = 30
    ) -> pd.DataFrame:
        """
        Get OHLCV (Open, High, Low, Close, Volume) market data as DataFrame.
        
        This is the main public method for retrieving market data with caching support.
        Use this method instead of calling _get_ohlcv_data_impl() directly.
        
        OPTIONAL DATE LOGIC:
        - If both start_date and end_date are provided: uses them as-is
        - If end_date is None: defaults to current date
        - If start_date is None: defaults to end_date - lookback_days
        
        Args:
            symbol: Ticker symbol
            start_date: Start date for data (optional, will be floored to interval boundary).
                If None, defaults to end_date - lookback_days
            end_date: End date for data (optional). If None, defaults to current date
            interval: Data interval
            use_cache: Whether to use caching
            max_cache_age_hours: Maximum age of cache in hours
            lookback_days: Days to look back if dates not provided (default: 30)
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        """
        # Capture the caller's ORIGINAL end_date before validate_date_range defaults it to
        # now: that is the only way to tell a live "latest" request from a pinned backtest
        # as_of once normalization has run (see _is_latest_request).
        is_latest = self._is_latest_request(end_date)

        # Validate and normalize dates with intelligent optional handling
        start_date, end_date = validate_date_range(start_date, end_date, lookback_days)

        # Normalize start_date to interval boundary for proper time series alignment
        normalized_start = self.normalize_time_to_interval(start_date, interval)

        # ----------------------------------------------------------------- #
        # Native parquet as_of cache (replaces the legacy per-class CSV cache).
        #
        # The parquet store is keyed on (provider, symbol, interval) and every
        # bar carries effective_date == its Date, so a read sliced to
        # effective_date<=as_of is no-lookahead by construction. We use the
        # resolved ``end_date`` as the as_of ceiling: because effective_date==Date,
        # slicing to Date<=end_date here and then re-applying the final
        # Date<=filter_end mask below is idempotent, so the rows returned are
        # byte-equivalent to the legacy path for BOTH live (end_date defaults to
        # now -> latest) and backtest (end_date pinned) requests.
        #
        # First (symbol,interval) request: a cache miss fetches a BOUNDED history
        # once via _get_ohlcv_data_impl, cleans it, stamps effective_date, and
        # writes parquet. Subsequent requests for any as_of read a slice from the
        # same parquet file with no re-fetch (cache-once).
        # ----------------------------------------------------------------- #
        from ba2_common.core import native_cache

        provider_name = type(self).__name__
        df = None
        if use_cache:
            df = native_cache.read_timeseries(provider_name, symbol, interval, as_of=end_date)
            if df is not None and df.empty:
                # An EMPTY as_of slice is NOT the same thing as an absent cache.
                #
                # read_timeseries returns the rows with effective_date<=as_of. For a
                # symbol whose FIRST cached bar postdates the as_of that slice is
                # legitimately empty — the instrument had not traded yet — and the
                # right answer is an empty frame, served free from disk.
                #
                # Mapping it to ``df = None`` (as this did) sent it into the cold-fetch
                # branch below, which for daily asks for now-15y..now: a ~450 KB
                # ``historical-price-full`` payload. The fetch cannot add rows below the
                # symbol's real first bar, so the NEXT read slices to empty again and
                # re-downloads — unbounded, once per read, forever. On the dev cache
                # 5 640 of 10 919 ``*_1d.parquet`` files (51.7%) have a first bar after
                # 2020-01-01, which is the window the 2020 forward test replays; FMP
                # billed 146.26k historical-price-full calls / 65.25 GB in one month,
                # and 65.25 GB / 146.26k = ~446 KB = exactly one full-history payload
                # per call.
                #
                # Only an ABSENT file, or one holding NO bars at all (a broken cache
                # that must be allowed to refill), is a real miss.
                if not native_cache.timeseries_row_count(provider_name, symbol, interval):
                    df = None
            # LATEST/live request: top the cache up so we never serve indefinitely stale
            # bars. A pinned historical end_date is immutable and skips this entirely, so
            # backtest reads stay byte-identical.
            #
            # INTRADAY has no age gate — it must stay current within the bar, and
            # _refresh_parquet_if_stale's own ``expected_latest`` check already suppresses a
            # redundant fetch inside the same bar.
            #
            # DAILY/weekly/monthly are gated on the parquet file's mtime via
            # max_cache_age_hours (the documented-but-previously-ignored parameter). Without
            # that gate every live daily read between sessions would re-hit the API (60+
            # symbols x many cycles -> FMP rate limiting), since the last bar legitimately
            # is not today on a Monday morning or over a weekend.
            #
            # ``not df.empty`` guards the top-up: since an empty as_of slice is now a
            # HIT (see above), df can legitimately hold zero rows here, and
            # _refresh_parquet_if_stale indexes ``df['Date'].iloc[-1]``. An empty slice
            # at a LATEST as_of would mean the cache's every bar is in the future,
            # which is not a staleness problem to fix by fetching.
            live_clock = forming_day = None
            if (df is not None and not df.empty and is_latest and interval not in _INTRADAY_INTERVALS
                    and ohlcv_final_bars.live_overlay_enabled()
                    and ohlcv_final_bars.is_daily_interval(interval)):
                live_clock = datetime.now(timezone.utc)
                forming_day = ohlcv_final_bars.forming_session_day(symbol, live_clock)
            if df is not None and not df.empty and is_latest:
                if interval in _INTRADAY_INTERVALS:
                    df = self._refresh_parquet_if_stale(
                        df, symbol, interval, provider_name)
                else:
                    cache_path = native_cache.find_timeseries_path(
                        provider_name, symbol, interval)
                    mtime_stale = cache_path is None or not self._is_cache_valid(
                        cache_path, max_cache_age_hours)
                    if self._refused_now(symbol, interval, provider_name):
                        # a refused top-up keeps raising on EVERY read (never skipped, never overlaid)
                        df = self._refresh_parquet_if_stale(df, symbol, interval, provider_name)
                    elif live_clock is not None and forming_day is not None:
                        # A session is open or settling (open <= now < close + 4 h): the file's mtime
                        # says nothing about today's forming bar (it is never written, and the 592
                        # repaired files are "fresh" by mtime). The memo of the last accepted fetch
                        # throttles this to one vendor call per FORMING_BAR_TTL_S. Outside that window
                        # the mtime gate alone decides, as it always did (a final bar that appeared
                        # after the 20:00 ET settlement is fetched at the next in-session read or
                        # when the file ages out).
                        age = self._fetch_age(symbol, interval, provider_name)
                        if age is None or age >= FORMING_BAR_TTL_S:
                            df = self._refresh_parquet_if_stale(df, symbol, interval, provider_name)
                    elif mtime_stale:
                        df = self._refresh_parquet_if_stale(df, symbol, interval, provider_name)

        # Fetch from source if needed (cache miss or caching disabled)
        if df is None:
            logger.info(f"Fetching DataFrame from source for {symbol}")

            # Determine the cache-fill range by interval. INTRADAY (both short '5m' and FMP
            # long-form '5min' spellings): honour the REQUESTED start so we fetch only the range
            # the caller needs (providers like FMP serve several years of intraday). The previous
            # code (a) listed only short forms, so '5min'/'1hour' fell through to the DAILY branch
            # and forced a wasteful ~15-year fetch (mostly empty pre-2021), and (b) ignored the
            # requested start entirely. DAILY/weekly/monthly still pre-fill a deep 15-year window.
            if interval in _INTRADAY_INTERVALS:
                fetch_start = normalized_start or (datetime.now() - timedelta(days=365 * 2))
                # Clamp the cache-fill END to the REQUESTED end_date (the backtest window) rather
                # than "now". A historical intraday backtest only needs [start, end_date], but
                # fetching to now pulled YEARS of extra bars (e.g. ~16x for a 2-week 5min window
                # against a 2.5y-old date) and triggered FMP rate-limiting (the dominant cold-fetch
                # cost). ``end_date`` is non-None here (validate_date_range defaults it to now), so
                # a LIVE request (end_date -> now) still fills to now. Warm reads of a PAST window
                # do NOT re-fetch: the top-up above is gated on ``is_latest``, which is False for a
                # pinned historical end_date. Backtest results are unchanged: the engine only ever
                # consumes bars in [start, end_date] regardless of how far the fill reached.
                fetch_end = end_date if end_date is not None else datetime.now()
            else:
                # Daily/weekly/monthly data can go back 15 years
                fetch_start = datetime.now() - timedelta(days=365 * 15)
                fetch_end = datetime.now()

            df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)

            if df is None or df.empty:
                raise Exception(f"Failed to fetch data for {symbol}")

            # Clean and harden against malformed data
            df = self._clean_dataframe(df)

            # Save to the parquet as_of store (effective_date == bar Date).
            if use_cache:
                # a cold fill during a session fetched today's forming bar too: live keeps it
                # (overlay below), the writer never persists it
                self._remember_unfinished_bars(df, symbol, interval, provider_name, record_empty=True)
                cold_file = native_cache.find_timeseries_path(provider_name, symbol, interval) is None
                self._write_ohlcv_parquet(df, provider_name, symbol, interval)
                if cold_file and self.WRITES_FULL_FETCH_MARKER and interval not in _INTRADAY_INTERVALS:
                    self._record_cold_full_fetch(provider_name, symbol, interval)
                # Re-read the as_of slice so the returned frame is byte-equivalent
                # to a subsequent cache-hit read (same effective_date<=end_date cut).
                sliced = native_cache.read_timeseries(
                    provider_name, symbol, interval, as_of=end_date)
                if sliced is not None and not sliced.empty:
                    df = sliced

        # LIVE still sees today's forming bar (DeterministicScorer's decision price is the last
        # close of this frame): served from the process memo, never from the disk.
        if use_cache and is_latest:
            df = self._overlay_unfinished_bars(df, symbol, interval, provider_name)

        # Drop the cache-internal effective_date column so the returned frame keeps
        # the legacy shape (Date, Open, High, Low, Close, Volume) byte-equivalently.
        if 'effective_date' in df.columns:
            df = df.drop(columns=['effective_date'])

        # Ensure Date column is datetime
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])

        # Handle timezone compatibility between DataFrame and filter dates
        from datetime import timezone as tz
        
        # Check if DataFrame has timezone-aware dates
        df_has_tz = hasattr(df['Date'], 'dt') and df['Date'].dt.tz is not None
        
        # Check if filter dates have timezone info
        start_has_tz = normalized_start.tzinfo is not None
        end_has_tz = end_date.tzinfo is not None
        
        filter_start = normalized_start
        filter_end = end_date
        
        if df_has_tz and not (start_has_tz and end_has_tz):
            # DataFrame is tz-aware, make filter dates tz-aware to match
            if not start_has_tz:
                filter_start = normalized_start.replace(tzinfo=tz.utc)
            if not end_has_tz:
                filter_end = end_date.replace(tzinfo=tz.utc)
        elif not df_has_tz and (start_has_tz or end_has_tz):
            # DataFrame is tz-naive, make it tz-aware to match filter dates
            df['Date'] = df['Date'].dt.tz_localize(tz.utc)
            # Also update filter dates if they weren't timezone-aware
            if not start_has_tz:
                filter_start = normalized_start.replace(tzinfo=tz.utc)
            if not end_has_tz:
                filter_end = end_date.replace(tzinfo=tz.utc)
        
        # Filter to requested date range (using normalized start)
        mask = (df['Date'] >= filter_start) & (df['Date'] <= filter_end)
        filtered_df = df[mask].copy()
        
        if use_cache and is_latest and ohlcv_final_bars.is_daily_interval(interval):
            self._stamp_forming_status(filtered_df, symbol, interval)
        # logger.debug(f"Returning DataFrame with {len(filtered_df)} records")
        
        return filtered_df
    
    def clear_cache(self, symbol: Optional[str] = None, interval: Optional[str] = None):
        """
        Clear cache files.
        
        Args:
            symbol: If provided, only clear cache for this symbol
            interval: If provided, only clear cache for this interval
        """
        if symbol and interval:
            # Clear specific cache file
            cache_file = self._get_cache_file_path(symbol, interval)
            if os.path.exists(cache_file):
                os.remove(cache_file)
                logger.info(f"Cleared cache for {symbol} {interval}")
        else:
            # Clear all cache files
            for file in os.listdir(self.cache_folder):
                if file.endswith('.csv'):
                    os.remove(os.path.join(self.cache_folder, file))
            logger.info(f"Cleared all cache files in {self.cache_folder}")
    
    def _format_ohlcv_as_markdown(self, data: dict) -> str:
        """Format OHLCV data as markdown table."""
        interval = data.get('interval', '1d')
        
        md = f"# OHLCV Data: {data['symbol']}\n\n"
        md += f"**Interval:** {interval}  \n"
        
        # Format period dates based on interval
        start_display = self.format_datetime_for_markdown(data.get('start_date'), interval)
        end_display = self.format_datetime_for_markdown(data.get('end_date'), interval)
        md += f"**Period:** {start_display} to {end_display}  \n"
        md += f"**Data Points:** {len(data.get('data', []))}  \n\n"
        
        if data.get('data'):
            md += "## Price Data\n\n"
            
            # Determine date column header based on interval
            date_header = "Date" if any(interval.endswith(s) for s in ['d', 'wk', 'mo']) else "DateTime"
            
            md += f"| {date_header} | Open | High | Low | Close | Volume |\n"
            md += "|------|------|------|-----|-------|--------|\n"
            
            # Show all data points
            for point in data['data']:
                # Format date based on interval
                date_str = self.format_datetime_for_markdown(point.get('date'), interval)
                
                md += (
                    f"| {date_str} | "
                    f"${point['open']:.2f} | "
                    f"${point['high']:.2f} | "
                    f"${point['low']:.2f} | "
                    f"${point['close']:.2f} | "
                    f"{point['volume']:,} |\n"
                )
            
            md += f"\n*Total data points: {len(data['data'])}*\n"
        else:
            md += "## Price Data\n\n"
            md += "*No data available for the specified period*\n"
        
        return md
    
    @log_provider_call
    def get_ohlcv_data_formatted(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        interval: str = "1d",
        format_type: str = "markdown"
    ) -> dict | str:
        """
        Get OHLCV data with flexible date parameters and formatting.
        
        This method handles:
        - Date range calculation (start_date/end_date OR lookback_days)
        - Data fetching via get_ohlcv_data()
        - Formatting as dict, markdown, or both
        - Logging via @log_provider_call decorator
        
        Subclasses should NOT override this method. Instead, implement _get_ohlcv_data_impl().
        
        Args:
            symbol: Stock ticker symbol
            start_date: Start date (use either this OR lookback_days, not both)
            end_date: End date (defaults to now if not provided)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            interval: Data interval (1m, 5m, 15m, 1h, 1d)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            If format_type='dict': Dictionary with OHLCV data
            If format_type='markdown': Formatted markdown string
            If format_type='both': Dict with keys 'text' (markdown) and 'data' (dict)
        """
        from ba2_common.core.provider_utils import calculate_date_range, validate_date_range
        from datetime import timezone
        
        # Calculate actual_start_date based on parameters
        if lookback_days:
            if start_date:
                raise ValueError("Provide either start_date OR lookback_days, not both")
            if not end_date:
                end_date = datetime.now(timezone.utc)
            actual_start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            if not start_date:
                raise ValueError("Must provide either start_date or lookback_days")
            if not end_date:
                end_date = datetime.now(timezone.utc)
            actual_start_date, end_date = validate_date_range(start_date, end_date)
        
        # Get data as DataFrame using caching-enabled method
        df = self.get_ohlcv_data(
            symbol=symbol,
            start_date=actual_start_date,
            end_date=end_date,
            interval=interval
        )
        
        # Convert to data points list
        data_points = []
        for _, row in df.iterrows():
            data_points.append({
                "date": row["Date"].isoformat(),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"])
            })
        
        # Build response
        response = {
            "symbol": symbol.upper(),
            "interval": interval,
            "start_date": actual_start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "data": data_points
        }
        
        if format_type == "dict":
            return response
        elif format_type == "both":
            return {
                "text": self._format_ohlcv_as_markdown(response),
                "data": response
            }
        else:  # markdown
            return self._format_ohlcv_as_markdown(response)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary for OHLCV providers.
        
        This implementation handles OHLCV-specific data formatting for all 
        MarketDataProvider subclasses. If data is a DataFrame, it converts
        it to the standard OHLCV dictionary format.
        
        Args:
            data: OHLCV data (DataFrame, dict, or other format)
            
        Returns:
            Dict[str, Any]: Structured dictionary with OHLCV data
        """
        import pandas as pd
        
        if isinstance(data, dict):
            # Already a dict, return as-is
            return data
        elif isinstance(data, pd.DataFrame) and not data.empty:
            # Convert DataFrame to OHLCV dict format
            data_points = []
            for _, row in data.iterrows():
                data_points.append({
                    "date": self.format_datetime_for_dict(row.get("Date")),
                    "open": round(float(row.get("Open", 0)), 2),
                    "high": round(float(row.get("High", 0)), 2), 
                    "low": round(float(row.get("Low", 0)), 2),
                    "close": round(float(row.get("Close", 0)), 2),
                    "volume": int(row.get("Volume", 0))
                })
            
            return {
                "symbol": "UNKNOWN",  # Will be overridden by get_ohlcv_data_formatted
                "interval": "1d",     # Will be overridden by get_ohlcv_data_formatted
                "data": data_points
            }
        else:
            # Fallback for other data types
            return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for OHLCV providers.
        
        This implementation handles OHLCV-specific markdown formatting for all
        MarketDataProvider subclasses. It uses the specialized _format_ohlcv_as_markdown
        method when dealing with OHLCV data to ensure proper datetime formatting.
        
        Args:
            data: OHLCV data (DataFrame, dict, or other format)
            
        Returns:
            str: Markdown-formatted string with proper OHLCV formatting
        """
        import pandas as pd
        
        if isinstance(data, pd.DataFrame) and not data.empty:
            # Convert DataFrame to dict format and use OHLCV markdown formatter
            dict_data = self._format_as_dict(data)
            return self._format_ohlcv_as_markdown(dict_data)
        elif isinstance(data, dict) and 'data' in data:
            # Use specialized OHLCV markdown formatter
            return self._format_ohlcv_as_markdown(data)
        else:
            # Generic fallback for non-OHLCV data
            return f"# Market Data\n\n```\n{str(data)}\n```"

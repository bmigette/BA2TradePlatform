"""
MarketDataProvider Interface

Abstract base class for market data providers with built-in caching strategy.
All data providers should extend this class and implement the fetch methods.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, time
from typing import List, Optional
import pandas as pd
import os
from ba2_trade_platform.core.types import MarketDataPoint
from ba2_trade_platform.logger import logger
from ba2_trade_platform import config


class MarketDataProviderInterface(ABC):
    """
    Abstract base class for market data providers.
    
    Provides a standardized interface for fetching historical market data
    with built-in caching capabilities.
    
    Subclasses must implement:
        - _fetch_data_from_source(): Fetch data from the actual data source
    """
    
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
    
    @abstractmethod
    def _fetch_data_from_source(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch data from the actual data source (e.g., API, database).
        
        This method must be implemented by subclasses.
        
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
    
    def _load_cache(self, cache_file: str) -> Optional[pd.DataFrame]:
        """
        Load data from cache file.
        
        Args:
            cache_file: Path to cache file
        
        Returns:
            DataFrame if successful, None if failed
        """
        try:
            df = pd.read_csv(cache_file)
            df['Date'] = pd.to_datetime(df['Date'])
            logger.debug(f"Loaded {len(df)} records from cache: {cache_file}")
            return df
        except Exception as e:
            logger.error(f"Failed to load cache file {cache_file}: {e}", exc_info=True)
            return None
    
    def _save_cache(self, data: pd.DataFrame, cache_file: str) -> bool:
        """
        Save data to cache file.
        
        Args:
            data: DataFrame to cache
            cache_file: Path to cache file
        
        Returns:
            True if successful, False otherwise
        """
        try:
            data.to_csv(cache_file, index=False)
            logger.debug(f"Saved {len(data)} records to cache: {cache_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save cache file {cache_file}: {e}", exc_info=True)
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
    
    def get_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d',
        use_cache: bool = True,
        max_cache_age_hours: int = 24
    ) -> List[MarketDataPoint]:
        """
        Get market data with smart caching.
        
        This is the main public method to fetch data. It handles:
        1. Normalize start/end dates to interval boundaries
        2. Cache validation (check if cache exists and is recent)
        3. Loading from cache if valid
        4. Fetching from source if cache invalid
        5. Filtering to requested date range
        6. Converting to MarketDataPoint objects
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data (will be floored to interval boundary)
            end_date: End date for data
            interval: Data interval ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1wk', '1mo')
            use_cache: Whether to use caching (default: True)
            max_cache_age_hours: Maximum age of cache in hours (default: 24)
        
        Returns:
            List of MarketDataPoint objects for the requested date range
        
        Raises:
            Exception: If data fetching fails
        """
        # Normalize start_date to interval boundary for proper time series alignment
        normalized_start = self.normalize_time_to_interval(start_date, interval)
        
        logger.info(f"Getting data for {symbol} from {normalized_start.date()} to {end_date.date()}, interval={interval}")
        if normalized_start != start_date:
            logger.debug(f"Start date normalized from {start_date} to {normalized_start} for interval {interval}")
        
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
            
            df = self._fetch_data_from_source(symbol, fetch_start, fetch_end, interval)
            
            if df is None or df.empty:
                raise Exception(f"Failed to fetch data for {symbol}")
            
            # Save to cache
            if use_cache:
                self._save_cache(df, cache_file)
        
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
    
    def get_dataframe(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d',
        use_cache: bool = True,
        max_cache_age_hours: int = 24
    ) -> pd.DataFrame:
        """
        Get market data as DataFrame (convenience method).
        
        Similar to get_data() but returns a DataFrame instead of MarketDataPoint objects.
        Useful for technical analysis and calculations.
        
        Args:
            symbol: Ticker symbol
            start_date: Start date for data (will be floored to interval boundary)
            end_date: End date for data
            interval: Data interval
            use_cache: Whether to use caching
            max_cache_age_hours: Maximum age of cache in hours
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        """
        # Normalize start_date to interval boundary for proper time series alignment
        normalized_start = self.normalize_time_to_interval(start_date, interval)
        
        logger.info(f"Getting DataFrame for {symbol} from {normalized_start.date()} to {end_date.date()}, interval={interval}")
        if normalized_start != start_date:
            logger.debug(f"Start date normalized from {start_date} to {normalized_start} for interval {interval}")
        
        cache_file = self._get_cache_file_path(symbol, interval)
        df = None
        
        # Try to load from cache
        if use_cache and self._is_cache_valid(cache_file, max_cache_age_hours):
            df = self._load_cache(cache_file)
        
        # Fetch from source if needed
        if df is None:
            logger.info(f"Fetching DataFrame from source for {symbol}")
            
            # Determine fetch range based on interval (Yahoo Finance limits)
            # Intraday data (1m, 5m, 15m, 30m, 1h, 4h): max 730 days
            # Daily data (1d, 1wk, 1mo): can go back 15 years
            if interval in ['1m', '5m', '15m', '30m', '1h', '4h']:
                # Intraday data limited to ~2 years (730 days)
                fetch_start = datetime.now() - timedelta(days=729)  # Use 729 to be safe
            else:
                # Daily/weekly/monthly data can go back 15 years
                fetch_start = datetime.now() - timedelta(days=365 * 15)
            
            fetch_end = datetime.now()
            
            df = self._fetch_data_from_source(symbol, fetch_start, fetch_end, interval)
            
            if df is None or df.empty:
                raise Exception(f"Failed to fetch data for {symbol}")
            
            # Save to cache
            if use_cache:
                self._save_cache(df, cache_file)
        
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
        
        logger.info(f"Returning DataFrame with {len(filtered_df)} records")
        
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

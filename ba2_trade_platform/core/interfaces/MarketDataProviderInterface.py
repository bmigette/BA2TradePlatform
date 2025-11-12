"""
MarketDataProvider Interface

Abstract base class for market data providers with built-in caching strategy.
All data providers should extend this class and implement the fetch methods.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, time, timezone
from typing import List, Optional, Dict, Any
import pandas as pd
import os
from ba2_trade_platform.core.types import MarketDataPoint
from ba2_trade_platform.logger import logger
from ba2_trade_platform import config
from ba2_trade_platform.core.provider_utils import log_provider_call, validate_date_range
from ba2_trade_platform.core.interfaces.DataProviderInterface import DataProviderInterface


class MarketDataProviderInterface(DataProviderInterface):
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
            
            df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)
            
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
        # Validate and normalize dates with intelligent optional handling
        start_date, end_date = validate_date_range(start_date, end_date, lookback_days)
        
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
            
            df = self._get_ohlcv_data_impl(symbol, fetch_start, fetch_end, interval)
            
            if df is None or df.empty:
                raise Exception(f"Failed to fetch data for {symbol}")
            
            # Save to cache
            if use_cache:
                self._save_cache(df, cache_file)
        
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
        from ba2_trade_platform.core.provider_utils import calculate_date_range, validate_date_range
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

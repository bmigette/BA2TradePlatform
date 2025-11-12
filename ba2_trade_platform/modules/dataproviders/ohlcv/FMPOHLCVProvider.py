"""
FMP OHLCV Provider

Historical stock price data provider using Financial Modeling Prep API.
Provides OHLCV (Open, High, Low, Close, Volume) data for stocks.

API Documentation: 
- Intraday (1-30min): https://site.financialmodelingprep.com/developer/docs#intraday-30-min
- Historical Daily: https://site.financialmodelingprep.com/developer/docs#historical-price-eod-full
"""

from typing import Annotated, Optional, Dict, Any
from datetime import datetime, timedelta
import pandas as pd
import requests

from ba2_trade_platform.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting


class FMPOHLCVProvider(MarketDataProviderInterface):
    """
    Financial Modeling Prep OHLCV data provider.
    
    Uses FMP API to retrieve historical stock price data.
    Supports multiple timeframes (1min, 5min, 15min, 30min, 1hour, 4hour, 1day).
    
    API Endpoints:
        - Intraday: /api/v3/historical-chart/{interval}/{symbol}
        - Daily: /api/v3/historical-price-full/{symbol}
    
    Requires:
        - FMP API key in app settings (FMP_API_KEY)
    """
    
    # Timeframe mapping: user format -> FMP API interval
    TIMEFRAME_MAP = {
        "1m": "1min",
        "1min": "1min",
        "5m": "5min",
        "5min": "5min",
        "15m": "15min",
        "15min": "15min",
        "30m": "30min",
        "30min": "30min",
        "1h": "1hour",
        "1hour": "1hour",
        "4h": "4hour",
        "4hour": "4hour",
        "1d": "daily",
        "1day": "daily",
    }
    
    # Base URL for FMP API
    BASE_URL = "https://financialmodelingprep.com/api/v3"
    
    def __init__(self):
        """Initialize FMP OHLCV provider with caching support."""
        # Call parent __init__ to set up caching
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("FMP_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "FMP API key not configured. "
                "Please set FMP_API_KEY in app settings."
            )
        
        logger.debug("Initialized FMPOHLCVProvider with caching")
    
    def _get_ohlcv_data_impl(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        start_date: Annotated[datetime, "Start date for data"],
        end_date: Annotated[datetime, "End date for data"],
        interval: Annotated[str, "Data interval (1m, 5m, 15m, 30m, 1h, 4h, 1d)"] = "1d"
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from FMP API (internal implementation).
        
        This method is called by the parent class's get_ohlcv_data() method
        when cache is invalid or disabled.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval (1m, 5m, 15m, 30m, 1h, 4h, 1d)
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        
        Raises:
            ValueError: If interval not supported
            requests.HTTPError: If API request fails
        """
        # Map interval to FMP format
        if interval not in self.TIMEFRAME_MAP:
            raise ValueError(
                f"Interval '{interval}' not supported by FMP. "
                f"Supported intervals: {list(self.TIMEFRAME_MAP.keys())}"
            )
        
        fmp_interval = self.TIMEFRAME_MAP[interval]
        
        logger.debug(
            f"Fetching FMP OHLCV data for {symbol} from {start_date.date()} "
            f"to {end_date.date()} with interval {interval} (FMP: {fmp_interval})"
        )
        
        try:
            # Choose endpoint based on interval
            if fmp_interval == "daily":
                # Use historical-price-full for daily data
                df = self._fetch_daily_data(symbol, start_date, end_date)
            else:
                # Use historical-chart for intraday data
                df = self._fetch_intraday_data(symbol, start_date, end_date, fmp_interval)
            
            if df.empty:
                logger.warning(f"No data returned from FMP for {symbol}")
                return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
            
            logger.info(f"Retrieved {len(df)} bars from FMP for {symbol}")
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to get FMP OHLCV data for {symbol}: {e}", exc_info=True)
            raise
    
    def _fetch_daily_data(
        self, 
        symbol: str, 
        start_date: datetime, 
        end_date: datetime
    ) -> pd.DataFrame:
        """
        Fetch daily OHLCV data from FMP API.
        
        Uses /api/v3/historical-price-full/{symbol} endpoint.
        
        Args:
            symbol: Stock ticker symbol
            start_date: Start date for data
            end_date: End date for data
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        """
        url = f"{self.BASE_URL}/historical-price-full/{symbol}"
        
        params = {
            "apikey": self.api_key,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d")
        }
        
        logger.debug(f"FMP API request: {url} with params: {params}")
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        # Extract historical data from response
        if "historical" not in data:
            logger.warning(f"No 'historical' key in FMP response for {symbol}")
            return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        
        historical = data["historical"]
        
        if not historical:
            logger.warning(f"Empty historical data from FMP for {symbol}")
            return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        
        # Convert to DataFrame
        df = pd.DataFrame(historical)
        
        # Rename columns to match expected format
        df = df.rename(columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        })
        
        # Select only needed columns
        df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
        
        # Convert Date to datetime with UTC timezone
        df['Date'] = pd.to_datetime(df['Date'], utc=True)
        
        # Sort by date (FMP returns newest first)
        df = df.sort_values('Date')
        
        # Reset index
        df = df.reset_index(drop=True)
        
        return df
    
    def _fetch_intraday_data(
        self, 
        symbol: str, 
        start_date: datetime, 
        end_date: datetime,
        fmp_interval: str
    ) -> pd.DataFrame:
        """
        Fetch intraday OHLCV data from FMP API.
        
        Uses /api/v3/historical-chart/{interval}/{symbol} endpoint.
        
        Note: FMP intraday API has limitations:
        - Free tier: Last 5 days only
        - Premium: More historical data available
        
        Args:
            symbol: Stock ticker symbol
            start_date: Start date for data
            end_date: End date for data
            fmp_interval: FMP interval format (1min, 5min, 15min, 30min, 1hour, 4hour)
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        """
        url = f"{self.BASE_URL}/historical-chart/{fmp_interval}/{symbol}"
        
        params = {
            "apikey": self.api_key,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d")
        }
        
        logger.debug(f"FMP API request: {url} with params: {params}")
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if not data or not isinstance(data, list):
            logger.warning(f"No data or invalid format from FMP intraday API for {symbol}")
            return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        
        # Convert to DataFrame
        df = pd.DataFrame(data)
        
        # Rename columns to match expected format
        df = df.rename(columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        })
        
        # Select only needed columns
        df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
        
        # Convert Date to datetime with UTC timezone
        df['Date'] = pd.to_datetime(df['Date'], utc=True)
        
        # Ensure start_date and end_date are timezone-aware for comparison
        # Convert to pandas Timestamp with UTC timezone if needed
        start_date_tz = pd.Timestamp(start_date).tz_localize('UTC') if pd.Timestamp(start_date).tz is None else pd.Timestamp(start_date).tz_convert('UTC')
        end_date_tz = pd.Timestamp(end_date).tz_localize('UTC') if pd.Timestamp(end_date).tz is None else pd.Timestamp(end_date).tz_convert('UTC')
        
        # Filter by date range (FMP may return more data than requested)
        df = df[(df['Date'] >= start_date_tz) & (df['Date'] <= end_date_tz)]
        
        # Sort by date (FMP returns newest first)
        df = df.sort_values('Date')
        
        # Reset index
        df = df.reset_index(drop=True)
        
        return df

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "fmp"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "intraday", "daily"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        # FMP API key validated by FMP common module
        return True


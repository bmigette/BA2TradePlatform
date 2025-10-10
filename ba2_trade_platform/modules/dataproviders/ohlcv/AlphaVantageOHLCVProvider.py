"""
Alpha Vantage OHLCV Data Provider

Provides historical OHLCV (Open, High, Low, Close, Volume) data using Alpha Vantage
TIME_SERIES_DAILY_ADJUSTED API. Includes adjusted close and split/dividend events.
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import pandas as pd
from io import StringIO

from ba2_trade_platform.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import (
    AlphaVantageBaseProvider,
    filter_csv_by_date_range,
    AlphaVantageRateLimitError
)
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger


class AlphaVantageOHLCVProvider(AlphaVantageBaseProvider, MarketDataProviderInterface):
    """
    Alpha Vantage OHLCV Data Provider.
    
    Provides daily adjusted time series data including:
    - Open, High, Low, Close prices
    - Adjusted close (accounting for splits and dividends)
    - Volume
    - Dividend amount
    - Split coefficient
    
    Features:
    - Fetches historical OHLCV data from Alpha Vantage
    - Symbol-based caching (one CSV per symbol)
    - Automatic cache refresh when data is older than 24 hours
    - Supports compact (100 days) and full (20+ years) output
    
    Cache Strategy:
    - File format: {SYMBOL}_1d.csv (e.g., AAPL_1d.csv)
    - Location: config.CACHE_FOLDER
    - Max age: 24 hours (configurable)
    - If cache exists and is fresh: use cached data
    - If cache is stale or missing: fetch from API and update cache
    
    Usage:
        from ba2_trade_platform.modules.dataproviders.ohlcv import AlphaVantageOHLCVProvider
        
        provider = AlphaVantageOHLCVProvider()
        
        # Get data as MarketDataPoint objects
        datapoints = provider.get_data(
            symbol='AAPL',
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
            interval='1d'
        )
        
        # Or get as DataFrame for analysis
        df = provider.get_dataframe(
            symbol='AAPL',
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
            interval='1d'
        )
    """
    
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize Alpha Vantage OHLCV provider.
        
        Args:
            source: Source identifier for API tracking (e.g., 'ba2_trade_platform', 'trading_agents')
        
        Caching is automatically configured using config.CACHE_FOLDER.
        """
        AlphaVantageBaseProvider.__init__(self, source)
        MarketDataProviderInterface.__init__(self)
        logger.debug(f"AlphaVantageOHLCVProvider initialized with source: {source}")
    
    @log_provider_call
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from Alpha Vantage API (internal implementation).
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval (only '1d' supported for Alpha Vantage)
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume, 
                                   Adjusted_Close, Dividend_Amount, Split_Coefficient
        
        Raises:
            ValueError: If interval is not '1d'
            AlphaVantageRateLimitError: If API rate limit is exceeded
        """
        # Alpha Vantage only supports daily data
        if interval != '1d':
            raise ValueError(f"Alpha Vantage only supports daily interval ('1d'), got '{interval}'")
        
        # Determine output size based on date range
        days_from_today_to_start = (datetime.now() - start_date).days
        outputsize = "compact" if days_from_today_to_start < 100 else "full"
        
        logger.debug(
            f"Fetching Alpha Vantage data for {symbol}: "
            f"start={start_date.date()}, end={end_date.date()}, outputsize={outputsize}"
        )
        
        params = {
            "symbol": symbol,
            "outputsize": outputsize,
            "datatype": "csv",
        }
        
        try:
            # Make API request
            csv_data = self.make_api_request("TIME_SERIES_DAILY_ADJUSTED", params)
            
            # Filter by date range
            filtered_csv = filter_csv_by_date_range(
                csv_data,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d")
            )
            
            # Parse CSV to DataFrame
            df = pd.read_csv(StringIO(filtered_csv))
            
            # Rename columns to match our standard format
            # Base class expects: Date, Open, High, Low, Close, Volume
            column_mapping = {
                'timestamp': 'Date',  # Standardize to 'Date' for base class compatibility
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume',
                'adjusted_close': 'Adjusted_Close',
                'dividend_amount': 'Dividend_Amount',
                'split_coefficient': 'Split_Coefficient'
            }
            
            # Only rename columns that exist
            existing_mapping = {k: v for k, v in column_mapping.items() if k in df.columns}
            df = df.rename(columns=existing_mapping)
            
            # Convert Date to datetime
            df['Date'] = pd.to_datetime(df['Date'])
            
            # Sort by Date (oldest first)
            df = df.sort_values('Date')
            
            logger.info(
                f"Fetched {len(df)} data points for {symbol} from Alpha Vantage "
                f"({start_date.date()} to {end_date.date()})"
            )
            
            return df
            
        except AlphaVantageRateLimitError:
            logger.error(f"Rate limit exceeded when fetching data for {symbol}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Alpha Vantage data for {symbol}: {e}")
            raise
    
    @log_provider_call
    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Get the most recent close price for a symbol.
        
        Args:
            symbol: Ticker symbol
        
        Returns:
            Latest close price or None if unavailable
        """
        try:
            # Get last 5 days of data to ensure we have at least one valid day
            end_date = datetime.now()
            start_date = end_date - timedelta(days=5)
            
            df = self.get_ohlcv_data(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval='1d'
            )
            
            if df.empty:
                logger.warning(f"No data available for {symbol}")
                return None
            
            latest_price = float(df.iloc[-1]['Close'])
            logger.debug(f"Latest price for {symbol}: ${latest_price:.2f}")
            return latest_price
            
        except Exception as e:
            logger.error(f"Error getting latest price for {symbol}: {e}")
            return None

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alphavantage"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "intraday", "daily"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        # AlphaVantage API key validated by alpha_vantage_common module
        return True
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: Provider data
            
        Returns:
            Dict[str, Any]: Structured dictionary
        """
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for LLM consumption.
        
        Args:
            data: Provider data
            
        Returns:
            str: Markdown-formatted string
        """
        if isinstance(data, dict):
            md = "# Data\n\n"
            for key, value in data.items():
                if isinstance(value, (list, dict)):
                    md += f"**{key}**: (complex data)\n"
                else:
                    md += f"**{key}**: {value}\n"
            return md
        return str(data)


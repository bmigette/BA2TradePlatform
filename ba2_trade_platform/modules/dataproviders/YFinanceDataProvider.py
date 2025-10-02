"""
YFinance Data Provider

Implementation of MarketDataProvider using Yahoo Finance as the data source.
Provides historical market data with smart caching strategy.
"""

from datetime import datetime
from typing import Optional
import pandas as pd
import yfinance as yf
from ba2_trade_platform.core.MarketDataProvider import MarketDataProvider
from ba2_trade_platform.logger import logger


class YFinanceDataProvider(MarketDataProvider):
    """
    Yahoo Finance data provider implementation.
    
    Features:
    - Fetches historical OHLCV data from Yahoo Finance
    - Symbol-based caching (one CSV per symbol+interval)
    - Automatic cache refresh when data is older than 24 hours
    - Caches 15 years of historical data by default
    
    Cache Strategy:
    - File format: {SYMBOL}_{INTERVAL}.csv (e.g., AAPL_1d.csv)
    - Location: config.CACHE_FOLDER
    - Max age: 24 hours (configurable)
    - If cache exists and is fresh: use cached data
    - If cache is stale or missing: fetch from API and update cache
    
    Usage:
        from ba2_trade_platform.config import CACHE_FOLDER
        from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
        
        provider = YFinanceDataProvider(CACHE_FOLDER)
        
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
    
    def __init__(self, cache_folder: str):
        """
        Initialize YFinance data provider.
        
        Args:
            cache_folder: Directory path where cache files will be stored
        """
        super().__init__(cache_folder)
        logger.info(f"YFinanceDataProvider initialized with cache folder: {cache_folder}")
    
    def _fetch_data_from_source(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch data from Yahoo Finance API.
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval ('1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo')
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        
        Raises:
            Exception: If data fetching fails
        """
        try:
            logger.info(f"Fetching {symbol} data from Yahoo Finance: "
                       f"{start_date.date()} to {end_date.date()}, interval={interval}")
            
            # Download data from Yahoo Finance
            data = yf.download(
                symbol,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval=interval,
                multi_level_index=False,
                progress=False,
                auto_adjust=True  # Adjust for splits and dividends
            )
            
            if data.empty:
                raise Exception(f"No data returned for {symbol}")
            
            # Reset index to make Date a column
            data = data.reset_index()
            
            # Ensure we have all required columns
            required_columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
            missing_columns = [col for col in required_columns if col not in data.columns]
            
            if missing_columns:
                raise Exception(f"Missing required columns: {missing_columns}")
            
            # Select only the required columns (drop any extras)
            data = data[required_columns]
            
            logger.info(f"Successfully fetched {len(data)} records for {symbol}")
            
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch data from Yahoo Finance for {symbol}: {e}", exc_info=True)
            raise Exception(f"Yahoo Finance data fetch failed for {symbol}: {str(e)}")
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get the current/latest price for a symbol.
        
        This is a convenience method that fetches the most recent closing price.
        It will use cached data if available and fresh, otherwise fetch from API.
        
        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
        
        Returns:
            Current closing price, or None if failed
        """
        try:
            # Get last 2 days of data to ensure we have latest
            end_date = datetime.now()
            start_date = end_date - pd.Timedelta(days=2)
            
            datapoints = self.get_data(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval='1d'
            )
            
            if not datapoints:
                logger.warning(f"No data available for {symbol}")
                return None
            
            # Get the most recent data point
            latest = datapoints[-1]
            logger.debug(f"Current price for {symbol}: {latest.close}")
            
            return latest.close
            
        except Exception as e:
            logger.error(f"Failed to get current price for {symbol}: {e}", exc_info=True)
            return None
    
    def validate_symbol(self, symbol: str) -> bool:
        """
        Check if a symbol is valid and has data available.
        
        Args:
            symbol: Ticker symbol to validate
        
        Returns:
            True if symbol is valid and has data, False otherwise
        """
        try:
            # Try to fetch 1 day of data
            end_date = datetime.now()
            start_date = end_date - pd.Timedelta(days=5)  # Look back 5 days to account for weekends
            
            data = self._fetch_data_from_source(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval='1d'
            )
            
            is_valid = data is not None and not data.empty
            
            if is_valid:
                logger.info(f"Symbol {symbol} is valid")
            else:
                logger.warning(f"Symbol {symbol} is invalid or has no data")
            
            return is_valid
            
        except Exception as e:
            logger.error(f"Symbol validation failed for {symbol}: {e}", exc_info=True)
            return False

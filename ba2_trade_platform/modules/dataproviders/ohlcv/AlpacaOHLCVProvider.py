"""
Alpaca OHLCV Provider

Historical stock price data provider using Alpaca Markets API via alpaca-py library.
Provides OHLCV (Open, High, Low, Close, Volume) data for stocks.
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime, timedelta, timezone
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from ba2_trade_platform.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting


class AlpacaOHLCVProvider(MarketDataProviderInterface):
    """
    Alpaca Markets OHLCV data provider.
    
    Uses alpaca-py library to retrieve historical stock price data from Alpaca Markets.
    Supports multiple timeframes (1min, 5min, 15min, 1hour, 1day).
    
    Requires:
        - Alpaca API key and secret in app settings
    """
    
    # Timeframe mapping: user format -> Alpaca TimeFrame
    TIMEFRAME_MAP = {
        "1m": TimeFrame(1, TimeFrameUnit.Minute),
        "1min": TimeFrame(1, TimeFrameUnit.Minute),
        "5m": TimeFrame(5, TimeFrameUnit.Minute),
        "5min": TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "15min": TimeFrame(15, TimeFrameUnit.Minute),
        "1h": TimeFrame(1, TimeFrameUnit.Hour),
        "1hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1d": TimeFrame(1, TimeFrameUnit.Day),
        "1day": TimeFrame(1, TimeFrameUnit.Day),
    }
    
    def __init__(self):
        """Initialize Alpaca OHLCV provider with caching support."""
        # Call parent __init__ to set up caching
        super().__init__()
        
        # Get API credentials from settings
        self.api_key = get_app_setting("alpaca_api_key")
        self.api_secret = get_app_setting("alpaca_api_secret")
        
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "Alpaca API credentials not configured. "
                "Please set alpaca_api_key and alpaca_api_secret in app settings."
            )
        
        # Initialize Alpaca client
        self.client = StockHistoricalDataClient(self.api_key, self.api_secret)
        
        logger.debug("Initialized AlpacaOHLCVProvider with caching")
    
    def _get_ohlcv_data_impl(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        start_date: Annotated[datetime, "Start date for data"],
        end_date: Annotated[datetime, "End date for data"],
        interval: Annotated[str, "Data interval (1m, 5m, 15m, 1h, 1d)"] = "1d"
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from Alpaca API (internal implementation).
        
        This method is called by the parent class's get_ohlcv_data() method
        when cache is invalid or disabled.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval (1m, 5m, 15m, 1h, 1d)
        
        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume
        
        Raises:
            ValueError: If interval not supported
        """
        # Map interval to Alpaca TimeFrame
        if interval not in self.TIMEFRAME_MAP:
            raise ValueError(
                f"Interval '{interval}' not supported by Alpaca. "
                f"Supported intervals: {list(self.TIMEFRAME_MAP.keys())}"
            )
        
        timeframe = self.TIMEFRAME_MAP[interval]
        
        logger.debug(
            f"Fetching Alpaca OHLCV data for {symbol} from {start_date.date()} "
            f"to {end_date.date()} with interval {interval}"
        )
        
        try:
            # Create request
            request_params = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date
            )
            
            # Fetch data
            bars = self.client.get_stock_bars(request_params)
            
            # Convert to DataFrame
            df = bars.df
            
            if df.empty:
                logger.warning(f"No data returned from Alpaca for {symbol}")
                return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
            
            # Reset index to get symbol and timestamp as columns
            df = df.reset_index()
            
            # Rename columns to match expected format
            df = df.rename(columns={
                'timestamp': 'Date',
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume'
            })
            
            # Select only needed columns
            df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
            
            # Ensure Date is datetime
            df['Date'] = pd.to_datetime(df['Date'])
            
            logger.info(f"Retrieved {len(df)} bars from Alpaca for {symbol}")
            
            return df
            
        except Exception as e:
            logger.error(f"Failed to get Alpaca OHLCV data for {symbol}: {e}", exc_info=True)
            raise

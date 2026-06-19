"""
EODHD (End of Day Historical Data) OHLCV Data Provider

Provides historical OHLCV data using the EODHD API.
Supports daily data for stocks, ETFs, and indices.
"""

from datetime import datetime, timedelta
from typing import Optional
import os

import pandas as pd
import requests

from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.provider_utils import log_provider_call
from ba2_common.logger import logger


class EODHDOHLCVProvider(MarketDataProviderInterface):
    """
    EODHD OHLCV Data Provider.

    Provides OHLCV data from EODHD API including:
    - Open, High, Low, Close prices
    - Adjusted close
    - Volume

    Features:
    - Supports daily data
    - Up to 30 years of historical data
    - Adjusted prices for splits and dividends
    - Global market coverage

    Usage:
        from ba2_providers.ohlcv import EODHDOHLCVProvider

        provider = EODHDOHLCVProvider()

        # Get data as MarketDataPoint objects
        datapoints = provider.get_data(
            symbol='AAPL.US',  # Format: TICKER.EXCHANGE
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
            interval='1d'
        )
    """

    BASE_URL = "https://eodhistoricaldata.com/api/eod"

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize EODHD OHLCV provider.

        Args:
            api_key: EODHD API key. If not provided, reads from
                    EODHD_API_KEY environment variable.
        """
        super().__init__()
        self.api_key = api_key or os.environ.get('EODHD_API_KEY')

        if not self.api_key:
            logger.warning("EODHD API key not configured")

        logger.debug("EODHDOHLCVProvider initialized")

    def _normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol to EODHD format.

        EODHD uses format TICKER.EXCHANGE (e.g., AAPL.US)
        If no exchange is specified, defaults to .US

        Args:
            symbol: Ticker symbol (e.g., 'AAPL' or 'AAPL.US')

        Returns:
            Normalized symbol with exchange
        """
        if '.' not in symbol:
            return f"{symbol}.US"
        return symbol

    @log_provider_call
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from EODHD API.

        Args:
            symbol: Ticker symbol (e.g., 'AAPL' or 'AAPL.US')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval (only '1d' supported)

        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume

        Raises:
            ValueError: If interval is not supported
            RuntimeError: If API key is not configured or API request fails
        """
        if not self.api_key:
            raise RuntimeError("EODHD API key not configured")

        # EODHD only supports daily data through the basic API
        if interval != '1d':
            raise ValueError(
                f"EODHD only supports daily interval ('1d'), got '{interval}'"
            )

        # Normalize symbol
        normalized_symbol = self._normalize_symbol(symbol)

        # Format dates
        from_date = start_date.strftime('%Y-%m-%d')
        to_date = end_date.strftime('%Y-%m-%d')

        # Build URL
        url = f"{self.BASE_URL}/{normalized_symbol}"

        params = {
            'api_token': self.api_key,
            'from': from_date,
            'to': to_date,
            'period': 'd',  # Daily
            'fmt': 'json'
        }

        logger.debug(f"Fetching EODHD data for {normalized_symbol}: {from_date} to {to_date}")

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if not data or isinstance(data, dict) and data.get('error'):
                error_msg = data.get('error', 'No data returned') if isinstance(data, dict) else 'No data returned'
                raise RuntimeError(f"EODHD API error: {error_msg}")

            if not data:
                logger.warning(f"No data returned for {normalized_symbol}")
                return pd.DataFrame()

            # Convert to DataFrame
            df = pd.DataFrame(data)

            # Rename columns
            column_mapping = {
                'date': 'Date',
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'adjusted_close': 'Adjusted_Close',
                'volume': 'Volume'
            }

            df = df.rename(columns=column_mapping)

            # Convert date to datetime
            df['Date'] = pd.to_datetime(df['Date'])

            # Ensure required columns exist
            required_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
            for col in required_cols:
                if col not in df.columns:
                    df[col] = 0.0

            # Sort by date
            df = df.sort_values('Date').reset_index(drop=True)

            logger.info(
                f"Fetched {len(df)} data points for {normalized_symbol} from EODHD "
                f"({from_date} to {to_date})"
            )

            return df

        except requests.exceptions.RequestException as e:
            logger.error(f"EODHD API request failed: {e}")
            raise RuntimeError(f"EODHD API request failed: {e}")

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
            end_date = datetime.now()
            start_date = end_date - timedelta(days=5)

            df = self.get_ohlcv_data(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval='1d'
            )

            if df.empty:
                return None

            return float(df.iloc[-1]['Close'])

        except Exception as e:
            logger.error(f"Error getting latest price for {symbol}: {e}")
            return None

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "eodhd"

    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "daily", "adjusted"]

    def validate_config(self) -> bool:
        """
        Validate provider configuration.

        Returns:
            bool: True if API key is configured
        """
        return bool(self.api_key)

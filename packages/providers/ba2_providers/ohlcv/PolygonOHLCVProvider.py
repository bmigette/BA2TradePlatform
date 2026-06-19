"""
Polygon.io OHLCV Data Provider

Provides historical OHLCV data using the Polygon.io Aggregates API.
Supports intraday and daily data with configurable time ranges.
"""

from datetime import datetime, timedelta
from typing import Optional
import os

import pandas as pd
import requests

from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.provider_utils import log_provider_call
from ba2_common.logger import logger


class PolygonOHLCVProvider(MarketDataProviderInterface):
    """
    Polygon.io OHLCV Data Provider.

    Provides OHLCV data from Polygon.io API including:
    - Open, High, Low, Close prices
    - Volume
    - VWAP (Volume Weighted Average Price)
    - Number of trades

    Features:
    - Supports multiple timeframes (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w)
    - Up to 2 years of historical data
    - Adjusted and unadjusted prices

    Usage:
        from ba2_providers.ohlcv import PolygonOHLCVProvider

        provider = PolygonOHLCVProvider()

        # Get data as MarketDataPoint objects
        datapoints = provider.get_data(
            symbol='AAPL',
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 12, 31),
            interval='1d'
        )
    """

    BASE_URL = "https://api.polygon.io/v2/aggs/ticker"

    # Interval mapping to Polygon.io timespan
    INTERVAL_MAP = {
        '1m': ('minute', 1),
        '5m': ('minute', 5),
        '15m': ('minute', 15),
        '30m': ('minute', 30),
        '1h': ('hour', 1),
        '4h': ('hour', 4),
        '1d': ('day', 1),
        '1w': ('week', 1),
    }

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Polygon.io OHLCV provider.

        Args:
            api_key: Polygon.io API key. If not provided, reads from
                    POLYGON_API_KEY environment variable.
        """
        super().__init__()
        self.api_key = api_key or os.environ.get('POLYGON_API_KEY')

        if not self.api_key:
            logger.warning("Polygon.io API key not configured")

        logger.debug("PolygonOHLCVProvider initialized")

    @log_provider_call
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data from Polygon.io API.

        Args:
            symbol: Ticker symbol (e.g., 'AAPL', 'MSFT')
            start_date: Start date for data
            end_date: End date for data
            interval: Data interval ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w')

        Returns:
            DataFrame with columns: Date, Open, High, Low, Close, Volume

        Raises:
            ValueError: If interval is not supported
            RuntimeError: If API key is not configured or API request fails
        """
        if not self.api_key:
            raise RuntimeError("Polygon.io API key not configured")

        if interval not in self.INTERVAL_MAP:
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported: {list(self.INTERVAL_MAP.keys())}"
            )

        timespan, multiplier = self.INTERVAL_MAP[interval]

        # Format dates
        from_date = start_date.strftime('%Y-%m-%d')
        to_date = end_date.strftime('%Y-%m-%d')

        # Build URL
        url = f"{self.BASE_URL}/{symbol}/range/{multiplier}/{timespan}/{from_date}/{to_date}"

        params = {
            'apiKey': self.api_key,
            'adjusted': 'true',
            'sort': 'asc',
            'limit': 50000
        }

        logger.debug(f"Fetching Polygon.io data for {symbol}: {from_date} to {to_date}")

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get('status') == 'ERROR':
                error_msg = data.get('error', 'Unknown error')
                raise RuntimeError(f"Polygon.io API error: {error_msg}")

            results = data.get('results', [])

            if not results:
                logger.warning(f"No data returned for {symbol}")
                return pd.DataFrame()

            # Convert to DataFrame
            df = pd.DataFrame(results)

            # Rename columns
            column_mapping = {
                't': 'Date',
                'o': 'Open',
                'h': 'High',
                'l': 'Low',
                'c': 'Close',
                'v': 'Volume',
                'vw': 'VWAP',
                'n': 'Trades'
            }

            df = df.rename(columns=column_mapping)

            # Convert timestamp (milliseconds) to datetime
            df['Date'] = pd.to_datetime(df['Date'], unit='ms')

            # Ensure required columns exist
            required_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
            for col in required_cols:
                if col not in df.columns:
                    df[col] = 0.0

            # Sort by date
            df = df.sort_values('Date').reset_index(drop=True)

            logger.info(
                f"Fetched {len(df)} data points for {symbol} from Polygon.io "
                f"({from_date} to {to_date})"
            )

            return df

        except requests.exceptions.RequestException as e:
            logger.error(f"Polygon.io API request failed: {e}")
            raise RuntimeError(f"Polygon.io API request failed: {e}")

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
        return "polygon"

    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "intraday", "daily", "weekly", "vwap"]

    def validate_config(self) -> bool:
        """
        Validate provider configuration.

        Returns:
            bool: True if API key is configured
        """
        return bool(self.api_key)

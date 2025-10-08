"""
YFinance Indicators Provider

Technical indicators provider using Yahoo Finance data via stockstats library.
Calculates indicators from historical price data.
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime, timedelta
import pandas as pd
from stockstats import wrap

from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
from ba2_trade_platform.core.provider_utils import validate_date_range
from ba2_trade_platform.logger import logger
from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
from ba2_trade_platform.config import CACHE_FOLDER


class YFinanceIndicatorsProvider(MarketIndicatorsInterface):
    """
    Yahoo Finance technical indicators provider.
    
    Uses stockstats library to calculate indicators from Yahoo Finance historical data.
    Supports moving averages, MACD, RSI, Bollinger Bands, ATR, and more.
    """
    
    # Indicators supported by this provider (references centralized ALL_INDICATORS)
    SUPPORTED_INDICATOR_KEYS = [
        # Moving Averages
        "close_50_sma",
        "close_200_sma",
        "close_10_ema",
        # MACD
        "macd",
        "macds",
        "macdh",
        # Momentum
        "rsi",
        # Volatility
        "boll",
        "boll_ub",
        "boll_lb",
        "atr",
        "vwma",
        "mfi"
    ]
    
    def __init__(self):
        """Initialize YFinance indicators provider."""
        self._data_provider = YFinanceDataProvider(CACHE_FOLDER)
        logger.debug("Initialized YFinanceIndicatorsProvider")
    
    def _calculate_indicator_for_range(
        self,
        symbol: str,
        indicator: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1d"
    ) -> pd.DataFrame:
        """
        Calculate technical indicator for a date range using stockstats.
        
        Args:
            symbol: Stock ticker symbol
            indicator: Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')
            start_date: Start date for data range
            end_date: End date for data range
            interval: Data interval (1d, 1h, etc.)
        
        Returns:
            DataFrame with Date and indicator value columns
        """
        # Add buffer for technical indicator calculation
        # Most technical indicators need 200-day history for calculations
        buffer_days = 365  # 1 year buffer
        fetch_start = start_date - timedelta(days=buffer_days)
        
        # Yahoo Finance limit: 730 days total for all intervals
        max_start = end_date - timedelta(days=730)
        if fetch_start < max_start:
            fetch_start = max_start
            logger.debug(f"Adjusted fetch_start to {fetch_start} to stay within 730-day limit")
        
        # Get price data via data provider (uses smart cache)
        data = self._data_provider.get_dataframe(
            symbol=symbol,
            start_date=fetch_start,
            end_date=datetime.now(),
            interval=interval
        )
        
        # Wrap data with stockstats for indicator calculation
        df = wrap(data)
        df["Date"] = pd.to_datetime(df["Date"])
        
        # Calculate indicator for all dates (triggers stockstats calculation)
        df[indicator]
        
        # Filter to requested date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # Make start_dt and end_dt timezone-aware if df['Date'] is timezone-aware
        if hasattr(df["Date"], 'dt') and df["Date"].dt.tz is not None:
            from datetime import timezone as tz
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=tz.utc)
        
        mask = (df["Date"] >= start_dt) & (df["Date"] <= end_dt)
        filtered_df = df.loc[mask, ["Date", indicator]].copy()
        
        # Format date to preserve time component for intraday data
        filtered_df["Date"] = filtered_df["Date"].dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # Rename indicator column to 'value' for consistency
        filtered_df = filtered_df.rename(columns={indicator: "value"})
        
        return filtered_df
    
    def get_indicator(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        indicator: Annotated[str, "Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')"],
        end_date: Annotated[datetime, "End date for data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for data (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        interval: Annotated[str, "Data interval (1d, 1h, etc.)"] = "1d",
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get technical indicator data for a symbol.
        
        Uses stockstats library to calculate indicators from Yahoo Finance data.
        Automatically adds buffer days for indicator calculation (moving averages need history).
        """
        # Validate inputs
        if indicator not in self.SUPPORTED_INDICATOR_KEYS:
            raise ValueError(
                f"Indicator '{indicator}' not supported by this provider. "
                f"Supported indicators: {self.SUPPORTED_INDICATOR_KEYS}"
            )
        
        # Validate date range
        actual_start_date = validate_date_range(end_date, start_date, lookback_days)
        
        # Format dates for stockstats
        curr_date_str = end_date.strftime("%Y-%m-%d")
        
        # Calculate lookback days from date range
        lookback = (end_date - actual_start_date).days
        
        logger.debug(
            f"Fetching {indicator} for {symbol} from {actual_start_date.strftime('%Y-%m-%d')} "
            f"to {curr_date_str} (lookback: {lookback} days, interval: {interval})"
        )
        
        try:
            # Calculate indicator using stockstats
            indicator_df = self._calculate_indicator_for_range(
                symbol=symbol,
                indicator=indicator,
                start_date=actual_start_date,
                end_date=end_date,
                interval=interval
            )
            
            # Convert DataFrame to data points list
            data_points = []
            for _, row in indicator_df.iterrows():
                try:
                    # Parse date and value
                    date_str = row["Date"]
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    value = float(row["value"])
                    
                    data_points.append({
                        "date": date_obj.isoformat(),
                        "value": round(value, 4)
                    })
                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping row due to parsing error: {e}")
                    continue
            
            # Get indicator metadata from centralized ALL_INDICATORS
            ind_meta = MarketIndicatorsInterface.ALL_INDICATORS[indicator]
            
            # Build response
            response = {
                "symbol": symbol.upper(),
                "indicator": indicator,
                "indicator_name": ind_meta["name"],
                "interval": interval,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "data": data_points,
                "metadata": {
                    "description": ind_meta["description"],
                    "usage": ind_meta["usage"],
                    "tips": ind_meta["tips"],
                    "interpretation": f"{ind_meta['description']} {ind_meta['usage']}"
                }
            }
            
            if format_type == "dict":
                return response
            else:
                return self._format_markdown(response)
                
        except Exception as e:
            logger.error(f"Failed to get {indicator} for {symbol}: {e}")
            raise
    
    def _format_markdown(self, data: Dict[str, Any]) -> str:
        """Format indicator data as markdown."""
        md = f"# {data['indicator_name']} ({data['indicator']})\n\n"
        md += f"**Symbol:** {data['symbol']}  \n"
        md += f"**Interval:** {data['interval']}  \n"
        md += f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}  \n\n"
        
        md += "## Description\n\n"
        md += f"{data['metadata']['description']}\n\n"
        
        md += "## Usage\n\n"
        md += f"{data['metadata']['usage']}\n\n"
        
        md += "## Tips\n\n"
        md += f"{data['metadata']['tips']}\n\n"
        
        if data['data']:
            md += "## Indicator Values\n\n"
            md += "| Date | Value |\n"
            md += "|------|-------|\n"
            
            # CRITICAL: Always return FULL data, never truncate
            for point in data['data']:
                md += f"| {point['date'][:10]} | {point['value']} |\n"
            
            md += f"\n*Total data points: {len(data['data'])}*\n"
        else:
            md += "## Indicator Values\n\n"
            md += "*No data available for the specified period*\n"
        
        return md
    
    def get_supported_indicators(self) -> list[str]:
        """Return list of supported indicator names."""
        return self.SUPPORTED_INDICATOR_KEYS

"""
Pandas Indicator Calculator

Technical indicators provider using any OHLCV data provider via stockstats library.
Calculates indicators from historical price data.
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime, timedelta
import pandas as pd
from stockstats import wrap

from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
from ba2_trade_platform.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_trade_platform.core.provider_utils import validate_date_range, log_provider_call
from ba2_trade_platform.logger import logger


class PandasIndicatorCalc(MarketIndicatorsInterface):
    """
    Pandas-based technical indicators calculator.
    
    Uses stockstats library to calculate indicators from OHLCV data provided by any data provider.
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
    
    def __init__(self, ohlcv_provider: MarketDataProviderInterface):
        """
        Initialize Pandas indicator calculator.
        
        Args:
            ohlcv_provider: Any OHLCV data provider implementing MarketDataProviderInterface
        """
        self._data_provider = ohlcv_provider
        logger.debug(f"Initialized PandasIndicatorCalc with provider: {ohlcv_provider.__class__.__name__}")
    
    def get_provider_name(self) -> str:
        """Get the name of this provider."""
        return "PandasIndicatorCalc"
    
    def get_supported_features(self) -> Dict[str, Any]:
        """Get supported features of this provider."""
        return {
            "indicators": self.SUPPORTED_INDICATOR_KEYS,
            "intervals": ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"],
            "max_lookback_days": 730  # Limited by underlying OHLCV provider
        }
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return self._data_provider is not None
    
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
        data = self._data_provider.get_ohlcv_data(
            symbol=symbol,
            start_date=fetch_start,
            end_date=datetime.now(),
            interval=interval
        )
        
        # Check if original data has timezone info
        has_tz = False
        if 'Date' in data.columns:
            has_tz = hasattr(data['Date'], 'dt') and data['Date'].dt.tz is not None
            logger.debug(f"Data has timezone: {has_tz}, dtype: {data['Date'].dtype}")
        
        # Wrap data with stockstats for indicator calculation
        df = wrap(data)
        
        # Preserve timezone info when converting Date
        if has_tz:
            # Data has timezone, preserve it
            df["Date"] = pd.to_datetime(df["Date"], utc=True)
            logger.debug(f"After wrap, preserved timezone: {df['Date'].dt.tz}")
        else:
            df["Date"] = pd.to_datetime(df["Date"])
        
        # Calculate indicator for all dates (triggers stockstats calculation)
        df[indicator]
        
        # Filter to requested date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        # Make start_dt and end_dt timezone-aware if df['Date'] is timezone-aware
        if hasattr(df["Date"], 'dt') and df["Date"].dt.tz is not None:
            # DataFrame dates are timezone-aware, make filter dates match
            if start_dt.tzinfo is None:
                # Timestamp is naive, localize it
                start_dt = start_dt.tz_localize('UTC')
            elif start_dt.tz != df["Date"].dt.tz:
                # Timestamp has different timezone, convert it
                start_dt = start_dt.tz_convert(df["Date"].dt.tz)
            
            if end_dt.tzinfo is None:
                # Timestamp is naive, localize it
                end_dt = end_dt.tz_localize('UTC')
            elif end_dt.tz != df["Date"].dt.tz:
                # Timestamp has different timezone, convert it
                end_dt = end_dt.tz_convert(df["Date"].dt.tz)
        
        mask = (df["Date"] >= start_dt) & (df["Date"] <= end_dt)
        filtered_df = df.loc[mask, ["Date", indicator]].copy()
        
        # Format date to preserve time component for intraday data
        filtered_df["Date"] = filtered_df["Date"].dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # Rename indicator column to 'value' for consistency
        filtered_df = filtered_df.rename(columns={indicator: "value"})
        
        return filtered_df
    
    @log_provider_call
    def get_indicator(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        indicator: Annotated[str, "Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')"],
        start_date: Annotated[Optional[datetime], "Start date for data (mutually exclusive with lookback_days)"] = None,
        end_date: Annotated[Optional[datetime], "End date for data (inclusive, mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date/end_date)"] = None,
        interval: Annotated[str, "Data interval (1d, 1h, etc.)"] = "1d",
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get technical indicator data for a symbol.
        
        Uses stockstats library to calculate indicators from Yahoo Finance data.
        Automatically adds buffer days for indicator calculation (moving averages need history).
        
        Returns:
            - format_type="markdown": Markdown-formatted string (for LLM consumption)
            - format_type="dict": Structured Python dict with dates/values/metadata (not markdown, JSON-serializable)
            - format_type="both": Dict with "text" (markdown) and "data" (structured dict) keys
        """
        # Validate inputs
        if indicator not in self.SUPPORTED_INDICATOR_KEYS:
            raise ValueError(
                f"Indicator '{indicator}' not supported by this provider. "
                f"Supported indicators: {self.SUPPORTED_INDICATOR_KEYS}"
            )
        
        # Validate date range - returns tuple (start_date, end_date)
        actual_start_date, actual_end_date = validate_date_range(start_date, end_date, lookback_days)
        
        # Use actual_end_date if provided, otherwise use end_date
        if actual_end_date:
            end_date = actual_end_date
        
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
            
            # Convert DataFrame to data points list and extract arrays for structured format
            data_points = []
            iso_dates = []
            values = []
            
            for _, row in indicator_df.iterrows():
                try:
                    # Parse date and value
                    date_str = row["Date"]
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    value = float(row["value"])
                    
                    iso_date = date_obj.isoformat()
                    data_points.append({
                        "date": iso_date,
                        "value": round(value, 4)
                    })
                    iso_dates.append(iso_date)
                    values.append(round(value, 4))
                    
                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping row due to parsing error: {e}")
                    continue
            
            # Get indicator metadata from centralized ALL_INDICATORS
            ind_meta = MarketIndicatorsInterface.ALL_INDICATORS[indicator]
            
            # Build structured dict response (JSON-serializable, no markdown)
            # This is used for format_type="dict" and "both"
            structured_response = {
                "symbol": symbol.upper(),
                "indicator": indicator,
                "indicator_name": ind_meta["name"],
                "interval": interval,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "dates": iso_dates,  # Structured format: direct arrays
                "values": values,    # Structured format: direct arrays
                "metadata": {
                    "count": len(data_points),
                    "first_date": iso_dates[0] if iso_dates else None,
                    "last_date": iso_dates[-1] if iso_dates else None,
                    "data_type": "float",
                    "precision": 4,
                    "description": ind_meta["description"],
                    "usage": ind_meta["usage"],
                    "tips": ind_meta["tips"],
                    "missing_periods": []  # TODO: Calculate from gaps in data
                }
            }
            
            # Build full response for markdown format (includes data points for reference)
            markdown_response = {
                "symbol": symbol.upper(),
                "indicator": indicator,
                "indicator_name": ind_meta["name"],
                "interval": interval,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "data": data_points,  # Keep for backward compatibility
                "metadata": {
                    "description": ind_meta["description"],
                    "usage": ind_meta["usage"],
                    "tips": ind_meta["tips"],
                    "interpretation": f"{ind_meta['description']} {ind_meta['usage']}"
                }
            }
            
            if format_type == "dict":
                return structured_response
            elif format_type == "both":
                return {
                    "text": self._format_markdown(markdown_response),
                    "data": structured_response
                }
            else:  # markdown
                return self._format_markdown(markdown_response)
                
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
            # Use interval-aware datetime formatting
            interval = data.get('interval', '1d')
            for point in data['data']:
                # Parse the ISO date string
                from datetime import datetime
                date_obj = datetime.fromisoformat(point['date'])
                # Format using interval-aware helper
                date_str = self.format_datetime_for_markdown(date_obj, interval)
                md += f"| {date_str} | {point['value']} |\n"
            
            md += f"\n*Total data points: {len(data['data'])}*\n"
        else:
            md += "## Indicator Values\n\n"
            md += "*No data available for the specified period*\n"
        
        return md
    
    def _format_as_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Format data as dictionary (already in dict format from get_indicator)."""
        return data
    
    def _format_as_markdown(self, data: Dict[str, Any]) -> str:
        """Format data as markdown (uses _format_markdown)."""
        return self._format_markdown(data)
    
    def get_supported_indicators(self) -> list[str]:
        """Return list of supported indicator names."""
        return self.SUPPORTED_INDICATOR_KEYS

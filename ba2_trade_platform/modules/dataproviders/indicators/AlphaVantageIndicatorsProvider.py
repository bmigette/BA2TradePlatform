"""
Alpha Vantage Indicators Provider

Technical indicators provider using Alpha Vantage API.
Provides direct API access to technical indicators (less computation than stockstats).
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime, timedelta
import json

from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
from ba2_trade_platform.core.provider_utils import validate_date_range, log_provider_call
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import (
    make_api_request,
    AlphaVantageRateLimitError
)
from ba2_trade_platform.logger import logger


class AlphaVantageIndicatorsProvider(MarketIndicatorsInterface):
    """
    Alpha Vantage technical indicators provider.
    
    Uses Alpha Vantage API to retrieve pre-calculated technical indicators.
    More API efficient than calculating from raw data, but has rate limits.
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
        "atr"
        # Note: Alpha Vantage doesn't support vwma or mfi
    ]
    
    def __init__(self):
        """Initialize Alpha Vantage indicators provider."""
        logger.debug("Initialized AlphaVantageIndicatorsProvider")
    
    def _fetch_indicator_data(
        self,
        symbol: str,
        indicator: str,
        interval: str = "daily",
        time_period: int = 14,
        series_type: str = "close"
    ) -> str:
        """
        Fetch raw indicator data from Alpha Vantage API.
        
        Args:
            symbol: Stock ticker symbol
            indicator: Indicator key (e.g., 'rsi', 'macd', 'close_50_sma')
            interval: Time interval (daily, weekly, monthly, 60min, etc.)
            time_period: Number of data points for calculation
            series_type: The desired price type (close, open, high, low)
            
        Returns:
            CSV string with indicator data
        """
        # Map internal indicator names to Alpha Vantage functions and parameters
        if indicator == "close_50_sma":
            data = make_api_request("SMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "50",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "close_200_sma":
            data = make_api_request("SMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "200",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "close_10_ema":
            data = make_api_request("EMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "10",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator in ["macd", "macds", "macdh"]:
            data = make_api_request("MACD", {
                "symbol": symbol,
                "interval": interval,
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "rsi":
            data = make_api_request("RSI", {
                "symbol": symbol,
                "interval": interval,
                "time_period": str(time_period),
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator in ["boll", "boll_ub", "boll_lb"]:
            data = make_api_request("BBANDS", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "20",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "atr":
            data = make_api_request("ATR", {
                "symbol": symbol,
                "interval": interval,
                "time_period": str(time_period),
                "datatype": "csv"
            })
        else:
            raise ValueError(f"Indicator {indicator} not supported")
        
        return data
    
    @log_provider_call
    def get_indicator(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        indicator: Annotated[str, "Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')"],
        end_date: Annotated[datetime, "End date for data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for data (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        interval: Annotated[str, "Data interval (1d, 1h, etc.)"] = "1d",
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get technical indicator data for a symbol using Alpha Vantage API.
        
        Uses Alpha Vantage's technical indicator endpoints for efficient retrieval.
        Subject to Alpha Vantage API rate limits.
        """
        # Validate inputs
        if indicator not in self.SUPPORTED_INDICATOR_KEYS:
            raise ValueError(
                f"Indicator '{indicator}' not supported by this provider. "
                f"Supported indicators: {self.SUPPORTED_INDICATOR_KEYS}"
            )
        
        # Validate date range - returns tuple (start_date, end_date)
        actual_start_date, actual_end_date = validate_date_range(start_date, end_date, lookback_days)
        
        # Use validated end_date if provided
        if actual_end_date:
            end_date = actual_end_date
        
        # Format dates for Alpha Vantage
        curr_date_str = end_date.strftime("%Y-%m-%d")
        
        # Calculate lookback days from date range
        lookback = (end_date - actual_start_date).days
        
        # Map interval to Alpha Vantage format
        av_interval_map = {
            "1d": "daily",
            "1h": "60min",
            "4h": "60min",  # Alpha Vantage doesn't have 4h, use 60min
            "1w": "weekly",
            "1mo": "monthly"
        }
        av_interval = av_interval_map.get(interval, "daily")
        
        logger.debug(
            f"Fetching {indicator} for {symbol} from Alpha Vantage "
            f"(lookback: {lookback} days, interval: {av_interval})"
        )
        
        try:
            # Get raw CSV data from Alpha Vantage
            csv_data = self._fetch_indicator_data(
                symbol=symbol,
                indicator=indicator,
                interval=av_interval,
                time_period=14,  # Default period for RSI, ATR, etc.
                series_type="close"
            )
            
            # Parse CSV and filter by date range
            data_points = self._parse_csv_data(
                csv_data=csv_data,
                indicator=indicator,
                start_date=actual_start_date,
                end_date=end_date
            )
            
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
                },
                "source": "Alpha Vantage API"
            }
            
            if format_type == "dict":
                return response
            elif format_type == "both":
                return {
                    "text": self._format_markdown(response),
                    "data": response
                }
            else:  # markdown
                return self._format_markdown(response)
                
        except Exception as e:
            logger.error(f"Failed to get {indicator} for {symbol} from Alpha Vantage: {e}")
            raise
    
    def _parse_csv_data(
        self,
        csv_data: str,
        indicator: str,
        start_date: datetime,
        end_date: datetime
    ) -> list[Dict[str, Any]]:
        """
        Parse CSV data from Alpha Vantage and filter by date range.
        
        Args:
            csv_data: Raw CSV string from API
            indicator: Indicator name
            start_date: Start date for filtering
            end_date: End date for filtering
            
        Returns:
            List of data points with date and value
        """
        lines = csv_data.strip().split('\n')
        if len(lines) < 2:
            return []
        
        # Parse header and find column indices
        header = [col.strip() for col in lines[0].split(',')]
        
        try:
            date_col_idx = header.index('time')
        except ValueError:
            logger.error(f"'time' column not found in data for {indicator}. Available columns: {header}")
            return []
        
        # Map internal indicator names to expected CSV column names from Alpha Vantage
        col_name_map = {
            "macd": "MACD",
            "macds": "MACD_Signal",
            "macdh": "MACD_Hist",
            "boll": "Real Middle Band",
            "boll_ub": "Real Upper Band",
            "boll_lb": "Real Lower Band",
            "rsi": "RSI",
            "atr": "ATR",
            "close_10_ema": "EMA",
            "close_50_sma": "SMA",
            "close_200_sma": "SMA"
        }
        
        target_col_name = col_name_map.get(indicator)
        
        if not target_col_name:
            # Default to the second column if no specific mapping exists
            value_col_idx = 1
        else:
            try:
                value_col_idx = header.index(target_col_name)
            except ValueError:
                logger.error(f"Column '{target_col_name}' not found for indicator '{indicator}'. Available columns: {header}")
                value_col_idx = 1  # Fallback to second column
        
        # Parse data rows
        result_data = []
        for line in lines[1:]:
            if not line.strip():
                continue
            
            values = line.split(',')
            if len(values) > value_col_idx:
                try:
                    date_str = values[date_col_idx].strip()
                    # Parse the date
                    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
                    
                    # Check if date is in our range
                    if start_date <= date_dt <= end_date:
                        value_str = values[value_col_idx].strip()
                        try:
                            value = float(value_str)
                        except ValueError:
                            value = value_str  # Keep as string if can't convert
                        
                        result_data.append({
                            "date": date_dt.isoformat(),
                            "value": value
                        })
                except (ValueError, IndexError) as e:
                    logger.debug(f"Error parsing line: {e}")
                    continue
        
        # Sort by date
        result_data.sort(key=lambda x: x["date"])
        
        return result_data
    
    def _parse_indicator_result(self, result_str: str, indicator: str) -> list[Dict[str, Any]]:
        """
        Parse indicator result string into structured data points.
        
        DEPRECATED: This method is kept for backwards compatibility.
        Use _parse_csv_data instead.
        
        Args:
            result_str: Raw indicator result from Alpha Vantage
            indicator: Indicator name
        
        Returns:
            List of data points with date and value
        """
        data_points = []
        
        # The result format from TradingAgents is:
        # "## INDICATOR values from YYYY-MM-DD to YYYY-MM-DD:\n\nYYYY-MM-DD: value\n..."
        lines = result_str.strip().split('\n')
        
        for line in lines:
            if ':' in line and not line.startswith('#'):
                parts = line.split(':')
                if len(parts) >= 2:
                    date_str = parts[0].strip()
                    try:
                        # Try to parse date
                        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                        # Extract numeric value
                        value_str = parts[1].strip()
                        # Handle "N/A" or non-numeric values
                        if value_str.lower() == 'n/a' or 'not available' in value_str.lower():
                            continue
                        try:
                            value = float(value_str)
                            data_points.append({
                                "date": date_obj.isoformat(),
                                "value": round(value, 4)
                            })
                        except ValueError:
                            # Skip non-numeric values
                            continue
                    except ValueError:
                        # Skip lines that don't match date format
                        continue
        
        return data_points
    
    def _format_markdown(self, data: Dict[str, Any]) -> str:
        """Format indicator data as markdown."""
        md = f"# {data['indicator_name']} ({data['indicator']})\n\n"
        md += f"**Symbol:** {data['symbol']}  \n"
        md += f"**Interval:** {data['interval']}  \n"
        md += f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}  \n"
        md += f"**Source:** {data.get('source', 'Alpha Vantage API')}  \n\n"
        
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

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alphavantage"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["technical_indicators", "market_indicators"]
    
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


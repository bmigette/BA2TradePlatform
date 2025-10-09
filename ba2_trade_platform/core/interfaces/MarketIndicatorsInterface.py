"""
Interface for technical market indicators providers.

This interface defines methods for retrieving technical indicators like RSI, MACD,
SMA, EMA, etc. from various data providers.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class MarketIndicatorsInterface(DataProviderInterface):
    """
    Interface for technical indicator providers.
    
    Providers implementing this interface can supply technical analysis indicators
    for stocks and other instruments.
    
    All indicator providers must accept an OHLCV data provider in their constructor
    to retrieve price data for indicator calculation.
    """
    
    @abstractmethod
    def __init__(self, ohlcv_provider: DataProviderInterface):
        """
        Initialize the indicator provider with an OHLCV data provider.
        
        Args:
            ohlcv_provider: Data provider implementing MarketDataProviderInterface
                           for retrieving OHLCV (Open, High, Low, Close, Volume) data
        """
        pass
    
    # Centralized indicator metadata - all providers share this catalog
    ALL_INDICATORS = {
        # Moving Averages
        "close_50_sma": {
            "name": "50-day Simple Moving Average",
            "description": "50 SMA: A medium-term trend indicator.",
            "usage": "Identify trend direction and serve as dynamic support/resistance.",
            "tips": "It lags price; combine with faster indicators for timely signals."
        },
        "close_200_sma": {
            "name": "200-day Simple Moving Average",
            "description": "200 SMA: A long-term trend benchmark.",
            "usage": "Confirm overall market trend and identify golden/death cross setups.",
            "tips": "It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        },
        "close_10_ema": {
            "name": "10-day Exponential Moving Average",
            "description": "10 EMA: A responsive short-term average.",
            "usage": "Capture quick shifts in momentum and potential entry points.",
            "tips": "Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        },
        # MACD
        "macd": {
            "name": "MACD Line",
            "description": "MACD: Computes momentum via differences of EMAs.",
            "usage": "Look for crossovers and divergence as signals of trend changes.",
            "tips": "Confirm with other indicators in low-volatility or sideways markets."
        },
        "macds": {
            "name": "MACD Signal Line",
            "description": "MACD Signal: An EMA smoothing of the MACD line.",
            "usage": "Use crossovers with the MACD line to trigger trades.",
            "tips": "Should be part of a broader strategy to avoid false positives."
        },
        "macdh": {
            "name": "MACD Histogram",
            "description": "MACD Histogram: Shows the gap between the MACD line and its signal.",
            "usage": "Visualize momentum strength and spot divergence early.",
            "tips": "Can be volatile; complement with additional filters in fast-moving markets."
        },
        # Momentum
        "rsi": {
            "name": "Relative Strength Index",
            "description": "RSI: Measures momentum to flag overbought/oversold conditions.",
            "usage": "Apply 70/30 thresholds and watch for divergence to signal reversals.",
            "tips": "In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        },
        # Volatility
        "boll": {
            "name": "Bollinger Middle Band",
            "description": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands.",
            "usage": "Acts as a dynamic benchmark for price movement.",
            "tips": "Combine with the upper and lower bands to effectively spot breakouts or reversals."
        },
        "boll_ub": {
            "name": "Bollinger Upper Band",
            "description": "Bollinger Upper Band: Typically 2 standard deviations above the middle line.",
            "usage": "Signals potential overbought conditions or resistance zones.",
            "tips": "Price touching this band doesn't guarantee reversal; check for confirmation."
        },
        "boll_lb": {
            "name": "Bollinger Lower Band",
            "description": "Bollinger Lower Band: Typically 2 standard deviations below the middle line.",
            "usage": "Signals potential oversold conditions or support zones.",
            "tips": "Price touching this band can indicate buying opportunities in strong uptrends."
        },
        "atr": {
            "name": "Average True Range",
            "description": "ATR: Averages true range to measure volatility.",
            "usage": "Set stop-loss levels and adjust position sizes based on current market volatility.",
            "tips": "It's a reactive measure, so use it as part of a broader risk management strategy."
        },
        "vwma": {
            "name": "Volume Weighted Moving Average",
            "description": "VWMA: A moving average weighted by volume.",
            "usage": "Confirm trends by integrating price action with volume data.",
            "tips": "Watch for skewed results from volume spikes; use in combination with other volume analyses."
        },
        "mfi": {
            "name": "Money Flow Index",
            "description": "MFI: Uses both price and volume to measure buying and selling pressure.",
            "usage": "Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals.",
            "tips": "Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        }
    }
    
    @abstractmethod
    def get_indicator(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        indicator: Annotated[str, "Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')"],
        start_date: Annotated[Optional[datetime], "Start date for data (mutually exclusive with lookback_days)"] = None,
        end_date: Annotated[Optional[datetime], "End date for data (inclusive, mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date/end_date)"] = None,
        interval: Annotated[str, "Data interval (1d, 1h, etc.)"] = "1d",
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get technical indicator data for a symbol.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            indicator: Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')
            start_date: Start date (use either start_date+end_date OR lookback_days, not both)
            end_date: End date for data (use either start_date+end_date OR lookback_days, not both)
            lookback_days: Number of days to look back from end_date (use either this OR start_date+end_date, not both)
            interval: Data interval (e.g., '1d' for daily, '1h' for hourly)
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "indicator": str,
                "interval": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "data": [{
                    "date": str (ISO format),
                    "value": float
                }],
                "metadata": {
                    "description": str,
                    "interpretation": str
                }
            }
            If format_type='markdown': Formatted markdown string with indicator data
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_supported_indicators(self) -> list[str]:
        """
        Return list of supported indicator names.
        
        Returns:
            list[str]: List of indicator names this provider supports
                      (e.g., ['rsi', 'macd', 'sma', 'ema', 'bbands'])
        """
        pass

"""
Interface for macroeconomic data providers.

This interface defines methods for retrieving macroeconomic indicators,
yield curves, and Federal Reserve calendar data.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class MacroEconomicsInterface(DataProviderInterface):
    """
    Interface for macroeconomic data providers.
    
    Providers implementing this interface supply macroeconomic data including
    economic indicators (GDP, unemployment, inflation), yield curves,
    and Federal Reserve calendar information.
    """
    
    @abstractmethod
    def get_economic_indicators(
        self,
        end_date: Annotated[datetime, "End date for indicators (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for indicators (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        indicators: Annotated[Optional[list[str]], "List of indicator names (e.g., ['GDP', 'UNRATE', 'CPIAUCSL'])"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get economic indicators (GDP, unemployment, inflation, etc.).
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            indicators: List of indicator names/codes (e.g., ['GDP', 'UNRATE', 'CPIAUCSL']),
                       or None to get all available indicators
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Common Indicators:
            - GDP: Gross Domestic Product
            - UNRATE: Unemployment Rate
            - CPIAUCSL: Consumer Price Index (CPI)
            - FEDFUNDS: Federal Funds Rate
            - DGS10: 10-Year Treasury Constant Maturity Rate
            - DEXUSEU: U.S. / Euro Foreign Exchange Rate
            - UMCSENT: Consumer Sentiment Index
            - PAYEMS: All Employees (Nonfarm Payrolls)
        
        Returns:
            If format_type='dict': {
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "indicators": [{
                    "code": str,
                    "name": str,
                    "description": str,
                    "unit": str,
                    "frequency": str,
                    "data": [{
                        "date": str (ISO format),
                        "value": float
                    }]
                }]
            }
            If format_type='markdown': Formatted markdown with indicator values
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_yield_curve(
        self,
        end_date: Annotated[datetime, "End date for yield curve data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Treasury yield curve data.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        If only end_date is provided (both start_date and lookback_days are None),
        returns single most recent yield curve as of end_date.
        
        Returns:
            If format_type='dict': {
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "curves": [{
                    "date": str (ISO format),
                    "maturities": {
                        "1m": float,   # 1-month
                        "3m": float,   # 3-month
                        "6m": float,   # 6-month
                        "1y": float,   # 1-year
                        "2y": float,   # 2-year
                        "3y": float,   # 3-year
                        "5y": float,   # 5-year
                        "7y": float,   # 7-year
                        "10y": float,  # 10-year
                        "20y": float,  # 20-year
                        "30y": float   # 30-year
                    },
                    "inversion": bool,  # True if curve is inverted
                    "spread_10y_2y": float  # 10Y-2Y spread (basis points)
                }]
            }
            If format_type='markdown': Formatted markdown with yield curve data
        
        Raises:
            ValueError: If both start_date and lookback_days are provided
        """
        pass
    
    @abstractmethod
    def get_fed_calendar(
        self,
        end_date: Annotated[datetime, "End date for Fed events (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for Fed events (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Federal Reserve calendar and meeting minutes.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "events": [{
                    "date": str (ISO format),
                    "event_type": str,  # 'fomc_meeting', 'statement', 'minutes', 'speech'
                    "title": str,
                    "description": str,
                    "speaker": str (optional - for speeches),
                    "decision": str (optional - rate decision),
                    "rate_change": float (optional - basis points),
                    "new_rate": float (optional - new fed funds rate),
                    "url": str (optional - link to minutes/statement),
                    "summary": str (optional - key takeaways)
                }]
            }
            If format_type='markdown': Formatted markdown with Fed events
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass

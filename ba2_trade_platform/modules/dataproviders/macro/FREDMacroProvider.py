"""
FRED Macro Economics Provider

Provides macroeconomic data from the Federal Reserve Economic Data (FRED) API
including economic indicators, yield curves, and Federal Reserve calendar.
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime, timedelta
import requests

from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.core.interfaces import MacroEconomicsInterface
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range,
    log_provider_call
)
from ba2_trade_platform.logger import logger


class FREDMacroProvider(MacroEconomicsInterface):
    """
    Federal Reserve Economic Data (FRED) macro provider.
    
    Provides economic indicators, yield curves, and Fed calendar data
    from the St. Louis Federal Reserve FRED API.
    """
    
    API_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
    
    # Economic indicator series IDs
    INDICATOR_SERIES = {
        "Federal Funds Rate": {
            "series": "FEDFUNDS",
            "description": "Federal Reserve's target interest rate",
            "unit": "%"
        },
        "Consumer Price Index (CPI)": {
            "series": "CPIAUCSL",
            "description": "Inflation measure based on consumer goods",
            "unit": "Index",
            "yoy": True
        },
        "Producer Price Index (PPI)": {
            "series": "PPIACO",
            "description": "Inflation measure at producer level",
            "unit": "Index",
            "yoy": True
        },
        "Unemployment Rate": {
            "series": "UNRATE",
            "description": "Percentage of labor force unemployed",
            "unit": "%"
        },
        "Nonfarm Payrolls": {
            "series": "PAYEMS",
            "description": "Monthly change in employment",
            "unit": "Thousands",
            "mom": True
        },
        "GDP Growth Rate": {
            "series": "GDP",
            "description": "Gross Domestic Product growth",
            "unit": "Billions",
            "qoq": True
        },
        "ISM Manufacturing PMI": {
            "series": "NAPM",
            "description": "Manufacturing sector health indicator",
            "unit": "Index"
        },
        "Consumer Confidence": {
            "series": "CSCICP03USM665S",
            "description": "Consumer sentiment indicator",
            "unit": "Index"
        },
        "VIX": {
            "series": "VIXCLS",
            "description": "Market volatility index",
            "unit": "Index"
        }
    }
    
    # Treasury yield series IDs
    YIELD_SERIES = {
        "1 Month": "DGS1MO",
        "3 Month": "DGS3MO",
        "6 Month": "DGS6MO",
        "1 Year": "DGS1",
        "2 Year": "DGS2",
        "3 Year": "DGS3",
        "5 Year": "DGS5",
        "7 Year": "DGS7",
        "10 Year": "DGS10",
        "20 Year": "DGS20",
        "30 Year": "DGS30"
    }
    
    def __init__(self):
        """Initialize FRED macro provider."""
        self._api_key = get_app_setting("fred_api_key")
        if not self._api_key:
            raise ValueError(
                "FRED API key not configured. "
                "Please set 'fred_api_key' in AppSetting table."
            )
        logger.debug("Initialized FREDMacroProvider")
    
    def _get_fred_data(self, series_id: str, start_date: str, end_date: str) -> Dict:
        """
        Get economic data from FRED API.
        
        Args:
            series_id: FRED series ID (e.g., 'FEDFUNDS', 'CPIAUCSL')
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            
        Returns:
            Dictionary with FRED data
        """
        # API key is validated in __init__, so we can proceed directly
        params = {
            'series_id': series_id,
            'api_key': self._api_key,
            'file_type': 'json',
            'observation_start': start_date,
            'observation_end': end_date,
            'sort_order': 'desc',
            'limit': 100
        }
        
        try:
            response = requests.get(self.API_BASE_URL, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": f"Failed to fetch FRED data for {series_id}: {str(e)}"}
    
    @log_provider_call
    def get_economic_indicators(
        self,
        end_date: Annotated[datetime, "End date for indicators (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for indicators (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        indicators: Annotated[Optional[list[str]], "List of indicator names"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get economic indicators (GDP, unemployment, inflation, etc.).
        """
        # Calculate start_date from lookback_days if not provided
        if start_date is None and lookback_days:
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        
        # Validate date range
        actual_start_date, actual_end_date = validate_date_range(start_date, end_date, max_days=None)
        
        # Use validated dates
        if actual_start_date is None:
            # Default to 365 days (1 year) lookback for meaningful trend analysis
            # Most economic indicators are monthly/quarterly, so 1 year gives ~4-12 data points
            actual_start_date, end_date = calculate_date_range(end_date, 365)
        if actual_end_date:
            end_date = actual_end_date
        
        start_str = actual_start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        
        logger.debug(f"Fetching economic indicators from {start_str} to {end_str}")
        
        # Build result
        result_data = {
            "start_date": start_str,
            "end_date": end_str,
            "indicators": []
        }
        
        result_md = f"## Economic Indicators Report ({start_str} to {end_str})\n\n"
        
        for indicator_name, config in self.INDICATOR_SERIES.items():
            data = self._get_fred_data(config["series"], start_str, end_str)
            
            if "error" in data:
                if format_type == "markdown":
                    result_md += f"### {indicator_name}\n**Error**: {data['error']}\n\n"
                continue
            
            observations = data.get("observations", [])
            if not observations:
                if format_type == "markdown":
                    result_md += f"### {indicator_name}\n**No data available**\n\n"
                continue
            
            # Filter out missing values
            valid_obs = [obs for obs in observations if obs.get("value") != "."]
            if not valid_obs:
                if format_type == "markdown":
                    result_md += f"### {indicator_name}\n**No valid data available**\n\n"
                continue
            
            latest = valid_obs[0]
            latest_value = float(latest["value"])
            latest_date = latest["date"]
            
            if format_type == "markdown":
                result_md += f"### {indicator_name}\n"
                result_md += f"- **Latest Value**: {latest_value:.2f} {config['unit']} (as of {latest_date})\n"
                result_md += f"- **Description**: {config['description']}\n"
                
                # Calculate changes if we have enough data
                if len(valid_obs) >= 2:
                    previous = valid_obs[1]
                    previous_value = float(previous["value"])
                    change = latest_value - previous_value
                    change_pct = (change / previous_value) * 100 if previous_value != 0 else 0
                    
                    result_md += f"- **Change**: {change:+.2f} {config['unit']} ({change_pct:+.2f}%)\n"
                    result_md += f"- **Previous**: {previous_value:.2f} {config['unit']} (as of {previous['date']})\n"
                
                # Add interpretation (simplified from macro_utils.py)
                if indicator_name == "Federal Funds Rate":
                    if latest_value > 4.0:
                        result_md += "- **💡 Analysis**: Restrictive monetary policy stance\n"
                    elif latest_value < 2.0:
                        result_md += "- **💡 Analysis**: Accommodative monetary policy stance\n"
                    else:
                        result_md += "- **💡 Analysis**: Neutral monetary policy stance\n"
                
                result_md += "\n"
        
        if format_type == "dict":
            return result_data
        elif format_type == "both":
            return {
                "text": result_md,
                "data": result_data
            }
        else:  # markdown
            return result_md
    
    @log_provider_call
    def get_yield_curve(
        self,
        end_date: Annotated[datetime, "End date for yield curve data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Treasury yield curve data.
        """
        # Calculate start_date from lookback_days if not provided
        if start_date is None and lookback_days:
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        
        # Validate date range
        actual_start_date, actual_end_date = validate_date_range(start_date, end_date, max_days=None)
        
        # Use validated dates
        if actual_start_date is None:
            # Default to 365 days (1 year) lookback for meaningful trend analysis
            # Yield curve data is daily, so 1 year gives good historical context
            actual_start_date, end_date = calculate_date_range(end_date, 365)
        if actual_end_date:
            end_date = actual_end_date
        
        start_str = actual_start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        
        logger.debug(f"Fetching yield curve from {start_str} to {end_str}")
        
        result_md = f"## Treasury Yield Curve as of {end_str}\n\n"
        
        yield_data = []
        for maturity, series_id in self.YIELD_SERIES.items():
            data = self._get_fred_data(series_id, start_str, end_str)
            
            if "error" in data:
                continue
            
            observations = data.get("observations", [])
            if observations:
                latest = observations[0]
                if latest.get("value") != ".":
                    yield_data.append({
                        "maturity": maturity,
                        "yield": float(latest["value"]),
                        "date": latest["date"]
                    })
        
        if yield_data:
            result_md += "| Maturity | Yield (%) | Date |\n"
            result_md += "|----------|-----------|------|\n"
            
            for item in yield_data:
                result_md += f"| {item['maturity']} | {item['yield']:.2f}% | {item['date']} |\n"
            
            # Calculate yield curve analysis
            result_md += "\n### Yield Curve Analysis\n"
            
            # Find 2Y and 10Y for inversion check
            two_year = next((item for item in yield_data if item["maturity"] == "2 Year"), None)
            ten_year = next((item for item in yield_data if item["maturity"] == "10 Year"), None)
            
            if two_year and ten_year:
                spread = ten_year["yield"] - two_year["yield"]
                result_md += f"- **2Y-10Y Spread**: {spread:.2f} basis points\n"
                
                if spread < 0:
                    result_md += "- **⚠️ INVERTED YIELD CURVE**: Potential recession signal\n"
                elif spread < 50:
                    result_md += "- **📊 FLAT YIELD CURVE**: Economic uncertainty\n"
                else:
                    result_md += "- **📈 NORMAL YIELD CURVE**: Healthy economic expectations\n"
        else:
            result_md += "No recent yield curve data available.\n"
        
        return result_md
    
    @log_provider_call
    def get_fed_calendar(
        self,
        end_date: Annotated[datetime, "End date for Fed events (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for Fed events (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Federal Reserve calendar and meeting minutes.
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=365)
        
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        
        logger.debug(f"Fetching Fed calendar from {start_str} to {end_str}")
        
        result_md = f"## Federal Reserve Calendar & Policy Updates\n\n"
        
        # Get recent Fed Funds rate data to show policy trajectory
        fed_data = self._get_fred_data("FEDFUNDS", start_str, end_str)
        
        if "error" not in fed_data:
            observations = fed_data.get("observations", [])
            valid_obs = [obs for obs in observations if obs.get("value") != "."]
            
            if valid_obs and len(valid_obs) >= 2:
                result_md += "### Recent Federal Funds Rate History\n"
                result_md += "| Date | Rate (%) | Change |\n"
                result_md += "|------|----------|--------|\n"
                
                for i, obs in enumerate(valid_obs[:6]):  # Show last 6 observations
                    rate = float(obs["value"])
                    if i < len(valid_obs) - 1:
                        prev_rate = float(valid_obs[i + 1]["value"])
                        change = rate - prev_rate
                        change_str = f"{change:+.2f}%" if change != 0 else "No change"
                    else:
                        change_str = "-"
                    
                    result_md += f"| {obs['date']} | {rate:.2f}% | {change_str} |\n"
                
                result_md += "\n"
        
        return result_md
    
    def get_provider_name(self) -> str:
        """Return the provider name."""
        return "fred"
    
    def get_supported_features(self) -> list[str]:
        """Return list of supported features."""
        return ["economic_indicators", "yield_curve", "fed_calendar"]
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return bool(self._api_key)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format data as structured dictionary."""
        # FRED provider already returns dict format directly from methods
        if isinstance(data, dict):
            return data
        return {"content": str(data)}
    
    def _format_as_markdown(self, data: Any) -> str:
        """Format data as markdown."""
        # FRED provider already returns markdown format directly from methods
        if isinstance(data, str):
            return data
        return str(data)

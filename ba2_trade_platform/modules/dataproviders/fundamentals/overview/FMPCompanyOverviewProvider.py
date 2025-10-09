"""
FMP (Financial Modeling Prep) Company Overview Provider

Provides company overview and fundamentals using FMP's company profile endpoint.
Documentation: https://site.financialmodelingprep.com/developer/docs#company
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime
import json

import fmpsdk

from ba2_trade_platform.core.interfaces import CompanyFundamentalsOverviewInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting


class FMPCompanyOverviewProvider(CompanyFundamentalsOverviewInterface):
    """
    FMP company overview provider.
    
    Uses Financial Modeling Prep's company profile endpoint to retrieve
    comprehensive company fundamentals including financials, ratios, and metrics.
    
    API Endpoint: https://financialmodelingprep.com/api/v3/profile/{symbol}
    """
    
    def __init__(self):
        """Initialize FMP company overview provider."""
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("FMP_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "FMP API key not configured. "
                "Please set 'FMP_API_KEY' in AppSetting table."
            )
        
        logger.debug("Initialized FMPCompanyOverviewProvider")
    
    def get_provider_name(self) -> str:
        """Return the provider name."""
        return "fmp"
    
    def get_supported_features(self) -> list[str]:
        """Return list of supported features."""
        return ["fundamentals_overview", "company_profile"]
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return bool(self.api_key)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        This is used by the base class format_response method.
        For FMP, the data is already in dict format from get_fundamentals_overview.
        """
        if isinstance(data, dict):
            return data
        return {}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown.
        
        This is used by the base class format_response method.
        For FMP, we build markdown from the dict data.
        """
        if isinstance(data, str):
            return data
        
        if not isinstance(data, dict):
            return ""
        
        # If it's already a "both" format response with text key, return that
        if "text" in data:
            return data["text"]
        
        # Otherwise build markdown from dict
        profile = data.get("metrics", {})
        symbol = data.get("symbol", "")
        company_name = data.get("company_name", "")
        
        lines = []
        lines.append(f"# Company Profile: {company_name or symbol}")
        lines.append(f"**Symbol:** {symbol}  ")
        lines.append("")
        
        if profile.get("industry"):
            lines.append(f"**Industry:** {profile['industry']}  ")
        if profile.get("sector"):
            lines.append(f"**Sector:** {profile['sector']}  ")
        if profile.get("market_cap"):
            lines.append(f"**Market Cap:** ${profile['market_cap']:,.0f}  ")
        if profile.get("price"):
            lines.append(f"**Price:** ${profile['price']:.2f}  ")
        
        return "\n".join(lines)
    
    @log_provider_call
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview from FMP.
        
        Args:
            symbol: Stock ticker symbol
            as_of_date: Date for fundamentals (FMP returns most recent data)
            format_type: Output format - 'dict' for structured data, 'markdown' for text, 'both' for dual format
            
        Returns:
            If format_type='dict': Dictionary with company overview data
            If format_type='markdown': Formatted markdown string
            If format_type='both': Dict with keys 'text' (markdown) and 'data' (dict)
            
        Note: FMP's profile endpoint returns the most recent data available.
        The as_of_date parameter is accepted for interface compatibility but FMP
        doesn't support historical point-in-time queries for company profiles.
        """
        logger.debug(f"Fetching company overview for {symbol} (as of {as_of_date.date()}) from FMP")
        
        try:
            # Make FMP API request for company profile using fmpsdk
            # API returns array with single profile object
            profile_data = fmpsdk.company_profile(apikey=self.api_key, symbol=symbol.upper())
            
            if not profile_data or len(profile_data) == 0:
                raise ValueError(f"No profile data found for symbol {symbol}")
            
            # Get first item (FMP returns array with single object)
            profile = profile_data[0]
            
            # Build dict response (always build it for "both" format support)
            dict_response = {
                "symbol": symbol.upper(),
                "company_name": profile.get("companyName", ""),
                "as_of_date": as_of_date.isoformat(),
                "data_date": as_of_date.isoformat(),  # FMP doesn't provide historical data date
                "metrics": {
                    # Price and Valuation
                    "price": self._safe_float(profile.get("price")),
                    "market_cap": self._safe_float(profile.get("mktCap")),
                    "beta": self._safe_float(profile.get("beta")),
                    "volume_avg": self._safe_float(profile.get("volAvg")),
                    "last_div": self._safe_float(profile.get("lastDiv")),
                    "range": profile.get("range"),
                    "changes": self._safe_float(profile.get("changes")),
                    
                    # Company Info
                    "cik": profile.get("cik"),
                    "isin": profile.get("isin"),
                    "cusip": profile.get("cusip"),
                    "exchange": profile.get("exchange"),
                    "exchange_short_name": profile.get("exchangeShortName"),
                    "industry": profile.get("industry"),
                    "sector": profile.get("sector"),
                    "country": profile.get("country"),
                    "website": profile.get("website"),
                    "description": profile.get("description"),
                    "ceo": profile.get("ceo"),
                    "full_time_employees": profile.get("fullTimeEmployees"),
                    "phone": profile.get("phone"),
                    "address": profile.get("address"),
                    "city": profile.get("city"),
                    "state": profile.get("state"),
                    "zip": profile.get("zip"),
                    
                    # Stock Info
                    "currency": profile.get("currency"),
                    "is_etf": profile.get("isEtf", False),
                    "is_actively_trading": profile.get("isActivelyTrading", False),
                    "is_adr": profile.get("isAdr", False),
                    "is_fund": profile.get("isFund", False),
                    
                    # Images
                    "image": profile.get("image"),
                    
                    # IPO
                    "ipo_date": profile.get("ipoDate"),
                    
                    # Default data currency
                    "default_image": profile.get("defaultImage"),
                    "dcf_diff": self._safe_float(profile.get("dcfDiff")),
                    "dcf": self._safe_float(profile.get("dcf")),
                }
            }
            
            # Build markdown response
            lines = []
            lines.append(f"# Company Profile: {profile.get('companyName', symbol)}")
            lines.append(f"**Symbol:** {symbol.upper()}  ")
            lines.append(f"**As of Date:** {as_of_date.date()}  ")
            lines.append("")
            
            # Company Information
            lines.append("## Company Information")
            lines.append(f"- **Industry:** {profile.get('industry', 'N/A')}")
            lines.append(f"- **Sector:** {profile.get('sector', 'N/A')}")
            lines.append(f"- **Country:** {profile.get('country', 'N/A')}")
            lines.append(f"- **Exchange:** {profile.get('exchangeShortName', 'N/A')}")
            if profile.get('website'):
                lines.append(f"- **Website:** {profile.get('website')}")
            if profile.get('ceo'):
                lines.append(f"- **CEO:** {profile.get('ceo')}")
            if profile.get('fullTimeEmployees'):
                employees = profile.get('fullTimeEmployees')
                if isinstance(employees, (int, float)):
                    lines.append(f"- **Employees:** {int(employees):,}")
                else:
                    lines.append(f"- **Employees:** {employees}")
            lines.append("")
            
            # Current Market Data
            lines.append("## Current Market Data")
            if profile.get('price'):
                lines.append(f"- **Price:** ${profile.get('price'):.2f}")
            if profile.get('changes'):
                change = profile.get('changes')
                sign = '+' if change >= 0 else ''
                lines.append(f"- **Change:** {sign}{change:.2f} ({sign}{(change/profile.get('price', 1)*100):.2f}%)")
            if profile.get('mktCap'):
                lines.append(f"- **Market Cap:** ${profile.get('mktCap'):,.0f}")
            if profile.get('volAvg'):
                lines.append(f"- **Avg Volume:** {profile.get('volAvg'):,.0f}")
            if profile.get('beta'):
                lines.append(f"- **Beta:** {profile.get('beta'):.2f}")
            if profile.get('range'):
                lines.append(f"- **52W Range:** {profile.get('range')}")
            if profile.get('lastDiv'):
                lines.append(f"- **Last Dividend:** ${profile.get('lastDiv'):.2f}")
            lines.append("")
            
            # Valuation (if DCF available)
            if profile.get('dcf') or profile.get('dcfDiff'):
                lines.append("## Valuation")
                if profile.get('dcf'):
                    lines.append(f"- **DCF Value:** ${profile.get('dcf'):.2f}")
                if profile.get('dcfDiff'):
                    lines.append(f"- **DCF Diff:** {profile.get('dcfDiff'):.2f}")
                lines.append("")
            
            # Description
            if profile.get('description'):
                lines.append("## Company Description")
                lines.append(profile.get('description'))
                lines.append("")
            
            # Additional Info
            lines.append("## Additional Information")
            if profile.get('ipoDate'):
                lines.append(f"- **IPO Date:** {profile.get('ipoDate')}")
            lines.append(f"- **Currency:** {profile.get('currency', 'N/A')}")
            lines.append(f"- **Is ETF:** {'Yes' if profile.get('isEtf') else 'No'}")
            lines.append(f"- **Is Fund:** {'Yes' if profile.get('isFund') else 'No'}")
            lines.append(f"- **Is ADR:** {'Yes' if profile.get('isAdr') else 'No'}")
            lines.append(f"- **Actively Trading:** {'Yes' if profile.get('isActivelyTrading') else 'No'}")
            
            markdown = "\n".join(lines)
            
            # Return based on format_type
            if format_type == "dict":
                return dict_response
            elif format_type == "both":
                return {
                    "text": markdown,
                    "data": dict_response
                }
            else:  # markdown
                return markdown
                
        except Exception as e:
            logger.error(f"Failed to get company overview for {symbol} from FMP: {e}", exc_info=True)
            raise
    
    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Safely convert value to float, returning None if conversion fails."""
        if value is None or value == "None" or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

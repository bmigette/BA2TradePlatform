"""
OpenAI Company Overview Provider

Provides company overview and fundamentals using OpenAI's web search capabilities.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime, timedelta
from openai import OpenAI

from ba2_trade_platform.core.interfaces import CompanyFundamentalsOverviewInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform import config
from ba2_trade_platform.config import get_app_setting


class OpenAICompanyOverviewProvider(CompanyFundamentalsOverviewInterface):
    """
    OpenAI company overview provider.
    
    Uses OpenAI's web search capabilities to retrieve company fundamentals
    including P/E ratio, P/S ratio, cash flow, and other key metrics.
    """
    
    def __init__(self, model: str = None):
        """
        Initialize OpenAI company overview provider.
        
        Args:
            model: OpenAI model to use (e.g., 'gpt-4', 'gpt-4o-mini').
                   If not provided, uses OPENAI_QUICK_THINK_LLM from app settings (default: 'gpt-4')
        """
        super().__init__()
        
        # Get OpenAI configuration
        self.backend_url = config.OPENAI_BACKEND_URL
        self.model = model or get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
        self.default_lookback_days = int(get_app_setting("ECONOMIC_DATA_DAYS", "90"))
        
        self.client = OpenAI(base_url=self.backend_url)
        logger.debug(f"Initialized OpenAICompanyOverviewProvider with model={self.model}, backend_url={self.backend_url}")
    
    @log_provider_call
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview using OpenAI web search.
        
        Args:
            symbol: Stock ticker symbol
            as_of_date: Date for fundamentals
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Company overview data with key metrics
            
        Note: OpenAI searches for the most recent fundamentals data available
        up to the as_of_date. The lookback period defaults to 90 days but can
        be configured via ECONOMIC_DATA_DAYS setting.
        """
        logger.debug(f"Fetching company overview for {symbol} (as of {as_of_date.date()}) using OpenAI")
        
        try:
            # Calculate date range for the search
            start_date = as_of_date - timedelta(days=self.default_lookback_days)
            
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = as_of_date.strftime("%Y-%m-%d")
            
            # Call OpenAI API with web search
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Can you search for fundamental data and discussions on {symbol} from {start_str} to {end_str}. Make sure you only get the data posted during that period. List as a table with key metrics including: P/E ratio, P/S ratio, PEG ratio, EPS, dividend yield, market cap, revenue, profit margin, operating margin, ROE, ROA, cash flow, debt-to-equity, and any other relevant fundamental metrics.",
                            }
                        ],
                    }
                ],
                text={"format": {"type": "text"}},
                reasoning={},
                tools=[
                    {
                        "type": "web_search_preview",
                        "user_location": {"type": "approximate"},
                        "search_context_size": "low",
                    }
                ],
                temperature=1,
                max_output_tokens=4096,
                top_p=1,
                store=True,
            )
            
            # Extract the response text
            fundamentals_text = response.output[1].content[0].text
            
            # Build dict response (always build it for "both" format support)
            dict_response = {
                "symbol": symbol.upper(),
                "company_name": None,  # OpenAI doesn't provide structured name
                "as_of_date": as_of_date.isoformat(),
                "data_date": as_of_date.isoformat(),
                "metrics": {
                    "content": fundamentals_text,
                    "source": "OpenAI Web Search",
                    "search_period": f"{start_str} to {end_str}"
                }
            }
            
            # Build markdown response
            lines = []
            lines.append(f"# Company Fundamentals Overview for {symbol}")
            lines.append(f"**As of Date:** {as_of_date.date()}")
            lines.append(f"**Search Period:** {start_str} to {end_str}")
            lines.append(f"**Source:** OpenAI Web Search")
            lines.append("")
            lines.append(fundamentals_text)
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
            logger.error(f"Failed to get company overview for {symbol} from OpenAI: {e}", exc_info=True)
            raise

"""
AI Company Overview Provider

Provides company overview and fundamentals using AI web search capabilities.
Uses the centralized do_llm_call_with_websearch function for provider-agnostic web search.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime, timedelta

from ba2_trade_platform.core.interfaces import CompanyFundamentalsOverviewInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.core.ModelFactory import ModelFactory
from ba2_trade_platform.logger import logger


class AICompanyOverviewProvider(CompanyFundamentalsOverviewInterface):
    """
    AI company overview provider.
    
    Uses AI web search capabilities to retrieve company fundamentals including
    P/E ratio, P/S ratio, cash flow, and other key metrics.
    
    Supports OpenAI, NagaAI, xAI, and Google models with web search via the
    centralized do_llm_call_with_websearch function.
    
    Model format: "Provider/ModelName" (e.g., "OpenAI/gpt5", "NagaAI/grok3")
    """
    
    def __init__(self, model: str = None):
        """
        Initialize AI company overview provider.
        
        Args:
            model: AI model to use in format "Provider/ModelName"
                   (e.g., "OpenAI/gpt4o", "NagaAI/grok3").
                   REQUIRED - must be provided by caller.
        """
        super().__init__()
        
        if not model:
            raise ValueError("model parameter is required for AICompanyOverviewProvider - no default fallback allowed")
        
        self.model_string = model
        self.default_lookback_days = 90
        logger.debug(f"Initialized AICompanyOverviewProvider with model={self.model_string}")
    
    @log_provider_call
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview using AI web search.
        
        Args:
            symbol: Stock ticker symbol
            as_of_date: Date for fundamentals
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Company overview data with key metrics
            
        Note: AI searches for the most recent fundamentals data available
        up to the as_of_date. The lookback period defaults to 90 days.
        """
        logger.debug(f"Fetching company overview for {symbol} (as of {as_of_date.date()}) using {self.model_string}")
        
        try:
            # Calculate date range for the search
            start_date = as_of_date - timedelta(days=self.default_lookback_days)
            
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = as_of_date.strftime("%Y-%m-%d")
            
            # Build prompt for fundamentals search
            prompt = (
                f"Can you search for fundamental data and discussions on {symbol} from {start_str} to {end_str}. "
                f"Make sure you only get the data posted during that period. List as a table with key metrics including: "
                f"P/E ratio, P/S ratio, PEG ratio, EPS, dividend yield, market cap, revenue, profit margin, operating margin, "
                f"ROE, ROA, cash flow, debt-to-equity, and any other relevant fundamental metrics."
            )
            
            # Call centralized web search function
            fundamentals_text = ModelFactory.do_llm_call_with_websearch(
                model_selection=self.model_string,
                prompt=prompt,
                max_tokens=4096,
                temperature=0.3,
            )
            
            if not fundamentals_text:
                fundamentals_text = "Error: Could not retrieve fundamentals data."
            
            logger.debug(f"Received fundamentals overview for {symbol}: {len(fundamentals_text)} chars")
            
            # Build dict response (always build it for "both" format support)
            dict_response = {
                "symbol": symbol.upper(),
                "company_name": None,  # AI doesn't provide structured name
                "as_of_date": as_of_date.isoformat(),
                "data_date": as_of_date.isoformat(),
                "metrics": {
                    "content": fundamentals_text,
                    "source": f"AI Web Search ({self.model_string})",
                    "search_period": f"{start_str} to {end_str}"
                }
            }
            
            # Build markdown response
            lines = []
            lines.append(f"# Company Fundamentals Overview for {symbol}")
            lines.append(f"**As of Date:** {as_of_date.date()}")
            lines.append(f"**Search Period:** {start_str} to {end_str}")
            lines.append(f"**Source:** AI Web Search ({self.model_string})")
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
            logger.error(f"Failed to get company overview for {symbol} using {self.model_string}: {e}", exc_info=True)
            raise

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "ai"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_overview", "company_profile"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        return True  # No client to validate, centralized function handles it
    
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

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
                   REQUIRED - must be provided by caller.
        """
        super().__init__()
        
        if not model:
            raise ValueError("model parameter is required for OpenAICompanyOverviewProvider - no default fallback allowed")
        
        # Get OpenAI configuration
        self.backend_url = config.OPENAI_BACKEND_URL
        self.model = model
        self.default_lookback_days = 90
        
        # Get API key from database settings
        api_key = config.get_app_setting('openai_api_key')
        if not api_key:
            api_key = config.OPENAI_API_KEY or "dummy-key-not-used"
            logger.warning("OpenAI API key not found in database settings, using config or dummy key")
        
        # Initialize OpenAI client with web search capabilities
        self.client = OpenAI(
            base_url=self.backend_url,
            api_key=api_key
        )
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
            
            # Build prompt for fundamentals search
            prompt = f"Can you search for fundamental data and discussions on {symbol} from {start_str} to {end_str}. Make sure you only get the data posted during that period. List as a table with key metrics including: P/E ratio, P/S ratio, PEG ratio, EPS, dividend yield, market cap, revenue, profit margin, operating margin, ROE, ROA, cash flow, debt-to-equity, and any other relevant fundamental metrics."
            
            # Call OpenAI API with web search enabled
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": prompt,
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
                max_output_tokens=65535,
                top_p=1,
                store=True,
            )
            
            # Extract text from response using robust parsing
            fundamentals_text = ""
            
            # Try response.output_text first (simple accessor for full text)
            if hasattr(response, 'output_text') and response.output_text and isinstance(response.output_text, str) and len(response.output_text.strip()) > 0:
                fundamentals_text = response.output_text
                logger.debug(f"Extracted text via response.output_text: {len(fundamentals_text)} chars")
            # Fall back to iterating through output items (output_text is often empty, need to check output array)
            elif hasattr(response, 'output') and response.output:
                logger.debug(f"Iterating through {len(response.output)} output items")
                for item in response.output:
                    # Check for ResponseOutputMessage with content
                    if hasattr(item, 'content') and isinstance(item.content, list):
                        logger.debug(f"Found item with content list ({len(item.content)} items)")
                        for content_item in item.content:
                            if hasattr(content_item, 'text'):
                                text_value = str(content_item.text)
                                logger.debug(f"Found text in content item: {len(text_value)} chars")
                                fundamentals_text += text_value + "\n\n"
                fundamentals_text = fundamentals_text.strip()
                logger.debug(f"Extracted text via output iteration: {len(fundamentals_text)} chars")
            
            if not fundamentals_text:
                logger.error(f"Could not extract text from OpenAI response for {symbol}")
                logger.error(f"Response has output_text: {hasattr(response, 'output_text')}, type: {type(response.output_text) if hasattr(response, 'output_text') else 'N/A'}, value: {repr(response.output_text)[:200] if hasattr(response, 'output_text') else 'N/A'}")
                fundamentals_text = "Error: Could not extract fundamentals data from OpenAI response."
            
            logger.debug(f"Received fundamentals overview for {symbol}: {len(fundamentals_text)} chars")
            
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

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "openai"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_overview", "company_profile"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        return self.client is not None
    
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


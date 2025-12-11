"""
AI-powered instrument selector using LangChain/ModelFactory for unified model access.
Supports web search via provider-specific configurations.
"""

import json
from typing import List, Optional
from ..logger import logger
from .db import get_instance, get_db
from .models import AppSetting
from .. import config


class AIInstrumentSelector:
    """
    AI-powered instrument selector that uses LangChain via ModelFactory to generate curated lists 
    of trading instruments based on user-defined prompts and criteria.
    Supports web search capabilities through provider-specific model configurations.
    """

    def __init__(self, model_string: Optional[str] = None):
        """
        Initialize the AI instrument selector with LangChain via ModelFactory.
        
        Args:
            model_string: Model selection string in new format (e.g., "nagaai/gpt5" or "native/gpt5")
                         or legacy format (e.g., "NagaAI/gpt-5-2025-08-07" or "OpenAI/gpt-5")
                         REQUIRED - no default fallback
        """
        if not model_string:
            raise ValueError("model_string is required for AIInstrumentSelector - no default fallback allowed")
        
        self.model_string = model_string
        self.llm = None
        self.provider = None  # Resolved provider name
        
        self._initialize_llm()

    def _initialize_llm(self):
        """Initialize LangChain LLM using ModelFactory."""
        try:
            from .ModelFactory import ModelFactory
            
            # Get provider info from ModelFactory
            model_info = ModelFactory.get_model_info(self.model_string)
            self.provider = model_info.get('provider', 'openai').lower()
            
            # Create LLM using ModelFactory
            # Note: Web search requires special handling per provider
            self.llm = ModelFactory.create_llm(
                model_selection=self.model_string,
                temperature=1.0,  # Higher temperature for creative selection
            )
            
            logger.debug(f"LangChain LLM initialized successfully for {self.model_string}")
            
        except ValueError as e:
            # API key not configured or model not found
            logger.debug(f"Could not initialize LLM for {self.model_string}: {e}")
            self.llm = None
        except Exception as e:
            logger.error(f"Failed to initialize LangChain LLM for {self.model_string}: {e}")
            self.llm = None

    def _call_with_web_search(self, prompt: str) -> str:
        """
        Call LLM with web search enabled (provider-specific handling).
        
        For providers that support web search through LangChain, we pass
        web_search_options in the model_kwargs.
        """
        from langchain_core.messages import HumanMessage
        
        # For NagaAI/NagaAC, we can use web_search through bind method
        if self.provider in ["nagaai", "nagaac"]:
            # Bind web_search_options to the LLM call
            llm_with_search = self.llm.bind(
                web_search_options={}  # Empty dict enables web search
            )
            response = llm_with_search.invoke([HumanMessage(content=prompt)])
        else:
            # For other providers, call without web search
            # (OpenAI web_search_preview is only available via Responses API, not chat completions)
            response = self.llm.invoke([HumanMessage(content=prompt)])
        
        # Extract content from response
        if hasattr(response, 'content'):
            return response.content
        return str(response)

    def get_default_prompt(self) -> str:
        """
        Get the default prompt for AI instrument selection.
        
        Returns:
            str: Default prompt for financial instrument selection
        """
        return """You are a financial advisor specializing in stock analysis. Give me a list of 30 stock symbols that have medium/high risk and high profit potential.

REQUIREMENTS:
- Consider recent market trends and developments  
- Include a mix of different sectors for diversification
- Prioritize stocks with medium to high risk profiles (avoid penny stocks)
- Look for companies with strong fundamentals and growth potential
- Search the web to get latest updates

CRITICAL: You MUST respond with ONLY a valid JSON array of stock symbols. Do not include any explanations, commentary, or additional text.

EXAMPLE FORMAT (respond exactly like this):
["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "NFLX", "AMD", "CRM", "JPM", "JNJ", "PG", "KO", "DIS", "V", "MA", "UNH", "HD", "PFE"]

Your response:"""

    def select_instruments(self, prompt: Optional[str] = None, expert_instance_id: Optional[int] = None) -> Optional[List[str]]:
        """
        Use AI to select instruments based on the provided prompt.
        
        Args:
            prompt (Optional[str]): Custom prompt for instrument selection. 
                                  If None, uses default prompt.
            expert_instance_id (Optional[int]): ID of the expert instance triggering this selection (for logging)
        
        Returns:
            Optional[List[str]]: List of selected instrument symbols, or None if failed
        """
        if not self.llm:
            raise Exception(f"LLM not initialized for {self.model_string}. Please check your API key configuration.")

        try:
            # Use provided prompt or default
            selection_prompt = prompt if prompt else self.get_default_prompt()
            
            logger.info(f"Requesting AI instrument selection using model: {self.model_string}" + 
                       (f" (Expert: {expert_instance_id})" if expert_instance_id else ""))
            logger.debug(f"Using prompt: {selection_prompt}...")

            # Call LLM with web search if supported
            response_content = self._call_with_web_search(selection_prompt)
                
            if not response_content:
                logger.error("AI returned empty response")
                return None
                
            response_content = response_content.strip()
            logger.debug(f"AI response: {response_content}")

            # Check for empty response after stripping
            if not response_content:
                logger.error("AI returned empty response after stripping whitespace")
                return None

            # Parse JSON response
            try:
                # Handle markdown-wrapped JSON responses
                json_content = response_content
                if response_content.startswith("```json") and response_content.endswith("```"):
                    # Extract JSON from markdown code block
                    json_content = response_content[7:-3].strip()
                elif response_content.startswith("```") and response_content.endswith("```"):
                    # Extract from generic code block
                    json_content = response_content[3:-3].strip()
                
                instruments = json.loads(json_content)
                
                # Validate response format
                if not isinstance(instruments, list):
                    logger.error(f"AI response is not a list: {type(instruments)}")
                    return None
                
                # Get max_instruments setting from expert if available
                max_instruments = 50  # Default fallback
                if expert_instance_id:
                    try:
                        from .utils import get_expert_instance_from_id, get_setting_safe
                        expert = get_expert_instance_from_id(expert_instance_id)
                        if expert and expert.settings:
                            max_instruments = get_setting_safe(expert.settings, 'max_instruments', 50, int)
                            logger.debug(f"Using max_instruments setting: {max_instruments}")
                    except Exception as e:
                        logger.debug(f"Could not retrieve max_instruments setting: {e}")
                
                # Validate all items are strings (symbols)
                valid_instruments = []
                for item in instruments:
                    if isinstance(item, str) and len(item) > 0:
                        # Clean and validate symbol format
                        symbol = item.strip().upper()
                        if symbol.isalpha() and len(symbol) <= 10:  # Basic symbol validation
                            valid_instruments.append(symbol)
                        else:
                            logger.warning(f"Skipping invalid symbol format: {symbol}")
                    else:
                        logger.warning(f"Skipping non-string instrument: {item}")

                if not valid_instruments:
                    logger.error("No valid instruments found in AI response")
                    return None
                
                # Ensure uniqueness and apply length limit
                unique_instruments = list(dict.fromkeys(valid_instruments))[:max_instruments]

                logger.info(f"AI selected {len(unique_instruments)} instruments (max_instruments={max_instruments}): {unique_instruments}")
                return unique_instruments

            except json.JSONDecodeError as e:
                # Enhanced error logging with model and expert info
                error_msg = f"Failed to parse AI response as JSON: {e}"
                logger.error(error_msg)
                logger.error(f"Model: {self.model_string}")
                logger.error(f"Expert Instance ID: {expert_instance_id if expert_instance_id else 'Unknown'}")
                logger.error(f"Raw response: {response_content[:500]}")  # Truncate very long responses
                
                # Log to activity log for better tracking
                try:
                    from .db import get_instance
                    from .models import ExpertInstance
                    from .db import log_activity
                    from .types import ActivityLogSeverity, ActivityLogType
                    
                    if expert_instance_id:
                        expert = get_instance(ExpertInstance, expert_instance_id)
                        if expert:
                            log_activity(
                                severity=ActivityLogSeverity.ERROR,
                                activity_type=ActivityLogType.ANALYSIS_FAILED,
                                description=f"AI instrument selection failed: Invalid JSON response from {self.model_string}",
                                data={
                                    "model": self.model_string,
                                    "provider": self.provider,
                                    "response_snippet": response_content[:200],
                                    "error": str(e)
                                },
                                source_expert_id=expert_instance_id
                            )
                except Exception as log_error:
                    logger.warning(f"Could not log activity: {log_error}")
                
                # Try to extract symbols from text if JSON parsing failed
                return self._extract_symbols_from_text(response_content, expert_instance_id)

        except Exception as e:
            logger.error(f"Error during AI instrument selection with {self.model_string}" + 
                        (f" (Expert: {expert_instance_id})" if expert_instance_id else "") + 
                        f": {e}", exc_info=True)
            return None

    def _extract_symbols_from_text(self, text: str, expert_instance_id: Optional[int] = None) -> Optional[List[str]]:
        """
        Fallback method to extract symbols from text when JSON parsing fails.
        
        Args:
            text (str): Raw text response from AI
            expert_instance_id (Optional[int]): ID of the expert instance triggering this selection (for getting max_instruments setting)
            
        Returns:
            Optional[List[str]]: Extracted symbols or None if extraction failed
        """
        try:
            import re
            
            # Get max_instruments setting from expert if available
            max_instruments = 100  # Default fallback
            if expert_instance_id:
                try:
                    from .utils import get_expert_instance_from_id, get_setting_safe
                    expert = get_expert_instance_from_id(expert_instance_id)
                    if expert and expert.settings:
                        max_instruments = get_setting_safe(expert.settings, 'max_instruments', 100, int)
                        logger.debug(f"Using max_instruments setting: {max_instruments}")
                except Exception as e:
                    logger.debug(f"Could not retrieve max_instruments setting: {e}")
            
            # Look for patterns like stock symbols (2-5 uppercase letters)
            symbol_pattern = r'\b[A-Z]{2,5}\b'
            potential_symbols = re.findall(symbol_pattern, text)
            
            # Filter out common words that might match the pattern
            common_words = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'HAD', 'HAS', 'USE', 'GET', 'NEW', 'NOW', 'OLD', 'SEE', 'HIM', 'TWO', 'HOW', 'ITS', 'WHO', 'OIL', 'SIT', 'SET', 'RUN', 'EAT', 'FAR', 'SEA', 'EYE', 'AGE', 'TOP', 'WIN', 'YES', 'YET', 'BAD', 'BIG', 'BOY', 'DID', 'END', 'FEW', 'GOT', 'HIT', 'HOT', 'LAY', 'LET', 'MAN', 'MAP', 'MAY', 'MEN', 'MIX', 'ODD', 'OFF', 'PUT', 'RED', 'RUN', 'SAW', 'SAY', 'SUN', 'TAX', 'TRY', 'WAR', 'WAY', 'WHY', 'WIN'}
            
            valid_symbols = []
            for symbol in potential_symbols:
                if symbol not in common_words and len(symbol) >= 2:
                    valid_symbols.append(symbol)
            
            # Remove duplicates and limit to max_instruments
            unique_symbols = list(dict.fromkeys(valid_symbols))[:max_instruments]
            
            if unique_symbols:
                logger.info(f"Extracted {len(unique_symbols)} symbols from text (max_instruments={max_instruments}): {unique_symbols}")
                return unique_symbols
            else:
                logger.error("No valid symbols could be extracted from AI response text")
                return None
                
        except Exception as e:
            logger.error(f"Error extracting symbols from text: {e}", exc_info=True)
            return None

    def validate_instruments(self, instruments: List[str]) -> List[str]:
        """
        Validate and clean instrument symbols.
        
        Args:
            instruments (List[str]): List of instrument symbols to validate
            
        Returns:
            List[str]: List of validated and cleaned instrument symbols
        """
        validated = []
        
        for instrument in instruments:
            if not isinstance(instrument, str):
                logger.warning(f"Skipping non-string instrument: {instrument}")
                continue
                
            # Clean and validate symbol
            symbol = instrument.strip().upper()
            
            # Basic validation rules
            if (len(symbol) >= 1 and len(symbol) <= 10 and 
                symbol.replace('.', '').replace('-', '').isalnum()):
                validated.append(symbol)
            else:
                logger.warning(f"Skipping invalid symbol: {symbol}")
        
        return validated

    def test_connection(self) -> bool:
        """
        Test the AI API connection with a simple request.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        if not self.llm:
            return False
            
        try:
            from langchain_core.messages import HumanMessage
            
            # Simple test prompt
            response = self.llm.invoke([HumanMessage(content="Hello")])
            return bool(response)
        except Exception as e:
            logger.error(f"LLM connection test failed for {self.model_string}: {e}")
            return False


def get_ai_instrument_selector(model_string: Optional[str] = None) -> AIInstrumentSelector:
    """
    Factory function to get an AI instrument selector instance.
    
    Args:
        model_string: Model to use in format "Provider/ModelName" (e.g., "NagaAI/gpt-5-2025-08-07")
                     REQUIRED - no default fallback allowed
    
    Returns:
        AIInstrumentSelector: Configured AI instrument selector instance
    """
    return AIInstrumentSelector(model_string=model_string)
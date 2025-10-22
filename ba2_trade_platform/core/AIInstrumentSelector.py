"""
AI-powered instrument selector using OpenAI or NagaAI to dynamically select trading instruments.
Supports web search for both OpenAI (responses API) and NagaAI (chat completions API).
"""

import json
from typing import List, Optional
from openai import OpenAI
from ..logger import logger
from .db import get_instance, get_db
from .models import AppSetting
from .. import config


class AIInstrumentSelector:
    """
    AI-powered instrument selector that uses OpenAI or NagaAI to generate curated lists 
    of trading instruments based on user-defined prompts and criteria.
    Supports web search capabilities for real-time market data.
    """

    def __init__(self, model_string: Optional[str] = None):
        """
        Initialize the AI instrument selector with OpenAI client.
        
        Args:
            model_string: Model to use in format "Provider/ModelName" (e.g., "NagaAI/gpt-5-2025-08-07" or "OpenAI/gpt-5")
                         If None, uses config.OPENAI_MODEL
        """
        self.client = None
        self.model_string = model_string or config.OPENAI_MODEL
        self.provider = None  # "openai" or "nagaai"
        self.model = None     # Model name without provider prefix
        self.api_type = None  # "responses" (OpenAI) or "chat" (NagaAI)
        
        self._parse_model_string()
        self._initialize_client()

    def _parse_model_string(self):
        """Parse model string to extract provider and model name."""
        try:
            if "/" in self.model_string:
                provider_part, model_part = self.model_string.split("/", 1)
                self.provider = provider_part.lower()
                self.model = model_part
            else:
                # No provider specified, assume it's just a model name for OpenAI
                self.provider = "openai"
                self.model = self.model_string
            
            # Determine API type based on provider
            if self.provider == "openai":
                self.api_type = "responses"  # Use responses API with web_search_preview
            elif self.provider == "nagaai":
                self.api_type = "chat"  # Use chat completions API with web_search_options
            else:
                logger.warning(f"Unknown provider '{self.provider}' in model string '{self.model_string}', defaulting to OpenAI")
                self.provider = "openai"
                self.api_type = "responses"
            
            logger.debug(f"Parsed model string '{self.model_string}': provider={self.provider}, model={self.model}, api_type={self.api_type}")
            
        except Exception as e:
            logger.error(f"Error parsing model string '{self.model_string}': {e}")
            self.provider = "openai"
            self.model = "gpt-5"
            self.api_type = "responses"

    def _initialize_client(self):
        """Initialize OpenAI client with API key from database settings."""
        try:
            from sqlmodel import select
            session = get_db()
            
            # Get appropriate API key based on provider
            if self.provider == "openai":
                key_setting = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
                base_url = None
            elif self.provider == "nagaai":
                key_setting = session.exec(select(AppSetting).where(AppSetting.key == 'naga_ai_api_key')).first()
                base_url = "https://api.naga.ac/v1"
            else:
                key_setting = session.exec(select(AppSetting).where(AppSetting.key == 'openai_api_key')).first()
                base_url = None
            
            session.close()
            
            if not key_setting or not key_setting.value_str:
                logger.debug(f"{self.provider.upper()} API key not found in settings. AI selection will not be available.")
                return

            # Initialize client with appropriate configuration
            if base_url:
                self.client = OpenAI(api_key=key_setting.value_str, base_url=base_url)
            else:
                self.client = OpenAI(api_key=key_setting.value_str)
            
            logger.debug(f"OpenAI client initialized successfully for {self.provider}/{self.model}")

        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client for {self.provider}: {e}")
            self.client = None

    def _call_openai_responses_api(self, prompt: str) -> str:
        """Call OpenAI Responses API with web search."""
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
            max_output_tokens=4096,
            top_p=1,
            store=True,
        )
        
        # Extract text from response
        result_text = ""
        try:
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    if hasattr(item, 'content'):
                        if isinstance(item.content, list):
                            for content_item in item.content:
                                if hasattr(content_item, 'text'):
                                    result_text += content_item.text
                        elif hasattr(item.content, 'text'):
                            result_text += item.content.text
        except Exception as extract_error:
            logger.error(f"Error extracting text from OpenAI Responses API: {extract_error}")
            raise
        
        return result_text.strip()
    
    def _call_nagaai_chat_api(self, prompt: str) -> str:
        """Call NagaAI Chat Completions API with web_search_options."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            web_search_options={
                # Enable web search with default options
                # For Grok models, optionally add: "return_citations": True
            },
            temperature=1.0,
            max_tokens=4096,
            top_p=1.0
        )
        
        # Extract text from chat completion response
        try:
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                    return choice.message.content
        except Exception as extract_error:
            logger.error(f"Error extracting text from NagaAI Chat API: {extract_error}")
            raise
        
        return ""
    
    def _call_ai_api(self, prompt: str) -> str:
        """Call appropriate AI API based on provider type."""
        try:
            if self.api_type == 'responses':
                return self._call_openai_responses_api(prompt)
            else:  # chat
                return self._call_nagaai_chat_api(prompt)
        except Exception as e:
            logger.error(f"Error calling {self.api_type} API for {self.model_string}: {e}", exc_info=True)
            raise

    def get_default_prompt(self) -> str:
        """
        Get the default prompt for AI instrument selection.
        
        Returns:
            str: Default prompt for financial instrument selection
        """
        return """You are a financial advisor specializing in stock analysis. Give me a list of 20 stock symbols that have medium risk and high profit potential.

REQUIREMENTS:
- Focus on well-established companies with good liquidity
- Consider recent market trends and developments  
- Include a mix of different sectors for diversification
- Prioritize stocks with medium risk profiles (avoid penny stocks and highly volatile assets)
- Look for companies with strong fundamentals and growth potential

CRITICAL: You MUST respond with ONLY a valid JSON array of stock symbols. Do not include any explanations, commentary, or additional text.

EXAMPLE FORMAT (respond exactly like this):
["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "NFLX", "AMD", "CRM", "JPM", "JNJ", "PG", "KO", "DIS", "V", "MA", "UNH", "HD", "PFE"]

Your response:"""

    def select_instruments(self, prompt: Optional[str] = None) -> Optional[List[str]]:
        """
        Use AI to select instruments based on the provided prompt.
        
        Args:
            prompt (Optional[str]): Custom prompt for instrument selection. 
                                  If None, uses default prompt.
        
        Returns:
            Optional[List[str]]: List of selected instrument symbols, or None if failed
        """
        if not self.client:
            raise Exception(f"{self.provider.upper()} API key not configured. Please set up your API key in the application settings.")

        try:
            # Use provided prompt or default
            selection_prompt = prompt if prompt else self.get_default_prompt()
            
            logger.info(f"Requesting AI instrument selection using model: {self.model_string}")
            logger.debug(f"Using prompt: {selection_prompt[:200]}...")

            # Call appropriate API with web search enabled
            response_content = self._call_ai_api(selection_prompt)
                
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

                logger.info(f"AI selected {len(valid_instruments)} instruments: {valid_instruments}")
                return valid_instruments

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse AI response as JSON: {e}")
                logger.error(f"Raw response: {response_content}")
                
                # Try to extract symbols from text if JSON parsing failed
                return self._extract_symbols_from_text(response_content)

        except Exception as e:
            logger.error(f"Error during AI instrument selection with {self.model_string}: {e}")
            return None

    def _extract_symbols_from_text(self, text: str) -> Optional[List[str]]:
        """
        Fallback method to extract symbols from text when JSON parsing fails.
        
        Args:
            text (str): Raw text response from AI
            
        Returns:
            Optional[List[str]]: Extracted symbols or None if extraction failed
        """
        try:
            import re
            
            # Look for patterns like stock symbols (2-5 uppercase letters)
            symbol_pattern = r'\b[A-Z]{2,5}\b'
            potential_symbols = re.findall(symbol_pattern, text)
            
            # Filter out common words that might match the pattern
            common_words = {'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'HAD', 'HAS', 'USE', 'GET', 'NEW', 'NOW', 'OLD', 'SEE', 'HIM', 'TWO', 'HOW', 'ITS', 'WHO', 'OIL', 'SIT', 'SET', 'RUN', 'EAT', 'FAR', 'SEA', 'EYE', 'AGE', 'TOP', 'WIN', 'YES', 'YET', 'BAD', 'BIG', 'BOY', 'DID', 'END', 'FEW', 'GOT', 'HIT', 'HOT', 'LAY', 'LET', 'MAN', 'MAP', 'MAY', 'MEN', 'MIX', 'ODD', 'OFF', 'PUT', 'RED', 'RUN', 'SAW', 'SAY', 'SUN', 'TAX', 'TRY', 'WAR', 'WAY', 'WHY', 'WIN'}
            
            valid_symbols = []
            for symbol in potential_symbols:
                if symbol not in common_words and len(symbol) >= 2:
                    valid_symbols.append(symbol)
            
            # Remove duplicates and limit to reasonable number
            unique_symbols = list(dict.fromkeys(valid_symbols))[:20]
            
            if unique_symbols:
                logger.info(f"Extracted {len(unique_symbols)} symbols from text: {unique_symbols}")
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
        if not self.client:
            return False
            
        try:
            # Simple test prompt
            test_prompt = "Hello"
            
            # Call appropriate API
            if self.api_type == 'responses':
                response = self.client.responses.create(
                    model=self.model,
                    input=[
                        {
                            "role": "system",
                            "content": [{"type": "input_text", "text": test_prompt}]
                        }
                    ],
                    text={"format": {"type": "text"}},
                    max_output_tokens=10
                )
            else:  # chat
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": test_prompt}],
                    max_tokens=10
                )
            return True
        except Exception as e:
            logger.error(f"{self.provider.upper()} connection test failed: {e}")
            return False


def get_ai_instrument_selector(model_string: Optional[str] = None) -> AIInstrumentSelector:
    """
    Factory function to get an AI instrument selector instance.
    
    Args:
        model_string: Model to use in format "Provider/ModelName" (e.g., "NagaAI/gpt-5-2025-08-07")
                     If None, uses config.OPENAI_MODEL
    
    Returns:
        AIInstrumentSelector: Configured AI instrument selector instance
    """
    return AIInstrumentSelector(model_string=model_string)
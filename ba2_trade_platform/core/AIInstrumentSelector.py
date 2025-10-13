"""
AI-powered instrument selector using OpenAI to dynamically select trading instruments.
"""

import json
from typing import List, Optional
from openai import OpenAI
from ..logger import logger
from .db import get_instance
from .models import AppSetting


class AIInstrumentSelector:
    """
    AI-powered instrument selector that uses OpenAI to generate curated lists of trading instruments
    based on user-defined prompts and criteria.
    """

    def __init__(self):
        """Initialize the AI instrument selector with OpenAI client."""
        self.client = None
        self._initialize_openai_client()

    def _initialize_openai_client(self):
        """Initialize OpenAI client with API key from database settings."""
        try:
            # Get OpenAI API key from database
            openai_key_setting = get_instance(AppSetting, "openai_api_key")
            if not openai_key_setting or not openai_key_setting.value:
                logger.error("OpenAI API key not found in settings. Please configure it in the settings page.")
                return

            self.client = OpenAI(api_key=openai_key_setting.value)
            logger.debug("OpenAI client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
            self.client = None

    def get_default_prompt(self) -> str:
        """
        Get the default prompt for AI instrument selection.
        
        Returns:
            str: Default prompt for financial instrument selection
        """
        return """You are a financial advisor specializing in stock analysis. Search recent news on the web and give me a list of 20 symbols that you think have medium risk and high profit potential.

Please follow these guidelines:
- Focus on well-established companies with good liquidity
- Consider recent market trends and news developments
- Include a mix of different sectors for diversification
- Prioritize stocks with medium risk profiles (avoid penny stocks and highly volatile assets)
- Look for companies with strong fundamentals and growth potential

Return your response as a JSON array of stock symbols only, like this:
["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "NFLX", "AMD", "CRM"]

Do not include any explanations or additional text - just the JSON array of symbols."""

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
            logger.error("OpenAI client not initialized. Cannot select instruments.")
            return None

        try:
            # Use provided prompt or default
            selection_prompt = prompt if prompt else self.get_default_prompt()
            
            logger.info("Requesting AI instrument selection...")
            logger.debug(f"Using prompt: {selection_prompt[:200]}...")

            # Make request to OpenAI
            response = self.client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional financial advisor with expertise in stock market analysis and portfolio construction."
                    },
                    {
                        "role": "user", 
                        "content": selection_prompt
                    }
                ],
                temperature=0.7,
                max_tokens=1000
            )

            # Extract response content
            response_content = response.choices[0].message.content.strip()
            logger.debug(f"AI response: {response_content}")

            # Parse JSON response
            try:
                instruments = json.loads(response_content)
                
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
            logger.error(f"Error during AI instrument selection: {e}", exc_info=True)
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
        Test the OpenAI connection with a simple request.
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        if not self.client:
            return False
            
        try:
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=10
            )
            return True
        except Exception as e:
            logger.error(f"OpenAI connection test failed: {e}")
            return False


def get_ai_instrument_selector() -> AIInstrumentSelector:
    """
    Factory function to get an AI instrument selector instance.
    
    Returns:
        AIInstrumentSelector: Configured AI instrument selector instance
    """
    return AIInstrumentSelector()
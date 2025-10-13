#!/usr/bin/env python3
"""
Test script to demonstrate GPT-5 configuration for AIInstrumentSelector
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform import config
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.AIInstrumentSelector import AIInstrumentSelector

def test_gpt5_configuration():
    """Test that AIInstrumentSelector uses GPT-5 configuration correctly."""
    
    logger.info("🧪 Testing GPT-5 Configuration for AIInstrumentSelector")
    logger.info("=" * 60)
    
    # Load configuration
    config.load_config_from_env()
    
    # Display current configuration
    logger.info(f"📋 Current OpenAI Model Configuration: {config.OPENAI_MODEL}")
    
    # Initialize AI selector
    try:
        selector = AIInstrumentSelector()
        
        if selector.client:
            logger.info("✅ AIInstrumentSelector initialized successfully")
            
            # Test connection
            if selector.test_connection():
                logger.info("✅ GPT-5 connection test passed")
                
                # Show default prompt
                default_prompt = selector.get_default_prompt()
                logger.info(f"📝 Default prompt length: {len(default_prompt)} characters")
                logger.info(f"📝 Default prompt preview: {default_prompt[:100]}...")
                
            else:
                logger.warning("⚠️ GPT-5 connection test failed (API key might not be configured)")
        else:
            logger.info("ℹ️ OpenAI client not initialized (API key not configured)")
            
    except Exception as e:
        logger.error(f"❌ Error testing GPT-5 configuration: {e}")
    
    logger.info("=" * 60)
    logger.info("🏁 GPT-5 Configuration Test Complete")

if __name__ == "__main__":
    test_gpt5_configuration()
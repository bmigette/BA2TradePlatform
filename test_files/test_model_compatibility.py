#!/usr/bin/env python3
"""
Test script to demonstrate GPT-5 configuration and compatibility handling
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform import config
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.AIInstrumentSelector import AIInstrumentSelector

def test_model_compatibility():
    """Test different OpenAI models for compatibility."""
    
    logger.info("🧪 Testing OpenAI Model Compatibility")
    logger.info("=" * 60)
    
    # Load configuration
    config.load_config_from_env()
    
    models_to_test = [
        ("gpt-4", "GPT-4 (Stable)"),
        ("gpt-5", "GPT-5 (Latest)"), 
        ("gpt-3.5-turbo", "GPT-3.5-Turbo (Fast)")
    ]
    
    for model, description in models_to_test:
        logger.info(f"\n📋 Testing {description}: {model}")
        logger.info("-" * 40)
        
        # Temporarily set the model
        original_model = config.OPENAI_MODEL
        config.OPENAI_MODEL = model
        
        try:
            selector = AIInstrumentSelector()
            
            if selector.client:
                logger.info("✅ Client initialized successfully")
                
                # Test connection
                if selector.test_connection():
                    logger.info("✅ Connection test passed")
                    
                    # Test actual selection (limited to avoid API costs)
                    try:
                        short_prompt = """Give me 5 popular tech stock symbols as a JSON array.
Example: ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]
Your response:"""
                        
                        logger.info("🔄 Testing instrument selection...")
                        instruments = selector.select_instruments(prompt=short_prompt)
                        
                        if instruments:
                            logger.info(f"✅ Selection successful: {instruments}")
                        else:
                            logger.warning("⚠️ Selection returned no results")
                            
                    except Exception as selection_e:
                        logger.error(f"❌ Selection failed: {selection_e}")
                        
                else:
                    logger.warning("⚠️ Connection test failed")
            else:
                logger.info("ℹ️ Client not initialized (API key not configured)")
                
        except Exception as e:
            logger.error(f"❌ Model test failed: {e}")
        finally:
            # Restore original model
            config.OPENAI_MODEL = original_model
    
    logger.info("\n" + "=" * 60)
    logger.info("🏁 Model Compatibility Test Complete")
    logger.info(f"📋 Current Default Model: {config.OPENAI_MODEL}")
    logger.info(f"📋 Fallback Model: {getattr(config, 'OPENAI_FALLBACK_MODEL', 'Not configured')}")

if __name__ == "__main__":
    test_model_compatibility()
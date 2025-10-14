"""
Test script for OpenAI Company Overview Provider
Tests the fundamentals provider with increased max_output_tokens
"""

import sys
import os
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.modules.dataproviders.fundamentals.overview.OpenAICompanyOverviewProvider import OpenAICompanyOverviewProvider
from ba2_trade_platform import config
from ba2_trade_platform.logger import logger

print("=" * 80)
print("OpenAI Company Overview Provider Test")
print("Testing fundamentals provider with increased token limit")
print("=" * 80)

def test_provider():
    """Test the OpenAI company overview provider"""
    
    print("\n" + "=" * 80)
    print("Testing OpenAI Company Overview Provider")
    print("=" * 80)
    
    # Initialize provider
    print("\n1. Initializing provider...")
    provider = OpenAICompanyOverviewProvider()
    print(f"   ✓ Provider initialized: OpenAICompanyOverviewProvider")
    print(f"   ✓ Model: {provider.model}")
    print(f"   ✓ Backend URL: {provider.backend_url}")
    
    # Test parameters
    symbol = "AAPL"
    as_of_date = datetime.now()
    print(f"\n2. Test parameters:")
    print(f"   Symbol: {symbol}")
    print(f"   As of date: {as_of_date.date()}")
    
    # Call provider
    print(f"\n3. Calling provider.get_fundamentals_overview()...")
    print(f"   This may take 30-60 seconds...")
    
    try:
        result = provider.get_fundamentals_overview(symbol=symbol, as_of_date=as_of_date, format_type='both')
        
        print(f"\n4. Response received:")
        print(f"   Response type: {type(result)}")
        print(f"   Response keys: {list(result.keys())}")
        
        # Check text output
        if 'text' in result:
            text_content = result['text']
            print(f"\n5. Text output ({len(text_content)} chars):")
            print("   " + "-" * 76)
            # Print first 500 chars
            preview = text_content[:500] if len(text_content) > 500 else text_content
            print(preview)
            if len(text_content) > 500:
                print(f"   ... (truncated, total {len(text_content)} chars)")
            print("   " + "-" * 76)
        
        # Check data output
        if 'data' in result:
            data = result['data']
            print(f"\n6. Data output:")
            print(f"   Data keys: {list(data.keys())}")
            if 'content' in data:
                content = data['content']
                print(f"   Content type: {type(content)}")
                print(f"   Content length: {len(content)} chars")
                # Print first 200 chars of content
                preview = content[:200] if len(content) > 200 else content
                print(f"   Content preview: {preview}")
        
        print("\n" + "=" * 80)
        print("✓ TEST COMPLETED SUCCESSFULLY")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        print(f"\n✗ TEST FAILED")
        print(f"   Error: {e}")
        logger.error(f"Provider test failed: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    print()
    success = test_provider()
    
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"Provider Test: {'✓ PASSED' if success else '✗ FAILED'}")
    print("=" * 80)
    
    sys.exit(0 if success else 1)

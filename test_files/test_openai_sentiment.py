"""
Test OpenAI Social Media Sentiment Provider

This script tests the OpenAISocialMediaSentiment provider to debug
the response parsing issue where only "Reasoning(...)" is returned.
"""

import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.modules.dataproviders.socialmedia.OpenAISocialMediaSentiment import OpenAISocialMediaSentiment
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting

def test_openai_sentiment():
    """Test OpenAI social media sentiment provider with detailed debugging."""
    
    print("\n" + "="*80)
    print("Testing OpenAI Social Media Sentiment Provider")
    print("="*80)
    
    # Initialize provider
    print("\n1. Initializing provider...")
    provider = OpenAISocialMediaSentiment()
    print(f"   ✓ Provider initialized: {provider.get_provider_name()}")
    print(f"   ✓ Model: {provider.model}")
    print(f"   ✓ Backend URL: {provider.backend_url}")
    
    # Test parameters
    symbol = "AAPL"
    end_date = datetime.now()
    lookback_days = 7
    
    print(f"\n2. Test parameters:")
    print(f"   Symbol: {symbol}")
    print(f"   End date: {end_date.date()}")
    print(f"   Lookback days: {lookback_days}")
    
    # Call the provider
    print(f"\n3. Calling provider.get_social_media_sentiment()...")
    print("   This may take 30-60 seconds...\n")
    
    try:
        response = provider.get_social_media_sentiment(
            symbol=symbol,
            end_date=end_date,
            lookback_days=lookback_days,
            format_type="both"  # Get both text and data
        )
        
        print("\n4. Response received:")
        print(f"   Response type: {type(response)}")
        
        if isinstance(response, dict):
            print(f"   Response keys: {list(response.keys())}")
            
            if "text" in response:
                print(f"\n5. Text output ({len(response['text'])} chars):")
                print("   " + "-"*76)
                print(response['text'][:500])
                if len(response['text']) > 500:
                    print(f"   ... (truncated, total {len(response['text'])} chars)")
                print("   " + "-"*76)
            
            if "data" in response:
                print(f"\n6. Data output:")
                data = response['data']
                print(f"   Data keys: {list(data.keys())}")
                if 'content' in data:
                    content = data['content']
                    print(f"   Content type: {type(content)}")
                    print(f"   Content length: {len(str(content))} chars")
                    print(f"   Content preview: {str(content)[:200]}")
        else:
            print(f"\n5. Response content ({len(response)} chars):")
            print("   " + "-"*76)
            print(response[:500])
            if len(response) > 500:
                print(f"   ... (truncated, total {len(response)} chars)")
            print("   " + "-"*76)
        
        print("\n" + "="*80)
        print("✓ TEST COMPLETED SUCCESSFULLY")
        print("="*80)
        
        # Check if response is just "Reasoning(...)"
        response_str = str(response)
        if "Reasoning(" in response_str and len(response_str) < 300:
            print("\n⚠️  WARNING: Response appears to be just Reasoning object!")
            print("   This indicates the text extraction failed.")
            return False
        
        return True
        
    except Exception as e:
        print("\n" + "="*80)
        print("❌ TEST FAILED")
        print("="*80)
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_raw_api():
    """Test the raw OpenAI API to see the actual response structure."""
    
    print("\n" + "="*80)
    print("Testing Raw OpenAI API Response Structure")
    print("="*80)
    
    from openai import OpenAI
    from ba2_trade_platform import config
    
    # Get API key from database settings
    api_key = get_app_setting('openai_api_key')
    if not api_key:
        print("⚠️  WARNING: No OpenAI API key found in database settings")
        print("   Set 'openai_api_key' in app settings to enable this test")
        return False
    
    print(f"✓ Found API key in database settings (length: {len(api_key)} chars)")
    
    # OpenAI client with proper API key
    client = OpenAI(
        base_url=config.OPENAI_BACKEND_URL,
        api_key=api_key
    )
    
    prompt = """Search and analyze social media sentiment for AAPL from 2025-10-07 to 2025-10-14.
Provide a brief summary of the overall sentiment (bullish/bearish/neutral) and key themes."""
    
    print("\n1. Sending API request...")
    print(f"   Prompt: {prompt[:100]}...")
    
    try:
        response = client.responses.create(
            model="gpt-5-mini",
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
        
        print("\n2. Response received:")
        print(f"   Response type: {type(response)}")
        print(f"   Response attributes: {dir(response)}")
        
        if hasattr(response, 'output'):
            print(f"\n3. Response.output:")
            print(f"   Output type: {type(response.output)}")
            print(f"   Output length: {len(response.output) if response.output else 0}")
            
            if response.output:
                for i, item in enumerate(response.output):
                    print(f"\n   Output[{i}]:")
                    print(f"      Type: {type(item)}")
                    print(f"      Attributes: {dir(item)}")
                    
                    # Check for content attribute
                    if hasattr(item, 'content'):
                        print(f"      Has 'content' attribute: {type(item.content)}")
                        if isinstance(item.content, list):
                            print(f"         Content is list with {len(item.content)} items")
                            for j, content_item in enumerate(item.content):
                                print(f"         Content[{j}]: {type(content_item)}")
                                if hasattr(content_item, 'text'):
                                    print(f"            Has 'text': {str(content_item.text)[:100]}")
                        elif hasattr(item.content, 'text'):
                            print(f"         Content has 'text': {str(item.content.text)[:100]}")
                    
                    # Check for text attribute
                    if hasattr(item, 'text'):
                        print(f"      Has 'text' attribute: {str(item.text)[:100]}")
                    
                    # Check if it's a string
                    if isinstance(item, str):
                        print(f"      Is string: {item[:100]}")
                    
                    # Print string representation
                    print(f"      String repr: {str(item)[:100]}")
        
        if hasattr(response, 'reasoning'):
            print(f"\n4. Response.reasoning:")
            print(f"   Reasoning type: {type(response.reasoning)}")
            print(f"   Reasoning value: {response.reasoning}")
        
        print("\n" + "="*80)
        print("✓ RAW API TEST COMPLETED")
        print("="*80)
        
        return True
        
    except Exception as e:
        print("\n" + "="*80)
        print("❌ RAW API TEST FAILED")
        print("="*80)
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("OpenAI Social Media Sentiment Provider Test Suite")
    print("This will help diagnose the 'Reasoning(...)' output issue\n")
    
    # First test the raw API to understand response structure
    print("="*80)
    print("PART 1: Raw API Structure Test")
    print("="*80)
    raw_api_success = test_raw_api()
    
    # Then test the provider implementation
    print("\n\n")
    print("="*80)
    print("PART 2: Provider Implementation Test")
    print("="*80)
    provider_success = test_openai_sentiment()
    
    # Summary
    print("\n\n")
    print("="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"Raw API Test: {'✓ PASSED' if raw_api_success else '❌ FAILED'}")
    print(f"Provider Test: {'✓ PASSED' if provider_success else '❌ FAILED'}")
    print("="*80)

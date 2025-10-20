"""
Test Finnhub News Provider

This script demonstrates how to use the Finnhub News Provider.

Before running:
1. Set 'finnhub_api_key' in the AppSetting table
2. Ensure the virtual environment is activated

Run with:
    .venv\Scripts\python.exe test_files\test_finnhub_news.py  (Windows)
    .venv/bin/python test_files/test_finnhub_news.py          (Unix)
"""

import sys
import os
# Add parent directory to path so we can import ba2_trade_platform
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta
from ba2_trade_platform.config import load_config_from_env
load_config_from_env()  # Load configuration first
from ba2_trade_platform.modules.dataproviders import get_provider
from ba2_trade_platform.logger import logger


def test_company_news():
    """Test fetching company-specific news."""
    print("\n" + "="*80)
    print("Testing Finnhub Company News Provider")
    print("="*80 + "\n")
    
    try:
        # Get Finnhub news provider
        finnhub_news = get_provider("news", "finnhub")
        print("✓ Finnhub news provider initialized successfully\n")
        
        # Test parameters
        symbol = "AAPL"
        end_date = datetime.now()
        lookback_days = 7
        
        print(f"Fetching news for {symbol}")
        print(f"Period: Last {lookback_days} days")
        print(f"End date: {end_date.strftime('%Y-%m-%d')}\n")
        
        # Fetch news in markdown format
        news_markdown = finnhub_news.get_company_news(
            symbol=symbol,
            end_date=end_date,
            lookback_days=lookback_days,
            limit=5,  # Limit to 5 articles for testing
            format_type="markdown"
        )
        
        print("Company News (Markdown Format):")
        print("-" * 80)
        print(news_markdown)
        print("-" * 80 + "\n")
        
        # Fetch news in dict format
        news_dict = finnhub_news.get_company_news(
            symbol=symbol,
            end_date=end_date,
            lookback_days=lookback_days,
            limit=5,
            format_type="dict"
        )
        
        print("Company News (Dict Format):")
        print("-" * 80)
        print(f"Symbol: {news_dict['symbol']}")
        print(f"Start Date: {news_dict['start_date']}")
        print(f"End Date: {news_dict['end_date']}")
        print(f"Article Count: {news_dict['article_count']}")
        print("\nFirst Article:")
        if news_dict['articles']:
            first_article = news_dict['articles'][0]
            print(f"  Title: {first_article['title']}")
            print(f"  Source: {first_article['source']}")
            print(f"  Published: {first_article['published_at']}")
            print(f"  URL: {first_article['url']}")
        else:
            print("  No articles found")
        print("-" * 80 + "\n")
        
        # Test 'both' format
        news_both = finnhub_news.get_company_news(
            symbol=symbol,
            end_date=end_date,
            lookback_days=lookback_days,
            limit=3,
            format_type="both"
        )
        
        print("Company News (Both Format):")
        print("-" * 80)
        print(f"Keys in response: {list(news_both.keys())}")
        print(f"Article count from dict: {news_both['data']['article_count']}")
        print("-" * 80 + "\n")
        
        print("✓ Company news test completed successfully\n")
        return True
        
    except Exception as e:
        print(f"✗ Error testing company news: {e}")
        logger.error(f"Error in test_company_news: {e}", exc_info=True)
        return False


def test_global_news():
    """Test fetching global/market news."""
    print("\n" + "="*80)
    print("Testing Finnhub Global News Provider")
    print("="*80 + "\n")
    
    try:
        # Get Finnhub news provider
        finnhub_news = get_provider("news", "finnhub")
        print("✓ Finnhub news provider initialized successfully\n")
        
        # Test parameters
        end_date = datetime.now()
        lookback_days = 3
        
        print(f"Fetching global market news")
        print(f"Period: Last {lookback_days} days")
        print(f"End date: {end_date.strftime('%Y-%m-%d')}\n")
        
        # Fetch news in markdown format
        news_markdown = finnhub_news.get_global_news(
            end_date=end_date,
            lookback_days=lookback_days,
            limit=5,
            format_type="markdown"
        )
        
        print("Global News (Markdown Format):")
        print("-" * 80)
        print(news_markdown[:1000] + "..." if len(news_markdown) > 1000 else news_markdown)
        print("-" * 80 + "\n")
        
        # Fetch news in dict format
        news_dict = finnhub_news.get_global_news(
            end_date=end_date,
            lookback_days=lookback_days,
            limit=5,
            format_type="dict"
        )
        
        print("Global News (Dict Format):")
        print("-" * 80)
        print(f"Start Date: {news_dict['start_date']}")
        print(f"End Date: {news_dict['end_date']}")
        print(f"Article Count: {news_dict['article_count']}")
        print("\nFirst Article:")
        if news_dict['articles']:
            first_article = news_dict['articles'][0]
            print(f"  Title: {first_article['title']}")
            print(f"  Source: {first_article['source']}")
            print(f"  Published: {first_article['published_at']}")
            print(f"  Category: {first_article.get('category', 'N/A')}")
        else:
            print("  No articles found")
        print("-" * 80 + "\n")
        
        print("✓ Global news test completed successfully\n")
        return True
        
    except Exception as e:
        print(f"✗ Error testing global news: {e}")
        logger.error(f"Error in test_global_news: {e}", exc_info=True)
        return False


def test_provider_info():
    """Test provider metadata methods."""
    print("\n" + "="*80)
    print("Testing Finnhub Provider Info")
    print("="*80 + "\n")
    
    try:
        # Get Finnhub news provider
        finnhub_news = get_provider("news", "finnhub")
        
        print(f"Provider Name: {finnhub_news.get_provider_name()}")
        print(f"Supported Features: {finnhub_news.get_supported_features()}")
        print(f"Config Valid: {finnhub_news.validate_config()}")
        print()
        
        print("✓ Provider info test completed successfully\n")
        return True
        
    except Exception as e:
        print(f"✗ Error testing provider info: {e}")
        logger.error(f"Error in test_provider_info: {e}", exc_info=True)
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("FINNHUB NEWS PROVIDER TEST SUITE")
    print("="*80)
    
    results = []
    
    # Test provider info
    results.append(("Provider Info", test_provider_info()))
    
    # Test company news
    results.append(("Company News", test_company_news()))
    
    # Test global news
    results.append(("Global News", test_global_news()))
    
    # Print summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    for test_name, success in results:
        status = "✓ PASSED" if success else "✗ FAILED"
        print(f"{test_name}: {status}")
    print("="*80 + "\n")
    
    # Overall result
    all_passed = all(result[1] for result in results)
    if all_passed:
        print("✓ All tests passed!")
    else:
        print("✗ Some tests failed. Check logs for details.")
    
    return all_passed


if __name__ == "__main__":
    try:
        success = main()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        exit(130)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        logger.error(f"Fatal error in test suite: {e}", exc_info=True)
        exit(1)

#!/usr/bin/env python3
"""
Test script to verify FMP News API is working.

Usage:
    cd backend
    python test_fmp_news.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

# Load .env file
from dotenv import load_dotenv
load_dotenv()

def test_fmp_news():
    """Test FMP news provider for AAPL in 2025."""

    print("=" * 60)
    print("FMP News Provider Test")
    print("=" * 60)

    # Check if API key is set
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("ERROR: FMP_API_KEY not found in environment variables or .env file")
        print("Please set FMP_API_KEY in your .env file")
        return False

    print(f"API Key found: {api_key[:8]}...{api_key[-4:]}")

    try:
        from ba2_providers.news import FMPNewsProvider

        provider = FMPNewsProvider()
        print(f"Provider initialized: {provider.get_provider_name()}")
        print(f"Supported features: {provider.get_supported_features()}")

        # Test for AAPL in 2025
        symbol = "AAPL"
        start_date = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end_date = datetime(2025, 12, 31, tzinfo=timezone.utc)

        print(f"\nFetching news for {symbol}")
        print(f"Date range: {start_date.date()} to {end_date.date()}")
        print("-" * 40)

        result = provider.get_company_news(
            symbol=symbol,
            end_date=end_date,
            start_date=start_date,
            limit=100,
            format_type="dict"
        )

        if "error" in result:
            print(f"ERROR: {result['error']}")
            return False

        article_count = result.get("article_count", 0)
        articles = result.get("articles", [])

        print(f"\nResults:")
        print(f"  Total articles found: {article_count}")

        if articles:
            # Find date range of articles
            dates = []
            for article in articles:
                pub_date = article.get("published_at", "")
                if pub_date:
                    try:
                        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                        dates.append(dt)
                    except:
                        pass

            if dates:
                dates.sort()
                print(f"  First article: {dates[0].strftime('%Y-%m-%d %H:%M')}")
                print(f"  Last article: {dates[-1].strftime('%Y-%m-%d %H:%M')}")

            print(f"\nSample articles (first 5):")
            for i, article in enumerate(articles[:5], 1):
                title = article.get("title", "No title")[:60]
                source = article.get("source", "Unknown")
                pub_date = article.get("published_at", "Unknown")[:19]
                print(f"  {i}. [{pub_date}] {source}: {title}...")
        else:
            print("  No articles found in the specified date range")
            print("\n  Note: FMP API may have limited historical data")
            print("  Try a more recent date range if needed")

        print("\n" + "=" * 60)
        print("Test completed successfully!")
        return True

    except ValueError as e:
        print(f"Configuration Error: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_fmp_news()
    sys.exit(0 if success else 1)

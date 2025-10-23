"""
Test Web Content Extraction Tool for Trading Agents News Analyst

This script tests the new extract_web_content tool that has been added to the
Trading Agents news analyst agent.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.utils.web_content_extractor import (
    extract_urls_parallel,
    extract_single_url
)


def test_single_url_extraction():
    """Test extracting content from a single URL."""
    print("\n" + "="*80)
    print("TEST 1: Single URL Extraction")
    print("="*80 + "\n")
    
    url = "https://www.cnbc.com/2025/10/23/cnbc-daily-open-teslas-revenue-rose-but-so-did-its-operating-costs.html"
    
    print(f"Extracting: {url}\n")
    
    result = extract_single_url(url, timeout=10)
    
    if result["success"]:
        print(f"✓ SUCCESS")
        print(f"  URL: {result['url']}")
        print(f"  Text Length: {result['text_length']:,} chars")
        print(f"  Estimated Tokens: ~{result['estimated_tokens']:,}")
        print(f"  Duration: {result['duration']:.2f}s")
        print(f"\n  Preview (first 300 chars):")
        print(f"  {result['text'][:300]}...")
    else:
        print(f"✗ FAILED")
        print(f"  URL: {result['url']}")
        print(f"  Error: {result.get('error', 'Unknown error')}")


def test_parallel_extraction():
    """Test extracting content from multiple URLs in parallel."""
    print("\n\n" + "="*80)
    print("TEST 2: Parallel Multi-URL Extraction")
    print("="*80 + "\n")
    
    # Mix of URLs - some should work, some might be blocked
    urls = [
        "https://www.benzinga.com/markets/tech/25/10/48371214/iphone-air-has-fallen-short-of-expectations-apple-analyst-ming-chi-kuo-flags-limited-market-potential",
        "https://www.benzinga.com/markets/tech/25/10/48373338/gary-black-says-elon-musks-buzzwords-and-technical-jargon-did-little-to-boost-investor-confidence-following-tesla-earnings-call",
        "https://www.cnbc.com/2025/10/23/cnbc-daily-open-teslas-revenue-rose-but-so-did-its-operating-costs.html",
        "https://www.zacks.com/stock/news/2774470/msft-vs-aapl-which-mega-cap-tech-stock-is-the-better-buy-now",
    ]
    
    print(f"Extracting {len(urls)} URLs with max_tokens=50000\n")
    
    result = extract_urls_parallel(
        urls=urls,
        max_workers=3,
        max_tokens=50000,  # Limit to 50K tokens for test
        timeout=10
    )
    
    if result["success"]:
        print(f"✓ EXTRACTION COMPLETE\n")
        print(f"  Total URLs: {len(urls)}")
        print(f"  Successfully Extracted: {result['extracted_count']}")
        print(f"  Skipped/Failed: {result['skipped_count']}")
        print(f"  Total Tokens: ~{result['total_tokens']:,}")
        print(f"  Duration: {result['duration']:.2f}s")
        
        if result['urls_skipped']:
            print(f"\n  Skipped URLs:")
            for url in result['urls_skipped']:
                print(f"    - {url}")
        
        print(f"\n  Markdown Output (first 500 chars):")
        print(f"  {result['content_markdown'][:500]}...")
    else:
        print(f"✗ FAILED")
        print(f"  Error: {result.get('error', 'Unknown error')}")


def test_token_limit():
    """Test that token limit is enforced correctly."""
    print("\n\n" + "="*80)
    print("TEST 3: Token Limit Enforcement")
    print("="*80 + "\n")
    
    urls = [
        "https://www.benzinga.com/markets/tech/25/10/48371214/iphone-air-has-fallen-short-of-expectations-apple-analyst-ming-chi-kuo-flags-limited-market-potential",
        "https://www.benzinga.com/markets/tech/25/10/48373338/gary-black-says-elon-musks-buzzwords-and-technical-jargon-did-little-to-boost-investor-confidence-following-tesla-earnings-call",
        "https://www.cnbc.com/2025/10/23/cnbc-daily-open-teslas-revenue-rose-but-so-did-its-operating-costs.html",
    ]
    
    max_tokens = 2000  # Very low limit to force skipping
    
    print(f"Extracting {len(urls)} URLs with max_tokens={max_tokens} (forcing skip)\n")
    
    result = extract_urls_parallel(
        urls=urls,
        max_workers=3,
        max_tokens=max_tokens,
        timeout=10
    )
    
    if result["success"]:
        print(f"✓ TOKEN LIMIT TEST COMPLETE\n")
        print(f"  Total URLs: {len(urls)}")
        print(f"  Successfully Extracted: {result['extracted_count']}")
        print(f"  Skipped (Token Limit): {result['skipped_count']}")
        print(f"  Total Tokens: ~{result['total_tokens']:,} (limit: {max_tokens:,})")
        
        # Verify we didn't exceed limit
        if result['total_tokens'] <= max_tokens:
            print(f"\n  ✓ Token limit enforced correctly!")
        else:
            print(f"\n  ✗ Token limit exceeded! ({result['total_tokens']} > {max_tokens})")
        
        if result['urls_skipped']:
            print(f"\n  Skipped URLs ({len(result['urls_skipped'])}):")
            for url in result['urls_skipped'][:3]:  # Show first 3
                print(f"    - {url[:80]}...")


def test_markdown_output():
    """Test the markdown formatting of extracted content."""
    print("\n\n" + "="*80)
    print("TEST 4: Markdown Output Format")
    print("="*80 + "\n")
    
    urls = [
        "https://www.benzinga.com/markets/tech/25/10/48371214/iphone-air-has-fallen-short-of-expectations-apple-analyst-ming-chi-kuo-flags-limited-market-potential",
    ]
    
    result = extract_urls_parallel(
        urls=urls,
        max_workers=1,
        max_tokens=128000,
        timeout=10
    )
    
    if result["success"]:
        print(f"✓ MARKDOWN OUTPUT:\n")
        print(result['content_markdown'])


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("Web Content Extraction Tool - Test Suite")
    print("="*80)
    print("\nTesting the new extract_web_content tool for Trading Agents News Analyst\n")
    
    # Check if trafilatura is installed
    try:
        import trafilatura
        print(f"✓ trafilatura {trafilatura.__version__} is installed\n")
    except ImportError:
        print("✗ trafilatura is NOT installed")
        print("\nTo install: pip install trafilatura")
        print("Then run this test again.\n")
        return
    
    # Run tests
    test_single_url_extraction()
    test_parallel_extraction()
    test_token_limit()
    test_markdown_output()
    
    print("\n" + "="*80)
    print("All Tests Complete")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()

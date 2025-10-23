"""
Test Trafilatura Web Content Extraction with News Providers

This script tests the trafilatura library's ability to extract clean content
from news article URLs returned by FMP and Alpaca news providers.

The goal is to evaluate if trafilatura can efficiently extract the main article
content while filtering out ads, navigation, and other boilerplate, making it
suitable for LLM consumption with minimal token usage.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.modules.dataproviders import get_provider
from ba2_trade_platform.logger import logger


def extract_webpage_content(url: str, include_links: bool = False) -> Dict[str, Any]:
    """
    Extract main content from a webpage using trafilatura.
    
    Args:
        url: URL to fetch
        include_links: Whether to include links in output
        
    Returns:
        Dict with extracted content, metadata, and stats
    """
    try:
        import trafilatura
    except ImportError:
        print("ERROR: trafilatura not installed. Run: pip install trafilatura")
        return {
            "success": False,
            "error": "trafilatura not installed",
            "url": url
        }
    
    try:
        logger.info(f"Fetching: {url}")
        
        # Download the webpage
        downloaded = trafilatura.fetch_url(url)
        
        if not downloaded:
            return {
                "success": False,
                "error": "Failed to download webpage",
                "url": url
            }
        
        # Extract main content
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            include_links=include_links,
            output_format='txt',
            no_fallback=False
        )
        
        if not text:
            # Try with fallback
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=True,
                include_links=include_links,
                output_format='txt',
                no_fallback=True
            )
        
        if not text:
            return {
                "success": False,
                "error": "Could not extract content",
                "url": url,
                "html_length": len(downloaded)
            }
        
        # Calculate stats
        return {
            "success": True,
            "url": url,
            "extracted_text": text,
            "text_length": len(text),
            "html_length": len(downloaded),
            "compression_ratio": round(len(text) / len(downloaded) * 100, 2),
            "word_count": len(text.split()),
            "estimated_tokens": len(text) // 4  # Rough estimate: 1 token ≈ 4 chars
        }
        
    except Exception as e:
        logger.error(f"Error extracting content from {url}: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "url": url
        }


def test_provider_news(provider_name: str, symbol: str, num_articles: int = 5) -> List[Dict[str, Any]]:
    """
    Test trafilatura extraction on news URLs from a provider.
    
    Args:
        provider_name: 'fmp' or 'alpaca'
        symbol: Stock symbol to fetch news for
        num_articles: Number of articles to test
        
    Returns:
        List of extraction results
    """
    print(f"\n{'='*80}")
    print(f"Testing {provider_name.upper()} News Provider - {symbol}")
    print(f"{'='*80}\n")
    
    try:
        # Get news provider
        provider = get_provider("news", provider_name)
        
        if not provider:
            print(f"ERROR: Could not initialize {provider_name} provider")
            return []
        
        # Fetch news
        end_date = datetime.now()
        
        print(f"Fetching news for {symbol} (last 7 days)...")
        news_data = provider.get_company_news(
            symbol=symbol,
            end_date=end_date,
            lookback_days=7,
            limit=num_articles,
            format_type="dict"
        )
        
        if not news_data or not news_data.get("articles"):
            print(f"No news articles found for {symbol}")
            return []
        
        articles = news_data["articles"][:num_articles]
        print(f"Found {len(articles)} articles\n")
        
        # Extract content from each article URL
        results = []
        for i, article in enumerate(articles, 1):
            url = article.get("url")
            title = article.get("title", "No Title")
            
            if not url:
                print(f"[{i}/{len(articles)}] Skipping - No URL")
                continue
            
            print(f"\n[{i}/{len(articles)}] {title[:80]}...")
            print(f"  URL: {url}")
            
            # Extract content
            result = extract_webpage_content(url)
            result["article_title"] = title
            result["article_source"] = article.get("source", "Unknown")
            result["provider"] = provider_name
            
            if result["success"]:
                print(f"  ✓ Extracted: {result['text_length']:,} chars ({result['word_count']} words)")
                print(f"  ✓ HTML Size: {result['html_length']:,} chars")
                print(f"  ✓ Compression: {result['compression_ratio']}% of original HTML")
                print(f"  ✓ Est. Tokens: ~{result['estimated_tokens']:,}")
                
                # Show first 200 chars of extracted text
                preview = result["extracted_text"][:200].replace('\n', ' ')
                print(f"  Preview: {preview}...")
            else:
                print(f"  ✗ Failed: {result.get('error', 'Unknown error')}")
            
            results.append(result)
        
        return results
        
    except Exception as e:
        logger.error(f"Error testing {provider_name} provider: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return []


def print_summary(all_results: List[Dict[str, Any]]):
    """Print summary statistics."""
    print(f"\n\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}\n")
    
    successful = [r for r in all_results if r.get("success")]
    failed = [r for r in all_results if not r.get("success")]
    
    print(f"Total articles tested: {len(all_results)}")
    print(f"Successful extractions: {len(successful)} ({len(successful)/len(all_results)*100:.1f}%)")
    print(f"Failed extractions: {len(failed)} ({len(failed)/len(all_results)*100:.1f}%)")
    
    if successful:
        avg_text_length = sum(r["text_length"] for r in successful) / len(successful)
        avg_html_length = sum(r["html_length"] for r in successful) / len(successful)
        avg_compression = sum(r["compression_ratio"] for r in successful) / len(successful)
        avg_tokens = sum(r["estimated_tokens"] for r in successful) / len(successful)
        
        print(f"\nAverage Statistics (successful extractions):")
        print(f"  Extracted Text: {avg_text_length:,.0f} chars")
        print(f"  Original HTML: {avg_html_length:,.0f} chars")
        print(f"  Compression Ratio: {avg_compression:.1f}%")
        print(f"  Estimated Tokens: ~{avg_tokens:,.0f}")
    
    if failed:
        print(f"\nFailed Extractions:")
        for r in failed:
            print(f"  • {r['url']}")
            print(f"    Error: {r.get('error', 'Unknown')}")
    
    # Group by provider
    providers = {}
    for r in all_results:
        prov = r.get("provider", "unknown")
        if prov not in providers:
            providers[prov] = {"success": 0, "failed": 0}
        if r.get("success"):
            providers[prov]["success"] += 1
        else:
            providers[prov]["failed"] += 1
    
    print(f"\nResults by Provider:")
    for prov, stats in providers.items():
        total = stats["success"] + stats["failed"]
        success_rate = stats["success"] / total * 100 if total > 0 else 0
        print(f"  {prov.upper()}: {stats['success']}/{total} successful ({success_rate:.1f}%)")


def main():
    """Main test function."""
    print("\n" + "="*80)
    print("Trafilatura News Content Extraction Test")
    print("="*80)
    print("\nThis test evaluates trafilatura's ability to extract clean article content")
    print("from news URLs provided by FMP and Alpaca news providers.\n")
    
    # Check if trafilatura is installed
    try:
        import trafilatura
        print(f"✓ trafilatura version: {trafilatura.__version__}")
    except ImportError:
        print("✗ trafilatura not installed")
        print("\nTo install: pip install trafilatura")
        print("Then run this test again.")
        return
    
    all_results = []
    
    # Test symbols
    test_symbols = ["AAPL", "TSLA"]
    
    # Test FMP Provider
    print("\n" + "="*80)
    print("TESTING FMP NEWS PROVIDER")
    print("="*80)
    for symbol in test_symbols:
        results = test_provider_news("fmp", symbol, num_articles=3)
        all_results.extend(results)
    
    # Test Alpaca Provider
    print("\n" + "="*80)
    print("TESTING ALPACA NEWS PROVIDER")
    print("="*80)
    for symbol in test_symbols:
        results = test_provider_news("alpaca", symbol, num_articles=3)
        all_results.extend(results)
    
    # Print summary
    if all_results:
        print_summary(all_results)
    else:
        print("\nNo results to summarize")
    
    print("\n" + "="*80)
    print("Test Complete")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()

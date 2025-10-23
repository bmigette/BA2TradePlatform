"""
Web Content Extraction Utility for Trading Agents

Extracts clean article content from multiple URLs in parallel using trafilatura.
Designed for LLM consumption with automatic token limit management.
"""

from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

from ba2_trade_platform.logger import logger


def extract_single_url(url: str) -> Dict[str, Any]:
    """
    Extract content from a single URL.
    
    Args:
        url: URL to extract content from
        
    Returns:
        Dict with success status, url, text, and metadata
    """
    if not TRAFILATURA_AVAILABLE:
        return {
            "success": False,
            "url": url,
            "error": "trafilatura not installed",
            "text_length": 0,
            "estimated_tokens": 0
        }
    
    try:
        logger.debug(f"Extracting content from: {url}")
        start_time = time.time()
        
        # Fetch webpage (trafilatura.fetch_url doesn't accept timeout parameter)
        downloaded = trafilatura.fetch_url(url)
        
        if not downloaded:
            return {
                "success": False,
                "url": url,
                "error": "Failed to download webpage",
                "text_length": 0,
                "estimated_tokens": 0
            }
        
        # Extract main content
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            include_links=False,
            output_format='txt',
            no_fallback=False
        )
        
        # Try fallback if primary extraction failed
        if not text:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=True,
                include_links=False,
                output_format='txt',
                no_fallback=True
            )
        
        if not text or len(text) < 50:  # Minimum length check
            return {
                "success": False,
                "url": url,
                "error": "Extracted content too short or empty",
                "text_length": len(text) if text else 0,
                "estimated_tokens": 0
            }
        
        duration = time.time() - start_time
        estimated_tokens = len(text) // 4  # Rough estimate: 1 token ≈ 4 chars
        
        logger.info(f"Extracted {len(text)} chars (~{estimated_tokens} tokens) from {url} in {duration:.2f}s")
        
        return {
            "success": True,
            "url": url,
            "text": text,
            "text_length": len(text),
            "estimated_tokens": estimated_tokens,
            "duration": duration
        }
        
    except Exception as e:
        logger.error(f"Error extracting content from {url}: {e}")
        return {
            "success": False,
            "url": url,
            "error": str(e),
            "text_length": 0,
            "estimated_tokens": 0
        }


def extract_urls_parallel(
    urls: List[str],
    max_workers: int = 5,
    max_tokens: int = 128000
) -> Dict[str, Any]:
    """
    Extract content from multiple URLs in parallel with token limit management.
    
    Args:
        urls: List of URLs to extract content from
        max_workers: Maximum number of parallel threads
        max_tokens: Maximum total tokens to extract (stops when limit reached)
        
    Returns:
        Dict with extracted content, statistics, and skipped URLs
    """
    if not TRAFILATURA_AVAILABLE:
        return {
            "success": False,
            "error": "trafilatura not installed. Run: pip install trafilatura",
            "extracted_count": 0,
            "skipped_count": len(urls),
            "total_tokens": 0,
            "urls_extracted": [],
            "urls_skipped": urls,
            "content_markdown": "**Error**: trafilatura library not installed.\n\nTo install: `pip install trafilatura`"
        }
    
    if not urls:
        return {
            "success": True,
            "extracted_count": 0,
            "skipped_count": 0,
            "total_tokens": 0,
            "urls_extracted": [],
            "urls_skipped": [],
            "content_markdown": "*No URLs provided for extraction.*"
        }
    
    logger.info(f"Starting parallel extraction for {len(urls)} URLs (max_tokens={max_tokens}, max_workers={max_workers})")
    start_time = time.time()
    
    results = []
    total_tokens = 0
    urls_extracted = []
    urls_skipped = []
    
    # Process URLs in parallel with futures
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_url = {
            executor.submit(extract_single_url, url): url 
            for url in urls
        }
        
        # Process results as they complete
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            
            try:
                result = future.result()
                
                # Check if adding this result would exceed token limit
                if result["success"]:
                    new_total = total_tokens + result["estimated_tokens"]
                    
                    if new_total <= max_tokens:
                        # Add result
                        results.append(result)
                        total_tokens = new_total
                        urls_extracted.append(url)
                    else:
                        # Skip due to token limit
                        logger.warning(f"Skipping {url} - would exceed token limit ({new_total} > {max_tokens})")
                        urls_skipped.append(url)
                        result["skip_reason"] = "Token limit reached"
                        results.append(result)
                else:
                    # Failed extraction
                    urls_skipped.append(url)
                    results.append(result)
                    
            except Exception as e:
                logger.error(f"Error processing future for {url}: {e}")
                urls_skipped.append(url)
                results.append({
                    "success": False,
                    "url": url,
                    "error": f"Future processing error: {str(e)}",
                    "text_length": 0,
                    "estimated_tokens": 0
                })
    
    duration = time.time() - start_time
    
    # Build markdown output
    markdown_lines = []
    markdown_lines.append(f"# Web Content Extraction Results\n")
    markdown_lines.append(f"**Total URLs**: {len(urls)}")
    markdown_lines.append(f"**Successfully Extracted**: {len(urls_extracted)}")
    markdown_lines.append(f"**Skipped/Failed**: {len(urls_skipped)}")
    markdown_lines.append(f"**Total Tokens**: ~{total_tokens:,}")
    markdown_lines.append(f"**Extraction Time**: {duration:.2f}s\n")
    
    if urls_skipped:
        markdown_lines.append(f"## ⚠️ Skipped URLs ({len(urls_skipped)})\n")
        for result in results:
            if not result["success"] or result.get("skip_reason"):
                reason = result.get("skip_reason") or result.get("error", "Unknown error")
                markdown_lines.append(f"- **{result['url']}**: {reason}")
        markdown_lines.append("")
    
    markdown_lines.append("---\n")
    
    # Add extracted content
    for i, result in enumerate(results, 1):
        if result["success"] and not result.get("skip_reason"):
            markdown_lines.append(f"## Article {i}: {result['url']}\n")
            markdown_lines.append(f"**Tokens**: ~{result['estimated_tokens']:,} | **Length**: {result['text_length']:,} chars\n")
            markdown_lines.append(result["text"])
            markdown_lines.append("\n---\n")
    
    content_markdown = "\n".join(markdown_lines)
    
    logger.info(
        f"Extraction complete: {len(urls_extracted)}/{len(urls)} URLs extracted, "
        f"{total_tokens:,} total tokens in {duration:.2f}s"
    )
    
    return {
        "success": True,
        "extracted_count": len(urls_extracted),
        "skipped_count": len(urls_skipped),
        "total_tokens": total_tokens,
        "urls_extracted": urls_extracted,
        "urls_skipped": urls_skipped,
        "content_markdown": content_markdown,
        "duration": duration,
        "results": results  # Full results for debugging
    }

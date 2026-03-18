"""
News Article Enrichment Utility

Fetches full article content from URLs using trafilatura, with file-based caching
and context-aware content trimming for LLM consumption.
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from ba2_trade_platform.logger import logger

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

# Browser headers for web requests
BROWSER_HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.9',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'upgrade-insecure-requests': '1',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
}

CHARS_PER_TOKEN = 4  # Rough estimate: 1 token ~ 4 characters


def _get_default_cache_dir() -> Path:
    """Returns the default cache directory for news articles, creating it if needed."""
    cache_dir = Path.home() / "Documents" / "ba2_trade_platform" / "cache" / "news_articles"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _url_hash(url: str) -> str:
    """Returns SHA256 hex digest of the URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def _get_cached_content(url: str, cache_dir: Path) -> Optional[str]:
    """Retrieve cached article content for a URL, or None if not cached."""
    try:
        h = _url_hash(url)
        cache_file = cache_dir / h[:2] / f"{h}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return data["content"]
        return None
    except Exception:
        return None


def _cache_content(url: str, content: str, cache_dir: Path) -> None:
    """Cache article content to disk."""
    try:
        h = _url_hash(url)
        sub_dir = cache_dir / h[:2]
        sub_dir.mkdir(parents=True, exist_ok=True)
        cache_file = sub_dir / f"{h}.json"
        cache_file.write_text(
            json.dumps({"content": content, "cached_at": datetime.now(tz=timezone.utc).isoformat()}),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Failed to cache article content for {url}: {e}", exc_info=True)


def fetch_article_content(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch and extract the main text content from an article URL.

    Uses requests to download the page and trafilatura to extract the article text.

    Args:
        url: The article URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Extracted article text, or None if extraction failed.
    """
    if not TRAFILATURA_AVAILABLE:
        logger.warning("trafilatura is not installed - cannot fetch article content")
        return None

    try:
        response = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)
        if response.ok and response.text:
            extracted = trafilatura.extract(
                response.text, include_comments=False, include_tables=True
            )
            if extracted:
                return extracted
        return None
    except Exception as e:
        logger.debug(f"Failed to fetch article from {url}: {e}")
        return None


def enrich_articles(
    articles: List[Dict[str, Any]],
    max_workers: int = 8,
    cache_dir: Path = None
) -> List[Dict[str, Any]]:
    """Enrich a list of article dicts with full content fetched from their URLs.

    Each article dict should have a "url" key. After enrichment, articles will have
    a "full_content" key with the extracted article text (if successful).

    Args:
        articles: List of article dicts, each with at least a "url" key.
        max_workers: Maximum number of parallel fetch threads.
        cache_dir: Directory for the file cache. Uses default if None.

    Returns:
        The same articles list, mutated in place with "full_content" added where possible.
    """
    if cache_dir is None:
        cache_dir = _get_default_cache_dir()

    cached_count = 0
    fetched_count = 0
    failed_count = 0

    # Phase 1: Check cache
    for article in articles:
        if "url" not in article:
            continue
        cached = _get_cached_content(article["url"], cache_dir)
        if cached:
            article["full_content"] = cached
            cached_count += 1

    # Phase 2: Collect articles still needing fetch
    to_fetch = [a for a in articles if "url" in a and "full_content" not in a]

    # Phase 3: Fetch in parallel
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_article = {
                executor.submit(fetch_article_content, article["url"]): article
                for article in to_fetch
            }
            for future in as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    content = future.result()
                except Exception:
                    content = None

                if content:
                    # Only use full content if it's longer than the existing summary
                    existing_summary = article.get("summary", "")
                    if len(content) > len(existing_summary or ""):
                        article["full_content"] = content
                        _cache_content(article["url"], content, cache_dir)
                        fetched_count += 1
                    else:
                        failed_count += 1
                else:
                    failed_count += 1

    logger.info(
        f"News enrichment: {cached_count} cached, {fetched_count} fetched, "
        f"{failed_count} failed, {len(articles)} total"
    )

    return articles


def trim_articles_to_token_budget(articles: List[Dict[str, Any]], token_budget: int) -> None:
    """Trim article content so the total fits within a token budget.

    Uses even distribution: each article gets an equal share of the budget.
    Short articles that don't use their full share donate the remainder to
    longer articles in a second pass.

    Args:
        articles: List of article dicts with "full_content" and/or "summary" keys.
        token_budget: Maximum total tokens across all articles.
    """
    if not articles:
        return

    total_budget_chars = token_budget * CHARS_PER_TOKEN

    # Determine content source and length for each article
    article_info = []
    for article in articles:
        if "full_content" in article:
            content = article["full_content"]
            source_key = "full_content"
        elif "summary" in article:
            content = article.get("summary") or ""
            source_key = "summary"
        else:
            content = ""
            source_key = None
        article_info.append({
            "article": article,
            "content": content,
            "length": len(content),
            "source_key": source_key,
        })

    total_chars = sum(info["length"] for info in article_info)

    # No trimming needed if within budget
    if total_chars <= total_budget_chars:
        return

    max_chars_per_article = total_budget_chars // len(articles)

    # First pass: identify short articles (under budget) and long articles
    short_total = 0
    long_articles = []
    for info in article_info:
        if info["length"] <= max_chars_per_article:
            short_total += info["length"]
        else:
            long_articles.append(info)

    # Redistribute remaining budget among long articles
    if long_articles:
        remaining_budget = total_budget_chars - short_total
        per_long_article_budget = remaining_budget // len(long_articles)
    else:
        per_long_article_budget = max_chars_per_article

    # Truncate long articles
    for info in long_articles:
        if info["source_key"] is None:
            continue
        truncated = info["content"][:per_long_article_budget] + "\n[... truncated ...]"
        info["article"][info["source_key"]] = truncated

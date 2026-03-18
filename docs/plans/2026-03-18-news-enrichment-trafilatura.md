# News Enrichment with Trafilatura - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port the trafilatura-based news article enrichment from BA2MLTestPlatform into this system, enriching news articles with full content at the provider/toolkit level, with file-based caching and context-aware content trimming.

**Architecture:** News providers return article summaries with URLs. A new shared utility (`news_enrichment.py`) fetches full article content via trafilatura, caches results on disk (URL hash keyed), and provides a context-size-aware trimming function that distributes available tokens evenly across articles. The TradingAgents toolkit enriches articles inline (removing the `extract_web_content` LLM tool), and PennyMomentumTrader also uses enrichment in `_gather_news`. Model context sizes are added to `models_registry.py` to calculate available token budgets.

**Tech Stack:** trafilatura (already in requirements.txt), requests, hashlib (SHA256), concurrent.futures (ThreadPoolExecutor), existing ModelFactory/models_registry

---

## Task 1: Add `context_size` to Model Registry

**Files:**
- Modify: `ba2_trade_platform/core/models_registry.py`

**Step 1: Add DEFAULT_CONTEXT_SIZE constant and context_size field to models**

Add `DEFAULT_CONTEXT_SIZE = 128000` near the top (after label definitions).

Add `"context_size": <value>` to each model entry in MODELS dict. Known values:

| Model | Context Size |
|-------|-------------|
| gpt5 family | 128000 |
| gpt5.1 | 1000000 |
| gpt5.2 | 1000000 |
| gpt4o / gpt4o_mini | 128000 |
| o1 | 200000 |
| o1_mini | 128000 |
| o3_mini | 200000 |
| o4_mini | 200000 |
| grok4 / grok3 family | 131072 |
| qwen3_max | 128000 |
| qwen3_80b | 128000 |
| deepseek_v3.2 / deepseek_chat | 128000 |
| deepseek_reasoner | 128000 |
| kimi_k2 family | 131072 |
| kimi_k2.5 | 262144 |
| kimi_k1.5 | 131072 |
| gemini_3_flash | 1000000 |
| gemini_3_pro | 1000000 |
| gemini_2.5_pro | 1048576 |
| gemini_2.5_flash | 1048576 |
| gemini_2.0_flash | 1048576 |
| claude_opus_4_5 | 200000 |
| claude_4_opus | 200000 |
| claude_4_sonnet | 200000 |
| claude_3.5_sonnet | 200000 |
| claude_3.5_haiku | 200000 |
| llama3_3_70b | 128000 |
| llama3_2_90b_vision | 128000 |
| llama3_2_11b_vision | 128000 |
| llama3_1_405b | 128000 |
| llama3_1_70b | 128000 |
| llama3_1_8b | 128000 |
| mistral_large_2 | 128000 |
| mistral_small | 32000 |
| amazon_nova_pro | 300000 |
| amazon_nova_lite | 300000 |
| amazon_nova_micro | 128000 |
| amazon_titan_premier | 32000 |
| cohere_command_r_plus | 128000 |
| cohere_command_r | 128000 |

For any model not in the table, use `DEFAULT_CONTEXT_SIZE`.

**Step 2: Add `get_model_context_size()` helper function**

```python
def get_model_context_size(friendly_name: str) -> int:
    """
    Get the context window size (in tokens) for a model.

    Args:
        friendly_name: The friendly model name (e.g., "gpt5", "claude_4_sonnet")

    Returns:
        Context size in tokens. Returns DEFAULT_CONTEXT_SIZE for unknown models.
    """
    model_info = MODELS.get(friendly_name)
    if not model_info:
        return DEFAULT_CONTEXT_SIZE
    return model_info.get("context_size", DEFAULT_CONTEXT_SIZE)
```

**Step 3: Commit**

```bash
git add ba2_trade_platform/core/models_registry.py
git commit -m "feat: add context_size to model registry for token budget calculation"
```

---

## Task 2: Create News Enrichment Utility

**Files:**
- Create: `ba2_trade_platform/core/news_enrichment.py`

This utility provides three things:
1. **`fetch_article_content(url, timeout=15)`** - Fetch a single article via requests + trafilatura
2. **`enrich_articles(articles, max_workers=8, cache_dir=None)`** - Enrich a list of article dicts with full content, using parallel fetching and file cache
3. **`trim_articles_to_token_budget(articles, token_budget, title_key="title", content_key="full_content", summary_key="summary")`** - Evenly trim article content to fit within a token budget

### Implementation Details

**Constants:**
```python
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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
    """Get default cache directory for article content."""
    base = Path.home() / "Documents" / "ba2_trade_platform" / "cache" / "news_articles"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _url_hash(url: str) -> str:
    """SHA256 hash of URL for cache key."""
    return hashlib.sha256(url.encode()).hexdigest()
```

**`fetch_article_content(url, timeout=15)`**:
- Use `requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)`
- If response OK, extract text with `trafilatura.extract(response.text, include_comments=False, include_tables=True)`
- Return extracted text or None
- No Wayback Machine fallback (per user requirement)

**`_get_cached_content(url, cache_dir)`**:
- Compute hash, check if `{cache_dir}/{hash[:2]}/{hash}.json` exists
- If exists, load and return content string
- Otherwise return None

**`_cache_content(url, content, cache_dir)`**:
- Compute hash, write JSON `{"content": content, "cached_at": ISO timestamp}` to `{cache_dir}/{hash[:2]}/{hash}.json`
- Create subdirectory if needed

**`enrich_articles(articles, max_workers=8, cache_dir=None)`**:
- `cache_dir` defaults to `_get_default_cache_dir()`
- For each article with a `url` key:
  - Check cache first
  - If cached, set `article["full_content"]` from cache
  - Otherwise, queue for parallel fetch
- Fetch uncached articles in parallel using ThreadPoolExecutor
- For each fetched result:
  - If content obtained and longer than existing `summary`, set `full_content` and cache it
- Returns the articles list (mutated in place)

**`trim_articles_to_token_budget(articles, token_budget, ...)`**:
- Calculate total content tokens across all articles (using `full_content` if available, else `summary`)
- If total fits within budget, return as-is
- Otherwise, calculate `max_chars_per_article = (token_budget * CHARS_PER_TOKEN) // len(articles)`
- For each article, truncate content to `max_chars_per_article` with `[... truncated ...]` suffix
- Returns list of (title, trimmed_content) tuples for the caller to format

**Step: Commit**

```bash
git add ba2_trade_platform/core/news_enrichment.py
git commit -m "feat: add news enrichment utility with trafilatura and file cache"
```

---

## Task 3: Remove `extract_web_content` from TradingAgents

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py` (lines ~450-457, ~597-604)
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py` (lines 57-84)
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py` (lines 392-474)

### trading_graph.py changes:

1. Remove the `extract_web_content` tool function definition (lines ~450-457)
2. Remove `extract_web_content` from the "news" LoggingToolNode list (line ~601)

### prompts.py changes:

Replace `NEWS_ANALYST_SYSTEM_PROMPT` with a version that:
- Removes mention of `extract_web_content` tool
- Removes "Deep Analysis Workflow" section about extracting full articles
- Removes "Content Extraction Best Practices" section
- Instead mentions that **news articles are returned with full content already enriched** when available
- Tells the analyst to use the full content in articles for detailed analysis

New prompt:
```python
NEWS_ANALYST_SYSTEM_PROMPT = """You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics.

**Available Tools:**
- **get_company_news**: Get company-specific news with enriched full article content
- **get_global_news**: Get macroeconomic and market news with enriched full article content

**Analysis Approach:**
- News articles are returned with full content already extracted when available, enabling deep analysis without additional tool calls
- Analyze the complete articles (not just summaries) for nuanced insights, specific data points, and detailed context
- Full articles provide exact quotes, detailed analysis, and complete methodology
- Look at news from all configured providers to be comprehensive

Do not simply state the trends are mixed—provide detailed and fine-grained analysis and insights that may help traders make decisions.

Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
```

### agent_utils_new.py changes:

Remove the `extract_web_content` method from the `Toolkit` class (lines 392-474).

**Step: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py
git commit -m "refactor: remove extract_web_content tool from TradingAgents, news now pre-enriched"
```

---

## Task 4: Integrate Enrichment into TradingAgents Toolkit

**Files:**
- Modify: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`

### Changes to `get_company_news` method:

After aggregating markdown results from all providers, add enrichment:

1. Collect all article dicts from the `data_dict` results (already retrieved via `_call_provider_with_both_format`)
2. Call `enrich_articles(all_articles)` on the combined article list
3. Determine the LLM's context size from `self.provider_args` (add `"analyst_model"` to provider_args or pass context_size directly)
4. Calculate token budget: `context_size - estimated_prompt_tokens` (estimate prompt at ~3000 tokens for safety)
5. Call `trim_articles_to_token_budget(all_articles, token_budget)`
6. Rebuild the markdown output with enriched content (full article text where available instead of just summaries)
7. Return enriched markdown

**Key design decision**: The enrichment + trimming happens at the `get_company_news`/`get_global_news` level in the toolkit, so the LLM agent receives pre-enriched content. The token budget uses the analyst model's context size.

### Adding model context info to toolkit:

Add `"analyst_context_size"` to `provider_args` dict. This will be set by the TradingAgents expert when configuring the toolkit, based on the analyst LLM model's context size.

In `__init__`, store: `self.analyst_context_size = provider_args.get("analyst_context_size", 128000)`

### Method changes (pseudocode for get_company_news):

```python
def get_company_news(self, symbol, end_date, lookback_days=None):
    # ... existing provider aggregation code ...

    # Collect all article dicts for enrichment
    all_articles = []
    for data_dict in collected_data_dicts:
        if data_dict and "articles" in data_dict:
            all_articles.extend(data_dict["articles"])

    # Enrich articles with full content (trafilatura + cache)
    if all_articles:
        from ba2_trade_platform.core.news_enrichment import enrich_articles, trim_articles_to_token_budget
        enrich_articles(all_articles)

        # Calculate token budget for news content
        # Reserve tokens for system prompt + other tool results
        NEWS_PROMPT_RESERVE = 4000  # tokens for system/analyst prompt overhead
        token_budget = self.analyst_context_size - NEWS_PROMPT_RESERVE
        # Use ~40% of remaining context for news (other tools need space too)
        news_token_budget = int(token_budget * 0.4)

        trim_articles_to_token_budget(all_articles, news_token_budget)

    # Rebuild markdown with enriched content
    # ... format enriched articles into markdown ...
```

Apply identical changes to `get_global_news`.

### Changes to TradingAgents expert setup:

In the TradingAgents expert `__init__.py` where the toolkit is created, pass `analyst_context_size` in provider_args:

```python
from ba2_trade_platform.core.models_registry import get_model_context_size, parse_model_selection

# When building provider_args:
_, analyst_model_name = parse_model_selection(self.settings["analyst_llm"])
provider_args["analyst_context_size"] = get_model_context_size(analyst_model_name)
```

**Step: Commit**

```bash
git add ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py
git add ba2_trade_platform/modules/experts/TradingAgents/__init__.py  # or wherever toolkit is configured
git commit -m "feat: integrate news enrichment into TradingAgents toolkit with context-aware trimming"
```

---

## Task 5: Integrate Enrichment into PennyMomentumTrader

**Files:**
- Modify: `ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py`

### Changes to `_gather_news` method (line ~2558):

After aggregating news from all vendors, enrich the articles:

1. First collect articles in dict format (change `format_type="markdown"` to `format_type="both"` temporarily, or make a separate enrichment pass)
2. Better approach: After getting markdown news, also fetch in "dict" format to get URLs, enrich, and rebuild markdown with full content

**Simpler approach**: Since `_gather_news` returns markdown for the deep triage prompt, and the deep triage prompt already truncates sections to 6000 chars via `_clean_section()`:

1. Get articles in "both" format
2. Enrich articles with `enrich_articles()`
3. Rebuild markdown that includes full content (instead of just summary)
4. The existing `_clean_section(news, max_chars=6000)` in `build_deep_triage_prompt` will handle truncation

```python
def _gather_news(self, symbol: str) -> str:
    """Aggregate news from all configured news vendors, enriched with full article content."""
    vendor_list = self.get_setting_with_interface_default("vendor_news", log_warning=False)
    from ....modules.dataproviders import get_provider
    from ....core.news_enrichment import enrich_articles

    all_articles = []
    all_news_markdown: List[str] = []

    for vendor_name in vendor_list:
        try:
            kwargs = {}
            if vendor_name == "ai":
                kwargs["model"] = self.get_setting_with_interface_default("websearch_llm", log_warning=False)
            provider = get_provider("news", vendor_name, **kwargs)
            result = provider.get_company_news(
                symbol,
                end_date=datetime.now(timezone.utc),
                lookback_days=3,
                format_type="both",
            )
            if isinstance(result, dict) and "data" in result:
                articles = result["data"].get("articles", [])
                all_articles.extend(articles)
            elif isinstance(result, str):
                all_news_markdown.append(f"--- {vendor_name} ---\n{result}")
        except Exception as e:
            self.logger.warning(f"News provider {vendor_name} failed for {symbol}: {e}")

    # Enrich articles with full content
    if all_articles:
        enrich_articles(all_articles)

        # Build markdown from enriched articles
        for article in all_articles:
            title = article.get("title", "No Title")
            content = article.get("full_content") or article.get("summary", "")
            source = article.get("source", "")
            pub = article.get("published_at", "")
            url = article.get("url", "")
            md = f"### {title}\n**Source:** {source} | **Published:** {pub}\n\n{content}"
            if url:
                md += f"\n[Read more]({url})"
            all_news_markdown.append(md)

    return "\n\n---\n\n".join(all_news_markdown) if all_news_markdown else "No news data available."
```

**Step: Commit**

```bash
git add ba2_trade_platform/modules/experts/PennyMomentumTrader/__init__.py
git commit -m "feat: enrich PennyMomentumTrader news with full article content via trafilatura"
```

---

## Task 6: Write Tests

**Files:**
- Create: `tests/test_news_enrichment.py`

### Test Cases:

**1. `test_get_model_context_size_known_model`** - Verify known models return correct context size
**2. `test_get_model_context_size_unknown_model`** - Verify unknown model returns DEFAULT_CONTEXT_SIZE
**3. `test_url_hash`** - Verify consistent SHA256 hashing
**4. `test_cache_write_and_read`** - Write content to cache, read it back
**5. `test_cache_miss`** - Verify None returned for uncached URL
**6. `test_fetch_article_content_success`** - Mock requests + trafilatura, verify extraction
**7. `test_fetch_article_content_failure`** - Mock failed request, verify None returned
**8. `test_enrich_articles_uses_cache`** - Verify cached content is used without network call
**9. `test_enrich_articles_parallel_fetch`** - Verify uncached articles are fetched and cached
**10. `test_trim_articles_under_budget`** - Articles under budget returned as-is
**11. `test_trim_articles_over_budget_even_distribution`** - Verify even trimming across articles
**12. `test_trim_articles_preserves_short_articles`** - Short articles not padded, long ones trimmed more
**13. `test_trafilatura_not_available`** - Verify graceful degradation when trafilatura not installed

```python
"""Tests for news enrichment utility."""
import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from ba2_trade_platform.core.models_registry import get_model_context_size, DEFAULT_CONTEXT_SIZE
from ba2_trade_platform.core.news_enrichment import (
    _url_hash,
    _get_cached_content,
    _cache_content,
    fetch_article_content,
    enrich_articles,
    trim_articles_to_token_budget,
    CHARS_PER_TOKEN,
)


class TestModelContextSize:
    def test_known_model(self):
        size = get_model_context_size("gpt5")
        assert size == 128000

    def test_unknown_model(self):
        size = get_model_context_size("nonexistent_model_xyz")
        assert size == DEFAULT_CONTEXT_SIZE

    def test_gemini_large_context(self):
        size = get_model_context_size("gemini_2.5_pro")
        assert size == 1048576


class TestUrlHash:
    def test_consistent_hash(self):
        url = "https://example.com/article/123"
        assert _url_hash(url) == _url_hash(url)

    def test_different_urls_different_hashes(self):
        assert _url_hash("https://a.com") != _url_hash("https://b.com")


class TestCaching:
    def test_cache_roundtrip(self, tmp_path):
        url = "https://example.com/article"
        content = "This is the full article content."
        _cache_content(url, content, tmp_path)
        cached = _get_cached_content(url, tmp_path)
        assert cached == content

    def test_cache_miss(self, tmp_path):
        cached = _get_cached_content("https://nonexistent.com", tmp_path)
        assert cached is None


class TestFetchArticleContent:
    @patch("ba2_trade_platform.core.news_enrichment.requests.get")
    @patch("ba2_trade_platform.core.news_enrichment.trafilatura")
    def test_success(self, mock_traf, mock_get):
        mock_get.return_value = MagicMock(ok=True, text="<html>article</html>")
        mock_traf.extract.return_value = "Extracted article content here."
        result = fetch_article_content("https://example.com")
        assert result == "Extracted article content here."

    @patch("ba2_trade_platform.core.news_enrichment.requests.get")
    def test_request_failure(self, mock_get):
        mock_get.side_effect = Exception("Connection error")
        result = fetch_article_content("https://example.com")
        assert result is None


class TestEnrichArticles:
    def test_uses_cache(self, tmp_path):
        url = "https://example.com/cached"
        _cache_content(url, "Cached content", tmp_path)
        articles = [{"url": url, "summary": "Short"}]

        with patch("ba2_trade_platform.core.news_enrichment.fetch_article_content") as mock_fetch:
            enrich_articles(articles, cache_dir=tmp_path)
            mock_fetch.assert_not_called()

        assert articles[0]["full_content"] == "Cached content"

    @patch("ba2_trade_platform.core.news_enrichment.fetch_article_content")
    def test_fetches_and_caches(self, mock_fetch, tmp_path):
        mock_fetch.return_value = "Full article text from web"
        articles = [{"url": "https://example.com/new", "summary": "Short"}]

        enrich_articles(articles, cache_dir=tmp_path)

        assert articles[0]["full_content"] == "Full article text from web"
        # Verify it was cached
        cached = _get_cached_content("https://example.com/new", tmp_path)
        assert cached == "Full article text from web"


class TestTrimArticles:
    def test_under_budget_no_trim(self):
        articles = [
            {"title": "A", "full_content": "Short content", "summary": "s"},
            {"title": "B", "full_content": "Also short", "summary": "s"},
        ]
        # Budget of 100 tokens = 400 chars, way more than needed
        trim_articles_to_token_budget(articles, token_budget=100)
        assert articles[0]["full_content"] == "Short content"
        assert articles[1]["full_content"] == "Also short"

    def test_over_budget_even_distribution(self):
        long_text_a = "A" * 2000
        long_text_b = "B" * 2000
        articles = [
            {"title": "A", "full_content": long_text_a, "summary": "s"},
            {"title": "B", "full_content": long_text_b, "summary": "s"},
        ]
        # Budget of 500 tokens = 2000 chars total, 1000 per article
        trim_articles_to_token_budget(articles, token_budget=500)
        assert len(articles[0]["full_content"]) <= 1050  # ~1000 + truncation marker
        assert len(articles[1]["full_content"]) <= 1050

    def test_short_articles_not_padded(self):
        articles = [
            {"title": "Short", "full_content": "tiny", "summary": "s"},
            {"title": "Long", "full_content": "X" * 5000, "summary": "s"},
        ]
        # Budget of 500 tokens = 2000 chars
        trim_articles_to_token_budget(articles, token_budget=500)
        assert articles[0]["full_content"] == "tiny"  # Unchanged
        assert len(articles[1]["full_content"]) < 5000  # Trimmed
```

**Step: Run tests**

```bash
.venv\Scripts\python.exe -m pytest tests/test_news_enrichment.py -v
```

**Step: Commit**

```bash
git add tests/test_news_enrichment.py
git commit -m "test: add tests for news enrichment, caching, trimming, and model context sizes"
```

---

## Task 7: Delete web_content_extractor.py

**Files:**
- Delete: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/utils/web_content_extractor.py`

This file is no longer used since enrichment is now handled by `news_enrichment.py` at the provider level.

Verify no other imports reference it:
```bash
grep -r "web_content_extractor" ba2_trade_platform/
```

If clean, delete and commit:
```bash
git rm ba2_trade_platform/thirdparties/TradingAgents/tradingagents/utils/web_content_extractor.py
git commit -m "chore: remove unused web_content_extractor.py, replaced by core/news_enrichment.py"
```

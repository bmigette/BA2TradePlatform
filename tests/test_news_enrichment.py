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

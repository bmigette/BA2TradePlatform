"""
Interface for market news providers.

This interface defines methods for retrieving company-specific and global market news.
Includes built-in article enrichment via trafilatura (fetches full article content from URLs).
"""

from abc import abstractmethod
from typing import Dict, Any, List, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class MarketNewsInterface(DataProviderInterface):
    """
    Interface for market news providers.

    Providers implementing this interface supply news articles for companies
    and general market news.

    Built-in enrichment: Call ``enrich_news_result()`` on any dict-format result
    to fetch full article content via trafilatura. The ``get_company_news_enriched()``
    and ``get_global_news_enriched()`` convenience methods do this automatically.
    """

    # ------------------------------------------------------------------
    # Enrichment helpers (concrete — available to all providers)
    # ------------------------------------------------------------------

    @staticmethod
    def enrich_news_result(
        result: Dict[str, Any],
        token_budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Enrich a news result dict in-place: fetch full article content from
        URLs and optionally trim to a token budget.

        Args:
            result: A dict with an ``"articles"`` list (as returned by
                    ``get_company_news(format_type="dict")``).
            token_budget: If set, trim article content so the total fits
                          within this many tokens.

        Returns:
            The same *result* dict, mutated.
        """
        from ..news_enrichment import enrich_articles, trim_articles_to_token_budget

        articles = result.get("articles")
        if not articles:
            return result

        enrich_articles(articles)

        if token_budget is not None and token_budget > 0:
            trim_articles_to_token_budget(articles, token_budget)

        return result

    @staticmethod
    def rebuild_markdown_from_articles(
        articles: List[Dict[str, Any]],
        heading: str = "News",
    ) -> str:
        """
        Build a markdown string from a list of (possibly enriched) article dicts.

        Uses ``full_content`` when available, falling back to ``summary``.
        """
        parts = []
        for article in articles:
            title = article.get("title", "No Title")
            content = article.get("full_content") or article.get("summary", "No content available.")
            source = article.get("source", "Unknown")
            published = article.get("published_at", "")
            url = article.get("url", "")
            md = f"### {title}\n**Source:** {source} | **Published:** {published}\n\n{content}"
            if url:
                md += f"\n\n[Read more]({url})"
            parts.append(md)

        return f"# {heading}\n**Articles:** {len(articles)}\n\n" + "\n\n---\n\n".join(parts)

    def get_company_news_enriched(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        token_budget: Optional[int] = None,
    ) -> str:
        """
        Convenience wrapper: fetch company news, enrich with full article
        content, and return markdown.

        Args:
            symbol: Stock ticker.
            end_date: End date (inclusive).
            start_date: Start date (mutually exclusive with lookback_days).
            lookback_days: Days to look back (mutually exclusive with start_date).
            limit: Max articles.
            token_budget: Optional token limit for total article content.

        Returns:
            Enriched markdown string.
        """
        raw = self.get_company_news(
            symbol=symbol,
            end_date=end_date,
            start_date=start_date,
            lookback_days=lookback_days,
            limit=limit,
            format_type="dict",
        )
        if not isinstance(raw, dict):
            return raw  # provider returned markdown directly (e.g. AINewsProvider)

        self.enrich_news_result(raw, token_budget=token_budget)
        return self.rebuild_markdown_from_articles(
            raw.get("articles", []),
            heading=f"News for {symbol}",
        )

    def get_global_news_enriched(
        self,
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        token_budget: Optional[int] = None,
    ) -> str:
        """
        Convenience wrapper: fetch global news, enrich, and return markdown.
        """
        raw = self.get_global_news(
            end_date=end_date,
            start_date=start_date,
            lookback_days=lookback_days,
            limit=limit,
            format_type="dict",
        )
        if not isinstance(raw, dict):
            return raw

        self.enrich_news_result(raw, token_budget=token_budget)
        return self.rebuild_markdown_from_articles(
            raw.get("articles", []),
            heading="Global News",
        )

    # ------------------------------------------------------------------
    # Abstract methods (providers must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_company_news(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "article_count": int,
                "articles": [{
                    "title": str,
                    "summary": str,
                    "content": str (optional - full article text if available),
                    "source": str,
                    "author": str (optional),
                    "published_at": str (ISO format),
                    "url": str,
                    "image_url": str (optional),
                    "sentiment": str (optional - 'positive', 'negative', 'neutral'),
                    "sentiment_score": float (optional - -1.0 to 1.0),
                    "symbols": list[str] (optional - all symbols mentioned),
                    "tags": list[str] (optional - article tags/categories)
                }]
            }
            If format_type='markdown': Formatted markdown with article summaries
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_global_news(
        self,
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news (not specific to any company).
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "article_count": int,
                "articles": [{
                    "title": str,
                    "summary": str,
                    "content": str (optional - full article text if available),
                    "source": str,
                    "author": str (optional),
                    "published_at": str (ISO format),
                    "url": str,
                    "image_url": str (optional),
                    "sentiment": str (optional - 'positive', 'negative', 'neutral'),
                    "sentiment_score": float (optional - -1.0 to 1.0),
                    "symbols": list[str] (optional - symbols mentioned in article),
                    "tags": list[str] (optional - article tags/categories),
                    "category": str (optional - 'market', 'economy', 'earnings', etc.)
                }]
            }
            If format_type='markdown': Formatted markdown with article summaries
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass

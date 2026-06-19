"""
Local Files News Provider

Provides news from locally exported JSON files.
These files are typically exported from the Tools news testing feature.
"""

from typing import Dict, Any, Literal, Optional, List
from datetime import datetime, timezone
from pathlib import Path
import json

from ba2_common.core.interfaces import MarketNewsInterface
from ba2_common.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range,
)
from ba2_common.logger import logger

# Default directory for exported news files
DEFAULT_NEWS_EXPORTS_DIR = Path("news_exports")


class LocalFilesNewsProvider(MarketNewsInterface):
    """
    Local Files News Provider.

    Reads news articles from locally stored JSON files that were exported
    from the Tools news testing feature. Supports filtering by date range
    and symbol.
    """

    def __init__(self, files: Optional[List[str]] = None, source: str = "ba2_ml_platform"):
        """
        Initialize the Local Files News Provider.

        Args:
            files: List of JSON file paths to read from. If None, reads all files
                   from the default news_exports directory.
            source: Source identifier for tracking
        """
        super().__init__()
        self._files = files or []
        self._source = source
        self._articles_cache: Dict[str, List[Dict[str, Any]]] = {}
        logger.info(f"LocalFilesNewsProvider initialized with {len(self._files)} files")

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "localfiles"

    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_news", "global_news", "cached_sentiment"]

    def validate_config(self) -> bool:
        """
        Validate provider configuration.

        Returns:
            bool: True if at least one valid file is available
        """
        if self._files:
            return any(Path(f).exists() for f in self._files)
        return DEFAULT_NEWS_EXPORTS_DIR.exists()

    def _load_files(self) -> List[Dict[str, Any]]:
        """
        Load and parse all configured JSON files.

        Returns:
            List of all articles from all files
        """
        all_articles = []

        # Determine which files to load
        if self._files:
            files_to_load = [Path(f) for f in self._files]
        elif DEFAULT_NEWS_EXPORTS_DIR.exists():
            files_to_load = list(DEFAULT_NEWS_EXPORTS_DIR.glob("*.json"))
        else:
            files_to_load = []

        for filepath in files_to_load:
            if not filepath.exists():
                logger.warning(f"File not found: {filepath}")
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Validate format
                if "articles" not in data:
                    logger.warning(f"Invalid format in {filepath}: missing 'articles' key")
                    continue

                # Add metadata to each article
                file_symbol = data.get("symbol", "")
                file_provider = data.get("provider", "unknown")
                file_news_type = data.get("news_type", "company")  # Default to company for backward compat

                for article in data["articles"]:
                    article["_file_symbol"] = file_symbol
                    article["_original_provider"] = file_provider
                    article["_source_file"] = str(filepath)
                    article["_news_type"] = file_news_type
                    all_articles.append(article)

                logger.debug(f"Loaded {len(data['articles'])} articles from {filepath}")

            except json.JSONDecodeError as e:
                logger.error(f"JSON parse error in {filepath}: {e}")
            except Exception as e:
                logger.error(f"Error loading {filepath}: {e}")

        logger.info(f"Loaded {len(all_articles)} total articles from {len(files_to_load)} files")
        return all_articles

    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        """
        if isinstance(data, dict):
            return data
        return {"data": data}

    def _format_as_markdown(self, data: Dict[str, Any]) -> str:
        """
        Format news data as markdown.
        """
        lines = []

        if "symbol" in data:
            lines.append(f"# News for {data['symbol']}")
        else:
            lines.append("# News (Local Files)")

        lines.append(f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}")
        lines.append(f"**Articles:** {data['article_count']}")
        lines.append("")

        for i, article in enumerate(data.get("articles", []), 1):
            lines.append(f"## {i}. {article['title']}")
            lines.append(f"**Source:** {article.get('source', 'Unknown')}")

            if article.get('published_at'):
                pub_date = article['published_at'][:19].replace('T', ' ')
                lines.append(f"**Published:** {pub_date}")

            if article.get('sentiment'):
                lines.append(f"**Sentiment:** {article['sentiment']}")

            lines.append("")
            lines.append(article.get('summary', ''))

            if article.get('url'):
                lines.append(f"\n[Read more]({article['url']})")

            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def get_company_news(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company from local files.

        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles
            format_type: Output format ('dict', 'markdown', or 'both')

        Returns:
            News data in requested format
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")

        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=365)

        logger.info(
            f"Fetching local news for {symbol} from {start_date.date()} to {end_date.date()}"
        )

        # Load all articles from files
        all_articles = self._load_files()

        # Filter by symbol (case-insensitive)
        symbol_upper = symbol.upper()
        filtered = [
            a for a in all_articles
            if a.get("_file_symbol", "").upper() == symbol_upper
        ]

        # Filter by date range
        articles = []
        for article in filtered:
            pub_date_str = article.get("published_at", "")
            if not pub_date_str:
                continue

            try:
                # Parse date string
                if "T" in pub_date_str:
                    pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                else:
                    pub_date = datetime.strptime(pub_date_str[:10], "%Y-%m-%d")

                # Make timezone-naive for comparison
                if pub_date.tzinfo is not None:
                    pub_date = pub_date.replace(tzinfo=None)

                start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
                end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

                if start_naive <= pub_date <= end_naive:
                    articles.append(article)
            except (ValueError, TypeError) as e:
                logger.debug(f"Date parse error for article: {e}")
                continue

        # Apply limit
        articles = articles[:limit]

        result = {
            "symbol": symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "article_count": len(articles),
            "articles": articles
        }

        logger.info(f"Retrieved {len(articles)} news articles for {symbol} from local files")

        # Format output
        if format_type == "dict":
            return result
        elif format_type == "both":
            return {
                "text": self._format_as_markdown(result),
                "data": result
            }
        else:
            return self._format_as_markdown(result)

    def get_global_news(
        self,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news from local files.

        Only returns articles from files exported with news_type='global'.
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")

        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=365)

        logger.info(f"Fetching global news from local files: {start_date.date()} to {end_date.date()}")

        # Load all articles
        all_articles = self._load_files()

        # Filter for global news only (news_type == 'global' or symbol == 'global')
        global_articles = [
            a for a in all_articles
            if a.get("_news_type") == "global" or a.get("_file_symbol", "").lower() == "global"
        ]

        # Filter by date range
        articles = []
        for article in global_articles:
            pub_date_str = article.get("published_at", "")
            if not pub_date_str:
                continue

            try:
                if "T" in pub_date_str:
                    pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                else:
                    pub_date = datetime.strptime(pub_date_str[:10], "%Y-%m-%d")

                if pub_date.tzinfo is not None:
                    pub_date = pub_date.replace(tzinfo=None)

                start_naive = start_date.replace(tzinfo=None) if start_date.tzinfo else start_date
                end_naive = end_date.replace(tzinfo=None) if end_date.tzinfo else end_date

                if start_naive <= pub_date <= end_naive:
                    articles.append(article)
            except (ValueError, TypeError):
                continue

        articles = articles[:limit]

        result = {
            "news_type": "global",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "article_count": len(articles),
            "articles": articles
        }

        logger.info(f"Retrieved {len(articles)} global news articles from local files")

        if format_type == "dict":
            return result
        elif format_type == "both":
            return {
                "text": self._format_as_markdown(result),
                "data": result
            }
        else:
            return self._format_as_markdown(result)

    @staticmethod
    def list_available_exports() -> List[Dict[str, Any]]:
        """
        List all available export files.

        Returns:
            List of export file metadata
        """
        exports = []

        if DEFAULT_NEWS_EXPORTS_DIR.exists():
            for filepath in DEFAULT_NEWS_EXPORTS_DIR.glob("*.json"):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    exports.append({
                        "filename": filepath.name,
                        "filepath": str(filepath),
                        "symbol": data.get("symbol", ""),
                        "provider": data.get("provider", ""),
                        "article_count": data.get("article_count", 0),
                        "export_date": data.get("export_date", "")
                    })
                except Exception as e:
                    logger.warning(f"Error reading export file {filepath}: {e}")

        return exports

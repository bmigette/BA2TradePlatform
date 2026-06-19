"""
Sentiment Analysis Service

Provides news sentiment analysis using Transformers library with
financial sentiment models. Creates aggregated sentiment features
for ML model training.

Includes caching functionality to avoid redundant API calls and
sentiment re-analysis.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import logging
import os

logger = logging.getLogger(__name__)

# Import cache service
try:
    from app.services.news_cache import NewsCacheService
    CACHE_AVAILABLE = True
except ImportError:
    CACHE_AVAILABLE = False
    logger.warning("NewsCacheService not available, caching disabled")

# Try to import transformers
try:
    from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False
    logger.warning("Transformers library not available. Sentiment analysis will use fallback.")


class SentimentService:
    """
    Service for news sentiment analysis and feature creation.

    Uses Hugging Face Transformers with financial sentiment models like
    FinBERT or ProsusAI/finbert for analyzing news articles.

    Creates aggregated sentiment features like:
    - news_1d_positive_short: Count of positive short-term news in last day
    - news_1w_negative_long: Count of negative long-term news in last week
    """

    # Lookback periods for sentiment aggregation
    LOOKBACK_PERIODS = {
        '1d': 1,      # 1 day
        '1w': 7,      # 1 week
        '1m': 30,     # 1 month
        '6m': 180     # 6 months
    }

    # Sentiment categories
    SENTIMENT_CATEGORIES = ['positive', 'neutral', 'negative']

    # Default financial sentiment model
    DEFAULT_MODEL = 'ProsusAI/finbert'

    def __init__(self, model_name: str = None, use_cache: bool = True):
        """
        Initialize SentimentService.

        Args:
            model_name: Hugging Face model name for sentiment analysis
            use_cache: Whether to use news caching (default: True)
        """
        self.model_name = model_name or self.DEFAULT_MODEL
        self._pipeline = None
        self._initialized = False
        self.use_cache = use_cache and CACHE_AVAILABLE
        self._cache_service = None

        if self.use_cache:
            try:
                self._cache_service = NewsCacheService()
                logger.info("News cache service initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize cache service: {e}")
                self.use_cache = False

    def _initialize_pipeline(self):
        """Lazy initialization of the sentiment pipeline."""
        if self._initialized:
            return

        if not TRANSFORMERS_AVAILABLE:
            logger.warning("Transformers not available, using fallback sentiment")
            self._initialized = True
            return

        try:
            logger.info(f"Loading sentiment model: {self.model_name}")
            self._pipeline = pipeline(
                "sentiment-analysis",
                model=self.model_name,
                tokenizer=self.model_name,
                truncation=True,
                max_length=512
            )
            self._initialized = True
            logger.info("Sentiment pipeline initialized successfully")
        except Exception as e:
            logger.error(f"Failed to load sentiment model: {e}")
            self._initialized = True  # Mark as initialized to avoid retry

    def analyze_text(self, text: str) -> Dict[str, Any]:
        """
        Analyze sentiment of a text.

        Args:
            text: Text to analyze

        Returns:
            Dictionary with sentiment label, score, and probabilities
        """
        self._initialize_pipeline()

        if self._pipeline is None:
            # Fallback: simple keyword-based sentiment
            return self._fallback_sentiment(text)

        try:
            result = self._pipeline(text)[0]

            # Map FinBERT labels to standard format
            label = result['label'].lower()
            score = result['score']

            return {
                'label': label,
                'score': score,
                'positive_prob': score if label == 'positive' else 0.0,
                'neutral_prob': score if label == 'neutral' else 0.0,
                'negative_prob': score if label == 'negative' else 0.0
            }

        except Exception as e:
            logger.error(f"Sentiment analysis error: {e}")
            return self._fallback_sentiment(text)

    def _fallback_sentiment(self, text: str) -> Dict[str, Any]:
        """
        Simple keyword-based sentiment fallback.

        Args:
            text: Text to analyze

        Returns:
            Sentiment result dictionary
        """
        text_lower = text.lower()

        positive_words = ['up', 'rise', 'gain', 'bull', 'growth', 'profit', 'beat', 'surge', 'rally']
        negative_words = ['down', 'fall', 'loss', 'bear', 'decline', 'miss', 'crash', 'drop', 'plunge']

        positive_count = sum(1 for word in positive_words if word in text_lower)
        negative_count = sum(1 for word in negative_words if word in text_lower)

        if positive_count > negative_count:
            return {'label': 'positive', 'score': 0.6, 'positive_prob': 0.6, 'neutral_prob': 0.3, 'negative_prob': 0.1}
        elif negative_count > positive_count:
            return {'label': 'negative', 'score': 0.6, 'positive_prob': 0.1, 'neutral_prob': 0.3, 'negative_prob': 0.6}
        else:
            return {'label': 'neutral', 'score': 0.5, 'positive_prob': 0.25, 'neutral_prob': 0.5, 'negative_prob': 0.25}

    def analyze_news_articles(
        self,
        articles: List[Dict[str, Any]],
        provider: str = None,
        ticker: str = None,
        progress_callback: callable = None
    ) -> List[Dict[str, Any]]:
        """
        Analyze sentiment of multiple news articles.

        Uses cache to skip re-analysis of previously analyzed articles.

        Args:
            articles: List of article dicts with 'title', 'summary', 'content', 'date' keys
            provider: Optional provider name for cache updates
            ticker: Optional ticker for cache updates
            progress_callback: Optional callback(done, total) for progress

        Returns:
            List of articles with sentiment added
        """
        results = []
        analyzed_count = 0
        cached_count = 0
        sentiment_updates = []  # Collect updates for batch processing

        for i, article in enumerate(articles):
            url = article.get('url', '')

            # Check if already has sentiment (from cache or provider)
            if article.get('sentiment') and article.get('sentiment_score'):
                results.append(article)
                cached_count += 1
                continue

            # Check cache for existing sentiment
            if self.use_cache and self._cache_service and url:
                cached = self._cache_service.get_cached_article(url, provider or 'unknown')
                if cached and cached.get('sentiment'):
                    # Use cached sentiment
                    result = {
                        **article,
                        'sentiment': cached['sentiment'],
                        'sentiment_score': cached['sentiment_score'],
                        'positive_prob': cached.get('positive_prob', 0),
                        'neutral_prob': cached.get('neutral_prob', 0),
                        'negative_prob': cached.get('negative_prob', 0)
                    }
                    results.append(result)
                    cached_count += 1
                    continue

            # Combine title, summary, and content for analysis
            # FinBERT's tokenizer handles truncation at 512 tokens
            title = article.get('title', '')
            summary = article.get('summary', '')
            content = article.get('content', '')
            if summary:
                text = f"{title}. {summary} {content}"
            else:
                text = f"{title}. {content}"

            # Debug log: article content
            logger.debug(f"[Article {i+1}/{len(articles)}] Title: {title}")
            logger.debug(f"[Article {i+1}/{len(articles)}] Content preview: {content[:200]}...")

            sentiment = self.analyze_text(text)

            # Debug log: sentiment result
            logger.debug(
                f"[Article {i+1}/{len(articles)}] Sentiment: {sentiment['label']} "
                f"(score={sentiment['score']:.3f}, pos={sentiment['positive_prob']:.3f}, "
                f"neu={sentiment['neutral_prob']:.3f}, neg={sentiment['negative_prob']:.3f})"
            )

            result = {
                **article,
                'sentiment': sentiment['label'],
                'sentiment_score': sentiment['score'],
                'positive_prob': sentiment['positive_prob'],
                'neutral_prob': sentiment['neutral_prob'],
                'negative_prob': sentiment['negative_prob']
            }
            results.append(result)
            analyzed_count += 1

            # Collect cache update for batch processing
            if self.use_cache and self._cache_service and url:
                sentiment_updates.append((url, sentiment))

            if progress_callback and ((i + 1) % 10 == 0 or i + 1 == len(articles)):
                progress_callback(i + 1, len(articles))

        # Batch update sentiment in cache (reduces DB lock contention)
        if sentiment_updates and self._cache_service:
            self._cache_service.update_sentiment_batch(sentiment_updates)

        logger.info(f"Sentiment analysis: {analyzed_count} analyzed, {cached_count} from cache (total {len(results)})")
        return results

    def create_sentiment_features(
        self,
        ohlc_df: pd.DataFrame,
        news_articles: List[Dict[str, Any]],
        provider: str = None,
        ticker: str = None
    ) -> pd.DataFrame:
        """
        Create aggregated sentiment features for dataset.

        Creates features like:
        - news_count: Total news count for the day
        - news_1d_count: News count in last day
        - news_1d_positive: Positive news count in last day
        - news_1d_neutral: Neutral news count in last day
        - news_1d_negative: Negative news count in last day

        Args:
            ohlc_df: DataFrame with Date column
            news_articles: List of analyzed news articles with dates
            provider: Optional news provider name for caching
            ticker: Optional ticker symbol for caching

        Returns:
            DataFrame with sentiment features added
        """
        result_df = ohlc_df.copy()
        result_df['Date'] = pd.to_datetime(result_df['Date'])
        # Remove timezone info to avoid tz-aware/tz-naive mixing
        if result_df['Date'].dt.tz is not None:
            result_df['Date'] = result_df['Date'].dt.tz_localize(None)

        # Analyze articles that don't yet have sentiment stored
        if news_articles:
            needs_analysis = any(
                not a.get('sentiment') or not a.get('sentiment_score')
                for a in news_articles
            )
            if needs_analysis:
                news_articles = self.analyze_news_articles(news_articles, provider, ticker)

        # Convert to DataFrame for easier manipulation
        if news_articles:
            news_df = pd.DataFrame(news_articles)
            # Convert dates to timezone-naive datetime to avoid mixing issues
            # Use utc=True to handle mixed tz-aware/tz-naive values, then convert to naive
            try:
                news_df['date'] = pd.to_datetime(news_df['date'], utc=True).dt.tz_localize(None)
            except Exception:
                # Fallback: convert each date individually
                def to_naive_datetime(dt):
                    if dt is None:
                        return pd.NaT
                    if isinstance(dt, str):
                        try:
                            dt = pd.to_datetime(dt)
                        except Exception:
                            return pd.NaT
                    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
                        return dt.replace(tzinfo=None)
                    return dt
                news_df['date'] = news_df['date'].apply(to_naive_datetime)
        else:
            news_df = pd.DataFrame(columns=['date', 'sentiment'])

        # Create all sentiment feature columns
        feature_columns = ['news_count']  # Total news count for the day
        for period_name, period_days in self.LOOKBACK_PERIODS.items():
            # Add total count per period
            feature_columns.append(f'news_{period_name}_count')
            # Add sentiment counts per period (positive, neutral, negative)
            for sentiment in self.SENTIMENT_CATEGORIES:
                feature_columns.append(f'news_{period_name}_{sentiment}')

        # Initialize columns with zeros
        for col in feature_columns:
            result_df[col] = 0

        # Calculate features for each row
        for idx, row in result_df.iterrows():
            row_date = row['Date']
            row_date_only = row_date.date() if hasattr(row_date, 'date') else row_date

            # Count news for this exact day
            if len(news_df) > 0:
                day_mask = news_df['date'].dt.date == row_date_only
                result_df.at[idx, 'news_count'] = day_mask.sum()

            for period_name, period_days in self.LOOKBACK_PERIODS.items():
                start_date = row_date - timedelta(days=period_days)

                # Filter news in this lookback period
                mask = (news_df['date'] >= start_date) & (news_df['date'] <= row_date)
                period_news = news_df[mask] if len(news_df) > 0 else pd.DataFrame()

                # Total count for this period
                result_df.at[idx, f'news_{period_name}_count'] = len(period_news)

                for sentiment in self.SENTIMENT_CATEGORIES:
                    # Count by sentiment
                    if len(period_news) > 0:
                        sentiment_count = len(period_news[period_news['sentiment'] == sentiment])
                        result_df.at[idx, f'news_{period_name}_{sentiment}'] = sentiment_count

        logger.info(f"Created {len(feature_columns)} sentiment features")
        return result_df

    def _generate_monthly_chunks(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> List[Tuple[datetime, datetime]]:
        """
        Split a date range into monthly chunks.

        Args:
            start_date: Start date
            end_date: End date

        Returns:
            List of (chunk_start, chunk_end) tuples
        """
        chunks = []
        current_start = start_date

        while current_start < end_date:
            # Calculate end of current month
            if current_start.month == 12:
                next_month_start = current_start.replace(year=current_start.year + 1, month=1, day=1)
            else:
                next_month_start = current_start.replace(month=current_start.month + 1, day=1)

            # Chunk end is min of next month start and end_date
            chunk_end = min(next_month_start, end_date)
            chunks.append((current_start, chunk_end))
            current_start = next_month_start

        return chunks

    def fetch_news_for_ticker(
        self,
        ticker: str,
        start_date: datetime,
        end_date: datetime,
        provider: str = "fmp",
        enrich_content: bool = True,
        limit: int = None,  # None means no limit, fetch all
        use_cache: bool = True,
        progress_callback: callable = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch news articles for a ticker in date range using real news providers.

        For date ranges longer than 1 month, fetches month by month to avoid
        API rate limits and get complete coverage.

        Checks cache first and only fetches articles not already cached.

        Args:
            ticker: Stock ticker symbol
            start_date: Start date
            end_date: End date
            provider: News provider to use ('fmp', 'alphavantage', 'finnhub', 'alpaca')
            enrich_content: Whether to fetch full article content for short summaries
            limit: Maximum number of articles to fetch per month (None = unlimited)
            use_cache: Whether to use cached articles (default: True)
            progress_callback: Optional callback(phase, progress_pct, message) for progress

        Returns:
            List of news articles with title, content, date, source

        Raises:
            ValueError: If provider is not available or unknown
            Exception: If news fetching fails (no fallback to mock data)
        """
        logger.info(f"Fetching news for {ticker} from {start_date} to {end_date} using {provider}")

        # Get the news provider - fail if not available
        news_provider = self._get_news_provider(provider)
        if news_provider is None:
            raise ValueError(f"News provider '{provider}' is not available or not configured")

        # Split into monthly chunks to avoid API limits and get complete data
        monthly_chunks = self._generate_monthly_chunks(start_date, end_date)
        logger.info(f"Fetching news in {len(monthly_chunks)} monthly chunks")

        all_raw_articles = []
        seen_urls = set()

        total_chunks = len(monthly_chunks)
        for chunk_idx, (chunk_start, chunk_end) in enumerate(monthly_chunks):
            try:
                logger.debug(f"Fetching chunk: {chunk_start.date()} to {chunk_end.date()}")

                if progress_callback:
                    pct = (chunk_idx / total_chunks) * 70  # fetching = 0-70%
                    progress_callback('fetch', pct,
                                      f"Fetching {chunk_start.strftime('%Y-%m')} ({chunk_idx+1}/{total_chunks}, {len(all_raw_articles)} articles so far)")

                # Fetch news for this chunk
                result = news_provider.get_company_news(
                    symbol=ticker,
                    end_date=chunk_end,
                    start_date=chunk_start,
                    limit=limit or 1000,  # High limit per chunk if no limit specified
                    format_type="dict"
                )

                # Check for error response
                if isinstance(result, dict) and "error" in result:
                    logger.warning(f"Error fetching chunk {chunk_start.date()}-{chunk_end.date()}: {result['error']}")
                    continue

                chunk_articles = result.get("articles", [])
                logger.debug(f"Got {len(chunk_articles)} articles for chunk {chunk_start.date()}-{chunk_end.date()}")

                # Deduplicate by URL
                for article in chunk_articles:
                    url = article.get('url', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_raw_articles.append(article)
                    elif not url:
                        # Articles without URL - dedupe by title
                        title = article.get('title', '')
                        if title and title not in seen_urls:
                            seen_urls.add(title)
                            all_raw_articles.append(article)

            except Exception as e:
                logger.warning(f"Error fetching chunk {chunk_start.date()}-{chunk_end.date()}: {e}")
                continue

        raw_articles = all_raw_articles
        new_articles_count = len(raw_articles)
        logger.info(f"Received {new_articles_count} articles from {provider}")

        # Pre-populate articles with cached content to skip re-fetching
        if self._cache_service and new_articles_count > 0:
            urls = [a.get('url', '') for a in raw_articles]
            cached_content = self._cache_service.get_cached_content_for_urls(urls)
            if cached_content:
                populated = 0
                for article in raw_articles:
                    url = article.get('url', '')
                    if url in cached_content:
                        article['full_content'] = cached_content[url]['content']
                        article['content_fetched'] = True
                        populated += 1
                logger.info(f"Pre-populated {populated}/{new_articles_count} articles with cached content")

        # Enrich articles with short summaries using trafilatura
        if enrich_content and new_articles_count > 0 and hasattr(news_provider, 'enrich_articles_with_content'):
            needs_fetch = sum(1 for a in raw_articles if not a.get('content_fetched'))
            if progress_callback:
                progress_callback('enrich', 70, f"Enriching {needs_fetch} articles with content ({new_articles_count - needs_fetch} already cached)...")
            logger.info(f"Enriching {needs_fetch} articles with URL content via trafilatura ({new_articles_count - needs_fetch} already cached)")

            def _enrich_progress(done, total):
                if progress_callback:
                    pct = 70 + (done / max(total, 1)) * 20  # enrich = 70-90%
                    progress_callback('enrich', pct, f"Enriching articles: {done}/{total}")

            raw_articles = news_provider.enrich_articles_with_content(
                raw_articles,
                max_workers=5,
                min_summary_length=100,
                progress_callback=_enrich_progress
            )

        # Convert to standard format
        articles = []
        for article in raw_articles:
            pub_date = article.get("published_at", "")
            # Parse date string to datetime if needed
            if isinstance(pub_date, str) and pub_date:
                try:
                    pub_date = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                except ValueError:
                    pub_date = start_date

            standard_article = {
                'title': article.get('title', ''),
                'summary': article.get('summary', ''),
                'content': article.get('full_content', article.get('summary', article.get('snippet', ''))),
                'date': pub_date,
                'source': article.get('source', provider.upper()),
                'url': article.get('url', ''),
                'content_fetched': article.get('content_fetched', False),
                # Preserve provider's built-in sentiment if available (e.g., from Alpha Vantage)
                'sentiment': article.get('sentiment'),
                'sentiment_score': article.get('sentiment_score')
            }
            articles.append(standard_article)

        # Cache articles in batch (reduces DB lock contention)
        if use_cache and self.use_cache and self._cache_service and articles:
            if progress_callback:
                progress_callback('cache', 90, f"Caching {len(articles)} articles...")
            cached_count, _ = self._cache_service.cache_articles_batch(articles, provider, ticker)
            logger.debug(f"Batch cached {cached_count} articles for {ticker}")

        logger.info(f"Total: {len(articles)} articles for {ticker}")
        return articles

    def fetch_global_news(
        self,
        start_date: datetime,
        end_date: datetime,
        provider: str = "fmp",
        limit: int = 500
    ) -> List[Dict[str, Any]]:
        """
        Fetch global/market news (not ticker-specific) from a provider.

        Args:
            start_date: Start date
            end_date: End date
            provider: News provider to use (must support global news)
            limit: Maximum number of articles to fetch

        Returns:
            List of news articles

        Raises:
            ValueError: If provider doesn't support global news
        """
        logger.info(f"Fetching global news from {start_date} to {end_date} using {provider} (limit={limit})")

        # Get the news provider
        news_provider = self._get_news_provider(provider)
        if news_provider is None:
            raise ValueError(f"News provider '{provider}' is not available or not configured")

        # Check if provider supports global news
        supported_features = news_provider.get_supported_features()
        if 'global_news' not in supported_features:
            # List providers that do support global news
            global_providers = ['fmp', 'finnhub', 'alpaca', 'localfiles']
            raise ValueError(
                f"Provider '{provider}' does not support global news. "
                f"Providers with global news support: {', '.join(global_providers)}"
            )

        # Fetch global news
        result = news_provider.get_global_news(
            end_date=end_date,
            start_date=start_date,
            limit=limit,
            format_type="dict"
        )

        # Check for error response
        if isinstance(result, dict) and "error" in result:
            raise Exception(result["error"])

        raw_articles = result.get("articles", [])
        logger.info(f"Received {len(raw_articles)} global news articles from {provider}")

        # Convert to standard format
        articles = []
        for article in raw_articles:
            pub_date = article.get("published_at", "")
            if isinstance(pub_date, str) and pub_date:
                try:
                    pub_date = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                except ValueError:
                    pub_date = start_date

            articles.append({
                'title': article.get('title', ''),
                'summary': article.get('summary', ''),
                'content': article.get('full_content', article.get('summary', article.get('snippet', ''))),
                'date': pub_date,
                'source': article.get('source', provider.upper()),
                'url': article.get('url', ''),
                'sentiment': article.get('sentiment'),
                'sentiment_score': article.get('sentiment_score')
            })

        logger.info(f"Fetched {len(articles)} global news articles from {provider}")
        return articles

    def _get_news_provider(self, provider: str):
        """
        Get news provider instance by name.

        Args:
            provider: Provider name ('fmp', 'alphavantage', 'finnhub', 'alpaca')

        Returns:
            News provider instance

        Raises:
            ValueError: If provider is unknown
            ImportError: If provider dependencies are not installed
            Exception: If provider initialization fails

        Note:
            GoogleNewsProvider has been removed (scraping unreliable).
            AINewsProvider requires ModelFactory dependency.
        """
        valid_providers = ['fmp', 'alphavantage', 'finnhub', 'alpaca', 'localfiles']

        if provider not in valid_providers:
            raise ValueError(f"Unknown news provider: '{provider}'. Valid providers: {valid_providers}")

        logger.debug(f"Initializing news provider: {provider}")

        # Secondary re-source seam (Phase 5, Task 6): when FEATURES_SOURCE=ba2_providers
        # is explicitly selected, route the news fetch through the shared ba2_providers
        # cache (category "news"; names alpaca/alphavantage/finnhub/fmp/google). The
        # returned provider exposes the same get_company_news/get_global_news contract
        # the legacy dataproviders.news clients do, so fetch_news_for_ticker / output
        # news_* columns are UNCHANGED. DEFAULT is legacy; verification is deferred to
        # plan Task 8 (do NOT flip the default until per-block equivalence is documented).
        # Any failure falls through to the legacy client below. 'localfiles' has no
        # ba2_providers equivalent, so it always uses the legacy path.
        from app.services.features_source import use_ba2_providers, get_ba2_provider

        if use_ba2_providers() and provider != "localfiles":
            ba2_provider = get_ba2_provider("news", provider)
            if ba2_provider is not None:
                logger.info(
                    f"FEATURES_SOURCE=ba2_providers: news provider '{provider}' via ba2_providers"
                )
                return ba2_provider
            logger.warning(
                f"FEATURES_SOURCE=ba2_providers: ba2_providers news '{provider}' unavailable; "
                f"falling back to legacy client"
            )

        if provider == "fmp":
            from ba2_providers.news import FMPNewsProvider
            if FMPNewsProvider is None:
                raise ImportError("FMPNewsProvider not available - check if fmpsdk is installed")
            return FMPNewsProvider()
        elif provider == "alphavantage":
            from ba2_providers.news import AlphaVantageNewsProvider
            if AlphaVantageNewsProvider is None:
                raise ImportError("AlphaVantageNewsProvider not available - check dependencies")
            return AlphaVantageNewsProvider()
        elif provider == "finnhub":
            from ba2_providers.news import FinnhubNewsProvider
            if FinnhubNewsProvider is None:
                raise ImportError("FinnhubNewsProvider not available - check if finnhub-python is installed")
            return FinnhubNewsProvider()
        elif provider == "alpaca":
            from ba2_providers.news import AlpacaNewsProvider
            if AlpacaNewsProvider is None:
                raise ImportError("AlpacaNewsProvider not available - check if alpaca-py is installed")
            return AlpacaNewsProvider()
        elif provider == "localfiles":
            from ba2_providers.news import LocalFilesNewsProvider
            if LocalFilesNewsProvider is None:
                raise ImportError("LocalFilesNewsProvider not available")
            return LocalFilesNewsProvider()

    @staticmethod
    def get_feature_descriptions() -> Dict[str, str]:
        """
        Get descriptions for all sentiment features.

        Returns:
            Dictionary mapping feature names to descriptions
        """
        descriptions = {
            'news_count': 'Total news count for the day'
        }
        for period_name, period_days in SentimentService.LOOKBACK_PERIODS.items():
            descriptions[f'news_{period_name}_count'] = (
                f"Total news count in the last {period_name} ({period_days} days)"
            )
            for sentiment in SentimentService.SENTIMENT_CATEGORIES:
                col_name = f'news_{period_name}_{sentiment}'
                descriptions[col_name] = (
                    f"Count of {sentiment} news articles "
                    f"in the last {period_name} ({period_days} days)"
                )
        return descriptions

"""
News Cache Service

Handles caching of news articles to avoid redundant fetching and sentiment analysis.
Articles are indexed in database and content is stored in files on disk.
"""

import hashlib
import json
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from sqlalchemy.orm import Session

from app.models.database import SessionLocal
from app.models.news_cache import NewsCache

logger = logging.getLogger(__name__)


class NewsCacheService:
    """
    Service for caching news articles.

    Architecture:
    - Database stores article metadata, URLs, and sentiment results
    - File system stores article content (base64 encoded JSON)
    - Files organized by provider: datasets/cache/news/{provider}/

    Usage:
        cache = NewsCacheService()

        # Check if article is cached
        cached = cache.get_cached_article(url, provider)
        if cached:
            return cached

        # Cache new article
        cache.cache_article(article, provider, ticker)
    """

    def __init__(self, cache_dir: Optional[str] = None):
        """
        Initialize NewsCacheService.

        Args:
            cache_dir: Base directory for news cache files. Defaults to the
                test-bucket news cache (app.paths.NEWS_CACHE_DIR, under
                ~/Documents/ba2/test/cache/news) — NOT the repo/CWD.
        """
        if cache_dir is None:
            from app.paths import NEWS_CACHE_DIR
            self.cache_dir = Path(NEWS_CACHE_DIR)
        else:
            self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_url_hash(self, url: str) -> str:
        """Generate SHA256 hash of URL for indexing."""
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

    def _get_provider_dir(self, provider: str) -> Path:
        """Get cache directory for a provider."""
        provider_dir = self.cache_dir / provider.lower()
        provider_dir.mkdir(parents=True, exist_ok=True)
        return provider_dir

    def _get_content_file_path(self, url_hash: str, provider: str) -> str:
        """Get relative path for content file."""
        return f"{provider.lower()}/{url_hash[:2]}/{url_hash}.json"

    def _save_content_file(self, content: str, file_path: str) -> bool:
        """
        Save article content to file as base64 encoded JSON.

        Args:
            content: Article content text
            file_path: Relative path from cache_dir

        Returns:
            True if saved successfully
        """
        try:
            full_path = self.cache_dir / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Create JSON structure and base64 encode
            data = {
                'content': content,
                'cached_at': datetime.now().isoformat()
            }
            json_str = json.dumps(data, ensure_ascii=False)
            encoded = base64.b64encode(json_str.encode('utf-8')).decode('ascii')

            with open(full_path, 'w') as f:
                f.write(encoded)

            return True

        except Exception as e:
            logger.error(f"Failed to save content file {file_path}: {e}")
            return False

    def _load_content_file(self, file_path: str) -> Optional[str]:
        """
        Load article content from base64 encoded JSON file.

        Args:
            file_path: Relative path from cache_dir

        Returns:
            Article content or None if not found
        """
        try:
            full_path = self.cache_dir / file_path
            if not full_path.exists():
                return None

            with open(full_path, 'r') as f:
                encoded = f.read()

            json_str = base64.b64decode(encoded.encode('ascii')).decode('utf-8')
            data = json.loads(json_str)
            return data.get('content', '')

        except Exception as e:
            logger.warning(f"Failed to load content file {file_path}: {e}")
            return None

    def get_cached_article(
        self,
        url: str,
        provider: str,
        db: Session = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached article by URL.

        Args:
            url: Article URL
            provider: News provider name
            db: Optional database session (creates new if not provided)

        Returns:
            Article dictionary or None if not cached
        """
        close_db = db is None
        if db is None:
            db = SessionLocal()

        try:
            url_hash = self._get_url_hash(url)
            cache_entry = db.query(NewsCache).filter(
                NewsCache.url_hash == url_hash
            ).first()

            if not cache_entry:
                return None

            # Load content from file
            content = None
            if cache_entry.content_file_path:
                content = self._load_content_file(cache_entry.content_file_path)

            return cache_entry.to_article_dict(content)

        finally:
            if close_db:
                db.close()

    def get_cached_articles_for_ticker(
        self,
        ticker: str,
        provider: str,
        start_date: datetime,
        end_date: datetime,
        db: Session = None
    ) -> List[Dict[str, Any]]:
        """
        Get all cached articles for a ticker in date range.

        Args:
            ticker: Stock ticker
            provider: News provider name
            start_date: Start of date range
            end_date: End of date range
            db: Optional database session

        Returns:
            List of article dictionaries
        """
        close_db = db is None
        if db is None:
            db = SessionLocal()

        try:
            # Normalize dates to naive datetime for consistent comparison
            if start_date is not None and hasattr(start_date, 'tzinfo') and start_date.tzinfo is not None:
                start_date = start_date.replace(tzinfo=None)
            if end_date is not None and hasattr(end_date, 'tzinfo') and end_date.tzinfo is not None:
                end_date = end_date.replace(tzinfo=None)

            # Debug: Check total cached for this ticker/provider
            total_cached = db.query(NewsCache).filter(
                NewsCache.ticker == ticker,
                NewsCache.provider == provider.lower()
            ).count()
            logger.debug(f"Cache lookup: {ticker}/{provider.lower()}, date range: {start_date} to {end_date}, total cached: {total_cached}")

            cache_entries = db.query(NewsCache).filter(
                NewsCache.ticker == ticker,
                NewsCache.provider == provider.lower(),
                NewsCache.published_at >= start_date,
                NewsCache.published_at <= end_date
            ).all()

            logger.debug(f"Cache hit: {len(cache_entries)} articles in date range")

            articles = []
            for entry in cache_entries:
                content = None
                if entry.content_file_path:
                    content = self._load_content_file(entry.content_file_path)
                articles.append(entry.to_article_dict(content))

            return articles

        finally:
            if close_db:
                db.close()

    def cache_article(
        self,
        article: Dict[str, Any],
        provider: str,
        ticker: str = None,
        db: Session = None
    ) -> Optional[NewsCache]:
        """
        Cache an article.

        Args:
            article: Article dictionary with url, title, content, date, etc.
            provider: News provider name
            ticker: Optional ticker symbol
            db: Optional database session

        Returns:
            NewsCache entry or None if failed
        """
        url = article.get('url', '')
        if not url:
            logger.debug("Cannot cache article without URL")
            return None

        close_db = db is None
        if db is None:
            db = SessionLocal()

        try:
            url_hash = self._get_url_hash(url)

            # Check if already cached
            existing = db.query(NewsCache).filter(
                NewsCache.url_hash == url_hash
            ).first()

            if existing:
                updated = False

                # Update summary if different
                new_summary = article.get('summary') or None
                if new_summary and new_summary != existing.summary:
                    existing.summary = new_summary
                    updated = True

                # Update content file if new content available
                content = article.get('content', '')
                if content and (not existing.content_fetched or not existing.content_file_path):
                    content_file_path = self._get_content_file_path(url_hash, provider)
                    self._save_content_file(content, content_file_path)
                    existing.content_file_path = content_file_path
                    existing.content_fetched = 1
                    updated = True

                # Update sentiment if newly analyzed
                if article.get('sentiment'):
                    existing.sentiment_label = article.get('sentiment')
                    existing.sentiment_score = article.get('sentiment_score')
                    existing.positive_prob = article.get('positive_prob')
                    existing.neutral_prob = article.get('neutral_prob')
                    existing.negative_prob = article.get('negative_prob')
                    existing.analyzed_at = datetime.now()
                    updated = True

                if updated:
                    db.commit()
                return existing

            # Save content to file
            content = article.get('content', '')
            content_file_path = None
            if content:
                content_file_path = self._get_content_file_path(url_hash, provider)
                self._save_content_file(content, content_file_path)

            # Parse published date and normalize to naive datetime (UTC)
            pub_date = article.get('date')
            if isinstance(pub_date, str):
                try:
                    pub_date = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                except ValueError:
                    pub_date = None
            # Convert timezone-aware datetime to naive (UTC)
            if pub_date is not None and hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
                pub_date = pub_date.replace(tzinfo=None)

            # Create cache entry
            cache_entry = NewsCache(
                provider=provider.lower(),
                original_url=url,
                resolved_url=article.get('resolved_url'),
                url_hash=url_hash,
                ticker=ticker,
                title=article.get('title') or None,
                summary=article.get('summary') or None,
                source=article.get('source') or None,
                published_at=pub_date,
                sentiment_label=article.get('sentiment'),
                sentiment_score=article.get('sentiment_score'),
                positive_prob=article.get('positive_prob'),
                neutral_prob=article.get('neutral_prob'),
                negative_prob=article.get('negative_prob'),
                content_file_path=content_file_path,
                content_fetched=1 if article.get('content_fetched') else 0,
                fetched_at=datetime.now(),
                analyzed_at=datetime.now() if article.get('sentiment') else None
            )

            db.add(cache_entry)
            db.commit()
            db.refresh(cache_entry)

            logger.debug(f"Cached article: {url[:80]}...")
            return cache_entry

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to cache article: {e}")
            return None

        finally:
            if close_db:
                db.close()

    def cache_articles_batch(
        self,
        articles: List[Dict[str, Any]],
        provider: str,
        ticker: str = None,
        batch_size: int = 50
    ) -> Tuple[int, int]:
        """
        Cache multiple articles with batched commits to reduce DB lock contention.

        Args:
            articles: List of article dictionaries
            provider: News provider name
            ticker: Optional ticker symbol
            batch_size: Number of articles to commit in each batch (default: 50)

        Returns:
            Tuple of (cached_count, skipped_count)
        """
        db = SessionLocal()
        cached = 0
        skipped = 0
        pending_count = 0

        try:
            for i, article in enumerate(articles):
                url = article.get('url', '')
                if not url:
                    skipped += 1
                    continue

                url_hash = self._get_url_hash(url)

                # Check if already cached
                existing = db.query(NewsCache).filter(
                    NewsCache.url_hash == url_hash
                ).first()

                if existing:
                    updated = False

                    # Update summary if different
                    new_summary = article.get('summary') or None
                    if new_summary and new_summary != existing.summary:
                        existing.summary = new_summary
                        updated = True

                    # Update content file if new content available
                    content = article.get('content', '')
                    if content and (not existing.content_fetched or not existing.content_file_path):
                        content_file_path = self._get_content_file_path(url_hash, provider)
                        self._save_content_file(content, content_file_path)
                        existing.content_file_path = content_file_path
                        existing.content_fetched = 1
                        updated = True

                    # Update sentiment if newly analyzed
                    if article.get('sentiment'):
                        existing.sentiment_label = article.get('sentiment')
                        existing.sentiment_score = article.get('sentiment_score')
                        existing.positive_prob = article.get('positive_prob')
                        existing.neutral_prob = article.get('neutral_prob')
                        existing.negative_prob = article.get('negative_prob')
                        existing.analyzed_at = datetime.now()
                        updated = True

                    if updated:
                        pending_count += 1
                    cached += 1
                else:
                    # Save content to file
                    content = article.get('content', '')
                    content_file_path = None
                    if content:
                        content_file_path = self._get_content_file_path(url_hash, provider)
                        self._save_content_file(content, content_file_path)

                    # Parse published date and normalize to naive datetime (UTC)
                    pub_date = article.get('date')
                    if isinstance(pub_date, str):
                        try:
                            pub_date = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                        except ValueError:
                            pub_date = None
                    # Convert timezone-aware datetime to naive (UTC)
                    if pub_date is not None and hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
                        pub_date = pub_date.replace(tzinfo=None)

                    # Create cache entry
                    cache_entry = NewsCache(
                        provider=provider.lower(),
                        original_url=url,
                        resolved_url=article.get('resolved_url'),
                        url_hash=url_hash,
                        ticker=ticker,
                        title=article.get('title') or None,
                        summary=article.get('summary') or None,
                        source=article.get('source') or None,
                        published_at=pub_date,
                        sentiment_label=article.get('sentiment'),
                        sentiment_score=article.get('sentiment_score'),
                        positive_prob=article.get('positive_prob'),
                        neutral_prob=article.get('neutral_prob'),
                        negative_prob=article.get('negative_prob'),
                        content_file_path=content_file_path,
                        content_fetched=1 if article.get('content_fetched') else 0,
                        fetched_at=datetime.now(),
                        analyzed_at=datetime.now() if article.get('sentiment') else None
                    )
                    db.add(cache_entry)
                    pending_count += 1
                    cached += 1

                # Commit in batches to reduce lock contention
                if pending_count >= batch_size:
                    db.commit()
                    pending_count = 0

            # Commit any remaining
            if pending_count > 0:
                db.commit()

            logger.info(f"Cached {cached} articles, skipped {skipped}")
            return cached, skipped

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to cache articles batch: {e}")
            return cached, skipped

        finally:
            db.close()

    def update_sentiment(
        self,
        url: str,
        sentiment_result: Dict[str, Any],
        db: Session = None
    ) -> bool:
        """
        Update sentiment for a cached article.

        Args:
            url: Article URL
            sentiment_result: Sentiment analysis result dict

        Returns:
            True if updated successfully
        """
        close_db = db is None
        if db is None:
            db = SessionLocal()

        try:
            url_hash = self._get_url_hash(url)
            cache_entry = db.query(NewsCache).filter(
                NewsCache.url_hash == url_hash
            ).first()

            if not cache_entry:
                return False

            cache_entry.sentiment_label = sentiment_result.get('label')
            cache_entry.sentiment_score = sentiment_result.get('score')
            cache_entry.positive_prob = sentiment_result.get('positive_prob')
            cache_entry.neutral_prob = sentiment_result.get('neutral_prob')
            cache_entry.negative_prob = sentiment_result.get('negative_prob')
            cache_entry.analyzed_at = datetime.now()

            db.commit()
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update sentiment: {e}")
            return False

        finally:
            if close_db:
                db.close()

    def update_sentiment_batch(
        self,
        sentiment_updates: List[Tuple[str, Dict[str, Any]]],
        batch_size: int = 50
    ) -> int:
        """
        Update sentiment for multiple cached articles with batched commits.

        Args:
            sentiment_updates: List of (url, sentiment_result) tuples
            batch_size: Number of updates to commit in each batch

        Returns:
            Number of successfully updated articles
        """
        if not sentiment_updates:
            return 0

        db = SessionLocal()
        updated = 0
        pending_count = 0

        try:
            for url, sentiment_result in sentiment_updates:
                url_hash = self._get_url_hash(url)
                cache_entry = db.query(NewsCache).filter(
                    NewsCache.url_hash == url_hash
                ).first()

                if cache_entry:
                    cache_entry.sentiment_label = sentiment_result.get('label')
                    cache_entry.sentiment_score = sentiment_result.get('score')
                    cache_entry.positive_prob = sentiment_result.get('positive_prob')
                    cache_entry.neutral_prob = sentiment_result.get('neutral_prob')
                    cache_entry.negative_prob = sentiment_result.get('negative_prob')
                    cache_entry.analyzed_at = datetime.now()
                    updated += 1
                    pending_count += 1

                    # Commit in batches
                    if pending_count >= batch_size:
                        db.commit()
                        pending_count = 0

            # Commit any remaining
            if pending_count > 0:
                db.commit()

            logger.debug(f"Batch updated sentiment for {updated} articles")
            return updated

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to batch update sentiment: {e}")
            return updated

        finally:
            db.close()

    def get_cached_content_for_urls(self, urls: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Bulk lookup cached content for a list of URLs.

        Returns a dict mapping URL -> {'content': str, 'content_fetched': bool}
        for URLs that have content cached. URLs without cached content are omitted.
        """
        if not urls:
            return {}

        db = SessionLocal()
        try:
            url_hashes = {self._get_url_hash(url): url for url in urls if url}
            if not url_hashes:
                return {}

            entries = db.query(NewsCache).filter(
                NewsCache.url_hash.in_(list(url_hashes.keys())),
                NewsCache.content_fetched == 1
            ).all()

            result = {}
            for entry in entries:
                url = url_hashes.get(entry.url_hash)
                if url and entry.content_file_path:
                    content = self._load_content_file(entry.content_file_path)
                    if content:
                        result[url] = {
                            'content': content,
                            'content_fetched': True,
                        }
            return result
        finally:
            db.close()

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats
        """
        db = SessionLocal()
        try:
            total = db.query(NewsCache).count()
            with_sentiment = db.query(NewsCache).filter(
                NewsCache.sentiment_label.isnot(None)
            ).count()
            with_content = db.query(NewsCache).filter(
                NewsCache.content_fetched == 1
            ).count()

            # Count by provider
            from sqlalchemy import func, case
            by_provider = dict(
                db.query(NewsCache.provider, func.count(NewsCache.id))
                .group_by(NewsCache.provider)
                .all()
            )

            # Count by provider + symbol with sentiment/content stats
            by_provider_symbol = {}
            rows = db.query(
                NewsCache.provider,
                NewsCache.ticker,
                func.count(NewsCache.id),
                func.sum(
                    case(
                        (NewsCache.sentiment_label.isnot(None), 1),
                        else_=0
                    )
                ),
                func.sum(
                    case(
                        (NewsCache.content_fetched == 1, 1),
                        else_=0
                    )
                ),
            ).group_by(NewsCache.provider, NewsCache.ticker).all()

            for prov, ticker, count, sent_count, content_count in rows:
                if prov not in by_provider_symbol:
                    by_provider_symbol[prov] = {}
                by_provider_symbol[prov][ticker or '(no ticker)'] = {
                    'count': count,
                    'with_sentiment': int(sent_count or 0),
                    'with_content': int(content_count or 0),
                }

            return {
                'total_articles': total,
                'with_sentiment': with_sentiment,
                'with_content': with_content,
                'by_provider': by_provider,
                'by_provider_symbol': by_provider_symbol,
            }

        finally:
            db.close()

    def clear_cache(self, provider: str = None, ticker: str = None) -> int:
        """
        Clear cache entries.

        Args:
            provider: Optional provider to filter
            ticker: Optional ticker to filter

        Returns:
            Number of entries deleted
        """
        db = SessionLocal()
        try:
            query = db.query(NewsCache)

            if provider:
                query = query.filter(NewsCache.provider == provider.lower())
            if ticker:
                query = query.filter(NewsCache.ticker == ticker)

            count = query.delete()
            db.commit()

            logger.info(f"Cleared {count} cache entries")
            return count

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to clear cache: {e}")
            return 0

        finally:
            db.close()

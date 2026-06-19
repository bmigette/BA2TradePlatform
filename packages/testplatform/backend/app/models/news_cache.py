"""
News Cache Model

Database model for caching news articles to avoid refetching and re-analyzing.
Content is stored in files on disk, indexed by this model.
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Index
from sqlalchemy.sql import func
from app.models.database import Base


class NewsCache(Base):
    """
    Cache entry for a news article.

    The actual content is stored in a file on disk (base64 encoded JSON).
    This model indexes the article for quick lookup and stores sentiment.
    """
    __tablename__ = "news_cache"

    id = Column(Integer, primary_key=True, index=True)

    # Provider identification
    provider = Column(String(50), nullable=False, index=True)

    # URL tracking (some providers like Finnhub use redirect URLs)
    original_url = Column(String(2048), nullable=False)
    resolved_url = Column(String(2048), nullable=True)  # After following redirects

    # URL hash for fast lookup (SHA256 of original_url)
    url_hash = Column(String(64), nullable=False, index=True, unique=True)

    # Article metadata
    ticker = Column(String(20), nullable=True, index=True)  # May be null for global news
    title = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)  # Original provider summary (separate from full content in file)
    source = Column(String(200), nullable=True)
    published_at = Column(DateTime, nullable=True, index=True)

    # Sentiment analysis results (cached to avoid re-running model)
    sentiment_label = Column(String(20), nullable=True)  # positive, neutral, negative
    sentiment_score = Column(Float, nullable=True)
    positive_prob = Column(Float, nullable=True)
    neutral_prob = Column(Float, nullable=True)
    negative_prob = Column(Float, nullable=True)

    # Content file path (relative to cache directory)
    content_file_path = Column(String(512), nullable=True)
    content_fetched = Column(Integer, default=0)  # 1 if full content was fetched via trafilatura

    # Timestamps
    fetched_at = Column(DateTime, default=func.now())
    analyzed_at = Column(DateTime, nullable=True)  # When sentiment was analyzed
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Composite index for common queries
    __table_args__ = (
        Index('ix_news_cache_provider_ticker', 'provider', 'ticker'),
        Index('ix_news_cache_provider_published', 'provider', 'published_at'),
        Index('ix_news_cache_ticker_published', 'ticker', 'published_at'),
    )

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'provider': self.provider,
            'original_url': self.original_url,
            'resolved_url': self.resolved_url,
            'ticker': self.ticker,
            'title': self.title,
            'summary': self.summary,
            'source': self.source,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'sentiment_label': self.sentiment_label,
            'sentiment_score': self.sentiment_score,
            'positive_prob': self.positive_prob,
            'neutral_prob': self.neutral_prob,
            'negative_prob': self.negative_prob,
            'content_fetched': bool(self.content_fetched),
            'fetched_at': self.fetched_at.isoformat() if self.fetched_at else None,
            'analyzed_at': self.analyzed_at.isoformat() if self.analyzed_at else None,
        }

    def to_article_dict(self, content: str = None):
        """
        Convert to standard article dictionary format used by SentimentService.

        Args:
            content: Optional content loaded from file

        Returns:
            Article dictionary compatible with SentimentService
        """
        return {
            'title': self.title or '',
            'summary': self.summary or '',
            'content': content or '',
            'date': self.published_at,
            'source': self.source or self.provider.upper(),
            'url': self.original_url,
            'content_fetched': bool(self.content_fetched),
            'sentiment': self.sentiment_label,
            'sentiment_score': self.sentiment_score,
            'positive_prob': self.positive_prob,
            'neutral_prob': self.neutral_prob,
            'negative_prob': self.negative_prob,
        }

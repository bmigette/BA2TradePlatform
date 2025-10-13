"""
Social Media Data Provider Interface

Interface for social media sentiment analysis providers.
Providers implementing this interface fetch and analyze social media sentiment
from various public sources (Twitter/X, Reddit, StockTwits, news forums, etc.).
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class SocialMediaDataProviderInterface(DataProviderInterface):
    """
    Interface for social media sentiment analysis providers.
    
    Implementations should aggregate sentiment data from public social media sources
    including Twitter/X, Reddit, StockTwits, financial forums, and news discussions.
    """
    
    @abstractmethod
    def get_social_media_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment analysis"],
        lookback_days: Annotated[int, "Number of days to look back for sentiment data"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get social media sentiment analysis for a symbol.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'TSLA')
            end_date: End date for sentiment analysis
            lookback_days: Number of days to analyze sentiment data
            format_type: Output format - 'dict' for structured data,
                        'markdown' for LLM-friendly text, 'both' for both formats
        
        Returns:
            Dict[str, Any] | str: Sentiment analysis data in requested format
            
            For dict format:
            {
                "symbol": str,
                "analysis_period": {
                    "start_date": str (ISO),
                    "end_date": str (ISO),
                    "days": int
                },
                "sentiment": {
                    "overall": str (e.g., "bullish", "bearish", "neutral"),
                    "score": float (-1.0 to 1.0),
                    "confidence": float (0.0 to 1.0)
                },
                "sources": {
                    "twitter": {...},
                    "reddit": {...},
                    "stocktwits": {...},
                    # etc.
                },
                "key_themes": List[str],
                "notable_mentions": List[Dict],
                "summary": str
            }
            
            For markdown format:
                Human-readable sentiment analysis with examples and sources
        """
        pass

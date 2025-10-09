"""
Provider wrapper for automatic database persistence.

This module provides a wrapper class that automatically saves data provider
outputs to the AnalysisOutput table while still returning data for use in
TradingAgents graph state or other workflows.
"""

from typing import Dict, Any, Optional, Literal, Callable
from datetime import datetime, timezone, timedelta
import json

from ba2_trade_platform.core.interfaces import DataProviderInterface
from ba2_trade_platform.core.models import AnalysisOutput
from ba2_trade_platform.core.db import get_db, add_instance
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

# NOT USED ATP

class ProviderWithPersistence:
    """
    Wrapper for data providers that automatically saves outputs to database.
    
    This class wraps any DataProviderInterface implementation and automatically
    persists the output to the AnalysisOutput table while still returning the data
    for use in graph state or other workflows.
    
    Features:
    - Automatic database persistence
    - Built-in caching with configurable TTL
    - Metadata tracking for audit trail
    - Optional link to MarketAnalysis for workflow tracking
    
    Example:
        >>> from ba2_trade_platform.modules.dataproviders import get_provider
        >>> news_provider = get_provider("news", "alpaca")
        >>> wrapper = ProviderWithPersistence(news_provider, "news", market_analysis_id=123)
        >>> 
        >>> # Fetch and auto-save
        >>> news = wrapper.fetch_and_save(
        ...     "get_company_news",
        ...     "AAPL_news_recent",
        ...     symbol="AAPL",
        ...     end_date=datetime.now(),
        ...     lookback_days=7,
        ...     format_type="markdown"
        ... )
        >>> # news is returned AND saved to database
    """
    
    def __init__(
        self, 
        provider: DataProviderInterface,
        category: str,
        market_analysis_id: Optional[int] = None
    ):
        """
        Initialize wrapper with a provider instance.
        
        Args:
            provider: The actual data provider instance
            category: Provider category ('news', 'indicators', 'fundamentals_overview', etc.)
            market_analysis_id: Optional link to MarketAnalysis for workflow tracking
        """
        self.provider = provider
        self.category = category
        self.market_analysis_id = market_analysis_id
        self.provider_name = provider.get_provider_name()
    
    def fetch_and_save(
        self,
        method_name: str,
        output_name: str,
        save_to_db: bool = True,
        **kwargs
    ) -> Dict[str, Any] | str:
        """
        Call a provider method and automatically save the output.
        
        Args:
            method_name: Name of provider method to call (e.g., 'get_company_news')
            output_name: Name for the saved output (e.g., 'AAPL_news_7days')
            save_to_db: Whether to save to database (default: True)
            **kwargs: Arguments to pass to the provider method
        
        Returns:
            The provider's output (dict or markdown string)
        
        Example:
            >>> news = wrapper.fetch_and_save(
            ...     "get_company_news",
            ...     "AAPL_news_recent",
            ...     symbol="AAPL",
            ...     end_date=datetime.now(),
            ...     lookback_days=7,
            ...     format_type="markdown"
            ... )
        """
        try:
            # Call the provider method
            method = getattr(self.provider, method_name)
            result = method(**kwargs)
            
            # Save to database if requested
            if save_to_db:
                self._save_output(output_name, result, method_name, kwargs)
            
            return result
            
        except Exception as e:
            logger.error(
                f"Error fetching {method_name} from {self.provider_name}: {e}",
                exc_info=True
            )
            raise
    
    def _save_output(
        self,
        output_name: str,
        result: Dict[str, Any] | str,
        method_name: str,
        kwargs: Dict[str, Any]
    ):
        """
        Save provider output to database.
        
        Args:
            output_name: Name for the output
            result: Provider result (dict or string)
            method_name: Provider method that was called
            kwargs: Arguments that were passed to the method
        """
        try:
            # Extract metadata from kwargs
            symbol = kwargs.get('symbol')
            start_date = kwargs.get('start_date')
            end_date = kwargs.get('end_date')
            format_type = kwargs.get('format_type', 'markdown')
            
            # Calculate start_date if using lookback
            if not start_date and 'lookback_days' in kwargs and end_date:
                lookback_days = kwargs['lookback_days']
                if isinstance(lookback_days, int):
                    start_date = end_date - timedelta(days=lookback_days)
            elif not start_date and 'lookback_periods' in kwargs:
                # For financial statements, store the period count in metadata
                # start_date will remain None
                pass
            
            # Prepare text content
            if isinstance(result, dict):
                text_content = json.dumps(result, indent=2, default=str)
            else:
                text_content = str(result)
            
            # Create AnalysisOutput record
            analysis_output = AnalysisOutput(
                market_analysis_id=self.market_analysis_id,
                provider_category=self.category,
                provider_name=self.provider_name,
                name=output_name,
                type=self.category,
                text=text_content,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                format_type=format_type,
                provider_metadata={
                    'method': method_name,
                    'kwargs': self._serialize_kwargs(kwargs),
                    'provider_features': self.provider.get_supported_features(),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
            )
            
            # Save to database
            output_id = add_instance(analysis_output)
            
            logger.info(
                f"Saved {self.category} output '{output_name}' from {self.provider_name} "
                f"(id={output_id}, symbol={symbol})"
            )
            
        except Exception as e:
            logger.error(
                f"Error saving provider output to database: {e}",
                exc_info=True
            )
            # Don't raise - we still want to return the data even if save fails
    
    def _serialize_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Serialize kwargs for JSON storage.
        
        Args:
            kwargs: Original kwargs dict
            
        Returns:
            Serialized kwargs safe for JSON
        """
        serialized = {}
        for key, value in kwargs.items():
            if isinstance(value, datetime):
                serialized[key] = value.isoformat()
            elif isinstance(value, (str, int, float, bool, type(None))):
                serialized[key] = value
            elif isinstance(value, (list, tuple)):
                serialized[key] = [str(v) for v in value]
            else:
                serialized[key] = str(value)
        return serialized
    
    def check_cache(
        self,
        output_name: str,
        max_age_hours: int = 24
    ) -> Optional[Dict[str, Any] | str]:
        """
        Check if cached output exists and is still fresh.
        
        Args:
            output_name: Name of the output to check
            max_age_hours: Maximum age in hours for cache validity (default: 24)
        
        Returns:
            Cached output if found and fresh, None otherwise
            
        Example:
            >>> # Check for cached news
            >>> cached = wrapper.check_cache("AAPL_news_7days", max_age_hours=6)
            >>> if cached:
            ...     print("Using cached data")
            ... else:
            ...     data = wrapper.fetch_and_save(...)
        """
        try:
            engine = get_db()
            with Session(engine.bind) as session:
                statement = select(AnalysisOutput).where(
                    AnalysisOutput.name == output_name,
                    AnalysisOutput.provider_category == self.category,
                    AnalysisOutput.provider_name == self.provider_name
                ).order_by(AnalysisOutput.created_at.desc())
                
                output = session.exec(statement).first()
                
                if output:
                    age = datetime.now(timezone.utc) - output.created_at.replace(tzinfo=timezone.utc)
                    if age < timedelta(hours=max_age_hours):
                        logger.info(
                            f"Cache HIT: Using cached {self.category} output '{output_name}' "
                            f"from {self.provider_name} (age: {age.total_seconds()/3600:.1f}h)"
                        )
                        
                        # Return cached data in original format
                        if output.format_type == 'dict' and output.text:
                            try:
                                return json.loads(output.text)
                            except json.JSONDecodeError:
                                logger.warning(f"Failed to parse cached dict output, returning as string")
                                return output.text
                        else:
                            return output.text
                    else:
                        logger.info(
                            f"Cache EXPIRED: {self.category} output '{output_name}' "
                            f"is too old (age: {age.total_seconds()/3600:.1f}h > {max_age_hours}h)"
                        )
                else:
                    logger.info(
                        f"Cache MISS: No cached {self.category} output '{output_name}' found"
                    )
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking cache: {e}", exc_info=True)
            return None
    
    def fetch_with_cache(
        self,
        method_name: str,
        output_name: str,
        max_age_hours: int = 24,
        **kwargs
    ) -> Dict[str, Any] | str:
        """
        Fetch data with automatic caching.
        
        Checks cache first, and only fetches from provider if cache miss or expired.
        
        Args:
            method_name: Name of provider method to call
            output_name: Name for the output (used as cache key)
            max_age_hours: Maximum cache age in hours (default: 24)
            **kwargs: Arguments to pass to the provider method
        
        Returns:
            The data (from cache or freshly fetched)
            
        Example:
            >>> # Automatically use cache if available and fresh
            >>> news = wrapper.fetch_with_cache(
            ...     "get_company_news",
            ...     "AAPL_news_7days",
            ...     max_age_hours=6,
            ...     symbol="AAPL",
            ...     end_date=datetime.now(),
            ...     lookback_days=7,
            ...     format_type="markdown"
            ... )
        """
        # Check cache first
        cached = self.check_cache(output_name, max_age_hours)
        if cached is not None:
            return cached
        
        # Cache miss - fetch and save
        return self.fetch_and_save(method_name, output_name, **kwargs)


def get_cached_output(
    category: str,
    provider_name: str,
    output_name: str,
    max_age_hours: int = 24
) -> Optional[AnalysisOutput]:
    """
    Utility function to retrieve cached output without provider instance.
    
    Args:
        category: Provider category
        provider_name: Provider name
        output_name: Output name
        max_age_hours: Maximum cache age in hours
    
    Returns:
        AnalysisOutput instance if found and fresh, None otherwise
    """
    try:
        engine = get_db()
        with Session(engine.bind) as session:
            statement = select(AnalysisOutput).where(
                AnalysisOutput.name == output_name,
                AnalysisOutput.provider_category == category,
                AnalysisOutput.provider_name == provider_name
            ).order_by(AnalysisOutput.created_at.desc())
            
            output = session.exec(statement).first()
            
            if output:
                age = datetime.now(timezone.utc) - output.created_at.replace(tzinfo=timezone.utc)
                if age < timedelta(hours=max_age_hours):
                    return output
        
        return None
        
    except Exception as e:
        logger.error(f"Error retrieving cached output: {e}", exc_info=True)
        return None

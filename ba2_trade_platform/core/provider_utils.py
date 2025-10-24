"""
Utility functions for data providers.

This module provides helper functions for validating inputs, managing cache,
and working with provider outputs.
"""

from typing import Dict, Any, Optional, List, Literal, Callable
from datetime import datetime, timezone, timedelta
import json
import functools
import inspect

from ba2_trade_platform.core.models import AnalysisOutput
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select, or_, and_


def log_provider_call(func: Callable) -> Callable:
    """
    Decorator that logs provider function calls with name, args, and results.
    
    Logs at DEBUG level:
    - Function name
    - Arguments (excluding 'self')
    - Full result for markdown/both formats, summary for dict
    
    Usage:
        @log_provider_call
        def get_company_news(self, symbol: str, lookback_days: int) -> Dict[str, Any]:
            ...
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Get function signature for better logging
        sig = inspect.signature(func)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()
        
        # Remove 'self' from logged args
        logged_args = {k: v for k, v in bound_args.arguments.items() if k != 'self'}
        
        # Get provider class name if available
        provider_name = args[0].__class__.__name__ if args and hasattr(args[0], '__class__') else "Provider"
        
        # Log the call
        logger.debug(f"{provider_name}.{func.__name__} called with args: {logged_args}")
        
        try:
            # Execute function
            result = func(*args, **kwargs)
            
            # Log result based on format
            if isinstance(result, dict):
                # Check if it's a "both" format dict with "text" and "data" keys
                if "text" in result and "data" in result:
                    logger.debug(f"{provider_name}.{func.__name__} returned 'both' format")
                    logger.debug(f"--- Markdown Output ({len(result['text'])} chars) ---\n{result['text'][:1000]}\n--- End Markdown ---")
                    logger.debug(f"Data dict has {len(result['data'])} keys")
                else:
                    logger.debug(f"{provider_name}.{func.__name__} returned dict with {len(result)} keys")
            elif isinstance(result, str):
                # Markdown or plain string result - log the full content
                logger.debug(f"{provider_name}.{func.__name__} returned markdown ({len(result)} chars)")
                logger.debug(f"--- Markdown Output ---\n{result[:1000]}\n--- End Markdown ---")
            elif isinstance(result, list):
                logger.debug(f"{provider_name}.{func.__name__} returned list with {len(result)} items")
            elif isinstance(result, tuple):
                logger.debug(f"{provider_name}.{func.__name__} returned tuple with {len(result)} elements")
            else:
                logger.debug(f"{provider_name}.{func.__name__} returned {type(result).__name__}")
            
            return result
            
        except Exception as e:
            logger.error(f"{provider_name}.{func.__name__} failed with error: {e}", exc_info=True)
            raise
    
    return wrapper


def validate_date_range(
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    max_days: Optional[int] = None
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Validate and normalize date range parameters.
    
    Args:
        start_date: Start date (optional)
        end_date: End date (optional)
        max_days: Maximum allowed days in range (optional)
    
    Returns:
        Tuple of (start_date, end_date) normalized to UTC timezone
        
    Raises:
        ValueError: If dates are invalid or range exceeds max_days
        
    Example:
        >>> start, end = validate_date_range(
        ...     datetime(2025, 1, 1),
        ...     datetime(2025, 1, 31),
        ...     max_days=365
        ... )
    """
    # Normalize to UTC
    if start_date and start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    
    # Validate order
    if start_date and end_date and start_date > end_date:
        raise ValueError(f"start_date ({start_date}) must be before end_date ({end_date})")
    
    # Validate future dates
    now = datetime.now(timezone.utc)
    if end_date and end_date > now + timedelta(days=1):
        logger.warning(f"end_date ({end_date}) is in the future, capping to now")
        end_date = now
    
    # Validate range
    if max_days and start_date and end_date:
        days = (end_date - start_date).days
        if days > max_days:
            raise ValueError(
                f"Date range ({days} days) exceeds maximum allowed ({max_days} days)"
            )
    
    return start_date, end_date


def validate_lookback_days(lookback_days: int, max_lookback: int = 365) -> int:
    """
    Validate lookback_days parameter.
    
    Args:
        lookback_days: Number of days to look back
        max_lookback: Maximum allowed lookback (default: 365)
    
    Returns:
        Validated lookback_days
        
    Raises:
        ValueError: If lookback_days is invalid
        
    Example:
        >>> days = validate_lookback_days(30, max_lookback=90)
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be positive (got {lookback_days})")
    
    if lookback_days > max_lookback:
        raise ValueError(
            f"lookback_days ({lookback_days}) exceeds maximum allowed ({max_lookback})"
        )
    
    return lookback_days


def calculate_date_range(
    end_date: datetime,
    lookback_days: int
) -> tuple[datetime, datetime]:
    """
    Calculate start_date from end_date and lookback.
    
    Args:
        end_date: End date
        lookback_days: Number of days to look back
    
    Returns:
        Tuple of (start_date, end_date)
        
    Example:
        >>> end = datetime.now(timezone.utc)
        >>> start, end = calculate_date_range(end, lookback_days=7)
    """
    # Ensure UTC
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    
    start_date = end_date - timedelta(days=lookback_days)
    return start_date, end_date


def query_provider_outputs(
    category: Optional[str] = None,
    provider_name: Optional[str] = None,
    symbol: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    market_analysis_id: Optional[int] = None,
    max_age_hours: Optional[int] = None,
    limit: int = 100
) -> List[AnalysisOutput]:
    """
    Query provider outputs from database with flexible filtering.
    
    Args:
        category: Filter by provider category
        provider_name: Filter by provider name
        symbol: Filter by symbol
        start_date: Filter by outputs with data >= this date
        end_date: Filter by outputs with data <= this date
        market_analysis_id: Filter by market analysis
        max_age_hours: Only return outputs created within this many hours
        limit: Maximum number of results (default: 100)
    
    Returns:
        List of AnalysisOutput records matching filters
        
    Example:
        >>> # Get all recent news for AAPL
        >>> outputs = query_provider_outputs(
        ...     category="news",
        ...     symbol="AAPL",
        ...     max_age_hours=24
        ... )
    """
    try:
        engine = get_db()
        with Session(engine.bind) as session:
            # Build query
            statement = select(AnalysisOutput)
            
            # Apply filters
            conditions = []
            
            if category:
                conditions.append(AnalysisOutput.provider_category == category)
            
            if provider_name:
                conditions.append(AnalysisOutput.provider_name == provider_name)
            
            if symbol:
                conditions.append(AnalysisOutput.symbol == symbol)
            
            if start_date:
                conditions.append(
                    or_(
                        AnalysisOutput.start_date >= start_date,
                        AnalysisOutput.end_date >= start_date
                    )
                )
            
            if end_date:
                conditions.append(
                    or_(
                        AnalysisOutput.end_date <= end_date,
                        AnalysisOutput.start_date <= end_date
                    )
                )
            
            if market_analysis_id:
                conditions.append(AnalysisOutput.market_analysis_id == market_analysis_id)
            
            if max_age_hours:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                conditions.append(AnalysisOutput.created_at >= cutoff)
            
            if conditions:
                statement = statement.where(and_(*conditions))
            
            # Order and limit
            statement = statement.order_by(AnalysisOutput.created_at.desc()).limit(limit)
            
            # Execute
            results = session.exec(statement).all()
            return list(results)
            
    except Exception as e:
        logger.error(f"Error querying provider outputs: {e}", exc_info=True)
        return []


def get_latest_output(
    category: str,
    provider_name: str,
    output_name: str,
    symbol: Optional[str] = None
) -> Optional[AnalysisOutput]:
    """
    Get the most recent output for a specific provider/name combination.
    
    Args:
        category: Provider category
        provider_name: Provider name
        output_name: Output name
        symbol: Optional symbol filter
    
    Returns:
        Most recent AnalysisOutput or None
        
    Example:
        >>> output = get_latest_output("news", "alpaca", "AAPL_news_7days", "AAPL")
    """
    try:
        engine = get_db()
        with Session(engine.bind) as session:
            statement = select(AnalysisOutput).where(
                AnalysisOutput.provider_category == category,
                AnalysisOutput.provider_name == provider_name,
                AnalysisOutput.name == output_name
            )
            
            if symbol:
                statement = statement.where(AnalysisOutput.symbol == symbol)
            
            statement = statement.order_by(AnalysisOutput.created_at.desc())
            
            return session.exec(statement).first()
            
    except Exception as e:
        logger.error(f"Error getting latest output: {e}", exc_info=True)
        return None


def delete_old_outputs(
    max_age_days: int = 30,
    category: Optional[str] = None,
    dry_run: bool = True
) -> int:
    """
    Delete old provider outputs to manage database size.
    
    Args:
        max_age_days: Delete outputs older than this many days
        category: Only delete from specific category (optional)
        dry_run: If True, only count without deleting (default: True)
    
    Returns:
        Number of outputs deleted (or that would be deleted in dry_run)
        
    Example:
        >>> # Check how many would be deleted
        >>> count = delete_old_outputs(max_age_days=90, dry_run=True)
        >>> print(f"Would delete {count} outputs")
        >>> 
        >>> # Actually delete
        >>> count = delete_old_outputs(max_age_days=90, dry_run=False)
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        
        engine = get_db()
        with Session(engine.bind) as session:
            # Build query
            statement = select(AnalysisOutput).where(
                AnalysisOutput.created_at < cutoff,
                AnalysisOutput.provider_category.isnot(None)  # Only provider outputs
            )
            
            if category:
                statement = statement.where(AnalysisOutput.provider_category == category)
            
            outputs = session.exec(statement).all()
            count = len(outputs)
            
            if not dry_run and count > 0:
                for output in outputs:
                    session.delete(output)
                session.commit()
                logger.info(f"Deleted {count} old provider outputs (age > {max_age_days} days)")
            elif dry_run and count > 0:
                logger.info(
                    f"DRY RUN: Would delete {count} old provider outputs (age > {max_age_days} days)"
                )
            
            return count
            
    except Exception as e:
        logger.error(f"Error deleting old outputs: {e}", exc_info=True)
        return 0


def parse_provider_output(
    output: AnalysisOutput,
    expected_format: Literal['dict', 'markdown'] = 'dict'
) -> Dict[str, Any] | str:
    """
    Parse a provider output from database back to its original format.
    
    Args:
        output: AnalysisOutput instance
        expected_format: Expected format type ('dict' or 'markdown')
    
    Returns:
        Parsed output (dict or string)
        
    Example:
        >>> output = get_latest_output("news", "alpaca", "AAPL_news")
        >>> data = parse_provider_output(output, expected_format='dict')
    """
    try:
        if output.format_type == 'dict' or expected_format == 'dict':
            if output.text:
                try:
                    return json.loads(output.text)
                except json.JSONDecodeError:
                    logger.warning(
                        f"Failed to parse output as JSON, returning as string (id={output.id})"
                    )
                    return output.text or ""
            return {}
        else:
            return output.text or ""
            
    except Exception as e:
        logger.error(f"Error parsing provider output: {e}", exc_info=True)
        return {} if expected_format == 'dict' else ""


def get_provider_statistics(
    category: Optional[str] = None,
    days: int = 30
) -> Dict[str, Any]:
    """
    Get usage statistics for data providers.
    
    Args:
        category: Filter by category (optional)
        days: Number of days to analyze (default: 30)
    
    Returns:
        Dictionary with statistics (total_outputs, by_provider, by_category, etc.)
        
    Example:
        >>> stats = get_provider_statistics(days=7)
        >>> print(f"Total outputs: {stats['total_outputs']}")
        >>> print(f"By provider: {stats['by_provider']}")
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        
        engine = get_db()
        with Session(engine.bind) as session:
            # Build base query
            statement = select(AnalysisOutput).where(
                AnalysisOutput.created_at >= cutoff,
                AnalysisOutput.provider_category.isnot(None)
            )
            
            if category:
                statement = statement.where(AnalysisOutput.provider_category == category)
            
            outputs = session.exec(statement).all()
            
            # Calculate statistics
            total = len(outputs)
            by_provider: Dict[str, int] = {}
            by_category: Dict[str, int] = {}
            by_symbol: Dict[str, int] = {}
            
            for output in outputs:
                # By provider
                if output.provider_name:
                    by_provider[output.provider_name] = by_provider.get(output.provider_name, 0) + 1
                
                # By category
                if output.provider_category:
                    by_category[output.provider_category] = by_category.get(output.provider_category, 0) + 1
                
                # By symbol
                if output.symbol:
                    by_symbol[output.symbol] = by_symbol.get(output.symbol, 0) + 1
            
            return {
                'total_outputs': total,
                'days_analyzed': days,
                'by_provider': by_provider,
                'by_category': by_category,
                'by_symbol': by_symbol,
                'period_start': cutoff.isoformat(),
                'period_end': datetime.now(timezone.utc).isoformat()
            }
            
    except Exception as e:
        logger.error(f"Error getting provider statistics: {e}", exc_info=True)
        return {
            'total_outputs': 0,
            'days_analyzed': days,
            'by_provider': {},
            'by_category': {},
            'by_symbol': {},
            'error': str(e)
        }


def format_output_summary(output: AnalysisOutput) -> str:
    """
    Create a human-readable summary of a provider output.
    
    Args:
        output: AnalysisOutput instance
    
    Returns:
        Formatted summary string
        
    Example:
        >>> output = get_latest_output("news", "alpaca", "AAPL_news")
        >>> print(format_output_summary(output))
    """
    lines = [
        f"Provider Output: {output.name}",
        f"  Category: {output.provider_category}",
        f"  Provider: {output.provider_name}",
        f"  Symbol: {output.symbol or 'N/A'}",
        f"  Date Range: {output.start_date or 'N/A'} to {output.end_date or 'N/A'}",
        f"  Format: {output.format_type}",
        f"  Created: {output.created_at}",
        f"  Size: {len(output.text or '')} chars"
    ]
    
    if output.metadata:
        lines.append(f"  Metadata: {json.dumps(output.metadata, indent=2)}")
    
    return "\n".join(lines)

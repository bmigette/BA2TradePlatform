"""
Utility functions for data providers.

This module provides helper functions for validating inputs, managing cache,
and working with provider outputs.
"""

from typing import Dict, Any, Optional, Callable, TypeVar
from datetime import datetime, timezone, timedelta
import functools
import inspect
import time

from ba2_trade_platform.logger import logger

T = TypeVar("T")


def yf_retry(func: Callable[[], T], max_retries: int = 3, base_delay: float = 2.0) -> T:
    """
    Retry a yfinance call with exponential backoff on rate limit errors (HTTP 429).

    Args:
        func: Zero-argument callable that performs the yfinance API call
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Base delay in seconds, doubled each retry (default: 2.0)

    Returns:
        The result of the callable

    Raises:
        The last exception if all retries are exhausted
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "too many requests" in err_str or "429" in err_str or "rate" in err_str
            if not is_rate_limit or attempt == max_retries:
                raise
            last_exc = e
            delay = base_delay * (2 ** attempt)
            logger.warning(
                f"yfinance rate limited (attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {delay:.1f}s: {e}"
            )
            time.sleep(delay)
    raise last_exc


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
            
        except ValueError as e:
            logger.warning(f"{provider_name}.{func.__name__}: {e}")
            raise
        except Exception as e:
            logger.error(f"{provider_name}.{func.__name__} failed with error: {e}", exc_info=True)
            raise
    
    return wrapper


def validate_date_range(
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    lookback_days: Optional[int] = None,
    max_days: Optional[int] = None
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Validate and normalize date range parameters with intelligent optional handling.
    
    OPTIONAL DATE LOGIC:
    - If both start_date and end_date are provided: use them as-is
    - If end_date is None or empty string: use current date as end_date
    - If start_date is None or empty string:
        * If lookback_days provided: start_date = end_date - lookback_days
        * Otherwise: use default 30-day lookback
    - If both start_date and lookback_days provided (no end_date): 
        * end_date = start_date + lookback_days
    
    Treats empty strings as None for convenience.
    
    Args:
        start_date: Start date (optional) - if None/empty, calculated from lookback_days
        end_date: End date (optional) - if None/empty, defaults to current date
        lookback_days: Days to look back (optional) - used to calculate missing dates
        max_days: Maximum allowed days in range (optional)
    
    Returns:
        Tuple of (start_date, end_date) normalized to UTC timezone
        
    Raises:
        ValueError: If dates are invalid or range exceeds max_days
        
    Example:
        >>> # Provide end_date only - uses end_date - 30 days
        >>> start, end = validate_date_range(None, datetime(2025, 1, 31))
        
        >>> # Provide start_date and lookback - calculates end_date
        >>> start, end = validate_date_range(datetime(2025, 1, 1), None, lookback_days=30)
        
        >>> # Provide both - uses both directly
        >>> start, end = validate_date_range(
        ...     datetime(2025, 1, 1),
        ...     datetime(2025, 1, 31),
        ... )
    """
    # Treat empty strings as None
    if isinstance(start_date, str) and not start_date.strip():
        start_date = None
    if isinstance(end_date, str) and not end_date.strip():
        end_date = None
    
    # Default lookback if not specified
    if lookback_days is None:
        lookback_days = 30
    
    # Get current time in UTC
    now = datetime.now(timezone.utc)
    
    # Handle case where both dates are missing
    if start_date is None and end_date is None:
        end_date = now
        start_date = end_date - timedelta(days=lookback_days)
    
    # Handle case where only end_date is missing
    elif end_date is None:
        # If start_date is provided, calculate end_date from start + lookback_days
        if start_date is not None:
            end_date = start_date + timedelta(days=lookback_days)
        else:
            # Both must have been None at start (handled above), but just in case
            end_date = now
            start_date = end_date - timedelta(days=lookback_days)
    
    # Handle case where only start_date is missing
    elif start_date is None:
        start_date = end_date - timedelta(days=lookback_days)
    
    # Normalize to UTC
    if start_date and start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    
    # Validate order
    if start_date and end_date and start_date > end_date:
        raise ValueError(f"start_date ({start_date}) must be before end_date ({end_date})")
    
    # Validate future dates
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

"""
Performance Logger - Track rendering and operation times across UI components.

Provides consistent logging format for measuring:
- Page render times
- Table operations (load, filter, sort, paginate)
- Component lifecycle events
- Data fetching operations

Log format: [PERF:{category}:{operation}] {component} - {message} ({duration}ms)

Usage:
    from ba2_trade_platform.ui.utils.perf_logger import PerfLogger, perf_timer
    
    # Context manager
    with PerfLogger.timer("page", "render", "ActivityMonitor"):
        render_page()
    
    # Decorator
    @perf_timer("table", "load_data")
    async def load_data():
        ...
    
    # Manual timing
    timer = PerfLogger.start("table", "filter", "TransactionsTable")
    # ... do work ...
    timer.stop()
"""

import time
import functools
import asyncio
from typing import Optional, Callable, Any
from contextlib import contextmanager
from dataclasses import dataclass
from ...logger import logger


@dataclass
class TimerResult:
    """Result of a timed operation."""
    category: str
    operation: str
    component: str
    duration_ms: float
    details: Optional[str] = None
    
    def __str__(self) -> str:
        base = f"[PERF:{self.category}:{self.operation}] {self.component} - {self.duration_ms:.2f}ms"
        if self.details:
            base += f" ({self.details})"
        return base


class ActiveTimer:
    """An active timer that can be stopped manually."""
    
    def __init__(self, category: str, operation: str, component: str):
        self.category = category
        self.operation = operation
        self.component = component
        self.start_time = time.perf_counter()
        self._stopped = False
    
    def stop(self, details: Optional[str] = None) -> TimerResult:
        """Stop the timer and log the result."""
        if self._stopped:
            return None
        
        self._stopped = True
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        
        result = TimerResult(
            category=self.category,
            operation=self.operation,
            component=self.component,
            duration_ms=duration_ms,
            details=details
        )
        
        # Log based on duration - warning if slow
        if duration_ms > 1000:
            logger.warning(str(result))
        elif duration_ms > 100:
            logger.info(str(result))
        else:
            logger.debug(str(result))
        
        return result


class PerfLogger:
    """Performance logging utility for UI components."""
    
    # Categories
    PAGE = "page"
    TABLE = "table"
    COMPONENT = "component"
    DATA = "data"
    
    # Operations
    RENDER = "render"
    LOAD = "load"
    FILTER = "filter"
    SORT = "sort"
    PAGINATE = "paginate"
    SELECT = "select"
    REFRESH = "refresh"
    FETCH = "fetch"
    FORMAT = "format"
    
    @staticmethod
    def start(category: str, operation: str, component: str) -> ActiveTimer:
        """
        Start a manual timer.
        
        Args:
            category: Category (page, table, component, data)
            operation: Operation type (render, load, filter, etc.)
            component: Component name
            
        Returns:
            ActiveTimer that can be stopped with .stop()
        """
        return ActiveTimer(category, operation, component)
    
    @staticmethod
    @contextmanager
    def timer(category: str, operation: str, component: str, details: Optional[str] = None):
        """
        Context manager for timing operations.
        
        Args:
            category: Category (page, table, component, data)
            operation: Operation type (render, load, filter, etc.)
            component: Component name
            details: Optional additional details
            
        Example:
            with PerfLogger.timer("page", "render", "ActivityMonitor"):
                render_page()
        """
        active_timer = ActiveTimer(category, operation, component)
        try:
            yield active_timer
        finally:
            active_timer.stop(details)
    
    @staticmethod
    def log_operation(
        category: str,
        operation: str,
        component: str,
        duration_ms: float,
        details: Optional[str] = None
    ):
        """
        Log an operation with known duration.
        
        Args:
            category: Category (page, table, component, data)
            operation: Operation type
            component: Component name
            duration_ms: Duration in milliseconds
            details: Optional additional details
        """
        result = TimerResult(
            category=category,
            operation=operation,
            component=component,
            duration_ms=duration_ms,
            details=details
        )
        
        if duration_ms > 1000:
            logger.warning(str(result))
        elif duration_ms > 100:
            logger.info(str(result))
        else:
            logger.debug(str(result))


def perf_timer(category: str, operation: str, component: Optional[str] = None):
    """
    Decorator for timing functions.
    
    Args:
        category: Category (page, table, component, data)
        operation: Operation type
        component: Component name (defaults to function name)
        
    Example:
        @perf_timer("data", "fetch", "ActivityLog")
        async def fetch_activities():
            ...
    """
    def decorator(func: Callable) -> Callable:
        comp_name = component or func.__name__
        
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                with PerfLogger.timer(category, operation, comp_name):
                    return await func(*args, **kwargs)
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                with PerfLogger.timer(category, operation, comp_name):
                    return func(*args, **kwargs)
            return sync_wrapper
    
    return decorator


# Convenience decorators for common patterns
def page_render(component: str):
    """Decorator for page render methods."""
    return perf_timer(PerfLogger.PAGE, PerfLogger.RENDER, component)


def table_load(component: str):
    """Decorator for table data loading."""
    return perf_timer(PerfLogger.TABLE, PerfLogger.LOAD, component)


def data_fetch(component: str):
    """Decorator for data fetching operations."""
    return perf_timer(PerfLogger.DATA, PerfLogger.FETCH, component)

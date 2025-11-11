"""
TableCacheManager - Intelligent data caching and async loading for NiceGUI tables.

Provides a reusable pattern for:
1. Detecting filter state changes via tuple hashing
2. Lazy-loading data with async/await
3. In-memory pagination (O(1) vs SQL OFFSET O(n))
4. Progressive row rendering for large datasets
5. Automatic cache invalidation on filter changes

Usage:
    cache_mgr = TableCacheManager()
    
    # Initial load with loading indicator
    async def load_data():
        data, total = await cache_mgr.get_data(
            fetch_func=lambda: db.query(...),
            filter_state=get_filter_state(),
            format_func=lambda rows: format_rows(rows)
        )
        table.rows = data
        total_records = total
    
    # On filter change, cache is automatically invalidated
    def on_filter_change():
        cache_mgr.invalidate_cache()
        load_data()
"""

from typing import Callable, Any, Tuple, List, Optional
import asyncio
from nicegui import ui
from ...logger import logger


class TableCacheManager:
    """Manages intelligent data caching and lazy loading for UI tables."""
    
    def __init__(self, cache_name: str = "table"):
        """Initialize cache manager.
        
        Args:
            cache_name: Identifier for logging purposes
        """
        self.cache_name = cache_name
        self.cached_data = []
        self.cache_valid = False
        self.last_filter_state = None
        self.is_loading = False
        self.load_error = None
        
    def _get_filter_state_tuple(self, filter_state: dict | tuple) -> tuple:
        """Convert filter state to hashable tuple for comparison.
        
        Args:
            filter_state: Dictionary or tuple of filter values
            
        Returns:
            Hashable tuple representation of filter state
        """
        if isinstance(filter_state, tuple):
            return filter_state
        elif isinstance(filter_state, dict):
            # Sort by key to ensure consistent ordering
            return tuple(sorted(filter_state.items()))
        else:
            return (filter_state,)
    
    def should_invalidate_cache(self, filter_state: dict | tuple) -> bool:
        """Check if cache should be invalidated due to filter change.
        
        Args:
            filter_state: Current filter state
            
        Returns:
            True if filters changed and cache should be invalidated
        """
        current_filter_tuple = self._get_filter_state_tuple(filter_state)
        if self.last_filter_state != current_filter_tuple:
            return True
        return False
    
    def invalidate_cache(self) -> None:
        """Clear cache and mark as invalid."""
        self.cached_data = []
        self.cache_valid = False
        self.last_filter_state = None
        logger.debug(f"[{self.cache_name}] Cache invalidated")
    
    async def get_data(
        self,
        fetch_func: Callable[[], List[Any]],
        filter_state: dict | tuple,
        format_func: Optional[Callable[[List[Any]], List[dict]]] = None,
        page: int = 1,
        page_size: int = 25
    ) -> Tuple[List[dict], int]:
        """Fetch data with intelligent caching and pagination.
        
        Returns cached data if filters unchanged, otherwise fetches fresh data.
        Applies pagination to cached dataset.
        
        Args:
            fetch_func: Async or sync function that fetches raw data from DB
            filter_state: Current filter state (dict or tuple)
            format_func: Optional function to format/transform raw data
            page: Page number (1-indexed)
            page_size: Rows per page
            
        Returns:
            Tuple of (paginated_rows, total_count)
        """
        try:
            self.is_loading = True
            self.load_error = None
            
            current_filter_tuple = self._get_filter_state_tuple(filter_state)
            
            # Check if we need to fetch fresh data
            should_fetch = not self.cache_valid or self.should_invalidate_cache(filter_state)
            
            if should_fetch:
                logger.debug(f"[{self.cache_name}] Fetching fresh data (cache invalid or filters changed)")
                
                # Execute fetch function (could be async or sync)
                if asyncio.iscoroutinefunction(fetch_func):
                    raw_data = await fetch_func()
                else:
                    raw_data = fetch_func()
                
                # Format data if formatter provided
                if format_func:
                    if asyncio.iscoroutinefunction(format_func):
                        self.cached_data = await format_func(raw_data)
                    else:
                        self.cached_data = format_func(raw_data)
                else:
                    self.cached_data = raw_data if isinstance(raw_data, list) else []
                
                # Update cache state
                self.cache_valid = True
                self.last_filter_state = current_filter_tuple
                logger.debug(f"[{self.cache_name}] Cached {len(self.cached_data)} rows")
            else:
                pass
                #logger.debug(f"[{self.cache_name}] Using cached data ({len(self.cached_data)} rows)")
            
            # Apply in-memory pagination to cached data
            total_count = len(self.cached_data)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            paginated_data = self.cached_data[start_idx:end_idx]
            
            return paginated_data, total_count
            
        except Exception as e:
            self.load_error = str(e)
            logger.error(f"[{self.cache_name}] Error loading data: {e}", exc_info=True)
            return [], 0
        finally:
            self.is_loading = False
    
    async def get_data_async_with_ui(
        self,
        fetch_func: Callable[[], Any],
        filter_state: dict | tuple,
        format_func: Optional[Callable[[List[Any]], List[dict]]] = None,
        page: int = 1,
        page_size: int = 25,
        loading_container: Optional[Any] = None
    ) -> Tuple[List[dict], int]:
        """Fetch data with async loading and UI feedback.
        
        Shows loading spinner and handles errors gracefully.
        
        Args:
            fetch_func: Function that fetches raw data from DB
            filter_state: Current filter state
            format_func: Optional function to format/transform raw data
            page: Page number (1-indexed)
            page_size: Rows per page
            loading_container: Optional NiceGUI container to show loading indicator
            
        Returns:
            Tuple of (paginated_rows, total_count)
        """
        try:
            # Show loading indicator if container provided
            if loading_container:
                loading_container.clear()
                with loading_container:
                    ui.spinner('dots').set_visibility(True)
            
            # Fetch and cache data
            result = await self.get_data(
                fetch_func=fetch_func,
                filter_state=filter_state,
                format_func=format_func,
                page=page,
                page_size=page_size
            )
            
            # Hide loading indicator
            if loading_container:
                loading_container.clear()
            
            return result
            
        except Exception as e:
            logger.error(f"[{self.cache_name}] Error in async UI load: {e}", exc_info=True)
            if loading_container:
                loading_container.clear()
                with loading_container:
                    ui.label(f"Error loading data: {e}").classes("text-red-600")
            return [], 0


class AsyncTableLoader:
    """Helper for implementing async table loading with progressive rendering."""
    
    def __init__(self, table_element: Any, batch_size: int = 50):
        """Initialize async loader.
        
        Args:
            table_element: NiceGUI table element
            batch_size: Number of rows to load per batch for progressive rendering
        """
        self.table = table_element
        self.batch_size = batch_size
        self.load_task = None
    
    async def load_rows_async(
        self,
        rows: List[dict],
        render_immediately: bool = True
    ) -> None:
        """Load rows progressively to avoid UI freezing.
        
        For large datasets, loads rows in batches with UI updates.
        
        Args:
            rows: All rows to load
            render_immediately: If True, render first batch immediately
        """
        try:
            if not rows:
                self.table.rows = []
                return
            
            # For small datasets, load all at once
            if len(rows) <= self.batch_size:
                self.table.rows = rows
                return
            
            # For large datasets, load in batches
            logger.debug(f"[AsyncTableLoader] Loading {len(rows)} rows in batches of {self.batch_size}")
            
            loaded_rows = []
            
            # Load first batch immediately
            first_batch = rows[:self.batch_size]
            loaded_rows.extend(first_batch)
            self.table.rows = loaded_rows
            
            # Load remaining batches asynchronously
            for i in range(self.batch_size, len(rows), self.batch_size):
                batch = rows[i:i + self.batch_size]
                loaded_rows.extend(batch)
                self.table.rows = loaded_rows
                await asyncio.sleep(0.01)  # Small delay to allow UI updates
            
            logger.debug(f"[AsyncTableLoader] Finished loading all {len(rows)} rows")
            
        except Exception as e:
            logger.error(f"[AsyncTableLoader] Error loading rows: {e}", exc_info=True)
            self.table.rows = rows if isinstance(rows, list) else []

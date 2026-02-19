"""
LazyTable - A reusable table component with lazy loading, pagination, filtering and selection.

Features:
1. Lazy data loading with callback and loading animation
2. Server-side or client-side pagination 
3. Global and per-column filtering
4. Custom sorting with support for numeric values in formatted strings
5. Single/multi selection that works with pagination/lazy load

Usage:
    from ba2_trade_platform.ui.components.LazyTable import LazyTable, ColumnDef
    
    # Define columns
    columns = [
        ColumnDef(name='id', label='ID', field='id', sortable=True),
        ColumnDef(name='symbol', label='Symbol', field='symbol', sortable=True, filterable=True),
        ColumnDef(name='price', label='Price', field='price', sortable=True, 
                  sort_key='price_numeric'),  # Use numeric field for sorting
        ColumnDef(name='status', label='Status', field='status', filterable=True,
                  filter_options=['active', 'closed']),  # Dropdown filter
    ]
    
    # Data loader callback
    async def load_data(page: int, page_size: int, filters: dict, sort_by: str, descending: bool):
        # For server-side pagination, query with LIMIT/OFFSET
        # Return (rows, total_count)
        data = await fetch_from_db(...)
        return data, total
    
    # Create table
    table = LazyTable(
        columns=columns,
        data_loader=load_data,
        page_size=25,
        selection_mode='multi',  # 'none', 'single', 'multi'
        row_key='id'
    )
    
    # Render
    await table.render()
    
    # Get selected items
    selected = table.get_selected()
"""

from typing import Callable, Any, Dict, List, Optional, Literal, Union, Awaitable
from dataclasses import dataclass, field
import asyncio
import time
from nicegui import ui
from ...logger import logger
from ..utils.perf_logger import PerfLogger


@dataclass
class ColumnDef:
    """Column definition for LazyTable."""
    name: str  # Unique column identifier
    label: str  # Display label in header
    field: str  # Data field to display
    align: Literal['left', 'center', 'right'] = 'left'
    sortable: bool = False
    filterable: bool = False
    
    # For custom sorting on a different field (e.g., sort by numeric value while displaying formatted)
    sort_key: Optional[str] = None
    
    # For dropdown filter options (if None and filterable=True, uses text input)
    filter_options: Optional[List[str]] = None
    
    # Custom cell slot template (Quasar Vue template)
    slot_template: Optional[str] = None
    
    # Width override
    width: Optional[str] = None
    
    def to_quasar_column(self) -> dict:
        """Convert to Quasar table column format."""
        col = {
            'name': self.name,
            'label': self.label,
            'field': self.sort_key if self.sort_key else self.field,  # Use sort_key for sorting
            'align': self.align,
            'sortable': self.sortable,
        }
        if self.width:
            col['style'] = f'width: {self.width}'
        return col


@dataclass 
class LazyTableConfig:
    """Configuration for LazyTable behavior."""
    page_size: int = 25
    page_size_options: List[int] = field(default_factory=lambda: [10, 25, 50, 100])
    selection_mode: Literal['none', 'single', 'multi'] = 'none'
    row_key: str = 'id'
    show_global_filter: bool = True
    show_column_filters: bool = True
    show_loading_overlay: bool = True
    dense: bool = False
    flat: bool = True
    bordered: bool = True
    wrap_cells: bool = False  # Enable Quasar wrap-cells prop (overrides default nowrap)
    
    # Default sort
    default_sort_by: Optional[str] = None
    default_sort_descending: bool = False
    
    # Auto refresh interval in seconds (0 = disabled)
    auto_refresh_interval: int = 0
    
    # Table name for performance logging
    table_name: str = "LazyTable"


# Type alias for data loader callback
# Returns: (rows: List[dict], total_count: int)
DataLoaderCallback = Callable[
    [int, int, Dict[str, Any], Optional[str], bool],  # page, page_size, filters, sort_by, descending
    Union[tuple[List[dict], int], Awaitable[tuple[List[dict], int]]]
]


class LazyTable:
    """Reusable table component with lazy loading, pagination, filtering and selection."""
    
    def __init__(
        self,
        columns: List[ColumnDef],
        data_loader: DataLoaderCallback,
        config: Optional[LazyTableConfig] = None,
        on_row_click: Optional[Callable[[dict], None]] = None,
        on_selection_change: Optional[Callable[[List[dict]], None]] = None,
    ):
        """
        Initialize LazyTable.
        
        Args:
            columns: List of column definitions
            data_loader: Async callback to load data. Signature:
                         (page, page_size, filters, sort_by, descending) -> (rows, total_count)
            config: Table configuration options
            on_row_click: Callback when row is clicked
            on_selection_change: Callback when selection changes
        """
        self.columns = columns
        self.data_loader = data_loader
        self.config = config or LazyTableConfig()
        self.on_row_click = on_row_click
        self.on_selection_change = on_selection_change
        
        # State
        self._current_page = 1
        self._page_size = self.config.page_size
        self._total_count = 0
        self._sort_by = self.config.default_sort_by
        self._sort_descending = self.config.default_sort_descending
        self._global_filter = ""
        self._column_filters: Dict[str, Any] = {}
        self._selected_ids: set = set()  # Track selected by row_key
        self._is_loading = False
        self._rows: List[dict] = []
        
        # UI elements
        self._container: Optional[ui.column] = None
        self._table: Optional[ui.table] = None
        self._loading_spinner: Optional[ui.spinner] = None
        self._pagination_label: Optional[ui.label] = None
        self._global_filter_input: Optional[ui.input] = None
        
        # Auto refresh task
        self._refresh_task: Optional[asyncio.Task] = None
    
    @property
    def total_pages(self) -> int:
        """Calculate total pages."""
        return max(1, (self._total_count + self._page_size - 1) // self._page_size)
    
    def get_selected(self) -> List[dict]:
        """Get currently selected rows."""
        row_key = self.config.row_key
        return [row for row in self._rows if row.get(row_key) in self._selected_ids]
    
    def get_selected_ids(self) -> List[Any]:
        """Get IDs of currently selected rows."""
        return list(self._selected_ids)
    
    def clear_selection(self):
        """Clear all selections."""
        self._selected_ids.clear()
        if self._table:
            self._table.selected = []
            self._table.update()
        if self.on_selection_change:
            self.on_selection_change([])
    
    def select_all(self):
        """Select all visible rows."""
        if self.config.selection_mode == 'none':
            return
        
        row_key = self.config.row_key
        for row in self._rows:
            self._selected_ids.add(row.get(row_key))
        
        if self._table:
            self._table.selected = self._rows.copy()
            self._table.update()
        
        if self.on_selection_change:
            self.on_selection_change(self.get_selected())
    
    def _get_filters(self) -> Dict[str, Any]:
        """Get combined filter state."""
        filters = {}
        if self._global_filter:
            filters['_global'] = self._global_filter
        filters.update(self._column_filters)
        return filters
    
    async def refresh(self):
        """Refresh data from the data loader."""
        await self._load_data(operation="refresh")
    
    async def _load_data(self, operation: str = "load"):
        """Load data using the data loader callback."""
        if self._is_loading:
            return
        
        self._is_loading = True
        table_name = self.config.table_name
        load_timer = PerfLogger.start(PerfLogger.TABLE, operation, table_name)
        
        try:
            # Show loading state
            if self._loading_spinner:
                self._loading_spinner.set_visibility(True)
            
            # Get filter state
            filters = self._get_filters()
            
            # Time the data fetch separately
            fetch_start = time.perf_counter()
            
            # Call data loader
            if asyncio.iscoroutinefunction(self.data_loader):
                result = await self.data_loader(
                    self._current_page,
                    self._page_size,
                    filters,
                    self._sort_by,
                    self._sort_descending
                )
            else:
                result = await asyncio.to_thread(
                    self.data_loader,
                    self._current_page,
                    self._page_size,
                    filters,
                    self._sort_by,
                    self._sort_descending
                )
            
            fetch_duration = (time.perf_counter() - fetch_start) * 1000
            PerfLogger.log_operation(PerfLogger.DATA, PerfLogger.FETCH, table_name, fetch_duration,
                                     f"page={self._current_page}, size={self._page_size}")
            
            self._rows, self._total_count = result
            
            # Update table
            if self._table:
                # Mark selected rows
                row_key = self.config.row_key
                for row in self._rows:
                    row['_selected'] = row.get(row_key) in self._selected_ids
                
                self._table.rows = self._rows
                # Sync Quasar pagination state for server-side sorting indicators
                self._table.pagination = {
                    'rowsPerPage': 0,
                    'sortBy': self._sort_by,
                    'descending': self._sort_descending,
                    'rowsNumber': self._total_count,
                }
                self._table.update()
            
            # Update pagination label
            self._update_pagination_label()
            
            # Update pagination button states
            self._update_pagination_button_states()
            
            # Stop timer with details
            load_timer.stop(f"rows={len(self._rows)}, total={self._total_count}")
            
        except Exception as e:
            logger.error(f"[LazyTable] Error loading data: {e}", exc_info=True)
            load_timer.stop(f"error: {str(e)[:50]}")
            ui.notify(f"Error loading data: {str(e)}", type='negative')
        finally:
            self._is_loading = False
            if self._loading_spinner:
                self._loading_spinner.set_visibility(False)
    
    def _update_pagination_label(self):
        """Update the pagination info label."""
        if self._pagination_label:
            start = (self._current_page - 1) * self._page_size + 1
            end = min(self._current_page * self._page_size, self._total_count)
            if self._total_count == 0:
                self._pagination_label.text = "No records found"
            else:
                self._pagination_label.text = f"Showing {start}-{end} of {self._total_count}"
    
    def _update_pagination_button_states(self):
        """Update pagination button enabled/disabled states."""
        on_first_page = self._current_page <= 1
        on_last_page = self._current_page >= self.total_pages
        
        # First and previous buttons - disabled on first page
        if hasattr(self, '_first_page_btn') and self._first_page_btn:
            if on_first_page:
                self._first_page_btn.props('disable')
            else:
                self._first_page_btn.props(remove='disable')
        
        if hasattr(self, '_prev_page_btn') and self._prev_page_btn:
            if on_first_page:
                self._prev_page_btn.props('disable')
            else:
                self._prev_page_btn.props(remove='disable')
        
        # Next and last buttons - disabled on last page
        if hasattr(self, '_next_page_btn') and self._next_page_btn:
            if on_last_page:
                self._next_page_btn.props('disable')
            else:
                self._next_page_btn.props(remove='disable')
        
        if hasattr(self, '_last_page_btn') and self._last_page_btn:
            if on_last_page:
                self._last_page_btn.props('disable')
            else:
                self._last_page_btn.props(remove='disable')
        
        # Update page input value
        if hasattr(self, '_page_input') and self._page_input:
            self._page_input.value = self._current_page
        
        # Update total pages label
        if hasattr(self, '_total_pages_label') and self._total_pages_label:
            self._total_pages_label.text = f'/ {self.total_pages}'

    async def _on_page_change(self, page: int):
        """Handle page change."""
        if page < 1 or page > self.total_pages:
            return
        self._current_page = page
        await self._load_data(operation=PerfLogger.PAGINATE)
    
    async def _on_page_size_change(self, size: int):
        """Handle page size change."""
        self._page_size = size
        self._current_page = 1  # Reset to first page
        await self._load_data(operation=PerfLogger.PAGINATE)
    
    async def _on_sort_change(self, sort_by: Optional[str], descending: bool):
        """Handle sort change."""
        self._sort_by = sort_by
        self._sort_descending = descending
        self._current_page = 1  # Reset to first page
        await self._load_data(operation=PerfLogger.SORT)

    def _handle_sort_request(self, e):
        """Handle sort request event from Quasar table (server-side mode).

        Quasar emits 'request' with {pagination: {sortBy, descending, ...}, filter, ...}
        when in server-side mode (rowsNumber is set in pagination).
        """
        if hasattr(e, 'args') and e.args:
            props = e.args[0] if isinstance(e.args, (list, tuple)) else e.args
            if isinstance(props, dict):
                pagination = props.get('pagination', props)
            else:
                return
            sort_by = pagination.get('sortBy')
            descending = pagination.get('descending', False)
            asyncio.create_task(self._on_sort_change(sort_by, descending))

    async def _on_global_filter_change(self, value: str):
        """Handle global filter change."""
        self._global_filter = value
        self._current_page = 1
        await self._load_data(operation=PerfLogger.FILTER)
    
    async def _on_column_filter_change(self, column: str, value: Any):
        """Handle column filter change."""
        if value is None or value == "" or value == []:
            self._column_filters.pop(column, None)
        else:
            self._column_filters[column] = value
        self._current_page = 1
        await self._load_data(operation=PerfLogger.FILTER)
    
    def _on_selection_update(self, selected_rows: List[dict]):
        """Handle selection update from table."""
        row_key = self.config.row_key
        
        if self.config.selection_mode == 'single':
            self._selected_ids.clear()
            if selected_rows:
                self._selected_ids.add(selected_rows[0].get(row_key))
        else:
            # For multi-select, track across pages
            current_page_ids = {row.get(row_key) for row in self._rows}
            selected_page_ids = {row.get(row_key) for row in selected_rows}
            
            # Remove deselected items from current page
            for row_id in current_page_ids:
                if row_id not in selected_page_ids:
                    self._selected_ids.discard(row_id)
            
            # Add newly selected items
            self._selected_ids.update(selected_page_ids)
        
        if self.on_selection_change:
            self.on_selection_change(self.get_selected())
    
    def _render_column_filters(self) -> ui.row:
        """Render column filter inputs."""
        filter_row = ui.row().classes('w-full gap-2 flex-wrap items-end')
        
        with filter_row:
            for col in self.columns:
                if not col.filterable:
                    continue
                
                with ui.column().classes('gap-1'):
                    ui.label(col.label).classes('text-xs text-gray-600')
                    
                    if col.filter_options:
                        # Dropdown filter
                        select = ui.select(
                            options=[{'label': 'All', 'value': None}] + 
                                    [{'label': opt, 'value': opt} for opt in col.filter_options],
                            value=None,
                            on_change=lambda e, c=col.name: asyncio.create_task(
                                self._on_column_filter_change(c, e.value)
                            )
                        ).classes('w-32')
                    else:
                        # Text filter
                        text_input = ui.input(
                            placeholder=f'Filter {col.label}...'
                        ).classes('w-32').on(
                            'keyup.enter',
                            lambda e, c=col.name: asyncio.create_task(
                                self._on_column_filter_change(c, e.sender.value)
                            )
                        )
                        # Also filter on blur
                        text_input.on(
                            'blur',
                            lambda e, c=col.name: asyncio.create_task(
                                self._on_column_filter_change(c, e.sender.value)
                            )
                        )
        
        return filter_row
    
    def _render_pagination_controls(self) -> ui.row:
        """Render pagination controls."""
        controls = ui.row().classes('w-full items-center justify-between py-2')
        
        with controls:
            # Left side: page size selector
            with ui.row().classes('items-center gap-2'):
                ui.label('Rows per page:').classes('text-sm')
                ui.select(
                    options=self.config.page_size_options,
                    value=self._page_size,
                    on_change=lambda e: asyncio.create_task(self._on_page_size_change(e.value))
                ).classes('w-20')
            
            # Center: pagination info
            self._pagination_label = ui.label('Loading...').classes('text-sm text-gray-600')
            
            # Right side: page navigation
            with ui.row().classes('items-center gap-1'):
                self._first_page_btn = ui.button(
                    icon='first_page',
                    on_click=lambda: asyncio.create_task(self._on_page_change(1))
                ).props('flat dense')
                
                self._prev_page_btn = ui.button(
                    icon='chevron_left',
                    on_click=lambda: asyncio.create_task(self._on_page_change(self._current_page - 1))
                ).props('flat dense')
                
                # Page input
                self._page_input = ui.number(
                    value=self._current_page,
                    min=1,
                    format='%d'
                ).classes('w-16').on(
                    'keyup.enter',
                    lambda e: asyncio.create_task(self._on_page_change(int(e.sender.value)))
                )
                
                self._total_pages_label = ui.label(f'/ {self.total_pages}').classes('text-sm')
                
                self._next_page_btn = ui.button(
                    icon='chevron_right',
                    on_click=lambda: asyncio.create_task(self._on_page_change(self._current_page + 1))
                ).props('flat dense')
                
                self._last_page_btn = ui.button(
                    icon='last_page',
                    on_click=lambda: asyncio.create_task(self._on_page_change(self.total_pages))
                ).props('flat dense')
                
                # Update button states initially
                self._update_pagination_button_states()
        
        return controls
    
    async def render(self) -> ui.column:
        """
        Render the table component.
        
        Returns:
            The container element
        """
        table_name = self.config.table_name
        render_timer = PerfLogger.start(PerfLogger.TABLE, PerfLogger.RENDER, table_name)
        
        self._container = ui.column().classes('w-full')
        
        with self._container:
            # Top toolbar with filters
            with ui.row().classes('w-full items-center gap-4 mb-2'):
                # Global filter
                if self.config.show_global_filter:
                    self._global_filter_input = ui.input(
                        placeholder='Search all columns...'
                    ).classes('flex-grow').on(
                        'keyup.enter',
                        lambda e: asyncio.create_task(self._on_global_filter_change(e.sender.value))
                    )
                    self._global_filter_input.on(
                        'blur',
                        lambda e: asyncio.create_task(self._on_global_filter_change(e.sender.value))
                    )
                
                # Refresh button
                ui.button(
                    icon='refresh',
                    on_click=lambda: asyncio.create_task(self.refresh())
                ).props('flat')
                
                # Loading spinner
                self._loading_spinner = ui.spinner('dots').set_visibility(False)
            
            # Column filters
            if self.config.show_column_filters and any(c.filterable for c in self.columns):
                self._render_column_filters()
            
            # Selection info bar (for multi-select)
            if self.config.selection_mode == 'multi':
                with ui.row().classes('w-full items-center gap-2 py-1'):
                    ui.button(
                        'Select All Page',
                        on_click=self.select_all
                    ).props('flat dense size=sm')
                    ui.button(
                        'Clear Selection',
                        on_click=self.clear_selection
                    ).props('flat dense size=sm')
                    ui.label().bind_text_from(
                        self._selected_ids, '__len__',
                        backward=lambda: f'{len(self._selected_ids)} selected'
                    ).classes('text-sm text-gray-600 ml-auto')
            
            # Quasar columns
            quasar_columns = [col.to_quasar_column() for col in self.columns]
            
            # Table props
            table_props = 'flat bordered' if self.config.flat and self.config.bordered else ''
            if self.config.dense:
                table_props += ' dense'
            if self.config.wrap_cells:
                table_props += ' wrap-cells'
            
            # Selection prop
            selection_prop = None
            if self.config.selection_mode == 'single':
                selection_prop = 'single'
            elif self.config.selection_mode == 'multi':
                selection_prop = 'multiple'
            
            # Create table with server-side sorting mode
            # rowsNumber enables Quasar server-side mode (emits 'request' events
            # instead of sorting client-side on visible rows only)
            self._table = ui.table(
                columns=quasar_columns,
                rows=[],
                row_key=self.config.row_key,
                selection=selection_prop,
                pagination={
                    'rowsPerPage': 0,
                    'sortBy': self._sort_by,
                    'descending': self._sort_descending,
                    'rowsNumber': 0,
                }
            ).classes('w-full')
            
            # Always hide Quasar's built-in pagination (we use our own controls)
            table_props += ' hide-pagination'
            self._table.props(table_props)
            
            # Add custom cell slots
            for col in self.columns:
                if col.slot_template:
                    self._table.add_slot(f'body-cell-{col.name}', col.slot_template)
                # If we have a sort_key different from field, add display slot
                elif col.sort_key and col.sort_key != col.field:
                    # Display the original field but sort by sort_key
                    self._table.add_slot(f'body-cell-{col.name}', f'''
                        <q-td :props="props">
                            {{{{ props.row.{col.field} }}}}
                        </q-td>
                    ''')
            
            # Handle row click
            if self.on_row_click:
                self._table.on('row-click', lambda e: self.on_row_click(e.args[1]))
            
            # Handle selection change
            if self.config.selection_mode != 'none':
                self._table.on('selection', lambda e: self._on_selection_update(e.args[1]))
            
            # Handle sort change from Quasar table (server-side mode)
            self._table.on('request', self._handle_sort_request)
            
            # Pagination controls
            self._render_pagination_controls()
        
        # Initial data load
        await self._load_data()
        
        # Start auto-refresh if configured
        if self.config.auto_refresh_interval > 0:
            self._start_auto_refresh()
        
        render_timer.stop(f"columns={len(self.columns)}")
        return self._container
    
    def _start_auto_refresh(self):
        """Start auto-refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            return
        
        async def refresh_loop():
            while True:
                await asyncio.sleep(self.config.auto_refresh_interval)
                try:
                    await self._load_data()
                except Exception as e:
                    logger.warning(f"[LazyTable] Auto-refresh error: {e}")
        
        self._refresh_task = asyncio.create_task(refresh_loop())
    
    def stop_auto_refresh(self):
        """Stop auto-refresh task."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
    
    def destroy(self):
        """Clean up resources."""
        self.stop_auto_refresh()
    
    @property
    def table(self) -> Optional[ui.table]:
        """Access the underlying NiceGUI table for custom slots and events."""
        return self._table
    
    @property
    def rows(self) -> List[dict]:
        """Get current rows."""
        return self._rows
    
    @property
    def total_count(self) -> int:
        """Get total record count."""
        return self._total_count
    
    def add_slot(self, name: str, template: str):
        """
        Add a custom slot to the table.
        
        Args:
            name: Slot name (e.g., 'body-cell-status')
            template: Vue/Quasar template string
        """
        if self._table:
            self._table.add_slot(name, template)
    
    def on_event(self, event_name: str, handler: Callable):
        """
        Register an event handler on the table.
        
        Args:
            event_name: Event name (e.g., 'edit', 'delete')
            handler: Callback function
        """
        if self._table:
            self._table.on(event_name, handler)
    
    def props(self, props_string: str):
        """
        Add Quasar props to the table.
        
        Args:
            props_string: Props string (e.g., 'dense flat')
        """
        if self._table:
            self._table.props(props_string)


# Convenience function for simple client-side tables
def create_simple_table(
    columns: List[ColumnDef],
    rows: List[dict],
    config: Optional[LazyTableConfig] = None,
    **kwargs
) -> LazyTable:
    """
    Create a simple table with client-side data (no lazy loading).
    
    This is useful for smaller datasets where all data is available upfront.
    Filtering, sorting, and pagination happen client-side.
    
    Args:
        columns: Column definitions
        rows: All data rows
        config: Optional configuration
        **kwargs: Additional arguments passed to LazyTable
    
    Returns:
        LazyTable instance
    """
    # Store all rows for client-side operations
    all_rows = rows.copy()
    
    def client_side_loader(
        page: int,
        page_size: int,
        filters: Dict[str, Any],
        sort_by: Optional[str],
        descending: bool
    ) -> tuple[List[dict], int]:
        """Client-side data loader with filtering, sorting, pagination."""
        result = all_rows.copy()
        
        # Apply global filter
        global_filter = filters.get('_global', '').lower()
        if global_filter:
            result = [
                row for row in result
                if any(
                    global_filter in str(v).lower()
                    for v in row.values()
                )
            ]
        
        # Apply column filters
        for col_name, filter_value in filters.items():
            if col_name == '_global':
                continue
            if filter_value:
                filter_lower = str(filter_value).lower()
                result = [
                    row for row in result
                    if filter_lower in str(row.get(col_name, '')).lower()
                ]
        
        # Apply sorting
        if sort_by:
            try:
                result.sort(
                    key=lambda r: (r.get(sort_by) is None, r.get(sort_by)),
                    reverse=descending
                )
            except TypeError:
                # Mixed types, try string sort
                result.sort(
                    key=lambda r: str(r.get(sort_by, '')),
                    reverse=descending
                )
        
        # Get total before pagination
        total = len(result)
        
        # Apply pagination
        start = (page - 1) * page_size
        end = start + page_size
        paginated = result[start:end]
        
        return paginated, total
    
    return LazyTable(
        columns=columns,
        data_loader=client_side_loader,
        config=config,
        **kwargs
    )

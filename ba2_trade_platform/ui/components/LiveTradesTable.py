"""
LiveTradesTable - Specialized table component for displaying transactions with expansion and actions.

Extends LazyTable with:
- Row expansion for showing related orders
- Custom selection with checkbox column
- Action buttons (edit, close, retry)
- Status badges with colors
- P/L formatting with colors

Usage:
    from ba2_trade_platform.ui.components import LiveTradesTable
    
    table = LiveTradesTable(
        data_loader=load_transactions,
        on_edit=handle_edit,
        on_close=handle_close,
        on_retry_close=handle_retry,
        on_recreate_tpsl=handle_recreate,
        on_view_recommendation=handle_view_rec
    )
    await table.render()
"""

from typing import Callable, Any, Dict, List, Optional, Awaitable, Union
import asyncio
import time
from nicegui import ui
from dataclasses import dataclass, field

from .LazyTable import LazyTable, ColumnDef, LazyTableConfig, DataLoaderCallback
from ...logger import logger
from ..utils.perf_logger import PerfLogger


@dataclass
class LiveTradesTableConfig(LazyTableConfig):
    """Configuration for LiveTradesTable."""
    page_size: int = 20
    page_size_options: List[int] = field(default_factory=lambda: [10, 20, 50, 100])
    selection_mode: str = 'none'  # We handle selection manually
    row_key: str = 'id'
    show_global_filter: bool = True
    show_column_filters: bool = False
    table_name: str = "LiveTradesTable"
    
    # Specific to LiveTradesTable
    show_expansion: bool = True
    show_actions: bool = True
    show_selection: bool = True


class LiveTradesTable(LazyTable):
    """
    Specialized table for displaying live transactions with expansion and actions.
    
    Features:
    - Row expansion showing related orders
    - Checkbox selection for batch operations
    - Action buttons (edit TP/SL, close, retry close, recreate TP/SL)
    - Status badges with color coding
    - P/L columns with green/red formatting
    """
    
    # Column definitions for transactions
    TRANSACTION_COLUMNS = [
        ColumnDef(name='select', label='', field='select', align='left', sortable=False),
        ColumnDef(name='expand', label='', field='expand', align='left', sortable=False),
        ColumnDef(name='id', label='ID', field='id', align='center', sortable=True),
        ColumnDef(name='account', label='Account', field='account_name', align='left', sortable=True),
        ColumnDef(name='symbol', label='Symbol', field='symbol', align='left', sortable=True),
        ColumnDef(name='direction', label='Direction', field='direction', align='center', sortable=True),
        ColumnDef(name='expert', label='Expert', field='expert', align='left', sortable=True),
        ColumnDef(name='quantity', label='Qty', field='quantity', align='right', sortable=True),
        ColumnDef(name='open_price', label='Open Price', field='open_price', align='right', sortable=True),
        ColumnDef(name='current_price', label='Current', field='current_price', align='right'),
        ColumnDef(name='value', label='Value', field='value', align='right', sortable=True),
        ColumnDef(name='close_price', label='Close Price', field='close_price', align='right'),
        ColumnDef(name='take_profit', label='TP', field='take_profit', align='right'),
        ColumnDef(name='stop_loss', label='SL', field='stop_loss', align='right'),
        ColumnDef(name='current_pnl', label='Current P/L', field='current_pnl_numeric', align='right', sortable=True),
        ColumnDef(name='closed_pnl', label='Closed P/L', field='closed_pnl_numeric', align='right', sortable=True),
        ColumnDef(name='status', label='Status', field='status', align='center', sortable=True),
        ColumnDef(name='order_count', label='Orders', field='order_count', align='center'),
        ColumnDef(name='created_at', label='Created', field='created_at', align='left', sortable=True),
        ColumnDef(name='closed_at', label='Closed', field='closed_at', align='left', sortable=True),
        ColumnDef(name='actions', label='Actions', field='actions', align='center'),
    ]
    
    # Vue template for the table body with expansion
    BODY_TEMPLATE = '''
        <q-tr :props="props">
            <q-td v-for="col in props.cols" :key="col.name" :props="props">
                <template v-if="col.name === 'select'">
                    <q-checkbox
                        :model-value="props.row._selected || false"
                        @update:model-value="(val) => $parent.$emit('toggle_selection', props.row.id)"
                    />
                </template>
                <template v-else-if="col.name === 'expand'">
                    <q-btn
                        size="sm"
                        color="primary"
                        round
                        dense
                        @click="props.expand = !props.expand"
                        :icon="props.expand ? 'expand_less' : 'expand_more'"
                    />
                </template>
                <template v-else-if="col.name === 'direction'">
                    <q-badge :color="col.value === 'BUY' ? 'positive' : 'negative'" :label="col.value" />
                </template>
                <template v-else-if="col.name === 'status'">
                    <q-badge :color="props.row.status_color" :label="col.value" />
                </template>
                <template v-else-if="col.name === 'current_pnl'">
                    <span :class="props.row.current_pnl_numeric > 0 ? 'number-positive font-bold' : props.row.current_pnl_numeric < 0 ? 'number-negative font-bold' : ''">
                        {{ props.row.current_pnl }}
                    </span>
                </template>
                <template v-else-if="col.name === 'closed_pnl'">
                    <span :class="props.row.closed_pnl_numeric > 0 ? 'number-positive font-bold' : props.row.closed_pnl_numeric < 0 ? 'number-negative font-bold' : ''">
                        {{ props.row.closed_pnl }}
                    </span>
                </template>
                <template v-else-if="col.name === 'actions'">
                    <q-btn icon="search"
                           size="sm"
                           flat
                           round
                           color="blue-grey"
                           @click="$parent.$emit('view_transaction_details', props.row.id)"
                           title="View Transaction Details"
                    >
                        <q-tooltip>View Transaction Details</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.has_missing_tpsl_orders"
                           icon="warning"
                           size="sm"
                           flat
                           round
                           color="warning"
                           @click="$parent.$emit('recreate_tpsl', props.row.id)"
                           title="TP/SL defined but no valid orders - Click to recreate"
                    >
                        <q-tooltip>TP/SL defined but no valid orders - Click to recreate</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.is_open"
                           icon="edit"
                           size="sm"
                           flat
                           round
                           color="primary"
                           @click="$parent.$emit('edit_transaction', props.row.id)"
                           title="Adjust TP/SL"
                    />
                    <q-btn v-if="(props.row.is_open || props.row.is_waiting) && !props.row.is_closing"
                           icon="close"
                           size="sm"
                           flat
                           round
                           color="negative"
                           @click="$parent.$emit('close_transaction', props.row.id)"
                           :title="props.row.is_waiting ? 'Cancel Orders' : 'Close Position'"
                    />
                    <q-btn v-else-if="props.row.is_closing"
                           icon="refresh"
                           size="sm"
                           flat
                           round
                           color="orange"
                           @click="$parent.$emit('retry_close', props.row.id)"
                           title="Retry Close (reset status and try again)"
                    />
                    <span v-if="!props.row.is_open && !props.row.is_waiting && !props.row.is_closing && !props.row.has_missing_tpsl_orders" class="text-grey-5">—</span>
                </template>
                <template v-else>
                    {{ col.value }}
                </template>
            </q-td>
        </q-tr>
        <q-tr v-show="props.expand" :props="props" class="bg-white/5">
            <q-td colspan="100%">
                <div class="q-pa-md">
                    <div class="text-subtitle2 q-mb-sm text-accent">📋 Related Orders ({{ props.row.order_count }})</div>
                    <q-markup-table flat bordered dense v-if="props.row.orders && props.row.orders.length > 0" class="bg-transparent">
                        <thead>
                            <tr class="bg-white/10">
                                <th class="text-left">ID</th>
                                <th class="text-left">Category</th>
                                <th class="text-left">Type</th>
                                <th class="text-left">Side</th>
                                <th class="text-right">Quantity</th>
                                <th class="text-right">Filled</th>
                                <th class="text-right">Limit Price</th>
                                <th class="text-right">Stop Price</th>
                                <th class="text-center">Status</th>
                                <th class="text-left">Broker ID</th>
                                <th class="text-left">Created</th>
                                <th class="text-left">Comment</th>
                                <th class="text-center">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="order in props.row.orders" :key="order.id">
                                <td class="text-left">{{ order.id }}</td>
                                <td class="text-left">
                                    <q-badge :color="order.category === 'Entry' ? 'blue' : order.category === 'Take Profit' ? 'green' : order.category === 'Stop Loss' ? 'red' : 'grey'"
                                             :label="order.category" />
                                </td>
                                <td class="text-left">{{ order.type }}</td>
                                <td class="text-left">
                                    <q-badge :color="order.side === 'BUY' ? 'positive' : 'negative'" :label="order.side" />
                                </td>
                                <td class="text-right">{{ order.quantity }}</td>
                                <td class="text-right">{{ order.filled_qty }}</td>
                                <td class="text-right">{{ order.limit_price }}</td>
                                <td class="text-right">{{ order.stop_price }}</td>
                                <td class="text-center">
                                    <q-badge :color="order.status_color" :label="order.status" />
                                </td>
                                <td class="text-left text-caption">{{ order.broker_order_id }}</td>
                                <td class="text-left">{{ order.created_at }}</td>
                                <td class="text-left text-caption">{{ order.comment }}</td>
                                <td class="text-center">
                                    <q-btn v-if="order.has_recommendation"
                                           icon="info"
                                           size="sm"
                                           flat
                                           round
                                           color="primary"
                                           @click="$parent.$emit('view_recommendation', order.expert_recommendation_id)"
                                           title="View Expert Recommendation"
                                    />
                                    <span v-else class="text-grey-5">—</span>
                                </td>
                            </tr>
                        </tbody>
                    </q-markup-table>
                    <div v-else class="text-grey-6 text-center q-pa-md">No orders found for this transaction</div>
                </div>
            </q-td>
        </q-tr>
    '''
    
    def __init__(
        self,
        data_loader: DataLoaderCallback,
        config: Optional[LiveTradesTableConfig] = None,
        on_edit: Optional[Callable[[int], None]] = None,
        on_close: Optional[Callable[[int], None]] = None,
        on_retry_close: Optional[Callable[[int], None]] = None,
        on_recreate_tpsl: Optional[Callable[[int], None]] = None,
        on_view_recommendation: Optional[Callable[[int], None]] = None,
        on_view_transaction_details: Optional[Callable[[int], None]] = None,
        on_selection_change: Optional[Callable[[List[int]], None]] = None,
    ):
        """
        Initialize LiveTradesTable.
        
        Args:
            data_loader: Callback to load transaction data
            config: Table configuration
            on_edit: Callback when edit button is clicked (receives transaction_id)
            on_close: Callback when close button is clicked (receives transaction_id)
            on_retry_close: Callback when retry close button is clicked (receives transaction_id)
            on_recreate_tpsl: Callback when recreate TP/SL button is clicked (receives transaction_id)
            on_view_recommendation: Callback when view recommendation button is clicked (receives rec_id)
            on_view_transaction_details: Callback when view transaction details button is clicked (receives transaction_id)
            on_selection_change: Callback when selection changes (receives list of selected ids)
        """
        self._config = config or LiveTradesTableConfig()
        
        # Action callbacks
        self.on_edit = on_edit
        self.on_close = on_close
        self.on_retry_close = on_retry_close
        self.on_recreate_tpsl = on_recreate_tpsl
        self.on_view_recommendation = on_view_recommendation
        self.on_view_transaction_details = on_view_transaction_details
        self._on_selection_change = on_selection_change
        
        # Selection tracking
        self._selected_ids: set = set()
        
        # Initialize parent class
        super().__init__(
            columns=self.TRANSACTION_COLUMNS,
            data_loader=data_loader,
            config=self._config,
            on_selection_change=None  # We handle selection ourselves
        )
    
    def get_selected_ids(self) -> List[int]:
        """Get IDs of currently selected transactions."""
        return list(self._selected_ids)
    
    def clear_selection(self):
        """Clear all selections."""
        self._selected_ids.clear()
        if self._table:
            for row in self._rows:
                row['_selected'] = False
            self._table.rows = self._rows
            self._table.update()
        if self._on_selection_change:
            self._on_selection_change([])
    
    def select_all_visible(self):
        """Select all visible rows."""
        row_key = self.config.row_key
        for row in self._rows:
            self._selected_ids.add(row.get(row_key))
            row['_selected'] = True
        if self._table:
            self._table.rows = self._rows
            self._table.update()
        if self._on_selection_change:
            self._on_selection_change(list(self._selected_ids))
    
    def _handle_toggle_selection(self, event_data):
        """Handle selection toggle event."""
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        if transaction_id in self._selected_ids:
            self._selected_ids.discard(transaction_id)
        else:
            self._selected_ids.add(transaction_id)
        
        # Update row state
        for row in self._rows:
            if row.get(self.config.row_key) == transaction_id:
                row['_selected'] = transaction_id in self._selected_ids
                break
        
        if self._table:
            self._table.rows = self._rows
            self._table.update()
        
        if self._on_selection_change:
            self._on_selection_change(list(self._selected_ids))
    
    def _setup_event_handlers(self):
        """Set up all event handlers for the table."""
        if not self._table:
            return
        
        # Selection toggle
        self._table.on('toggle_selection', self._handle_toggle_selection)
        
        # Action handlers
        if self.on_edit:
            self._table.on('edit_transaction', lambda e: self.on_edit(
                e.args if hasattr(e, 'args') else e
            ))
        
        if self.on_close:
            self._table.on('close_transaction', lambda e: self.on_close(
                e.args if hasattr(e, 'args') else e
            ))
        
        if self.on_retry_close:
            self._table.on('retry_close', lambda e: self.on_retry_close(
                e.args if hasattr(e, 'args') else e
            ))
        
        if self.on_recreate_tpsl:
            self._table.on('recreate_tpsl', lambda e: self.on_recreate_tpsl(
                e.args if hasattr(e, 'args') else e
            ))
        
        if self.on_view_recommendation:
            self._table.on('view_recommendation', lambda e: self.on_view_recommendation(
                e.args if hasattr(e, 'args') else e
            ))
        
        if self.on_view_transaction_details:
            self._table.on('view_transaction_details', lambda e: self.on_view_transaction_details(
                e.args if hasattr(e, 'args') else e
            ))
    
    async def render(self) -> ui.column:
        """
        Render the live trades table.
        
        Returns:
            The container element
        """
        table_name = self.config.table_name
        render_timer = PerfLogger.start(PerfLogger.TABLE, PerfLogger.RENDER, table_name)
        
        self._container = ui.column().classes('w-full')
        
        with self._container:
            # Top toolbar with filters and selection controls
            with ui.row().classes('w-full items-center gap-4 mb-2'):
                # Global filter
                if self.config.show_global_filter:
                    self._global_filter_input = ui.input(
                        placeholder='Search transactions...'
                    ).classes('flex-grow').on(
                        'keyup.enter',
                        lambda e: asyncio.create_task(self._on_global_filter_change(e.sender.value))
                    )
                    self._global_filter_input.on(
                        'blur',
                        lambda e: asyncio.create_task(self._on_global_filter_change(e.sender.value))
                    )
                
                # Selection controls
                if self._config.show_selection:
                    ui.button('Select All', on_click=self.select_all_visible).props('flat dense size=sm')
                    ui.button('Clear Selection', on_click=self.clear_selection).props('flat dense size=sm')
                    self._selection_label = ui.label('0 selected').classes('text-sm text-gray-600')
                
                # Refresh button
                ui.button(
                    icon='refresh',
                    on_click=lambda: asyncio.create_task(self.refresh())
                ).props('flat')
                
                # Loading spinner
                self._loading_spinner = ui.spinner('dots').set_visibility(False)
            
            # Quasar columns
            quasar_columns = [col.to_quasar_column() for col in self.columns]
            
            # Create table - hide built-in pagination since we use custom dark-themed controls
            self._table = ui.table(
                columns=quasar_columns,
                rows=[],
                row_key=self.config.row_key,
                pagination={'rowsPerPage': 0}  # Disable client-side pagination, we do server-side
            ).classes('w-full').props('flat bordered hide-pagination')
            
            # Add the body template for expansion and custom cells
            self._table.add_slot('body', self.BODY_TEMPLATE)
            
            # Setup event handlers
            self._setup_event_handlers()
            
            # Pagination controls
            self._render_pagination_controls()
        
        # Initial data load
        await self._load_data()
        
        # Update selection label if present
        if hasattr(self, '_selection_label'):
            self._update_selection_label()
        
        render_timer.stop(f"columns={len(self.columns)}")
        return self._container
    
    def _update_selection_label(self):
        """Update the selection count label."""
        if hasattr(self, '_selection_label'):
            count = len(self._selected_ids)
            self._selection_label.text = f'{count} selected'
    
    async def _load_data(self, operation: str = "load"):
        """Load data and preserve selection state."""
        await super()._load_data(operation)
        
        # Mark previously selected rows
        row_key = self.config.row_key
        for row in self._rows:
            row['_selected'] = row.get(row_key) in self._selected_ids
        
        if self._table:
            self._table.rows = self._rows
            self._table.update()
        
        # Update selection label
        if hasattr(self, '_selection_label'):
            self._update_selection_label()

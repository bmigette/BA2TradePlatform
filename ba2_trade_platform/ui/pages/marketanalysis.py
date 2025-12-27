from nicegui import ui
from datetime import datetime, timezone
from typing import List, Optional
import math
import asyncio

from ...core.WorkerQueue import get_worker_queue
from ...core.db import get_all_instances, get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput, ExpertRecommendation
from ...core.types import MarketAnalysisStatus, WorkerTaskStatus, OrderDirection, OrderRecommendation, OrderOpenType
from ...core.models import TradingOrder
from ...core.types import OrderStatus
from ...core.models import AccountDefinition
from ...modules.accounts import get_account_class
from ...core.db import add_instance
from ...logger import logger
from ...core.utils import get_account_instance_from_id
from ..components.MarketAnalysisDetailDialog import MarketAnalysisDetailDialog
from ..components.SmartRiskManagerDetailDialog import SmartRiskManagerDetailDialog
from sqlmodel import select, func, distinct


class JobMonitoringTab:
    def __init__(self):
        self.worker_queue = None  # Lazy initialization
        self.analysis_table = None
        self.refresh_timer = None
        self.pagination_container = None  # Container for pagination controls
        # Queue status labels for live updates
        self.queue_status_labels = {
            'worker_count': None,
            'running_tasks': None,
            'pending_tasks': None,
            'total_tasks': None,
            'persisted_pending': None,
            'persisted_running': None
        }
        # Resume/clear buttons for persisted queue
        self.resume_button = None
        self.clear_persisted_button = None
        # Pagination and filtering state
        self.current_page = 1
        self.page_size = 25
        self.total_pages = 1
        self.total_records = 0
        self.status_filter = 'all'
        self.expert_filter = 'all'  # Filter by expert
        self.type_filter = 'all'  # Filter by analysis type (ENTER_MARKET or OPEN_POSITIONS)
        self.recommendation_filter = 'all'  # Filter by recommendation (BUY, SELL, HOLD)
        self.symbol_filter = ''  # Filter by symbol - filters at database level, not client-side
        
        # ===== CACHING for performance =====
        # Cache entire filtered dataset to avoid recomputing on pagination/page size changes
        self.cached_analysis_data = []  # Full filtered result set
        self.cache_valid = False  # Whether cache matches current filters
        self.last_filter_state = None  # Track filter state to detect changes
        
        # Smart Risk Manager Jobs pagination and filtering state
        self.smart_risk_current_page = 1
        self.smart_risk_page_size = 25
        self.smart_risk_total_pages = 1
        self.smart_risk_total_records = 0
        self.smart_risk_status_filter = 'all'  # Filter by status (RUNNING, COMPLETED, FAILED)
        self.smart_risk_expert_filter = 'all'  # Filter by expert
        self.smart_risk_pagination_container = None  # Container for Smart Risk Manager pagination controls
        
        # Smart Risk Manager Jobs caching
        self.cached_smart_risk_data = []  # Full filtered result set
        self.smart_risk_cache_valid = False  # Whether cache matches current filters
        self.last_smart_risk_filter_state = None  # Track filter state to detect changes
        
        self.render()
    
    def _get_worker_queue(self):
        """Lazy initialization of worker queue."""
        if self.worker_queue is None:
            self.worker_queue = get_worker_queue()
        return self.worker_queue
    
    def _get_filter_state(self) -> tuple:
        """Get current filter state as a hashable tuple for cache invalidation."""
        return (
            self.status_filter,
            self.expert_filter,
            self.type_filter,
            self.recommendation_filter,
            self.symbol_filter
        )
    
    def _should_invalidate_cache(self) -> bool:
        """Check if filters have changed, requiring cache invalidation."""
        current_state = self._get_filter_state()
        if self.last_filter_state != current_state:
            self.last_filter_state = current_state
            return True
        return False
    
    def _invalidate_cache(self):
        """Invalidate the analysis data cache."""
        self.cached_analysis_data = []
        self.cache_valid = False


    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Market Analysis Job Monitoring').classes('text-lg font-bold')
            
            # Filters and controls
            with ui.row().classes('w-full justify-between items-center mb-4'):
                with ui.row().classes('gap-4'):
                    # Status filter
                    status_options = {
                        'all': 'All Status',
                        'pending': '⏳ Pending',
                        'running': '🔄 Running', 
                        'completed': '✅ Completed',
                        'failed': '❌ Failed',
                        'cancelled': '🚫 Cancelled'
                    }
                    self.status_select = ui.select(
                        options=status_options,
                        value='all',
                        label='Status Filter'
                    ).classes('w-40')
                    self.status_select.on_value_change(self._on_status_filter_change)
                    
                    # Expert filter
                    expert_options = self._get_expert_options()
                    self.expert_select = ui.select(
                        options=expert_options,
                        value='all',
                        label='Expert Filter'
                    ).classes('w-48')
                    self.expert_select.on_value_change(self._on_expert_filter_change)
                    
                    # Type filter (ENTER_MARKET vs OPEN_POSITIONS)
                    type_options = {
                        'all': 'All Types',
                        'enter_market': '📊 Enter Market',
                        'open_positions': '📈 Open Positions'
                    }
                    self.type_select = ui.select(
                        options=type_options,
                        value='all',
                        label='Analysis Type'
                    ).classes('w-40')
                    self.type_select.on_value_change(self._on_type_filter_change)
                    
                    # Recommendation filter (BUY, SELL, HOLD)
                    recommendation_options = {
                        'all': 'All Actions',
                        'BUY': '📈 BUY',
                        'SELL': '📉 SELL',
                        'HOLD': '⏸️ HOLD'
                    }
                    self.recommendation_select = ui.select(
                        options=recommendation_options,
                        value='all',
                        label='Recommendation'
                    ).classes('w-40')
                    self.recommendation_select.on_value_change(self._on_recommendation_filter_change)
                    
                    # Symbol filter
                    self.symbol_input = ui.input(
                        'Symbol Filter',
                        placeholder='e.g., AAPL, MSFT'
                    ).props('stack-label').classes('w-40')
                    self.symbol_input.on_value_change(self._on_symbol_filter_change)
                
                with ui.row().classes('gap-2'):
                    ui.button('Clear Filters', on_click=self._clear_filters, icon='clear')
                    ui.button('Refresh', on_click=self._start_async_refresh, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Analysis jobs table container (will be populated async)
            self.analysis_table_container = ui.column().classes('w-full')
            self._create_analysis_table_placeholder()
            
            # Pagination controls container
            self.pagination_container = ui.row().classes('w-full')
            with self.pagination_container:
                self._create_pagination_controls()
            
            ui.separator().classes('my-4')
            
            # Worker queue status
            with ui.card().classes('w-full'):
                ui.label('Worker Queue Status').classes('text-md font-bold')
                self._create_queue_status()
            
            # Queued Tasks table (in-memory worker queue tasks)
            self.queued_tasks_container = ui.column().classes('w-full mt-4')
            self._create_queued_tasks_table()
            
            ui.separator().classes('my-4')
            
            # Smart Risk Manager jobs table
            self._create_smart_risk_manager_table()
        
        # Start loading data asynchronously - don't block on this
        asyncio.create_task(self._async_load_analysis_table())
        
        # Start auto-refresh
        self.start_auto_refresh()

    def _create_analysis_table_placeholder(self):
        """Create a placeholder with loading indicator for the analysis table."""
        self.analysis_table_container.clear()
        with self.analysis_table_container:
            with ui.card().classes('w-full'):
                with ui.row().classes('w-full justify-between items-center mb-2'):
                    ui.label('Analysis Jobs').classes('text-md font-bold')
                    ui.spinner('dots').classes('ml-auto')
                ui.label('Loading analysis jobs...').classes('text-sm text-gray-500')
    
    async def _async_load_analysis_table(self):
        """Load analysis table data asynchronously and update the UI."""
        try:
            #logger.debug("[JobMonitoringTab] Starting async analysis table load")
            
            # Fetch data in background (non-blocking)
            analysis_data, total_records = await asyncio.to_thread(self._get_analysis_data)
            self.total_records = total_records
            self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
            
            logger.debug(f"[JobMonitoringTab] Fetched {len(analysis_data)} analysis records")
            
            # Update the container with actual table
            self.analysis_table_container.clear()
            with self.analysis_table_container:
                self._create_analysis_table(analysis_data, self.total_records)
            
            # Update pagination controls
            self._create_pagination_controls()
            
            logger.debug("[JobMonitoringTab] Analysis table loaded successfully")
        except Exception as e:
            logger.error(f"[JobMonitoringTab] Error loading analysis table: {e}", exc_info=True)
            self.analysis_table_container.clear()
            with self.analysis_table_container:
                with ui.card().classes('w-full'):
                    ui.label('Error Loading Analysis Jobs').classes('text-md font-bold text-red-600')
                    ui.label(f'Failed to load data: {str(e)}').classes('text-sm text-gray-600')
                    ui.button('Retry', on_click=lambda: asyncio.create_task(self._async_load_analysis_table()))
    
    def _start_async_refresh(self):
        """Start async refresh of analysis table."""
        asyncio.create_task(self._async_load_analysis_table())

    def _create_analysis_table(self, analysis_data=None, total_records=None):
        """Create the analysis jobs table."""
        columns = [
            {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
            {'name': 'status', 'label': 'Status', 'field': 'status_display', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'recommendation', 'label': 'Recommendation', 'field': 'recommendation', 'sortable': True, 'style': 'width: 130px'},
            {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'expected_profit', 'label': 'Expected Profit', 'field': 'expected_profit', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at_local', 'sortable': True, 'style': 'width: 160px'},
            {'name': 'subtype', 'label': 'Type', 'field': 'subtype', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'sortable': False, 'style': 'width: 100px'}
        ]
        
        # Use provided data or fetch it (for backward compatibility)
        if analysis_data is None:
            analysis_data, self.total_records = self._get_analysis_data()
        elif total_records is not None:
            self.total_records = total_records
        
        self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
        
        with ui.card().classes('w-full'):
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('Analysis Jobs').classes('text-md font-bold')
                ui.label(f'Showing {len(analysis_data)} of {self.total_records} records').classes('text-sm text-gray-600')
            
            self.analysis_table = ui.table(
                columns=columns, 
                rows=analysis_data, 
                row_key='id'
            ).classes('w-full')
            
            # Add status icon slot
            self.analysis_table.add_slot('body-cell-status', '''
                <q-td :props="props">
                    <div class="row items-center no-wrap">
                        <div v-if="props.row.skip_reason" class="cursor-help">
                            <span v-html="props.row.status_display"></span>
                            <q-tooltip max-width="300px">{{ props.row.skip_reason }}</q-tooltip>
                        </div>
                        <span v-else v-html="props.row.status_display"></span>
                    </div>
                </q-td>
            ''')
            
            # Add action buttons
            self.analysis_table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn flat dense icon="info" 
                           color="primary" 
                           @click="$parent.$emit('view_details', props.row.id)">
                        <q-tooltip>View Analysis Details</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.has_evaluation_data" 
                           flat dense icon="search" 
                           color="secondary" 
                           @click="$parent.$emit('view_rule_evaluation', props.row.id)">
                        <q-tooltip>View Rule Evaluation Details</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.can_cancel" 
                           flat dense icon="cancel" 
                           color="negative" 
                           @click="$parent.$emit('cancel_analysis', props.row.id)"
                           :disable="props.row.status === 'running'">
                        <q-tooltip>Cancel Analysis</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.status === 'failed'" 
                           flat dense icon="refresh" 
                           color="orange" 
                           @click="$parent.$emit('rerun_analysis', props.row.id)">
                        <q-tooltip>Re-run Failed Analysis</q-tooltip>
                    </q-btn>
                    <q-btn flat dense icon="bug_report" 
                           color="accent" 
                           @click="$parent.$emit('troubleshoot_ruleset', props.row.id)">
                        <q-tooltip>Troubleshoot Ruleset</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Handle events
            self.analysis_table.on('cancel_analysis', self.cancel_analysis)
            self.analysis_table.on('view_details', self.view_analysis_details)
            self.analysis_table.on('troubleshoot_ruleset', self.troubleshoot_ruleset)
            self.analysis_table.on('rerun_analysis', self.rerun_analysis)
            self.analysis_table.on('view_rule_evaluation', self.view_rule_evaluation)
    
    def _create_smart_risk_manager_table(self):
        """Create the Smart Risk Manager jobs table."""
        columns = [
            {'name': 'id', 'label': 'Job ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
            {'name': 'status', 'label': 'Status', 'field': 'status_display', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'run_date', 'label': 'Run Date', 'field': 'run_date_local', 'sortable': True, 'style': 'width: 160px'},
            {'name': 'duration', 'label': 'Duration', 'field': 'duration_display', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'iterations', 'label': 'Iterations', 'field': 'iteration_count', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'actions', 'label': 'Actions Taken', 'field': 'actions_taken_count', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'detail', 'label': '', 'field': 'detail', 'sortable': False, 'style': 'width: 80px'}
        ]
        
        with ui.card().classes('w-full'):
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('Smart Risk Manager Jobs').classes('text-md font-bold')
                ui.button('Refresh', on_click=self.refresh_smart_risk_data, icon='refresh').props('flat dense')
            
            # Filters row
            with ui.row().classes('w-full gap-4 mb-4'):
                with ui.column().classes('w-48'):
                    # Status filter
                    status_options = {
                        'all': 'All Statuses',
                        'RUNNING': 'Running',
                        'COMPLETED': 'Completed', 
                        'FAILED': 'Failed'
                    }
                    self.smart_risk_status_select = ui.select(
                        options=status_options,
                        value=self.smart_risk_status_filter,
                        label='Status Filter'
                    ).classes('w-full')
                    self.smart_risk_status_select.on_value_change(self._on_smart_risk_status_filter_change)
                
                with ui.column().classes('w-48'):
                    # Expert filter
                    expert_options = self._get_smart_risk_expert_options()
                    self.smart_risk_expert_select = ui.select(
                        options=expert_options,
                        value=self.smart_risk_expert_filter,
                        label='Expert Filter'
                    ).classes('w-full')
                    self.smart_risk_expert_select.on_value_change(self._on_smart_risk_expert_filter_change)
            
            # Get initial data
            smart_risk_data, total_records = self._get_smart_risk_manager_data()
            
            # Info row showing total records and current page info
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label(f'Total: {total_records} jobs | Page {self.smart_risk_current_page} of {self.smart_risk_total_pages}').classes('text-sm text-gray-600')
                if total_records > 0:
                    start_record = (self.smart_risk_current_page - 1) * self.smart_risk_page_size + 1
                    end_record = min(self.smart_risk_current_page * self.smart_risk_page_size, total_records)
                    ui.label(f'Showing {start_record}-{end_record}').classes('text-sm text-gray-600')
            
            self.smart_risk_table = ui.table(
                columns=columns,
                rows=smart_risk_data,
                row_key='id'
            ).classes('w-full')
            
            # Add status badge slot
            self.smart_risk_table.add_slot('body-cell-status', '''
                <q-td :props="props">
                    <q-badge :color="props.row.status_color" :label="props.row.status" />
                </q-td>
            ''')
            
            # Add detail button slot
            self.smart_risk_table.add_slot('body-cell-detail', '''
                <q-td :props="props">
                    <q-btn flat dense icon="visibility" 
                           color="primary" 
                           @click="$parent.$emit('view_smart_risk_detail', props.row.id)">
                        <q-tooltip>View Job Details</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Pagination controls container
            self.smart_risk_pagination_container = ui.row().classes('w-full')
            with self.smart_risk_pagination_container:
                self._create_smart_risk_pagination_controls()
            
            # Handle events
            self.smart_risk_table.on('view_smart_risk_detail', self.view_smart_risk_detail)
    
    def _get_smart_risk_manager_data(self):
        """Get Smart Risk Manager jobs data for the table with pagination and filtering.
        
        Returns:
            Tuple of (paginated_data, total_records)
            
        Caches the entire filtered dataset to avoid recomputing on pagination changes.
        """
        try:
            # Check if cache is valid (filter state hasn't changed)
            current_filter_state = (
                self.smart_risk_status_filter,
                self.smart_risk_expert_filter
            )
            
            if not self.smart_risk_cache_valid or self.last_smart_risk_filter_state != current_filter_state:
                # Cache is invalid - fetch and format ALL matching records (no pagination in query)
                self.cached_smart_risk_data = self._fetch_all_smart_risk_jobs()
                self.smart_risk_cache_valid = True
                self.last_smart_risk_filter_state = current_filter_state
            
            # Apply pagination to cached data
            total_records = len(self.cached_smart_risk_data)
            self.smart_risk_total_records = total_records
            self.smart_risk_total_pages = max(1, math.ceil(total_records / self.smart_risk_page_size))
            
            # Ensure current page is within valid range
            if self.smart_risk_current_page > self.smart_risk_total_pages:
                self.smart_risk_current_page = self.smart_risk_total_pages
            
            # Get paginated subset
            start_idx = (self.smart_risk_current_page - 1) * self.smart_risk_page_size
            end_idx = start_idx + self.smart_risk_page_size
            paginated_data = self.cached_smart_risk_data[start_idx:end_idx]
            
            return paginated_data, total_records
            
        except Exception as e:
            logger.error(f"Error getting Smart Risk Manager jobs data: {e}", exc_info=True)
            return [], 0
    
    def _fetch_all_smart_risk_jobs(self) -> List[dict]:
        """Fetch all Smart Risk Manager jobs matching current filters (no pagination)."""
        try:
            from ...core.models import SmartRiskManagerJob, ExpertInstance
            from sqlmodel import select, desc, and_
            
            with get_db() as session:
                # Build base query
                statement = select(SmartRiskManagerJob)
                
                # Apply filters
                filters = []
                if self.smart_risk_status_filter != 'all':
                    filters.append(SmartRiskManagerJob.status == self.smart_risk_status_filter)
                
                if self.smart_risk_expert_filter != 'all':
                    filters.append(SmartRiskManagerJob.expert_instance_id == int(self.smart_risk_expert_filter))
                
                if filters:
                    statement = statement.where(and_(*filters))
                
                # Order by run date descending
                statement = statement.order_by(desc(SmartRiskManagerJob.run_date))
                
                jobs = session.exec(statement).all()
                
                rows = []
                for job in jobs:
                    # Get expert name (alias with fallback to classname + ID)
                    expert_name = "Unknown"
                    try:
                        expert_instance = session.get(ExpertInstance, job.expert_instance_id)
                        if expert_instance:
                            # Use alias if available, otherwise fallback to "classname (ID: expert_id)"
                            if expert_instance.alias:
                                expert_name = expert_instance.alias
                            else:
                                expert_name = f"{expert_instance.expert} (ID: {expert_instance.id})"
                    except Exception:
                        pass
                    
                    # Format duration
                    duration_display = "N/A"
                    if job.duration_seconds is not None:
                        if job.duration_seconds < 60:
                            duration_display = f"{job.duration_seconds}s"
                        else:
                            minutes = job.duration_seconds // 60
                            seconds = job.duration_seconds % 60
                            duration_display = f"{minutes}m {seconds}s"
                    
                    # Format run date - convert UTC to local time
                    if job.run_date:
                        # Ensure the datetime is timezone-aware (treat as UTC if naive)
                        if job.run_date.tzinfo is None:
                            from datetime import timezone
                            utc_time = job.run_date.replace(tzinfo=timezone.utc)
                        else:
                            utc_time = job.run_date
                        local_time = utc_time.astimezone()
                        run_date_local = local_time.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        run_date_local = "N/A"
                    
                    # Status badge color
                    status_color = 'positive' if job.status == 'COMPLETED' else ('negative' if job.status == 'FAILED' else 'warning')
                    
                    rows.append({
                        'id': job.id,
                        'expert_name': expert_name,
                        'expert_instance_id': job.expert_instance_id,
                        'status': job.status,
                        'status_display': job.status,
                        'status_color': status_color,
                        'run_date_local': run_date_local,
                        'duration_display': duration_display,
                        'iteration_count': job.iteration_count or 0,
                        'actions_taken_count': job.actions_taken_count or 0,
                        'detail': 'detail'  # Placeholder for detail slot (NiceGUI 3.2 requires all columns to have values)
                    })
                
                return rows
                
        except Exception as e:
            logger.error(f"Error fetching Smart Risk Manager jobs: {e}", exc_info=True)
            return []
    
    def view_smart_risk_detail(self, event_data):
        """Open the Smart Risk Manager job detail dialog."""
        job_id = None
        try:
            # Extract job_id from event data
            if hasattr(event_data, 'args') and hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                job_id = int(event_data.args[0])
            elif isinstance(event_data, int):
                job_id = event_data
            elif hasattr(event_data, 'args') and isinstance(event_data.args, int):
                job_id = event_data.args
            else:
                logger.error(f"Invalid event data for view_smart_risk_detail: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Open the detail dialog (lazy initialization)
            if not hasattr(self, '_smart_risk_dialog'):
                self._smart_risk_dialog = SmartRiskManagerDetailDialog()
            self._smart_risk_dialog.open(job_id)
            
        except Exception as e:
            logger.error(f"Error opening Smart Risk Manager detail {job_id if job_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error opening details: {str(e)}", type='negative')
    
    def _create_smart_risk_pagination_controls(self):
        """Create pagination controls for Smart Risk Manager jobs."""
        # Clear existing controls if container exists
        if self.smart_risk_pagination_container is not None:
            self.smart_risk_pagination_container.clear()
        
        if self.smart_risk_total_pages <= 1:
            return
        
        # Create controls in the container
        with self.smart_risk_pagination_container:
            with ui.row().classes('w-full justify-center items-center mt-4 gap-2'):
                # Previous button
                prev_btn = ui.button('Previous', 
                                   on_click=lambda: self._change_smart_risk_page(self.smart_risk_current_page - 1),
                                   icon='chevron_left')
                prev_btn.props('flat')
                if self.smart_risk_current_page <= 1:
                    prev_btn.props('disable')
                
                # Page info
                ui.label(f'Page {self.smart_risk_current_page} of {self.smart_risk_total_pages}').classes('mx-4')
                
                # Next button  
                next_btn = ui.button('Next',
                                   on_click=lambda: self._change_smart_risk_page(self.smart_risk_current_page + 1),
                                   icon='chevron_right')
                next_btn.props('flat')
                if self.smart_risk_current_page >= self.smart_risk_total_pages:
                    next_btn.props('disable')
                
                # Page size selector
                ui.separator().props('vertical').classes('mx-4')
                page_size_options = {'10': '10 per page', '25': '25 per page', '50': '50 per page', '100': '100 per page'}
                page_size_select = ui.select(
                    options=page_size_options,
                    value=str(self.smart_risk_page_size),
                    label='Page Size'
                ).classes('w-32')
                page_size_select.on_value_change(self._on_smart_risk_page_size_change)
    
    def _change_smart_risk_page(self, new_page: int):
        """Change to a specific page for Smart Risk Manager jobs - synchronous update using cached data."""
        if 1 <= new_page <= self.smart_risk_total_pages:
            self.smart_risk_current_page = new_page
            # Update table synchronously using cached data (no async needed for pagination)
            self._update_smart_risk_table_from_cache()
    
    def _update_smart_risk_table_from_cache(self):
        """Update Smart Risk Manager table and pagination controls synchronously from cached data."""
        try:
            # Get paginated data from cache (this is synchronous)
            smart_risk_data, total_records = self._get_smart_risk_manager_data()
            
            # Update table if it exists
            if self.smart_risk_table:
                self.smart_risk_table.rows = smart_risk_data
            
            # Update pagination controls with current state
            self._create_smart_risk_pagination_controls()
            
        except Exception as e:
            logger.error(f"Error updating Smart Risk Manager table from cache: {e}", exc_info=True)
    
    def _on_smart_risk_page_size_change(self, event):
        """Handle page size change for Smart Risk Manager jobs - synchronous update using cached data."""
        try:
            new_size = int(event.value)
            self.smart_risk_page_size = new_size
            self.smart_risk_current_page = 1  # Reset to first page
            # Recalculate total pages based on new page size
            self.smart_risk_total_pages = max(1, math.ceil(self.smart_risk_total_records / self.smart_risk_page_size))
            # Update table synchronously using cached data (no async needed)
            self._update_smart_risk_table_from_cache()
        except ValueError:
            pass
    
    def _get_smart_risk_expert_options(self) -> dict:
        """Get available expert instances for Smart Risk Manager filtering."""
        try:
            with get_db() as session:
                # Get expert instances that have Smart Risk Manager jobs
                from ...core.models import SmartRiskManagerJob
                statement = select(ExpertInstance).join(SmartRiskManagerJob).distinct()
                expert_instances = session.exec(statement).all()
                
                # Build options dictionary
                options = {'all': 'All Experts'}
                for expert in expert_instances:
                    # Use alias if available, otherwise expert type
                    # Format: alias-ID or expertType-ID
                    base_name = expert.alias or expert.expert
                    display_name = f"{base_name}-{expert.id}"
                    options[str(expert.id)] = display_name
                
                return options
        except Exception as e:
            logger.error(f"Error getting Smart Risk Manager expert options: {e}", exc_info=True)
            return {'all': 'All Experts'}
    
    def _on_smart_risk_status_filter_change(self, event):
        """Handle status filter change for Smart Risk Manager jobs."""
        self.smart_risk_status_filter = event.value
        self.smart_risk_current_page = 1  # Reset to first page when filtering
        self.refresh_smart_risk_data()
    
    def _on_smart_risk_expert_filter_change(self, event):
        """Handle expert filter change for Smart Risk Manager jobs."""
        self.smart_risk_expert_filter = event.value
        self.smart_risk_current_page = 1  # Reset to first page when filtering
        self.refresh_smart_risk_data()
    
    def refresh_smart_risk_data(self):
        """Refresh Smart Risk Manager jobs data with current filters."""
        try:
            # Invalidate cache to force refresh
            self.smart_risk_cache_valid = False
            
            # Get fresh data with current filters
            smart_risk_data, total_records = self._get_smart_risk_manager_data()
            
            # Update table if it exists
            if hasattr(self, 'smart_risk_table') and self.smart_risk_table:
                self.smart_risk_table.rows = smart_risk_data
            
            # Update pagination controls
            self._create_smart_risk_pagination_controls()
            
            logger.debug(f"Refreshed Smart Risk Manager data: {total_records} total records, {len(smart_risk_data)} on current page")
            
        except Exception as e:
            logger.error(f"Error refreshing Smart Risk Manager data: {e}", exc_info=True)
            ui.notify("Error refreshing data", type='negative')

    def _create_queue_status(self):
        """Create worker queue status display with live-updating labels."""
        queue_info = self._get_queue_info()
        persisted_info = self._get_persisted_queue_info()
        
        with ui.row().classes('w-full'):
            with ui.card().classes('flex-1'):
                ui.label('Worker Status')
                self.queue_status_labels['worker_count'] = ui.label(f"Workers: {queue_info['worker_count']}")
                self.queue_status_labels['running_tasks'] = ui.label(f"Running: {queue_info['running_tasks']}")
                
            with ui.card().classes('flex-1'):
                ui.label('Queue Status')
                self.queue_status_labels['pending_tasks'] = ui.label(f"Pending: {queue_info['pending_tasks']}")
                self.queue_status_labels['total_tasks'] = ui.label(f"Total Tasks: {queue_info['total_tasks']}")
            
            # Persisted queue resume section
            with ui.card().classes('flex-1'):
                ui.label('Saved Queue (from previous session)')
                self.queue_status_labels['persisted_pending'] = ui.label(f"Saved Pending: {persisted_info['pending']}")
                self.queue_status_labels['persisted_running'] = ui.label(f"Saved Running: {persisted_info['running']}")
                
                # Resume button - only show if there are persisted tasks
                with ui.row().classes('gap-2 mt-2'):
                    self.resume_button = ui.button(
                        'Resume Saved Queue',
                        on_click=self._on_resume_persisted_queue,
                        icon='play_arrow'
                    ).props('color=positive')
                    
                    self.clear_persisted_button = ui.button(
                        'Clear Saved',
                        on_click=self._on_clear_persisted_queue,
                        icon='delete'
                    ).props('color=negative outline')
                
                # Update button visibility based on persisted task count
                self._update_persisted_buttons_visibility(persisted_info['total'])
    
    def _get_persisted_queue_info(self) -> dict:
        """Get info about persisted queue tasks."""
        try:
            worker_queue = self._get_worker_queue()
            return worker_queue.get_persisted_tasks_count()
        except Exception as e:
            logger.error(f"Failed to get persisted queue info: {e}")
            return {'pending': 0, 'running': 0, 'total': 0}
    
    def _update_persisted_buttons_visibility(self, total_persisted: int):
        """Show/hide resume and clear buttons based on persisted task count."""
        if hasattr(self, 'resume_button') and self.resume_button:
            if total_persisted > 0:
                self.resume_button.enable()
                self.clear_persisted_button.enable()
            else:
                self.resume_button.disable()
                self.clear_persisted_button.disable()
    
    async def _on_resume_persisted_queue(self):
        """Handle resume button click to restore persisted queue tasks."""
        try:
            worker_queue = self._get_worker_queue()
            
            # Show confirmation dialog
            with ui.dialog() as confirm_dialog, ui.card():
                ui.label('Resume Saved Queue?').classes('text-lg font-bold')
                persisted = worker_queue.get_persisted_tasks_count()
                ui.label(f"This will restore {persisted['pending']} pending and {persisted['running']} interrupted tasks.")
                ui.label("Interrupted tasks will be restarted from the beginning.").classes('text-sm text-gray-600')
                
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=confirm_dialog.close).props('flat')
                    
                    async def do_resume():
                        confirm_dialog.close()
                        ui.notify('Restoring saved queue...', type='info')
                        
                        result = worker_queue.restore_persisted_tasks()
                        
                        if result['restored'] > 0:
                            ui.notify(f"Restored {result['restored']} tasks to queue", type='positive')
                        else:
                            ui.notify("No tasks were restored", type='warning')
                        
                        if result['failed'] > 0:
                            ui.notify(f"{result['failed']} tasks failed to restore", type='negative')
                        
                        # Refresh the UI
                        self._invalidate_cache()
                        await self._async_load_analysis_table()
                        self._refresh_queued_tasks_table()
                        self._update_queue_status_display()
                    
                    ui.button('Resume', on_click=do_resume).props('color=positive')
            
            confirm_dialog.open()
            
        except Exception as e:
            logger.error(f"Failed to resume persisted queue: {e}")
            ui.notify(f"Error resuming queue: {str(e)}", type='negative')
    
    async def _on_clear_persisted_queue(self):
        """Handle clear button click to remove all persisted queue tasks."""
        try:
            worker_queue = self._get_worker_queue()
            
            # Show confirmation dialog
            with ui.dialog() as confirm_dialog, ui.card():
                ui.label('Clear Saved Queue?').classes('text-lg font-bold')
                persisted = worker_queue.get_persisted_tasks_count()
                ui.label(f"This will permanently delete {persisted['total']} saved tasks.")
                ui.label("This action cannot be undone.").classes('text-sm text-red-600')
                
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=confirm_dialog.close).props('flat')
                    
                    async def do_clear():
                        confirm_dialog.close()
                        count = worker_queue.clear_persisted_tasks()
                        ui.notify(f"Cleared {count} saved tasks", type='positive')
                        
                        # Update button visibility
                        self._update_persisted_buttons_visibility(0)
                        self._update_queue_status_display()
                    
                    ui.button('Clear', on_click=do_clear).props('color=negative')
            
            confirm_dialog.open()
            
        except Exception as e:
            logger.error(f"Failed to clear persisted queue: {e}")
            ui.notify(f"Error clearing queue: {str(e)}", type='negative')

    def _create_queued_tasks_table(self):
        """Create table showing in-memory queued tasks from worker queue.
        
        This shows tasks that are in the worker queue but may not yet have 
        MarketAnalysis database records (which are only created when tasks start executing).
        """
        self.queued_tasks_container.clear()
        with self.queued_tasks_container:
            with ui.card().classes('w-full'):
                with ui.row().classes('w-full justify-between items-center mb-2'):
                    ui.label('Queued Tasks (In-Memory Worker Queue)').classes('text-md font-bold')
                    ui.button('Refresh', on_click=self._refresh_queued_tasks_table, icon='refresh').props('flat dense')
                
                queued_tasks_data = self._get_queued_tasks_data()
                
                if not queued_tasks_data:
                    ui.label('No tasks currently in queue').classes('text-sm text-gray-500 italic')
                else:
                    ui.label(f'Showing {len(queued_tasks_data)} tasks in worker queue').classes('text-sm text-gray-600 mb-2')
                    
                    columns = [
                        {'name': 'task_id', 'label': 'Task ID', 'field': 'task_id', 'sortable': True, 'style': 'width: 200px'},
                        {'name': 'type', 'label': 'Type', 'field': 'type', 'sortable': True, 'style': 'width: 100px'},
                        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
                        {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
                        {'name': 'status', 'label': 'Status', 'field': 'status_display', 'sortable': True, 'style': 'width: 100px'},
                        {'name': 'priority', 'label': 'Priority', 'field': 'priority', 'sortable': True, 'style': 'width: 80px'},
                        {'name': 'created_at', 'label': 'Created', 'field': 'created_at_display', 'sortable': True, 'style': 'width: 160px'},
                        {'name': 'batch_id', 'label': 'Batch', 'field': 'batch_id', 'sortable': True, 'style': 'width: 220px'},
                    ]
                    
                    self.queued_tasks_table = ui.table(
                        columns=columns,
                        rows=queued_tasks_data,
                        row_key='task_id',
                        pagination={'rowsPerPage': 10, 'sortBy': 'created_at', 'descending': True}
                    ).classes('w-full dark-pagination')
                    
                    # Add status badge slot
                    self.queued_tasks_table.add_slot('body-cell-status', '''
                        <q-td :props="props">
                            <q-badge :color="props.row.status_color" :label="props.row.status_display" />
                        </q-td>
                    ''')
                    
                    # Add batch ID slot with tooltip for long IDs
                    self.queued_tasks_table.add_slot('body-cell-batch_id', '''
                        <q-td :props="props">
                            <span class="cursor-help" :title="props.row.batch_id">
                                {{ props.row.batch_id || '-' }}
                            </span>
                        </q-td>
                    ''')

    def _get_queued_tasks_data(self) -> List[dict]:
        """Get formatted data for queued tasks table from worker queue.
        
        Returns list of dicts with task info for all PENDING and RUNNING tasks.
        """
        try:
            worker_queue = self._get_worker_queue()
            all_tasks_dict = worker_queue.get_all_tasks()
            all_tasks = list(all_tasks_dict.values()) if isinstance(all_tasks_dict, dict) else all_tasks_dict
            
            # Build a cache of expert IDs to shortnames for efficient lookup
            expert_shortnames = {}
            try:
                with get_db() as session:
                    experts = session.exec(select(ExpertInstance)).all()
                    for expert in experts:
                        # Use alias, or fallback to "expert_name-id"
                        shortname = expert.alias or f"{expert.expert}-{expert.id}"
                        expert_shortnames[expert.id] = shortname
            except Exception as e:
                logger.warning(f"Failed to fetch expert shortnames: {e}")
            
            formatted_tasks = []
            for task in all_tasks:
                # Get task status
                task_status = getattr(task, 'status', None) or getattr(task, 'state', None)
                
                # Only show pending and running tasks
                if task_status not in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING, 'pending', 'running']:
                    continue
                
                # Determine task type
                task_type = 'Analysis'
                symbol = ''
                if hasattr(task, 'symbol'):
                    symbol = task.symbol
                    task_type = 'Analysis'
                elif hasattr(task, 'expansion_type'):
                    task_type = f'Expansion ({task.expansion_type})'
                elif hasattr(task, 'job_id') or 'smart_risk' in str(getattr(task, 'id', '')).lower():
                    task_type = 'Smart Risk'
                
                # Format created timestamp (convert UTC to local time for display)
                created_at = getattr(task, 'created_at', None)
                if created_at:
                    try:
                        from ...core.date_utils import format_for_display
                        created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
                        created_at_display = format_for_display(created_dt)
                    except:
                        created_at_display = 'Unknown'
                else:
                    created_at_display = 'Unknown'
                
                # Get status display and color
                if task_status == WorkerTaskStatus.PENDING or task_status == 'pending':
                    status_display = 'Pending'
                    status_color = 'orange'
                elif task_status == WorkerTaskStatus.RUNNING or task_status == 'running':
                    status_display = 'Running'
                    status_color = 'blue'
                else:
                    status_display = str(task_status)
                    status_color = 'grey'
                
                # Get expert shortname from cache, fallback to ID if not found
                expert_instance_id = getattr(task, 'expert_instance_id', '')
                expert_name = expert_shortnames.get(expert_instance_id, f"ID:{expert_instance_id}" if expert_instance_id else '')
                
                # Get batch_id from task
                task_batch_id = getattr(task, 'batch_id', None) or ''
                
                formatted_tasks.append({
                    'task_id': getattr(task, 'id', 'Unknown'),
                    'type': task_type,
                    'symbol': symbol,
                    'expert_name': expert_name,
                    'status_display': status_display,
                    'status_color': status_color,
                    'priority': getattr(task, 'priority', 0),
                    'created_at_display': created_at_display,
                    'batch_id': task_batch_id,
                })
            
            # Sort by priority (lower = higher priority), then by created time
            formatted_tasks.sort(key=lambda x: (x['priority'], x['created_at_display']))
            
            return formatted_tasks
            
        except Exception as e:
            logger.error(f"Error getting queued tasks data: {e}", exc_info=True)
            return []

    def _refresh_queued_tasks_table(self):
        """Refresh the queued tasks table."""
        self._create_queued_tasks_table()

    def _get_analysis_data(self, preserve_page: bool = False) -> tuple[List[dict], int]:
        """Get analysis jobs data for the table with pagination and filtering.
        
        OPTIMIZATION: Uses joins to avoid N+1 queries and eager-loads relationships.
        Caches the entire filtered dataset to avoid recomputing on pagination changes.
        Only recomputes when filters actually change or cache is empty.
        
        Args:
            preserve_page: If True, don't reset to page 1 even if cache is invalidated.
                          Used during auto-refresh to maintain current page.
        """
        try:
            # Check if cache needs invalidation due to filter changes OR cache is empty
            filters_changed = self._should_invalidate_cache()
            if filters_changed or not self.cache_valid:
                logger.debug(f"Cache invalidated - {'filters changed' if filters_changed else 'cache empty'}. Recomputing...")
                # Only reset to first page when filters actually change, not during auto-refresh
                if filters_changed and not preserve_page:
                    self.current_page = 1
                
                # Fetch and format ALL matching records (no pagination in query)
                with get_db() as session:
                    from sqlalchemy.orm import joinedload, selectinload
                    from sqlalchemy import and_
                    
                    # Use eager loading to avoid N+1 queries
                    statement = select(MarketAnalysis).options(
                        selectinload(MarketAnalysis.expert_recommendations)
                    )
                    
                    # Apply all filters
                    filters = []
                    
                    if self.status_filter != 'all':
                        filters.append(MarketAnalysis.status == MarketAnalysisStatus(self.status_filter))
                    
                    if self.expert_filter != 'all':
                        filters.append(MarketAnalysis.expert_instance_id == int(self.expert_filter))
                    
                    if self.type_filter != 'all':
                        from ...core.types import AnalysisUseCase
                        if self.type_filter == 'enter_market':
                            filters.append(MarketAnalysis.subtype == AnalysisUseCase.ENTER_MARKET)
                        elif self.type_filter == 'open_positions':
                            filters.append(MarketAnalysis.subtype == AnalysisUseCase.OPEN_POSITIONS)
                    
                    if self.symbol_filter:
                        filters.append(MarketAnalysis.symbol.ilike(f"%{self.symbol_filter}%"))
                    
                    if self.recommendation_filter != 'all':
                        from ...core.types import OrderRecommendation
                        statement = statement.join(
                            ExpertRecommendation,
                            MarketAnalysis.id == ExpertRecommendation.market_analysis_id
                        )
                        filters.append(
                            ExpertRecommendation.recommended_action == OrderRecommendation[self.recommendation_filter]
                        )
                    
                    # Apply all filters at once
                    if filters:
                        statement = statement.where(and_(*filters))
                    
                    # Order by newest first
                    statement = statement.order_by(MarketAnalysis.created_at.desc())
                    
                    # Fetch ALL matching records at once using scalars() to get model objects
                    market_analyses = list(session.scalars(statement))
                    
                    # Pre-fetch expert instances for all records
                    expert_ids = set(m.expert_instance_id for m in market_analyses)
                    expert_instances = {}
                    if expert_ids:
                        stmt = select(ExpertInstance).where(ExpertInstance.id.in_(expert_ids))
                        for expert in session.scalars(stmt):
                            expert_instances[expert.id] = expert
                    
                    # Process all records and cache them (without has_evaluation_data - too slow)
                    self.cached_analysis_data = self._format_analysis_records(market_analyses, expert_instances)
                    self.cache_valid = True
            
            # Use cached data and apply pagination
            self.total_records = len(self.cached_analysis_data)
            self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
            
            # Ensure current page is valid
            if self.current_page > self.total_pages:
                self.current_page = max(1, self.total_pages)
            
            # Apply pagination to cached data
            start_idx = (self.current_page - 1) * self.page_size
            end_idx = start_idx + self.page_size
            paginated_data = self.cached_analysis_data[start_idx:end_idx]
            
            # OPTIMIZATION: Fetch has_evaluation_data only for current page items
            # This avoids N+1 queries for ALL records
            self._populate_evaluation_data_flags(paginated_data)
            
            # Create fresh copies of dicts to ensure Vue reactivity detects changes
            # This is needed because Vue doesn't detect mutations to existing objects
            paginated_data = [dict(item) for item in paginated_data]
            
            return paginated_data, self.total_records
                
        except Exception as e:
            logger.error(f"Error getting analysis data: {e}", exc_info=True)
            return [], 0
    
    def _format_analysis_records(self, market_analyses, expert_instances=None) -> List[dict]:
        """Format raw market analysis records into displayable data.
        
        Args:
            market_analyses: List of MarketAnalysis objects
            expert_instances: Optional dict of expert_id -> ExpertInstance for avoiding N+1 queries
        
        Extracted to separate method so it can be called once per filter change,
        then cached for pagination operations.
        """
        analysis_data = []
        
        # Use provided expert instances or fetch them individually (slower)
        if expert_instances is None:
            expert_instances = {}
        
        status_icons = {
            'pending': '⏳',
            'running': '🔄', 
            'completed': '✅',
            'failed': '❌',
            'cancelled': '🚫'
        }
        
        action_icons = {
            'BUY': '📈',
            'SELL': '📉',
            'HOLD': '⏸️',
            'ERROR': '❌'
        }
        
        for analysis in market_analyses:
            try:
                # Get expert instance info - use pre-fetched if available
                try:
                    expert_instance = expert_instances.get(analysis.expert_instance_id)
                    if expert_instance:
                        expert_alias = expert_instance.alias or expert_instance.expert
                        expert_name = f"{expert_alias}-{analysis.expert_instance_id}"
                    else:
                        expert_name = f"Unknown-{analysis.expert_instance_id}"
                except Exception as e:
                    logger.warning(f"Expert instance {analysis.expert_instance_id} not found: {e}")
                    expert_name = f"[Deleted]-{analysis.expert_instance_id}"
                
                # Format timestamp
                local_time = analysis.created_at.replace(tzinfo=timezone.utc).astimezone() if analysis.created_at else None
                created_local = local_time.strftime("%Y-%m-%d %H:%M:%S") if local_time else "Unknown"
                
                # Format status
                status_value = analysis.status.value if analysis.status else 'unknown'
                status_icon = status_icons.get(status_value, '❓')
                status_display = f'{status_icon} {status_value.title()}'
                
                skip_reason = None
                if analysis.state and isinstance(analysis.state, dict) and analysis.state.get('skipped'):
                    skip_reason = analysis.state.get('skip_reason', 'Analysis was skipped')
                    status_display = '⊘ Skipped'
                
                # Format recommendations
                recommendation_display = '-'
                confidence_display = '-'
                expected_profit_display = '-'
                
                if analysis.expert_recommendations and len(analysis.expert_recommendations) > 0:
                    recommendations = analysis.expert_recommendations
                    
                    if len(recommendations) == 1:
                        rec = recommendations[0]
                        if rec.recommended_action:
                            action_value = rec.recommended_action.value
                            action_icon = action_icons.get(action_value, '')
                            recommendation_display = f'{action_icon} {action_value}'
                        
                        if rec.confidence is not None:
                            confidence_display = f'{rec.confidence:.1f}%'
                        
                        if rec.expected_profit_percent is not None:
                            sign = '+' if rec.expected_profit_percent >= 0 else ''
                            expected_profit_display = f'{sign}{rec.expected_profit_percent:.2f}%'
                    else:
                        # Multiple recommendations
                        action_counts = {}
                        confidences = []
                        profits = []
                        
                        for rec in recommendations:
                            if rec.recommended_action:
                                action = rec.recommended_action.value
                                action_counts[action] = action_counts.get(action, 0) + 1
                            
                            if rec.confidence is not None:
                                confidences.append(rec.confidence)
                            
                            if rec.expected_profit_percent is not None:
                                profits.append(rec.expected_profit_percent)
                        
                        if action_counts:
                            action_summary = []
                            for action, count in sorted(action_counts.items()):
                                icon = action_icons.get(action, '')
                                action_summary.append(f'{icon}{count}')
                            recommendation_display = ' '.join(action_summary) + f' ({len(recommendations)} symbols)'
                        
                        if confidences:
                            avg_confidence = sum(confidences) / len(confidences)
                            confidence_display = f'{avg_confidence:.1f}% avg'
                        
                        if profits:
                            avg_profit = sum(profits) / len(profits)
                            sign = '+' if avg_profit >= 0 else ''
                            expected_profit_display = f'{sign}{avg_profit:.2f}% avg'
                
                # Format symbol
                symbol_display = analysis.symbol
                if analysis.expert_recommendations and len(analysis.expert_recommendations) > 1:
                    rec_symbols = [rec.symbol for rec in analysis.expert_recommendations if rec.symbol]
                    if rec_symbols:
                        if len(rec_symbols) <= 3:
                            symbol_display = ', '.join(rec_symbols)
                        else:
                            symbol_display = f'{", ".join(rec_symbols[:3])}... (+{len(rec_symbols)-3})'
                
                subtype_display = analysis.subtype.value.replace('_', ' ').title() if analysis.subtype else 'Unknown'
                can_cancel = analysis.status in [MarketAnalysisStatus.PENDING]
                
                # NOTE: has_evaluation_data is populated LATER by _populate_evaluation_data_flags()
                # for only the current page items to avoid N+1 queries on ALL records
                
                analysis_data.append({
                    'id': analysis.id,
                    'symbol': symbol_display,
                    'expert_name': expert_name,
                    'status': status_value,
                    'status_display': status_display,
                    'skip_reason': skip_reason,
                    'recommendation': recommendation_display,
                    'confidence': confidence_display,
                    'expected_profit': expected_profit_display,
                    'created_at_local': created_local,
                    'subtype': subtype_display,
                    'can_cancel': can_cancel,
                    'has_evaluation_data': False,  # Populated later by _populate_evaluation_data_flags()
                    'expert_instance_id': analysis.expert_instance_id,
                    'actions': 'actions'
                })
            except Exception as e:
                logger.warning(f"Error formatting analysis {analysis.id}: {e}")
                continue
        
        return analysis_data

    def _populate_evaluation_data_flags(self, paginated_data: List[dict]):
        """Populate has_evaluation_data flags for current page items only.
        
        OPTIMIZATION: Uses batch queries to check for evaluation data
        instead of N+1 lazy-loading queries for all records.
        
        Args:
            paginated_data: List of formatted analysis records (current page only)
        """
        if not paginated_data:
            return
        
        try:
            # Get analysis IDs for current page
            analysis_ids = [item['id'] for item in paginated_data]
            logger.debug(f"[_populate_evaluation_data_flags] Checking {len(analysis_ids)} analysis IDs: {analysis_ids}")
            
            with get_db() as session:
                from ...core.models import TradeActionResult
                
                # Query: Get market_analysis_id and data for all TradeActionResults
                # related to recommendations for these analyses
                stmt = (
                    select(
                        ExpertRecommendation.market_analysis_id,
                        TradeActionResult.data
                    )
                    .join(ExpertRecommendation, TradeActionResult.expert_recommendation_id == ExpertRecommendation.id)
                    .where(
                        ExpertRecommendation.market_analysis_id.in_(analysis_ids),
                        TradeActionResult.data.isnot(None)
                    )
                )
                
                # Build set of analysis IDs with evaluation data
                analysis_ids_with_eval = set()
                for market_analysis_id, data in session.execute(stmt):
                    if data and isinstance(data, dict) and 'evaluation_details' in data:
                        analysis_ids_with_eval.add(market_analysis_id)
            
            logger.debug(f"[_populate_evaluation_data_flags] Found {len(analysis_ids_with_eval)} IDs with evaluation data: {analysis_ids_with_eval}")
            
            # Update the paginated data in-place
            for item in paginated_data:
                item['has_evaluation_data'] = item['id'] in analysis_ids_with_eval
                
        except Exception as e:
            logger.warning(f"Error populating evaluation data flags: {e}")
            # Leave all as False on error

    def _create_pagination_controls(self):
        """Create pagination controls."""
        # Clear existing controls if container exists
        if self.pagination_container is not None:
            self.pagination_container.clear()
        
        if self.total_pages <= 1:
            return
        
        # Create controls in the container
        with self.pagination_container:
            with ui.row().classes('w-full justify-center items-center mt-4 gap-2'):
                # Previous button
                prev_btn = ui.button('Previous', 
                                   on_click=lambda: self._change_page(self.current_page - 1),
                                   icon='chevron_left')
                prev_btn.props('flat')
                if self.current_page <= 1:
                    prev_btn.props('disable')
                
                # Page info
                ui.label(f'Page {self.current_page} of {self.total_pages}').classes('mx-4')
                
                # Next button  
                next_btn = ui.button('Next',
                                   on_click=lambda: self._change_page(self.current_page + 1),
                                   icon='chevron_right')
                next_btn.props('flat')
                if self.current_page >= self.total_pages:
                    next_btn.props('disable')
                
                # Page size selector
                ui.separator().props('vertical').classes('mx-4')
                page_size_options = {'10': '10 per page', '25': '25 per page', '50': '50 per page', '100': '100 per page'}
                page_size_select = ui.select(
                    options=page_size_options,
                    value=str(self.page_size),
                    label='Page Size'
                ).classes('w-32')
                page_size_select.on_value_change(self._on_page_size_change)
    
    def _change_page(self, new_page: int):
        """Change to a specific page - synchronous update using cached data."""
        if 1 <= new_page <= self.total_pages:
            self.current_page = new_page
            # Update table synchronously using cached data (no async needed for pagination)
            self._update_table_from_cache()
    
    def _update_table_from_cache(self):
        """Update table and pagination controls synchronously from cached data."""
        try:
            # Get paginated data from cache (this is synchronous)
            analysis_data, total_records = self._get_analysis_data()
            
            # Update table if it exists
            if self.analysis_table:
                self.analysis_table.rows = analysis_data
                self.analysis_table.update()  # Force UI refresh
            
            # Update pagination controls with current state
            self._create_pagination_controls()
            
        except Exception as e:
            logger.error(f"Error updating table from cache: {e}", exc_info=True)
    
    def _on_page_size_change(self, event):
        """Handle page size change - synchronous update using cached data."""
        try:
            new_size = int(event.value)
            self.page_size = new_size
            self.current_page = 1  # Reset to first page
            # Recalculate total pages based on new page size
            self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
            # Update table synchronously using cached data (no async needed)
            self._update_table_from_cache()
        except ValueError:
            pass
    
    def _get_expert_options(self) -> dict:
        """Get available expert instances for filtering."""
        try:
            with get_db() as session:
                # Get all expert instances
                expert_instances = session.exec(select(ExpertInstance)).all()
                
                # Build options dictionary
                options = {'all': 'All Experts'}
                for expert in expert_instances:
                    # Use alias if available, otherwise expert type
                    # Format: alias-ID or expertType-ID
                    base_name = expert.alias or expert.expert
                    display_name = f"{base_name}-{expert.id}"
                    options[str(expert.id)] = display_name
                
                return options
        except Exception as e:
            logger.error(f"Error getting expert options: {e}", exc_info=True)
            return {'all': 'All Experts'}
    
    def _on_status_filter_change(self, event):
        """Handle status filter change."""
        self.status_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_expert_filter_change(self, event):
        """Handle expert filter change."""
        self.expert_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_type_filter_change(self, event):
        """Handle analysis type filter change."""
        self.type_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_recommendation_filter_change(self, event):
        """Handle recommendation filter change."""
        self.recommendation_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_symbol_filter_change(self, event):
        """Handle symbol filter change - queries database with filter applied."""
        self.symbol_filter = event.value.strip().upper()  # Normalize input (uppercase for consistency)
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()

    def _clear_filters(self):
        """Clear all filters."""
        self.status_filter = 'all'
        self.expert_filter = 'all'
        self.type_filter = 'all'
        self.recommendation_filter = 'all'
        self.symbol_filter = ''
        self.current_page = 1
        if hasattr(self, 'status_select'):
            self.status_select.value = 'all'
        if hasattr(self, 'expert_select'):
            self.expert_select.value = 'all'
        if hasattr(self, 'type_select'):
            self.type_select.value = 'all'
        if hasattr(self, 'recommendation_select'):
            self.recommendation_select.value = 'all'
        if hasattr(self, 'symbol_input'):
            self.symbol_input.value = ''
        self.refresh_data()

    def _get_queue_info(self) -> dict:
        """Get worker queue information."""
        try:
            worker_queue = self._get_worker_queue()
            worker_count = worker_queue.get_worker_count()
            all_tasks_dict = worker_queue.get_all_tasks()
            
            # Convert dict to list of tasks for easier iteration
            all_tasks = list(all_tasks_dict.values()) if isinstance(all_tasks_dict, dict) else all_tasks_dict
            
            # logger.debug(f"All tasks count: {len(all_tasks)}")
            
            # Debug: Log first few tasks to see their structure
            # if all_tasks:
            #     for i, t in enumerate(all_tasks[:3]):
            #         logger.debug(f"Task {i}: type={type(t)}, dir={[attr for attr in dir(t) if not attr.startswith('_')]}")
            #         if hasattr(t, 'status'):
            #             logger.debug(f"  status={t.status}, status_type={type(t.status)}")
            #         if hasattr(t, 'state'):
            #             logger.debug(f"  state={t.state}, state_type={type(t.state)}")
            
            # Filter tasks safely, checking if they have status attribute
            # Status can be string or enum, so check both
            running_tasks = 0
            pending_tasks = 0
            
            for t in all_tasks:
                task_status = None
                
                # Try status attribute first
                if hasattr(t, 'status'):
                    task_status = t.status
                # Try state attribute as fallback
                elif hasattr(t, 'state'):
                    task_status = t.state
                
                if task_status:
                    # Handle both enum and string comparisons
                    if task_status == WorkerTaskStatus.RUNNING or task_status == 'running':
                        running_tasks += 1
                    elif task_status == WorkerTaskStatus.PENDING or task_status == 'pending':
                        pending_tasks += 1
            
            # Total should only include active tasks (pending + running), not completed/failed
            total_tasks = running_tasks + pending_tasks
            
            # logger.info(f"Queue info: workers={worker_count}, running={running_tasks}, pending={pending_tasks}, total={total_tasks}")
            
            return {
                'worker_count': worker_count,
                'running_tasks': running_tasks,
                'pending_tasks': pending_tasks,
                'total_tasks': total_tasks
            }
        except Exception as e:
            logger.error(f"Error getting queue info: {e}", exc_info=True)
            return {
                'worker_count': 0,
                'running_tasks': 0,
                'pending_tasks': 0,
                'total_tasks': 0
            }

    def _update_queue_status_display(self):
        """Update the queue status display labels with current queue info."""
        try:
            queue_info = self._get_queue_info()
            persisted_info = self._get_persisted_queue_info()
            
            # Update labels if they exist (they are created in _create_queue_status)
            if self.queue_status_labels['worker_count']:
                self.queue_status_labels['worker_count'].set_text(f"Workers: {queue_info['worker_count']}")
            if self.queue_status_labels['running_tasks']:
                self.queue_status_labels['running_tasks'].set_text(f"Running: {queue_info['running_tasks']}")
            if self.queue_status_labels['pending_tasks']:
                self.queue_status_labels['pending_tasks'].set_text(f"Pending: {queue_info['pending_tasks']}")
            if self.queue_status_labels['total_tasks']:
                self.queue_status_labels['total_tasks'].set_text(f"Total Tasks: {queue_info['total_tasks']}")
            
            # Update persisted queue labels
            if self.queue_status_labels.get('persisted_pending'):
                self.queue_status_labels['persisted_pending'].set_text(f"Saved Pending: {persisted_info['pending']}")
            if self.queue_status_labels.get('persisted_running'):
                self.queue_status_labels['persisted_running'].set_text(f"Saved Running: {persisted_info['running']}")
            
            # Update button visibility
            self._update_persisted_buttons_visibility(persisted_info['total'])
            
            # Also refresh the queued tasks table
            if hasattr(self, 'queued_tasks_container'):
                self._create_queued_tasks_table()
        except Exception as e:
            logger.error(f"Error updating queue status display: {e}", exc_info=True)

    def cancel_analysis(self, event_data):
        """Cancel an analysis job."""
        analysis_id = None
        try:
            # Extract analysis_id from event data
            # NiceGUI passes GenericEventArguments with args attribute
            if hasattr(event_data, 'args'):
                if hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                    analysis_id = int(event_data.args[0])
                elif isinstance(event_data.args, int):
                    analysis_id = event_data.args
                else:
                    logger.error(f"Invalid event data args for cancel_analysis: {event_data.args}", exc_info=True)
                    ui.notify("Invalid event data", type='negative')
                    return
            elif isinstance(event_data, int):
                analysis_id = event_data
            else:
                logger.error(f"Invalid event data for cancel_analysis: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Get the market analysis
            analysis = get_instance(MarketAnalysis, analysis_id)
            if not analysis:
                ui.notify("Analysis not found", type='negative')
                return
            
            if analysis.status != MarketAnalysisStatus.PENDING:
                ui.notify("Can only cancel pending analyses", type='negative')
                return
            
            # Try to cancel the task in the worker queue
            success, message = self._get_worker_queue().cancel_analysis_by_market_analysis_id(analysis_id)
            
            if success:
                # Update the analysis status
                analysis.status = MarketAnalysisStatus.CANCELLED
                from ...core.db import update_instance
                update_instance(analysis)
                
                ui.notify(f"Analysis {analysis_id} cancelled successfully", type='positive')
                self.refresh_data()
            else:
                ui.notify(f"Failed to cancel analysis: {message}", type='warning')
                
        except Exception as e:
            logger.error(f"Error cancelling analysis {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error cancelling analysis: {str(e)}", type='negative')



    def view_analysis_details(self, event_data):
        """Navigate to the detailed analysis page."""
        analysis_id = None
        try:
            # Extract analysis_id from event data
            if hasattr(event_data, 'args') and hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                analysis_id = int(event_data.args[0])
            elif isinstance(event_data, int):
                analysis_id = event_data
            elif hasattr(event_data, 'args') and isinstance(event_data.args, int):
                analysis_id = event_data.args
            else:
                logger.error(f"Invalid event data for view_analysis_details: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Navigate to the detail page via dialog
            if not hasattr(self, '_analysis_dialog'):
                self._analysis_dialog = MarketAnalysisDetailDialog()
            self._analysis_dialog.open(analysis_id)
            
        except Exception as e:
            logger.error(f"Error navigating to analysis details {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error opening details: {str(e)}", type='negative')
    
    def troubleshoot_ruleset(self, event_data):
        """Navigate to the ruleset test page with market analysis parameters."""
        analysis_id = None
        try:
            # Extract analysis_id from event data
            if hasattr(event_data, 'args') and hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                analysis_id = int(event_data.args[0])
            elif isinstance(event_data, int):
                analysis_id = event_data
            elif hasattr(event_data, 'args') and isinstance(event_data.args, int):
                analysis_id = event_data.args
            else:
                logger.error(f"Invalid event data for troubleshoot_ruleset: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Navigate to ruleset test page with market analysis ID
            ui.navigate.to(f'/rulesettest?market_analysis_id={analysis_id}')
            
        except Exception as e:
            logger.error(f"Error navigating to ruleset test {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error opening ruleset test: {str(e)}", type='negative')
    
    def view_rule_evaluation(self, event_data):
        """Show rule evaluation details from a market analysis in a dialog."""
        analysis_id = None
        try:
            # Extract analysis_id from event data
            if hasattr(event_data, 'args') and hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                analysis_id = int(event_data.args[0])
            elif isinstance(event_data, int):
                analysis_id = event_data
            elif hasattr(event_data, 'args') and isinstance(event_data.args, int):
                analysis_id = event_data.args
            else:
                logger.error(f"Invalid event data for view_rule_evaluation: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Load evaluation details from the analysis
            from ..components.RuleEvaluationDisplay import render_rule_evaluations
            
            with get_db() as session:
                # Get the analysis with recommendations
                analysis = session.get(MarketAnalysis, analysis_id)
                if not analysis:
                    ui.notify('Analysis not found', type='warning')
                    return
                
                # Find evaluation details from any recommendation's trade action results
                evaluation_data = None
                for rec in analysis.expert_recommendations:
                    if hasattr(rec, 'trade_action_results') and rec.trade_action_results:
                        for result in rec.trade_action_results:
                            if result.data and 'evaluation_details' in result.data:
                                evaluation_data = result.data['evaluation_details']
                                break
                    if evaluation_data:
                        break
                
                if not evaluation_data:
                    ui.notify('No rule evaluation details found for this analysis', type='warning')
                    return
                
                # Show dialog with evaluation details
                with ui.dialog() as eval_dialog, ui.card().classes('w-full max-w-4xl'):
                    ui.label('🔍 Rule Evaluation Details').classes('text-h6 mb-4')
                    ui.label(f'Analysis #{analysis_id} - {analysis.symbol}').classes('text-sm text-grey-6 mb-4')
                    
                    # Use the reusable component to display evaluation details
                    render_rule_evaluations(evaluation_data, show_actions=True, compact=False)
                    
                    with ui.row().classes('w-full justify-end mt-4'):
                        ui.button('Close', on_click=eval_dialog.close).props('outline')
                
                eval_dialog.open()
                
        except Exception as e:
            logger.error(f"Error viewing rule evaluation {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error viewing rule evaluation: {str(e)}", type='negative')

    def rerun_analysis(self, event_data):
        """Re-run a failed analysis by clearing outputs and re-queuing."""
        analysis_id = None
        try:
            # Extract analysis_id from event data
            if hasattr(event_data, 'args') and hasattr(event_data.args, '__len__') and len(event_data.args) > 0:
                analysis_id = int(event_data.args[0])
            elif isinstance(event_data, int):
                analysis_id = event_data
            elif hasattr(event_data, 'args') and isinstance(event_data.args, int):
                analysis_id = event_data.args
            else:
                logger.error(f"Invalid event data for rerun_analysis: {event_data}", exc_info=True)
                ui.notify("Invalid event data", type='negative')
                return
            
            # Get the market analysis
            analysis = get_instance(MarketAnalysis, analysis_id)
            if not analysis:
                ui.notify("Analysis not found", type='negative')
                return
            
            # Only allow re-run for failed analyses
            if analysis.status != MarketAnalysisStatus.FAILED:
                ui.notify("Can only re-run failed analyses", type='warning')
                return
            
            # Clear existing data
            from ...core.db import get_db
            from ...core.models import AnalysisOutput, ExpertRecommendation
            from sqlmodel import select
            
            with get_db() as session:
                # Delete all analysis outputs
                outputs_statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id)
                outputs = session.exec(outputs_statement).all()
                for output in outputs:
                    session.delete(output)
                
                # Delete all expert recommendations
                recs_statement = select(ExpertRecommendation).where(ExpertRecommendation.market_analysis_id == analysis_id)
                recommendations = session.exec(recs_statement).all()
                for rec in recommendations:
                    session.delete(rec)
                
                # Clear state and reset status
                analysis.state = None
                analysis.status = MarketAnalysisStatus.PENDING
                session.add(analysis)
                session.commit()
                session.refresh(analysis)
                
                logger.info(f"Cleared {len(outputs)} outputs and {len(recommendations)} recommendations for analysis {analysis_id}")
            
            # Re-queue the analysis with the existing market_analysis_id
            try:
                worker_queue = self._get_worker_queue()
                task_id = worker_queue.submit_analysis_task(
                    expert_instance_id=analysis.expert_instance_id,
                    symbol=analysis.symbol,
                    subtype=analysis.subtype,
                    priority=0,  # Normal priority for re-runs
                    market_analysis_id=analysis_id  # Reuse the same MarketAnalysis record
                )
                
                ui.notify(f"Analysis {analysis_id} queued for re-run (Task: {task_id})", type='positive')
                self.refresh_data()
                
            except ValueError as ve:
                # Task already exists - this shouldn't happen since we reset status
                logger.warning(f"Duplicate task when re-running analysis {analysis_id}: {ve}")
                ui.notify(f"Analysis already queued: {str(ve)}", type='warning')
                self.refresh_data()
                
            except Exception as qe:
                # Restore failed status if queueing failed
                analysis.status = MarketAnalysisStatus.FAILED
                from ...core.db import update_instance
                update_instance(analysis)
                logger.error(f"Failed to queue re-run for analysis {analysis_id}: {qe}", exc_info=True)
                ui.notify(f"Failed to queue analysis for re-run: {str(qe)}", type='negative')
                
        except Exception as e:
            logger.error(f"Error re-running analysis {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error re-running analysis: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the data in all tables."""
        try:
            # Update analysis table asynchronously - force fresh data fetch (bypass cache)
            if self.analysis_table:
                asyncio.create_task(self._async_refresh_analysis_table(force_fresh=True))
            
            # Update Smart Risk Manager table
            if hasattr(self, 'smart_risk_table') and self.smart_risk_table:
                smart_risk_data, _ = self._get_smart_risk_manager_data()
                self.smart_risk_table.rows = smart_risk_data
            
            # Update worker queue status display
            self._update_queue_status_display()
            
        except RuntimeError as e:
            # Handle client disconnection gracefully - stop auto-refresh timer
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                logger.debug("[JobMonitoringTab] Client disconnected in refresh_data, stopping auto-refresh timer")
                self.stop_auto_refresh()
            else:
                logger.error(f"Error refreshing job monitoring data: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error refreshing job monitoring data: {e}", exc_info=True)
    
    async def _async_refresh_analysis_table(self, force_fresh: bool = False):
        """Asynchronously refresh analysis table data without blocking UI.
        
        Args:
            force_fresh: If True, always fetch fresh data from database (ignore cache).
                        Used by auto-refresh timer to show current job status.
        """
        try:
            # For auto-refresh (force_fresh=True), always invalidate cache to get current job status
            # but preserve the current page
            if force_fresh:
                logger.debug("[JobMonitoringTab] Auto-refresh: invalidating cache to get fresh job status")
                self._invalidate_cache()
            
            # Check if filters changed - if not and cache valid, just re-paginate the cached data
            if not self._should_invalidate_cache() and self.cached_analysis_data and not force_fresh:
                # Filters didn't change, just use cached data
                logger.debug("[JobMonitoringTab] Using cached analysis data (no filter changes)")
                analysis_data, total_records = self._get_analysis_data(preserve_page=True)
            else:
                # Filters changed or cache empty or force_fresh - fetch fresh data in background
                # preserve_page=True for auto-refresh to maintain current pagination
                logger.debug("[JobMonitoringTab] Fetching fresh analysis data")
                analysis_data, total_records = await asyncio.to_thread(
                    lambda: self._get_analysis_data(preserve_page=force_fresh)
                )
            
            self.total_records = total_records
            self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
            
            # Ensure current page is valid
            if self.current_page > self.total_pages:
                self.current_page = max(1, self.total_pages)
            
            # Update table if it exists
            if self.analysis_table:
                self.analysis_table.rows = analysis_data
            
            # Re-create pagination controls to update button states
            self._create_pagination_controls()
            
            logger.debug(f"[JobMonitoringTab] Analysis table refreshed with {len(analysis_data)} records")
        except RuntimeError as e:
            # Handle client disconnection gracefully - stop auto-refresh timer
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                logger.debug("[JobMonitoringTab] Client disconnected, stopping auto-refresh timer")
                self.stop_auto_refresh()
            else:
                logger.error(f"Error in async analysis table refresh: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in async analysis table refresh: {e}", exc_info=True)

    def toggle_auto_refresh(self, enabled: bool):
        """Toggle auto-refresh on/off."""
        if enabled:
            self.start_auto_refresh()
        else:
            self.stop_auto_refresh()

    def start_auto_refresh(self):
        """Start auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
        
        self.refresh_timer = ui.timer(30.0, self.refresh_data)  # Refresh every 30 seconds

    def stop_auto_refresh(self):
        """Stop auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
            self.refresh_timer = None


class ManualAnalysisTab:
    def __init__(self):
        self.expert_instance_id = None
        # Initialize with default analysis type
        from ...core.types import AnalysisUseCase
        self.analysis_type = AnalysisUseCase.ENTER_MARKET.value
        self.instrument_selector = None
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Manual Analysis Jobs').classes('text-lg font-bold')
            ui.label('Submit analysis jobs for multiple instruments with selected expert and analysis type').classes('text-sm text-gray-600 mb-4')
            
            # Configuration row
            with ui.row().classes('w-full gap-4 mb-4'):
                with ui.column().classes('flex-1'):
                    # Expert instance selection
                    expert_instances = get_all_instances(ExpertInstance)
                    # Use alias if available, otherwise fall back to expert class + ID
                    expert_options = {}
                    for exp in expert_instances:
                        if exp.enabled:
                            if exp.alias:
                                display_name = f"{exp.alias} (ID: {exp.id})"
                            else:
                                display_name = f"{exp.expert} (ID: {exp.id})"
                            expert_options[f"{exp.id}"] = display_name
                    
                    self.expert_select = ui.select(
                        options=expert_options,
                        label='Expert Instance',
                        on_change=self.on_expert_change
                    ).classes('w-full')
                    
                with ui.column().classes('flex-1'):
                    # Analysis type selection
                    from ...core.types import AnalysisUseCase
                    analysis_options = {
                        AnalysisUseCase.ENTER_MARKET.value: 'Enter Market Analysis',
                        AnalysisUseCase.OPEN_POSITIONS.value: 'Open Positions Analysis'
                    }
                    
                    self.analysis_type_select = ui.select(
                        options=analysis_options,
                        label='Analysis Type',
                        value=AnalysisUseCase.ENTER_MARKET.value,
                        on_change=self.on_analysis_type_change
                    ).classes('w-full')
                    
                with ui.column():
                    ui.button(
                        'Run Analysis for Selected', 
                        on_click=self.submit_bulk_analysis, 
                        icon='play_arrow'
                    ).classes('mt-6').props('color=primary size=md')
            
            # Instrument selector (will be populated when expert is selected)
            self.instrument_selector_container = ui.column().classes('w-full')
            
            # Initially disable the analysis type and button
            self.analysis_type_select.disable()
            
    def on_expert_change(self):
        """Handle expert instance selection change."""
        self.expert_instance_id = self.expert_select.value
        
        if not self.expert_instance_id:
            # Clear instrument selector
            self.instrument_selector_container.clear()
            self.analysis_type_select.disable()
            return
            
        try:
            # Get the expert instance and its enabled instruments
            from ...core.utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(int(self.expert_instance_id))
            
            if expert:
                # Get instrument selection method from expert settings
                instrument_selection_method = expert.settings.get('instrument_selection_method', 'static')
                logger.debug(f"Expert {self.expert_instance_id} using instrument selection method: {instrument_selection_method}")
                
                # Get expert properties to check capabilities
                expert_properties = expert.__class__.get_expert_properties()
                can_recommend_instruments = expert_properties.get('can_recommend_instruments', False)
                
                # Clear and recreate instrument selector based on selection method
                self.instrument_selector_container.clear()
                self._render_instrument_selection_ui(expert, instrument_selection_method, can_recommend_instruments)
                
                # Enable analysis type selection
                self.analysis_type_select.enable()
            else:
                ui.notify("Failed to load expert instance", type='negative')
                
        except Exception as e:
            logger.error(f"Error loading expert instruments: {e}", exc_info=True)
            ui.notify(f"Error loading expert: {str(e)}", type='negative')
    
    def _render_instrument_selection_ui(self, expert, selection_method: str, can_recommend_instruments: bool):
        """Render the appropriate instrument selection UI based on method."""
        with self.instrument_selector_container:
            if selection_method == 'expert':
                if can_recommend_instruments:
                    # Expert will select instruments - show message
                    with ui.card().classes('w-full p-4 alert-banner info'):
                        with ui.row():
                            ui.icon('auto_awesome').classes('text-[#4dabf7] text-xl mr-3')
                            with ui.column():
                                ui.label('Expert-Driven Instrument Selection').classes('text-lg font-semibold text-[#4dabf7]')
                                ui.label('This expert will automatically select the instruments for analysis. No manual selection required.').classes('text-secondary-custom')
                    self.instrument_selector = None  # No manual selector needed
                else:
                    # Expert doesn't support instrument selection - fall back to static
                    with ui.card().classes('w-full p-4 alert-banner warning'):
                        with ui.row():
                            ui.icon('warning').classes('text-[#ffd93d] text-xl mr-3')
                            with ui.column():
                                ui.label('Expert Selection Not Supported').classes('text-lg font-semibold text-[#ffd93d]')
                                ui.label('This expert does not support automatic instrument selection. Using manual selection instead.').classes('text-secondary-custom')
                    self._render_static_selector(expert)
                    
            elif selection_method == 'dynamic':
                # Check if this is OPEN_POSITIONS analysis - skip AI selection
                from ...core.types import AnalysisUseCase
                if self.analysis_type == AnalysisUseCase.OPEN_POSITIONS.value:
                    # For OPEN_POSITIONS, just show info that we'll use existing positions
                    with ui.card().classes('w-full p-4 alert-banner info'):
                        with ui.row():
                            ui.icon('inventory').classes('text-[#9775fa] text-xl mr-3')
                            with ui.column():
                                ui.label('Open Positions Analysis').classes('text-lg font-semibold text-[#9775fa]')
                                ui.label('Analysis will be performed on existing open positions for this expert.').classes('text-secondary-custom')
                    
                    # Add manual override option
                    with ui.column().classes('w-full mt-4'):
                        ui.label('Override Instruments (Optional):').classes('text-sm font-medium mb-2')
                        ui.label('Enter comma-separated symbols to override automatic open position detection').classes('text-xs text-secondary-custom mb-1')
                        self.instrument_override_input = ui.input(
                            placeholder='e.g., AAPL, GOOGL, MSFT (leave empty to use open positions)'
                        ).classes('w-full')
                    
                    self.instrument_selector = None
                    self.ai_prompt_textarea = None
                else:
                    # AI-driven dynamic selection - show prompt input
                    with ui.card().classes('w-full p-4 alert-banner success'):
                        with ui.row():
                            ui.icon('psychology').classes('text-[#00d4aa] text-xl mr-3')
                            with ui.column():
                                ui.label('AI-Powered Dynamic Instrument Selection').classes('text-lg font-semibold text-[#00d4aa]')
                                ui.label('Enter a prompt to let AI select instruments based on your criteria.').classes('text-secondary-custom')
                    
                    with ui.column().classes('w-full mt-4'):
                        ui.label('AI Selection Prompt:').classes('text-sm font-medium mb-2')
                        
                        # Get default prompt from AIInstrumentSelector
                        # Get model from expert settings - use dynamic_instrument_selection_model setting
                        model_string = expert.settings.get('dynamic_instrument_selection_model')
                        if not model_string:
                            logger.error(f"Expert {expert.id} has no dynamic_instrument_selection_model setting - cannot perform AI instrument selection")
                            ui.notify("Expert is not configured for AI instrument selection (missing model setting)", type='negative')
                            return
                        
                        from ...core.AIInstrumentSelector import AIInstrumentSelector
                        ai_selector = AIInstrumentSelector(model_string=model_string)
                        default_prompt = ai_selector.get_default_prompt()
                        
                        self.ai_prompt_textarea = ui.textarea(
                            value=default_prompt,
                            placeholder='Enter your prompt for AI instrument selection...'
                        ).classes('w-full').props('rows=6')
                        
                        with ui.row().classes('w-full justify-between mt-2'):
                            ui.button('Reset to Default', on_click=lambda: self.ai_prompt_textarea.set_value(default_prompt), icon='refresh').props('flat')
                            self.ai_generate_button = ui.button('Generate AI Selection', on_click=self._generate_ai_selection, icon='auto_awesome').props('color=positive')
                        
                        # Container for AI-selected instruments (will be populated after AI selection)
                        self.ai_results_container = ui.column().classes('w-full mt-4')
                        
                        # Add manual override option for ENTER_MARKET as well
                        ui.label('Override Instruments (Optional):').classes('text-sm font-medium mb-2 mt-4')
                        ui.label('Enter comma-separated symbols to override AI selection').classes('text-xs text-gray-500 mb-1')
                        self.instrument_override_input = ui.input(
                            placeholder='e.g., AAPL, GOOGL, MSFT (leave empty to use AI selection)'
                        ).classes('w-full')
                    
                    self.instrument_selector = None  # Will be created after AI selection
                
            else:  # static (default)
                self._render_static_selector(expert)
    
    def _render_static_selector(self, expert):
        """Render the traditional static instrument selector."""
        enabled_instruments = expert.get_enabled_instruments()
        logger.debug(f"Expert {self.expert_instance_id} has {len(enabled_instruments)} enabled instruments")
        
        from ..components.InstrumentSelector import InstrumentSelector
        self.instrument_selector = InstrumentSelector(
            on_selection_change=self.on_instrument_selection_change,
            instrument_list=enabled_instruments if enabled_instruments else None,
            hide_weights=True
        )
        self.instrument_selector.render()
    
    async def _generate_ai_selection(self):
        """Generate AI instrument selection based on user prompt."""
        try:
            prompt = self.ai_prompt_textarea.value.strip()
            if not prompt:
                ui.notify("Please enter a prompt for AI selection", type='negative')
                return
            
            # Get the expert instance to access its model setting
            from ...core.utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(int(self.expert_instance_id))
            
            if not expert:
                ui.notify("Expert instance not found", type='negative')
                return
            
            # Get model from expert settings - use dynamic_instrument_selection_model setting
            model_string = expert.settings.get('dynamic_instrument_selection_model')
            if not model_string:
                logger.error(f"Expert {expert.id} has no dynamic_instrument_selection_model setting - cannot perform AI instrument selection")
                ui.notify("Expert is not configured for AI instrument selection (missing model setting)", type='negative')
                return
            
            # Disable button and show spinner
            self.ai_generate_button.props('loading')
            self.ai_generate_button.set_enabled(False)
            
            # Show loading indicator in results container
            with self.ai_results_container:
                self.ai_results_container.clear()
                with ui.row().classes('items-center'):
                    ui.spinner('dots', size='sm').classes('mr-2')
                    ui.label('AI is selecting instruments...').classes('text-gray-600')
            
            # Perform AI selection in background thread
            from ...core.AIInstrumentSelector import AIInstrumentSelector
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            
            ai_selector = AIInstrumentSelector(model_string=model_string)
            
            # Run AI selection in thread pool to avoid blocking UI
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                selected_symbols = await loop.run_in_executor(executor, ai_selector.select_instruments, prompt)
            
            # Auto-add instruments to database if they don't exist
            if selected_symbols:
                try:
                    from ...core.InstrumentAutoAdder import get_instrument_auto_adder
                    auto_adder = get_instrument_auto_adder()
                    auto_adder.queue_instruments_for_addition(
                        symbols=selected_symbols,
                        expert_shortname='ai_selector',
                        source='ai'
                    )
                except Exception as e:
                    logger.warning(f"Could not queue AI-selected instruments for auto-addition: {e}")
            
            # Clear loading and show results
            self.ai_results_container.clear()
            
            if selected_symbols:
                with self.ai_results_container:
                    with ui.card().classes('w-full p-4 alert-banner success'):
                        with ui.column().classes('w-full'):
                            with ui.row().classes('items-center mb-3'):
                                ui.icon('check_circle').classes('text-[#00d4aa] mr-2')
                                ui.label(f'AI Selected {len(selected_symbols)} Instruments').classes('text-lg font-semibold')
                            
                            # Show selected symbols as badges
                            with ui.row().classes('flex-wrap gap-2'):
                                for symbol in selected_symbols:
                                    ui.badge(symbol).props('color=primary')
                            
                            # Create traditional instrument selector with AI-selected instruments
                            from ..components.InstrumentSelector import InstrumentSelector
                            self.instrument_selector = InstrumentSelector(
                                on_selection_change=self.on_instrument_selection_change,
                                instrument_list=selected_symbols,  # Pass list of symbol strings
                                hide_weights=True
                            )
                            self.instrument_selector.render()
                            
                            # Pre-select all AI-selected instruments
                            # The InstrumentSelector needs instrument IDs, so we need to get them from the loaded instruments
                            instrument_configs = {}
                            for instrument in self.instrument_selector.instruments:
                                instrument_configs[instrument.id] = {'enabled': True, 'weight': 100.0}
                            self.instrument_selector.set_selected_instruments(instrument_configs)
                
                ui.notify(f"AI selected {len(selected_symbols)} instruments successfully", type='positive')
            else:
                with self.ai_results_container:
                    with ui.card().classes('w-full p-4 alert-banner danger'):
                        with ui.row():
                            ui.icon('error').classes('text-[#ff6b6b] mr-2')
                            ui.label('AI selection failed. Please try a different prompt or check your OpenAI API key.').classes('text-[#ff6b6b]')
                ui.notify("AI instrument selection failed", type='negative')
                
        except Exception as e:
            logger.error(f"Error during AI instrument selection: {e}", exc_info=True)
            self.ai_results_container.clear()
            with self.ai_results_container:
                with ui.card().classes('w-full p-4 alert-banner danger'):
                    with ui.row():
                        ui.icon('error').classes('text-[#ff6b6b] mr-2')
                        ui.label(f'Error: {str(e)}').classes('text-[#ff6b6b]')
            ui.notify(f"Error during AI selection: {str(e)}", type='negative')
        finally:
            # Re-enable button and remove spinner
            self.ai_generate_button.props(remove='loading')
            self.ai_generate_button.set_enabled(True)
    
    def on_analysis_type_change(self):
        """Handle analysis type selection change."""
        self.analysis_type = self.analysis_type_select.value
        logger.debug(f"Analysis type changed to: {self.analysis_type}")
    
    def on_instrument_selection_change(self, selected_instruments):
        """Handle instrument selection change."""
        logger.debug(f"Selected {len(selected_instruments)} instruments for analysis")
    
    def submit_bulk_analysis(self):
        """Submit analysis jobs for all selected instruments."""
        from ...core.types import AnalysisUseCase
        
        try:
            if not self.expert_instance_id:
                ui.notify("Please select an expert instance", type='negative')
                return
                
            if not self.analysis_type:
                ui.notify("Please select an analysis type", type='negative')
                return
            
            # Get the expert instance to determine selection method
            from ...core.utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(int(self.expert_instance_id))
            
            if not expert:
                ui.notify("Failed to load expert instance", type='negative')
                return
            
            # Get instrument selection method
            instrument_selection_method = expert.settings.get('instrument_selection_method', 'static')
            expert_properties = expert.__class__.get_expert_properties()
            can_recommend_instruments = expert_properties.get('can_recommend_instruments', False)
            
            # Get instruments based on selection method
            if instrument_selection_method == 'expert' and can_recommend_instruments:
                # Expert-driven selection
                try:
                    instruments_to_analyze = expert.get_recommended_instruments()
                    if not instruments_to_analyze:
                        ui.notify("Expert returned no instrument recommendations", type='warning')
                        return
                    
                    # Auto-add instruments to database if they don't exist
                    try:
                        from ...core.InstrumentAutoAdder import get_instrument_auto_adder
                        auto_adder = get_instrument_auto_adder()
                        auto_adder.queue_instruments_for_addition(
                            symbols=instruments_to_analyze,
                            expert_shortname=expert.shortname,
                            source='expert'
                        )
                    except Exception as e:
                        logger.warning(f"Could not queue instruments for auto-addition: {e}")
                    
                    # Convert to expected format
                    selected_instruments = [{'name': symbol} for symbol in instruments_to_analyze]
                    logger.info(f"Expert recommended {len(selected_instruments)} instruments: {instruments_to_analyze}")
                except Exception as e:
                    logger.error(f"Error getting expert instrument recommendations: {e}", exc_info=True)
                    ui.notify(f"Error getting expert recommendations: {str(e)}", type='negative')
                    return
            elif instrument_selection_method == 'dynamic':
                # First check for manual override
                instrument_override = getattr(self, 'instrument_override_input', None)
                if instrument_override and instrument_override.value and instrument_override.value.strip():
                    # User provided manual override - parse comma-separated symbols
                    symbols_str = instrument_override.value.strip()
                    selected_symbols = [s.strip().upper() for s in symbols_str.split(',') if s.strip()]
                    
                    if not selected_symbols:
                        ui.notify("Override input is empty or invalid. Please enter comma-separated symbols.", type='negative')
                        return
                    
                    logger.info(f"Using manual instrument override for analysis: {selected_symbols}")
                    ui.notify(f"Using manual override: {len(selected_symbols)} symbol(s)", type='info')
                    
                    # Convert to expected format
                    selected_instruments = [{'name': symbol} for symbol in selected_symbols]
                
                # For OPEN_POSITIONS with dynamic selection, use special symbol that will be expanded by job execution
                elif self.analysis_type == AnalysisUseCase.OPEN_POSITIONS.value:
                    # Use special OPEN_POSITIONS symbol - the job execution will expand this to actual open positions
                    selected_instruments = [{'name': 'OPEN_POSITIONS'}]
                    logger.info(f"Using OPEN_POSITIONS symbol for expert {expert.id} - will expand to open positions at execution time")
                    ui.notify("Analysis will be performed on all open positions", type='info')
                
                # Otherwise, use AI selection (for ENTER_MARKET)
                elif self.instrument_selector:
                    # User already generated AI selection - use selected instruments
                    selected_instruments = self.instrument_selector.get_selected_instruments()
                    
                    if not selected_instruments:
                        ui.notify("Please select at least one instrument from AI-generated list", type='negative')
                        return
                else:
                    # User didn't click "Generate AI Selection" - automatically fetch all dynamic instruments
                    try:
                        # Get model from expert settings
                        model_string = expert.settings.get('dynamic_instrument_selection_model')
                        if not model_string:
                            logger.error(f"Expert {expert.id} has no dynamic_instrument_selection_model setting")
                            ui.notify("Expert is not configured for AI instrument selection (missing model setting)", type='negative')
                            return
                        
                        # Get prompt from textarea
                        prompt = self.ai_prompt_textarea.value.strip()
                        if not prompt:
                            ui.notify("Please enter a prompt for AI selection or click 'Generate AI Selection' first", type='negative')
                            return
                        
                        # Show notification that we're generating selection
                        ui.notify("Generating AI instrument selection automatically...", type='info')
                        
                        # Perform AI selection
                        from ...core.AIInstrumentSelector import AIInstrumentSelector
                        ai_selector = AIInstrumentSelector(model_string=model_string)
                        selected_symbols = ai_selector.select_instruments(prompt)
                        
                        if not selected_symbols:
                            ui.notify("AI instrument selection returned no results", type='negative')
                            return
                        
                        # Auto-add instruments to database if they don't exist
                        try:
                            from ...core.InstrumentAutoAdder import get_instrument_auto_adder
                            auto_adder = get_instrument_auto_adder()
                            auto_adder.queue_instruments_for_addition(
                                symbols=selected_symbols,
                                expert_shortname='ai_selector',
                                source='ai'
                            )
                        except Exception as e:
                            logger.warning(f"Could not queue AI-selected instruments for auto-addition: {e}")
                        
                        # Convert to expected format (all instruments selected)
                        selected_instruments = [{'name': symbol} for symbol in selected_symbols]
                        logger.info(f"AI automatically selected {len(selected_instruments)} instruments: {selected_symbols}")
                        
                    except Exception as e:
                        logger.error(f"Error during automatic AI instrument selection: {e}", exc_info=True)
                        ui.notify(f"Error during AI selection: {str(e)}", type='negative')
                        return
            else:
                # Static (manual selection required)
                if not self.instrument_selector:
                    ui.notify("No instruments available for selection", type='negative')
                    return
                
                selected_instruments = self.instrument_selector.get_selected_instruments()
                
                if not selected_instruments:
                    ui.notify("Please select at least one instrument", type='negative')
                    return
            
            # Submit analysis jobs for all selected instruments
            from ...core.JobManager import get_job_manager
            import time
            import uuid
            
            job_manager = get_job_manager()
            
            # Convert analysis type string to enum
            subtype = AnalysisUseCase.ENTER_MARKET if self.analysis_type == AnalysisUseCase.ENTER_MARKET.value else AnalysisUseCase.OPEN_POSITIONS
            
            # Generate batch ID for this manual submission
            # Format: "manual_TIMESTAMP_expert_EXPERT_ID_BATCH_UUID"
            timestamp = int(time.time())
            batch_uuid = str(uuid.uuid4())[:8]  # Use first 8 chars of UUID for brevity
            expert_id = int(self.expert_instance_id)
            
            is_batch = len(selected_instruments) > 1
            batch_id = f"manual_{timestamp}_{expert_id}_{batch_uuid}"
            
            # Log the manual analysis submission
            try:
                from ...core.utils import log_manual_analysis
                symbols = [inst['name'] for inst in selected_instruments]
                log_manual_analysis(
                    expert_instance_id=expert_id,
                    symbols=symbols,
                    analysis_type=subtype,
                    is_batch=is_batch
                )
            except Exception as e:
                logger.warning(f"Failed to log manual analysis: {e}")
            
            successful_submissions = 0
            failed_submissions = 0
            duplicate_submissions = 0
            
            for instrument in selected_instruments:
                symbol = instrument['name']
                
                try:
                    success = job_manager.submit_market_analysis(
                        expert_id, 
                        symbol, 
                        subtype=subtype,
                        bypass_balance_check=True,  # Manual analysis bypasses balance check
                        bypass_transaction_check=True,  # Manual analysis bypasses transaction checks
                        batch_id=batch_id  # All symbols in this submission share the same batch ID
                    )
                    
                    if success:
                        successful_submissions += 1
                        logger.debug(f"Successfully submitted analysis for {symbol} with batch_id={batch_id}")
                    else:
                        duplicate_submissions += 1
                        logger.debug(f"Analysis already pending for {symbol}")
                        
                except Exception as e:
                    failed_submissions += 1
                    logger.error(f"Failed to submit analysis for {symbol}: {e}", exc_info=True)
            
            # Show summary notification
            if successful_submissions > 0:
                message = f"Successfully submitted {successful_submissions} analysis jobs"
                if duplicate_submissions > 0:
                    message += f" ({duplicate_submissions} already pending)"
                if failed_submissions > 0:
                    message += f" ({failed_submissions} failed)"
                ui.notify(message, type='positive')
            elif duplicate_submissions > 0:
                ui.notify(f"All {duplicate_submissions} analyses are already pending", type='warning')
            else:
                ui.notify(f"Failed to submit any analyses ({failed_submissions} failed)", type='negative')
                
        except Exception as e:
            logger.error(f"Error submitting bulk analysis: {e}", exc_info=True)
            ui.notify(f"Error submitting analyses: {str(e)}", type='negative')


class ScheduledJobsTab:
    def __init__(self):
        self.scheduled_jobs_table = None
        self.refresh_timer = None
        self.pagination_container = None  # Container for pagination controls
        # Pagination and filtering state
        self.current_page = 1
        self.page_size = 25
        self.total_pages = 1
        self.total_records = 0
        self.expert_filter = 'all'  # Filter by expert instance ID
        self.analysis_type_filter = 'all'  # Filter by analysis type (Enter Market / Open Positions)
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Scheduled Analysis Jobs').classes('text-lg font-bold')
            ui.label('View all scheduled analysis jobs for the current week').classes('text-sm text-gray-600 mb-4')
            
            # Weekly calendar view container
            self.calendar_container = ui.column().classes('w-full')
            with self.calendar_container:
                self._create_weekly_calendar()
            
            ui.separator().classes('my-4')
            
            # Filters and controls
            with ui.row().classes('w-full justify-between items-center mb-4'):
                with ui.row().classes('gap-4'):
                    # Expert instance filter
                    expert_options = self._get_expert_filter_options()
                    self.expert_select = ui.select(
                        options=expert_options,
                        value='all',
                        label='Expert Filter'
                    ).classes('w-48')
                    self.expert_select.on_value_change(self._on_expert_filter_change)
                    
                    # Analysis type filter
                    analysis_type_options = {
                        'all': 'All Types',
                        'enter_market': 'Enter Market',
                        'open_positions': 'Open Positions'
                    }
                    self.analysis_type_select = ui.select(
                        options=analysis_type_options,
                        value='all',
                        label='Analysis Type'
                    ).classes('w-48')
                    self.analysis_type_select.on_value_change(self._on_analysis_type_filter_change)
                    
                    # Page size selector
                    page_size_options = {10: '10', 25: '25', 50: '50', 100: '100'}
                    self.page_size_select = ui.select(
                        options=page_size_options,
                        value=self.page_size,
                        label='Rows per page'
                    ).classes('w-32')
                    self.page_size_select.on_value_change(self._on_page_size_change)
                    
                    # Text filter
                    self.filter_input = ui.input('Search', placeholder='Filter by symbol...').props('stack-label').classes('w-40')
                
                with ui.row().classes('gap-2'):
                    ui.button('Clear Filters', on_click=self._clear_filters, icon='clear')
                    ui.button('Refresh', on_click=self._start_async_refresh, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Scheduled jobs table container (will be populated async)
            self.scheduled_jobs_table_container = ui.column().classes('w-full')
            with self.scheduled_jobs_table_container:
                with ui.card().classes('w-full'):
                    with ui.row().classes('w-full items-center gap-2 mb-2'):
                        ui.label('Current Week Scheduled Jobs').classes('text-md font-bold')
                        ui.spinner('dots').classes('ml-auto')
                    ui.label('Loading scheduled jobs...').classes('text-sm text-gray-500')
            
            # Pagination controls container
            self.pagination_container = ui.row().classes('w-full')
            with self.pagination_container:
                self._create_pagination_controls()
        
        # Start loading table data asynchronously (not calendar - it renders immediately)
        asyncio.create_task(self._async_load_scheduled_jobs_table())
        
        # Start auto-refresh
        self.start_auto_refresh()

    async def _async_load_scheduled_jobs_table(self):
        """Load scheduled jobs table asynchronously."""
        try:
            logger.debug("[ScheduledJobs] Starting async table load")
            
            # Fetch data in background
            scheduled_data, total_records = await asyncio.to_thread(self._get_scheduled_jobs_data)
            self.total_records = total_records
            self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
            
            logger.debug(f"[ScheduledJobs] Fetched {len(scheduled_data)} scheduled jobs")
            
            # Update the container with actual table
            self.scheduled_jobs_table_container.clear()
            with self.scheduled_jobs_table_container:
                self._create_scheduled_jobs_table(scheduled_data, total_records)
            
            # Bind text filter to table
            if self.scheduled_jobs_table:
                self.filter_input.bind_value(self.scheduled_jobs_table, "filter")
            
            # Update pagination controls
            self._create_pagination_controls()
            
            logger.debug("[ScheduledJobs] Table loaded successfully")
        except Exception as e:
            logger.error(f"[ScheduledJobs] Error loading table: {e}", exc_info=True)
            self.scheduled_jobs_table_container.clear()
            with self.scheduled_jobs_table_container:
                with ui.card().classes('w-full'):
                    ui.label('Error Loading Scheduled Jobs').classes('text-md font-bold text-red-600')
                    ui.label(f'Failed to load data: {str(e)}').classes('text-sm text-gray-600')
                    ui.button('Retry', on_click=lambda: asyncio.create_task(self._async_load_scheduled_jobs_table()))

    def _start_async_refresh(self):
        """Start async refresh of table only."""
        asyncio.create_task(self._async_load_scheduled_jobs_table())

    def _create_weekly_calendar(self):
        """Create a condensed weekly timeline view showing scheduled jobs by expert."""
        from datetime import datetime, timedelta
        import json
        from ...core.types import AnalysisUseCase
        
        # Initialize hidden experts state if not exists
        if not hasattr(self, 'hidden_experts'):
            self.hidden_experts = set()
        
        # Get current week (Monday to Sunday)
        today = datetime.now()
        start_of_week = today - timedelta(days=today.weekday())  # Monday
        
        # Days of the week
        weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        
        # Get all enabled expert instances
        expert_instances = get_all_instances(ExpertInstance)
        enabled_experts = [ei for ei in expert_instances if ei.enabled]
        
        # Generate colors for experts (use hashing for consistent colors)
        def get_expert_color(expert_id):
            """Generate consistent color for expert based on ID."""
            colors = [
                '#3b82f6',  # blue
                '#ef4444',  # red
                '#10b981',  # green
                '#f59e0b',  # amber
                '#8b5cf6',  # purple
                '#ec4899',  # pink
                '#06b6d4',  # cyan
                '#f97316',  # orange
                '#14b8a6',  # teal
                '#a855f7',  # violet
                '#f43f5e',  # rose
                '#84cc16',  # lime
                '#0ea5e9',  # sky
                '#f59e0b',  # yellow
                '#d946ef',  # fuchsia
                '#22c55e',  # emerald
                '#fb923c',  # orange-400
                '#6366f1',  # indigo
                '#64748b',  # slate
                '#78716c',  # stone
            ]
            return colors[expert_id % len(colors)]
        
        def time_to_position(time_str):
            """Convert time string (HH:MM) to percentage position (0-100)."""
            try:
                hour, minute = map(int, time_str.split(':'))
                total_minutes = hour * 60 + minute
                return (total_minutes / (24 * 60)) * 100
            except:
                return 0
        
        # Collect schedule data per expert and day
        expert_schedules = {}  # {expert_id: {day_index: {enter_market: [times], open_positions: [times]}}}
        
        for expert_instance in enabled_experts:
            try:
                from ...core.utils import get_expert_instance_from_id
                expert = get_expert_instance_from_id(expert_instance.id)
                if not expert:
                    continue
                
                expert_schedules[expert_instance.id] = {
                    'name': expert_instance.alias or expert_instance.expert,
                    'color': get_expert_color(expert_instance.id),
                    'days': {}
                }
                
                # Process both schedule types
                for schedule_key, schedule_type in [
                    ('execution_schedule_enter_market', 'enter_market'),
                    ('execution_schedule_open_positions', 'open_positions')
                ]:
                    schedule_setting = expert.settings.get(schedule_key)
                    if not schedule_setting:
                        continue
                    
                    # Parse schedule
                    if isinstance(schedule_setting, str):
                        schedule_config = json.loads(schedule_setting)
                    else:
                        schedule_config = schedule_setting
                    
                    if not isinstance(schedule_config, dict):
                        continue
                    
                    days = schedule_config.get('days', {})
                    times = schedule_config.get('times', [])
                    
                    if not any(days.values()) or not times:
                        continue
                    
                    # Map days to indices
                    day_map = {
                        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                        'friday': 4, 'saturday': 5, 'sunday': 6
                    }
                    
                    for day_name, enabled in days.items():
                        if enabled and day_name in day_map:
                            day_idx = day_map[day_name]
                            if day_idx not in expert_schedules[expert_instance.id]['days']:
                                expert_schedules[expert_instance.id]['days'][day_idx] = {
                                    'enter_market': [],
                                    'open_positions': []
                                }
                            expert_schedules[expert_instance.id]['days'][day_idx][schedule_type] = times
                
            except Exception as e:
                logger.error(f"Error processing schedule for calendar view: {e}", exc_info=True)
        
        # Collect all unique time slots across all schedules
        all_times = set()
        for expert_data in expert_schedules.values():
            for day_schedule in expert_data['days'].values():
                all_times.update(day_schedule.get('enter_market', []))
                all_times.update(day_schedule.get('open_positions', []))
        
        # Sort time slots
        sorted_times = sorted(list(all_times))
        
        # Create grid layout with days as columns and times as rows
        with ui.card().classes('w-full mb-4 p-3'):
            ui.label('Weekly Schedule Overview').classes('text-md font-bold mb-3')
            
            # Create table structure
            with ui.element('div').classes('overflow-x-auto'):
                with ui.element('table').classes('w-full border-collapse'):
                    # Header row with days
                    with ui.element('thead'):
                        with ui.element('tr'):
                            # Time column header
                            with ui.element('th').classes('border border-gray-300 bg-gray-100 p-2 text-left font-bold text-sm sticky left-0 z-10'):
                                ui.label('Time').classes('text-sm')
                            
                            # Day column headers
                            for day_idx, day_name in enumerate(weekdays):
                                date = start_of_week + timedelta(days=day_idx)
                                is_today = date.date() == today.date()
                                header_class = 'border border-gray-300 bg-gray-100 p-2 text-center font-bold text-sm'
                                if is_today:
                                    header_class += ' bg-blue-100'
                                
                                with ui.element('th').classes(header_class):
                                    ui.label(day_name).classes(f'{"text-blue-600" if is_today else ""}')
                                    ui.label(date.strftime('%m/%d')).classes('text-xs text-gray-500 block')
                    
                    # Body rows with time slots
                    with ui.element('tbody'):
                        for time_str in sorted_times:
                            with ui.element('tr'):
                                # Time label
                                with ui.element('td').classes('border border-gray-300 bg-gray-50 p-2 text-sm font-medium sticky left-0 z-10'):
                                    ui.label(time_str)
                                
                                # Day cells
                                for day_idx in range(7):
                                    date = start_of_week + timedelta(days=day_idx)
                                    is_today = date.date() == today.date()
                                    cell_class = 'border border-gray-300 p-2 text-center'
                                    if is_today:
                                        cell_class += ' bg-blue-50'
                                    
                                    with ui.element('td').classes(cell_class):
                                        # Collect experts scheduled at this time on this day (filter hidden)
                                        cell_experts = []
                                        for expert_id, expert_data in expert_schedules.items():
                                            # Skip hidden experts
                                            if expert_id in self.hidden_experts:
                                                continue
                                                
                                            if day_idx in expert_data['days']:
                                                day_schedule = expert_data['days'][day_idx]
                                                schedule_types = []
                                                
                                                if time_str in day_schedule.get('enter_market', []):
                                                    schedule_types.append('EM')
                                                if time_str in day_schedule.get('open_positions', []):
                                                    schedule_types.append('OP')
                                                
                                                if schedule_types:
                                                    cell_experts.append({
                                                        'name': expert_data['name'],
                                                        'color': expert_data['color'],
                                                        'types': schedule_types
                                                    })
                                        
                                        # Display expert badges
                                        if cell_experts:
                                            with ui.row().classes('gap-1 flex-wrap justify-center'):
                                                for expert in cell_experts:
                                                    types_str = '/'.join(expert['types'])
                                                    badge_html = f'''
                                                    <div style="display: inline-block; padding: 2px 6px; border-radius: 4px; 
                                                                background-color: {expert['color']}; color: white; 
                                                                font-size: 10px; font-weight: bold; white-space: nowrap;"
                                                         title="{expert['name']} - {types_str}">
                                                        {types_str}
                                                    </div>
                                                    '''
                                                    ui.html(badge_html, sanitize=False)
            
            # Legend
            with ui.row().classes('w-full mt-3 gap-4 flex-wrap'):
                ui.label('Legend:').classes('text-xs font-bold text-gray-600')
                with ui.row().classes('gap-1 items-center'):
                    ui.label('EM = Enter Market').classes('text-xs text-gray-600')
                with ui.row().classes('gap-1 items-center'):
                    ui.label('OP = Open Positions').classes('text-xs text-gray-600')
                
                # Expert color legend (clickable to show/hide)
                ui.label('|').classes('text-xs text-gray-400 mx-1')
                ui.label('Experts (click to show/hide):').classes('text-xs font-bold text-gray-600')
                
                def toggle_expert(expert_id):
                    """Toggle expert visibility."""
                    if expert_id in self.hidden_experts:
                        self.hidden_experts.remove(expert_id)
                    else:
                        self.hidden_experts.add(expert_id)
                    # Refresh the calendar view
                    self.calendar_container.clear()
                    with self.calendar_container:
                        self._create_weekly_calendar()
                
                for expert_id, expert_data in expert_schedules.items():
                    is_hidden = expert_id in self.hidden_experts
                    with ui.row().classes('gap-1 items-center cursor-pointer hover:bg-gray-100 px-2 py-1 rounded').on('click', lambda e, eid=expert_id: toggle_expert(eid)):
                        # Color dot with opacity based on visibility
                        opacity = '0.3' if is_hidden else '1.0'
                        ui.html(f'<div style="width: 12px; height: 12px; border-radius: 50%; background-color: {expert_data["color"]}; opacity: {opacity};"></div>', sanitize=False)
                        # Expert name with strikethrough if hidden
                        text_class = 'text-xs text-gray-400 line-through' if is_hidden else 'text-xs text-gray-600'
                        ui.label(expert_data['name']).classes(text_class)

    def _get_expert_filter_options(self) -> dict:
        """Get expert instance filter options."""
        try:
            expert_instances = get_all_instances(ExpertInstance)
            options = {'all': 'All Experts'}
            
            for instance in expert_instances:
                if instance.enabled:
                    # Use alias if available, otherwise expert type
                    display_name = instance.alias or instance.expert
                    label = f"{display_name} (ID: {instance.id})"
                    options[str(instance.id)] = label
            
            return options
        except Exception as e:
            logger.error(f"Error getting expert filter options: {e}", exc_info=True)
            return {'all': 'All Experts'}
    
    def _on_expert_filter_change(self, event):
        """Handle expert filter change."""
        self.expert_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_analysis_type_filter_change(self, event):
        """Handle analysis type filter change."""
        self.analysis_type_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_page_size_change(self, event):
        """Handle page size change."""
        self.page_size = int(event.value)
        self.current_page = 1  # Reset to first page when changing page size
        self.refresh_data()
    
    def _clear_filters(self):
        """Clear all filters."""
        self.expert_filter = 'all'
        self.analysis_type_filter = 'all'
        self.current_page = 1
        if hasattr(self, 'expert_select'):
            self.expert_select.value = 'all'
        if hasattr(self, 'analysis_type_select'):
            self.analysis_type_select.value = 'all'
        if hasattr(self, 'filter_input'):
            self.filter_input.value = ''
        self.refresh_data()

    def _create_scheduled_jobs_table(self, scheduled_data=None, total_records=None):
        """Create the scheduled jobs table."""
        columns = [
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True},
            {'name': 'instance_id', 'label': 'Instance ID', 'field': 'expert_instance_id', 'sortable': True},
            {'name': 'job_type', 'label': 'Job Type', 'field': 'job_type', 'sortable': True},
            {'name': 'weekdays', 'label': 'Days', 'field': 'weekdays', 'sortable': True},
            {'name': 'times', 'label': 'Times', 'field': 'times', 'sortable': True},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'sortable': False}
        ]
        
        # Use provided data or fetch it (for backward compatibility)
        if scheduled_data is None:
            scheduled_data, _ = self._get_scheduled_jobs_data()
        
        with ui.card().classes('w-full'):
            # Header with bulk action button
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('Current Week Scheduled Jobs').classes('text-md font-bold')
                self.start_selected_btn = ui.button('Start Selected Jobs', 
                         on_click=self._start_selected_jobs, 
                         icon='play_arrow',
                         color='primary').props('outline')
                self.start_selected_btn.enabled = False  # Disabled by default
            
            self.scheduled_jobs_table = ui.table(
                columns=columns, 
                rows=scheduled_data, 
                row_key='id',
                selection='multiple'
            ).classes('w-full')
            
            # Initialize selected property (NiceGUI tables automatically track selection in .selected property)
            self.scheduled_jobs_table.selected = []
            
            # Bind the button's enabled state to the table's selection
            self.start_selected_btn.bind_enabled_from(
                self.scheduled_jobs_table, 
                'selected', 
                lambda selected: len(selected) > 0
            )
            
            # Add action buttons
            self.scheduled_jobs_table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn flat dense icon="play_arrow" 
                           color="primary" 
                           @click="$parent.$emit('run_now', props.row.expert_instance_id, props.row.symbol, props.row.subtype)"
                           :disable="props.row.expert_disabled">
                        <q-tooltip>Run Analysis Now</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Handle events
            self.scheduled_jobs_table.on('run_now', self.run_analysis_now)

    def _get_scheduled_jobs_data(self) -> tuple:
        """Get scheduled jobs data with pagination and filtering.
        Returns: (jobs_data, total_count)
        """
        try:
            from datetime import datetime, timedelta
            import json
            from ...core.types import AnalysisUseCase
            
            # Get all enabled expert instances
            expert_instances = get_all_instances(ExpertInstance)
            
            # Filter by expert instance if specified
            if self.expert_filter != 'all':
                expert_instance_id = int(self.expert_filter)
                expert_instances = [ei for ei in expert_instances if ei.id == expert_instance_id]
            
            # Group by (expert_instance_id, symbol, job_type) to create one line per combination
            jobs_by_combination = {}
            
            for expert_instance in expert_instances:
                if not expert_instance.enabled:
                    continue
                
                try:
                    from ...core.utils import get_expert_instance_from_id
                    
                    expert = get_expert_instance_from_id(expert_instance.id)
                    if not expert:
                        continue
                    
                    # Get enabled instruments for this expert
                    enabled_instruments = expert.get_enabled_instruments()
                    
                    # Process both schedule types
                    schedule_configs = [
                        ('execution_schedule_enter_market', 'Enter Market', AnalysisUseCase.ENTER_MARKET),
                        ('execution_schedule_open_positions', 'Open Positions', AnalysisUseCase.OPEN_POSITIONS)
                    ]
                    
                    for schedule_key, job_type_display, subtype in schedule_configs:
                        # Apply analysis type filter
                        if self.analysis_type_filter != 'all':
                            if self.analysis_type_filter == 'enter_market' and schedule_key != 'execution_schedule_enter_market':
                                continue
                            elif self.analysis_type_filter == 'open_positions' and schedule_key != 'execution_schedule_open_positions':
                                continue
                        
                        schedule_setting = expert.settings.get(schedule_key)
                        
                        if not schedule_setting:
                            continue
                        
                        # Parse schedule
                        if isinstance(schedule_setting, str):
                            schedule_config = json.loads(schedule_setting)
                        else:
                            schedule_config = schedule_setting
                        
                        if not isinstance(schedule_config, dict):
                            continue
                        
                        days = schedule_config.get('days', {})
                        times = schedule_config.get('times', [])
                        
                        # Skip if no schedule is defined
                        if not any(days.values()) or not times:
                            continue
                        
                        # Get enabled weekdays with short names
                        day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                        short_weekday_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
                        
                        enabled_weekdays = []
                        for i, day_name in enumerate(day_names):
                            if days.get(day_name, False):
                                enabled_weekdays.append(short_weekday_names[i])
                        
                        # For OPEN_POSITIONS schedule, create single job entry with OPEN_POSITIONS symbol
                        # (will expand to individual symbols at execution time)
                        if schedule_key == 'execution_schedule_open_positions':
                            combination_key = f"{expert_instance.id}_OPEN_POSITIONS_{schedule_key}"
                            
                            jobs_by_combination[combination_key] = {
                                'id': combination_key,
                                'symbol': 'OPEN_POSITIONS',
                                'expert_name': expert_instance.alias or expert_instance.expert,
                                'expert_instance_id': expert_instance.id,
                                'job_type': job_type_display,
                                'subtype': subtype.value,
                                'weekdays': ', '.join(enabled_weekdays) if enabled_weekdays else 'None',
                                'times': ', '.join(times) if times else 'Not specified',
                                'actions': 'actions',
                                'expert_disabled': False
                            }
                        else:
                            # For ENTER_MARKET and other schedules, create one entry per instrument
                            for symbol in enabled_instruments:
                                combination_key = f"{expert_instance.id}_{symbol}_{schedule_key}"
                                
                                jobs_by_combination[combination_key] = {
                                    'id': combination_key,
                                    'symbol': symbol,
                                    'expert_name': expert_instance.alias or expert_instance.expert,
                                    'expert_instance_id': expert_instance.id,
                                    'job_type': job_type_display,
                                    'subtype': subtype.value,
                                    'weekdays': ', '.join(enabled_weekdays) if enabled_weekdays else 'None',
                                    'times': ', '.join(times) if times else 'Not specified',
                                    'actions': 'actions',
                                    'expert_disabled': False
                                }
                
                except Exception as e:
                    logger.error(f"Error processing schedule for expert instance {expert_instance.id}: {e}", exc_info=True)
                    continue
            
            # Convert to list and sort by expert name, then symbol
            scheduled_jobs = list(jobs_by_combination.values())
            scheduled_jobs.sort(key=lambda x: (x['expert_name'], x['symbol']))
            
            # Get total count
            total_count = len(scheduled_jobs)
            
            # Apply pagination
            start_idx = (self.current_page - 1) * self.page_size
            end_idx = start_idx + self.page_size
            paginated_jobs = scheduled_jobs[start_idx:end_idx]
            
            return paginated_jobs, total_count
            
        except Exception as e:
            logger.error(f"Error getting scheduled jobs data: {e}", exc_info=True)
            return [], 0

    def _create_pagination_controls(self):
        """Create pagination controls."""
        # Clear existing controls if container exists
        if self.pagination_container is not None:
            self.pagination_container.clear()
        
        if self.total_pages <= 1:
            return
        
        # Create controls in the container
        with self.pagination_container:
            with ui.row().classes('w-full justify-center items-center mt-4 gap-2'):
                # Previous button
                prev_btn = ui.button('Previous', 
                                   on_click=lambda: self._change_page(self.current_page - 1),
                                   icon='chevron_left')
                prev_btn.props('flat')
                if self.current_page <= 1:
                    prev_btn.props('disable')
                
                # Page info
                ui.label(f'Page {self.current_page} of {self.total_pages}').classes('mx-4')
                
                # Next button  
                next_btn = ui.button('Next',
                                   on_click=lambda: self._change_page(self.current_page + 1),
                                   icon='chevron_right')
                next_btn.props('flat')
                if self.current_page >= self.total_pages:
                    next_btn.props('disable')
                
                # Page size selector
                ui.separator().props('vertical').classes('mx-4')
                page_size_options = {'10': '10 per page', '25': '25 per page', '50': '50 per page', '100': '100 per page'}
                page_size_select = ui.select(
                    options=page_size_options,
                    value=str(self.page_size),
                    label='Page Size'
                ).classes('w-32')
                page_size_select.on_value_change(self._on_page_size_change)
    
    def _change_page(self, new_page: int):
        """Change to a specific page."""
        if 1 <= new_page <= self.total_pages:
            self.current_page = new_page
            self.refresh_data()
    
    def _on_page_size_change(self, event):
        """Handle page size change."""
        try:
            new_size = int(event.value)
            self.page_size = new_size
            self.current_page = 1  # Reset to first page
            self.refresh_data()
        except ValueError:
            pass

    def run_analysis_now(self, event_data):
        """Run analysis immediately for the selected expert and symbol with proper subtype."""
        try:
            # Extract expert_instance_id, symbol, and subtype from event data
            # NiceGUI passes GenericEventArguments with args attribute
            if hasattr(event_data, 'args') and len(event_data.args) >= 3:
                expert_instance_id = int(event_data.args[0])
                symbol = str(event_data.args[1])
                subtype = str(event_data.args[2])
            elif isinstance(event_data, (list, tuple)) and len(event_data) >= 3:
                expert_instance_id = int(event_data[0])
                symbol = str(event_data[1])
                subtype = str(event_data[2])
            else:
                logger.error(f"Invalid event data for run_analysis_now: {event_data}", exc_info=True)
                ui.notify("Invalid event data - missing subtype", type='negative')
                return
            
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            
            success = job_manager.submit_market_analysis(
                expert_instance_id, 
                symbol, 
                subtype=subtype,
                bypass_balance_check=True,  # Manual analysis bypasses balance check
                bypass_transaction_check=True  # Manual analysis bypasses transaction checks
            )
            
            if success:
                ui.notify(f"Analysis started for {symbol} ({subtype}) using expert instance {expert_instance_id}", type='positive')
            else:
                ui.notify("Analysis already pending for this symbol and expert, or open positions exists", type='warning')
                
        except Exception as e:
            logger.error(f"Error running analysis now: {e}", exc_info=True)
            ui.notify(f"Error starting analysis: {str(e)}", type='negative')

    def _start_selected_jobs(self):
        """Start all selected jobs from the scheduled jobs table."""
        try:
            selected = self.scheduled_jobs_table.selected if hasattr(self, 'scheduled_jobs_table') else []
            
            logger.debug(f"Selected jobs raw data: {selected}, type: {type(selected)}")
            
            if not selected:
                ui.notify("No jobs selected", type='warning')
                return
            
            # Ensure selected is a list
            if not isinstance(selected, list):
                selected = [selected]
            
            logger.info(f"Starting {len(selected)} selected jobs")
            
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            
            # Track submission results
            successful_submissions = 0
            duplicate_submissions = 0
            failed_submissions = 0
            
            # Submit each selected job
            for job in selected:
                try:
                    logger.debug(f"Processing job: {job}, type: {type(job)}")
                    
                    # Handle both dict format (full row data) and string format (row key only)
                    if isinstance(job, dict):
                        expert_instance_id = int(job['expert_instance_id'])
                        symbol = str(job['symbol'])
                        subtype = str(job['subtype'])
                    elif isinstance(job, str):
                        # If we only got row keys, we need to look up from table rows
                        # Find the row in the table data
                        matching_row = None
                        for row in self.scheduled_jobs_table.rows:
                            if row.get('id') == job:
                                matching_row = row
                                break
                        
                        if not matching_row:
                            logger.warning(f"Could not find row data for key: {job}")
                            failed_submissions += 1
                            continue
                        
                        expert_instance_id = int(matching_row['expert_instance_id'])
                        symbol = str(matching_row['symbol'])
                        subtype = str(matching_row['subtype'])
                    else:
                        logger.warning(f"Unexpected job data type: {type(job)}")
                        failed_submissions += 1
                        continue
                    
                    success = job_manager.submit_market_analysis(
                        expert_instance_id,
                        symbol,
                        subtype=subtype,
                        bypass_balance_check=True,  # Manual analysis bypasses balance check
                        bypass_transaction_check=True  # Manual analysis bypasses transaction checks
                    )
                    
                    if success:
                        successful_submissions += 1
                        logger.debug(f"Successfully submitted analysis for {symbol} ({subtype}) with expert {expert_instance_id}")
                    else:
                        duplicate_submissions += 1
                        logger.debug(f"Analysis already pending for {symbol} ({subtype}) with expert {expert_instance_id}")
                        
                except Exception as e:
                    failed_submissions += 1
                    logger.error(f"Failed to submit analysis for job {job}: {e}", exc_info=True)
            
            # Show summary notification
            if successful_submissions > 0:
                message = f"Successfully started {successful_submissions} analysis job{'s' if successful_submissions > 1 else ''}"
                if duplicate_submissions > 0:
                    message += f" ({duplicate_submissions} already pending)"
                if failed_submissions > 0:
                    message += f" ({failed_submissions} failed)"
                ui.notify(message, type='positive')
            elif duplicate_submissions > 0:
                ui.notify(f"All {duplicate_submissions} selected job{'s' if duplicate_submissions > 1 else ''} are already pending", type='warning')
            else:
                ui.notify(f"Failed to start any jobs ({failed_submissions} failed)", type='negative')
            
            # Clear selection after starting jobs
            if self.scheduled_jobs_table:
                self.scheduled_jobs_table.selected = []
                
        except Exception as e:
            logger.error(f"Error starting selected jobs: {e}", exc_info=True)
            ui.notify(f"Error starting jobs: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the scheduled jobs data asynchronously."""
        try:
            asyncio.create_task(self._async_load_scheduled_jobs_table())
        except Exception as e:
            logger.error(f"Error starting scheduled jobs refresh: {e}", exc_info=True)
    
    def toggle_auto_refresh(self, enabled: bool):
        """Toggle auto-refresh on/off."""
        if enabled:
            self.start_auto_refresh()
        else:
            self.stop_auto_refresh()

    def start_auto_refresh(self):
        """Start auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
        
        self.refresh_timer = ui.timer(30.0, self.refresh_data)  # Refresh every 30 seconds

    def stop_auto_refresh(self):
        """Stop auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
            self.refresh_timer = None


class OrderRecommendationsTab:
    def __init__(self):
        self.selected_symbol = None
        self.recommendations_container = None
        self.summary_table = None
        self.detail_container = None
        self.refresh_timer = None
        self.expert_select = None
        self.expert_filter = 'all'  # Track selected expert filter
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Trade Recommendations').classes('text-lg font-bold')
            ui.label('Expert recommendations and generated Trades').classes('text-sm text-gray-600 mb-4')
            
            # Controls Row 1: Action Buttons
            with ui.row().classes('w-full justify-between items-center mb-2 gap-4'):
                with ui.row().classes('items-center gap-2'):
                    ui.button('Refresh', on_click=self.refresh_data).props('color=primary outline')
                    
                    ui.button(
                        'Process Recommendations', 
                        on_click=self._handle_process_recommendations,
                        icon='play_arrow'
                    ).props('color=positive').tooltip('Process all recommendations for the selected expert and create orders')
                    
                    ui.button(
                        'Run Risk Mgmt & Submit Orders',
                        on_click=self._handle_risk_management_and_submit,
                        icon='assessment'
                    ).props('color=secondary').tooltip('Run risk management on pending orders and submit them to broker')
            
            # Controls Row 2: Filters
            with ui.row().classes('w-full items-center mb-4 gap-2'):
                # Expert filter
                expert_options = self._get_expert_options()
                self.expert_select = ui.select(
                    options=expert_options,
                    label='Expert Filter',
                    value='all'  # Default to 'all'
                ).classes('w-48').props('dense outlined')
                self.expert_select.on_value_change(self._on_expert_filter_change)
                
                # Symbol search filter
                self.symbol_search = ui.input(
                    label='Symbol',
                    placeholder='Filter by symbol...'
                ).classes('w-40').props('dense outlined')
                self.symbol_search.on_value_change(lambda: self.refresh_data())
                
                # Action filter
                self.action_filter = ui.select(
                    options=['All', 'BUY', 'SELL', 'HOLD'],
                    label='Action',
                    value='All'
                ).classes('w-32').props('dense outlined')
                self.action_filter.on_value_change(lambda: self.refresh_data())
                
                # Show/Hide filter for orders
                self.order_status_filter = ui.select(
                    options=['All', 'With Orders', 'Without Orders'],
                    label='Order Status',
                    value='All'
                ).classes('w-48').props('dense outlined')
                self.order_status_filter.on_value_change(lambda: self.refresh_data())
            
            # Summary table container
            self.summary_container = ui.element('div').classes('w-full mb-4')
            
            # Detail container for selected recommendations
            self.detail_container = ui.element('div').classes('w-full')
            
            # Initial load
            self.refresh_data()
            
            # Auto-refresh every 30 seconds
            self.refresh_timer = ui.timer(30.0, self.refresh_data)

    def _on_expert_filter_change(self, event):
        """Handle expert filter change."""
        self.expert_filter = event.value
        self.refresh_data()
    
    def refresh_data(self):
        """Refresh the recommendations data."""
        try:
            self.summary_container.clear()
            
            with self.summary_container:
                # Get summary of recommendations by symbol
                recommendations_summary = self._get_recommendations_summary()
                
                if not recommendations_summary:
                    ui.label('No recommendations found').classes('text-gray-500 text-center py-8')
                    return
                
                with ui.card().classes('w-full'):
                    ui.label('📊 Recommendations Summary by Symbol').classes('text-h6 mb-4')
                    
                    # Create summary table
                    summary_columns = [
                        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                        {'name': 'total_recommendations', 'label': 'Total Recs', 'field': 'total_recommendations', 'align': 'right'},
                        {'name': 'buy_count', 'label': 'BUY', 'field': 'buy_count', 'align': 'right'},
                        {'name': 'sell_count', 'label': 'SELL', 'field': 'sell_count', 'align': 'right'},
                        {'name': 'hold_count', 'label': 'HOLD', 'field': 'hold_count', 'align': 'right'},
                        {'name': 'avg_confidence', 'label': 'Avg Confidence', 'field': 'avg_confidence', 'align': 'right'},
                        {'name': 'orders_created', 'label': 'Orders Created', 'field': 'orders_created', 'align': 'right'},
                        {'name': 'latest', 'label': 'Latest', 'field': 'latest', 'align': 'center'},
                        {'name': 'last_confidence', 'label': 'Last Confidence', 'field': 'last_confidence', 'align': 'right'},
                        {'name': 'last_recommendation', 'label': 'Last Rec', 'field': 'last_recommendation', 'align': 'center'},
                        {'name': 'last_price', 'label': 'Last Price', 'field': 'last_price', 'align': 'right'},
                        {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
                    ]
                    
                    self.summary_table = ui.table(
                        columns=summary_columns,
                        rows=recommendations_summary,
                        row_key='symbol',
                        pagination=10
                    ).classes('w-full dark-pagination')
                    
                    # Add action buttons
                    self.summary_table.add_slot('body-cell-actions', '''
                        <q-td :props="props">
                            <q-btn icon="visibility" 
                                   flat 
                                   dense 
                                   color="primary" 
                                   title="View Details"
                                   @click="$parent.$emit('view_details', props.row.symbol)" />
                            <q-btn icon="history" 
                                   flat 
                                   dense 
                                   color="purple" 
                                   title="View Analysis History"
                                   @click="$parent.$emit('view_history', props.row.symbol)" />
                            <q-btn icon="send" 
                                   flat 
                                   dense 
                                   color="green" 
                                   title="Place Order"
                                   @click="$parent.$emit('place_order', props.row.symbol)" />
                        </q-td>
                    ''')
                    
                    # Handle events
                    self.summary_table.on('view_details', self._handle_view_details)
                    self.summary_table.on('view_history', self._handle_view_history)
                    self.summary_table.on('place_order', self._handle_place_order)
            
            # Clear detail container if no symbol selected
            if not self.selected_symbol:
                self.detail_container.clear()
                
        except Exception as e:
            logger.error(f"Error refreshing trade recommendations data: {e}", exc_info=True)

    def _get_recommendations_summary(self):
        """Get summary of recommendations grouped by symbol."""
        try:
            from ...core.models import ExpertRecommendation, TradingOrder, ExpertInstance
            from ...core.types import OrderRecommendation
            from sqlmodel import func
            
            with get_db() as session:
                # Get recommendations with counts by symbol
                from sqlalchemy import case
                
                statement = (
                    select(
                        ExpertRecommendation.symbol,
                        func.count(ExpertRecommendation.id).label('total_recommendations'),
                        func.sum(case((ExpertRecommendation.recommended_action == OrderRecommendation.BUY, 1), else_=0)).label('buy_count'),
                        func.sum(case((ExpertRecommendation.recommended_action == OrderRecommendation.SELL, 1), else_=0)).label('sell_count'),
                        func.sum(case((ExpertRecommendation.recommended_action == OrderRecommendation.HOLD, 1), else_=0)).label('hold_count'),
                        func.avg(ExpertRecommendation.confidence).label('avg_confidence'),
                        func.max(ExpertRecommendation.created_at).label('latest_created_at')
                    )
                )
                
                # Apply expert filter if not 'all'
                if self.expert_filter and self.expert_filter != 'all':
                    # expert_filter is already the expert ID (string) from the select options
                    try:
                        expert_id = int(self.expert_filter)
                        statement = statement.where(ExpertRecommendation.instance_id == expert_id)
                    except (ValueError, IndexError):
                        pass  # If parsing fails, show all
                
                # Apply symbol search filter if provided
                if hasattr(self, 'symbol_search') and self.symbol_search.value:
                    search_term = self.symbol_search.value.strip().upper()
                    if search_term:
                        statement = statement.where(ExpertRecommendation.symbol.like(f'%{search_term}%'))
                
                # Apply action filter if not 'All'
                if hasattr(self, 'action_filter') and self.action_filter.value != 'All':
                    action = OrderRecommendation[self.action_filter.value]
                    statement = statement.where(ExpertRecommendation.recommended_action == action)
                
                statement = statement.group_by(ExpertRecommendation.symbol).order_by(func.max(ExpertRecommendation.created_at).desc())
                
                results = session.exec(statement).all()
                
                summary_data = []
                for result in results:
                    symbol = result.symbol
                    
                    # Get the latest recommendation for this symbol to extract last confidence, action, and price
                    latest_rec_statement = (
                        select(ExpertRecommendation)
                        .where(ExpertRecommendation.symbol == symbol)
                    )
                    
                    # Apply expert filter to latest recommendation if not 'all'
                    if self.expert_filter and self.expert_filter != 'all':
                        try:
                            expert_id = int(self.expert_filter)
                            latest_rec_statement = latest_rec_statement.where(ExpertRecommendation.instance_id == expert_id)
                        except (ValueError, IndexError):
                            pass
                    
                    latest_rec_statement = latest_rec_statement.order_by(ExpertRecommendation.created_at.desc()).limit(1)
                    latest_recommendation = session.exec(latest_rec_statement).first()
                    
                    # Count orders created for this symbol
                    orders_statement = select(func.count(TradingOrder.id)).where(
                        TradingOrder.symbol == symbol,
                        TradingOrder.transaction_id.is_not(None)
                    )
                    
                    # Apply expert filter to orders count if not 'all'
                    if self.expert_filter and self.expert_filter != 'all':
                        try:
                            expert_id = int(self.expert_filter)
                            # Join with ExpertRecommendation to filter by expert
                            orders_statement = orders_statement.join(
                                ExpertRecommendation,
                                TradingOrder.expert_recommendation_id == ExpertRecommendation.id
                            ).where(ExpertRecommendation.instance_id == expert_id)
                        except (ValueError, IndexError):
                            pass  # If parsing fails, don't filter orders
                    
                    orders_count = session.exec(orders_statement).first() or 0
                    
                    # Apply order status filter
                    if hasattr(self, 'order_status_filter') and self.order_status_filter.value != 'All':
                        if self.order_status_filter.value == 'With Orders' and orders_count == 0:
                            continue  # Skip symbols without orders
                        elif self.order_status_filter.value == 'Without Orders' and orders_count > 0:
                            continue  # Skip symbols with orders
                    
                    # Extract last recommendation details
                    last_confidence = 'N/A'
                    last_recommendation = 'N/A'
                    last_price = 'N/A'
                    
                    if latest_recommendation:
                        if latest_recommendation.confidence is not None:
                            last_confidence = f"{latest_recommendation.confidence:.1f}%"
                        
                        if latest_recommendation.recommended_action:
                            last_recommendation = latest_recommendation.recommended_action.value
                        
                        if latest_recommendation.price_at_date is not None:
                            last_price = f"${latest_recommendation.price_at_date:.2f}"
                    
                    summary_data.append({
                        'symbol': symbol,
                        'total_recommendations': result.total_recommendations,
                        'buy_count': result.buy_count or 0,
                        'sell_count': result.sell_count or 0,
                        'hold_count': result.hold_count or 0,
                        'avg_confidence': f"{result.avg_confidence:.1f}%" if result.avg_confidence else 'N/A',
                        'orders_created': orders_count,
                        'latest': result.latest_created_at.strftime('%Y-%m-%d %H:%M') if result.latest_created_at else 'N/A',
                        'last_confidence': last_confidence,
                        'last_recommendation': last_recommendation,
                        'last_price': last_price,
                        'actions': 'actions'
                    })
                
                return summary_data
                
        except Exception as e:
            logger.error(f"Error getting recommendations summary: {e}", exc_info=True)
            return []

    def _handle_view_details(self, event_data):
        """Handle view details for a symbol."""
        try:
            symbol = event_data.args if hasattr(event_data, 'args') else event_data
            self.selected_symbol = symbol
            self._load_symbol_details(symbol)
        except Exception as e:
            logger.error(f"Error viewing details for symbol: {e}", exc_info=True)

    def _handle_view_history(self, event_data):
        """Handle view market analysis history for a symbol."""
        try:
            symbol = event_data.args if hasattr(event_data, 'args') else event_data
            logger.info(f"Navigating to market analysis history for {symbol}")
            ui.navigate.to(f'/marketanalysishistory/{symbol}')
        except Exception as e:
            logger.error(f"Error navigating to market analysis history: {e}", exc_info=True)

    def _handle_place_order(self, event_data):
        """Handle place order for a symbol."""
        try:
            symbol = event_data.args if hasattr(event_data, 'args') else event_data
            self._show_place_order_dialog(symbol)
        except Exception as e:
            logger.error(f"Error showing place order dialog: {e}", exc_info=True)

    def _load_symbol_details(self, symbol):
        """Load detailed recommendations for a specific symbol."""
        try:
            self.detail_container.clear()
            
            with self.detail_container:
                with ui.card().classes('w-full'):
                    ui.label(f'📋 Detailed Recommendations for {symbol}').classes('text-h6 mb-4')
                    
                    # Get detailed recommendations for this symbol
                    recommendations = self._get_symbol_recommendations(symbol)
                    
                    if not recommendations:
                        ui.label('No recommendations found for this symbol').classes('text-gray-500')
                        return
                    
                    # Recommendations table
                    rec_columns = [
                        {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
                        {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'align': 'right'},
                        {'name': 'expected_profit', 'label': 'Expected Profit %', 'field': 'expected_profit', 'align': 'right'},
                        {'name': 'price_at_date', 'label': 'Price at Analysis', 'field': 'price_at_date', 'align': 'right'},
                        {'name': 'risk_level', 'label': 'Risk Level', 'field': 'risk_level', 'align': 'center'},
                        {'name': 'time_horizon', 'label': 'Time Horizon', 'field': 'time_horizon', 'align': 'center'},
                        {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'align': 'left'},
                        {'name': 'trader_name', 'label': 'Trader', 'field': 'trader_name', 'align': 'left'},
                        {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left'},
                        {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
                    ]
                    
                    rec_table = ui.table(
                        columns=rec_columns, 
                        rows=recommendations, 
                        row_key='id',
                        pagination={'rowsPerPage': 20, 'sortBy': 'created_at', 'descending': True}
                    ).classes('w-full')
                    
                    # Add colored action badges
                    rec_table.add_slot('body-cell-action', '''
                        <q-td :props="props">
                            <q-badge :color="props.row.action_color" :label="props.row.action" />
                        </q-td>
                    ''')
                    
                    # Add action buttons for individual recommendations
                    rec_table.add_slot('body-cell-actions', '''
                        <q-td :props="props">
                            <q-btn v-if="props.row.can_place_order" 
                                   icon="send" 
                                   flat 
                                   dense 
                                   color="green" 
                                   title="Place Order for this Recommendation"
                                   @click="$parent.$emit('place_order_rec', props.row.id)" />
                            <q-icon v-else 
                                    name="send" 
                                    size="sm" 
                                    color="grey-5" 
                                    class="q-ml-xs">
                                <q-tooltip>Order already exists for this recommendation</q-tooltip>
                            </q-icon>
                            <q-btn v-if="props.row.analysis_id" 
                                   icon="visibility" 
                                   flat 
                                   dense 
                                   color="primary" 
                                   title="View Analysis"
                                   @click="$parent.$emit('view_analysis', props.row.analysis_id)" />
                            <q-btn v-if="props.row.has_evaluation_data" 
                                   icon="search" 
                                   flat 
                                   dense 
                                   color="secondary" 
                                   title="View Rule Evaluation Details"
                                   @click="$parent.$emit('view_evaluation', props.row.id)" />
                        </q-td>
                    ''')
                    
                    rec_table.on('place_order_rec', self._handle_place_order_recommendation)
                    rec_table.on('view_analysis', self._handle_view_analysis)
                    rec_table.on('view_evaluation', self._handle_view_evaluation)
                    
        except Exception as e:
            logger.error(f"Error loading symbol details: {e}", exc_info=True)

    def _get_symbol_recommendations(self, symbol):
        """Get detailed recommendations for a specific symbol."""
        try:
            from ...core.models import ExpertRecommendation, ExpertInstance, MarketAnalysis
            
            with get_db() as session:
                statement = (
                    select(ExpertRecommendation, ExpertInstance, MarketAnalysis)
                    .join(ExpertInstance, ExpertRecommendation.instance_id == ExpertInstance.id)
                    .outerjoin(MarketAnalysis, ExpertRecommendation.market_analysis_id == MarketAnalysis.id)
                    .where(ExpertRecommendation.symbol == symbol)
                )
                
                # Apply expert filter if not 'all'
                if self.expert_filter and self.expert_filter != 'all':
                    # expert_filter is already the expert ID (string) from the select options
                    try:
                        expert_id = int(self.expert_filter)
                        statement = statement.where(ExpertRecommendation.instance_id == expert_id)
                    except (ValueError, IndexError):
                        pass  # If parsing fails, show all
                
                statement = statement.order_by(ExpertRecommendation.created_at.desc())
                
                results = session.exec(statement).all()
                
                recommendations = []
                for recommendation, expert_instance, analysis in results:
                    # Check for existing orders linked to this recommendation
                    order_statement = select(TradingOrder).where(
                        TradingOrder.expert_recommendation_id == recommendation.id
                    )
                    existing_orders = session.exec(order_statement).all()
                    
                    # Check for TradeActionResult with evaluation details
                    from ...core.models import TradeActionResult
                    action_result_statement = select(TradeActionResult).where(
                        TradeActionResult.expert_recommendation_id == recommendation.id
                    )
                    action_results = session.exec(action_result_statement).all()
                    
                    # Check if any action result has evaluation_details in data
                    has_evaluation_data = False
                    for result in action_results:
                        if result.data and 'evaluation_details' in result.data:
                            has_evaluation_data = True
                            break
                    
                    # Determine order status for this recommendation
                    has_non_pending_order = any(order.status != OrderStatus.PENDING for order in existing_orders)
                    has_pending_order = any(order.status == OrderStatus.PENDING for order in existing_orders)
                    can_place_order = not has_non_pending_order  # Can place if no non-pending orders exist
                    
                    # Always get the enum value, not the string representation
                    if hasattr(recommendation.recommended_action, 'value'):
                        action_raw = recommendation.recommended_action.value
                    else:
                        # Fallback for non-enum values - use .name to avoid "EnumName.VALUE" format
                        action_raw = recommendation.recommended_action.name if hasattr(recommendation.recommended_action, 'name') else str(recommendation.recommended_action)
                        
                    # Convert enum values to readable text
                    action_mapping = {
                        OrderRecommendation.BUY.value: 'Buy', 
                        OrderRecommendation.SELL.value: 'Sell', 
                        OrderRecommendation.HOLD.value: 'Hold',
                        OrderRecommendation.ERROR.value: 'Error'
                    }
                    action = action_mapping.get(action_raw, action_raw)
                    created_at = recommendation.created_at.strftime('%Y-%m-%d %H:%M:%S') if recommendation.created_at else ''
                    
                    # Extract trader name from analysis state if available (for SenateCopy expert)
                    trader_name = None
                    if analysis and analysis.state:
                        # Check for multi-instrument copy trade analysis
                        if 'copy_trade_multi' in analysis.state:
                            traders_by_symbol = analysis.state['copy_trade_multi'].get('traders_by_symbol', {})
                            trader_name = traders_by_symbol.get(recommendation.symbol)
                        # Check for single-symbol copy trade analysis (legacy)
                        elif 'copy_trade' in analysis.state:
                            trader_name = analysis.state['copy_trade'].get('trader_name')
                    
                    # Format trader name for display
                    trader_display = f" (Trader: {trader_name})" if trader_name else ""
                    
                    recommendations.append({
                        'id': recommendation.id,
                        'symbol': recommendation.symbol,
                        'action': action,
                        'action_color': {'Buy': 'green', 'Sell': 'red', 'Hold': 'orange', 'Error': 'red'}.get(action, 'grey'),
                        'confidence': f"{recommendation.confidence:.1f}%" if recommendation.confidence is not None else 'N/A',
                        'expected_profit': f"{recommendation.expected_profit_percent:.2f}%" if recommendation.expected_profit_percent else 'N/A',
                        'price_at_date': f"${recommendation.price_at_date:.2f}" if recommendation.price_at_date else 'N/A',
                        'risk_level': recommendation.risk_level.value.title() if hasattr(recommendation.risk_level, 'value') else (recommendation.risk_level.name.title() if hasattr(recommendation.risk_level, 'name') else str(recommendation.risk_level)),
                        'time_horizon': recommendation.time_horizon.value.replace('_', ' ').title() if hasattr(recommendation.time_horizon, 'value') else str(recommendation.time_horizon),
                        'expert_name': expert_instance.alias or expert_instance.expert,
                        'trader_name': trader_name,  # Store trader name for display
                        'created_at': created_at,
                        'analysis_id': analysis.id if analysis else None,
                        'can_place_order': can_place_order,
                        'has_pending_order': has_pending_order,
                        'existing_orders_count': len(existing_orders),
                        'has_evaluation_data': has_evaluation_data,  # NEW: Flag for showing magnifying glass icon
                        'actions': 'actions'
                    })
                
                return recommendations
                
        except Exception as e:
            logger.error(f"Error getting symbol recommendations: {e}", exc_info=True)
            return []

    def _show_place_order_dialog(self, symbol, recommendation_id=None):
        """Show place order dialog for a symbol."""
        try:
            with ui.dialog() as order_dialog:
                with ui.card().classes('w-96'):
                    ui.label(f'Place Order for {symbol}').classes('text-h6 mb-4')
                    
                    # Get current price
                    current_price = self._get_current_price(symbol)
                    if current_price:
                        ui.label(f'Current Price: ${current_price:.2f}').classes('text-subtitle1 mb-4')
                    else:
                        ui.label('Current Price: N/A').classes('text-subtitle1 mb-4')
                    
                    # Order form
                    side_select = ui.select([OrderDirection.BUY.value, OrderDirection.SELL.value], value=OrderDirection.BUY.value, label='Side').classes('w-full mb-2')
                    quantity_input = ui.number('Quantity', value=1, min=0.01, step=0.01).classes('w-full mb-2')
                    order_type_select = ui.select(['market', 'limit'], value='market', label='Order Type').classes('w-full mb-2')
                    
                    # Limit price input (conditional)
                    limit_price_input = ui.number('Limit Price', value=current_price if current_price else 0, min=0.01, step=0.01).classes('w-full mb-4')
                    limit_price_input.visible = False
                    
                    def on_order_type_change():
                        limit_price_input.visible = order_type_select.value == 'limit'
                    
                    order_type_select.on_value_change(on_order_type_change)
                    
                    with ui.row().classes('w-full justify-end gap-2'):
                        ui.button('Cancel', on_click=order_dialog.close).props('flat')
                        ui.button('Place Order', on_click=lambda: self._place_order(
                            symbol=symbol,
                            side=side_select.value,
                            quantity=quantity_input.value,
                            order_type=order_type_select.value,
                            limit_price=limit_price_input.value if order_type_select.value == 'limit' else None,
                            dialog=order_dialog,
                            recommendation_id=recommendation_id
                        )).props('color=primary')
            
            order_dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing place order dialog: {e}", exc_info=True)

    def _handle_place_order_recommendation(self, event_data):
        """Handle place order for a specific recommendation."""
        try:
            recommendation_id = event_data.args if hasattr(event_data, 'args') else event_data
            
            # Check for existing orders linked to this recommendation
            with get_db() as session:
                order_statement = select(TradingOrder).where(
                    TradingOrder.expert_recommendation_id == recommendation_id
                )
                existing_orders = session.exec(order_statement).all()
                
                # Check if there's a non-pending order
                has_non_pending_order = any(order.status != OrderStatus.PENDING for order in existing_orders)
                if has_non_pending_order:
                    ui.notify('Order already exists for this recommendation and is no longer pending', type='warning')
                    return
                
                # Check if there's a pending order
                pending_order = next((order for order in existing_orders if order.status == OrderStatus.PENDING), None)
                if pending_order:
                    # Submit the existing pending order
                    try:
                        account = get_instance(AccountDefinition, pending_order.account_id)
                        if not account:
                            ui.notify('Account not found for existing order', type='negative')
                            return
                        
                        # Submit the order through the account provider
                        provider_obj = get_account_instance_from_id(account.id)
                        if not provider_obj:
                            ui.notify(f'Failed to get account instance for {account.name}', type='negative')
                            return
                        
                        submitted_order = provider_obj.submit_order(pending_order)
                        
                        if submitted_order:
                            ui.notify(f'Existing order {pending_order.id} submitted successfully to {account.provider}', type='positive')
                            self.refresh_data()
                        else:
                            ui.notify(f'Failed to submit existing order {pending_order.id} to broker', type='negative')
                    except Exception as e:
                        logger.error(f"Error submitting existing order {pending_order.id}: {e}", exc_info=True)
                        ui.notify(f'Error submitting existing order: {str(e)}', type='negative')
                    return
            
            # No existing orders, show dialog to create new one
            recommendation = self._get_recommendation_details(recommendation_id)
            if recommendation:
                self._show_place_order_dialog(recommendation['symbol'], recommendation_id)
        except Exception as e:
            logger.error(f"Error placing order for recommendation: {e}", exc_info=True)

    def _handle_view_analysis(self, event_data):
        """Handle view analysis click."""
        try:
            analysis_id = event_data.args if hasattr(event_data, 'args') else event_data
            if analysis_id:
                if not hasattr(self, '_analysis_dialog'):
                    self._analysis_dialog = MarketAnalysisDetailDialog()
                self._analysis_dialog.open(analysis_id)
        except Exception as e:
            logger.error(f"Error navigating to analysis detail: {e}", exc_info=True)

    def _handle_view_evaluation(self, event_data):
        """Handle view rule evaluation details click."""
        try:
            recommendation_id = event_data.args if hasattr(event_data, 'args') else event_data
            if not recommendation_id:
                return
            
            # Load TradeActionResult with evaluation details
            from ...core.models import TradeActionResult
            from ..components.RuleEvaluationDisplay import render_rule_evaluations
            
            with get_db() as session:
                # Get action results for this recommendation
                statement = select(TradeActionResult).where(
                    TradeActionResult.expert_recommendation_id == recommendation_id
                )
                action_results = session.exec(statement).all()
                
                # Find the first result with evaluation_details
                evaluation_data = None
                for result in action_results:
                    if result.data and 'evaluation_details' in result.data:
                        evaluation_data = result.data['evaluation_details']
                        break
                
                if not evaluation_data:
                    ui.notify('No evaluation details found', type='warning')
                    return
                
                # Show dialog with evaluation details
                with ui.dialog() as eval_dialog, ui.card().classes('w-full max-w-4xl'):
                    ui.label('🔍 Rule Evaluation Details').classes('text-h6 mb-4')
                    
                    # Use the reusable component to display evaluation details
                    render_rule_evaluations(evaluation_data, show_actions=True, compact=False)
                    
                    with ui.row().classes('w-full justify-end mt-4'):
                        ui.button('Close', on_click=eval_dialog.close).props('outline')
                
                eval_dialog.open()
                
        except Exception as e:
            logger.error(f"Error viewing evaluation details: {e}", exc_info=True)
            ui.notify(f'Error loading evaluation details: {str(e)}', type='negative')

    def _get_current_price(self, symbol):
        """Get current price for a symbol."""
        try:
            # This is a placeholder - in production you'd get this from market data
            # For now, we'll try to get it from recent recommendations
            from ...core.models import ExpertRecommendation
            
            with get_db() as session:
                statement = (
                    select(ExpertRecommendation.price_at_date)
                    .where(ExpertRecommendation.symbol == symbol)
                    .order_by(ExpertRecommendation.created_at.desc())
                    .limit(1)
                )
                
                result = session.exec(statement).first()
                return result if result else None
                
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None

    def _get_recommendation_details(self, recommendation_id):
        """Get details for a specific recommendation."""
        try:
            from ...core.models import ExpertRecommendation
            
            with get_db() as session:
                recommendation = session.get(ExpertRecommendation, recommendation_id)
                if recommendation:
                    # Always get the enum value, not the string representation
                    if hasattr(recommendation.recommended_action, 'value'):
                        action_raw = recommendation.recommended_action.value
                    else:
                        # Fallback for non-enum values - use .name to avoid "EnumName.VALUE" format
                        action_raw = recommendation.recommended_action.name if hasattr(recommendation.recommended_action, 'name') else str(recommendation.recommended_action)
                        
                    # Convert enum values to readable text
                    action_mapping = {
                        OrderRecommendation.BUY.value: 'Buy', 
                        OrderRecommendation.SELL.value: 'Sell', 
                        OrderRecommendation.HOLD.value: 'Hold',
                        OrderRecommendation.ERROR.value: 'Error'
                    }
                    action = action_mapping.get(action_raw, action_raw)
                    return {
                        'id': recommendation.id,
                        'symbol': recommendation.symbol,
                        'action': action
                    }
                return None
                
        except Exception as e:
            logger.error(f"Error getting recommendation details: {e}", exc_info=True)
            return None

    def _place_order(self, symbol, side, quantity, order_type, limit_price, dialog, recommendation_id=None):
        """Place an order."""
        try:
            # Get the first available account for order submission
            accounts = get_all_instances(AccountDefinition)
            if not accounts:
                ui.notify('No trading accounts configured', type='negative')
                return
                
            account = accounts[0]  # Use first available account
            
            # Create order object with account_id
            # Convert side to uppercase to match OrderDirection enum
            side_upper = side.upper() if isinstance(side, str) else side
            order = TradingOrder(
                account_id=account.id,
                symbol=symbol,
                quantity=quantity,
                side=side_upper,
                order_type=order_type,
                status=OrderStatus.PENDING,
                limit_price=limit_price,
                open_type=OrderOpenType.MANUAL,
                expert_recommendation_id=recommendation_id,
                comment=f"Manual order from Trade Recommendations - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # Add to database first
            order_id = add_instance(order)
            
            if order_id:
                # Get the order back from database to get the complete object
                from ...core.db import get_instance
                order = get_instance(TradingOrder, order_id)
                
                # Submit the order through the account provider
                try:
                    provider_obj = get_account_instance_from_id(account.id)
                    if provider_obj:
                        submitted_order = provider_obj.submit_order(order)
                        if submitted_order:
                            ui.notify(f'Order {order_id} submitted successfully to {account.provider}', type='positive')
                        else:
                            ui.notify(f'Order {order_id} created but failed to submit to broker', type='warning')
                    else:
                        ui.notify(f'Order {order_id} created but failed to get account instance', type='warning')
                except Exception as e:
                    logger.error(f"Error submitting order {order_id}: {e}", exc_info=True)
                    ui.notify(f'Order {order_id} created but submission failed: {str(e)}', type='warning')
            else:
                ui.notify('Failed to create order', type='negative')
                return
            
            if order_id:
                ui.notify(f'Order {order_id} placed successfully for {symbol}', type='positive')
                dialog.close()
                self.refresh_data()  # Refresh the data
            else:
                ui.notify('Failed to place order', type='negative')
                
        except Exception as e:
            logger.error(f"Error placing order: {e}", exc_info=True)
            ui.notify(f'Error placing order: {str(e)}', type='negative')

    def _get_expert_options(self):
        """Get list of enabled expert instances for dropdown."""
        try:
            from ...core.models import ExpertInstance
            from ...core.db import get_all_instances
            
            experts = get_all_instances(ExpertInstance)
            enabled_experts = [e for e in experts if e.enabled]

            # Build mapping for select: key -> label
            options = {'all': 'All Experts'}

            # Create options: key is expert id string, value is display name
            for expert in enabled_experts:
                base_name = expert.alias or expert.expert
                display_name = f"{base_name} (ID: {expert.id})"
                options[str(expert.id)] = display_name

            return options
            
        except Exception as e:
            logger.error(f"Error getting expert options: {e}", exc_info=True)
            return ['all']

    def _handle_process_recommendations(self):
        """Process all recommendations for the selected expert."""
        try:
            if not self.expert_select or not self.expert_select.value:
                ui.notify('Please select an expert instance', type='warning')
                return
            
            # Check if "All Experts" is selected
            selected_value = self.expert_select.value
            if selected_value == 'all':
                ui.notify(
                    'Please select a specific expert instance. Recommendation processing cannot run for "All Experts".',
                    type='warning',
                    timeout=5000
                )
                return
            
            # Extract expert instance ID from the selected value
            # Format is "alias/expertType-ID" (e.g., "taQuickGrok-9")
            selected_text = selected_value
            # Get the ID by splitting on the last dash and taking the last part
            expert_id = int(selected_text.split('-')[-1])
            
            # Show dialog to ask for days lookback period
            with ui.dialog() as config_dialog, ui.card().classes('p-4'):
                ui.label('Process Recommendations').classes('text-h6 mb-4')
                ui.label('How many days back should we process recommendations?').classes('mb-2')
                
                days_input = ui.number(
                    label='Days',
                    value=1,
                    min=1,
                    max=30,
                    step=1,
                    format='%.0f'
                ).classes('w-full')
                
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=config_dialog.close).props('flat')
                    ui.button('Process', on_click=lambda: self._execute_process_recommendations(expert_id, int(days_input.value), config_dialog)).props('color=primary')
            
            config_dialog.open()
                
        except Exception as e:
            logger.error(f"Error handling process recommendations: {e}", exc_info=True)
            ui.notify('Error processing recommendations', type='negative')
    
    async def _execute_process_recommendations(self, expert_id: int, days: int, config_dialog):
        """Execute the recommendation processing with the specified days lookback (async to avoid UI blocking)."""
        try:
            config_dialog.close()
            
            # Get the Trade Manager
            from ...core.TradeManager import get_trade_manager
            trade_manager = get_trade_manager()
            
            # Process recommendations
            with ui.dialog() as processing_dialog, ui.card():
                ui.label('Processing Recommendations...').classes('text-h6')
                ui.spinner(size='lg')
                ui.label(f'Processing recommendations from the last {days} day(s)').classes('text-sm text-gray-600')
                ui.label('This may take a few moments...').classes('text-xs text-gray-500 mt-2')
            
            processing_dialog.open()
            
            try:
                # Run the processing in a separate thread to avoid blocking the UI
                import asyncio
                from concurrent.futures import ThreadPoolExecutor
                
                # Function to run in thread
                def process_recommendations():
                    return trade_manager.process_expert_recommendations_after_analysis(expert_id, lookback_days=days)
                
                # Run in thread pool to avoid blocking UI
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as executor:
                    created_orders = await loop.run_in_executor(executor, process_recommendations)
                
                processing_dialog.close()
                
                if created_orders:
                    order_ids = [order.id for order in created_orders]
                    ui.notify(
                        f'Successfully processed recommendations for expert {expert_id}. '
                        f'Created {len(created_orders)} order(s): {", ".join(map(str, order_ids))}',
                        type='positive',
                        close_button=True,
                        timeout=5000
                    )
                else:
                    ui.notify(
                        f'No orders created for expert {expert_id}. '
                        f'Either no recommendations passed the ruleset filters or automated trading is disabled.',
                        type='info',
                        close_button=True,
                        timeout=5000
                    )
                
                # Refresh data to show new orders
                self.refresh_data()
            
            except Exception as e:
                processing_dialog.close()
                logger.error(f"Error processing recommendations for expert {expert_id}: {e}", exc_info=True)
                ui.notify(f'Error processing recommendations: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error in execute_process_recommendations: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')

    def _handle_risk_management_and_submit(self):
        """Run risk management on pending orders and submit them to broker."""
        try:
            if not self.expert_select or not self.expert_select.value:
                ui.notify('Please select an expert instance', type='warning')
                return
            
            # Check if "All Experts" is selected
            selected_value = self.expert_select.value
            if selected_value == 'all':
                ui.notify(
                    'Please select a specific expert instance. Risk management cannot run for "All Experts".',
                    type='warning',
                    timeout=5000
                )
                return
            
            # Extract expert instance ID from the selected value
            # Format is "alias/expertType-ID" (e.g., "taQuickGrok-9")
            selected_text = selected_value
            # Get the ID by splitting on the last dash and taking the last part
            expert_id = int(selected_text.split('-')[-1])
            
            # Load expert instance and check risk_manager_mode setting
            from ...core.utils import get_expert_instance_from_id
            expert_instance = get_expert_instance_from_id(expert_id)
            
            if not expert_instance:
                ui.notify(f'Expert instance {expert_id} not found', type='negative')
                return
            
            # Check risk_manager_mode setting (default to "classic" if not set)
            from ...core.utils import get_risk_manager_mode
            risk_manager_mode = get_risk_manager_mode(expert_instance.settings)
            
            if risk_manager_mode == "smart":
                # Run Smart Risk Manager (AI-powered agentic workflow)
                self._run_smart_risk_manager(expert_id, expert_instance)
            else:
                # Run classic rule-based risk management
                self._run_classic_risk_management(expert_id)
                
        except Exception as e:
            logger.error(f"Error in _handle_risk_management_and_submit: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _run_smart_risk_manager(self, expert_id: int, expert_instance):
        """Enqueue AI-powered Smart Risk Manager workflow to SmartRiskManagerQueue."""
        try:
            # Get expert instance record from database to access account_id
            from ...core.db import get_instance
            from ...core.models import ExpertInstance
            
            expert_record = get_instance(ExpertInstance, expert_id)
            if not expert_record:
                logger.error(f"Expert instance {expert_id} not found in database")
                ui.notify(f'Error: Expert instance not found', type='negative')
                return
            
            account_id = expert_record.account_id
            
            # Enqueue Smart Risk Manager job to dedicated queue
            from ...core.SmartRiskManagerQueue import get_smart_risk_manager_queue
            smart_queue = get_smart_risk_manager_queue()
            
            task_id = smart_queue.submit_task(expert_id, account_id)
            
            if task_id:
                # Show success notification with link to monitoring page
                ui.notify(
                    f'Smart Risk Manager job enqueued (Task ID: {task_id}). Check Job Monitoring tab for progress.',
                    type='positive',
                    timeout=5000,
                    close_button=True
                )
                
                # Switch to Job Monitoring tab
                # Note: This assumes we're in the market analysis page with tabs
                # The tab switching will be handled by the user manually
            else:
                ui.notify(
                    'Smart Risk Manager job already running for this expert',
                    type='warning',
                    timeout=5000
                )
                
        except Exception as e:
            logger.error(f"Error enqueueing Smart Risk Manager for expert {expert_id}: {e}", exc_info=True)
            ui.notify(f'Error enqueueing Smart Risk Manager: {str(e)}', type='negative', timeout=5000)
    
    def _run_classic_risk_management(self, expert_id: int):
        """Run classic rule-based risk management."""
        try:
            # Get the Risk Management system
            from ...core.TradeRiskManagement import get_risk_management
            
            # Show processing dialog
            with ui.dialog() as processing_dialog, ui.card():
                ui.label('Running Risk Management...').classes('text-h6')
                ui.spinner(size='lg')
                ui.label('Calculating order quantities and priorities').classes('text-sm text-gray-600')
            
            processing_dialog.open()
            
            try:
                # Step 1: Run risk management
                risk_management = get_risk_management()
                updated_orders = risk_management.review_and_prioritize_pending_orders(expert_id)
                
                processing_dialog.close()
                
                if not updated_orders:
                    ui.notify(
                        f'No pending orders found for expert {expert_id} or no orders needed quantity updates.',
                        type='info',
                        timeout=5000
                    )
                    return
                
                # Step 2: Show order review dialog
                self._show_order_review_dialog(expert_id, updated_orders)
                
            except Exception as e:
                processing_dialog.close()
                logger.error(f"Error in classic risk management for expert {expert_id}: {e}", exc_info=True)
                ui.notify(f'Error in risk management: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error in _run_classic_risk_management: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error in _handle_risk_management_and_submit: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')

    def _show_order_review_dialog(self, expert_id: int, orders: List):
        """Show dialog to review calculated orders before submission."""
        try:
            from ...core.models import TradingOrder, ExpertInstance
            from ...core.db import get_instance
            from ...core.utils import get_account_instance_from_id
            
            # Get account instance for price lookups
            try:
                expert_instance = get_instance(ExpertInstance, expert_id)
            except Exception as e:
                logger.error(f"Expert instance {expert_id} not found: {e}")
                ui.notify(f'Error: Expert instance {expert_id} not found', type='negative')
                return
            
            if not expert_instance:
                ui.notify(f'Error: Expert instance {expert_id} not found', type='negative')
                return
            
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                ui.notify(f'Error: Account {expert_instance.account_id} not found', type='negative')
                return
            
            # Filter orders with quantity > 0
            orders_to_submit = [order for order in orders if order.quantity > 0]
            orders_zero_quantity = [order for order in orders if order.quantity == 0]
            
            # Track selected orders
            selected_orders = []
            
            with ui.dialog() as review_dialog:
                with ui.card().classes('w-full max-w-6xl'):
                    ui.label(f'📋 Order Review - Expert {expert_id}').classes('text-h5 mb-4')
                    
                    if orders_to_submit:
                        ui.label(f'✅ Orders Ready for Submission ({len(orders_to_submit)})').classes('text-h6 text-green-600 mb-2')
                        ui.label('Select orders to submit:').classes('text-caption text-gray-600 mb-2')
                        
                        # Create table for orders to submit with selection
                        submit_columns = [
                            {'name': 'order_id', 'label': 'ID', 'field': 'order_id', 'align': 'left'},
                            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                            {'name': 'side', 'label': 'Side', 'field': 'side', 'align': 'center'},
                            {'name': 'quantity', 'label': 'Quantity', 'field': 'quantity', 'align': 'right'},
                            {'name': 'estimated_value', 'label': 'Est. Value', 'field': 'estimated_value', 'align': 'right'},
                            {'name': 'roi', 'label': 'Expected ROI', 'field': 'roi', 'align': 'right'},
                            {'name': 'risk_level', 'label': 'Risk', 'field': 'risk_level', 'align': 'center'},
                            {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'}
                        ]
                        
                        submit_data = []
                        order_map = {}  # Map row keys to order objects
                        total_estimated_value = 0
                        
                        # Fetch all prices at once (bulk fetching)
                        all_symbols = list(set(order.symbol for order in orders_to_submit))
                        logger.debug(f"Fetching prices for {len(all_symbols)} symbols in bulk for order submission table")
                        symbol_prices = account.get_instrument_current_price(all_symbols)
                        logger.info(f"Bulk fetched {len(symbol_prices)} prices for market analysis page")
                        
                        for order in orders_to_submit:
                            # Get recommendation data
                            recommendation = None
                            roi = 'N/A'
                            risk_level = 'N/A'
                            current_price = None
                            
                            if order.expert_recommendation_id:
                                from ...core.models import ExpertRecommendation
                                recommendation = get_instance(ExpertRecommendation, order.expert_recommendation_id)
                                if recommendation:
                                    roi = f"{recommendation.expected_profit_percent:.2f}%" if recommendation.expected_profit_percent else 'N/A'
                                    risk_level = recommendation.risk_level.value.title() if hasattr(recommendation.risk_level, 'value') else (recommendation.risk_level.name.title() if hasattr(recommendation.risk_level, 'name') else str(recommendation.risk_level))
                            
                            # Get current price from bulk-fetched prices
                            current_price = symbol_prices.get(order.symbol) if symbol_prices else None
                            if current_price is None:
                                logger.warning(f"Could not get current price for {order.symbol}, using price_at_date from recommendation")
                                # Fallback to historical price from recommendation if available
                                if recommendation and recommendation.price_at_date:
                                    current_price = recommendation.price_at_date
                                else:
                                    logger.error(f"No price available for {order.symbol}, cannot calculate estimated value")
                                    continue  # Skip this order in the display
                            
                            estimated_value = order.quantity * current_price
                            total_estimated_value += estimated_value
                            
                            row_key = f"order_{order.id}"
                            order_map[row_key] = order
                            
                            submit_data.append({
                                'order_id': order.id,
                                'symbol': order.symbol,
                                'side': order.side.value if hasattr(order.side, 'value') else str(order.side),
                                'quantity': order.quantity,
                                'estimated_value': f"${estimated_value:.2f}",
                                'roi': roi,
                                'risk_level': risk_level,
                                'comment': order.comment or ''
                            })
                        
                        # Table with selection (multiple selection enabled)
                        orders_table = ui.table(
                            columns=submit_columns,
                            rows=submit_data,
                            row_key='order_id',
                            selection='multiple'
                        ).classes('w-full mb-4')
                        
                        # Select all by default
                        orders_table.selected = [row['order_id'] for row in submit_data]
                        
                        # Summary
                        summary_label = ui.label(f'📊 Total Estimated Value: ${total_estimated_value:.2f}').classes('text-subtitle1 font-bold mb-4')
                    
                    if orders_zero_quantity:
                        ui.label(f'❌ Orders with Zero Quantity ({len(orders_zero_quantity)})').classes('text-h6 text-orange-600 mb-2')
                        
                        zero_columns = [
                            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                            {'name': 'side', 'label': 'Side', 'field': 'side', 'align': 'center'},
                            {'name': 'reason', 'label': 'Reason', 'field': 'reason', 'align': 'left'}
                        ]
                        
                        zero_data = []
                        for order in orders_zero_quantity:
                            zero_data.append({
                                'symbol': order.symbol,
                                'side': order.side.value if hasattr(order.side, 'value') else str(order.side),
                                'reason': 'Insufficient funds or position limits exceeded'
                            })
                        
                        ui.table(
                            columns=zero_columns,
                            rows=zero_data,
                            row_key='symbol'
                        ).classes('w-full mb-4')
                    
                    # Action buttons
                    with ui.row().classes('w-full justify-end gap-2 mt-4'):
                        ui.button('Cancel', on_click=review_dialog.close).props('flat color=grey')
                        
                        if orders_to_submit:
                            def submit_selected():
                                # Get selected order IDs from table
                                selected_ids = orders_table.selected if hasattr(orders_table, 'selected') else []
                                if not selected_ids:
                                    ui.notify('Please select at least one order to submit', type='warning')
                                    return
                                
                                # Get the actual order objects for selected IDs
                                selected_order_objects = [order_map[f"order_{order_id}"] for order_id in selected_ids if f"order_{order_id}" in order_map]
                                
                                if not selected_order_objects:
                                    ui.notify('No valid orders selected', type='warning')
                                    return
                                
                                self._submit_reviewed_orders(expert_id, selected_order_objects, review_dialog)
                            
                            ui.button(
                                f'Submit Selected Orders',
                                on_click=submit_selected
                            ).props('color=primary')
                        else:
                            ui.label('No orders to submit').classes('text-grey-5')
            
            review_dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing order review dialog: {e}", exc_info=True)
            ui.notify(f'Error showing order review: {str(e)}', type='negative')

    def _submit_reviewed_orders(self, expert_id: int, orders: List, dialog):
        """Submit the reviewed orders to broker."""
        try:
            from ...core.TradeManager import get_trade_manager
            from ...core.models import ExpertInstance
            from ...core.db import get_instance
            
            # Get the expert instance
            try:
                expert_instance = get_instance(ExpertInstance, expert_id)
            except Exception as e:
                logger.error(f"Expert instance {expert_id} not found: {e}")
                ui.notify(f'Expert instance {expert_id} not found', type='negative')
                return
            
            if not expert_instance:
                ui.notify(f'Expert instance {expert_id} not found', type='negative')
                return
            
            # Submit orders with progress tracking
            trade_manager = get_trade_manager()
            submitted_count = 0
            failed_count = 0
            
            with ui.dialog() as submit_dialog, ui.card():
                ui.label('Submitting Orders...').classes('text-h6')
                progress = ui.linear_progress(value=0).classes('w-full')
                status_label = ui.label('Starting submission...').classes('text-sm text-gray-600')
            
            submit_dialog.open()
            
            for i, order in enumerate(orders):
                try:
                    # Update progress
                    progress.value = i / len(orders)
                    status_label.text = f'Submitting order {i+1}/{len(orders)}: {order.symbol} ({order.side.value})'
                    
                    # Submit the order
                    submitted_order = trade_manager._place_order(order, expert_instance)
                    if submitted_order:
                        submitted_count += 1
                        logger.info(f"Successfully submitted order {order.id} for {order.symbol}")
                    else:
                        failed_count += 1
                        logger.error(f"Failed to submit order {order.id} for {order.symbol}")
                        
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error submitting order {order.id}: {e}", exc_info=True)
            
            # Complete progress
            progress.value = 1.0
            status_label.text = f'Submission complete: {submitted_count} successful, {failed_count} failed'
            
            # Close dialogs after short delay
            ui.timer(2.0, lambda: [submit_dialog.close(), dialog.close()])
            
            # Show final notification
            if submitted_count > 0:
                result_message = f'Successfully submitted {submitted_count} orders for expert {expert_id}'
                if failed_count > 0:
                    result_message += f' ({failed_count} failed)'
                    message_type = 'warning'
                else:
                    message_type = 'positive'
                
                ui.notify(
                    result_message,
                    type=message_type,
                    close_button=True,
                    timeout=5000
                )
            else:
                ui.notify(
                    f'No orders were successfully submitted for expert {expert_id}',
                    type='negative',
                    timeout=5000
                )
            
            # Refresh data to show updated orders
            self.refresh_data()
            
        except Exception as e:
            logger.error(f"Error submitting reviewed orders: {e}", exc_info=True)
            ui.notify(f'Error submitting orders: {str(e)}', type='negative')

    def stop_auto_refresh(self):
        """Stop auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
            self.refresh_timer = None


def content() -> None:
    # Tab configuration: (tab_name, tab_label)
    tab_config = [
        ('monitoring', 'Job Monitoring'),
        ('manual', 'Manual Analysis'),
        ('scheduled', 'Scheduled Jobs'),
        ('recommendations', 'Trade Recommendations')
    ]
    
    with ui.tabs() as tabs:
        tab_objects = {}
        for tab_name, tab_label in tab_config:
            tab_objects[tab_name] = ui.tab(tab_name, label=tab_label)

    with ui.tab_panels(tabs, value=tab_objects['monitoring']).classes('w-full'):
        with ui.tab_panel(tab_objects['monitoring']):
            JobMonitoringTab()
        with ui.tab_panel(tab_objects['manual']):
            ManualAnalysisTab()
        with ui.tab_panel(tab_objects['scheduled']):
            ScheduledJobsTab()
        with ui.tab_panel(tab_objects['recommendations']):
            OrderRecommendationsTab()
    
    # Setup HTML5 history navigation for tabs (NiceGUI 3.0 compatible)
    async def setup_tab_navigation():
        # In NiceGUI 3.0, ui.run_javascript automatically waits for client.connected()
        # So we use await to properly handle the async nature
        from nicegui import context
        await context.client.connected()
        await ui.run_javascript('''
            (function() {
                let isPopstateNavigation = false;
                
                // Map display labels to tab names
                const labelToName = {
                    'Job Monitoring': 'monitoring',
                    'Manual Analysis': 'manual',
                    'Scheduled Jobs': 'scheduled',
                    'Trade Recommendations': 'recommendations'
                };
                
                // Get tab name from tab element
                function getTabName(tab) {
                    const label = tab.textContent.trim();
                    return labelToName[label] || label.toLowerCase().replace(/\s+/g, '-');
                }
                
                // Handle browser back/forward buttons
                window.addEventListener('popstate', (e) => {
                    isPopstateNavigation = true;
                    const hash = window.location.hash.substring(1) || 'monitoring';
                    
                    // Find and click the correct tab
                    const tabs = document.querySelectorAll('.q-tab');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        if (tabName === hash) {
                            tab.click();
                        }
                    });
                    
                    setTimeout(() => { isPopstateNavigation = false; }, 100);
                });
                
                // Setup click handlers for tabs to update URL
                function setupTabClickHandlers() {
                    const tabs = document.querySelectorAll('.q-tab');
                    console.log('Found', tabs.length, 'tabs');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        console.log('Setting up listener for tab:', tabName, '(label:', tab.textContent.trim() + ')');
                        tab.addEventListener('click', () => {
                            if (!isPopstateNavigation) {
                                console.log('Tab clicked:', tabName);
                                history.pushState({tab: tabName}, '', '#' + tabName);
                            }
                        });
                    });
                }
                
                // Handle initial page load with hash
                const hash = window.location.hash.substring(1);
                if (hash && hash !== 'monitoring') {
                    // Wait a bit for tabs to be fully rendered
                    setTimeout(() => {
                        const tabs = document.querySelectorAll('.q-tab');
                        tabs.forEach(tab => {
                            const tabName = getTabName(tab);
                            if (tabName === hash) {
                                console.log('Initial load: activating tab for hash:', hash);
                                tab.click();
                            }
                        });
                    }, 50);
                } else if (!hash) {
                    // Set initial hash if none exists
                    history.replaceState({tab: 'monitoring'}, '', '#monitoring');
                }
                
                setupTabClickHandlers();
            })();
        ''', timeout=3.0)
    
    # Use timer to run async setup (shorter delay since we explicitly wait for connection)
    ui.timer(0.1, setup_tab_navigation, once=True)
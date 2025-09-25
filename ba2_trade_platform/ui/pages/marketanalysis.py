from nicegui import ui
from datetime import datetime, timezone
from typing import List, Optional
import math

from ...core.WorkerQueue import get_worker_queue
from ...core.db import get_all_instances, get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput
from ...core.types import MarketAnalysisStatus, WorkerTaskStatus
from ...logger import logger
from sqlmodel import select, func, Session


class JobMonitoringTab:
    def __init__(self):
        self.worker_queue = None  # Lazy initialization
        self.analysis_table = None
        self.refresh_timer = None
        # Pagination and filtering state
        self.current_page = 1
        self.page_size = 25
        self.total_pages = 1
        self.total_records = 0
        self.status_filter = 'all'
        self.symbol_filter = ''
        self.render()
    
    def _get_worker_queue(self):
        """Lazy initialization of worker queue."""
        if self.worker_queue is None:
            self.worker_queue = get_worker_queue()
        return self.worker_queue

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
                    
                    # Symbol filter
                    self.symbol_input = ui.input(
                        'Symbol Filter',
                        placeholder='e.g., AAPL, MSFT'
                    ).classes('w-40')
                    self.symbol_input.on_value_change(self._on_symbol_filter_change)
                
                with ui.row().classes('gap-2'):
                    ui.button('Clear Filters', on_click=self._clear_filters, icon='clear')
                    ui.button('Refresh', on_click=self.refresh_data, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Analysis jobs table
            self._create_analysis_table()
            
            # Pagination controls
            self._create_pagination_controls()
            
            ui.separator().classes('my-4')
            
            # Worker queue status
            with ui.card().classes('w-full'):
                ui.label('Worker Queue Status').classes('text-md font-bold')
                self._create_queue_status()
        
        # Start auto-refresh
        self.start_auto_refresh()

    def _create_analysis_table(self):
        """Create the analysis jobs table."""
        columns = [
            {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
            {'name': 'status', 'label': 'Status', 'field': 'status_display', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at_local', 'sortable': True, 'style': 'width: 160px'},
            {'name': 'subtype', 'label': 'Type', 'field': 'subtype', 'sortable': True, 'style': 'width: 120px'},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'sortable': False, 'style': 'width: 100px'}
        ]
        
        analysis_data, self.total_records = self._get_analysis_data()
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
                        <span v-html="props.row.status_display"></span>
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
                    <q-btn v-if="props.row.can_cancel" 
                           flat dense icon="cancel" 
                           color="negative" 
                           @click="$parent.$emit('cancel_analysis', props.row.id)"
                           :disable="props.row.status === 'running'">
                        <q-tooltip>Cancel Analysis</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Handle events
            self.analysis_table.on('cancel_analysis', self.cancel_analysis)
            self.analysis_table.on('view_details', self.view_analysis_details)

    def _create_queue_status(self):
        """Create worker queue status display."""
        queue_info = self._get_queue_info()
        
        with ui.row().classes('w-full'):
            with ui.card().classes('flex-1'):
                ui.label('Worker Status')
                ui.label(f"Workers: {queue_info['worker_count']}")
                ui.label(f"Running: {queue_info['running_tasks']}")
                
            with ui.card().classes('flex-1'):
                ui.label('Queue Status')
                ui.label(f"Pending: {queue_info['pending_tasks']}")
                ui.label(f"Total Tasks: {queue_info['total_tasks']}")

    def _get_analysis_data(self) -> tuple[List[dict], int]:
        """Get analysis jobs data for the table with pagination and filtering."""
        try:
            with Session(get_db().bind) as session:
                # Build base query
                statement = select(MarketAnalysis)
                
                # Apply status filter
                if self.status_filter != 'all':
                    statement = statement.where(MarketAnalysis.status == MarketAnalysisStatus(self.status_filter))
                
                # Apply symbol filter
                if self.symbol_filter.strip():
                    # Support multiple symbols separated by comma
                    symbols = [s.strip().upper() for s in self.symbol_filter.split(',') if s.strip()]
                    if symbols:
                        statement = statement.where(MarketAnalysis.symbol.in_(symbols))
                
                # Get total count for pagination
                count_statement = select(func.count(MarketAnalysis.id))
                if self.status_filter != 'all':
                    count_statement = count_statement.where(MarketAnalysis.status == MarketAnalysisStatus(self.status_filter))
                if self.symbol_filter.strip():
                    symbols = [s.strip().upper() for s in self.symbol_filter.split(',') if s.strip()]
                    if symbols:
                        count_statement = count_statement.where(MarketAnalysis.symbol.in_(symbols))
                
                total_count = session.exec(count_statement).first() or 0
                
                # Apply ordering and pagination
                statement = statement.order_by(MarketAnalysis.created_at.desc())
                offset = (self.current_page - 1) * self.page_size
                statement = statement.offset(offset).limit(self.page_size)
                
                market_analyses = session.exec(statement).all()
                
                analysis_data = []
                for analysis in market_analyses:
                    # Get expert instance info
                    expert_instance = get_instance(ExpertInstance, analysis.source_expert_instance_id)
                    expert_name = expert_instance.expert if expert_instance else "Unknown"
                    
                    # Convert UTC to local time for display
                    local_time = analysis.created_at.replace(tzinfo=timezone.utc).astimezone() if analysis.created_at else None
                    created_local = local_time.strftime("%Y-%m-%d %H:%M:%S") if local_time else "Unknown"
                    
                    # Status with icon
                    status_icons = {
                        'pending': '⏳',
                        'running': '🔄', 
                        'completed': '✅',
                        'failed': '❌',
                        'cancelled': '🚫'
                    }
                    status_value = analysis.status.value if analysis.status else 'unknown'
                    status_icon = status_icons.get(status_value, '❓')
                    status_display = f'{status_icon} {status_value.title()}'
                    
                    # Determine if can cancel
                    can_cancel = analysis.status in [MarketAnalysisStatus.PENDING]
                    
                    # Get subtype display
                    subtype_display = analysis.subtype.value.replace('_', ' ').title() if analysis.subtype else 'Unknown'
                    
                    analysis_data.append({
                        'id': analysis.id,
                        'symbol': analysis.symbol,
                        'expert_name': expert_name,
                        'status': status_value,
                        'status_display': status_display,
                        'created_at_local': created_local,
                        'subtype': subtype_display,
                        'can_cancel': can_cancel,
                        'expert_instance_id': analysis.source_expert_instance_id
                    })
                
                return analysis_data, total_count
                
        except Exception as e:
            logger.error(f"Error getting analysis data: {e}", exc_info=True)
            return [], 0

    def _create_pagination_controls(self):
        """Create pagination controls."""
        if self.total_pages <= 1:
            return
            
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
    
    def _on_status_filter_change(self, event):
        """Handle status filter change."""
        self.status_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _on_symbol_filter_change(self, event):
        """Handle symbol filter change."""
        self.symbol_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    
    def _clear_filters(self):
        """Clear all filters."""
        self.status_filter = 'all'
        self.symbol_filter = ''
        self.current_page = 1
        if hasattr(self, 'status_select'):
            self.status_select.value = 'all'
        if hasattr(self, 'symbol_input'):
            self.symbol_input.value = ''
        self.refresh_data()

    def _get_queue_info(self) -> dict:
        """Get worker queue information."""
        try:
            worker_queue = self._get_worker_queue()
            worker_count = worker_queue.get_worker_count()
            all_tasks = worker_queue.get_all_tasks()
            
            # Filter tasks safely, checking if they have status attribute
            running_tasks = len([t for t in all_tasks if hasattr(t, 'status') and t.status == WorkerTaskStatus.RUNNING])
            pending_tasks = len([t for t in all_tasks if hasattr(t, 'status') and t.status == WorkerTaskStatus.PENDING])
            total_tasks = len(all_tasks)
            
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
            success = self._get_worker_queue().cancel_analysis_task(analysis.source_expert_instance_id, analysis.symbol)
            
            if success:
                # Update the analysis status
                analysis.status = MarketAnalysisStatus.CANCELLED
                from ...core.db import update_instance
                update_instance(analysis)
                
                ui.notify(f"Analysis {analysis_id} cancelled successfully", type='positive')
                self.refresh_data()
            else:
                ui.notify("Failed to cancel analysis - task may already be running", type='warning')
                
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
            
            # Navigate to the detail page
            ui.navigate.to(f'/market_analysis/{analysis_id}')
            
        except Exception as e:
            logger.error(f"Error navigating to analysis details {analysis_id if analysis_id else 'unknown'}: {e}", exc_info=True)
            ui.notify(f"Error opening details: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the data in all tables."""
        try:
            # Update analysis table with pagination
            if self.analysis_table:
                analysis_data, self.total_records = self._get_analysis_data()
                self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
                
                # Ensure current page is valid
                if self.current_page > self.total_pages:
                    self.current_page = max(1, self.total_pages)
                
                self.analysis_table.rows = analysis_data
                self.analysis_table.update()
                
                # Re-create pagination controls to update button states
                # Note: In a real app, you'd want to update controls more efficiently
            
            #logger.debug("Job monitoring data refreshed")
            
        except Exception as e:
            logger.error(f"Error refreshing job monitoring data: {e}", exc_info=True)

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
        
        self.refresh_timer = ui.timer(5.0, self.refresh_data)  # Refresh every 5 seconds

    def stop_auto_refresh(self):
        """Stop auto-refresh timer."""
        if self.refresh_timer:
            self.refresh_timer.cancel()
            self.refresh_timer = None


class ManualAnalysisTab:
    def __init__(self):
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Manual Analysis Jobs').classes('text-lg font-bold')
            ui.label('Submit manual analysis jobs for specific symbols and experts').classes('text-sm text-gray-600 mb-4')
            
            # Manual analysis form
            with ui.row().classes('w-full gap-4'):
                with ui.column().classes('flex-1'):
                    self.symbol_input = ui.input('Symbol', placeholder='e.g., AAPL').classes('w-full')
                    
                with ui.column().classes('flex-1'):
                    # Expert selection
                    expert_instances = get_all_instances(ExpertInstance)
                    expert_options = {f"{exp.id}": f"{exp.expert} (ID: {exp.id})" for exp in expert_instances if exp.enabled}
                    
                    self.expert_select = ui.select(
                        options=expert_options,
                        label='Expert Instance'
                    ).classes('w-full')
                    
                with ui.column():
                    ui.button('Submit Analysis', on_click=self.submit_market_analysis, icon='play_arrow').classes('mt-6')

    def submit_market_analysis(self):
        """Submit a manual analysis job."""
        try:
            symbol = self.symbol_input.value.strip().upper()
            expert_instance_id = self.expert_select.value
            
            if not symbol:
                ui.notify("Please enter a symbol", type='negative')
                return
                
            if not expert_instance_id:
                ui.notify("Please select an expert instance", type='negative')
                return
            
            # Submit to job manager
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            
            success = job_manager.submit_market_analysis(int(expert_instance_id), symbol)
            
            if success:
                ui.notify(f"Manual analysis submitted for {symbol}", type='positive')
                # Clear form
                self.symbol_input.value = ''
                self.expert_select.value = None
            else:
                ui.notify("Analysis already pending for this symbol and expert", type='warning')
                
        except Exception as e:
            logger.error(f"Error submitting manual analysis: {e}", exc_info=True)
            ui.notify(f"Error submitting analysis: {str(e)}", type='negative')


class ScheduledJobsTab:
    def __init__(self):
        self.scheduled_jobs_table = None
        self.refresh_timer = None
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Scheduled Analysis Jobs').classes('text-lg font-bold')
            ui.label('View all scheduled analysis jobs for the current week').classes('text-sm text-gray-600 mb-4')
            
            with ui.row().classes('w-full justify-between items-center mb-4'):
                filter_input = ui.input('Filter', placeholder='Filter scheduled jobs...').classes('w-1/3')
                with ui.row():
                    ui.button('Refresh', on_click=self.refresh_data, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Scheduled jobs table
            self._create_scheduled_jobs_table()
            
            # Bind filter to table
            filter_input.bind_value(self.scheduled_jobs_table, "filter")
        
        # Start auto-refresh
        self.start_auto_refresh()

    def _create_scheduled_jobs_table(self):
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
        
        scheduled_data = self._get_scheduled_jobs_data()
        
        with ui.card().classes('w-full'):
            ui.label('Current Week Scheduled Jobs').classes('text-md font-bold mb-2')
            self.scheduled_jobs_table = ui.table(
                columns=columns, 
                rows=scheduled_data, 
                row_key='id'
            ).classes('w-full')
            
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

    def _get_scheduled_jobs_data(self) -> List[dict]:
        """Get scheduled jobs data for the current week - one line per instrument per expert instance per job type."""
        try:
            from datetime import datetime, timedelta
            import json
            from ...core.types import AnalysisUseCase
            
            # Get all enabled expert instances
            expert_instances = get_all_instances(ExpertInstance)
            
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
                        
                        # Create one entry per instrument for this expert instance and job type
                        for symbol in enabled_instruments:
                            combination_key = f"{expert_instance.id}_{symbol}_{schedule_key}"
                            
                            jobs_by_combination[combination_key] = {
                                'id': combination_key,
                                'symbol': symbol,
                                'expert_name': f"{expert_instance.expert}",
                                'expert_instance_id': expert_instance.id,
                                'job_type': job_type_display,
                                'subtype': subtype.value,
                                'weekdays': ', '.join(enabled_weekdays) if enabled_weekdays else 'None',
                                'times': ', '.join(times) if times else 'Not specified',
                                'expert_disabled': False
                            }
                
                except Exception as e:
                    logger.error(f"Error processing schedule for expert instance {expert_instance.id}: {e}", exc_info=True)
                    continue
            
            # Convert to list and sort by expert name, then symbol
            scheduled_jobs = list(jobs_by_combination.values())
            scheduled_jobs.sort(key=lambda x: (x['expert_name'], x['symbol']))
            
            return scheduled_jobs
            
        except Exception as e:
            logger.error(f"Error getting scheduled jobs data: {e}", exc_info=True)
            return []

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
            
            success = job_manager.submit_market_analysis(expert_instance_id, symbol, subtype=subtype)
            
            if success:
                ui.notify(f"Analysis started for {symbol} ({subtype}) using expert instance {expert_instance_id}", type='positive')
            else:
                ui.notify("Analysis already pending for this symbol and expert", type='warning')
                
        except Exception as e:
            logger.error(f"Error running analysis now: {e}", exc_info=True)
            ui.notify(f"Error starting analysis: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the scheduled jobs data."""
        try:
            if self.scheduled_jobs_table:
                self.scheduled_jobs_table.rows = self._get_scheduled_jobs_data()
                self.scheduled_jobs_table.update()
            
            #logger.debug("Scheduled jobs data refreshed")
            
        except Exception as e:
            logger.error(f"Error refreshing scheduled jobs data: {e}", exc_info=True)

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


def content() -> None:
    with ui.tabs() as tabs:
        ui.tab('Job Monitoring')
        ui.tab('Manual Analysis')
        ui.tab('Scheduled Jobs')

    with ui.tab_panels(tabs, value='Job Monitoring').classes('w-full'):
        with ui.tab_panel('Job Monitoring'):
            JobMonitoringTab()
        with ui.tab_panel('Manual Analysis'):
            ManualAnalysisTab()
        with ui.tab_panel('Scheduled Jobs'):
            ScheduledJobsTab()
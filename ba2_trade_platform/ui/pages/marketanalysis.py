from nicegui import ui
from datetime import datetime
from typing import List, Optional

from ...core.WorkerQueue import get_worker_queue
from ...core.db import get_all_instances, get_instance
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput
from ...core.types import MarketAnalysisStatus, WorkerTaskStatus
from ...logger import logger


class JobMonitoringTab:
    def __init__(self):
        self.worker_queue = get_worker_queue()
        self.analysis_table = None
        self.refresh_timer = None
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Market Analysis Job Monitoring').classes('text-lg font-bold')
            
            with ui.row().classes('w-full justify-between items-center mb-4'):
                ui.button('Refresh', on_click=self.refresh_data, icon='refresh')
                with ui.switch('Auto-refresh', value=True) as auto_refresh:
                    auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Analysis jobs table
            self._create_analysis_table()
            
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
            {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True},
            {'name': 'status', 'label': 'Status', 'field': 'status', 'sortable': True},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at_str', 'sortable': True},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'sortable': False}
        ]
        
        analysis_data = self._get_analysis_data()
        
        with ui.card().classes('w-full'):
            ui.label('Analysis Jobs').classes('text-md font-bold mb-2')
            self.analysis_table = ui.table(
                columns=columns, 
                rows=analysis_data, 
                row_key='id'
            ).classes('w-full')
            
            # Add action buttons
            self.analysis_table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn v-if="props.row.can_cancel" 
                           flat dense icon="cancel" 
                           color="negative" 
                           @click="$parent.$emit('cancel_analysis', props.row.id)"
                           :disable="props.row.status === 'RUNNING'">
                        <q-tooltip>Cancel Analysis</q-tooltip>
                    </q-btn>
                    <q-btn v-if="props.row.status === 'COMPLETED'" 
                           flat dense icon="visibility" 
                           color="primary" 
                           @click="$parent.$emit('view_results', props.row.id)">
                        <q-tooltip>View Results</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Handle events
            self.analysis_table.on('cancel_analysis', self.cancel_analysis)
            self.analysis_table.on('view_results', self.view_analysis_results)

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

    def _get_analysis_data(self) -> List[dict]:
        """Get analysis jobs data for the table."""
        try:
            # Get all market analysis records
            market_analyses = get_all_instances(MarketAnalysis)
            
            analysis_data = []
            for analysis in market_analyses:
                # Get expert instance info
                expert_instance = get_instance(ExpertInstance, analysis.source_expert_instance_id)
                expert_name = expert_instance.expert if expert_instance else "Unknown"
                
                # Format created timestamp
                created_str = analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if analysis.created_at else "Unknown"
                
                # Determine if can cancel
                can_cancel = analysis.status in [MarketAnalysisStatus.PENDING]
                
                analysis_data.append({
                    'id': analysis.id,
                    'symbol': analysis.symbol,
                    'expert_name': expert_name,
                    'status': analysis.status.value if analysis.status else 'UNKNOWN',
                    'created_at_str': created_str,
                    'can_cancel': can_cancel,
                    'expert_instance_id': analysis.source_expert_instance_id
                })
            
            # Sort by created date, newest first
            analysis_data.sort(key=lambda x: x['created_at_str'], reverse=True)
            return analysis_data
            
        except Exception as e:
            logger.error(f"Error getting analysis data: {e}")
            return []

    def _get_queue_info(self) -> dict:
        """Get worker queue information."""
        try:
            worker_count = self.worker_queue.get_worker_count()
            running_tasks = len([t for t in self.worker_queue.get_all_tasks() if t.status == WorkerTaskStatus.RUNNING])
            pending_tasks = len([t for t in self.worker_queue.get_all_tasks() if t.status == WorkerTaskStatus.PENDING])
            total_tasks = len(self.worker_queue.get_all_tasks())
            
            return {
                'worker_count': worker_count,
                'running_tasks': running_tasks,
                'pending_tasks': pending_tasks,
                'total_tasks': total_tasks
            }
        except Exception as e:
            logger.error(f"Error getting queue info: {e}")
            return {
                'worker_count': 0,
                'running_tasks': 0,
                'pending_tasks': 0,
                'total_tasks': 0
            }

    def cancel_analysis(self, analysis_id: int):
        """Cancel an analysis job."""
        try:
            # Get the market analysis
            analysis = get_instance(MarketAnalysis, analysis_id)
            if not analysis:
                ui.notify("Analysis not found", type='negative')
                return
            
            if analysis.status != MarketAnalysisStatus.PENDING:
                ui.notify("Can only cancel pending analyses", type='negative')
                return
            
            # Try to cancel the task in the worker queue
            success = self.worker_queue.cancel_analysis_task(analysis.source_expert_instance_id, analysis.symbol)
            
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
            logger.error(f"Error cancelling analysis {analysis_id}: {e}")
            ui.notify(f"Error cancelling analysis: {str(e)}", type='negative')

    def view_analysis_results(self, analysis_id: int):
        """View analysis results in a dialog."""
        try:
            analysis = get_instance(MarketAnalysis, analysis_id)
            if not analysis:
                ui.notify("Analysis not found", type='negative')
                return
            
            # Get analysis outputs
            from sqlmodel import Session, select
            from ...core.db import get_db
            
            session = get_db()
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id)
            outputs = session.exec(statement).all()
            session.close()
            
            # Create dialog
            with ui.dialog() as dialog, ui.card().classes('w-96 max-w-full'):
                ui.label(f'Analysis Results - {analysis.symbol}').classes('text-lg font-bold')
                
                with ui.scroll_area().classes('h-64 w-full'):
                    if analysis.state:
                        ui.label('Analysis State:').classes('font-bold mt-2')
                        ui.json_editor({'content': {'json': analysis.state}}).classes('w-full').props('read-only')
                    
                    if outputs:
                        ui.label('Analysis Outputs:').classes('font-bold mt-4')
                        for output in outputs:
                            with ui.expansion(output.name):
                                if output.text:
                                    ui.label(output.text).classes('whitespace-pre-wrap')
                                else:
                                    ui.label(f"Binary data ({len(output.blob)} bytes)" if output.blob else "No content")
                    else:
                        ui.label('No analysis outputs available')
                
                with ui.row().classes('w-full justify-end'):
                    ui.button('Close', on_click=dialog.close)
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error viewing analysis results {analysis_id}: {e}")
            ui.notify(f"Error loading results: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the data in all tables."""
        try:
            # Update analysis table
            if self.analysis_table:
                self.analysis_table.rows = self._get_analysis_data()
                self.analysis_table.update()
            
            #logger.debug("Job monitoring data refreshed")
            
        except Exception as e:
            logger.error(f"Error refreshing job monitoring data: {e}")

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
                    ui.button('Submit Analysis', on_click=self.submit_manual_analysis, icon='play_arrow').classes('mt-6')

    def submit_manual_analysis(self):
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
            
            success = job_manager.submit_manual_analysis(int(expert_instance_id), symbol)
            
            if success:
                ui.notify(f"Manual analysis submitted for {symbol}", type='positive')
                # Clear form
                self.symbol_input.value = ''
                self.expert_select.value = None
            else:
                ui.notify("Analysis already pending for this symbol and expert", type='warning')
                
        except Exception as e:
            logger.error(f"Error submitting manual analysis: {e}")
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
            {'name': 'weekday', 'label': 'Weekday', 'field': 'weekday', 'sortable': True},
            {'name': 'times', 'label': 'Scheduled Times', 'field': 'times', 'sortable': True},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True},
            {'name': 'instance_id', 'label': 'Instance ID', 'field': 'expert_instance_id', 'sortable': True},
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
                           @click="$parent.$emit('run_now', props.row.expert_instance_id, props.row.symbol)"
                           :disable="props.row.expert_disabled">
                        <q-tooltip>Run Analysis Now</q-tooltip>
                    </q-btn>
                </q-td>
            ''')
            
            # Handle events
            self.scheduled_jobs_table.on('run_now', self.run_analysis_now)

    def _get_scheduled_jobs_data(self) -> List[dict]:
        """Get scheduled jobs data for the current week."""
        try:
            from datetime import datetime, timedelta
            import json
            
            # Get all enabled expert instances
            expert_instances = get_all_instances(ExpertInstance)
            
            scheduled_jobs = []
            
            for expert_instance in expert_instances:
                if not expert_instance.enabled:
                    continue
                
                # Get the execution schedule setting
                try:
                    from ...core.ExtendableSettingsInterface import ExtendableSettingsInterface
                    from ...modules.experts import get_expert_class
                    
                    expert_class = get_expert_class(expert_instance.expert)
                    if not expert_class:
                        continue
                    
                    expert = expert_class(expert_instance.id)
                    schedule_setting = expert.settings.get('execution_schedule')
                    
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
                    
                    # Get enabled instruments for this expert
                    enabled_instruments = expert.get_enabled_instruments()
                    
                    # Create entries for each enabled day and instrument combination
                    day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                    weekday_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                    
                    for i, day_name in enumerate(day_names):
                        if days.get(day_name, False):
                            for symbol in enabled_instruments:
                                scheduled_jobs.append({
                                    'id': f"{expert_instance.id}_{day_name}_{symbol}",
                                    'weekday': weekday_names[i],
                                    'times': ', '.join(times) if times else 'Not specified',
                                    'symbol': symbol,
                                    'expert_name': f"{expert_instance.expert}",
                                    'expert_instance_id': expert_instance.id,
                                    'expert_disabled': False
                                })
                
                except Exception as e:
                    logger.error(f"Error processing schedule for expert instance {expert_instance.id}: {e}")
                    continue
            
            # Sort by weekday and time
            day_order = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}
            scheduled_jobs.sort(key=lambda x: (day_order.get(x['weekday'], 7), x['times'], x['symbol']))
            
            return scheduled_jobs
            
        except Exception as e:
            logger.error(f"Error getting scheduled jobs data: {e}")
            return []

    def run_analysis_now(self, event_data):
        """Run analysis immediately for the selected expert and symbol."""
        try:
            # Extract expert_instance_id and symbol from event data
            # NiceGUI passes GenericEventArguments with args attribute
            if hasattr(event_data, 'args') and len(event_data.args) >= 2:
                expert_instance_id = int(event_data.args[0])
                symbol = str(event_data.args[1])
            elif isinstance(event_data, (list, tuple)) and len(event_data) >= 2:
                expert_instance_id = int(event_data[0])
                symbol = str(event_data[1])
            else:
                logger.error(f"Invalid event data for run_analysis_now: {event_data}")
                ui.notify("Invalid event data", type='negative')
                return
            
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            
            success = job_manager.submit_manual_analysis(expert_instance_id, symbol)
            
            if success:
                ui.notify(f"Analysis started for {symbol} using expert instance {expert_instance_id}", type='positive')
            else:
                ui.notify("Analysis already pending for this symbol and expert", type='warning')
                
        except Exception as e:
            logger.error(f"Error running analysis now: {e}")
            ui.notify(f"Error starting analysis: {str(e)}", type='negative')

    def refresh_data(self):
        """Refresh the scheduled jobs data."""
        try:
            if self.scheduled_jobs_table:
                self.scheduled_jobs_table.rows = self._get_scheduled_jobs_data()
                self.scheduled_jobs_table.update()
            
            logger.debug("Scheduled jobs data refreshed")
            
        except Exception as e:
            logger.error(f"Error refreshing scheduled jobs data: {e}")

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
from nicegui import ui
from datetime import datetime, timezone
from typing import List, Optional
import math

from ...core.WorkerQueue import get_worker_queue
from ...core.db import get_all_instances, get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput
from ...core.types import MarketAnalysisStatus, WorkerTaskStatus, OrderDirection, OrderRecommendation, OrderOpenType
from ...core.models import TradingOrder
from ...core.types import OrderStatus
from ...core.models import AccountDefinition
from ...modules.accounts import get_account_class
from ...core.db import add_instance
from ...logger import logger
from sqlmodel import select, func


class JobMonitoringTab:
    def __init__(self):
        self.worker_queue = None  # Lazy initialization
        self.analysis_table = None
        self.refresh_timer = None
        self.pagination_container = None  # Container for pagination controls
        # Pagination and filtering state
        self.current_page = 1
        self.page_size = 25
        self.total_pages = 1
        self.total_records = 0
        self.status_filter = 'all'
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
                    
                
                with ui.row().classes('gap-2'):
                    ui.button('Clear Filters', on_click=self._clear_filters, icon='clear')
                    ui.button('Refresh', on_click=self.refresh_data, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Analysis jobs table
            self._create_analysis_table()
            self.symbol_input.bind_value(self.analysis_table, "filter")
            # Pagination controls container
            self.pagination_container = ui.row().classes('w-full')
            with self.pagination_container:
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
            {'name': 'recommendation', 'label': 'Recommendation', 'field': 'recommendation', 'sortable': True, 'style': 'width: 130px'},
            {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'sortable': True, 'style': 'width: 100px'},
            {'name': 'expected_profit', 'label': 'Expected Profit', 'field': 'expected_profit', 'sortable': True, 'style': 'width: 120px'},
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
            with get_db() as session:
                # Build base query
                statement = select(MarketAnalysis)
                
                # Apply status filter
                if self.status_filter != 'all':
                    statement = statement.where(MarketAnalysis.status == MarketAnalysisStatus(self.status_filter))
                
          
                # Get total count for pagination
                count_statement = select(func.count(MarketAnalysis.id))
                if self.status_filter != 'all':
                    count_statement = count_statement.where(MarketAnalysis.status == MarketAnalysisStatus(self.status_filter))
 
                total_count = session.exec(count_statement).first() or 0
                
                # Apply ordering and pagination
                statement = statement.order_by(MarketAnalysis.created_at.desc())
                offset = (self.current_page - 1) * self.page_size
                statement = statement.offset(offset).limit(self.page_size)
                
                market_analyses = session.exec(statement).all()
                
                analysis_data = []
                for analysis in market_analyses:
                    # Get expert instance info
                    expert_instance = get_instance(ExpertInstance, analysis.expert_instance_id)
                    expert_name = expert_instance.user_description or expert_instance.expert if expert_instance else "Unknown"
                    
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
                    
                    # Get recommendation and confidence from expert_recommendations
                    recommendation_display = '-'
                    confidence_display = '-'
                    expected_profit_display = '-'
                    if analysis.expert_recommendations and len(analysis.expert_recommendations) > 0:
                        # Get the first (most recent) recommendation
                        rec = analysis.expert_recommendations[0]
                        if rec.recommended_action:
                            # Add icons for BUY/SELL
                            action_icons = {
                                'BUY': '📈',
                                'SELL': '📉',
                                'HOLD': '⏸️',
                                'ERROR': '❌'
                            }
                            action_value = rec.recommended_action.value
                            action_icon = action_icons.get(action_value, '')
                            recommendation_display = f'{action_icon} {action_value}'
                        
                        if rec.confidence is not None:
                            confidence_display = f'{rec.confidence:.1f}%'
                        
                        if rec.expected_profit_percent is not None:
                            # Format with + or - sign and color indicator
                            sign = '+' if rec.expected_profit_percent >= 0 else ''
                            expected_profit_display = f'{sign}{rec.expected_profit_percent:.2f}%'
                    
                    analysis_data.append({
                        'id': analysis.id,
                        'symbol': analysis.symbol,
                        'expert_name': expert_name,
                        'status': status_value,
                        'status_display': status_display,
                        'recommendation': recommendation_display,
                        'confidence': confidence_display,
                        'expected_profit': expected_profit_display,
                        'created_at_local': created_local,
                        'subtype': subtype_display,
                        'can_cancel': can_cancel,
                        'expert_instance_id': analysis.expert_instance_id
                    })
                
                return analysis_data, total_count
                
        except Exception as e:
            logger.error(f"Error getting analysis data: {e}", exc_info=True)
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
    
    def _on_status_filter_change(self, event):
        """Handle status filter change."""
        self.status_filter = event.value
        self.current_page = 1  # Reset to first page when filtering
        self.refresh_data()
    

    def _clear_filters(self):
        """Clear all filters."""
        self.status_filter = 'all'
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
            
            # Navigate to the detail page
            ui.navigate.to(f'/market_analysis/{analysis_id}')
            
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
                self._create_pagination_controls()
            
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
                    expert_options = {f"{exp.id}": f"{exp.expert} (ID: {exp.id})" for exp in expert_instances if exp.enabled}
                    
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
                enabled_instruments = expert.get_enabled_instruments()
                logger.debug(f"Expert {self.expert_instance_id} has {len(enabled_instruments)} enabled instruments")
                
                # Clear and recreate instrument selector with filtered instruments
                self.instrument_selector_container.clear()
                
                with self.instrument_selector_container:
                    from ..components.InstrumentSelector import InstrumentSelector
                    self.instrument_selector = InstrumentSelector(
                        on_selection_change=self.on_instrument_selection_change,
                        instrument_list=enabled_instruments if enabled_instruments else None,
                        hide_weights=True
                    )
                    self.instrument_selector.render()
                
                # Enable analysis type selection
                self.analysis_type_select.enable()
            else:
                ui.notify("Failed to load expert instance", type='negative')
                
        except Exception as e:
            logger.error(f"Error loading expert instruments: {e}", exc_info=True)
            ui.notify(f"Error loading expert: {str(e)}", type='negative')
    
    def on_analysis_type_change(self):
        """Handle analysis type selection change."""
        self.analysis_type = self.analysis_type_select.value
        logger.debug(f"Analysis type changed to: {self.analysis_type}")
    
    def on_instrument_selection_change(self, selected_instruments):
        """Handle instrument selection change."""
        logger.debug(f"Selected {len(selected_instruments)} instruments for analysis")
    
    def submit_bulk_analysis(self):
        """Submit analysis jobs for all selected instruments."""
        try:
            if not self.expert_instance_id:
                ui.notify("Please select an expert instance", type='negative')
                return
                
            if not self.analysis_type:
                ui.notify("Please select an analysis type", type='negative')
                return
                
            if not self.instrument_selector:
                ui.notify("No instruments available for selection", type='negative')
                return
            
            selected_instruments = self.instrument_selector.get_selected_instruments()
            
            if not selected_instruments:
                ui.notify("Please select at least one instrument", type='negative')
                return
            
            # Submit analysis jobs for all selected instruments
            from ...core.JobManager import get_job_manager
            from ...core.types import AnalysisUseCase
            job_manager = get_job_manager()
            
            # Convert analysis type string to enum
            subtype = AnalysisUseCase.ENTER_MARKET if self.analysis_type == AnalysisUseCase.ENTER_MARKET.value else AnalysisUseCase.OPEN_POSITIONS
            
            successful_submissions = 0
            failed_submissions = 0
            duplicate_submissions = 0
            
            for instrument in selected_instruments:
                symbol = instrument['name']
                
                try:
                    success = job_manager.submit_market_analysis(
                        int(self.expert_instance_id), 
                        symbol, 
                        subtype=subtype,
                        bypass_balance_check=True,  # Manual analysis bypasses balance check
                        bypass_transaction_check=True  # Manual analysis bypasses transaction checks
                    )
                    
                    if success:
                        successful_submissions += 1
                        logger.debug(f"Successfully submitted analysis for {symbol}")
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
        self.render()

    def render(self):
        with ui.card().classes('w-full'):
            ui.label('Scheduled Analysis Jobs').classes('text-lg font-bold')
            ui.label('View all scheduled analysis jobs for the current week').classes('text-sm text-gray-600 mb-4')
            
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
                    
                    # Text filter
                    self.filter_input = ui.input('Search', placeholder='Filter by symbol...').classes('w-40')
                
                with ui.row().classes('gap-2'):
                    ui.button('Clear Filters', on_click=self._clear_filters, icon='clear')
                    ui.button('Refresh', on_click=self.refresh_data, icon='refresh')
                    with ui.switch('Auto-refresh', value=True) as auto_refresh:
                        auto_refresh.on_value_change(self.toggle_auto_refresh)
            
            # Scheduled jobs table
            self._create_scheduled_jobs_table()
            
            # Bind text filter to table
            self.filter_input.bind_value(self.scheduled_jobs_table, "filter")
            
            # Pagination controls container
            self.pagination_container = ui.row().classes('w-full')
            with self.pagination_container:
                self._create_pagination_controls()
        
        # Start auto-refresh
        self.start_auto_refresh()

    def _get_expert_filter_options(self) -> dict:
        """Get expert instance filter options."""
        try:
            expert_instances = get_all_instances(ExpertInstance)
            options = {'all': 'All Experts'}
            
            for instance in expert_instances:
                if instance.enabled:
                    label = f"{instance.expert} (ID: {instance.id})"
                    if instance.user_description:
                        label += f" - {instance.user_description}"
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
    
    def _clear_filters(self):
        """Clear all filters."""
        self.expert_filter = 'all'
        self.current_page = 1
        if hasattr(self, 'expert_select'):
            self.expert_select.value = 'all'
        if hasattr(self, 'filter_input'):
            self.filter_input.value = ''
        self.refresh_data()

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

    def refresh_data(self):
        """Refresh the scheduled jobs data."""
        try:
            if self.scheduled_jobs_table:
                scheduled_data, self.total_records = self._get_scheduled_jobs_data()
                self.total_pages = max(1, math.ceil(self.total_records / self.page_size))
                
                # Ensure current page is valid
                if self.current_page > self.total_pages:
                    self.current_page = max(1, self.total_pages)
                
                self.scheduled_jobs_table.rows = scheduled_data
                self.scheduled_jobs_table.update()
                
                # Re-create pagination controls to update button states
                self._create_pagination_controls()
            
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
            ui.label('Order Recommendations').classes('text-lg font-bold')
            ui.label('Expert recommendations and generated orders').classes('text-sm text-gray-600 mb-4')
            
            # Controls
            with ui.row().classes('w-full justify-between items-center mb-4 gap-4'):
                with ui.row().classes('items-center gap-2'):
                    ui.button('Refresh', on_click=self.refresh_data).props('color=primary outline')
                    
                    # Expert selector for processing recommendations
                    expert_options = self._get_expert_options()
                    self.expert_select = ui.select(
                        options=expert_options,
                        label='Select Expert',
                        value='all'  # Default to 'all'
                    ).classes('w-64').props('dense outlined')
                    self.expert_select.on_value_change(self._on_expert_filter_change)
                    
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
                        {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
                    ]
                    
                    self.summary_table = ui.table(
                        columns=summary_columns,
                        rows=recommendations_summary,
                        row_key='symbol',
                        pagination=10
                    ).classes('w-full')
                    
                    # Add action buttons
                    self.summary_table.add_slot('body-cell-actions', '''
                        <q-td :props="props">
                            <q-btn icon="visibility" 
                                   flat 
                                   dense 
                                   color="primary" 
                                   title="View Details"
                                   @click="$parent.$emit('view_details', props.row.symbol)" />
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
                    self.summary_table.on('place_order', self._handle_place_order)
            
            # Clear detail container if no symbol selected
            if not self.selected_symbol:
                self.detail_container.clear()
                
        except Exception as e:
            logger.error(f"Error refreshing order recommendations data: {e}", exc_info=True)

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
                    # Extract expert ID from the filter string format: "ExpertName (ID: 123)"
                    try:
                        expert_id = int(self.expert_filter.split('ID: ')[-1].rstrip(')'))
                        statement = statement.where(ExpertRecommendation.instance_id == expert_id)
                    except (ValueError, IndexError):
                        pass  # If parsing fails, show all
                
                statement = statement.group_by(ExpertRecommendation.symbol).order_by(func.max(ExpertRecommendation.created_at).desc())
                
                
                results = session.exec(statement).all()
                
                summary_data = []
                for result in results:
                    symbol = result.symbol
                    
                    # Count orders created for this symbol
                    orders_statement = select(func.count(TradingOrder.id)).where(
                        TradingOrder.symbol == symbol,
                        TradingOrder.transaction_id.is_not(None)
                    )
                    orders_count = session.exec(orders_statement).first() or 0
                    
                    summary_data.append({
                        'symbol': symbol,
                        'total_recommendations': result.total_recommendations,
                        'buy_count': result.buy_count or 0,
                        'sell_count': result.sell_count or 0,
                        'hold_count': result.hold_count or 0,
                        'avg_confidence': f"{result.avg_confidence:.1f}%" if result.avg_confidence else 'N/A',
                        'orders_created': orders_count,
                        'latest': result.latest_created_at.strftime('%Y-%m-%d %H:%M') if result.latest_created_at else 'N/A'
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
                        {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left'},
                        {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
                    ]
                    
                    rec_table = ui.table(columns=rec_columns, rows=recommendations, row_key='id').classes('w-full')
                    
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
                            <span v-else class="text-grey-5">Order exists</span>
                            <q-btn v-if="props.row.analysis_id" 
                                   icon="visibility" 
                                   flat 
                                   dense 
                                   color="primary" 
                                   title="View Analysis"
                                   @click="$parent.$emit('view_analysis', props.row.analysis_id)" />
                        </q-td>
                    ''')
                    
                    rec_table.on('place_order_rec', self._handle_place_order_recommendation)
                    rec_table.on('view_analysis', self._handle_view_analysis)
                    
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
                    # Extract expert ID from the filter string format: "ExpertName (ID: 123)"
                    try:
                        expert_id = int(self.expert_filter.split('ID: ')[-1].rstrip(')'))
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
                    
                    # Determine order status for this recommendation
                    has_non_pending_order = any(order.status != OrderStatus.PENDING for order in existing_orders)
                    has_pending_order = any(order.status == OrderStatus.PENDING for order in existing_orders)
                    can_place_order = not has_non_pending_order  # Can place if no non-pending orders exist
                    
                    # Always get the enum value, not the string representation
                    if hasattr(recommendation.recommended_action, 'value'):
                        action_raw = recommendation.recommended_action.value
                    else:
                        # Fallback for non-enum values
                        action_raw = str(recommendation.recommended_action)
                        
                    # Convert enum values to readable text
                    action_mapping = {
                        OrderRecommendation.BUY.value: 'Buy', 
                        OrderRecommendation.SELL.value: 'Sell', 
                        OrderRecommendation.HOLD.value: 'Hold',
                        OrderRecommendation.ERROR.value: 'Error'
                    }
                    action = action_mapping.get(action_raw, action_raw)
                    created_at = recommendation.created_at.strftime('%Y-%m-%d %H:%M:%S') if recommendation.created_at else ''
                    
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
                        'expert_name': expert_instance.user_description or expert_instance.expert,
                        'created_at': created_at,
                        'analysis_id': analysis.id if analysis else None,
                        'can_place_order': can_place_order,
                        'has_pending_order': has_pending_order,
                        'existing_orders_count': len(existing_orders)
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
                        from ...modules.accounts import providers
                        provider_cls = providers.get(account.provider)
                        if not provider_cls:
                            ui.notify(f'No provider found for {account.provider}', type='negative')
                            return
                        
                        provider_obj = provider_cls(account.id)
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
                ui.navigate.to(f'/market_analysis/{analysis_id}')
        except Exception as e:
            logger.error(f"Error navigating to analysis detail: {e}", exc_info=True)

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
                        # Fallback for non-enum values
                        action_raw = str(recommendation.recommended_action)
                        
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
                comment=f"Manual order from Order Recommendations - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # Add to database first
            order_id = add_instance(order)
            
            if order_id:
                # Get the order back from database to get the complete object
                from ...core.db import get_instance
                order = get_instance(TradingOrder, order_id)
                
                # Submit the order through the account provider
                provider_cls = get_account_class(account.provider)
                if provider_cls:
                    try:
                        provider_obj = provider_cls(account.id)
                        submitted_order = provider_obj.submit_order(order)
                        if submitted_order:
                            ui.notify(f'Order {order_id} submitted successfully to {account.provider}', type='positive')
                        else:
                            ui.notify(f'Order {order_id} created but failed to submit to broker', type='warning')
                    except Exception as e:
                        logger.error(f"Error submitting order {order_id}: {e}", exc_info=True)
                        ui.notify(f'Order {order_id} created but submission failed: {str(e)}', type='warning')
                else:
                    ui.notify(f'Order {order_id} created but no provider found for {account.provider}', type='warning')
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
            
            # Always start with 'All Experts' option
            options = ['all']
            
            # Create options: display name with instance ID
            for expert in enabled_experts:
                desc = expert.user_description or f"Expert {expert.id}"
                display_name = f"{expert.expert} - {desc} (ID: {expert.id})"
                options.append(display_name)
            
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
            # Format is "ExpertType - Description (ID: X)"
            selected_text = selected_value
            expert_id = int(selected_text.split('ID: ')[-1].rstrip(')'))
            
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
    
    def _execute_process_recommendations(self, expert_id: int, days: int, config_dialog):
        """Execute the recommendation processing with the specified days lookback."""
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
            
            processing_dialog.open()
            
            try:
                created_orders = trade_manager.process_expert_recommendations_after_analysis(expert_id, lookback_days=days)
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
            # Format is "ExpertType - Description (ID: X)"
            selected_text = selected_value
            expert_id = int(selected_text.split('ID: ')[-1].rstrip(')'))
            
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
                logger.error(f"Error in risk management for expert {expert_id}: {e}", exc_info=True)
                ui.notify(f'Error in risk management: {str(e)}', type='negative')
                
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
            expert_instance = get_instance(ExpertInstance, expert_id)
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
                            
                            # Get current price from account interface
                            current_price = account.get_instrument_current_price(order.symbol)
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
            expert_instance = get_instance(ExpertInstance, expert_id)
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
        ('recommendations', 'Order Recommendations')
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
    
    # Setup HTML5 history navigation for tabs using timer for async compatibility
    def setup_tab_navigation():
        ui.run_javascript('''
            (function() {
                let isPopstateNavigation = false;
                
                // Map display labels to tab names
                const labelToName = {
                    'Job Monitoring': 'monitoring',
                    'Manual Analysis': 'manual',
                    'Scheduled Jobs': 'scheduled',
                    'Order Recommendations': 'recommendations'
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
                    const tabs = document.querySelectorAll('.q-tab');
                    tabs.forEach(tab => {
                        if (tab.getAttribute('name') === hash) {
                            tab.click();
                        }
                    });
                } else if (!hash) {
                    // Set initial hash if none exists
                    history.replaceState({tab: 'monitoring'}, '', '#monitoring');
                }
                
                setupTabClickHandlers();
            })();
        ''')
    
    # Use timer to run JavaScript after page is loaded (increased delay to ensure tabs are rendered)
    ui.timer(0.5, setup_tab_navigation, once=True)
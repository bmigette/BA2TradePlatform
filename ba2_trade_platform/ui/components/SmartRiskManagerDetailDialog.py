"""
Smart Risk Manager Detail Dialog Component

A full-screen dialog that displays Smart Risk Manager job details without navigating away
from the current page, preserving filters and state.
"""

from nicegui import ui
from typing import Optional, Callable
import json
from datetime import datetime, timezone

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition, Transaction
from ...logger import logger


def _format_duration(seconds: Optional[int]) -> str:
    """Format duration in seconds to human-readable string."""
    if seconds is None:
        return "N/A"
    
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def _get_status_color(status: str) -> str:
    """Get color class for status badge."""
    if status == "COMPLETED":
        return "positive"
    elif status == "RUNNING":
        return "warning"
    elif status == "FAILED":
        return "negative"
    else:
        return "grey"


class SmartRiskManagerDetailDialog:
    """
    A full-screen dialog component for displaying Smart Risk Manager job details.
    
    Usage:
        dialog = SmartRiskManagerDetailDialog()
        dialog.open(job_id)
    """
    
    def __init__(self, on_close: Optional[Callable] = None):
        """
        Initialize the dialog.
        
        Args:
            on_close: Optional callback when dialog is closed
        """
        self.on_close_callback = on_close
        self.dialog = None
        self.content_container = None
        self.current_job_id = None
        self._create_dialog()
    
    def _create_dialog(self):
        """Create the dialog structure."""
        self.dialog = ui.dialog().props('full-width full-height maximized transition-show="slide-up" transition-hide="slide-down"')
        
        with self.dialog:
            with ui.card().classes('w-full h-full flex flex-col').style('max-width: 100%; max-height: 100vh; background: #1a1f2e;'):
                # Header with close button
                with ui.row().classes('w-full items-center justify-between p-4 border-b').style('border-color: rgba(160,174,192,0.2);'):
                    self.header_label = ui.label('Smart Risk Manager Job Details').classes('text-xl font-bold').style('color: #e2e8f0;')
                    with ui.row().classes('gap-2'):
                        # Open in new tab button
                        self.open_url_btn = ui.button(icon='open_in_new', on_click=self._open_in_new_tab).props('flat round').tooltip('Open in new tab')
                        # Close button
                        ui.button(icon='close', on_click=self.close).props('flat round').tooltip('Close')
                
                # Scrollable content area
                with ui.scroll_area().classes('flex-grow w-full'):
                    self.content_container = ui.column().classes('w-full p-4')
    
    def _open_in_new_tab(self):
        """Open the current job in a new browser tab."""
        if self.current_job_id:
            ui.run_javascript(f"window.open('/smartriskmanagerdetail/{self.current_job_id}', '_blank')")
    
    def open(self, job_id: int):
        """
        Open the dialog and load the job details.
        
        Args:
            job_id: The ID of the SmartRiskManagerJob to display
        """
        self.current_job_id = job_id
        self._load_content(job_id)
        self.dialog.open()
    
    def close(self):
        """Close the dialog."""
        self.dialog.close()
        if self.on_close_callback:
            self.on_close_callback()
    
    def _load_content(self, job_id: int):
        """Load and render the job content."""
        self.content_container.clear()
        
        with self.content_container:
            try:
                self._render_job_detail(job_id)
            except Exception as e:
                logger.error(f"Error loading Smart Risk Manager job detail {job_id}: {e}", exc_info=True)
                with ui.card().classes('w-full p-8 text-center').style('background-color: #1e2a3a;'):
                    ui.label(f'Error loading job: {str(e)}').classes('text-h5').style('color: #ff6b6b;')
    
    def _render_job_detail(self, job_id: int):
        """Render the full job detail content."""
        # Load the Smart Risk Manager job
        job = get_instance(SmartRiskManagerJob, job_id)
        if not job:
            with ui.card().classes('w-full p-8 text-center').style('background-color: #1e2a3a;'):
                ui.label(f'Smart Risk Manager Job {job_id} not found').classes('text-h5').style('color: #ff6b6b;')
            return
        
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, job.expert_instance_id)
        expert_name = expert_instance.expert if expert_instance else "Unknown"
        if expert_instance and expert_instance.alias:
            expert_name = expert_instance.alias
        
        # Load the account
        account = get_instance(AccountDefinition, job.account_id)
        account_name = account.name if account else "Unknown"
        
        # Update header
        self.header_label.set_text(f'Smart Risk Manager Job #{job_id}')
        
        # Page header info
        with ui.row().classes('w-full items-center justify-between mb-4'):
            with ui.column().classes('gap-1'):
                ui.label(f'Expert: {expert_name} | Account: {account_name}').classes('text-subtitle2').style('color: #a0aec0;')
            ui.badge(job.status, color=_get_status_color(job.status)).props('size="lg"')
        
        # Job metadata card
        with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
            ui.label('Job Information').classes('text-h6 mb-2').style('color: #e2e8f0;')
            ui.separator()
            
            with ui.grid(columns=2).classes('w-full gap-4 mt-2'):
                # Left column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('event').style('color: #a0aec0;')
                        # Convert UTC to local time for display
                        if job.run_date:
                            if job.run_date.tzinfo is None:
                                utc_time = job.run_date.replace(tzinfo=timezone.utc)
                            else:
                                utc_time = job.run_date
                            local_time = utc_time.astimezone()
                            run_date_str = local_time.strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            run_date_str = 'N/A'
                        ui.label(f"Run Date: {run_date_str}").classes('text-sm').style('color: #e2e8f0;')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('schedule').style('color: #a0aec0;')
                        ui.label(f"Duration: {_format_duration(job.duration_seconds)}").classes('text-sm').style('color: #e2e8f0;')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('psychology').style('color: #a0aec0;')
                        ui.label(f"Model: {job.model_used or 'N/A'}").classes('text-sm').style('color: #e2e8f0;')
                
                # Right column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('loop').style('color: #a0aec0;')
                        ui.label(f"Iterations: {job.iteration_count or 0}").classes('text-sm').style('color: #e2e8f0;')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('trending_up').style('color: #a0aec0;')
                        ui.label(f"Actions Taken: {job.actions_taken_count or 0}").classes('text-sm').style('color: #e2e8f0;')
        
        # Portfolio snapshot card (if available)
        if job.initial_portfolio_equity is not None or job.final_portfolio_equity is not None or job.initial_available_balance is not None:
            with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                ui.label('Portfolio Snapshot').classes('text-h6 mb-2').style('color: #e2e8f0;')
                ui.separator()
                
                # Initial Equity at Run
                with ui.row().classes('w-full gap-4 mt-2 mb-3'):
                    with ui.column().classes('text-center flex-1'):
                        ui.label('Initial Portfolio Equity at Run').classes('text-sm').style('color: #a0aec0;')
                        equity_text = f"${job.initial_portfolio_equity:,.2f}" if job.initial_portfolio_equity else "N/A"
                        ui.label(equity_text).classes('text-h6').style('color: #e2e8f0;')
                
                ui.separator()
                
                # Available Balance Before/After
                with ui.grid(columns=3).classes('w-full gap-4 mt-3'):
                    # Initial balance
                    with ui.column().classes('text-center'):
                        ui.label('Available Balance Before').classes('text-sm').style('color: #a0aec0;')
                        balance_before = f"${job.initial_available_balance:,.2f}" if job.initial_available_balance else "N/A"
                        ui.label(balance_before).classes('text-h6').style('color: #e2e8f0;')
                    
                    # Final balance
                    with ui.column().classes('text-center'):
                        ui.label('Available Balance After').classes('text-sm').style('color: #a0aec0;')
                        balance_after = f"${job.final_available_balance:,.2f}" if job.final_available_balance else "N/A"
                        ui.label(balance_after).classes('text-h6').style('color: #e2e8f0;')
                    
                    # Change
                    with ui.column().classes('text-center'):
                        ui.label('Balance Change').classes('text-sm').style('color: #a0aec0;')
                        if job.initial_available_balance is not None and job.final_available_balance is not None:
                            change = job.final_available_balance - job.initial_available_balance
                            change_pct = (change / job.initial_available_balance * 100) if job.initial_available_balance > 0 else 0
                            color = '#00d4aa' if change >= 0 else '#ff6b6b'
                            sign = '+' if change >= 0 else ''
                            ui.label(f"{sign}${change:,.2f} ({sign}{change_pct:.2f}%)").classes('text-h6').style(f'color: {color};')
                        else:
                            ui.label("N/A").classes('text-h6').style('color: #e2e8f0;')
        
        # User instructions (if available)
        if job.user_instructions:
            with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                ui.label('User Instructions').classes('text-h6 mb-2').style('color: #e2e8f0;')
                ui.separator()
                ui.label(job.user_instructions).classes('text-sm whitespace-pre-wrap mt-2').style('color: #e2e8f0;')
        
        # Actions summary card
        if job.actions_summary:
            with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                ui.label('Actions Summary').classes('text-h6 mb-2').style('color: #e2e8f0;')
                ui.separator()
                ui.label(job.actions_summary).classes('text-sm whitespace-pre-wrap mt-2').style('color: #e2e8f0;')
        
        # Detailed Actions Log
        if job.graph_state:
            try:
                state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                actions_log = state.get("actions_log", [])
                
                if actions_log:
                    with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                        ui.label(f'Detailed Actions Log ({len(actions_log)} actions)').classes('text-h6 mb-2').style('color: #e2e8f0;')
                        ui.separator()
                        
                        # Display each action with full details
                        for i, action in enumerate(actions_log):
                            # Extract symbol from arguments or result
                            symbol = None
                            if action.get('arguments'):
                                symbol = action['arguments'].get('symbol')
                            if not symbol and action.get('result'):
                                symbol = action['result'].get('symbol')
                            
                            # If still no symbol, try to look it up from transaction_id
                            if not symbol:
                                transaction_id = None
                                if action.get('arguments'):
                                    transaction_id = action['arguments'].get('transaction_id')
                                if not transaction_id and action.get('result'):
                                    transaction_id = action['result'].get('transaction_id')
                                
                                if transaction_id:
                                    try:
                                        transaction = get_instance(Transaction, transaction_id)
                                        if transaction:
                                            symbol = transaction.symbol
                                    except Exception:
                                        pass
                            
                            action_name = action.get('action_type') or action.get('action', 'Unknown')
                            # Handle both 'success' boolean and 'status' string formats
                            if 'success' in action:
                                status = 'success' if action['success'] else 'error'
                            else:
                                status = action.get('status', 'unknown')
                            status_color = '#00d4aa' if status == 'success' else '#ff6b6b' if status == 'error' else '#ffa94d'
                            
                            with ui.expansion(f'Action {i+1}: {action_name}' + (f' ({symbol})' if symbol else ''), icon='play_arrow').classes('w-full mb-2').style('color: #e2e8f0;'):
                                with ui.column().classes('gap-2 mt-2'):
                                    # Status
                                    with ui.row().classes('items-center gap-2'):
                                        ui.label('Status:').classes('text-sm font-bold').style('color: #a0aec0;')
                                        ui.badge(status, color='positive' if status == 'success' else 'negative' if status == 'error' else 'warning')
                                    
                                    # Summary (if available)
                                    if action.get('summary'):
                                        ui.label('Summary:').classes('text-sm font-bold mt-2').style('color: #a0aec0;')
                                        ui.label(action['summary']).classes('text-sm whitespace-pre-wrap').style('color: #e2e8f0;')
                                    
                                    # Reasoning (support both 'reason' and 'reasoning' keys)
                                    reasoning = action.get('reason') or action.get('reasoning')
                                    if reasoning:
                                        ui.label('Reasoning:').classes('text-sm font-bold mt-2').style('color: #a0aec0;')
                                        ui.label(reasoning).classes('text-sm whitespace-pre-wrap').style('color: #e2e8f0;')
                                    
                                    # Arguments
                                    if action.get('arguments'):
                                        ui.label('Arguments:').classes('text-sm font-bold mt-2').style('color: #a0aec0;')
                                        with ui.column().classes('gap-1 mt-1'):
                                            for key, value in action['arguments'].items():
                                                ui.label(f"  • {key}: {value}").classes('text-sm').style('color: #e2e8f0;')
                                    
                                    # Result
                                    if action.get('result'):
                                        result = action['result']
                                        ui.label('Result:').classes('text-sm font-bold mt-2').style('color: #a0aec0;')
                                        
                                        if result.get('message'):
                                            ui.label(f"Message: {result['message']}").classes('text-sm').style('color: #e2e8f0;')
                                        
                                        # Show relevant result fields
                                        with ui.column().classes('gap-1 mt-1'):
                                            if result.get('symbol'):
                                                ui.label(f"  • Symbol: {result.get('symbol')}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('quantity'):
                                                ui.label(f"  • Quantity: {result.get('quantity')}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('direction'):
                                                ui.label(f"  • Direction: {result.get('direction')}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('entry_price'):
                                                ui.label(f"  • Entry Price: ${result.get('entry_price'):.2f}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('tp_price'):
                                                ui.label(f"  • Take Profit: ${result.get('tp_price'):.2f}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('sl_price'):
                                                ui.label(f"  • Stop Loss: ${result.get('sl_price'):.2f}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('transaction_id'):
                                                ui.label(f"  • Transaction ID: {result.get('transaction_id')}").classes('text-sm').style('color: #e2e8f0;')
                                            if result.get('order_id'):
                                                ui.label(f"  • Order ID: {result.get('order_id')}").classes('text-sm').style('color: #e2e8f0;')
                                    
                                    # Error (if present)
                                    if action.get('error'):
                                        ui.label('Error:').classes('text-sm font-bold mt-2').style('color: #ff6b6b;')
                                        ui.label(action['error']).classes('text-sm whitespace-pre-wrap').style('color: #ff6b6b;')
                
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                logger.debug(f"Could not extract actions_log from graph_state: {e}")
        
        # Extract research findings and final summary from graph_state
        research_findings = None
        final_summary = None
        if job.graph_state:
            try:
                state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                research_findings = state.get("research_findings")
                final_summary = state.get("final_summary")
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        
        # Research Node Analysis card
        if research_findings:
            with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('psychology', color='primary').props('size=md')
                    ui.label('Research Node Analysis').classes('text-h6').style('color: #e2e8f0;')
                ui.separator()
                ui.label(research_findings).classes('text-sm whitespace-pre-wrap mt-2').style('color: #e2e8f0;')
        
        # Final Summary card
        if final_summary:
            with ui.card().classes('w-full mb-4').style('background-color: #1e2a3a;'):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('summarize', color='primary').props('size=md')
                    ui.label('Final Summary').classes('text-h6').style('color: #e2e8f0;')
                ui.separator()
                ui.label(final_summary).classes('text-sm whitespace-pre-wrap mt-2').style('color: #e2e8f0;')
        
        # Graph state (collapsible JSON viewer) - now showing full technical details
        if job.graph_state:
            with ui.expansion('Graph State (Technical Details)', icon='account_tree').classes('w-full mb-4').style('color: #e2e8f0;'):
                try:
                    # Try to parse as JSON
                    state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                    ui.json_editor({'content': {'json': state}}).classes('w-full')
                except (json.JSONDecodeError, TypeError):
                    # Display as plain text if not valid JSON
                    ui.label(str(job.graph_state)).classes('text-sm whitespace-pre-wrap').style('color: #e2e8f0;')
        
        # Error message (if failed)
        if job.status == "FAILED" and job.error_message:
            with ui.card().classes('w-full mb-4').style('background-color: rgba(255, 107, 107, 0.1);'):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error', color='negative').props('size=md')
                    ui.label('Error Details').classes('text-h6').style('color: #ff6b6b;')
                ui.separator()
                ui.label(job.error_message).classes('text-sm whitespace-pre-wrap mt-2').style('color: #ff6b6b;')

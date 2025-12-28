"""
Smart Risk Manager Detail Dialog Component

A full-screen dialog that displays Smart Risk Manager job details without navigating away
from the current page, preserving filters and state.
"""

from nicegui import ui
from typing import Optional, Callable

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition
from ...logger import logger
from .smart_risk_manager_content import render_smart_risk_manager_content, get_status_color


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
            ui.badge(job.status, color=get_status_color(job.status)).props('size="lg"')
        
        # Render job content using shared component (with explicit dark colors for dialog)
        render_smart_risk_manager_content(job, use_explicit_dark_colors=True)

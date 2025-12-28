from nicegui import ui

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition
from ...logger import logger
from ..components.smart_risk_manager_content import render_smart_risk_manager_content, get_status_color


def content(job_id: int) -> None:
    """
    Smart Risk Manager Job Detail Page Content
    
    Displays detailed results for a specific SmartRiskManagerJob ID,
    including portfolio snapshots, actions taken, and execution details.
    
    Args:
        job_id: The ID of the SmartRiskManagerJob to display
    """
    
    try:
        # Load the Smart Risk Manager job
        job = get_instance(SmartRiskManagerJob, job_id)
        if not job:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Smart Risk Manager Job {job_id} not found').classes('text-h5 text-negative')
                ui.button('Back to Job Monitoring', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).classes('mt-4')
            return
        
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, job.expert_instance_id)
        expert_name = expert_instance.expert if expert_instance else "Unknown"
        if expert_instance and expert_instance.alias:
            expert_name = expert_instance.alias
        
        # Load the account
        account = get_instance(AccountDefinition, job.account_id)
        account_name = account.name if account else "Unknown"
        
        # Page header
        with ui.row().classes('w-full items-center justify-between mb-4'):
            with ui.column().classes('gap-1'):
                ui.label(f'Smart Risk Manager Job #{job_id}').classes('text-h4')
                ui.label(f'Expert: {expert_name} | Account: {account_name}').classes('text-subtitle2 text-grey-7')
            
            with ui.row().classes('gap-2'):
                # Status badge
                ui.badge(job.status, color=get_status_color(job.status)).props('size="lg"')
                
                # Back button
                ui.button('Back to Monitoring', icon='arrow_back', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).props('flat')
        
        # Render job content using shared component (with Quasar semantic classes)
        render_smart_risk_manager_content(job, use_explicit_dark_colors=False)
        
        # Consulted analyses section - Future enhancement
        # Market analyses consulted are tracked in graph_state under 'recent_analyses'
        # and can be cross-referenced with the detailed_outputs_cache
        with ui.card().classes('w-full mb-4'):
            ui.label('Consulted Market Analyses').classes('text-h6 mb-2')
            ui.separator()
            ui.label('Market analyses consulted during this session are tracked in the graph state.').classes('text-sm text-grey-6 mt-2 italic')
    
    except Exception as e:
        logger.error(f"Error loading Smart Risk Manager job detail for job_id {job_id}: {e}", exc_info=True)
        with ui.card().classes('w-full p-8 text-center'):
            ui.label(f'Error loading job details: {str(e)}').classes('text-h5 text-negative')
            ui.button('Back to Job Monitoring', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).classes('mt-4')

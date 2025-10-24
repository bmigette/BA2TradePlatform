from nicegui import ui
from typing import Optional
import json
from datetime import datetime

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition, MarketAnalysis
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
                ui.badge(job.status, color=_get_status_color(job.status)).props('size="lg"')
                
                # Back button
                ui.button('Back to Monitoring', icon='arrow_back', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).props('flat')
        
        # Job metadata card
        with ui.card().classes('w-full mb-4'):
            ui.label('Job Information').classes('text-h6 mb-2')
            ui.separator()
            
            with ui.grid(columns=2).classes('w-full gap-4 mt-2'):
                # Left column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('event').classes('text-grey-7')
                        ui.label(f"Run Date: {job.run_date.strftime('%Y-%m-%d %H:%M:%S') if job.run_date else 'N/A'}").classes('text-sm')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('schedule').classes('text-grey-7')
                        ui.label(f"Duration: {_format_duration(job.duration_seconds)}").classes('text-sm')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('psychology').classes('text-grey-7')
                        ui.label(f"Model: {job.model_used or 'N/A'}").classes('text-sm')
                
                # Right column
                with ui.column().classes('gap-2'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('loop').classes('text-grey-7')
                        ui.label(f"Iterations: {job.iteration_count or 0}").classes('text-sm')
                    
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('trending_up').classes('text-grey-7')
                        ui.label(f"Actions Taken: {job.actions_taken_count or 0}").classes('text-sm')
        
        # Portfolio snapshot card (if available)
        if job.initial_portfolio_equity is not None or job.final_portfolio_equity is not None:
            with ui.card().classes('w-full mb-4'):
                ui.label('Portfolio Snapshot').classes('text-h6 mb-2')
                ui.separator()
                
                with ui.grid(columns=3).classes('w-full gap-4 mt-2'):
                    # Initial value
                    with ui.column().classes('text-center'):
                        ui.label('Initial Portfolio Equity').classes('text-sm text-grey-7')
                        ui.label(f"${job.initial_portfolio_equity:,.2f}" if job.initial_portfolio_equity else "N/A").classes('text-h6')
                    
                    # Final value
                    with ui.column().classes('text-center'):
                        ui.label('Final Portfolio Equity').classes('text-sm text-grey-7')
                        ui.label(f"${job.final_portfolio_equity:,.2f}" if job.final_portfolio_equity else "N/A").classes('text-h6')
                    
                    # Change
                    with ui.column().classes('text-center'):
                        ui.label('Change').classes('text-sm text-grey-7')
                        if job.initial_portfolio_equity and job.final_portfolio_equity:
                            change = job.final_portfolio_equity - job.initial_portfolio_equity
                            change_pct = (change / job.initial_portfolio_equity * 100) if job.initial_portfolio_equity > 0 else 0
                            color = 'positive' if change >= 0 else 'negative'
                            sign = '+' if change >= 0 else ''
                            ui.label(f"{sign}${change:,.2f} ({sign}{change_pct:.2f}%)").classes(f'text-h6 text-{color}')
                        else:
                            ui.label("N/A").classes('text-h6')
        
        # User instructions (if available)
        if job.user_instructions:
            with ui.card().classes('w-full mb-4'):
                ui.label('User Instructions').classes('text-h6 mb-2')
                ui.separator()
                ui.label(job.user_instructions).classes('text-sm whitespace-pre-wrap mt-2')
        
        # Actions summary card
        if job.actions_summary:
            with ui.card().classes('w-full mb-4'):
                ui.label('Actions Summary').classes('text-h6 mb-2')
                ui.separator()
                ui.label(job.actions_summary).classes('text-sm whitespace-pre-wrap mt-2')
        
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
            with ui.card().classes('w-full mb-4'):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('psychology', color='primary').props('size=md')
                    ui.label('Research Node Analysis').classes('text-h6')
                ui.separator()
                ui.label(research_findings).classes('text-sm whitespace-pre-wrap mt-2')
        
        # Final Summary card
        if final_summary:
            with ui.card().classes('w-full mb-4'):
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.icon('summarize', color='primary').props('size=md')
                    ui.label('Final Summary').classes('text-h6')
                ui.separator()
                ui.label(final_summary).classes('text-sm whitespace-pre-wrap mt-2')
        
        # Graph state (collapsible JSON viewer) - now showing full technical details
        if job.graph_state:
            with ui.expansion('Graph State (Technical Details)', icon='account_tree').classes('w-full mb-4'):
                try:
                    # Try to parse as JSON
                    state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                    ui.json_editor({'content': {'json': state}}).classes('w-full')
                except (json.JSONDecodeError, TypeError):
                    # Display as plain text if not valid JSON
                    ui.label(str(job.graph_state)).classes('text-sm whitespace-pre-wrap')
        
        # Error message (if failed)
        if job.status == "FAILED" and job.error_message:
            with ui.card().classes('w-full mb-4 bg-negative-1'):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error', color='negative').props('size=md')
                    ui.label('Error Details').classes('text-h6 text-negative')
                ui.separator()
                ui.label(job.error_message).classes('text-sm whitespace-pre-wrap mt-2 text-negative')
        
        # Consulted analyses section
        # Note: This requires SmartRiskManagerJobAnalysis junction table to be populated
        # For now, show placeholder
        with ui.card().classes('w-full mb-4'):
            ui.label('Consulted Market Analyses').classes('text-h6 mb-2')
            ui.separator()
            ui.label('Analysis linkage feature coming soon...').classes('text-sm text-grey-6 mt-2 italic')
            # TODO: Query SmartRiskManagerJobAnalysis table and display linked analyses
    
    except Exception as e:
        logger.error(f"Error loading Smart Risk Manager job detail for job_id {job_id}: {e}", exc_info=True)
        with ui.card().classes('w-full p-8 text-center'):
            ui.label(f'Error loading job details: {str(e)}').classes('text-h5 text-negative')
            ui.button('Back to Job Monitoring', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).classes('mt-4')

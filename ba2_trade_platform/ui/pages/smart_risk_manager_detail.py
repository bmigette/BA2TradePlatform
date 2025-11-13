from nicegui import ui
from typing import Optional
import json
from datetime import datetime

from ...core.db import get_instance
from ...core.models import SmartRiskManagerJob, ExpertInstance, AccountDefinition, MarketAnalysis, Transaction
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
                        # Convert UTC to local time for display
                        if job.run_date:
                            # Ensure the datetime is timezone-aware (treat as UTC if naive)
                            if job.run_date.tzinfo is None:
                                from datetime import timezone
                                utc_time = job.run_date.replace(tzinfo=timezone.utc)
                            else:
                                utc_time = job.run_date
                            local_time = utc_time.astimezone()
                            run_date_str = local_time.strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            run_date_str = 'N/A'
                        ui.label(f"Run Date: {run_date_str}").classes('text-sm')
                    
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
        if job.initial_portfolio_equity is not None or job.final_portfolio_equity is not None or job.initial_available_balance is not None:
            with ui.card().classes('w-full mb-4'):
                ui.label('Portfolio Snapshot').classes('text-h6 mb-2')
                ui.separator()
                
                # Initial Equity at Run
                with ui.row().classes('w-full gap-4 mt-2 mb-3'):
                    with ui.column().classes('text-center flex-1'):
                        ui.label('Initial Portfolio Equity at Run').classes('text-sm text-grey-7')
                        ui.label(f"${job.initial_portfolio_equity:,.2f}" if job.initial_portfolio_equity else "N/A").classes('text-h6')
                
                ui.separator()
                
                # Available Balance Before/After
                with ui.grid(columns=3).classes('w-full gap-4 mt-3'):
                    # Initial balance
                    with ui.column().classes('text-center'):
                        ui.label('Available Balance Before').classes('text-sm text-grey-7')
                        ui.label(f"${job.initial_available_balance:,.2f}" if job.initial_available_balance else "N/A").classes('text-h6')
                    
                    # Final balance
                    with ui.column().classes('text-center'):
                        ui.label('Available Balance After').classes('text-sm text-grey-7')
                        ui.label(f"${job.final_available_balance:,.2f}" if job.final_available_balance else "N/A").classes('text-h6')
                    
                    # Change
                    with ui.column().classes('text-center'):
                        ui.label('Balance Change').classes('text-sm text-grey-7')
                        if job.initial_available_balance is not None and job.final_available_balance is not None:
                            change = job.final_available_balance - job.initial_available_balance
                            change_pct = (change / job.initial_available_balance * 100) if job.initial_available_balance > 0 else 0
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
        
        # Detailed Actions Log
        if job.graph_state:
            try:
                state = json.loads(job.graph_state) if isinstance(job.graph_state, str) else job.graph_state
                actions_log = state.get("actions_log", [])
                
                if actions_log:
                    with ui.card().classes('w-full mb-4'):
                        ui.label(f'Detailed Actions Log ({len(actions_log)} actions)').classes('text-h6 mb-2')
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
                                    except Exception as e:
                                        logger.debug(f"Could not lookup symbol for transaction {transaction_id}: {e}")
                            
                            # Build expansion title with symbol
                            title_parts = [f"{i+1}.", action.get('action_type', 'Unknown')]
                            if symbol:
                                title_parts.append(f"({symbol})")
                            title_parts.append('-')
                            title_parts.append('✓ Success' if action.get('success') else '✗ Failed')
                            
                            with ui.expansion(
                                ' '.join(title_parts),
                                icon='check_circle' if action.get('success') else 'error'
                            ).classes('w-full mt-2'):
                                with ui.column().classes('gap-2 p-2'):
                                    # Timestamp and iteration
                                    with ui.row().classes('gap-4'):
                                        ui.label(f"Iteration: {action.get('iteration', 'N/A')}").classes('text-sm text-grey-7')
                                        if action.get('timestamp'):
                                            try:
                                                ts = datetime.fromisoformat(action['timestamp'].replace('Z', '+00:00'))
                                                local_ts = ts.astimezone()
                                                ui.label(f"Time: {local_ts.strftime('%Y-%m-%d %H:%M:%S')}").classes('text-sm text-grey-7')
                                            except:
                                                ui.label(f"Time: {action.get('timestamp')}").classes('text-sm text-grey-7')
                                    
                                    # Confidence and source
                                    with ui.row().classes('gap-4'):
                                        if action.get('confidence') is not None:
                                            conf = action['confidence']
                                            ui.label(f"Confidence: {conf:.1f}%").classes('text-sm text-grey-7')
                                        if action.get('source'):
                                            ui.label(f"Source: {action['source']}").classes('text-sm text-grey-7')
                                    
                                    # Reason
                                    if action.get('reason'):
                                        ui.label('Reason:').classes('text-sm font-bold mt-2')
                                        ui.label(action['reason']).classes('text-sm whitespace-pre-wrap')
                                    
                                    # Arguments
                                    if action.get('arguments'):
                                        ui.label('Arguments:').classes('text-sm font-bold mt-2')
                                        with ui.column().classes('gap-1'):
                                            for key, value in action['arguments'].items():
                                                ui.label(f"  • {key}: {value}").classes('text-sm')
                                    
                                    # Result details
                                    result = action.get('result', {})
                                    if result:
                                        ui.label('Result:').classes('text-sm font-bold mt-2')
                                        
                                        # Success status with color
                                        success = result.get('success', False)
                                        status_color = 'positive' if success else 'negative'
                                        ui.label(f"Status: {'Success' if success else 'Failed'}").classes(f'text-sm text-{status_color}')
                                        
                                        # Message
                                        if result.get('message'):
                                            ui.label(f"Message: {result['message']}").classes('text-sm whitespace-pre-wrap')
                                        
                                        # Additional details from open_new_position
                                        if result.get('symbol'):
                                            with ui.column().classes('gap-1 mt-1'):
                                                ui.label(f"  • Symbol: {result.get('symbol')}").classes('text-sm')
                                                if result.get('quantity'):
                                                    ui.label(f"  • Quantity: {result.get('quantity')}").classes('text-sm')
                                                if result.get('direction'):
                                                    ui.label(f"  • Direction: {result.get('direction')}").classes('text-sm')
                                                if result.get('entry_price'):
                                                    ui.label(f"  • Entry Price: ${result.get('entry_price'):.2f}").classes('text-sm')
                                                if result.get('tp_price'):
                                                    ui.label(f"  • Take Profit: ${result.get('tp_price'):.2f}").classes('text-sm')
                                                if result.get('sl_price'):
                                                    ui.label(f"  • Stop Loss: ${result.get('sl_price'):.2f}").classes('text-sm')
                                                if result.get('transaction_id'):
                                                    ui.label(f"  • Transaction ID: {result.get('transaction_id')}").classes('text-sm')
                                                if result.get('order_id'):
                                                    ui.label(f"  • Order ID: {result.get('order_id')}").classes('text-sm')
                                        
                                        # Additional details from other actions
                                        if result.get('old_quantity') is not None:
                                            ui.label(f"  • Old Quantity: {result.get('old_quantity')}").classes('text-sm')
                                        if result.get('new_quantity') is not None:
                                            ui.label(f"  • New Quantity: {result.get('new_quantity')}").classes('text-sm')
                                        if result.get('old_sl_price') is not None:
                                            ui.label(f"  • Old Stop Loss: ${result.get('old_sl_price'):.2f}").classes('text-sm')
                                        if result.get('new_sl_price') is not None:
                                            ui.label(f"  • New Stop Loss: ${result.get('new_sl_price'):.2f}").classes('text-sm')
                                        if result.get('old_tp_price') is not None:
                                            ui.label(f"  • Old Take Profit: ${result.get('old_tp_price'):.2f}").classes('text-sm')
                                        if result.get('new_tp_price') is not None:
                                            ui.label(f"  • New Take Profit: ${result.get('new_tp_price'):.2f}").classes('text-sm')
                                    
                                    # Error (if present)
                                    if action.get('error'):
                                        ui.label('Error:').classes('text-sm font-bold mt-2 text-negative')
                                        ui.label(action['error']).classes('text-sm text-negative whitespace-pre-wrap')
            
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

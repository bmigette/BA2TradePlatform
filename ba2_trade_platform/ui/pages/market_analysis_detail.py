from nicegui import ui
from typing import Optional, Dict, Any, List
import json
from datetime import datetime

from ...core.db import get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput, Instrument
from ...core.types import MarketAnalysisStatus
from ...logger import logger
from sqlmodel import select


def _get_instrument_details(symbol: str) -> Optional[Instrument]:
    """Get instrument details by symbol."""
    try:
        session = get_db()
        statement = select(Instrument).where(Instrument.name == symbol)
        instrument = session.exec(statement).first()
        session.close()
        return instrument
    except Exception as e:
        logger.error(f"Error loading instrument details for {symbol}: {e}")
        return None


def content(analysis_id: int) -> None:
    """
    Market Analysis Detail Page Content
    
    Displays detailed analysis results for a specific MarketAnalysis ID,
    including both tool outputs and LLM outputs organized by agent in sub-tabs.
    
    Args:
        analysis_id: The ID of the MarketAnalysis to display
    """
    try:
        # Load the market analysis
        market_analysis = get_instance(MarketAnalysis, analysis_id)
        if not market_analysis:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Market Analysis {analysis_id} not found').classes('text-h5 text-negative')
                ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis')).classes('mt-4')
            return
        
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, market_analysis.source_expert_instance_id)
        if not expert_instance:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Expert Instance {market_analysis.source_expert_instance_id} not found').classes('text-h5 text-negative')
                ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis')).classes('mt-4')
            return
        
        # Get expert instance with appropriate class
        from ...core.utils import get_expert_instance_from_id
        expert = get_expert_instance_from_id(expert_instance.id)
        if not expert:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Expert class {expert_instance.expert} not found').classes('text-h5 text-negative')
                ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis')).classes('mt-4')
            return
        
        # Load analysis outputs
        session = get_db()
        statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis_id).order_by(AnalysisOutput.created_at)
        analysis_outputs = session.exec(statement).all()
        session.close()
        
        # Load instrument details
        instrument = _get_instrument_details(market_analysis.symbol)
        
        # Header section
        with ui.card().classes('w-full mb-4'):
            with ui.row().classes('w-full items-center justify-between'):
                with ui.column():
                    ui.label(f'Market Analysis Detail - {market_analysis.symbol}').classes('text-h4')
                    
                    # Instrument details if available
                    if instrument:
                        if instrument.company_name:
                            ui.label(f'Company: {instrument.company_name}').classes('text-subtitle1 text-grey-8')
                        
                        # Display categories (sectors)
                        if instrument.categories:
                            categories_text = ', '.join(instrument.categories)
                            ui.label(f'Sector: {categories_text}').classes('text-subtitle2 text-grey-7')
                        
                        # Display labels
                        if instrument.labels:
                            labels_text = ', '.join(instrument.labels)
                            ui.label(f'Labels: {labels_text}').classes('text-subtitle2 text-grey-7')
                    
                    # Analysis details
                    ui.label(f'Expert: {expert_instance.expert} (ID: {expert_instance.id})').classes('text-subtitle1 text-grey-7 mt-2')
                    ui.label(f'Status: {market_analysis.status.value if market_analysis.status else "Unknown"}').classes('text-subtitle2')
                    ui.label(f'Created: {market_analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if market_analysis.created_at else "Unknown"}').classes('text-subtitle2')
                
                ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis'), icon='arrow_back')
        
        # Handle different states based on analysis status
        if market_analysis.status == MarketAnalysisStatus.PENDING:
            _render_pending_state(market_analysis)
            return
        elif market_analysis.status == MarketAnalysisStatus.CANCELLED:
            _render_cancelled_state(market_analysis)
            return
        elif market_analysis.status in [MarketAnalysisStatus.FAILED]:
            _render_error_state(market_analysis)
            return
        
        # Check for failed analysis even if status is not ERROR
        has_error = _check_for_analysis_errors(market_analysis)
        if has_error:
            _render_error_banner(market_analysis)
        
        # Render the expert-specific analysis using the expert's render_market_analysis method
        _render_expert_analysis(market_analysis, expert)
        
    except Exception as e:
        logger.error(f"Error loading market analysis detail {analysis_id}: {e}", exc_info=True)
        with ui.card().classes('w-full p-8 text-center'):
            ui.label(f'Error loading analysis: {str(e)}').classes('text-h5 text-negative')
            ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis')).classes('mt-4')


def _render_pending_state(market_analysis: MarketAnalysis) -> None:
    """Render UI for pending analysis."""
    with ui.card().classes('w-full p-8 text-center'):
        ui.spinner(size='lg').classes('mb-4')
        ui.label('Analysis is pending...').classes('text-h5')
        ui.label('Please check back in a few minutes.').classes('text-grey-7')
        
        # Auto-refresh for pending analyses
        ui.timer(10.0, lambda: ui.navigate.reload())


def _render_cancelled_state(market_analysis: MarketAnalysis) -> None:
    """Render UI for cancelled analysis."""
    with ui.card().classes('w-full p-8 text-center'):
        ui.icon('cancel', size='3rem', color='orange').classes('mb-4')
        ui.label('Analysis was cancelled').classes('text-h5')
        ui.label(f'Cancelled on: {market_analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if market_analysis.created_at else "Unknown"}').classes('text-grey-7')


def _render_error_state(market_analysis: MarketAnalysis) -> None:
    """Render UI for error analysis."""
    with ui.card().classes('w-full p-8 text-center'):
        ui.icon('error', size='3rem', color='negative').classes('mb-4')
        ui.label('Analysis encountered an error').classes('text-h5 text-negative')
        ui.label(f'Error time: {market_analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if market_analysis.created_at else "Unknown"}').classes('text-grey-7')
        
        # Show error details if available in state
        error_message = _extract_error_message(market_analysis.state)
        if error_message:
            with ui.card().classes('w-full max-w-4xl mt-4 bg-red-50 border-l-4 border-red-500'):
                with ui.row().classes('items-start p-4'):
                    ui.icon('error_outline', color='negative').classes('mt-1 mr-3')
                    with ui.column().classes('flex-1'):
                        ui.label('Error Details:').classes('font-medium text-red-800 mb-2')
                        with ui.element('pre').classes('bg-red-100 p-3 rounded text-sm overflow-auto max-h-48 whitespace-pre-wrap font-mono text-red-900'):
                            ui.label(error_message)


def _check_for_analysis_errors(market_analysis: MarketAnalysis) -> bool:
    """Check if the analysis has errors even if status is not ERROR."""
    if not market_analysis.state or not isinstance(market_analysis.state, dict):
        return False
    
    # Check for various error indicators in the state
    error_keys = ['error', 'exception', 'failure', 'failed']
    for key in error_keys:
        if key in market_analysis.state and market_analysis.state[key]:
            return True
    
    # Check nested state structures for errors
    trading_agent_state = market_analysis.state.get('trading_agent_graph', {})
    if isinstance(trading_agent_state, dict):
        for agent_state in trading_agent_state.values():
            if isinstance(agent_state, dict):
                for key in error_keys:
                    if key in agent_state and agent_state[key]:
                        return True
    
    return False


def _extract_error_message(state: Optional[Dict]) -> Optional[str]:
    """Extract error message from analysis state."""
    if not state or not isinstance(state, dict):
        return None
    
    # Look for direct error messages
    error_keys = ['error', 'exception', 'failure', 'failed']
    for key in error_keys:
        if key in state and state[key]:
            error_value = state[key]
            if isinstance(error_value, str):
                return error_value
            elif isinstance(error_value, dict):
                # Extract message from error object
                if 'message' in error_value:
                    return error_value['message']
                elif 'error' in error_value:
                    return str(error_value['error'])
                else:
                    return str(error_value)
            else:
                return str(error_value)
    
    # Look in nested trading agent state
    trading_agent_state = state.get('trading_agent_graph', {})
    if isinstance(trading_agent_state, dict):
        for agent_name, agent_state in trading_agent_state.items():
            if isinstance(agent_state, dict):
                for key in error_keys:
                    if key in agent_state and agent_state[key]:
                        error_value = agent_state[key]
                        if isinstance(error_value, str):
                            return f"[{agent_name}] {error_value}"
                        else:
                            return f"[{agent_name}] {str(error_value)}"
    
    return None


def _render_error_banner(market_analysis: MarketAnalysis) -> None:
    """Render an error banner for failed analyses."""
    error_message = _extract_error_message(market_analysis.state)
    
    with ui.card().classes('w-full mb-4 bg-red-50 border-l-4 border-red-500'):
        with ui.row().classes('items-start p-4'):
            ui.icon('error_outline', color='negative', size='lg').classes('mt-1 mr-3')
            with ui.column().classes('flex-1'):
                ui.label('Analysis Failed').classes('text-h6 font-medium text-red-800 mb-2')
                if error_message:
                    ui.label('Error Details:').classes('font-medium text-red-700 mb-2')
                    with ui.element('pre').classes('bg-red-100 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono text-red-900 border'):
                        ui.label(error_message)
                else:
                    ui.label('The analysis encountered an error during execution.').classes('text-red-700')


def _render_expert_analysis(market_analysis: MarketAnalysis, expert) -> None:
    """Render the expert-specific analysis by delegating to the expert's render_market_analysis method."""
    try:
        # Call the expert's render_market_analysis method
        # This method now handles the UI rendering directly using NiceGUI components
        expert.render_market_analysis(market_analysis)
        
    except Exception as e:
        logger.error(f"Error rendering expert analysis: {e}", exc_info=True)
        with ui.card().classes('w-full'):
            ui.label('Error rendering expert analysis').classes('text-h5 text-negative')
            ui.label(str(e)).classes('text-grey-7')


# All complex rendering logic has been moved to the expert's render_market_analysis method
# The page is now generic and delegates expert-specific rendering to the expert implementation
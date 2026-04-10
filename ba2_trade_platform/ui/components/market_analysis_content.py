"""
Market Analysis Content Component

Shared rendering logic for Market Analysis details.
Used by both the detail dialog and the detail page to avoid code duplication.
"""

from nicegui import ui, run
from typing import Optional, Dict
from datetime import timezone

from ...core.db import get_instance, get_db
from ...core.models import MarketAnalysis, Instrument
from ...core.MarketAnalysisPDFExport import export_market_analysis_pdf
from ...logger import logger
from sqlmodel import select


def check_for_analysis_errors(market_analysis: MarketAnalysis) -> bool:
    """Check if the analysis has errors even if status is not ERROR."""
    if not market_analysis.state or not isinstance(market_analysis.state, dict):
        return False

    error_keys = ['error', 'exception', 'failure', 'failed']
    for key in error_keys:
        if key in market_analysis.state and market_analysis.state[key]:
            return True

    trading_agent_state = market_analysis.state.get('trading_agent_graph', {})
    if isinstance(trading_agent_state, dict):
        for agent_state in trading_agent_state.values():
            if isinstance(agent_state, dict):
                for key in error_keys:
                    if key in agent_state and agent_state[key]:
                        return True

    return False


def extract_error_message(state: Optional[Dict]) -> Optional[str]:
    """Extract error message from analysis state dict."""
    if not state or not isinstance(state, dict):
        return None

    error_keys = ['error', 'exception', 'failure', 'failed']
    for key in error_keys:
        if key in state and state[key]:
            error_value = state[key]
            if isinstance(error_value, str):
                return error_value
            elif isinstance(error_value, dict):
                if 'message' in error_value:
                    return error_value['message']
                elif 'error' in error_value:
                    return str(error_value['error'])
                else:
                    return str(error_value)
            else:
                return str(error_value)

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


def render_cancelled_state(market_analysis: MarketAnalysis, use_explicit_dark_colors: bool = False) -> None:
    """Render UI for cancelled analysis."""
    with ui.card().classes('w-full p-8 text-center'):
        ui.icon('cancel', size='3rem', color='orange').classes('mb-4')
        ui.label('Analysis was cancelled').classes('text-h5')
        created_str = market_analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if market_analysis.created_at else "Unknown"
        label = ui.label(f'Cancelled on: {created_str}')
        if use_explicit_dark_colors:
            label.style('color: #a0aec0;')
        else:
            label.classes('text-grey-7')


def render_error_banner(market_analysis: MarketAnalysis, use_explicit_dark_colors: bool = False) -> None:
    """Render an error banner for failed analyses."""
    error_message = extract_error_message(market_analysis.state)

    with ui.card().classes('w-full mb-4 alert-banner danger'):
        with ui.row().classes('items-start p-4'):
            ui.icon('error_outline', color='negative', size='lg').classes('mt-1 mr-3')
            with ui.column().classes('flex-1'):
                label = ui.label('Analysis Failed').classes('text-h6 font-medium mb-2')
                if use_explicit_dark_colors:
                    label.style('color: #ff6b6b;')
                else:
                    label.classes('text-[#ff6b6b]')
                if error_message:
                    if use_explicit_dark_colors:
                        with ui.element('pre').classes('bg-white/5 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono border border-white/10').style('color: #ff6b6b;'):
                            ui.label(error_message)
                    else:
                        ui.label('Error Details:').classes('font-medium text-[#ff6b6b] mb-2')
                        with ui.element('pre').classes('bg-white/5 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono text-[#ff6b6b] border border-white/10'):
                            ui.label(error_message)
                else:
                    error_label = ui.label('The analysis encountered an error during execution.')
                    if use_explicit_dark_colors:
                        error_label.style('color: #ff6b6b;')
                    else:
                        error_label.classes('text-[#ff6b6b]')


def render_expert_analysis(market_analysis: MarketAnalysis, expert, use_explicit_dark_colors: bool = False) -> None:
    """Render the expert-specific analysis by delegating to the expert's render_market_analysis method."""
    try:
        expert.render_market_analysis(market_analysis)
    except Exception as e:
        logger.error(f"Error rendering expert analysis: {e}", exc_info=True)
        with ui.card().classes('w-full'):
            label = ui.label('Error rendering expert analysis').classes('text-h5')
            err_label = ui.label(str(e))
            if use_explicit_dark_colors:
                label.style('color: #ff6b6b;')
                err_label.style('color: #a0aec0;')
            else:
                label.classes('text-negative')
                err_label.classes('text-grey-7')


async def export_to_pdf(analysis_id: int) -> None:
    """Export analysis to PDF with progress notification."""
    try:
        logger.info(f"PDF export button clicked for analysis {analysis_id}")
        notification = ui.notification('Generating PDF...', type='ongoing', timeout=None)

        try:
            pdf_path = await run.cpu_bound(export_market_analysis_pdf, analysis_id)
            notification.dismiss()
            ui.notification(f'PDF generated successfully!', type='positive')
            ui.download(pdf_path)
            logger.info(f"PDF export completed for analysis {analysis_id}: {pdf_path}")
        except Exception as e:
            notification.dismiss()
            ui.notification(f'Error generating PDF: {str(e)}', type='negative')
            logger.error(f"Error exporting analysis {analysis_id} to PDF: {e}", exc_info=True)
    except ImportError as e:
        ui.notification('PDF export requires reportlab package. Please install it.', type='negative')
        logger.error(f"Missing reportlab dependency for PDF export: {e}", exc_info=True)
    except Exception as e:
        ui.notification(f'Error starting PDF export: {str(e)}', type='negative')
        logger.error(f"Error starting PDF export for analysis {analysis_id}: {e}", exc_info=True)


def get_instrument_details(symbol: str) -> Optional[Instrument]:
    """Load instrument details from database using context manager."""
    try:
        with get_db() as session:
            statement = select(Instrument).where(Instrument.name == symbol)
            instrument = session.exec(statement).first()
            return instrument
    except Exception as e:
        logger.error(f"Error loading instrument details for {symbol}: {e}", exc_info=True)
        return None

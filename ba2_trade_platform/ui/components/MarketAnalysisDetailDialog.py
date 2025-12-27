"""
Market Analysis Detail Dialog Component

A full-screen dialog that displays market analysis details without navigating away
from the current page, preserving filters and state.
"""

from nicegui import ui, run
from typing import Optional, Dict, Any, List, Callable
import json
from datetime import datetime, timezone

from ...core.db import get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput, Instrument, TradingOrder, ExpertRecommendation
from ...core.types import MarketAnalysisStatus, OrderStatus
from ...core.MarketAnalysisPDFExport import export_market_analysis_pdf
from ...logger import logger
from sqlmodel import select


class MarketAnalysisDetailDialog:
    """
    A full-screen dialog component for displaying market analysis details.
    
    Usage:
        dialog = MarketAnalysisDetailDialog()
        dialog.open(analysis_id)
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
        self.current_analysis_id = None
        self._create_dialog()
    
    def _create_dialog(self):
        """Create the dialog structure."""
        self.dialog = ui.dialog().props('full-width full-height maximized transition-show="slide-up" transition-hide="slide-down"')
        
        with self.dialog:
            with ui.card().classes('w-full h-full flex flex-col').style('max-width: 100%; max-height: 100vh; background: #1a1f2e;'):
                # Header with close button
                with ui.row().classes('w-full items-center justify-between p-4 border-b').style('border-color: rgba(160,174,192,0.2);'):
                    self.header_label = ui.label('Market Analysis Details').classes('text-xl font-bold').style('color: #e2e8f0;')
                    with ui.row().classes('gap-2'):
                        # Open in new tab button
                        self.open_url_btn = ui.button(icon='open_in_new', on_click=self._open_in_new_tab).props('flat round').tooltip('Open in new tab')
                        # Close button
                        ui.button(icon='close', on_click=self.close).props('flat round').tooltip('Close')
                
                # Scrollable content area
                with ui.scroll_area().classes('flex-grow w-full'):
                    self.content_container = ui.column().classes('w-full p-4')
    
    def _open_in_new_tab(self):
        """Open the current analysis in a new browser tab."""
        if self.current_analysis_id:
            ui.run_javascript(f"window.open('/market_analysis/{self.current_analysis_id}', '_blank')")
    
    def open(self, analysis_id: int):
        """
        Open the dialog and load the analysis details.
        
        Args:
            analysis_id: The ID of the MarketAnalysis to display
        """
        self.current_analysis_id = analysis_id
        self._load_content(analysis_id)
        self.dialog.open()
    
    def close(self):
        """Close the dialog."""
        self.dialog.close()
        if self.on_close_callback:
            self.on_close_callback()
    
    def _load_content(self, analysis_id: int):
        """Load and render the analysis content."""
        self.content_container.clear()
        
        with self.content_container:
            try:
                self._render_analysis_detail(analysis_id)
            except Exception as e:
                logger.error(f"Error loading market analysis detail {analysis_id}: {e}", exc_info=True)
                with ui.card().classes('w-full p-8 text-center'):
                    ui.label(f'Error loading analysis: {str(e)}').classes('text-h5').style('color: #ff6b6b;')
    
    def _render_analysis_detail(self, analysis_id: int):
        """Render the full analysis detail content."""
        # Load the market analysis
        market_analysis = get_instance(MarketAnalysis, analysis_id)
        if not market_analysis:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Market Analysis {analysis_id} not found').classes('text-h5').style('color: #ff6b6b;')
            return
        
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, market_analysis.expert_instance_id)
        if not expert_instance:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Expert Instance {market_analysis.expert_instance_id} not found').classes('text-h5').style('color: #ff6b6b;')
            return
        
        # Get expert instance with appropriate class
        from ...core.utils import get_expert_instance_from_id
        expert = get_expert_instance_from_id(expert_instance.id)
        if not expert:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Expert class {expert_instance.expert} not found').classes('text-h5').style('color: #ff6b6b;')
            return
        
        # Update header
        self.header_label.set_text(f'Market Analysis - {market_analysis.symbol}')
        
        # Load instrument details
        instrument = self._get_instrument_details(market_analysis.symbol)
        
        # Header section
        with ui.card().classes('w-full mb-4'):
            with ui.row().classes('w-full items-center justify-between'):
                with ui.column():
                    ui.label(f'Market Analysis Detail - {market_analysis.symbol}').classes('text-h4')
                    
                    # Instrument details if available
                    if instrument:
                        if instrument.company_name:
                            ui.label(f'Company: {instrument.company_name}').classes('text-subtitle1').style('color: #a0aec0;')
                        
                        if instrument.categories:
                            categories_text = ', '.join(instrument.categories)
                            ui.label(f'Sector: {categories_text}').classes('text-subtitle2').style('color: #a0aec0;')
                        
                        if instrument.labels:
                            labels_text = ', '.join(instrument.labels)
                            ui.label(f'Labels: {labels_text}').classes('text-subtitle2').style('color: #a0aec0;')
                    
                    # Analysis details
                    expert_display = expert_instance.alias or expert_instance.expert
                    ui.label(f'Expert: {expert_display} (ID: {expert_instance.id})').classes('text-subtitle1 mt-2').style('color: #a0aec0;')
                    ui.label(f'Status: {market_analysis.status.value if market_analysis.status else "Unknown"}').classes('text-subtitle2')
                    
                    # Convert UTC to local time for display
                    if market_analysis.created_at:
                        local_time = market_analysis.created_at.replace(tzinfo=timezone.utc).astimezone()
                        created_display = local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                    else:
                        created_display = "Unknown"
                    ui.label(f'Created: {created_display}').classes('text-subtitle2')
                
                with ui.column().classes('gap-2'):
                    ui.button('Export PDF', on_click=lambda: self._export_to_pdf(analysis_id), icon='picture_as_pdf').classes('bg-blue-600')
        
        # Handle different states based on analysis status
        if market_analysis.status == MarketAnalysisStatus.PENDING:
            self._render_pending_state(market_analysis)
            return
        elif market_analysis.status == MarketAnalysisStatus.CANCELLED:
            self._render_cancelled_state(market_analysis)
            return
        elif market_analysis.status in [MarketAnalysisStatus.FAILED]:
            self._render_error_state(market_analysis)
            return
        
        # Check for failed analysis even if status is not ERROR
        has_error = self._check_for_analysis_errors(market_analysis)
        if has_error:
            self._render_error_banner(market_analysis)
        
        # Main content - render expert analysis
        self._render_expert_analysis(market_analysis, expert)
    
    def _get_instrument_details(self, symbol: str) -> Optional[Instrument]:
        """Get instrument details by symbol."""
        try:
            session = get_db()
            statement = select(Instrument).where(Instrument.name == symbol)
            instrument = session.exec(statement).first()
            session.close()
            return instrument
        except Exception as e:
            logger.error(f"Error loading instrument details for {symbol}: {e}", exc_info=True)
            return None
    
    async def _export_to_pdf(self, analysis_id: int):
        """Export the market analysis to PDF and trigger download."""
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
        except Exception as e:
            ui.notification(f'Error starting PDF export: {str(e)}', type='negative')
            logger.error(f"Error starting PDF export for analysis {analysis_id}: {e}", exc_info=True)
    
    def _render_pending_state(self, market_analysis: MarketAnalysis):
        """Render UI for pending analysis."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='lg').classes('mb-4')
            ui.label('Analysis is pending...').classes('text-h5')
            ui.label('Please check back in a few minutes.').style('color: #a0aec0;')
            
            # Refresh button instead of auto-reload (preserves parent page state)
            ui.button('Refresh', icon='refresh', on_click=lambda: self._load_content(market_analysis.id)).classes('mt-4')
    
    def _render_cancelled_state(self, market_analysis: MarketAnalysis):
        """Render UI for cancelled analysis."""
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('cancel', size='3rem', color='orange').classes('mb-4')
            ui.label('Analysis was cancelled').classes('text-h5')
            created_str = market_analysis.created_at.strftime("%Y-%m-%d %H:%M:%S") if market_analysis.created_at else "Unknown"
            ui.label(f'Cancelled on: {created_str}').style('color: #a0aec0;')
    
    def _render_error_state(self, market_analysis: MarketAnalysis):
        """Render UI for error analysis."""
        is_skipped = False
        skip_reason = None
        
        if market_analysis.state and isinstance(market_analysis.state, dict):
            is_skipped = market_analysis.state.get('skipped', False)
            skip_reason = market_analysis.state.get('skip_reason')
        
        if is_skipped and skip_reason:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='3rem', color='orange').classes('mb-4')
                ui.label('Analysis was skipped').classes('text-h5').style('color: #ffa94d;')
                ui.label(f'Reason: {skip_reason}').classes('text-subtitle1 mt-2').style('color: #ffa94d;')
        else:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Analysis encountered an error').classes('text-h5').style('color: #ff6b6b;')
                
                error_message = self._extract_error_message(market_analysis.state)
                if error_message:
                    with ui.card().classes('w-full max-w-4xl mt-4 alert-banner danger'):
                        with ui.row().classes('items-start p-4'):
                            ui.icon('error_outline', color='negative').classes('mt-1 mr-3')
                            with ui.column().classes('flex-1'):
                                ui.label('Error Details:').classes('font-medium mb-2').style('color: #ff6b6b;')
                                with ui.element('pre').classes('bg-white/5 p-3 rounded text-sm overflow-auto max-h-48 whitespace-pre-wrap font-mono').style('color: #ff6b6b;'):
                                    ui.label(error_message)
    
    def _check_for_analysis_errors(self, market_analysis: MarketAnalysis) -> bool:
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
    
    def _extract_error_message(self, state: Optional[Dict]) -> Optional[str]:
        """Extract error message from analysis state."""
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
    
    def _render_error_banner(self, market_analysis: MarketAnalysis):
        """Render an error banner for failed analyses."""
        error_message = self._extract_error_message(market_analysis.state)
        
        with ui.card().classes('w-full mb-4 alert-banner danger'):
            with ui.row().classes('items-start p-4'):
                ui.icon('error_outline', color='negative', size='lg').classes('mt-1 mr-3')
                with ui.column().classes('flex-1'):
                    ui.label('Analysis Failed').classes('text-h6 font-medium mb-2').style('color: #ff6b6b;')
                    if error_message:
                        with ui.element('pre').classes('bg-white/5 p-3 rounded text-sm overflow-auto max-h-32 whitespace-pre-wrap font-mono border border-white/10').style('color: #ff6b6b;'):
                            ui.label(error_message)
                    else:
                        ui.label('The analysis encountered an error during execution.').style('color: #ff6b6b;')
    
    def _render_expert_analysis(self, market_analysis: MarketAnalysis, expert):
        """Render the expert-specific analysis."""
        try:
            expert.render_market_analysis(market_analysis)
        except Exception as e:
            logger.error(f"Error rendering expert analysis: {e}", exc_info=True)
            with ui.card().classes('w-full'):
                ui.label('Error rendering expert analysis').classes('text-h5').style('color: #ff6b6b;')
                ui.label(str(e)).style('color: #a0aec0;')


# Singleton instance for use across the application
_dialog_instance: Optional[MarketAnalysisDetailDialog] = None


def get_analysis_dialog() -> MarketAnalysisDetailDialog:
    """Get or create the singleton dialog instance."""
    global _dialog_instance
    if _dialog_instance is None:
        _dialog_instance = MarketAnalysisDetailDialog()
    return _dialog_instance


def open_analysis_dialog(analysis_id: int):
    """
    Convenience function to open the analysis dialog.
    
    Args:
        analysis_id: The ID of the MarketAnalysis to display
    """
    dialog = get_analysis_dialog()
    dialog.open(analysis_id)

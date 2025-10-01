from nicegui import ui, run
from typing import Optional, Dict, Any, List
import json
from datetime import datetime, timezone

from ...core.db import get_instance, get_db
from ...core.models import MarketAnalysis, ExpertInstance, AnalysisOutput, Instrument, TradingOrder, ExpertRecommendation
from ...core.types import MarketAnalysisStatus, OrderStatus
from ...core.MarketAnalysisPDFExport import export_market_analysis_pdf
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
        logger.error(f"Error loading instrument details for {symbol}: {e}", exc_info=True)
        return None


def content(analysis_id: int) -> None:
    """
    Market Analysis Detail Page Content
    
    Displays detailed analysis results for a specific MarketAnalysis ID,
    including both tool outputs and LLM outputs organized by agent in sub-tabs.
    
    Args:
        analysis_id: The ID of the MarketAnalysis to display
    """
    
    async def export_to_pdf() -> None:
        """Export the market analysis to PDF and trigger download."""
        try:
            logger.info(f"PDF export button clicked for analysis {analysis_id}")
            
            # Show loading notification
            notification = ui.notification('Generating PDF...', type='ongoing', timeout=None)
            
            try:
                # Generate PDF asynchronously using run.cpu_bound to avoid blocking UI
                pdf_path = await run.cpu_bound(export_market_analysis_pdf, analysis_id)
                
                # Close loading notification
                notification.dismiss()
                
                # Show success notification and trigger download
                ui.notification(f'PDF generated successfully!', type='positive')
                
                # Trigger file download
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
    
    try:
        # Load the market analysis
        market_analysis = get_instance(MarketAnalysis, analysis_id)
        if not market_analysis:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Market Analysis {analysis_id} not found').classes('text-h5 text-negative')
                ui.button('Back to Market Analysis', on_click=lambda: ui.navigate.to('/marketanalysis')).classes('mt-4')
            return
        
        # Load the expert instance
        expert_instance = get_instance(ExpertInstance, market_analysis.expert_instance_id)
        if not expert_instance:
            with ui.card().classes('w-full p-8 text-center'):
                ui.label(f'Expert Instance {market_analysis.expert_instance_id} not found').classes('text-h5 text-negative')
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
                    
                    # Convert UTC to local time for display
                    if market_analysis.created_at:
                        local_time = market_analysis.created_at.replace(tzinfo=timezone.utc).astimezone()
                        created_display = local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                    else:
                        created_display = "Unknown"
                    ui.label(f'Created: {created_display}').classes('text-subtitle2')
                
                with ui.column().classes('gap-2'):
                    ui.button('Export PDF', on_click=export_to_pdf, icon='picture_as_pdf').classes('bg-blue-600')
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
        
        # Main content - just show the analysis results directly
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


def _render_order_recommendations_tab(market_analysis: MarketAnalysis) -> None:
    """Render the Order Recommendations tab showing orders linked to this analysis."""
    try:
        session = get_db()
        
        # Get expert recommendations from this analysis
        statement = select(ExpertRecommendation).where(
            ExpertRecommendation.market_analysis_id == market_analysis.id
        )
        recommendations = list(session.exec(statement).all())
        
        if not recommendations:
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('info', size='2rem', color='grey').classes('mb-4')
                ui.label('No recommendations generated from this analysis').classes('text-h6 text-grey-6')
                ui.label('This analysis may still be processing or may not have generated actionable recommendations.').classes('text-grey-7')
            session.close()
            return
        
        # Get orders linked to these recommendations
        recommendation_ids = [rec.id for rec in recommendations]
        orders_statement = select(TradingOrder).where(
            TradingOrder.expert_recommendation_id.in_(recommendation_ids)
        ).order_by(TradingOrder.created_at.desc())
        orders = list(session.exec(orders_statement).all())
        
        session.close()
        
        # Summary section
        with ui.card().classes('w-full mb-4'):
            ui.label('📋 Order Summary').classes('text-h6 mb-4')
            
            with ui.row().classes('w-full gap-8'):
                # Recommendations stats
                with ui.column().classes('flex-1'):
                    ui.label('Expert Recommendations').classes('text-subtitle1 font-bold mb-2')
                    
                    rec_counts = {'BUY': 0, 'SELL': 0, 'HOLD': 0}
                    for rec in recommendations:
                        if hasattr(rec.recommended_action, 'value'):
                            action = rec.recommended_action.value
                        else:
                            action = str(rec.recommended_action)
                        rec_counts[action] = rec_counts.get(action, 0) + 1
                    
                    for action, count in rec_counts.items():
                        if count > 0:
                            color = {'BUY': 'green', 'SELL': 'red', 'HOLD': 'orange'}.get(action, 'grey')
                            with ui.row().classes('items-center mb-1'):
                                ui.icon('circle', color=color, size='sm').classes('mr-2')
                                ui.label(f'{action}: {count}').classes('text-sm')
                
                # Orders stats
                with ui.column().classes('flex-1'):
                    ui.label('Created Orders').classes('text-subtitle1 font-bold mb-2')
                    
                    if orders:
                        order_counts = {}
                        for order in orders:
                            status = order.status.value if hasattr(order.status, 'value') else str(order.status)
                            order_counts[status] = order_counts.get(status, 0) + 1
                        
                        for status, count in order_counts.items():
                            color = {
                                'open': 'orange', 
                                'closed': 'grey', 
                                'filled': 'green',
                                'pending': 'blue',
                                'canceled': 'red'
                            }.get(status.lower(), 'grey')
                            with ui.row().classes('items-center mb-1'):
                                ui.icon('circle', color=color, size='sm').classes('mr-2')
                                ui.label(f'{status.title()}: {count}').classes('text-sm')
                    else:
                        ui.label('No orders created yet').classes('text-sm text-grey-6')
        
        # Recommendations table
        with ui.card().classes('w-full mb-4'):
            ui.label('💡 Expert Recommendations').classes('text-h6 mb-4')
            
            if recommendations:
                rec_columns = [
                    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                    {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
                    {'name': 'confidence', 'label': 'Confidence', 'field': 'confidence', 'align': 'right'},
                    {'name': 'expected_profit', 'label': 'Expected Profit %', 'field': 'expected_profit', 'align': 'right'},
                    {'name': 'price_at_date', 'label': 'Price at Analysis', 'field': 'price_at_date', 'align': 'right'},
                    {'name': 'risk_level', 'label': 'Risk Level', 'field': 'risk_level', 'align': 'center'},
                    {'name': 'time_horizon', 'label': 'Time Horizon', 'field': 'time_horizon', 'align': 'center'},
                    {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left'}
                ]
                
                rec_rows = []
                for rec in recommendations:
                    action = rec.recommended_action.value if hasattr(rec.recommended_action, 'value') else str(rec.recommended_action)
                    created_at = rec.created_at.strftime('%Y-%m-%d %H:%M:%S') if rec.created_at else ''
                    
                    rec_rows.append({
                        'id': rec.id,
                        'symbol': rec.symbol,
                        'action': action,
                        'action_color': {'BUY': 'green', 'SELL': 'red', 'HOLD': 'orange'}.get(action, 'grey'),
                        'confidence': f"{rec.confidence:.1%}" if rec.confidence is not None else 'N/A',
                        'expected_profit': f"{rec.expected_profit_percent:.2f}%" if rec.expected_profit_percent else 'N/A',
                        'price_at_date': f"${rec.price_at_date:.2f}" if rec.price_at_date else 'N/A',
                        'risk_level': rec.risk_level.value if hasattr(rec.risk_level, 'value') else str(rec.risk_level),
                        'time_horizon': rec.time_horizon.value.replace('_', ' ').title() if hasattr(rec.time_horizon, 'value') else str(rec.time_horizon),
                        'created_at': created_at
                    })
                
                rec_table = ui.table(columns=rec_columns, rows=rec_rows, row_key='id').classes('w-full')
                
                # Add colored action badges
                rec_table.add_slot('body-cell-action', '''
                    <q-td :props="props">
                        <q-badge :color="props.row.action_color" :label="props.row.action" />
                    </q-td>
                ''')
            
            else:
                ui.label('No recommendations generated from this analysis.').classes('text-grey-6')
        
        # Orders table (if any orders exist)
        if orders:
            with ui.card().classes('w-full'):
                ui.label('📦 Created Orders').classes('text-h6 mb-4')
                
                order_columns = [
                    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                    {'name': 'side', 'label': 'Side', 'field': 'side', 'align': 'left'},
                    {'name': 'quantity', 'label': 'Quantity', 'field': 'quantity', 'align': 'right'},
                    {'name': 'order_type', 'label': 'Type', 'field': 'order_type', 'align': 'left'},
                    {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center'},
                    {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price', 'align': 'right'},
                    {'name': 'filled_qty', 'label': 'Filled', 'field': 'filled_qty', 'align': 'right'},
                    {'name': 'open_type', 'label': 'Open Type', 'field': 'open_type', 'align': 'center'},
                    {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left'}
                ]
                
                order_rows = []
                for order in orders:
                    status = order.status.value if hasattr(order.status, 'value') else str(order.status)
                    open_type = order.open_type.value if hasattr(order.open_type, 'value') else str(order.open_type)
                    created_at = order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else ''
                    
                    order_rows.append({
                        'id': order.id,
                        'symbol': order.symbol,
                        'side': order.side,
                        'quantity': f"{order.quantity:.2f}" if order.quantity else '',
                        'order_type': order.order_type,
                        'status': status,
                        'status_color': {
                            'open': 'orange', 
                            'closed': 'grey', 
                            'filled': 'green',
                            'pending': 'blue',
                            'canceled': 'red'
                        }.get(status.lower(), 'grey'),
                        'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                        'filled_qty': f"{order.filled_qty:.2f}" if order.filled_qty else '',
                        'open_type': open_type.replace('_', ' ').title(),
                        'created_at': created_at
                    })
                
                order_table = ui.table(columns=order_columns, rows=order_rows, row_key='id').classes('w-full')
                
                # Add colored status badges
                order_table.add_slot('body-cell-status', '''
                    <q-td :props="props">
                        <q-badge :color="props.row.status_color" :label="props.row.status" />
                    </q-td>
                ''')
        
        else:
            with ui.card().classes('w-full'):
                ui.label('📦 Created Orders').classes('text-h6 mb-4')
                ui.label('No orders have been created from the recommendations yet.').classes('text-grey-6')
                ui.label('Orders may be created manually or automatically by the Trade Manager when enabled.').classes('text-sm text-grey-7')
    
    except Exception as e:
        logger.error(f"Error rendering order recommendations tab: {e}", exc_info=True)
        with ui.card().classes('w-full'):
            ui.label('Error loading order recommendations').classes('text-h5 text-negative')
            ui.label(str(e)).classes('text-grey-7')


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
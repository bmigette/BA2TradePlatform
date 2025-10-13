"""
Floating P/L Per Expert Widget
Displays unrealized profit/loss for open positions grouped by expert.
"""
from nicegui import ui
import asyncio
from typing import Dict
from sqlmodel import select
from ...logger import logger
from ...core.db import get_db
from ...core.models import Transaction, ExpertInstance, TradingOrder
from ...core.types import TransactionStatus
from ...core.utils import get_account_instance_from_id


class FloatingPLPerExpertWidget:
    """Widget component showing floating profit/loss per expert."""
    
    def __init__(self):
        """Initialize and render the widget."""
        self.render()
    
    def render(self):
        """Render the widget with loading state."""
        with ui.card().classes('p-4'):
            ui.label('📊 Floating P/L Per Expert').classes('text-h6 mb-4')
            
            # Create loading placeholder
            loading_label = ui.label('🔄 Calculating floating P/L...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')
            
            # Load data asynchronously (non-blocking)
            asyncio.create_task(self._load_data_async(loading_label, content_container))
    
    def _calculate_pl_sync(self) -> Dict[str, float]:
        """Synchronous P/L calculation (runs in thread pool to avoid blocking)."""
        expert_pl = {}
        
        session = get_db()
        try:
            # Get all open transactions with expert attribution
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
            # Calculate P/L for each transaction
            for trans in transactions:
                try:
                    # Get expert info
                    expert = session.get(ExpertInstance, trans.expert_id)
                    if not expert:
                        continue
                    
                    expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                    
                    # Get account interface for current price
                    account = None
                    first_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                        .limit(1)
                    ).first()
                    
                    if first_order and first_order.account_id:
                        account = get_account_instance_from_id(first_order.account_id)
                    
                    # Get current market price
                    current_price = None
                    if account:
                        current_price = account.get_instrument_current_price(trans.symbol)
                    
                    if not current_price or trans.open_price is None:
                        continue
                    
                    # Calculate P/L: (current_price - open_price) * quantity
                    pl = (current_price - trans.open_price) * trans.quantity
                    
                    if expert_name not in expert_pl:
                        expert_pl[expert_name] = 0.0
                    expert_pl[expert_name] += pl
                    
                except Exception as e:
                    logger.error(f"Error calculating P/L for transaction {trans.id}: {e}")
                    continue
            
        finally:
            session.close()
        
        return expert_pl
    
    async def _load_data_async(self, loading_label, content_container):
        """Calculate and display floating P/L per expert (async wrapper for thread pool execution)."""
        try:
            # Run database queries in thread pool to avoid blocking UI
            loop = asyncio.get_event_loop()
            expert_pl = await loop.run_in_executor(None, self._calculate_pl_sync)
            
            # Clear loading message
            try:
                loading_label.delete()
            except RuntimeError:
                return
            
            # Display results
            try:
                with content_container:
                    if not expert_pl:
                        ui.label('No open positions').classes('text-sm text-gray-500')
                        return
                    
                    # Sort by P/L (highest to lowest)
                    sorted_pl = sorted(expert_pl.items(), key=lambda x: x[1], reverse=True)
                    
                    # Calculate totals
                    total_pl = sum(expert_pl.values())
                    positive_pl = sum(v for v in expert_pl.values() if v > 0)
                    negative_pl = sum(v for v in expert_pl.values() if v < 0)
                    
                    # Display each expert's P/L
                    for expert_name, pl in sorted_pl:
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label(expert_name).classes('text-sm truncate max-w-[200px]')
                            pl_color = 'text-green-600' if pl >= 0 else 'text-red-600'
                            ui.label(f'${pl:,.2f}').classes(f'text-sm font-bold {pl_color}')
                    
                    # Separator
                    ui.separator().classes('my-2')
                    
                    # Summary statistics
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('Positive P/L:').classes('text-sm font-bold')
                        ui.label(f'${positive_pl:,.2f}').classes('text-sm font-bold text-green-600')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('Negative P/L:').classes('text-sm font-bold')
                        ui.label(f'${negative_pl:,.2f}').classes('text-sm font-bold text-red-600')
                    
                    ui.separator().classes('my-2')
                    
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label('Total P/L:').classes('text-sm font-bold')
                        total_color = 'text-green-600' if total_pl >= 0 else 'text-red-600'
                        ui.label(f'${total_pl:,.2f}').classes(f'text-lg font-bold {total_color}')
                    
            except RuntimeError:
                return
                
        except Exception as e:
            logger.error(f"Error loading floating P/L per expert: {e}", exc_info=True)
            try:
                loading_label.delete()
            except RuntimeError:
                return
            try:
                with content_container:
                    ui.label('❌ Error calculating P/L').classes('text-sm text-red-600')
            except RuntimeError:
                pass

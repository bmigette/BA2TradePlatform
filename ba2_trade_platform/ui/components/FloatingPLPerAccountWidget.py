"""
Floating P/L Per Account Widget
Displays unrealized profit/loss for open positions grouped by account.
"""
from nicegui import ui
import asyncio
from typing import Dict
from sqlmodel import select
from ...logger import logger
from ...core.db import get_db
from ...core.models import Transaction, AccountDefinition, TradingOrder
from ...core.types import TransactionStatus
from ...core.utils import get_account_instance_from_id


class FloatingPLPerAccountWidget:
    """Widget component showing floating profit/loss per account."""
    
    def __init__(self):
        """Initialize and render the widget."""
        self.render()
    
    def render(self):
        """Render the widget with loading state."""
        with ui.card().classes('p-4'):
            ui.label('📊 Floating P/L Per Account').classes('text-h6 mb-4')
            
            # Create loading placeholder
            loading_label = ui.label('🔄 Calculating floating P/L...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')
            
            # Load data asynchronously (non-blocking)
            asyncio.create_task(self._load_data_async(loading_label, content_container))
    
    def _calculate_pl_sync(self) -> Dict[str, float]:
        """Synchronous P/L calculation (runs in thread pool to avoid blocking). Uses bulk price fetching."""
        account_pl = {}
        
        session = get_db()
        try:
            # Get all open transactions
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
            # Group transactions by account to batch price fetching
            account_transactions = {}  # account_id -> [(transaction, account_name), ...]
            
            for trans in transactions:
                try:
                    # Get first order to determine account
                    first_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                        .limit(1)
                    ).first()
                    
                    if not first_order or not first_order.account_id:
                        continue
                    
                    # Get account info
                    account_def = session.get(AccountDefinition, first_order.account_id)
                    if not account_def:
                        continue
                    
                    account_name = account_def.name
                    account_id = first_order.account_id
                    
                    if account_id not in account_transactions:
                        account_transactions[account_id] = []
                    account_transactions[account_id].append((trans, account_name))
                    
                except Exception as e:
                    logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                    continue
            
            # Fetch prices in bulk per account and calculate P/L
            for account_id, trans_list in account_transactions.items():
                try:
                    # Get account interface once
                    account = get_account_instance_from_id(account_id, session=session)
                    if not account:
                        continue
                    
                    # Collect all unique symbols for this account
                    symbols = list(set(trans.symbol for trans, _ in trans_list))
                    
                    # Fetch all prices at once (single API call)
                    prices = account.get_instrument_current_price(symbols)
                    
                    # Calculate P/L for each transaction using fetched prices
                    for trans, account_name in trans_list:
                        current_price = prices.get(trans.symbol) if prices else None
                        
                        if not current_price or trans.open_price is None:
                            continue
                        
                        # Calculate P/L: (current_price - open_price) * quantity
                        pl = (current_price - trans.open_price) * trans.quantity
                        
                        if account_name not in account_pl:
                            account_pl[account_name] = 0.0
                        account_pl[account_name] += pl
                        
                except Exception as e:
                    logger.error(f"Error calculating P/L for account {account_id}: {e}", exc_info=True)
                    continue
            
        finally:
            session.close()
        
        return account_pl
    
    async def _load_data_async(self, loading_label, content_container):
        """Calculate and display floating P/L per account (async wrapper for thread pool execution)."""
        try:
            # Run database queries in thread pool to avoid blocking UI
            loop = asyncio.get_event_loop()
            account_pl = await loop.run_in_executor(None, self._calculate_pl_sync)
            
            # Clear loading message
            try:
                loading_label.delete()
            except RuntimeError:
                return
            
            # Display results
            try:
                with content_container:
                    if not account_pl:
                        ui.label('No open positions').classes('text-sm text-gray-500')
                        return
                    
                    # Sort by P/L (highest to lowest)
                    sorted_pl = sorted(account_pl.items(), key=lambda x: x[1], reverse=True)
                    
                    # Calculate totals
                    total_pl = sum(account_pl.values())
                    positive_pl = sum(v for v in account_pl.values() if v > 0)
                    negative_pl = sum(v for v in account_pl.values() if v < 0)
                    
                    # Display each account's P/L
                    for account_name, pl in sorted_pl:
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label(account_name).classes('text-sm truncate max-w-[200px]')
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
            logger.error(f"Error loading floating P/L per account: {e}", exc_info=True)
            try:
                loading_label.delete()
            except RuntimeError:
                return
            try:
                with content_container:
                    ui.label('❌ Error calculating P/L').classes('text-sm text-red-600')
            except RuntimeError:
                pass

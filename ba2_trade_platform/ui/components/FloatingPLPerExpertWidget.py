"""
Floating P/L Per Expert Widget
Displays unrealized profit/loss for open positions grouped by expert.
"""
from nicegui import ui
import asyncio
from typing import Dict
from datetime import datetime, timezone
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
        """Synchronous P/L calculation (runs in thread pool to avoid blocking). Uses bulk price fetching."""
        expert_pl = {}
        
        session = get_db()
        try:
            # Get all open transactions with expert attribution
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
            # Group transactions by account to batch price fetching
            account_transactions = {}  # account_id -> [(transaction, expert_name), ...]
            
            for trans in transactions:
                try:
                    # Get expert info
                    expert = session.get(ExpertInstance, trans.expert_id)
                    if not expert:
                        continue
                    
                    expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                    
                    # Get account ID from first order
                    first_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                        .limit(1)
                    ).first()
                    
                    if not first_order or not first_order.account_id:
                        continue
                    
                    account_id = first_order.account_id
                    
                    if account_id not in account_transactions:
                        account_transactions[account_id] = []
                    account_transactions[account_id].append((trans, expert_name))
                    
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
                    
                    # Get broker positions to use their current_price (same as broker P/L calculation)
                    broker_positions = account.get_positions()
                    prices = {}
                    if broker_positions:
                        for pos in broker_positions:
                            pos_dict = pos if isinstance(pos, dict) else dict(pos)
                            prices[pos_dict['symbol']] = float(pos_dict['current_price'])
                    
                    # Calculate P/L for each transaction using broker prices
                    for trans, expert_name in trans_list:
                        current_price = prices.get(trans.symbol)
                        
                        if not current_price:
                            continue
                        
                        # Get all orders for this transaction
                        from ...core.types import OrderStatus, OrderDirection, OrderType
                        all_orders = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == trans.id)
                        ).all()
                        
                        if not all_orders:
                            continue
                        
                        # Determine position direction from first order
                        first_order = min(all_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
                        position_direction = first_order.side
                        
                        # Get all FILLED orders (any type that affects position)
                        # Exclude: OCO, OTO orders (they are TP/SL brackets, not position-affecting until triggered)
                        filled_orders = [
                            o for o in all_orders 
                            if o.status in OrderStatus.get_executed_statuses()
                            and o.order_type not in [OrderType.OCO, OrderType.OTO]
                            and o.filled_qty and o.filled_qty > 0
                        ]
                        
                        # Calculate net position and weighted average cost
                        # BUY orders add to position (+), SELL orders reduce position (-)
                        total_buy_cost = 0.0
                        total_buy_qty = 0.0
                        total_sell_qty = 0.0
                        
                        for order in filled_orders:
                            if not order.open_price or not order.filled_qty:
                                continue
                            
                            if order.side == OrderDirection.BUY:
                                total_buy_cost += order.filled_qty * order.open_price
                                total_buy_qty += order.filled_qty
                            elif order.side == OrderDirection.SELL:
                                total_sell_qty += order.filled_qty
                        
                        # Net filled quantity = buys - sells
                        net_filled_qty = total_buy_qty - total_sell_qty
                        
                        if abs(net_filled_qty) < 0.01:  # No net position (fully closed or not filled)
                            continue
                        
                        # Calculate weighted average price from BUY orders (entry cost basis)
                        if total_buy_qty < 0.01:
                            continue  # No buy orders, can't calculate avg price
                        avg_price = total_buy_cost / total_buy_qty
                        
                        # Use transaction.quantity as source of truth for current position
                        # This should match net_filled_qty, but transaction.quantity is authoritative
                        position_qty = abs(trans.quantity)
                        
                        # Calculate P/L: (current_price - avg_price) * position_quantity
                        # For long positions: profit when price > avg_price
                        # For short positions: profit when price < avg_price
                        pl = (current_price - avg_price) * position_qty
                        if position_direction == OrderDirection.SELL:
                            pl = -pl  # Invert for short positions
                        
                        if expert_name not in expert_pl:
                            expert_pl[expert_name] = 0.0
                        expert_pl[expert_name] += pl
                        
                        # Debug: Log when net filled qty differs from transaction qty
                        if abs(net_filled_qty - abs(trans.quantity)) > 0.01:
                            logger.debug(
                                f"Transaction {trans.id}: net_filled_qty={net_filled_qty:.2f} "
                                f"(buys={total_buy_qty:.2f} - sells={total_sell_qty:.2f}), "
                                f"transaction.quantity={trans.quantity}"
                            )
                        
                except Exception as e:
                    logger.error(f"Error calculating P/L for account {account_id}: {e}", exc_info=True)
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
                    
                    # Total P/L
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

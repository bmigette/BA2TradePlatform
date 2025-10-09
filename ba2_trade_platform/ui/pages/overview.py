from nicegui import ui
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func
from typing import Dict, Any
import requests
import aiohttp
import asyncio
import json

from ...core.db import get_all_instances, get_db, get_instance
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, ExpertInstance, AppSetting, TradingOrder
from ...core.types import MarketAnalysisStatus, OrderRecommendation, OrderStatus, OrderOpenType
from ...core.utils import get_expert_instance_from_id, get_market_analysis_id_from_order_id
from ...modules.accounts import providers
from ...logger import logger
from ..components import ProfitPerExpertChart, InstrumentDistributionChart, BalanceUsagePerExpertChart

class OverviewTab:
    def __init__(self, tabs_ref=None):
        self.tabs_ref = tabs_ref
        self.render()
    
    def _check_and_display_error_orders(self):
        """Check for orders with ERROR status and display alert banner."""
        session = get_db()
        try:
            # Query for ERROR orders
            error_orders = session.exec(
                select(TradingOrder)
                .where(TradingOrder.status == OrderStatus.ERROR)
            ).all()
            
            if error_orders:
                error_count = len(error_orders)
                
                # Function to switch to Account Overview tab
                def switch_to_account_tab():
                    if self.tabs_ref:
                        self.tabs_ref.set_value('account')
                
                # Function to attempt broker sync with heuristic mapping
                async def attempt_broker_sync():
                    try:
                        sync_button.set_enabled(False)
                        sync_button.props('loading')
                        
                        # Get all accounts and call refresh_orders with heuristic_mapping=True
                        accounts = get_all_instances(AccountDefinition)
                        success_count = 0
                        
                        for account_def in accounts:
                            try:
                                # Instantiate account provider
                                account_provider_class = providers.get(account_def.provider)
                                if account_provider_class:
                                    account = account_provider_class(account_def.id)
                                    # Call refresh_orders with heuristic_mapping=True
                                    if account.refresh_orders(heuristic_mapping=True):
                                        success_count += 1
                                        logger.info(f"Successfully synced orders for account {account_def.id} ({account_def.provider}) with heuristic mapping")
                            except Exception as e:
                                logger.error(f"Error syncing orders for account {account_def.id}: {e}", exc_info=True)
                        
                        if success_count > 0:
                            ui.notify(f'Successfully synced {success_count} account(s) from broker - refreshing page...', type='positive')
                        else:
                            ui.notify('No accounts synced successfully', type='warning')
                        
                        # Always refresh the page after sync to update order counts
                        await asyncio.sleep(1.5)  # Brief delay to show notification
                        ui.navigate.reload()
                    
                    except Exception as e:
                        logger.error(f"Error during broker sync: {e}", exc_info=True)
                        ui.notify(f'Error syncing from broker: {str(e)}', type='negative')
                        # Still refresh to show any partial updates
                        await asyncio.sleep(1.5)
                        ui.navigate.reload()
                
                # Create alert banner
                with ui.card().classes('w-full bg-red-50 border-l-4 border-red-500 p-4 mb-4'):
                    with ui.row().classes('w-full items-center gap-4'):
                        ui.icon('error', size='lg').classes('text-red-600')
                        with ui.column().classes('flex-1'):
                            ui.label(f'⚠️ {error_count} Order{"s" if error_count > 1 else ""} Failed').classes('text-lg font-bold text-red-800')
                            ui.label(f'There {"are" if error_count > 1 else "is"} {error_count} order{"s" if error_count > 1 else ""} with ERROR status that need{"" if error_count > 1 else "s"} attention.').classes('text-sm text-red-700')
                        
                        with ui.row().classes('gap-2'):
                            # Attempt sync from broker button
                            sync_button = ui.button('Attempt sync from broker', on_click=attempt_broker_sync).props('outline color=orange')
                            
                            # View details button
                            ui.button('View Orders', on_click=switch_to_account_tab).props('outline color=red')
                
                # Log the error orders for debugging
                logger.warning(f"Found {error_count} orders with ERROR status on overview page")
                for order in error_orders:
                    logger.debug(f"ERROR Order {order.id}: {order.symbol} - {order.comment}")
        
        except Exception as e:
            logger.error(f"Error checking for ERROR orders: {e}", exc_info=True)
        finally:
            session.close()
    
    def _check_and_display_pending_orders(self, tabs_ref=None):
        """Check for pending orders and display notification banner."""
        session = get_db()
        try:
            # Query for PENDING orders that haven't been submitted to broker yet
            pending_orders = session.exec(
                select(TradingOrder)
                .where(TradingOrder.status == OrderStatus.PENDING)
                .where(TradingOrder.broker_order_id == None)
            ).all()
            
            if pending_orders:
                pending_count = len(pending_orders)
                
                # Function to switch to Account Overview tab
                def switch_to_account_overview():
                    if tabs_ref:
                        tabs_ref.set_value('Account Overview')
                
                # Create notification banner
                with ui.card().classes('w-full bg-blue-50 border-l-4 border-blue-500 p-4 mb-4'):
                    with ui.row().classes('w-full items-center gap-4'):
                        ui.icon('pending_actions', size='lg').classes('text-blue-600')
                        with ui.column().classes('flex-1'):
                            ui.label(f'📋 {pending_count} Pending Order{"s" if pending_count > 1 else ""} to Review').classes('text-lg font-bold text-blue-800')
                            ui.label(f'There {"are" if pending_count > 1 else "is"} {pending_count} order{"s" if pending_count > 1 else ""} awaiting review and submission.').classes('text-sm text-blue-700')
                        
                        # Buttons for pending orders
                        with ui.column().classes('gap-2'):
                            ui.button('Review Orders', on_click=switch_to_account_overview).props('outline color=blue')
                            # Store only order IDs to avoid capturing database objects
                            order_ids = [order.id for order in pending_orders]
                            ui.button('Run Risk Management', on_click=lambda ids=order_ids: self._handle_risk_management_from_overview_by_ids(ids)).props('outline color=green')
                
                # Log the pending orders
                logger.info(f"Found {pending_count} pending orders on overview page")
                for order in pending_orders:
                    logger.debug(f"PENDING Order {order.id}: {order.symbol} - {order.comment}")
        
        except Exception as e:
            logger.error(f"Error checking for pending orders: {e}", exc_info=True)
        finally:
            session.close()
    
    def _check_and_display_quantity_mismatches(self):
        """Check for quantity mismatches between broker positions and transactions.
        
        DEPRECATED: This synchronous version blocks the UI. Use _check_and_display_quantity_mismatches_async() instead.
        """
        from ...core.models import Transaction
        from ...core.types import TransactionStatus
        
        session = get_db()
        mismatches = []
        
        try:
            # Get all accounts
            accounts = get_all_instances(AccountDefinition)
            
            for acc in accounts:
                provider_cls = providers.get(acc.provider)
                if not provider_cls:
                    continue
                
                provider_obj = provider_cls(acc.id)
                
                try:
                    # Get positions from broker
                    positions = provider_obj.get_positions()
                    
                    # Create a map of symbol -> broker quantity
                    broker_positions = {}
                    for pos in positions:
                        pos_dict = pos if isinstance(pos, dict) else dict(pos)
                        symbol = pos_dict.get('symbol')
                        qty = pos_dict.get('qty', 0)
                        if symbol and qty:
                            broker_positions[symbol] = float(qty)
                    
                    # Get all open transactions for this account
                    # First, get all trading orders for this account to find their transaction IDs
                    orders_stmt = select(TradingOrder).where(
                        TradingOrder.account_id == acc.id,
                        TradingOrder.transaction_id.isnot(None)
                    )
                    account_orders = session.exec(orders_stmt).all()
                    
                    # Get unique transaction IDs
                    transaction_ids = list(set(o.transaction_id for o in account_orders if o.transaction_id))
                    
                    # Get transactions
                    if transaction_ids:
                        txn_stmt = select(Transaction).where(
                            Transaction.id.in_(transaction_ids),
                            Transaction.status == TransactionStatus.OPENED
                        )
                        transactions = session.exec(txn_stmt).all()
                        
                        # Calculate total quantity per symbol from transactions
                        transaction_quantities = {}
                        for txn in transactions:
                            if txn.symbol and txn.quantity:
                                if txn.symbol not in transaction_quantities:
                                    transaction_quantities[txn.symbol] = 0
                                transaction_quantities[txn.symbol] += float(txn.quantity)
                        
                        # Compare broker positions with transaction quantities
                        for symbol, broker_qty in broker_positions.items():
                            txn_qty = transaction_quantities.get(symbol, 0)
                            
                            # Check if broker quantity is less than transaction quantity (potential issue)
                            if abs(broker_qty) < abs(txn_qty) - 0.01:  # 0.01 tolerance for float comparison
                                # Get all transactions for this symbol
                                symbol_txns = [t for t in transactions if t.symbol == symbol]
                                
                                mismatches.append({
                                    'account': acc.name,
                                    'account_id': acc.id,
                                    'symbol': symbol,
                                    'broker_qty': broker_qty,
                                    'transaction_qty': txn_qty,
                                    'difference': txn_qty - broker_qty,
                                    'transactions': symbol_txns
                                })
                
                except Exception as e:
                    logger.error(f"Error checking quantity mismatch for account {acc.name}: {e}", exc_info=True)
            
            # Display alerts for mismatches
            if mismatches:
                for mismatch in mismatches:
                    self._render_quantity_mismatch_alert(mismatch)
        
        except Exception as e:
            logger.error(f"Error checking for quantity mismatches: {e}", exc_info=True)
        finally:
            session.close()
    
    async def _check_and_display_quantity_mismatches_async(self):
        """Check for quantity mismatches between broker positions and transactions asynchronously."""
        from ...core.models import Transaction
        from ...core.types import TransactionStatus
        
        try:
            session = get_db()
            try:
                mismatches = []
                
                # Get all accounts
                accounts = get_all_instances(AccountDefinition)
                
                for acc in accounts:
                    provider_cls = providers.get(acc.provider)
                    if not provider_cls:
                        continue
                    
                    provider_obj = provider_cls(acc.id)
                    
                    try:
                        # Get positions from broker
                        positions = provider_obj.get_positions()
                        
                        # Create a map of symbol -> broker quantity
                        broker_positions = {}
                        for pos in positions:
                            pos_dict = pos if isinstance(pos, dict) else dict(pos)
                            symbol = pos_dict.get('symbol')
                            qty = pos_dict.get('qty', 0)
                            if symbol and qty:
                                broker_positions[symbol] = float(qty)
                        
                        # Get all open transactions for this account
                        # First, get all trading orders for this account to find their transaction IDs
                        orders_stmt = select(TradingOrder).where(
                            TradingOrder.account_id == acc.id,
                            TradingOrder.transaction_id.isnot(None)
                        )
                        account_orders = session.exec(orders_stmt).all()
                        
                        # Get unique transaction IDs
                        transaction_ids = list(set(o.transaction_id for o in account_orders if o.transaction_id))
                        
                        # Get transactions
                        if transaction_ids:
                            txn_stmt = select(Transaction).where(
                                Transaction.id.in_(transaction_ids),
                                Transaction.status == TransactionStatus.OPENED
                            )
                            transactions = session.exec(txn_stmt).all()
                            
                            # Calculate total quantity per symbol from transactions
                            transaction_quantities = {}
                            for txn in transactions:
                                if txn.symbol and txn.quantity:
                                    if txn.symbol not in transaction_quantities:
                                        transaction_quantities[txn.symbol] = 0
                                    transaction_quantities[txn.symbol] += float(txn.quantity)
                            
                            # Compare broker positions with transaction quantities
                            for symbol, broker_qty in broker_positions.items():
                                txn_qty = transaction_quantities.get(symbol, 0)
                                
                                # Check if broker quantity is less than transaction quantity (potential issue)
                                if abs(broker_qty) < abs(txn_qty) - 0.01:  # 0.01 tolerance for float comparison
                                    # Get all transactions for this symbol
                                    symbol_txns = [t for t in transactions if t.symbol == symbol]
                                    
                                    mismatches.append({
                                        'account': acc.name,
                                        'account_id': acc.id,
                                        'symbol': symbol,
                                        'broker_qty': broker_qty,
                                        'transaction_qty': txn_qty,
                                        'difference': txn_qty - broker_qty,
                                        'transactions': symbol_txns
                                    })
                    
                    except Exception as e:
                        logger.error(f"Error checking quantity mismatch for account {acc.name}: {e}", exc_info=True)
                
                # Display alerts for mismatches - check if client still exists
                if mismatches:
                    try:
                        with self.mismatch_alerts_container:
                            for mismatch in mismatches:
                                self._render_quantity_mismatch_alert(mismatch)
                    except RuntimeError:
                        # Client has been deleted (user navigated away), stop processing
                        return
                        
            finally:
                session.close()
                
        except Exception as e:
            logger.error(f"Error checking for quantity mismatches: {e}", exc_info=True)
    
    def _render_quantity_mismatch_alert(self, mismatch):
        """Render an alert for a quantity mismatch."""
        symbol = mismatch['symbol']
        broker_qty = mismatch['broker_qty']
        txn_qty = mismatch['transaction_qty']
        diff = mismatch['difference']
        account = mismatch['account']
        
        with ui.card().classes('w-full bg-yellow-50 border-l-4 border-yellow-500 p-4 mb-4'):
            with ui.row().classes('w-full items-center gap-4'):
                ui.icon('warning', size='lg').classes('text-yellow-600')
                with ui.column().classes('flex-1'):
                    ui.label(f'⚠️ Quantity Mismatch: {symbol}').classes('text-lg font-bold text-yellow-800')
                    ui.label(
                        f'Account: {account} | Broker: {broker_qty:+.2f} | Transactions: {txn_qty:+.2f} | Difference: {diff:+.2f}'
                    ).classes('text-sm text-yellow-700')
                    ui.label(
                        'The quantity at the broker is lower than the sum of open transaction quantities. This may indicate closed positions not reflected in transactions.'
                    ).classes('text-xs text-yellow-600')
                
                # Button to open correction dialog
                # Store only scalar values to avoid capturing database objects
                mismatch_data = {
                    'account': mismatch['account'],
                    'account_id': mismatch['account_id'],
                    'symbol': mismatch['symbol'],
                    'broker_qty': mismatch['broker_qty'],
                    'transaction_qty': mismatch['transaction_qty'],
                    'difference': mismatch['difference'],
                    'transaction_ids': [t.id for t in mismatch['transactions']]
                }
                ui.button(
                    'Adjust Quantities',
                    on_click=lambda data=mismatch_data: self._show_quantity_correction_dialog_by_ids(data)
                ).props('outline color=yellow-800')
    
    def _show_quantity_correction_dialog(self, mismatch):
        """Show dialog to adjust transaction quantities to match broker."""
        from ...core.models import Transaction
        from ...core.db import update_instance
        
        symbol = mismatch['symbol']
        broker_qty = mismatch['broker_qty']
        txn_qty = mismatch['transaction_qty']
        transactions = mismatch['transactions']
        account = mismatch['account']
        
        with ui.dialog() as dialog, ui.card().classes('w-[800px]'):
            ui.label(f'Adjust Quantities for {symbol}').classes('text-h6 mb-4')
            
            with ui.column().classes('w-full gap-4'):
                # Summary
                with ui.card().classes('bg-blue-50 p-4'):
                    ui.label(f'Account: {account}').classes('text-sm font-bold')
                    ui.label(f'Broker Position: {broker_qty:+.2f}').classes('text-sm')
                    ui.label(f'Transaction Total: {txn_qty:+.2f}').classes('text-sm')
                    ui.label(f'Difference: {txn_qty - broker_qty:+.2f}').classes('text-sm text-red-600 font-bold')
                
                ui.label('Open Transactions:').classes('text-subtitle2 mt-4')
                
                # Table of transactions with editable quantities
                transaction_inputs = {}
                
                with ui.card().classes('w-full'):
                    for txn in transactions:
                        with ui.row().classes('w-full items-center gap-4 p-2 border-b'):
                            ui.label(f'ID: {txn.id}').classes('w-20')
                            ui.label(f'{txn.symbol}').classes('w-24')
                            ui.label(f'Opened: {txn.open_date.strftime("%Y-%m-%d") if txn.open_date else "N/A"}').classes('w-32')
                            
                            # Editable quantity
                            qty_input = ui.number(
                                label='Quantity',
                                value=txn.quantity,
                                format='%.2f'
                            ).classes('w-32')
                            transaction_inputs[txn.id] = qty_input
                            
                            ui.label(f'Original: {txn.quantity:+.2f}').classes('text-xs text-gray-500')
                
                # Action buttons
                with ui.row().classes('w-full justify-between mt-4'):
                    with ui.row().classes('gap-2'):
                        ui.button(
                            'Set All to Match Broker',
                            on_click=lambda: self._distribute_broker_quantity(
                                transactions, broker_qty, transaction_inputs
                            )
                        ).props('color=primary outline')
                    
                    with ui.row().classes('gap-2'):
                        ui.button('Cancel', on_click=dialog.close).props('flat')
                        ui.button(
                            'Apply Changes',
                            on_click=lambda: self._apply_quantity_changes(
                                transactions, transaction_inputs, dialog
                            )
                        ).props('color=primary')
        
        dialog.open()
    
    def _distribute_broker_quantity(self, transactions, broker_qty, transaction_inputs):
        """Distribute broker quantity proportionally across transactions."""
        total_current_qty = sum(t.quantity for t in transactions if t.quantity)
        
        if total_current_qty == 0:
            # Equal distribution
            qty_per_txn = broker_qty / len(transactions)
            for txn in transactions:
                if txn.id in transaction_inputs:
                    transaction_inputs[txn.id].value = qty_per_txn
        else:
            # Proportional distribution
            for txn in transactions:
                if txn.id in transaction_inputs:
                    proportion = txn.quantity / total_current_qty
                    new_qty = broker_qty * proportion
                    transaction_inputs[txn.id].value = new_qty
        
        ui.notify('Quantities distributed proportionally', type='info')
    
    def _apply_quantity_changes(self, transactions, transaction_inputs, dialog):
        """Apply quantity changes to transactions."""
        from ...core.db import update_instance
        
        try:
            updated_count = 0
            for txn in transactions:
                if txn.id in transaction_inputs:
                    new_qty = transaction_inputs[txn.id].value
                    if new_qty != txn.quantity:
                        txn.quantity = new_qty
                        update_instance(txn)
                        updated_count += 1
            
            ui.notify(f'Updated {updated_count} transaction(s)', type='positive')
            dialog.close()
            
            # Refresh the page to show updated data
            async def reload_page():
                await ui.navigate.reload()
            ui.timer(0.1, reload_page, once=True)
        
        except Exception as e:
            logger.error(f"Error applying quantity changes: {e}", exc_info=True)
            ui.notify(f'Error updating quantities: {str(e)}', type='negative')
    
    def render(self):
        """Render the overview tab."""
        logger.debug("[RENDER] OverviewTab.render() - START")
        
        # Check for ERROR orders and display alert
        self._check_and_display_error_orders()
        
        # Create container for quantity mismatch alerts
        self.mismatch_alerts_container = ui.column().classes('w-full')
        
        # Check for quantity mismatches asynchronously
        asyncio.create_task(self._check_and_display_quantity_mismatches_async())
        
        # Check for PENDING orders and display notification
        self._check_and_display_pending_orders(self.tabs_ref)
        
        with ui.grid(columns=4).classes('w-full gap-4'):
            pass
            # Row 1: OpenAI Spending, Analysis Jobs, Order Statistics, and Order Recommendations
            self._render_openai_spending_widget()
            self._render_analysis_jobs_widget()
            self._render_order_statistics_widget()
            with ui.column().classes(''):
                self._render_order_recommendations_widget()
            
            # Row 2: Profit Per Expert and Balance Usage Per Expert
            ProfitPerExpertChart()
            BalanceUsagePerExpertChart()
            
            # Row 3: Position Distribution by Label
            self._render_position_distribution_widget(grouping_field='labels')
            
            # Row 3: Position Distribution by Category (can span or be paired with other widgets)
            self._render_position_distribution_widget(grouping_field='categories')
    
    def _render_position_distribution_widget(self, grouping_field='labels'):
        """Fetch positions from accounts and render distribution chart.
        
        Args:
            grouping_field: Either 'labels' or 'categories' to determine grouping
        """
        # Create card with loading placeholder
        with ui.card().classes('p-4'):
            title = '📊 Position Distribution by ' + ('Labels' if grouping_field == 'labels' else 'Categories')
            ui.label(title).classes('text-h6 mb-4')
            
            loading_label = ui.label('🔄 Loading positions...').classes('text-sm text-gray-500')
            chart_container = ui.column().classes('w-full')
            
            # Load positions asynchronously
            asyncio.create_task(self._load_position_distribution_async(loading_label, chart_container, grouping_field))
    
    async def _load_position_distribution_async(self, loading_label, chart_container, grouping_field):
        """Load position distribution data asynchronously."""
        try:
            # Fetch positions from all accounts
            accounts = get_all_instances(AccountDefinition)
            all_positions_raw = []
            
            for acc in accounts:
                provider_cls = providers.get(acc.provider)
                if provider_cls:
                    provider_obj = provider_cls(acc.id)
                    try:
                        positions = provider_obj.get_positions()
                        for pos in positions:
                            pos_dict = pos if isinstance(pos, dict) else dict(pos)
                            pos_dict['account'] = acc.name
                            all_positions_raw.append(pos_dict)
                    except Exception as e:
                        logger.error(f"Error fetching positions from account {acc.name}: {e}", exc_info=True)
            
            # Clear loading message - check if client still exists
            try:
                loading_label.delete()
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
            
            # Render chart - check if client still exists
            try:
                with chart_container:
                    InstrumentDistributionChart(positions=all_positions_raw, grouping_field=grouping_field)
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
                
        except Exception as e:
            # Clear loading message and show error - check if client still exists
            try:
                loading_label.delete()
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
            
            try:
                with chart_container:
                    ui.label('❌ Failed to load positions').classes('text-sm text-red-600')
                    ui.label(f'Error: {str(e)}').classes('text-xs text-gray-500')
            except RuntimeError:
                # Client has been deleted (user navigated away), ignore the error
                pass
    
    def _render_openai_spending_widget(self):
        """Widget showing OpenAI API spending."""
        with ui.card().classes('p-4'):
            ui.label('💰 OpenAI API Usage').classes('text-h6 mb-4')
            
            # Create loading placeholder and load data asynchronously
            loading_label = ui.label('🔄 Loading usage data...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')
            
            # Load data asynchronously
            asyncio.create_task(self._load_openai_usage_data(loading_label, content_container))
    
    def _render_analysis_jobs_widget(self):
        """Widget showing analysis job statistics."""
        with ui.card().classes('p-4'):
            ui.label('📊 Analysis Jobs').classes('text-h6 mb-4')
            
            # Get actual data from database
            session = get_db()
            try:
                # Count successful analyses
                successful_count = session.exec(
                    select(func.count(MarketAnalysis.id))
                    .where(MarketAnalysis.status == MarketAnalysisStatus.COMPLETED)
                ).first() or 0
                
                # Count failed analyses
                failed_count = session.exec(
                    select(func.count(MarketAnalysis.id))
                    .where(MarketAnalysis.status == MarketAnalysisStatus.FAILED)
                ).first() or 0
                
                # Count running analyses
                running_count = session.exec(
                    select(func.count(MarketAnalysis.id))
                    .where(MarketAnalysis.status == MarketAnalysisStatus.RUNNING)
                ).first() or 0
                
            finally:
                session.close()
            
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('✅ Successful:').classes('text-sm')
                ui.label(str(successful_count)).classes('text-sm font-bold text-green-600')
            
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('❌ Failed:').classes('text-sm')
                ui.label(str(failed_count)).classes('text-sm font-bold text-red-600')
            
            with ui.row().classes('w-full justify-between items-center mb-2'):
                ui.label('⏳ Running:').classes('text-sm')
                ui.label(str(running_count)).classes('text-sm font-bold text-orange-600')
    
    def _render_order_statistics_widget(self):
        """Widget showing order statistics per account."""
        with ui.card().classes('p-4'):
            ui.label('📋 Orders by Account').classes('text-h6 mb-4')
            
            # Get all accounts
            accounts = get_all_instances(AccountDefinition)
            
            if not accounts:
                ui.label('No accounts configured').classes('text-sm text-gray-500')
                return
            
            session = get_db()
            try:
                for account in accounts:
                    # Count orders by status for this account
                    # Open orders = FILLED, NEW, OPEN, ACCEPTED
                    open_count = session.exec(
                        select(func.count(TradingOrder.id))
                        .where(TradingOrder.account_id == account.id)
                        .where(TradingOrder.status.in_([
                            OrderStatus.FILLED, 
                            OrderStatus.NEW, 
                            OrderStatus.OPEN, 
                            OrderStatus.ACCEPTED
                        ]))
                    ).first() or 0
                    
                    pending_count = session.exec(
                        select(func.count(TradingOrder.id))
                        .where(TradingOrder.account_id == account.id)
                        .where(TradingOrder.status.in_([OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]))
                    ).first() or 0
                    
                    error_count = session.exec(
                        select(func.count(TradingOrder.id))
                        .where(TradingOrder.account_id == account.id)
                        .where(TradingOrder.status == OrderStatus.ERROR)
                    ).first() or 0
                    
                    # Display account section
                    with ui.column().classes('w-full mb-4'):
                        ui.label(f'🏦 {account.name}').classes('text-sm font-bold mb-2')
                        
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('📂 Open:').classes('text-xs')
                            ui.label(str(open_count)).classes('text-xs font-bold text-blue-600')
                        
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('⏳ Pending:').classes('text-xs')
                            ui.label(str(pending_count)).classes('text-xs font-bold text-orange-600')
                        
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('❌ Error:').classes('text-xs')
                            ui.label(str(error_count)).classes('text-xs font-bold text-red-600')
                        
                        # Add separator between accounts (except for last one)
                        if account != accounts[-1]:
                            ui.separator().classes('my-2')
                            
            finally:
                session.close()
    
    def _render_order_recommendations_widget(self):
        """Widget showing order recommendation statistics."""
        with ui.card().classes('p-4'):
            ui.label('📈 Order Recommendations').classes('text-h6 mb-4')
            
            # Calculate date ranges
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            session = get_db()
            try:
                # Get recommendations for last week
                week_recs = self._get_recommendation_counts(session, week_ago)
                month_recs = self._get_recommendation_counts(session, month_ago)
                
            finally:
                session.close()
            
            with ui.row().classes('w-full gap-8'):
                # Last Week column
                with ui.column().classes('flex-1'):
                    ui.label('Last Week').classes('text-subtitle1 font-bold mb-2')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🟢 BUY:').classes('text-sm')
                        ui.label(str(week_recs['BUY'])).classes('text-sm font-bold text-green-600')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🔴 SELL:').classes('text-sm')
                        ui.label(str(week_recs['SELL'])).classes('text-sm font-bold text-red-600')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🟡 HOLD:').classes('text-sm')
                        ui.label(str(week_recs['HOLD'])).classes('text-sm font-bold text-orange-600')
                
                # Last Month column
                with ui.column().classes('flex-1'):
                    ui.label('Last Month').classes('text-subtitle1 font-bold mb-2')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🟢 BUY:').classes('text-sm')
                        ui.label(str(month_recs['BUY'])).classes('text-sm font-bold text-green-600')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🔴 SELL:').classes('text-sm')
                        ui.label(str(month_recs['SELL'])).classes('text-sm font-bold text-red-600')
                    
                    with ui.row().classes('w-full justify-between items-center mb-1'):
                        ui.label('🟡 HOLD:').classes('text-sm')
                        ui.label(str(month_recs['HOLD'])).classes('text-sm font-bold text-orange-600')
    
    def _get_recommendation_counts(self, session, since_date: datetime) -> Dict[str, int]:
        """Get recommendation counts since a specific date."""
        counts = {'BUY': 0, 'SELL': 0, 'HOLD': 0}
        
        try:
            # Count BUY recommendations
            buy_count = session.exec(
                select(func.count(ExpertRecommendation.id))
                .where(ExpertRecommendation.recommended_action == OrderRecommendation.BUY)
                .where(ExpertRecommendation.created_at >= since_date)
            ).first() or 0
            
            # Count SELL recommendations
            sell_count = session.exec(
                select(func.count(ExpertRecommendation.id))
                .where(ExpertRecommendation.recommended_action == OrderRecommendation.SELL)
                .where(ExpertRecommendation.created_at >= since_date)
            ).first() or 0
            
            # Count HOLD recommendations
            hold_count = session.exec(
                select(func.count(ExpertRecommendation.id))
                .where(ExpertRecommendation.recommended_action == OrderRecommendation.HOLD)
                .where(ExpertRecommendation.created_at >= since_date)
            ).first() or 0
            
            counts = {'BUY': buy_count, 'SELL': sell_count, 'HOLD': hold_count}
        except Exception as e:
            logger.error(f"Error getting recommendation counts: {e}", exc_info=True)
        
        return counts
    
    async def _load_openai_usage_data(self, loading_label, content_container):
        """Load OpenAI usage data asynchronously and update UI."""
        try:
            usage_data = await self._get_openai_usage_data_async()
            
            # Clear loading message - check if client still exists
            try:
                loading_label.delete()
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
            
            # Populate content - check if client still exists
            try:
                with content_container:
                    if usage_data.get('error'):
                        ui.label('⚠️ Error fetching usage data').classes('text-sm text-red-600 mb-2')
                        error_message = usage_data['error']
                        
                        # Check if this is an admin key requirement error
                        if 'admin-keys' in error_message:
                            # Split the error message at the URL
                            parts = error_message.split('https://platform.openai.com/settings/organization/admin-keys')
                            if len(parts) == 2:
                                ui.label(parts[0]).classes('text-xs text-gray-500')
                                ui.link('Get OpenAI Admin Key', 'https://platform.openai.com/settings/organization/admin-keys', new_tab=True).classes('text-xs text-blue-600 underline mb-2')
                            else:
                                ui.label(error_message).classes('text-xs text-gray-500')
                        else:
                            ui.label(error_message).classes('text-xs text-gray-500')
                    else:
                        with ui.row().classes('w-full justify-between items-center mb-2'):
                            ui.label('Last Week:').classes('text-sm')
                            week_cost = usage_data.get('week_cost', 0)
                            ui.label(f'${week_cost:.2f}').classes('text-sm font-bold text-orange-600')
                        
                        with ui.row().classes('w-full justify-between items-center mb-2'):
                            ui.label('Last Month:').classes('text-sm')
                            month_cost = usage_data.get('month_cost', 0)
                            ui.label(f'${month_cost:.2f}').classes('text-sm font-bold text-red-600')
                        
                        # Show remaining credit only if available
                        remaining = usage_data.get('remaining_credit')
                        if remaining is not None:
                            with ui.row().classes('w-full justify-between items-center mb-2'):
                                ui.label('Remaining Credit:').classes('text-sm')
                                ui.label(f'${remaining:.2f}').classes('text-sm font-bold text-green-600')
                        else:
                            with ui.row().classes('w-full justify-between items-center mb-2'):
                                ui.label('Remaining Credit:').classes('text-sm')
                                ui.label('Not available').classes('text-sm text-gray-500')
                        
                        # Show hard limit if available
                        hard_limit = usage_data.get('hard_limit')
                        if hard_limit:
                            with ui.row().classes('w-full justify-between items-center mb-2'):
                                ui.label('Credit Limit:').classes('text-sm')
                                ui.label(f'${hard_limit:.2f}').classes('text-sm text-gray-600')
                        
                        ui.separator().classes('my-2')
                        last_updated = usage_data.get('last_updated', 'Unknown')
                        ui.label(f'Last updated: {last_updated}').classes('text-xs text-gray-500')
                        
                        # Show note if using simulated data
                        note = usage_data.get('note')
                        if note:
                            ui.label(f'📝 {note}').classes('text-xs text-blue-600')
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
        except Exception as e:
            # Clear loading message and show error - check if client still exists
            try:
                loading_label.delete()
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
            
            try:
                with content_container:
                    ui.label('❌ Failed to load usage data').classes('text-sm text-red-600')
                    ui.label(f'Error: {str(e)}').classes('text-xs text-gray-500')
            except RuntimeError:
                # Client has been deleted (user navigated away), ignore the error
                pass
    
    async def _get_openai_usage_data_async(self) -> Dict[str, Any]:
        """Fetch real OpenAI usage data from the API asynchronously."""
        try:
            # Get OpenAI API key from app settings (prefer admin key for usage data)
            session = get_db()
            try:
                # Try to get admin key first (has more permissions)
                admin_key_setting = session.exec(
                    select(AppSetting).where(AppSetting.key == 'openai_admin_api_key')
                ).first()
                
                # Fall back to regular API key
                regular_key_setting = session.exec(
                    select(AppSetting).where(AppSetting.key == 'openai_api_key')
                ).first()
                
                api_key = None
                key_type = None
                
                if admin_key_setting and admin_key_setting.value_str:
                    # Validate admin key format
                    if not admin_key_setting.value_str.startswith("sk-admin"):
                        return {
                            'error': 'Invalid admin key format. Admin keys should start with "sk-admin".',
                            'link': 'https://platform.openai.com/settings/organization/api-keys'
                        }
                    api_key = admin_key_setting.value_str
                    key_type = 'admin'
                elif regular_key_setting and regular_key_setting.value_str:
                    api_key = regular_key_setting.value_str
                    key_type = 'regular'
                
                if not api_key:
                    return {'error': 'OpenAI API key not configured in settings'}
                
            finally:
                session.close()
            
            # Calculate date ranges
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            # Fetch usage data from OpenAI API
            headers = {
                'Authorization': f'Bearer {api_key}'
            }
            
            # Use the correct OpenAI costs API endpoint
            week_cost = 0
            month_cost = 0
            
            # Get costs for the past month using the correct API
            costs_url = 'https://api.openai.com/v1/organization/costs'
            params = {
                'start_time': int(month_ago.timestamp()),
                'end_time': int(now.timestamp()),
                'bucket_width': '1d',  # Daily buckets
                'limit': 35
            }
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(costs_url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                        if response.status == 200:
                            costs_data = await response.json()
                            
                            # Process daily cost data
                            for cost_bucket in costs_data.get('data', []):
                                bucket_start_time = cost_bucket.get('start_time', 0)
                                bucket_date = datetime.fromtimestamp(bucket_start_time)
                                
                                # Calculate daily cost from results array
                                daily_cost = 0
                                for result in cost_bucket.get('results', []):
                                    amount = result.get('amount', {})
                                    daily_cost += amount.get('value', 0)
                                
                                # Add to appropriate time periods
                                if bucket_date >= week_ago:
                                    week_cost += daily_cost
                                month_cost += daily_cost
                            
                            # Try to get organization limits
                            limits_url = 'https://api.openai.com/v1/organization/limits'
                            async with session.get(limits_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as limits_response:
                                remaining_credit = None
                                hard_limit = None
                                
                                if limits_response.status == 200:
                                    limits_data = await limits_response.json()
                                    hard_limit = limits_data.get('max_usage_usd')
                                    if hard_limit:
                                        remaining_credit = max(0, hard_limit - month_cost)
                            
                            return {
                                'week_cost': week_cost,
                                'month_cost': month_cost,
                                'remaining_credit': remaining_credit,
                                'hard_limit': hard_limit,
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                                'note': 'Real OpenAI usage data'
                            }
                        
                        elif response.status == 401:
                            # Check if this is a permissions issue that requires admin key
                            try:
                                error_data = await response.json()
                                error_message = error_data.get('error', {}).get('message', '')
                                
                                if 'insufficient permissions' in error_message.lower() and 'api.usage.read' in error_message:
                                    if key_type == 'admin':
                                        return {'error': 'Admin API key is invalid - please check your admin key in settings'}
                                    else:
                                        return {'error': 'Regular API key lacks usage permissions. You need an OpenAI Admin API key. Get one at: https://platform.openai.com/settings/organization/admin-keys'}
                                else:
                                    return {'error': f'Invalid OpenAI API key - {error_message}'}
                            except:
                                return {'error': 'Invalid OpenAI API key - please check your API key in settings'}
                        elif response.status == 403:
                            return {'error': 'API key does not have permission to access billing data'}
                        elif response.status == 429:
                            return {'error': 'OpenAI API rate limit exceeded - try again later'}
                        else:
                            error_text = await response.text()
                            logger.error(f'OpenAI API error {response.status}: {error_text}', exc_info=True)
                            return {'error': f'OpenAI API error ({response.status}): {error_text[:100]}...'}
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling OpenAI costs API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
            
            # If we get here, something went wrong but we didn't catch it above
            return {
                'week_cost': 0,
                'month_cost': 0,
                'remaining_credit': None,
                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                'note': 'Unable to fetch real usage data'
            }
                
        except asyncio.TimeoutError:
            return {'error': 'Request timeout - OpenAI API not responding'}
        except aiohttp.ClientError as e:
            logger.error(f'Error fetching OpenAI usage data: {e}', exc_info=True)
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logger.error(f'Unexpected error fetching OpenAI usage data: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    def _get_openai_usage_data(self) -> Dict[str, Any]:
        """Fetch real OpenAI usage data from the API."""
        try:
            # Get OpenAI API key from app settings (prefer admin key for usage data)
            session = get_db()
            try:
                # Try to get admin key first (has more permissions)
                admin_key_setting = session.exec(
                    select(AppSetting).where(AppSetting.key == 'openai_admin_api_key')
                ).first()
                
                # Fall back to regular API key
                regular_key_setting = session.exec(
                    select(AppSetting).where(AppSetting.key == 'openai_api_key')
                ).first()
                
                api_key = None
                key_type = None
                
                if admin_key_setting and admin_key_setting.value_str:
                    # Validate admin key format
                    if not admin_key_setting.value_str.startswith("sk-admin"):
                        return {
                            'error': 'Invalid admin key format. Admin keys should start with "sk-admin".',
                            'link': 'https://platform.openai.com/settings/organization/api-keys'
                        }
                    api_key = admin_key_setting.value_str
                    key_type = 'admin'
                elif regular_key_setting and regular_key_setting.value_str:
                    api_key = regular_key_setting.value_str
                    key_type = 'regular'
                
                if not api_key:
                    return {'error': 'OpenAI API key not configured in settings'}
                
            finally:
                session.close()
            
            # Calculate date ranges
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            # Fetch usage data from OpenAI API
            headers = {
                'Authorization': f'Bearer {api_key}'
            }
            
            # Use the correct OpenAI costs API endpoint
            week_cost = 0
            month_cost = 0
            
            # Get costs for the past month using the correct API
            costs_url = 'https://api.openai.com/v1/organization/costs'
            params = {
                'start_time': int(month_ago.timestamp()),
                'end_time': int(now.timestamp()),
                'bucket_width': '1d',  # Daily buckets
                'limit': 35
            }
            
            try:
                response = requests.get(costs_url, headers=headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    costs_data = response.json()
                    
                    # Process daily cost data
                    for cost_bucket in costs_data.get('data', []):
                        bucket_start_time = cost_bucket.get('start_time', 0)
                        bucket_date = datetime.fromtimestamp(bucket_start_time)
                        
                        # Calculate daily cost from results array
                        daily_cost = 0
                        for result in cost_bucket.get('results', []):
                            amount = result.get('amount', {})
                            daily_cost += amount.get('value', 0)
                        
                        # Add to appropriate time periods
                        if bucket_date >= week_ago:
                            week_cost += daily_cost
                        month_cost += daily_cost
                    
                    # Try to get organization limits
                    limits_url = 'https://api.openai.com/v1/organization/limits'
                    limits_response = requests.get(limits_url, headers=headers, timeout=5)
                    
                    remaining_credit = None
                    hard_limit = None
                    
                    if limits_response.status_code == 200:
                        limits_data = limits_response.json()
                        hard_limit = limits_data.get('max_usage_usd')
                        if hard_limit:
                            remaining_credit = max(0, hard_limit - month_cost)
                    
                    return {
                        'week_cost': week_cost,
                        'month_cost': month_cost,
                        'remaining_credit': remaining_credit,
                        'hard_limit': hard_limit,
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                        'note': 'Real OpenAI usage data'
                    }
                
                elif response.status_code == 401:
                    # Check if this is a permissions issue that requires admin key
                    try:
                        error_data = response.json()
                        error_message = error_data.get('error', {}).get('message', '')
                        
                        if 'insufficient permissions' in error_message.lower() and 'api.usage.read' in error_message:
                            if key_type == 'admin':
                                return {'error': 'Admin API key is invalid - please check your admin key in settings'}
                            else:
                                return {'error': 'Regular API key lacks usage permissions. You need an OpenAI Admin API key. Get one at: https://platform.openai.com/settings/organization/admin-keys'}
                        else:
                            return {'error': f'Invalid OpenAI API key - {error_message}'}
                    except:
                        return {'error': 'Invalid OpenAI API key - please check your API key in settings'}
                elif response.status_code == 403:
                    return {'error': 'API key does not have permission to access billing data'}
                elif response.status_code == 429:
                    return {'error': 'OpenAI API rate limit exceeded - try again later'}
                else:
                    error_text = response.text
                    logger.error(f'OpenAI API error {response.status_code}: {error_text}', exc_info=True)
                    return {'error': f'OpenAI API error ({response.status_code}): {error_text[:100]}...'}
                    
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling OpenAI costs API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
            
            # If we get here, something went wrong but we didn't catch it above
            return {
                'week_cost': 0,
                'month_cost': 0,
                'remaining_credit': None,
                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                'note': 'Unable to fetch real usage data'
            }
                
        except requests.exceptions.Timeout:
            return {'error': 'Request timeout - OpenAI API not responding'}
        except requests.exceptions.RequestException as e:
            logger.error(f'Error fetching OpenAI usage data: {e}', exc_info=True)
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logger.error(f'Unexpected error fetching OpenAI usage data: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}

    def _handle_risk_management_from_overview(self, pending_orders):
        """Handle risk management execution from overview page."""
        try:
            if not pending_orders:
                ui.notify('No pending orders to process', type='info')
                return
            
            # Group orders by expert instance to run risk management per expert
            from collections import defaultdict
            orders_by_expert = defaultdict(list)
            
            # Get expert instance IDs from order recommendations
            from ...core.db import get_db
            from ...core.models import ExpertRecommendation
            
            with get_db() as session:
                for order in pending_orders:
                    if order.expert_recommendation_id:
                        recommendation = session.get(ExpertRecommendation, order.expert_recommendation_id)
                        if recommendation:
                            orders_by_expert[recommendation.instance_id].append(order)
            
            if not orders_by_expert:
                ui.notify('No orders with valid expert recommendations found', type='warning')
                return
            
            # Show processing dialog
            with ui.dialog() as processing_dialog, ui.card():
                ui.label('Running Risk Management for All Experts...').classes('text-h6')
                ui.spinner(size='lg')
                ui.label('Processing pending orders and calculating quantities').classes('text-sm text-gray-600')
            
            processing_dialog.open()
            
            try:
                from ...core.TradeRiskManagement import get_risk_management
                risk_management = get_risk_management()
                
                total_processed = 0
                experts_processed = 0
                
                # Run risk management for each expert
                for expert_id, expert_orders in orders_by_expert.items():
                    try:
                        updated_orders = risk_management.review_and_prioritize_pending_orders(expert_id)
                        total_processed += len(updated_orders)
                        experts_processed += 1
                        logger.info(f"Processed {len(updated_orders)} orders for expert {expert_id}")
                    except Exception as e:
                        logger.error(f"Error processing risk management for expert {expert_id}: {e}", exc_info=True)
                
                processing_dialog.close()
                
                # Report results
                if total_processed > 0:
                    ui.notify(
                        f'Risk Management completed!\n'
                        f'• Experts processed: {experts_processed}\n'
                        f'• Orders updated: {total_processed}\n'
                        f'Check the Account Overview tab to review and submit orders.',
                        type='positive',
                        close_button=True,
                        timeout=7000
                    )
                else:
                    ui.notify(
                        'No orders were updated. All orders may already have quantities assigned or risk management criteria not met.',
                        type='info',
                        timeout=5000
                    )
                
                # Refresh the overview to update the display
                # Note: We would ideally call a refresh method here, but for now
                # the user can refresh manually or switch tabs
                
            except Exception as e:
                processing_dialog.close()
                logger.error(f"Error during risk management execution: {e}", exc_info=True)
                ui.notify(f'Error running risk management: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error in _handle_risk_management_from_overview: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _handle_risk_management_from_overview_by_ids(self, order_ids):
        """Handle risk management execution from overview page using order IDs.
        
        This method fetches fresh order data from the database using the provided IDs,
        avoiding JSON serialization issues with captured database objects.
        """
        try:
            if not order_ids:
                ui.notify('No pending orders to process', type='info')
                return
            
            # Fetch fresh order data from database
            session = get_db()
            try:
                pending_orders = []
                for order_id in order_ids:
                    order = session.get(TradingOrder, order_id)
                    if order:
                        pending_orders.append(order)
            finally:
                session.close()
            
            # Delegate to the original method
            self._handle_risk_management_from_overview(pending_orders)
            
        except Exception as e:
            logger.error(f"Error in _handle_risk_management_from_overview_by_ids: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _show_quantity_correction_dialog_by_ids(self, mismatch_data):
        """Show dialog to adjust transaction quantities using transaction IDs.
        
        This method fetches fresh transaction data from the database,
        avoiding JSON serialization issues with captured database objects.
        """
        from ...core.models import Transaction
        from ...core.db import get_db
        
        try:
            # Fetch fresh transaction data from database
            session = get_db()
            try:
                transactions = []
                for txn_id in mismatch_data['transaction_ids']:
                    txn = session.get(Transaction, txn_id)
                    if txn:
                        transactions.append(txn)
            finally:
                session.close()
            
            # Reconstruct mismatch dictionary with fresh transaction objects
            mismatch = {
                'account': mismatch_data['account'],
                'account_id': mismatch_data['account_id'],
                'symbol': mismatch_data['symbol'],
                'broker_qty': mismatch_data['broker_qty'],
                'transaction_qty': mismatch_data['transaction_qty'],
                'difference': mismatch_data['difference'],
                'transactions': transactions
            }
            
            # Delegate to the original method
            self._show_quantity_correction_dialog(mismatch)
            
        except Exception as e:
            logger.error(f"Error in _show_quantity_correction_dialog_by_ids: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')


class AccountOverviewTab:
    def __init__(self):
        self.render()
        pass

    def render(self):
        logger.debug("[RENDER] AccountOverviewTab.render() - START")
        accounts = get_all_instances(AccountDefinition)
        all_positions = []
        # Keep unformatted positions for chart calculations
        all_positions_raw = []
        
        for acc in accounts:
            provider_cls = providers.get(acc.provider)
            if provider_cls:
                provider_obj = provider_cls(acc.id)
                try:
                    positions = provider_obj.get_positions()
                    # Attach account name to each position for clarity
                    for pos in positions:
                        pos_dict = pos if isinstance(pos, dict) else dict(pos)
                        pos_dict['account'] = acc.name
                        
                        # Keep raw copy for chart
                        all_positions_raw.append(pos_dict.copy())
                        
                        # Format all float values to 2 decimal places for display
                        for k, v in pos_dict.items():
                            if isinstance(v, float):
                                pos_dict[k] = f"{v:.2f}"
                        all_positions.append(pos_dict)
                except Exception as e:
                    all_positions.append({'account': acc.name, 'error': str(e)})
        
        columns = [
            {'name': 'account', 'label': 'Account', 'field': 'account'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol'},
            {'name': 'exchange', 'label': 'Exchange', 'field': 'exchange'},
            {'name': 'asset_class', 'label': 'Asset Class', 'field': 'asset_class'},
            {'name': 'side', 'label': 'Side', 'field': 'side'},
            {'name': 'qty', 'label': 'Quantity', 'field': 'qty'},
            {'name': 'current_price', 'label': 'Current Price', 'field': 'current_price'},
            {'name': 'avg_entry_price', 'label': 'Entry Price', 'field': 'avg_entry_price'},
            {'name': 'market_value', 'label': 'Market Value', 'field': 'market_value'},
            {'name': 'unrealized_pl', 'label': 'Unrealized P/L', 'field': 'unrealized_pl'},
            {'name': 'unrealized_plpc', 'label': 'P/L %', 'field': 'unrealized_plpc'},
            {'name': 'change_today', 'label': 'Today Change %', 'field': 'change_today'}
        ]
        # Open Positions Table
        with ui.card():
            ui.label('Open Positions Across All Accounts').classes('text-h6 mb-4')
            ui.table(columns=columns, rows=all_positions, row_key='account').classes('w-full')
        
        # All Orders Table
        with ui.card().classes('mt-4'):
            ui.label('Recent Orders from All Accounts (Past 15 Days)').classes('text-h6 mb-4')
            self._render_live_orders_table()
        
        # Pending Orders Table
        with ui.card().classes('mt-4'):
            ui.label('Pending Orders (PENDING, WAITING_TRIGGER, or ERROR)').classes('text-h6 mb-4')
            self._render_pending_orders_table()
    
    def _render_live_orders_table(self):
        """Render table with recent orders from database (past 15 days) with expert information."""
        session = get_db()
        all_orders = []
        
        # Calculate cutoff date (15 days ago) - use UTC to avoid timezone issues
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=15)
        
        try:
            # Query orders from database with joins to get expert information
            # Exclude PENDING and WAITING_TRIGGER orders (they have their own section)
            statement = (
                select(TradingOrder, AccountDefinition, ExpertRecommendation, ExpertInstance)
                .join(AccountDefinition, TradingOrder.account_id == AccountDefinition.id)
                .outerjoin(ExpertRecommendation, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
                .outerjoin(ExpertInstance, ExpertRecommendation.instance_id == ExpertInstance.id)
                .where(TradingOrder.created_at >= cutoff_date)
                .where(TradingOrder.status.not_in([OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]))
                .order_by(TradingOrder.created_at.desc())
            )
            
            results = session.exec(statement).all()
            
            for order, account, recommendation, expert_instance in results:
                # Get expert name with ID if available
                expert_name = ""
                if expert_instance:
                    # Format: ExpertType-ID (e.g., "TradingAgents-1")
                    base_name = expert_instance.user_description or expert_instance.expert
                    expert_name = f"{base_name}-{expert_instance.id}"
                elif not recommendation:
                    expert_name = "Manual"  # No recommendation means manual order
                
                # Format the order data
                order_dict = {
                    'account': account.name,
                    'provider': account.provider,
                    'symbol': order.symbol,
                    'side': order.side.value if hasattr(order.side, 'value') else str(order.side),
                    'qty': f"{order.quantity:.2f}" if order.quantity else "",
                    'order_type': order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type),
                    'status': order.status.value if hasattr(order.status, 'value') else str(order.status),
                    'limit_price': f"${order.limit_price:.2f}" if order.limit_price else "",
                    'filled_qty': f"{order.filled_qty:.2f}" if order.filled_qty else "",
                    'created_at': order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else "",
                    'comment': order.comment or "",
                    'expert': expert_name
                }
                
                all_orders.append(order_dict)
                
        except Exception as e:
            logger.error(f"Error fetching orders from database: {e}", exc_info=True)
            all_orders.append({
                'account': 'ERROR',
                'provider': '',
                'symbol': 'ERROR',
                'side': str(e),
                'qty': '',
                'order_type': '',
                'status': '',
                'limit_price': '',
                'filled_price': '',
                'created_at': '',
                'comment': '',
                'expert': ''
            })
        finally:
            session.close()
        
        # Define columns for orders table
        order_columns = [
            {'name': 'created_at', 'label': 'Date', 'field': 'created_at', 'align': 'left'},
            {'name': 'account', 'label': 'Account', 'field': 'account', 'align': 'left'},
            {'name': 'provider', 'label': 'Provider', 'field': 'provider', 'align': 'left'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
            {'name': 'side', 'label': 'Side', 'field': 'side', 'align': 'center'},
            {'name': 'qty', 'label': 'Quantity', 'field': 'qty', 'align': 'right'},
            {'name': 'order_type', 'label': 'Order Type', 'field': 'order_type', 'align': 'center'},
            {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center'},
            {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price', 'align': 'right'},
            {'name': 'filled_qty', 'label': 'Filled Qty', 'field': 'filled_qty', 'align': 'right'},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert', 'align': 'left'},
            {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'}
        ]
        
        if all_orders:
            ui.table(
                columns=order_columns, 
                rows=all_orders, 
                row_key='symbol',
                pagination={'rowsPerPage': 20, 'sortBy': 'created_at', 'descending': True}
            ).classes('w-full')
        else:
            ui.label('No orders found or no accounts configured.').classes('text-gray-500')
    
    
    def _render_pending_orders_table(self):
        """Render table with pending orders from database (PENDING, WAITING_TRIGGER, or ERROR)."""
        # Create a container that can be refreshed
        self.pending_orders_container = ui.column().classes('w-full')
        with self.pending_orders_container:
            self._render_pending_orders_content()
    
    def _render_pending_orders_content(self):
        """Render the actual pending orders table content."""
        session = get_db()
        try:
            # Get orders with PENDING, WAITING_TRIGGER, or ERROR status
            pending_statuses = [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER, OrderStatus.ERROR]
            statement = (
                select(TradingOrder)
                .where(TradingOrder.status.in_(pending_statuses))
                .order_by(TradingOrder.created_at.desc())
            )
            orders_tuples = session.exec(statement).all()
            # Unpack tuples to get actual order objects
            orders = [order[0] if isinstance(order, tuple) else order for order in orders_tuples if order]
            
            if not orders:
                ui.label('No pending orders found.').classes('text-gray-500')
                return
            
            # Prepare data for table
            rows = []
            for order in orders:
                # Get account name
                account_name = ""
                try:
                    account = get_instance(AccountDefinition, order.account_id)
                    if account:
                        account_name = account.name
                except Exception:
                    account_name = f"Account {order.account_id}"
                
                # Format dates
                created_at_str = order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else ''
                
                row = {
                    'order_id': order.id,
                    'account': account_name,
                    'symbol': order.symbol,
                    'side': order.side,
                    'quantity': f"{order.quantity:.2f}" if order.quantity else '',
                    'order_type': order.order_type,
                    'status': order.status.value,
                    'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                    'comment': order.comment or '',
                    'created_at': created_at_str,
                    'waited_status': order.depends_order_status_trigger if order.status == OrderStatus.WAITING_TRIGGER else '',
                    'can_submit': order.status == OrderStatus.PENDING and not order.broker_order_id
                }
                rows.append(row)
            
            # Define table columns
            columns = [
                {'name': 'order_id', 'label': 'Order ID', 'field': 'order_id'},
                {'name': 'account', 'label': 'Account', 'field': 'account'},
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol'},
                {'name': 'side', 'label': 'Side', 'field': 'side'},
                {'name': 'quantity', 'label': 'Quantity', 'field': 'quantity'},
                {'name': 'order_type', 'label': 'Order Type', 'field': 'order_type'},
                {'name': 'status', 'label': 'Status', 'field': 'status'},
                {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price'},
                {'name': 'comment', 'label': 'Comment', 'field': 'comment'},
                {'name': 'created_at', 'label': 'Created', 'field': 'created_at'},
                {'name': 'waited_status', 'label': 'Waited Status', 'field': 'waited_status'},
                {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
            ]
            
            # Enable multiple selection on the table
            table = ui.table(columns=columns, rows=rows, row_key='order_id', selection='multiple').classes('w-full')
            table.selected = []  # Initialize selected list
            
            # Add delete button above table (after table is created so we can bind to it)
            with table.add_slot('top-left'):
                with ui.row().classes('items-center gap-4'):
                    ui.button('Delete Selected Orders', 
                             icon='delete', 
                             on_click=lambda: self._handle_delete_selected_orders(table.selected))\
                        .props('color=red')\
                        .bind_enabled_from(table, 'selected', backward=lambda val: len(val) > 0)
                    ui.label().bind_text_from(table, 'selected', backward=lambda val: f'{len(val)} order(s) selected')\
                        .classes('text-sm text-gray-600')
            
            # Add submit button slot for pending orders
            table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn v-if="props.row.can_submit" 
                           icon="send" 
                           flat 
                           dense 
                           color="primary" 
                           @click="$parent.$emit('submit_order', props.row.order_id)"
                           title="Submit Order to Broker">
                        <q-tooltip>Submit Order</q-tooltip>
                    </q-btn>
                    <span v-else class="text-grey-5">—</span>
                </q-td>
            ''')
            
            # Handle submit order event
            table.on('submit_order', self._handle_submit_order)
            
        except Exception as e:
            logger.error(f"Error rendering pending orders table: {e}", exc_info=True)
            ui.label(f'Error loading pending orders: {str(e)}').classes('text-red-500')
        finally:
            session.close()
    
    def _handle_submit_order(self, event_data):
        """Handle submit order button click."""
        try:
            order_id = event_data.args if hasattr(event_data, 'args') else event_data
            
            # Get the order
            order = get_instance(TradingOrder, order_id)
            if not order:
                ui.notify('Order not found', type='negative')
                return
                
            if order.status != OrderStatus.PENDING:
                ui.notify('Can only submit orders with PENDING status', type='negative')
                return
                
            if order.broker_order_id:
                ui.notify('Order already submitted to broker', type='warning')
                return
            
            # Mark this order as manually submitted
            if order.open_type != OrderOpenType.MANUAL:
                from ...core.db import update_instance
                order.open_type = OrderOpenType.MANUAL
                update_instance(order)
            
            # Get the account
            account = get_instance(AccountDefinition, order.account_id)
            if not account:
                ui.notify('Account not found', type='negative')
                return
            
            # Submit the order through the account provider
            from ...modules.accounts import providers
            provider_cls = providers.get(account.provider)
            if not provider_cls:
                ui.notify(f'No provider found for {account.provider}', type='negative')
                return
                
            try:
                provider_obj = provider_cls(account.id)
                submitted_order = provider_obj.submit_order(order)
                
                if submitted_order:
                    ui.notify(f'Order {order_id} submitted successfully to {account.provider}', type='positive')
                    # Refresh the table
                    self.render()
                else:
                    ui.notify(f'Failed to submit order {order_id} to broker', type='negative')
                    
            except Exception as e:
                logger.error(f"Error submitting order {order_id}: {e}", exc_info=True)
                ui.notify(f'Error submitting order: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error handling submit order: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _handle_delete_selected_orders(self, selected_rows):
        """Handle deletion of selected pending orders."""
        try:
            if not selected_rows:
                ui.notify('No orders selected', type='warning')
                return
            
            # Get order IDs from selected rows
            order_ids = [row['order_id'] for row in selected_rows]
            
            # Confirmation dialog
            with ui.dialog() as dialog, ui.card():
                ui.label(f'Delete {len(order_ids)} order(s)?').classes('text-h6 mb-4')
                ui.label('This action cannot be undone. Are you sure you want to delete the selected orders?').classes('mb-4')
                
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Delete', on_click=lambda: self._confirm_delete_orders(order_ids, dialog)).props('color=red')
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error handling delete selected orders: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _confirm_delete_orders(self, order_ids, dialog):
        """Confirm and execute order deletion."""
        from ...core.db import delete_instance
        session = get_db()
        
        try:
            deleted_count = 0
            errors = []
            
            for order_id in order_ids:
                try:
                    # Get the order to check if it can be deleted
                    order = get_instance(TradingOrder, order_id)
                    if not order:
                        errors.append(f"Order {order_id} not found")
                        continue
                    
                    # Only allow deletion of PENDING, WAITING_TRIGGER, or ERROR orders
                    if order.status not in [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER, OrderStatus.ERROR]:
                        errors.append(f"Order {order_id} cannot be deleted (status: {order.status.value})")
                        continue
                    
                    # Don't delete orders that have been submitted to broker
                    if order.broker_order_id:
                        errors.append(f"Order {order_id} already submitted to broker, cannot delete")
                        continue
                    
                    # Delete the order
                    delete_instance(order, session)
                    deleted_count += 1
                    logger.info(f"Deleted pending order {order_id}: {order.symbol} {order.side} {order.quantity}")
                    
                except Exception as e:
                    logger.error(f"Error deleting order {order_id}: {e}", exc_info=True)
                    errors.append(f"Order {order_id}: {str(e)}")
            
            # Show results
            if deleted_count > 0:
                ui.notify(f'Successfully deleted {deleted_count} order(s)', type='positive')
            
            if errors:
                error_msg = '; '.join(errors[:3])  # Show first 3 errors
                if len(errors) > 3:
                    error_msg += f'... and {len(errors) - 3} more errors'
                ui.notify(f'Errors: {error_msg}', type='warning')
            
            # Close dialog and refresh the table
            dialog.close()
            
            # Refresh the pending orders table
            if hasattr(self, 'pending_orders_container'):
                self.pending_orders_container.clear()
                with self.pending_orders_container:
                    self._render_pending_orders_content()
            
        except Exception as e:
            logger.error(f"Error confirming delete orders: {e}", exc_info=True)
            ui.notify(f'Error deleting orders: {str(e)}', type='negative')
        finally:
            session.close()

class TransactionsTab:
    """Comprehensive transactions management tab with full control over positions."""
    
    def __init__(self):
        self.transactions_container = None
        self.transactions_table = None
        self.selected_transaction = None
        self.render()
    
    def _get_order_status_color(self, status):
        """Get color for order status badge."""
        from ...core.types import OrderStatus
        
        color_map = {
            OrderStatus.FILLED: 'green',
            OrderStatus.OPEN: 'blue',
            OrderStatus.PENDING: 'orange',
            OrderStatus.WAITING_TRIGGER: 'purple',
            OrderStatus.CANCELED: 'grey',
            OrderStatus.REJECTED: 'red',
            OrderStatus.ERROR: 'red',
            OrderStatus.EXPIRED: 'grey',
            OrderStatus.PARTIALLY_FILLED: 'teal',
            OrderStatus.CLOSED: 'grey',
        }
        return color_map.get(status, 'grey')
    
    def render(self):
        """Render the transactions tab with filtering and control options."""
        logger.debug("[RENDER] TransactionsTab.render() - START")
        with ui.card().classes('w-full'):
            with ui.row().classes('w-full items-center justify-between mb-4'):
                ui.label('💼 Transactions').classes('text-h6')
                
                # Filter controls
                with ui.row().classes('gap-2'):
                    self.status_filter = ui.select(
                        label='Status Filter',
                        options=['All', 'Open', 'Closed', 'Waiting'],
                        value='All',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-32')
                    
                    # Expert filter - will be populated dynamically
                    self.expert_filter = ui.select(
                        label='Expert',
                        options=['All'],
                        value='All',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')
                    
                    self.symbol_filter = ui.input(
                        label='Symbol',
                        placeholder='Filter by symbol...',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-40')
                    
                    ui.button('Refresh', icon='refresh', on_click=lambda: self._refresh_transactions()).props('outline')
            
            # Transactions table container
            self.transactions_container = ui.column().classes('w-full')
            self._render_transactions_table()
    
    def _refresh_transactions(self):
        """Refresh the transactions table."""
        logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")
        
        # If table doesn't exist yet, create it
        if not self.transactions_table:
            logger.debug("[REFRESH] Table doesn't exist, creating new table")
            self.transactions_container.clear()
            with self.transactions_container:
                self._render_transactions_table()
            return
        
        # Otherwise, just update the rows data
        try:
            new_rows = self._get_transactions_data()
            logger.debug(f"[REFRESH] Updating table with {len(new_rows)} rows")
            
            # Update the table rows
            self.transactions_table.rows.clear()
            self.transactions_table.rows.extend(new_rows)
            # Note: In NiceGUI 3.0+, .update() is automatic when modifying table.rows
            # self.transactions_table.update()  # Not needed in 3.0+
            
            logger.debug("[REFRESH] _refresh_transactions() - Complete")
        except Exception as e:
            logger.error(f"Error refreshing transactions table: {e}", exc_info=True)
            # If update fails, recreate the table
            logger.debug("[REFRESH] Update failed, recreating table")
            self.transactions_container.clear()
            with self.transactions_container:
                self._render_transactions_table()
    
    def _get_transactions_data(self):
        """Get transactions data for the table.
        
        Returns:
            List of row dictionaries for the table
        """
        from ...core.models import Transaction, ExpertInstance
        from ...core.types import TransactionStatus
        from sqlmodel import col
        
        session = get_db()
        try:
            # Populate expert filter options if not already done
            if hasattr(self, 'expert_filter'):
                # Get all unique experts from transactions
                expert_statement = select(ExpertInstance).join(
                    Transaction, Transaction.expert_id == ExpertInstance.id
                ).distinct()
                experts = list(session.exec(expert_statement).all())
                
                # Build expert options list with shortnames
                expert_options = ['All']
                expert_map = {'All': 'All'}
                for expert in experts:
                    # Create shortname: "expert_name-id" or use user_description if available
                    shortname = expert.user_description if expert.user_description else f"{expert.expert}-{expert.id}"
                    expert_options.append(shortname)
                    expert_map[shortname] = expert.id
                
                # Store the map for filtering
                self.expert_id_map = expert_map
                
                # Update expert filter options
                current_value = self.expert_filter.value
                self.expert_filter.options = expert_options
                if current_value not in expert_options:
                    self.expert_filter.value = 'All'
            
            # Build query based on filters - join with ExpertInstance for expert info
            statement = select(Transaction, ExpertInstance).outerjoin(
                ExpertInstance, Transaction.expert_id == ExpertInstance.id
            ).order_by(Transaction.created_at.desc())
            
            # Apply status filter
            status_value = self.status_filter.value if hasattr(self, 'status_filter') else 'All'
            if status_value != 'All':
                status_map = {
                    'Open': TransactionStatus.OPENED,
                    'Closed': TransactionStatus.CLOSED,
                    'Waiting': TransactionStatus.WAITING
                }
                statement = statement.where(Transaction.status == status_map[status_value])
            
            # Apply expert filter
            if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
                expert_id = self.expert_id_map.get(self.expert_filter.value)
                if expert_id and expert_id != 'All':
                    statement = statement.where(Transaction.expert_id == expert_id)
            
            # Apply symbol filter
            if hasattr(self, 'symbol_filter') and self.symbol_filter.value:
                statement = statement.where(Transaction.symbol.contains(self.symbol_filter.value.upper()))
            
            # Execute query and separate transaction and expert
            results = list(session.exec(statement).all())
            transactions = []
            transaction_experts = {}
            for txn, expert in results:
                transactions.append(txn)
                transaction_experts[txn.id] = expert
            
            if not transactions:
                ui.label('No transactions found.').classes('text-gray-500')
                return
            
            # Prepare table data
            logger.debug(f"[RENDER] _render_transactions_table() - Building rows for {len(transactions)} transactions")
            rows = []
            for txn in transactions:
                # Skip current price fetching on initial load to avoid blocking UI
                # Users can refresh to get latest prices if needed
                current_pnl = ''
                current_price_str = ''
                
                # Note: Removed synchronous price fetching to prevent UI freeze
                # The get_instrument_current_price() call was blocking the UI
                # Consider adding a refresh button or async loading if current prices are needed
                
                # Closed P/L - calculate from open/close prices
                closed_pnl = ''
                if txn.close_price and txn.open_price and txn.quantity:
                    if txn.quantity > 0:  # Long position
                        pnl_closed = (txn.close_price - txn.open_price) * abs(txn.quantity)
                    else:  # Short position
                        pnl_closed = (txn.open_price - txn.close_price) * abs(txn.quantity)
                    closed_pnl = f"${pnl_closed:+.2f}"
                
                # Status styling
                status_color = {
                    TransactionStatus.OPENED: 'green',
                    TransactionStatus.CLOSING: 'orange',
                    TransactionStatus.CLOSED: 'gray',
                    TransactionStatus.WAITING: 'orange'
                }.get(txn.status, 'gray')
                
                # Get expert shortname
                expert = transaction_experts.get(txn.id)
                expert_shortname = ''
                if expert:
                    expert_shortname = expert.user_description if expert.user_description else f"{expert.expert}-{expert.id}"
                
                # Get all orders for this transaction
                orders_data = []
                if txn.id:
                    try:
                        orders_statement = select(TradingOrder).where(
                            TradingOrder.transaction_id == txn.id
                        ).order_by(TradingOrder.created_at)
                        txn_orders = list(session.exec(orders_statement).all())
                        
                        for order in txn_orders:
                            order_type_display = order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type)
                            order_side_display = order.side.value if hasattr(order.side, 'value') else str(order.side)
                            order_status_display = order.status.value if hasattr(order.status, 'value') else str(order.status)
                            
                            # Determine if this is a TP/SL order
                            order_category = 'Entry'
                            if order.depends_on_order:
                                if 'TP' in order.comment or 'take_profit' in order.comment.lower() if order.comment else False:
                                    order_category = 'Take Profit'
                                elif 'SL' in order.comment or 'stop_loss' in order.comment.lower() if order.comment else False:
                                    order_category = 'Stop Loss'
                                else:
                                    order_category = 'Dependent'
                            
                            orders_data.append({
                                'id': order.id,
                                'type': order_type_display,
                                'side': order_side_display,
                                'category': order_category,
                                'quantity': f"{order.quantity:.2f}" if order.quantity else '0.00',
                                'filled_qty': f"{order.filled_qty:.2f}" if order.filled_qty else '0.00',
                                'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                                'stop_price': f"${order.stop_price:.2f}" if order.stop_price else '',
                                'status': order_status_display,
                                'status_color': self._get_order_status_color(order.status),
                                'broker_order_id': order.broker_order_id or '',
                                'created_at': order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else '',
                                'comment': order.comment or '',
                                'expert_recommendation_id': order.expert_recommendation_id,
                                'has_recommendation': order.expert_recommendation_id is not None
                            })
                    except Exception as e:
                        logger.error(f"Error loading orders for transaction {txn.id}: {e}")
                
                row = {
                    'id': txn.id,
                    'symbol': txn.symbol,
                    'expert': expert_shortname,
                    'quantity': f"{txn.quantity:+.2f}",
                    'open_price': f"${txn.open_price:.2f}" if txn.open_price else '',
                    'current_price': current_price_str,
                    'close_price': f"${txn.close_price:.2f}" if txn.close_price else '',
                    'take_profit': f"${txn.take_profit:.2f}" if txn.take_profit else '',
                    'stop_loss': f"${txn.stop_loss:.2f}" if txn.stop_loss else '',
                    'current_pnl': current_pnl,
                    'closed_pnl': closed_pnl,
                    'status': txn.status.value,
                    'status_color': status_color,
                    'created_at': txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else '',
                    'is_open': txn.status == TransactionStatus.OPENED,
                    'is_waiting': txn.status == TransactionStatus.WAITING,  # Track WAITING status
                    'is_closing': txn.status == TransactionStatus.CLOSING,  # Track CLOSING status
                    'orders': orders_data,  # Add orders for expansion
                    'order_count': len(orders_data)  # Show order count
                }
                rows.append(row)
            
            return rows
            
        except Exception as e:
            logger.error(f"Error getting transactions data: {e}", exc_info=True)
            return []
        finally:
            session.close()
    
    def _render_transactions_table(self):
        """Render the main transactions table."""
        logger.debug("[RENDER] _render_transactions_table() - START")
        
        # Get transactions data
        rows = self._get_transactions_data()
        
        if not rows:
            ui.label('No transactions found.').classes('text-gray-500')
            return
        
        # Table columns
        columns = [
            {'name': 'expand', 'label': '', 'field': 'expand', 'align': 'left'},  # Expand column
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert', 'align': 'left', 'sortable': True},
            {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'align': 'right', 'sortable': True},
            {'name': 'open_price', 'label': 'Open Price', 'field': 'open_price', 'align': 'right', 'sortable': True},
            {'name': 'current_price', 'label': 'Current', 'field': 'current_price', 'align': 'right'},
            {'name': 'close_price', 'label': 'Close Price', 'field': 'close_price', 'align': 'right'},
            {'name': 'take_profit', 'label': 'TP', 'field': 'take_profit', 'align': 'right'},
            {'name': 'stop_loss', 'label': 'SL', 'field': 'stop_loss', 'align': 'right'},
            {'name': 'current_pnl', 'label': 'Current P/L', 'field': 'current_pnl', 'align': 'right'},
            {'name': 'closed_pnl', 'label': 'Closed P/L', 'field': 'closed_pnl', 'align': 'right'},
            {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center', 'sortable': True},
            {'name': 'order_count', 'label': 'Orders', 'field': 'order_count', 'align': 'center'},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left', 'sortable': True},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
        ]
        
        # Create table with expansion enabled
        logger.debug(f"[RENDER] _render_transactions_table() - Creating table with {len(rows)} rows")
        self.transactions_table = ui.table(
            columns=columns, 
            rows=rows, 
            row_key='id',
            pagination={'rowsPerPage': 20}
        ).classes('w-full')
        
        # Add Quasar table props for expansion
        self.transactions_table.props('flat bordered')
        
        # Add expand button in first column
        self.transactions_table.add_slot('body-cell-expand', '''
            <q-td :props="props">
                <q-btn
                    size="sm"
                    color="primary"
                    round
                    dense
                    @click="props.expand = !props.expand"
                    :icon="props.expand ? 'expand_less' : 'expand_more'"
                />
            </q-td>
        ''')
        
        # Add expansion details showing orders
        self.transactions_table.add_slot('body', '''
                <q-tr :props="props">
                    <q-td v-for="col in props.cols" :key="col.name" :props="props">
                        <template v-if="col.name === 'expand'">
                            <q-btn
                                size="sm"
                                color="primary"
                                round
                                dense
                                @click="props.expand = !props.expand"
                                :icon="props.expand ? 'expand_less' : 'expand_more'"
                            />
                        </template>
                        <template v-else-if="col.name === 'status'">
                            <q-badge :color="props.row.status_color" :label="col.value" />
                        </template>
                        <template v-else-if="col.name === 'current_pnl'">
                            <span :class="col.value.startsWith('+') ? 'text-green-600 font-bold' : col.value.startsWith('-') ? 'text-red-600 font-bold' : ''">
                                {{ col.value }}
                            </span>
                        </template>
                        <template v-else-if="col.name === 'closed_pnl'">
                            <span :class="col.value.startsWith('+') ? 'text-green-600 font-bold' : col.value.startsWith('-') ? 'text-red-600 font-bold' : ''">
                                {{ col.value }}
                            </span>
                        </template>
                        <template v-else-if="col.name === 'actions'">
                            <q-btn v-if="props.row.is_open" 
                                   icon="edit" 
                                   size="sm" 
                                   flat 
                                   round 
                                   color="primary"
                                   @click="$parent.$emit('edit_transaction', props.row.id)"
                                   title="Adjust TP/SL"
                            />
                            <q-btn v-if="(props.row.is_open || props.row.is_waiting) && !props.row.is_closing" 
                                   icon="close" 
                                   size="sm" 
                                   flat 
                                   round 
                                   color="negative"
                                   @click="$parent.$emit('close_transaction', props.row.id)"
                                   :title="props.row.is_waiting ? 'Cancel Orders' : 'Close Position'"
                            />
                            <q-btn v-else-if="props.row.is_closing"
                                   icon="refresh"
                                   size="sm"
                                   flat
                                   round
                                   color="orange"
                                   @click="$parent.$emit('retry_close_transaction', props.row.id)"
                                   title="Retry Close (reset status and try again)"
                            />
                            <span v-else class="text-grey-5">—</span>
                        </template>
                        <template v-else>
                            {{ col.value }}
                        </template>
                    </q-td>
                </q-tr>
                <q-tr v-show="props.expand" :props="props" class="bg-blue-50">
                    <q-td colspan="100%">
                        <div class="q-pa-md">
                            <div class="text-subtitle2 q-mb-sm">📋 Related Orders ({{ props.row.order_count }})</div>
                            <q-markup-table flat bordered dense v-if="props.row.orders.length > 0">
                                <thead>
                                    <tr class="bg-grey-3">
                                        <th class="text-left">ID</th>
                                        <th class="text-left">Category</th>
                                        <th class="text-left">Type</th>
                                        <th class="text-left">Side</th>
                                        <th class="text-right">Quantity</th>
                                        <th class="text-right">Filled</th>
                                        <th class="text-right">Limit Price</th>
                                        <th class="text-right">Stop Price</th>
                                        <th class="text-center">Status</th>
                                        <th class="text-left">Broker ID</th>
                                        <th class="text-left">Created</th>
                                        <th class="text-left">Comment</th>
                                        <th class="text-center">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="order in props.row.orders" :key="order.id">
                                        <td class="text-left">{{ order.id }}</td>
                                        <td class="text-left">
                                            <q-badge :color="order.category === 'Entry' ? 'blue' : order.category === 'Take Profit' ? 'green' : order.category === 'Stop Loss' ? 'red' : 'grey'" 
                                                     :label="order.category" />
                                        </td>
                                        <td class="text-left">{{ order.type }}</td>
                                        <td class="text-left">
                                            <q-badge :color="order.side === 'BUY' ? 'positive' : 'negative'" :label="order.side" />
                                        </td>
                                        <td class="text-right">{{ order.quantity }}</td>
                                        <td class="text-right">{{ order.filled_qty }}</td>
                                        <td class="text-right">{{ order.limit_price }}</td>
                                        <td class="text-right">{{ order.stop_price }}</td>
                                        <td class="text-center">
                                            <q-badge :color="order.status_color" :label="order.status" />
                                        </td>
                                        <td class="text-left text-caption">{{ order.broker_order_id }}</td>
                                        <td class="text-left">{{ order.created_at }}</td>
                                        <td class="text-left text-caption">{{ order.comment }}</td>
                                        <td class="text-center">
                                            <q-btn v-if="order.has_recommendation" 
                                                   icon="info" 
                                                   size="sm" 
                                                   flat 
                                                   round 
                                                   color="primary"
                                                   @click="$parent.$emit('view_recommendation', order.expert_recommendation_id)"
                                                   title="View Expert Recommendation"
                                            />
                                            <span v-else class="text-grey-5">—</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </q-markup-table>
                            <div v-else class="text-grey-6 text-center q-pa-md">No orders found for this transaction</div>
                        </div>
                    </q-td>
                </q-tr>
        ''')
        
        # Handle events
        logger.debug("[RENDER] _render_transactions_table() - Setting up event handlers")
        self.transactions_table.on('edit_transaction', self._show_edit_dialog)
        self.transactions_table.on('close_transaction', self._show_close_dialog)
        self.transactions_table.on('retry_close_transaction', self._show_retry_close_dialog)
        self.transactions_table.on('view_recommendation', self._show_recommendation_dialog)
        logger.debug("[RENDER] _render_transactions_table() - END (success)")
    
    def _show_edit_dialog(self, event_data):
        """Show dialog to edit TP/SL for a transaction."""
        from ...core.models import Transaction
        
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return
        
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label(f'Adjust TP/SL for {txn.symbol}').classes('text-h6 mb-4')
            
            with ui.column().classes('w-full gap-4'):
                ui.label(f'Position: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm text-gray-600')
                
                tp_input = ui.number(
                    label='Take Profit Price',
                    value=txn.take_profit if txn.take_profit else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')
                
                sl_input = ui.number(
                    label='Stop Loss Price',
                    value=txn.stop_loss if txn.stop_loss else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')
                
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Update', on_click=lambda: self._update_tp_sl(
                        transaction_id, tp_input.value, sl_input.value, dialog
                    )).props('color=primary')
        
        dialog.open()
    
    def _update_tp_sl(self, transaction_id, tp_price, sl_price, dialog):
        """Update TP/SL for a transaction."""
        from ...core.models import Transaction
        from ...core.db import update_instance
        
        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return
            
            # Query for the first (opening) order associated with this transaction
            session = get_db()
            order_statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction_id
            ).order_by(TradingOrder.created_at).limit(1)
            order = session.exec(order_statement).first()
            
            if not order or not order.account_id:
                ui.notify('No orders linked to this transaction or order account not found', type='negative')
                return
            
            # Get account to use set_order_tp/set_order_sl methods
            from ...modules.accounts import get_account_class
            from ...core.models import AccountDefinition
            
            acc_def = get_instance(AccountDefinition, order.account_id)
            if not acc_def:
                ui.notify('Account definition not found', type='negative')
                return
            
            account_class = get_account_class(acc_def.provider)
            if not account_class:
                ui.notify(f'Account provider {acc_def.provider} not found', type='negative')
                return
            
            account = account_class(acc_def.id)
            
            # Update TP if changed
            if tp_price and tp_price != txn.take_profit:
                try:
                    account.set_order_tp(order, tp_price)
                    ui.notify(f'Take Profit updated to ${tp_price:.2f}', type='positive')
                except Exception as e:
                    ui.notify(f'Error updating TP: {str(e)}', type='negative')
                    logger.error(f"Error updating TP: {e}", exc_info=True)
            
            # Update SL if changed and method exists
            if sl_price and sl_price != txn.stop_loss:
                if hasattr(account, 'set_order_sl'):
                    try:
                        account.set_order_sl(order, sl_price)
                        ui.notify(f'Stop Loss updated to ${sl_price:.2f}', type='positive')
                    except Exception as e:
                        ui.notify(f'Error updating SL: {str(e)}', type='negative')
                        logger.error(f"Error updating SL: {e}", exc_info=True)
                else:
                    # Manually update transaction if method doesn't exist
                    txn.stop_loss = sl_price
                    update_instance(txn)
                    ui.notify(f'Stop Loss updated to ${sl_price:.2f} (in transaction only)', type='info')
            
            dialog.close()
            self._refresh_transactions()
            
        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error updating TP/SL: {e}", exc_info=True)
    
    def _show_retry_close_dialog(self, event_data):
        """Show dialog to retry closing a transaction stuck in CLOSING status."""
        from ...core.models import Transaction
        from ...core.types import TransactionStatus
        
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return
        
        if txn.status != TransactionStatus.CLOSING:
            ui.notify('Transaction is not in CLOSING status', type='warning')
            return
        
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('⚠️ Retry Close Transaction').classes('text-h6 mb-4')
            
            ui.label('This transaction is stuck in CLOSING status.').classes('mb-2')
            ui.label(f'{txn.symbol}: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm font-bold mb-2')
            
            ui.separator().classes('my-4')
            
            ui.label('This will:').classes('font-bold mb-2')
            with ui.column().classes('ml-4 mb-4'):
                ui.label('1. Reset status back to OPENED/WAITING').classes('text-sm')
                ui.label('2. Allow you to retry closing the position').classes('text-sm')
                ui.label('3. You can then click Close again').classes('text-sm')
            
            ui.label('⚠️ Use this if the close operation failed or got stuck.').classes('text-sm text-orange mb-4')
            
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Reset & Retry', on_click=lambda: self._retry_close_position(transaction_id, dialog)).props('color=orange')
        
        dialog.open()
    
    def _retry_close_position(self, transaction_id, dialog):
        """Reset transaction status from CLOSING to allow retry, then close using AccountInterface."""
        from ...core.models import Transaction, TradingOrder
        from ...core.types import TransactionStatus, OrderStatus
        from ...core.db import update_instance
        
        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return
            
            if txn.status != TransactionStatus.CLOSING:
                ui.notify('Transaction is not in CLOSING status', type='warning')
                dialog.close()
                return
            
            # Get account interface
            session = get_db()
            orders_statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction_id
            ).limit(1)
            first_order = session.exec(orders_statement).first()
            
            if not first_order or not first_order.account_id:
                ui.notify('Cannot find account for transaction', type='negative')
                return
            
            from ...modules.accounts import get_account_class
            from ...core.models import AccountDefinition
            
            acc_def = get_instance(AccountDefinition, first_order.account_id)
            if not acc_def:
                ui.notify('Account not found', type='negative')
                return
            
            account_class = get_account_class(acc_def.provider)
            if not account_class:
                ui.notify(f'Account provider not found', type='negative')
                return
            
            account = account_class(acc_def.id)
            
            # Capture client for background task
            from nicegui import context
            client = context.client
            
            # Use async AccountInterface close_transaction method (handles retry logic and refresh)
            logger.info(f"Retrying close for transaction {transaction_id}")
            
            async def retry_close_async():
                try:
                    result = await account.close_transaction_async(transaction_id)
                    
                    # Use client.safe_invoke for UI updates from background task
                    def show_result():
                        if result['success']:
                            ui.notify(result['message'], type='positive')
                            logger.info(f"Retry close transaction {transaction_id}: {result['message']}")
                        else:
                            ui.notify(result['message'], type='negative')
                            logger.error(f"Retry close transaction {transaction_id} failed: {result['message']}")
                        self._refresh_transactions()
                    
                    client.safe_invoke(show_result)
                    
                except Exception as e:
                    # Schedule error notification via client
                    def show_error():
                        ui.notify(f'Error during retry close: {str(e)}', type='negative')
                    client.safe_invoke(show_error)
                    logger.error(f"Error in retry_close_async: {e}", exc_info=True)
            
            # Run async operation using background_tasks
            from nicegui import background_tasks
            background_tasks.create(retry_close_async(), name=f'retry_close_{transaction_id}')
            
            dialog.close()
            # Show immediate feedback
            ui.notify('Closing transaction...', type='info')
            
        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error retrying close position: {e}", exc_info=True)
    
    def _show_close_dialog(self, event_data):
        """Show confirmation dialog before closing a position."""
        from ...core.models import Transaction
        
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return
        
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label(f'Close Position').classes('text-h6 mb-4')
            
            ui.label(f'Are you sure you want to close this position?').classes('mb-2')
            ui.label(f'{txn.symbol}: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm font-bold mb-4')
            
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Close Position', on_click=lambda: self._close_position(transaction_id, dialog)).props('color=negative')
        
        dialog.open()
    
    def _close_position(self, transaction_id, dialog):
        """
        Close a position using AccountInterface.close_transaction method.
        This handles all closing logic including:
        - Canceling unfilled orders
        - Deleting WAITING_TRIGGER orders
        - Checking for existing close orders
        - Creating new closing orders if needed
        """
        from ...core.models import Transaction, TradingOrder
        from ...core.types import TransactionStatus
        
        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return
            
            # Check if transaction is already being closed
            if txn.status == TransactionStatus.CLOSING:
                ui.notify('Transaction is already being closed', type='warning')
                dialog.close()
                return
            
            # Get account from first order
            session = get_db()
            order_statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction_id
            ).limit(1)
            first_order = session.exec(order_statement).first()
            
            if not first_order or not first_order.account_id:
                ui.notify('Cannot find account for transaction', type='negative')
                return
            
            # Get account interface
            from ...modules.accounts import get_account_class
            from ...core.models import AccountDefinition
            
            acc_def = get_instance(AccountDefinition, first_order.account_id)
            if not acc_def:
                ui.notify('Account not found', type='negative')
                return
            
            account_class = get_account_class(acc_def.provider)
            if not account_class:
                ui.notify(f'Account provider not found', type='negative')
                return
            
            account = account_class(acc_def.id)
            
            # Capture client for background task
            from nicegui import context
            client = context.client
            
            # Use async AccountInterface close_transaction method (includes refresh)
            logger.info(f"Closing transaction {transaction_id}")
            
            async def close_async():
                try:
                    result = await account.close_transaction_async(transaction_id)
                    
                    # Use client.safe_invoke for UI updates from background task
                    def show_result():
                        if result['success']:
                            ui.notify(result['message'], type='positive')
                            logger.info(f"Close transaction {transaction_id}: {result['message']}")
                        else:
                            ui.notify(result['message'], type='negative')
                            logger.error(f"Close transaction {transaction_id} failed: {result['message']}")
                        self._refresh_transactions()
                    
                    client.safe_invoke(show_result)
                    
                except Exception as e:
                    # Schedule error notification via client
                    def show_error():
                        ui.notify(f'Error during close: {str(e)}', type='negative')
                    client.safe_invoke(show_error)
                    logger.error(f"Error in close_async: {e}", exc_info=True)
            
            # Run async operation using background_tasks
            from nicegui import background_tasks
            background_tasks.create(close_async(), name=f'close_{transaction_id}')
            
            dialog.close()
            # Show immediate feedback
            ui.notify('Closing transaction...', type='info')
            
        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error closing position: {e}", exc_info=True)
    
    def _show_recommendation_dialog(self, event_data):
        """Show expert recommendation details in a dialog."""
        from ...core.models import ExpertRecommendation, ExpertInstance
        
        # Extract recommendation_id from event_data
        recommendation_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        if not recommendation_id:
            ui.notify('No recommendation ID provided', type='warning')
            return
        
        # Get the recommendation
        rec = get_instance(ExpertRecommendation, recommendation_id)
        if not rec:
            ui.notify('Recommendation not found', type='negative')
            return
        
        # Get expert instance
        expert = get_instance(ExpertInstance, rec.instance_id) if rec.instance_id else None
        expert_name = f"{expert.expert} (ID: {expert.id})" if expert else "Unknown Expert"
        
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label('📊 Expert Recommendation Details').classes('text-h6 mb-4')
            
            # Expert and symbol info
            with ui.row().classes('w-full mb-4'):
                with ui.card().classes('flex-1'):
                    ui.label('Expert').classes('text-caption text-grey-7')
                    ui.label(expert_name).classes('text-body1 font-bold')
                with ui.card().classes('flex-1'):
                    ui.label('Symbol').classes('text-caption text-grey-7')
                    ui.label(rec.symbol).classes('text-body1 font-bold')
            
            # Recommendation details
            with ui.grid(columns=2).classes('w-full gap-4 mb-4'):
                # Order recommendation
                with ui.card():
                    ui.label('Recommendation').classes('text-caption text-grey-7')
                    rec_color = 'green' if rec.recommended_action.value == 'BUY' else 'red' if rec.recommended_action.value == 'SELL' else 'grey'
                    ui.badge(rec.recommended_action.value, color=rec_color).classes('text-body1')
                
                # Confidence
                with ui.card():
                    ui.label('Confidence').classes('text-caption text-grey-7')
                    confidence_pct = rec.confidence if rec.confidence else 0.0
                    ui.label(f'{confidence_pct:.1f}%').classes('text-body1 font-bold')
                
                # Expected profit
                with ui.card():
                    ui.label('Expected Profit').classes('text-caption text-grey-7')
                    profit_str = f'{rec.expected_profit_percent:+.2f}%' if rec.expected_profit_percent else 'N/A'
                    ui.label(profit_str).classes('text-body1 font-bold')
                
                # Price at date
                with ui.card():
                    ui.label('Price at Recommendation').classes('text-caption text-grey-7')
                    price_str = f'${rec.price_at_date:.2f}' if rec.price_at_date else 'N/A'
                    ui.label(price_str).classes('text-body1')
                
                # Time horizon
                if rec.time_horizon:
                    with ui.card():
                        ui.label('Time Horizon').classes('text-caption text-grey-7')
                        ui.label(rec.time_horizon.value).classes('text-body1')
                
                # Risk level
                if rec.risk_level:
                    with ui.card():
                        ui.label('Risk Level').classes('text-caption text-grey-7')
                        risk_color = 'red' if 'HIGH' in rec.risk_level.value else 'orange' if 'MEDIUM' in rec.risk_level.value else 'green'
                        ui.badge(rec.risk_level.value, color=risk_color).classes('text-body1')
            
            # Analysis/Reasoning
            if rec.details:
                with ui.card().classes('w-full mb-4'):
                    ui.label('Analysis').classes('text-caption text-grey-7 mb-2')
                    ui.label(rec.details).classes('text-body2 whitespace-pre-wrap')
            
            # Metadata
            with ui.expansion('Metadata', icon='info').classes('w-full'):
                with ui.grid(columns=2).classes('gap-2'):
                    ui.label('Recommendation ID:').classes('text-caption font-bold')
                    ui.label(str(rec.id)).classes('text-caption')
                    
                    ui.label('Created:').classes('text-caption font-bold')
                    created_str = rec.created_at.strftime('%Y-%m-%d %H:%M:%S') if rec.created_at else 'N/A'
                    ui.label(created_str).classes('text-caption')
                    
                    ui.label('Market Analysis ID:').classes('text-caption font-bold')
                    ui.label(str(rec.market_analysis_id) if rec.market_analysis_id else 'N/A').classes('text-caption')
            
            # Action buttons
            with ui.row().classes('w-full justify-between mt-4'):
                # Navigate to analysis button (only show if market_analysis_id exists)
                if rec.market_analysis_id:
                    ui.button('View Market Analysis', 
                             on_click=lambda: ui.navigate.to(f'/market_analysis/{rec.market_analysis_id}'),
                             icon='analytics').props('color=secondary')
                else:
                    ui.space()  # Empty space if no analysis link
                
                # Close button
                ui.button('Close', on_click=dialog.close).props('flat color=primary')
        
        dialog.open()

class PerformanceTab:
    """Performance analytics tab showing comprehensive trading metrics."""
    
    def __init__(self):
        self.render()
    
    def render(self):
        logger.debug("[RENDER] PerformanceTab.render() - START")
        
        # Get all accounts to allow filtering
        accounts = get_all_instances(AccountDefinition)
        
        if not accounts:
            with ui.card():
                ui.label('No accounts configured. Please add an account first.').classes('text-gray-500')
            return
        
        # Account selector
        with ui.card().classes('w-full mb-4'):
            ui.label('Account Selection').classes('text-lg font-bold mb-2')
            
            selected_account_id = accounts[0].id if accounts else None
            
            def render_performance_for_account(account_id: int):
                """Render performance analytics for selected account."""
                performance_container.clear()
                with performance_container:
                    # Import here to avoid circular dependency
                    from .performance import PerformanceTab as PerformanceAnalytics
                    analytics = PerformanceAnalytics(account_id)
                    analytics.render()
            
            # Account dropdown
            account_options = {acc.id: acc.name for acc in accounts}
            
            def on_account_change(e):
                render_performance_for_account(e.value)
            
            ui.select(
                options=account_options,
                value=selected_account_id,
                label='Select Account'
            ).on_value_change(on_account_change).classes('w-64')
        
        # Performance content container
        performance_container = ui.column().classes('w-full')
        
        # Initial render with first account
        if selected_account_id:
            render_performance_for_account(selected_account_id)

def content() -> None:
    logger.debug("[RENDER] overview.content() - START")
    # Tab configuration: (tab_name, tab_label)
    tab_config = [
        ('overview', 'Overview'),
        ('account', 'Account Overview'),
        ('transactions', 'Transactions'),
        ('performance', 'Performance')
    ]
    
    with ui.tabs() as tabs:
        tab_objects = {}
        for tab_name, tab_label in tab_config:
            tab_objects[tab_name] = ui.tab(tab_name, label=tab_label)

    with ui.tab_panels(tabs, value=tab_objects['overview']).classes('w-full'):
        logger.debug("[RENDER] overview.content() - Creating tab panels")
        with ui.tab_panel(tab_objects['overview']):
            OverviewTab(tabs_ref=tabs)
        with ui.tab_panel(tab_objects['account']):
            AccountOverviewTab()
        with ui.tab_panel(tab_objects['transactions']):
            TransactionsTab()
        with ui.tab_panel(tab_objects['performance']):
            PerformanceTab()
    
    # Setup HTML5 history navigation for tabs (NiceGUI 3.0 compatible)
    async def setup_tab_navigation():
        # In NiceGUI 3.0, ui.run_javascript automatically waits for client.connected()
        # So we use await to properly handle the async nature
        from nicegui import context
        await context.client.connected()
        await ui.run_javascript('''
            (function() {
                let isPopstateNavigation = false;
                
                // Map display labels to tab names
                const labelToName = {
                    'Overview': 'overview',
                    'Account Overview': 'account',
                    'Transactions': 'transactions',
                    'Performance': 'performance'
                };
                
                // Get tab name from tab element
                function getTabName(tab) {
                    const label = tab.textContent.trim();
                    return labelToName[label] || label.toLowerCase().replace(/\s+/g, '-');
                }
                
                // Handle browser back/forward buttons
                window.addEventListener('popstate', (e) => {
                    isPopstateNavigation = true;
                    const hash = window.location.hash.substring(1) || 'overview';
                    
                    // Find and click the correct tab
                    const tabs = document.querySelectorAll('.q-tab');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        if (tabName === hash) {
                            tab.click();
                        }
                    });
                    
                    setTimeout(() => { isPopstateNavigation = false; }, 100);
                });
                
                // Setup click handlers for tabs to update URL
                function setupTabClickHandlers() {
                    const tabs = document.querySelectorAll('.q-tab');
                    console.log('Found', tabs.length, 'tabs');
                    tabs.forEach(tab => {
                        const tabName = getTabName(tab);
                        console.log('Setting up listener for tab:', tabName, '(label:', tab.textContent.trim() + ')');
                        tab.addEventListener('click', () => {
                            if (!isPopstateNavigation) {
                                console.log('Tab clicked:', tabName);
                                history.pushState({tab: tabName}, '', '#' + tabName);
                            }
                        });
                    });
                }
                
                // Handle initial page load with hash
                const hash = window.location.hash.substring(1);
                if (hash && hash !== 'overview') {
                    // Wait a bit for tabs to be fully rendered
                    setTimeout(() => {
                        const tabs = document.querySelectorAll('.q-tab');
                        tabs.forEach(tab => {
                            const tabName = getTabName(tab);
                            if (tabName === hash) {
                                console.log('Initial load: activating tab for hash:', hash);
                                tab.click();
                            }
                        });
                    }, 50);
                } else if (!hash) {
                    // Set initial hash if none exists
                    history.replaceState({tab: 'overview'}, '', '#overview');
                }
                
                setupTabClickHandlers();
            })();
        ''', timeout=3.0)
    
    # Use timer to run async setup (shorter delay since we explicitly wait for connection)
    ui.timer(0.1, setup_tab_navigation, once=True)

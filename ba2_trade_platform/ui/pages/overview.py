from nicegui import ui
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func
from typing import Dict, Any
import requests
import aiohttp
import asyncio
import json

from ...core.db import get_all_instances, get_db, get_instance, update_instance
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, ExpertInstance, AppSetting, TradingOrder, Transaction
from ...core.types import MarketAnalysisStatus, OrderRecommendation, OrderStatus, OrderOpenType
from ...core.utils import get_expert_instance_from_id, get_market_analysis_id_from_order_id, get_account_instance_from_id
from ...modules.accounts import providers
from ...logger import logger
from ..components import ProfitPerExpertChart, InstrumentDistributionChart, BalanceUsagePerExpertChart
from ..components.FloatingPLPerExpertWidget import FloatingPLPerExpertWidget
from ..components.FloatingPLPerAccountWidget import FloatingPLPerAccountWidget

class OverviewTab:
    def __init__(self, tabs_ref=None):
        self.tabs_ref = tabs_ref
        self.container = ui.column().classes('w-full')
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
                                    # Call refresh_orders with heuristic_mapping=True and fetch_all=True
                                    # fetch_all ensures pagination to get ALL orders from broker
                                    if account.refresh_orders(heuristic_mapping=True, fetch_all=True):
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
                        tabs_ref.set_value('account')
                
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
                try:
                    provider_obj = get_account_instance_from_id(acc.id)
                    if not provider_obj:
                        continue
                    
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
                    try:
                        provider_obj = get_account_instance_from_id(acc.id)
                        if not provider_obj:
                            continue
                        
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
        
        # Clear container and rebuild
        self.container.clear()
        
        with self.container:
            # Add refresh button at the top
            with ui.row().classes('w-full justify-end mb-2'):
                ui.button('🔄 Refresh', on_click=lambda: self.render()).props('flat color=primary')
            
            # Check for ERROR orders and display alert
            self._check_and_display_error_orders()
            
            # Create container for quantity mismatch alerts
            self.mismatch_alerts_container = ui.column().classes('w-full')
            
            # Check for quantity mismatches asynchronously
            asyncio.create_task(self._check_and_display_quantity_mismatches_async())
            
            # Check for PENDING orders and display notification
            self._check_and_display_pending_orders(self.tabs_ref)
            
            with ui.grid(columns=4).classes('w-full gap-4'):
                # Row 1: API Usage, Analysis Jobs, Order Statistics, and Order Recommendations
                self._render_api_usage_widget()
                self._render_analysis_jobs_widget()
                self._render_order_statistics_widget()
                with ui.column().classes(''):
                    self._render_order_recommendations_widget()
            
            # Row 2: Profit Per Expert and Balance Usage Per Expert (double width each)
            with ui.grid(columns=2).classes('w-full gap-4'):
                ProfitPerExpertChart()
                BalanceUsagePerExpertChart()
            
            with ui.grid(columns=4).classes('w-full gap-4'):
                # Row 3: Floating P/L widgets
                FloatingPLPerExpertWidget()
                FloatingPLPerAccountWidget()
                
                # Row 4: Position Distribution by Label
                self._render_position_distribution_widget(grouping_field='labels')
                
                # Row 4: Position Distribution by Category (can span or be paired with other widgets)
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
                try:
                    provider_obj = get_account_instance_from_id(acc.id)
                    if provider_obj:
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
    
    def _render_api_usage_widget(self):
        """Widget showing API usage for OpenAI and Naga AI."""
        with ui.card().classes('p-4'):
            ui.label('💰 API Usage').classes('text-h6 mb-4')
            
            # Create loading placeholder and load data asynchronously
            loading_label = ui.label('🔄 Loading usage data...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')
            
            # Load data asynchronously
            asyncio.create_task(self._load_api_usage_data(loading_label, content_container))
    
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
    
    async def _load_api_usage_data(self, loading_label, content_container):
        """Load API usage data for both OpenAI and Naga AI asynchronously and update UI."""
        try:
            # Fetch data from both providers concurrently
            openai_data_task = asyncio.create_task(self._get_openai_usage_data_async())
            naga_ai_data_task = asyncio.create_task(self._get_naga_ai_usage_data_async())
            
            openai_data, naga_ai_data = await asyncio.gather(openai_data_task, naga_ai_data_task)
            
            # Clear loading message - check if client still exists
            try:
                loading_label.delete()
            except RuntimeError:
                # Client has been deleted (user navigated away), stop processing
                return
            
            # Populate content - check if client still exists
            try:
                with content_container:
                    # OpenAI Section
                    ui.label('🤖 OpenAI').classes('text-subtitle2 font-bold mb-2')
                    
                    if openai_data.get('error'):
                        ui.label('⚠️ Error fetching usage data').classes('text-sm text-red-600 mb-2')
                        error_message = openai_data['error']
                        
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
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('Last Week:').classes('text-xs')
                            week_cost = openai_data.get('week_cost', 0)
                            ui.label(f'${week_cost:.2f}').classes('text-xs font-bold text-orange-600')
                        
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('Last Month:').classes('text-xs')
                            month_cost = openai_data.get('month_cost', 0)
                            ui.label(f'${month_cost:.2f}').classes('text-xs font-bold text-red-600')
                        
                        # Show remaining credit only if available
                        remaining = openai_data.get('remaining_credit')
                        if remaining is not None:
                            with ui.row().classes('w-full justify-between items-center mb-1'):
                                ui.label('Remaining:').classes('text-xs')
                                ui.label(f'${remaining:.2f}').classes('text-xs font-bold text-green-600')
                    
                    # Separator between providers
                    ui.separator().classes('my-3')
                    
                    # Naga AI Section
                    ui.label('🌊 Naga AI').classes('text-subtitle2 font-bold mb-2')
                    
                    if naga_ai_data.get('error'):
                        ui.label('⚠️ Error fetching usage data').classes('text-sm text-red-600 mb-2')
                        error_message = naga_ai_data['error']
                        ui.label(error_message).classes('text-xs text-gray-500')
                    else:
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('Last Week:').classes('text-xs')
                            week_cost = naga_ai_data.get('week_cost', 0)
                            ui.label(f'${week_cost:.2f}').classes('text-xs font-bold text-orange-600')
                        
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label('Last Month:').classes('text-xs')
                            month_cost = naga_ai_data.get('month_cost', 0)
                            ui.label(f'${month_cost:.2f}').classes('text-xs font-bold text-red-600')
                        
                        # Show remaining credit only if available
                        remaining = naga_ai_data.get('remaining_credit')
                        if remaining is not None:
                            with ui.row().classes('w-full justify-between items-center mb-1'):
                                ui.label('Balance:').classes('text-xs')
                                ui.label(f'${remaining:.2f}').classes('text-xs font-bold text-green-600')
                    
                    # Show last updated timestamp (use most recent)
                    ui.separator().classes('my-2')
                    last_updated = openai_data.get('last_updated', naga_ai_data.get('last_updated', 'Unknown'))
                    ui.label(f'Last updated: {last_updated}').classes('text-xs text-gray-500')
                    
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
            
            logger.debug(f'[OpenAI Usage] Calling {costs_url} with start_time={params["start_time"]}, end_time={params["end_time"]}')
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(costs_url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                        logger.debug(f'[OpenAI Usage] Response status: {response.status}')
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
                            logger.error(f'OpenAI API error {response.status}: {error_text}')
                            # For 500 errors, provide a more helpful message
                            if response.status == 500:
                                return {'error': 'OpenAI server error (500) - their API may be experiencing issues. Try again later.'}
                            else:
                                return {'error': f'OpenAI API error ({response.status}): {error_text[:150]}...'}
                            
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
            logger.error('Request timeout - OpenAI API not responding')
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
                logger.debug(f'[OpenAI Usage] Sync response status: {response.status_code}')
                
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
                    logger.error(f'OpenAI API error {response.status_code}: {error_text}')
                    # For 500 errors, provide a more helpful message
                    if response.status_code == 500:
                        return {'error': 'OpenAI server error (500) - their API may be experiencing issues. Try again later.'}
                    else:
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

    async def _get_naga_ai_usage_data_async(self) -> Dict[str, Any]:
        """Fetch Naga AI usage data from the API asynchronously."""
        try:
            # Get Naga AI admin API key from app settings
            session = get_db()
            try:
                admin_key_setting = session.exec(
                    select(AppSetting).where(AppSetting.key == 'naga_ai_admin_api_key')
                ).first()
                
                if not admin_key_setting or not admin_key_setting.value_str:
                    return {'error': 'Naga AI Admin API key not configured in settings'}
                
                api_key = admin_key_setting.value_str
                
            finally:
                session.close()
            
            # Calculate date ranges
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            # Fetch usage data from Naga AI API
            headers = {
                'Authorization': f'Bearer {api_key}'
            }
            
            try:
                async with aiohttp.ClientSession() as session:
                    # Get account balance
                    balance_url = 'https://api.naga.ac/v1/account/balance'
                    balance_data = None
                    
                    async with session.get(balance_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            balance_data = await response.json()
                        elif response.status == 401:
                            return {'error': 'Invalid Naga AI Admin API key'}
                        else:
                            error_text = await response.text()
                            logger.error(f'Naga AI balance API error {response.status}: {error_text}')
                    
                    # Get account activity
                    activity_url = 'https://api.naga.ac/v1/account/activity'
                    activity_data = None
                    
                    async with session.get(activity_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            activity_data = await response.json()
                        elif response.status == 401:
                            return {'error': 'Invalid Naga AI Admin API key'}
                        else:
                            error_text = await response.text()
                            logger.error(f'Naga AI activity API error {response.status}: {error_text}')
                    
                    # Process the data
                    week_cost = 0
                    month_cost = 0
                    remaining_credit = None
                    
                    # Extract balance information
                    if balance_data:
                        balance_str = balance_data.get('balance', '0')
                        try:
                            remaining_credit = float(balance_str)
                        except (ValueError, TypeError):
                            remaining_credit = 0
                    
                    # Extract activity/usage information
                    if activity_data:
                        # Check for daily_stats array (preferred for time-based calculation)
                        daily_stats = activity_data.get('daily_stats', [])
                        
                        if daily_stats:
                            for day_stat in daily_stats:
                                date_str = day_stat.get('date')
                                if date_str:
                                    try:
                                        # Parse date string
                                        try:
                                            day_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                        except:
                                            day_date = datetime.strptime(date_str, '%Y-%m-%d')
                                        
                                        # Extract cost (handle string format like "0E-10")
                                        cost_str = day_stat.get('total_cost', '0')
                                        try:
                                            cost = float(cost_str)
                                        except (ValueError, TypeError):
                                            cost = 0
                                        
                                        # Add to appropriate time periods
                                        if day_date >= week_ago:
                                            week_cost += abs(cost)
                                        if day_date >= month_ago:
                                            month_cost += abs(cost)
                                    except Exception as e:
                                        logger.debug(f"Error parsing daily stat: {e}")
                                        continue
                        else:
                            # Fallback to total_stats if no daily breakdown available
                            total_stats = activity_data.get('total_stats', {})
                            if total_stats:
                                total_cost_str = total_stats.get('total_cost', '0')
                                try:
                                    total_cost = float(total_cost_str)
                                    # Assume total is for the month if no daily stats
                                    month_cost = abs(total_cost)
                                    week_cost = month_cost  # Can't distinguish without daily data
                                except (ValueError, TypeError):
                                    pass
                    
                    return {
                        'week_cost': week_cost,
                        'month_cost': month_cost,
                        'remaining_credit': remaining_credit,
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                        'note': 'Real Naga AI usage data'
                    }
                    
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling Naga AI API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except asyncio.TimeoutError:
            return {'error': 'Request timeout - Naga AI API not responding'}
        except aiohttp.ClientError as e:
            logger.error(f'Error fetching Naga AI usage data: {e}', exc_info=True)
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logger.error(f'Unexpected error fetching Naga AI usage data: {e}', exc_info=True)
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
                from ...core.utils import get_expert_instance_from_id
                risk_management = get_risk_management()
                
                total_processed = 0
                experts_processed = 0
                smart_manager_results = []
                
                # Run risk management for each expert
                for expert_id, expert_orders in orders_by_expert.items():
                    try:
                        # Load expert instance and check risk_manager_mode
                        expert_instance = get_expert_instance_from_id(expert_id)
                        if not expert_instance:
                            logger.warning(f"Expert instance {expert_id} not found, skipping")
                            continue
                        
                        # Check risk_manager_mode setting (default to "classic" if not set)
                        risk_manager_mode = expert_instance.settings.get("risk_manager_mode", "classic")
                        
                        if risk_manager_mode == "smart":
                            # Enqueue Smart Risk Manager job for this expert to dedicated queue
                            logger.info(f"Enqueueing Smart Risk Manager for expert {expert_id}")
                            try:
                                from ...core.SmartRiskManagerQueue import get_smart_risk_manager_queue
                                from ...core.db import get_instance
                                from ...core.models import ExpertInstance
                                
                                smart_queue = get_smart_risk_manager_queue()
                                
                                # Get expert record from database to access account_id
                                expert_record = get_instance(ExpertInstance, expert_id)
                                if not expert_record:
                                    logger.error(f"Expert instance {expert_id} not found in database")
                                    continue
                                
                                account_id = expert_record.account_id
                                
                                task_id = smart_queue.submit_task(expert_id, account_id)
                                
                                if task_id:
                                    smart_manager_results.append({
                                        "expert_id": expert_id,
                                        "task_id": task_id,
                                        "status": "enqueued"
                                    })
                                    experts_processed += 1
                                    logger.info(f"Smart Risk Manager job enqueued for expert {expert_id} (Task ID: {task_id})")
                                else:
                                    logger.warning(f"Smart Risk Manager job already running for expert {expert_id}")
                            except Exception as smart_error:
                                logger.error(f"Error enqueueing Smart Risk Manager for expert {expert_id}: {smart_error}", exc_info=True)
                        else:
                            # Run classic rule-based risk management
                            updated_orders = risk_management.review_and_prioritize_pending_orders(expert_id)
                            total_processed += len(updated_orders)
                            experts_processed += 1
                            logger.info(f"Processed {len(updated_orders)} orders for expert {expert_id} using classic risk management")
                            
                            # AUTO-SUBMIT: After risk management, auto-submit eligible orders to broker
                            # (matching the behavior of the automated WorkerQueue workflow)
                            try:
                                from ...core.models import ExpertInstance, AccountDefinition
                                from ...modules.accounts import get_account_class
                                
                                if expert_instance.account_id:
                                    account_def = get_instance(AccountDefinition, expert_instance.account_id)
                                    if account_def:
                                        account_class = get_account_class(account_def.provider)
                                        if account_class:
                                            account = account_class(account_def.id)
                                            
                                            # Auto-submit orders with quantity > 0
                                            submitted_count = 0
                                            for order in updated_orders:
                                                if order.quantity and order.quantity > 0:
                                                    try:
                                                        logger.info(f"Auto-submitting order {order.id} for {order.symbol}: {order.quantity} shares")
                                                        submitted_order = account.submit_order(order)
                                                        if submitted_order:
                                                            submitted_count += 1
                                                            logger.info(f"Successfully submitted order {order.id} to broker")
                                                        else:
                                                            logger.warning(f"Failed to submit order {order.id} to broker")
                                                    except Exception as submit_error:
                                                        logger.error(f"Error submitting order {order.id}: {submit_error}", exc_info=True)
                                            
                                            if submitted_count > 0:
                                                logger.info(f"Auto-submitted {submitted_count}/{len(updated_orders)} orders to broker")
                            except Exception as auto_submit_error:
                                logger.error(f"Error during auto-submission for expert {expert_id}: {auto_submit_error}", exc_info=True)
                    
                    except Exception as e:
                        logger.error(f"Error processing risk management for expert {expert_id}: {e}", exc_info=True)
                
                processing_dialog.close()
                
                # Report results
                if total_processed > 0 or smart_manager_results:
                    # Build notification message
                    message_parts = [f'Risk Management completed!']
                    message_parts.append(f'• Experts processed: {experts_processed}')
                    
                    if total_processed > 0:
                        message_parts.append(f'• Orders updated (classic): {total_processed}')
                    
                    if smart_manager_results:
                        total_smart_jobs = len(smart_manager_results)
                        message_parts.append(f'• Smart Manager jobs enqueued: {total_smart_jobs}')
                    
                    if total_processed > 0:
                        message_parts.append('Check the Account Overview tab to review and submit orders.')
                    
                    if smart_manager_results:
                        message_parts.append('Check the Job Monitoring page for Smart Risk Manager progress.')
                    
                    ui.notify(
                        '\n'.join(message_parts),
                        type='positive',
                        close_button=True,
                        timeout=7000
                    )
                    
                    # If there are smart manager results, show detailed summary with link to job monitoring
                    if smart_manager_results:
                        with ui.dialog() as smart_summary_dialog, ui.card().classes('w-96 max-h-96 overflow-auto'):
                            ui.label('Smart Risk Manager Jobs Enqueued').classes('text-h6 text-green-600')
                            ui.separator()
                            for result in smart_manager_results:
                                ui.label(f'Expert {result["expert_id"]} (Task {result["task_id"]}):').classes('text-sm font-bold mt-2')
                                ui.label(f'  Status: {result["status"]}').classes('text-sm')
                            ui.separator().classes('mt-2')
                            ui.label('Jobs are running in the background. Check Job Monitoring page for progress.').classes('text-xs text-gray-600')
                            with ui.row().classes('mt-2 gap-2'):
                                ui.button('Job Monitoring', on_click=lambda: ui.navigate.to('/marketanalysis#monitoring')).props('flat color=primary')
                                ui.button('Close', on_click=smart_summary_dialog.close).props('flat')
                        smart_summary_dialog.open()
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
        self.container = ui.column().classes('w-full')
        self.render()

    def render(self):
        logger.debug("[RENDER] AccountOverviewTab.render() - START")
        
        # Clear container and rebuild
        self.container.clear()
        
        with self.container:
            # Add refresh button at the top
            with ui.row().classes('w-full justify-end mb-2'):
                ui.button('🔄 Refresh', on_click=lambda: self.render()).props('flat color=primary')
            
            accounts = get_all_instances(AccountDefinition)
            all_positions = []
            # Keep unformatted positions for chart calculations
            all_positions_raw = []
            position_counter = 0  # Counter for unique row keys
            
            for acc in accounts:
                try:
                    provider_obj = get_account_instance_from_id(acc.id)
                    if provider_obj:
                        positions = provider_obj.get_positions()
                        # Attach account name to each position for clarity
                        for pos in positions:
                            pos_dict = pos if isinstance(pos, dict) else dict(pos)
                            pos_dict['account'] = acc.name
                            # Add unique row key combining account and symbol
                            pos_dict['_row_key'] = f"{acc.name}_{pos_dict.get('symbol', '')}_{position_counter}"
                            position_counter += 1
                            
                            # Keep raw copy for chart
                            all_positions_raw.append(pos_dict.copy())
                            
                            # Store raw numeric values for sorting, add formatted versions for display
                            for k, v in list(pos_dict.items()):
                                if isinstance(v, float):
                                    if k in ['unrealized_plpc', 'change_today', 'unrealized_intraday_plpc']:
                                        # Keep raw value for sorting, add display version
                                        pos_dict[f'{k}_display'] = f"{v * 100:.2f}%"
                                        pos_dict[k] = v * 100  # Store as percentage number for sorting
                                    else:
                                        # Keep raw value for sorting, add display version
                                        pos_dict[f'{k}_display'] = f"{v:.2f}"
                            all_positions.append(pos_dict)
                except Exception as e:
                    logger.warning(f"Failed to load account {acc.name} (ID: {acc.id}): {e}")
                    # Continue processing other accounts
            
            columns = [
                {'name': 'account', 'label': 'Account', 'field': 'account', 'sortable': True, 'align': 'left'},
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                {'name': 'exchange', 'label': 'Exchange', 'field': 'exchange', 'sortable': True, 'align': 'left'},
                {'name': 'asset_class', 'label': 'Asset Class', 'field': 'asset_class', 'sortable': True, 'align': 'left'},
                {'name': 'side', 'label': 'Side', 'field': 'side', 'sortable': True, 'align': 'center'},
                {'name': 'qty', 'label': 'Quantity', 'field': 'qty', 'sortable': True, 'align': 'right'},
                {'name': 'current_price', 'label': 'Current Price', 'field': 'current_price', 'sortable': True, 'align': 'right'},
                {'name': 'avg_entry_price', 'label': 'Entry Price', 'field': 'avg_entry_price', 'sortable': True, 'align': 'right'},
                {'name': 'market_value', 'label': 'Market Value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
                {'name': 'unrealized_pl', 'label': 'Unrealized P/L', 'field': 'unrealized_pl', 'sortable': True, 'align': 'right'},
                {'name': 'unrealized_plpc', 'label': 'P/L %', 'field': 'unrealized_plpc', 'sortable': True, 'align': 'right'},
                {'name': 'change_today', 'label': 'Today Change %', 'field': 'change_today', 'sortable': True, 'align': 'right'}
            ]
            # Open Positions Table
            with ui.card():
                ui.label('Open Positions Across All Accounts').classes('text-h6 mb-4')
                
                # Add built-in filter before the table
                with ui.row().classes('w-full gap-2 mb-4'):
                    filter_input = ui.input(label='Filter table', placeholder='Type to filter across all columns...').classes('flex-grow')
                    ui.button('Clear', on_click=lambda: filter_input.set_value('')).props('flat')
                
                # Create the table with sortable columns
                positions_table = ui.table(
                    columns=columns, 
                    rows=all_positions, 
                    row_key='_row_key',  # Unique key for each position
                    pagination={'rowsPerPage': 20, 'sortBy': 'account', 'descending': False}
                ).classes('w-full')
                
                # Bind filter to table after table is created
                filter_input.bind_value(positions_table, 'filter')
                
                # Add custom cell rendering for numeric columns with proper formatting
                # Quantity, prices - format to 2 decimals
                positions_table.add_slot('body-cell-qty', r'''
                    <q-td :props="props">
                        {{ props.row.qty_display || props.value.toFixed(2) }}
                    </q-td>
                ''')
                
                positions_table.add_slot('body-cell-current_price', r'''
                    <q-td :props="props">
                        {{ props.row.current_price_display || props.value.toFixed(2) }}
                    </q-td>
                ''')
                
                positions_table.add_slot('body-cell-avg_entry_price', r'''
                    <q-td :props="props">
                        {{ props.row.avg_entry_price_display || props.value.toFixed(2) }}
                    </q-td>
                ''')
                
                positions_table.add_slot('body-cell-market_value', r'''
                    <q-td :props="props">
                        {{ props.row.market_value_display || props.value.toFixed(2) }}
                    </q-td>
                ''')
                
                # Add custom cell rendering for P/L columns with color coding
                positions_table.add_slot('body-cell-unrealized_pl', r'''
                    <q-td :props="props">
                        <span :style="props.value >= 0 ? 'color: green; font-weight: 500;' : 'color: red; font-weight: 500;'">
                            {{ props.row.unrealized_pl_display || '$' + props.value.toFixed(2) }}
                        </span>
                    </q-td>
                ''')
                
                positions_table.add_slot('body-cell-unrealized_plpc', r'''
                    <q-td :props="props">
                        <span :style="props.value >= 0 ? 'color: green; font-weight: 500;' : 'color: red; font-weight: 500;'">
                            {{ props.row.unrealized_plpc_display || props.value.toFixed(2) + '%' }}
                        </span>
                    </q-td>
                ''')
                
                positions_table.add_slot('body-cell-change_today', r'''
                    <q-td :props="props">
                        <span :style="props.value >= 0 ? 'color: green; font-weight: 500;' : 'color: red; font-weight: 500;'">
                            {{ props.row.change_today_display || props.value.toFixed(2) + '%' }}
                        </span>
                    </q-td>
                ''')
                
                # Add totals row below table
                ui.separator().classes('my-2')
                
                # Calculate totals from all positions
                def calculate_totals():
                    total_qty = sum(float(pos['qty']) if isinstance(pos['qty'], (int, float)) else 0 for pos in all_positions)
                    total_pl = sum(float(pos['unrealized_pl']) if isinstance(pos['unrealized_pl'], (int, float)) else 0 for pos in all_positions)
                    total_mv = sum(float(pos['market_value']) if isinstance(pos['market_value'], (int, float)) else 0 for pos in all_positions)
                    return total_qty, total_pl, total_mv
                
                total_qty, total_pl, total_mv = calculate_totals()
                
                with ui.row().classes('w-full justify-end items-center gap-6 px-4 py-3 bg-gray-50 border-t-2 border-gray-300'):
                    ui.label('TOTAL:').classes('text-sm font-bold text-gray-700')
                    ui.label(f'Qty: {total_qty:.2f}').classes('text-sm font-semibold')
                    pl_color = 'text-green-600' if total_pl >= 0 else 'text-red-600'
                    ui.label(f'Unrealized P/L: ${total_pl:,.2f}').classes(f'text-sm font-bold {pl_color}')
                    ui.label(f'Market Value: ${total_mv:,.2f}').classes('text-sm font-semibold')
                    
                ui.label('Note: Total reflects all positions. Use filter to view subsets.').classes('text-xs text-gray-500 italic px-4 pb-2')
                
                # Compare with Broker button
                with ui.row().classes('w-full justify-end px-4 pb-3'):
                    ui.button('🔍 Compare with Broker', on_click=self._show_broker_comparison_dialog).props('outline color=primary')
            
            # All Orders Table
            with ui.card().classes('mt-4'):
                ui.label('Recent Orders from All Accounts (Past 15 Days)').classes('text-h6 mb-4')
                self._render_live_orders_table()
            
            # Pending Orders Table
            with ui.card().classes('mt-4'):
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label('Pending Orders (PENDING, WAITING_TRIGGER, or ERROR)').classes('text-h6 mb-4')
                    ui.button('Clean Pending Orders', icon='delete_sweep', on_click=self._clean_pending_orders_dialog).props('outline color=warning')
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
                select(TradingOrder, AccountDefinition)
                .join(AccountDefinition, TradingOrder.account_id == AccountDefinition.id)
                .where(TradingOrder.created_at >= cutoff_date)
                .where(TradingOrder.status.not_in([OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]))
                .order_by(TradingOrder.created_at.desc())
            )
            
            results = session.exec(statement).all()
            
            for order, account in results:
                # Use the new helper method to get expert ID
                expert_id = order.get_expert_id(session)
                expert_name = ""
                
                if expert_id:
                    expert_instance = session.get(ExpertInstance, expert_id)
                    if expert_instance:
                        base_name = expert_instance.alias or expert_instance.expert
                        expert_name = f"{base_name}-{expert_instance.id}"
                
                # Only mark as "Manual" if no expert found via either path
                if not expert_name:
                    expert_name = "Manual"
                
                # Format the order data
                order_dict = {
                    'order_id': order.id,
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
            {'name': 'order_id', 'label': 'Order ID', 'field': 'order_id', 'align': 'left'},
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
            # Get orders with PENDING, WAITING_TRIGGER, or ERROR status with expert information
            pending_statuses = [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER, OrderStatus.ERROR]
            statement = (
                select(TradingOrder, AccountDefinition)
                .join(AccountDefinition, TradingOrder.account_id == AccountDefinition.id)
                .where(TradingOrder.status.in_(pending_statuses))
                .order_by(TradingOrder.created_at.desc())
            )
            results = session.exec(statement).all()
            
            if not results:
                ui.label('No pending orders found.').classes('text-gray-500')
                return
            
            # Prepare data for table
            rows = []
            for order, account in results:
                # Use the new helper method to get expert ID
                expert_id = order.get_expert_id(session)
                expert_name = ""
                
                if expert_id:
                    expert_instance = session.get(ExpertInstance, expert_id)
                    if expert_instance:
                        base_name = expert_instance.alias or expert_instance.expert
                        expert_name = f"{base_name}-{expert_instance.id}"
                
                # Only mark as "Manual" if no expert found via either path
                if not expert_name:
                    expert_name = "Manual"
                
                # Format dates
                created_at_str = order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else ''
                
                row = {
                    'order_id': order.id,
                    'account': account.name,
                    'symbol': order.symbol,
                    'side': order.side,
                    'quantity': f"{order.quantity:.2f}" if order.quantity else '',
                    'order_type': order.order_type,
                    'status': order.status.value,
                    'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                    'stop_price': f"${order.stop_price:.2f}" if order.stop_price else '',
                    'comment': order.comment or '',
                    'created_at': created_at_str,
                    'expert': expert_name,
                    'waited_status': order.depends_order_status_trigger if order.status == OrderStatus.WAITING_TRIGGER else '',
                    'can_submit': order.status == OrderStatus.PENDING and not order.broker_order_id
                }
                rows.append(row)
            
            # Define table columns
            columns = [
                {'name': 'order_id', 'label': 'Order ID', 'field': 'order_id'},
                {'name': 'created_at', 'label': 'Date', 'field': 'created_at'},
                {'name': 'account', 'label': 'Account', 'field': 'account'},
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol'},
                {'name': 'side', 'label': 'Side', 'field': 'side'},
                {'name': 'quantity', 'label': 'Quantity', 'field': 'quantity'},
                {'name': 'order_type', 'label': 'Order Type', 'field': 'order_type'},
                {'name': 'status', 'label': 'Status', 'field': 'status'},
                {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price'},
                {'name': 'stop_price', 'label': 'Stop Price', 'field': 'stop_price'},
                {'name': 'expert', 'label': 'Expert', 'field': 'expert'},
                {'name': 'waited_status', 'label': 'Waited Status', 'field': 'waited_status'},
                {'name': 'comment', 'label': 'Comment', 'field': 'comment'},
                {'name': 'actions', 'label': 'Actions', 'field': 'actions'}
            ]
            
            # Enable multiple selection on the table
            table = ui.table(columns=columns, rows=rows, row_key='order_id', selection='multiple').classes('w-full')
            table.selected = []  # Initialize selected list
            
            # Add buttons above table (after table is created so we can bind to it)
            with table.add_slot('top-left'):
                with ui.row().classes('items-center gap-4'):
                    ui.button('Retry Selected Orders', 
                             icon='refresh', 
                             on_click=lambda: self._handle_retry_selected_orders(table.selected))\
                        .props('color=orange')\
                        .bind_enabled_from(table, 'selected', backward=lambda val: len(val) > 0)
                    ui.button('Map to Broker Orders', 
                             icon='link', 
                             on_click=lambda: self._handle_map_selected_orders(table.selected))\
                        .props('color=blue')\
                        .bind_enabled_from(table, 'selected', backward=lambda val: len(val) > 0)
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
            
            # Only allow submission for PENDING status
            if order.status != OrderStatus.PENDING:
                ui.notify(f'Can only submit orders with PENDING status (current: {order.status.value})', type='negative')
                return
                
            if order.broker_order_id:
                ui.notify('Order already submitted to broker', type='warning')
                return
            
            # Check if quantity is 0 and show dialog to input quantity
            if order.quantity == 0:
                self._show_quantity_input_dialog(order)
                return
            
            # Proceed with normal submission
            self._submit_order_to_broker(order)
                
        except Exception as e:
            logger.error(f"Error handling submit order: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _show_quantity_input_dialog(self, order: TradingOrder):
        """Show dialog to input quantity for zero-quantity orders."""
        try:
            quantity_input = None
            
            with ui.dialog() as dialog, ui.card().classes('p-6 min-w-96'):
                ui.label('Order has Quantity = 0').classes('text-h6 mb-2')
                ui.label(f'Order #{order.id} - {order.symbol} ({order.side})').classes('text-subtitle1 mb-4 text-gray-700')
                ui.label('This order has quantity = 0 and cannot be submitted. Please enter the desired quantity:').classes('mb-4')
                
                # Get current price for reference
                try:
                    account = get_instance(AccountDefinition, order.account_id)
                    if account:
                        provider_obj = get_account_instance_from_id(account.id)
                        if provider_obj:
                            current_price = provider_obj.get_instrument_current_price(order.symbol)
                            if current_price:
                                ui.label(f'Current Price: ${current_price:.2f}').classes('mb-2 text-sm text-gray-600')
                except Exception as price_error:
                    logger.warning(f"Could not fetch current price for {order.symbol}: {price_error}")
                
                # Quantity input
                quantity_input = ui.number(
                    label='Quantity',
                    value=1,
                    min=1,
                    step=1,
                    precision=0
                ).classes('w-full mb-4').props('outlined')
                
                ui.label('Note: The order and its linked transaction will be updated with the new quantity.').classes('text-sm text-gray-600 mb-4')
                
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button(
                        'Update & Submit',
                        on_click=lambda: self._update_quantity_and_submit(order, quantity_input.value, dialog)
                    ).props('color=primary')
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing quantity input dialog: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _update_quantity_and_submit(self, order: TradingOrder, new_quantity: float, dialog):
        """Update order quantity, update linked transaction, and submit."""
        try:
            from ...core.db import update_instance
            
            if not new_quantity or new_quantity <= 0:
                ui.notify('Please enter a valid quantity (must be greater than 0)', type='warning')
                return
            
            # Convert to integer for whole shares
            new_quantity = int(new_quantity)
            
            logger.info(f"Updating order {order.id} quantity from {order.quantity} to {new_quantity}")
            
            # Update the order quantity
            order.quantity = new_quantity
            update_instance(order)
            
            # Update linked transaction if exists (order has transaction_id pointing to transaction)
            if order.transaction_id:
                session = get_db()
                try:
                    transaction = session.get(Transaction, order.transaction_id)
                    
                    if transaction:
                        logger.info(f"Updating linked transaction {transaction.id} quantity from {transaction.quantity} to {new_quantity}")
                        transaction.quantity = new_quantity
                        update_instance(transaction)
                    else:
                        logger.warning(f"Transaction {order.transaction_id} not found for order {order.id}")
                        
                finally:
                    session.close()
            else:
                logger.debug(f"Order {order.id} has no linked transaction")
            
            # Close dialog
            dialog.close()
            
            # Submit the order to broker
            self._submit_order_to_broker(order)
            
        except Exception as e:
            logger.error(f"Error updating quantity and submitting order: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _submit_order_to_broker(self, order: TradingOrder):
        """Submit order to broker via account provider."""
        try:
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
                provider_obj = get_account_instance_from_id(account.id)
                if not provider_obj:
                    ui.notify(f'Failed to get account instance for {account.name}', type='negative')
                    return
                submitted_order = provider_obj.submit_order(order)
                
                if submitted_order:
                    ui.notify(f'Order {order.id} submitted successfully to {account.provider}', type='positive')
                    # Refresh the table
                    self.render()
                else:
                    ui.notify(f'Failed to submit order {order.id} to broker', type='negative')
                    
            except Exception as e:
                logger.error(f"Error submitting order {order.id}: {e}", exc_info=True)
                ui.notify(f'Error submitting order: {str(e)}', type='negative')
                
        except Exception as e:
            logger.error(f"Error submitting order to broker: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _handle_retry_selected_orders(self, selected_rows):
        """Handle retrying selected orders."""
        try:
            if not selected_rows:
                ui.notify('No orders selected', type='warning')
                return
            
            # Get order IDs from selected rows
            order_ids = [row['order_id'] for row in selected_rows]
            
            # Filter to only ERROR orders
            error_order_ids = []
            for row in selected_rows:
                if row.get('status') == 'ERROR':
                    error_order_ids.append(row['order_id'])
            
            if not error_order_ids:
                ui.notify('Only orders with ERROR status can be retried', type='warning')
                return
            
            # Confirmation dialog
            with ui.dialog() as dialog, ui.card():
                ui.label(f'Retry {len(error_order_ids)} ERROR order(s)?').classes('text-h6 mb-4')
                ui.label('This will resubmit the orders to the broker. Orders that succeed will change to PENDING status.').classes('mb-4')
                
                if len(error_order_ids) < len(order_ids):
                    ui.label(f'Note: {len(order_ids) - len(error_order_ids)} non-ERROR orders will be skipped.').classes('mb-4 text-orange-600')
                
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Retry Orders', on_click=lambda: self._confirm_retry_orders(error_order_ids, dialog)).props('color=orange')
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error handling retry selected orders: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _confirm_retry_orders(self, order_ids, dialog):
        """Confirm and execute order retry."""
        try:
            retried_count = 0
            errors = []
            zero_qty_orders = []
            
            for order_id in order_ids:
                try:
                    # Get the order
                    order = get_instance(TradingOrder, order_id)
                    if not order:
                        errors.append(f"Order {order_id} not found")
                        continue
                    
                    # Only retry ERROR orders
                    if order.status != OrderStatus.ERROR:
                        errors.append(f"Order {order_id} is not in ERROR status (status: {order.status.value})")
                        continue
                    
                    # Check if quantity is 0 - cannot retry these automatically
                    if order.quantity == 0:
                        zero_qty_orders.append(order_id)
                        logger.warning(f"Order {order_id} has quantity=0, skipping retry. User must manually set quantity.")
                        continue
                    
                    # Get the account
                    account = get_instance(AccountDefinition, order.account_id)
                    if not account:
                        errors.append(f"Account for order {order_id} not found")
                        continue
                    
                    # Submit the order through the account provider
                    try:
                        provider_obj = get_account_instance_from_id(account.id)
                        if not provider_obj:
                            errors.append(f"Failed to get account instance for {account.name}")
                            continue
                        submitted_order = provider_obj.submit_order(order)
                        
                        if submitted_order:
                            retried_count += 1
                            logger.info(f"Successfully retried order {order_id}: {order.symbol} {order.side} {order.quantity}")
                        else:
                            errors.append(f"Order {order_id}: Failed to resubmit to broker")
                            
                    except Exception as e:
                        logger.error(f"Error retrying order {order_id}: {e}", exc_info=True)
                        errors.append(f"Order {order_id}: {str(e)}")
                        
                except Exception as e:
                    logger.error(f"Error processing retry for order {order_id}: {e}", exc_info=True)
                    errors.append(f"Order {order_id}: {str(e)}")
            
            # Show results
            if retried_count > 0:
                ui.notify(f'Successfully retried {retried_count} order(s)', type='positive')
            
            if zero_qty_orders:
                ui.notify(f'{len(zero_qty_orders)} order(s) with quantity=0 skipped. Please update quantity manually and submit.', type='warning')
            
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
            logger.error(f"Error confirming retry orders: {e}", exc_info=True)
            ui.notify(f'Error retrying orders: {str(e)}', type='negative')
    
    def _handle_map_selected_orders(self, selected_rows):
        """Handle mapping selected database orders to broker orders."""
        try:
            if not selected_rows:
                ui.notify('No orders selected', type='warning')
                return
            
            # Get order IDs from selected rows
            order_ids = [row['order_id'] for row in selected_rows]
            
            # Get database orders
            db_orders = []
            for order_id in order_ids:
                order = get_instance(TradingOrder, order_id)
                if order:
                    db_orders.append(order)
            
            if not db_orders:
                ui.notify('No valid orders found', type='warning')
                return
            
            # Open mapping dialog
            self._show_order_mapping_dialog(db_orders)
            
        except Exception as e:
            logger.error(f"Error handling map selected orders: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _show_order_mapping_dialog(self, db_orders):
        """Show dialog to map database orders to broker orders."""
        try:
            # Get all unique accounts from selected orders
            account_ids = list(set(order.account_id for order in db_orders))
            
            # Fetch broker orders for these accounts
            all_broker_orders = {}
            for account_id in account_ids:
                try:
                    account = get_instance(AccountDefinition, account_id)
                    if not account:
                        continue
                    
                    provider_obj = get_account_instance_from_id(account.id)
                    if not provider_obj:
                        continue
                    
                    broker_orders = provider_obj.get_orders()
                    
                    # Filter to recent orders and convert to dict format
                    if broker_orders:
                        all_broker_orders[account_id] = [
                            {
                                'broker_order_id': str(bo.broker_order_id) if hasattr(bo, 'broker_order_id') and bo.broker_order_id is not None else str(bo.id),
                                'symbol': bo.symbol,
                                'side': bo.side,
                                'quantity': bo.quantity,
                                'status': bo.status,
                                'order_type': getattr(bo, 'order_type', 'Unknown'),
                                'created_at': getattr(bo, 'created_at', 'Unknown'),
                                'client_order_id': getattr(bo, 'client_order_id', 'N/A')
                            }
                            for bo in broker_orders[-50:]  # Last 50 orders
                        ]
                        
                except Exception as e:
                    logger.warning(f"Could not fetch broker orders for account {account_id}: {e}")
                    all_broker_orders[account_id] = []
            
            # Create mapping dialog
            with ui.dialog().classes('w-[1200px] max-w-[90vw]') as dialog, ui.card():
                ui.label('Map Database Orders to Broker Orders').classes('text-h5 mb-4')
                ui.label('Match database orders with corresponding broker orders. Orders with matching symbol and quantity will be suggested.').classes('text-sm text-gray-600 mb-4')
                
                mapping_data = {}
                
                with ui.scroll_area().style('height: 600px'):
                    for i, db_order in enumerate(db_orders):
                        account_broker_orders = all_broker_orders.get(db_order.account_id, [])
                        
                        with ui.card().classes('mb-4 p-4'):
                            # Database order info
                            with ui.row().classes('w-full items-center mb-3'):
                                ui.label(f'Database Order #{db_order.id}').classes('text-lg font-bold')
                                ui.badge(f'{db_order.symbol}', color='blue').classes('ml-2')
                                ui.badge(f'{db_order.side.value}', color='orange').classes('ml-1')
                                ui.badge(f'{db_order.quantity}', color='green').classes('ml-1')
                                ui.badge(f'Status: {db_order.status.value}', color='red').classes('ml-1')
                            
                            if db_order.broker_order_id:
                                ui.label(f'Current Broker ID: {db_order.broker_order_id}').classes('text-sm text-gray-600 mb-2')
                            else:
                                ui.label('No broker order ID assigned').classes('text-sm text-orange-600 mb-2')
                            
                            # Broker order selection
                            ui.label('Select matching broker order:').classes('text-sm font-medium mb-2')
                            
                            # Create options for broker orders
                            broker_options = [{'label': 'No mapping', 'value': None}]
                            suggested_match = None
                            
                            for bo in account_broker_orders:
                                # Check if broker order is already used
                                already_used = any(
                                    other_order.broker_order_id == bo['broker_order_id'] 
                                    for other_order in db_orders 
                                    if other_order.id != db_order.id and other_order.broker_order_id
                                )
                                
                                status_badge = '🔴' if already_used else '🟢'
                                match_score = 0
                                
                                # Calculate match score
                                if bo['symbol'] == db_order.symbol:
                                    match_score += 10
                                if bo['side'] == db_order.side.value:
                                    match_score += 5
                                if abs(float(bo['quantity']) - float(db_order.quantity)) < 0.01:
                                    match_score += 10
                                
                                # Format creation date for display
                                created_date = str(bo['created_at'])[:19] if bo['created_at'] != 'Unknown' else 'Unknown'
                                
                                # Format order type for display
                                order_type_display = bo['order_type'].value if hasattr(bo['order_type'], 'value') else str(bo['order_type'])
                                
                                # Create detailed display label with symbol, qty, order type, creation date, and client_order_id
                                label = f"{status_badge} {bo['symbol']} | {order_type_display} | Qty: {bo['quantity']} | {created_date} | Client: {bo['client_order_id']}"
                                if already_used:
                                    label += " (USED)"
                                
                                broker_options.append({
                                    'label': label,
                                    'value': bo['broker_order_id'],
                                    'disabled': already_used,
                                    'match_score': match_score
                                })
                                
                                # Suggest best match that's not already used
                                if not already_used and match_score >= 20 and (not suggested_match or match_score > suggested_match['match_score']):
                                    suggested_match = {'value': bo['broker_order_id'], 'match_score': match_score}
                            
                            # Sort options by match score (but keep "No mapping" first)
                            broker_options = [broker_options[0]] + sorted(broker_options[1:], key=lambda x: x.get('match_score', 0), reverse=True)
                            
                            # Create select widget - ensure all values are strings for consistency
                            default_value = str(suggested_match['value']) if suggested_match and suggested_match['value'] is not None else None
                            
                            # Convert all broker option values to strings
                            for opt in broker_options:
                                if opt['value'] is not None:
                                    opt['value'] = str(opt['value'])
                            
                            if db_order.broker_order_id:
                                broker_order_id_str = str(db_order.broker_order_id)
                                # Check if current broker ID exists in options
                                broker_id_exists = any(opt['value'] == broker_order_id_str for opt in broker_options[1:])
                                if not broker_id_exists:
                                    # Current broker ID not in list, add it
                                    broker_options.insert(1, {
                                        'label': f'🔴 {broker_order_id_str} (CURRENT - NOT FOUND IN BROKER)',
                                        'value': broker_order_id_str,
                                        'disabled': True
                                    })
                                # Always set current broker ID as default if it exists
                                default_value = broker_order_id_str
                            
                            # Final safety check: ensure default_value is in options
                            logger.debug(f"Order mapping debug - DB Order {db_order.id}: broker_order_id='{db_order.broker_order_id}' (type: {type(db_order.broker_order_id)}), default_value='{default_value}' (type: {type(default_value)})")
                            logger.debug(f"Order mapping debug - Broker options: {[(opt['value'], type(opt['value'])) for opt in broker_options]}")
                            
                            if default_value is not None and not any(opt['value'] == default_value for opt in broker_options):
                                logger.warning(f"Default value '{default_value}' (type: {type(default_value)}) not found in broker options, falling back to None")
                                # Let's also try string comparison in case there's a type mismatch
                                str_match = any(str(opt['value']) == str(default_value) for opt in broker_options)
                                logger.warning(f"String comparison match: {str_match}")
                                default_value = None
                            
                            logger.debug(f"Order mapping debug - Final default_value: '{default_value}'")
                            
                            # Create select with proper options mapping
                            # Convert options to the format NiceGUI expects: {value: label}
                            select_options = {opt['value']: opt['label'] for opt in broker_options}
                            
                            try:
                                select = ui.select(
                                    options=select_options,
                                    value=default_value,
                                    label='Broker Order'
                                ).classes('w-full')
                            except ValueError as e:
                                logger.error(f"Failed to create select with value '{default_value}': {e}")
                                logger.error(f"Available options: {list(select_options.keys())}")
                                # Force fallback to None and retry
                                select = ui.select(
                                    options=select_options,
                                    value=None,
                                    label='Broker Order'
                                ).classes('w-full')
                            
                            mapping_data[db_order.id] = select
                            
                            if suggested_match and not db_order.broker_order_id:
                                ui.label(f'✨ Suggested match (score: {suggested_match["match_score"]})').classes('text-xs text-green-600')
                
                # Dialog buttons
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Apply Mapping', 
                             on_click=lambda: self._apply_order_mapping(mapping_data, db_orders, dialog))\
                        .props('color=blue')
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing order mapping dialog: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
    def _apply_order_mapping(self, mapping_data, db_orders, dialog):
        """Apply the order mapping selections."""
        try:
            updated_count = 0
            errors = []
            
            for db_order in db_orders:
                try:
                    select_widget = mapping_data[db_order.id]
                    new_broker_id = select_widget.value
                    
                    # Skip if no change
                    if new_broker_id == db_order.broker_order_id:
                        continue
                    
                    # Validate broker order ID isn't already used (double check)
                    if new_broker_id:
                        existing_order = None
                        with get_db() as session:
                            stmt = select(TradingOrder).where(
                                TradingOrder.broker_order_id == new_broker_id,
                                TradingOrder.id != db_order.id
                            )
                            existing_order = session.exec(stmt).first()
                        
                        if existing_order:
                            errors.append(f"Order {db_order.id}: Broker ID {new_broker_id} already used by order {existing_order.id}")
                            continue
                    
                    # Update only the broker_order_id field - do NOT update status during mapping
                    # Status updates should only happen through proper account refresh, not mapping
                    old_broker_id = db_order.broker_order_id
                    
                    # Warn if overwriting existing non-None broker_order_id with different value
                    if old_broker_id and old_broker_id != new_broker_id:
                        logger.warning(f"Order mapping: Overwriting existing broker_order_id '{old_broker_id}' with '{new_broker_id}' for order {db_order.id}")
                    
                    db_order.broker_order_id = new_broker_id
                    
                    logger.info(f"Order mapping: Updated order {db_order.id} broker_order_id from '{old_broker_id}' to '{new_broker_id}' (status remains {db_order.status.value})")
                    
                    # Save the order
                    if update_instance(db_order):
                        updated_count += 1
                        logger.info(f"Mapped order {db_order.id} to broker order {new_broker_id}")
                    else:
                        errors.append(f"Order {db_order.id}: Failed to save mapping")
                        
                except Exception as e:
                    logger.error(f"Error mapping order {db_order.id}: {e}", exc_info=True)
                    errors.append(f"Order {db_order.id}: {str(e)}")
            
            # Show results
            if updated_count > 0:
                ui.notify(f'Successfully mapped {updated_count} order(s)', type='positive')
            
            if errors:
                error_msg = '; '.join(errors[:3])
                if len(errors) > 3:
                    error_msg += f'... and {len(errors) - 3} more errors'
                ui.notify(f'Errors: {error_msg}', type='warning')
            
            # Close dialog and refresh
            dialog.close()
            
            # Note: We intentionally do NOT call account refresh here to avoid triggering
            # any automatic order creation or status update logic. Order mapping should
            # ONLY update the broker_order_id field. Status updates should happen through
            # the normal account refresh cycle, not during manual mapping.
            logger.info(f"Order mapping completed. Updated {updated_count} order(s). No automatic refresh performed.")
            
            # Refresh the pending orders table
            if hasattr(self, 'pending_orders_container'):
                self.pending_orders_container.clear()
                with self.pending_orders_container:
                    self._render_pending_orders_content()
            
        except Exception as e:
            logger.error(f"Error applying order mapping: {e}", exc_info=True)
            ui.notify(f'Error applying mapping: {str(e)}', type='negative')
    
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
    
    def _show_broker_comparison_dialog(self):
        """Show dialog comparing broker positions with our transaction data."""
        from ...core.models import Transaction, AccountDefinition
        from ...core.types import TransactionStatus
        
        try:
            # Get comparison results
            result = self._compare_positions_with_broker()
            
            # Check if error occurred (None returned)
            if result is None:
                ui.notify('❌ Error comparing positions with broker. Check logs for details.', type='negative')
                return
            
            discrepancies = result['discrepancies']
            orphaned_orders = result['orphaned_orders']
            
            if not discrepancies and not orphaned_orders:
                ui.notify('✅ No discrepancies or orphaned orders found! All positions match broker data.', type='positive')
                return
            
            # Show dialog with results
            with ui.dialog() as dialog, ui.card().classes('w-full max-w-6xl'):
                ui.label('🔍 Broker Position Comparison').classes('text-h5 mb-4')
                
                # Summary message
                summary_parts = []
                if discrepancies:
                    summary_parts.append(f'{len(discrepancies)} discrepancies')
                if orphaned_orders:
                    summary_parts.append(f'{len(orphaned_orders)} orphaned orders')
                ui.label(f'Found {" and ".join(summary_parts)} across accounts').classes('text-subtitle2 text-orange mb-4')
                
                # Group discrepancies by account
                by_account = {}
                for disc in discrepancies:
                    acc_name = disc['account']
                    if acc_name not in by_account:
                        by_account[acc_name] = []
                    by_account[acc_name].append(disc)
                
                # Display each account's discrepancies
                for account_name, account_discs in by_account.items():
                    with ui.expansion(account_name, icon='account_balance').classes('w-full mb-2').props('default-opened'):
                        columns = [
                            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                            {'name': 'our_qty', 'label': 'Our Qty', 'field': 'our_qty', 'sortable': True, 'align': 'right'},
                            {'name': 'broker_qty', 'label': 'Broker Qty', 'field': 'broker_qty', 'sortable': True, 'align': 'right'},
                            {'name': 'qty_diff', 'label': 'Qty Diff', 'field': 'qty_diff', 'sortable': True, 'align': 'right'},
                            {'name': 'our_avg_price', 'label': 'Our Avg Price', 'field': 'our_avg_price', 'sortable': True, 'align': 'right'},
                            {'name': 'broker_avg_price', 'label': 'Broker Avg Price', 'field': 'broker_avg_price', 'sortable': True, 'align': 'right'},
                            {'name': 'price_diff_pct', 'label': 'Price Diff %', 'field': 'price_diff_pct', 'sortable': True, 'align': 'right'},
                            {'name': 'discrepancy_type', 'label': 'Issue', 'field': 'discrepancy_type', 'sortable': True, 'align': 'left'}
                        ]
                        
                        rows = []
                        for disc in account_discs:
                            rows.append({
                                'symbol': disc['symbol'],
                                'our_qty': f"{disc['our_qty']:.2f}",
                                'broker_qty': f"{disc['broker_qty']:.2f}",
                                'qty_diff': f"{disc['qty_diff']:.2f}",
                                'our_avg_price': f"${disc['our_avg_price']:.2f}" if disc['our_avg_price'] else 'N/A',
                                'broker_avg_price': f"${disc['broker_avg_price']:.2f}" if disc['broker_avg_price'] else 'N/A',
                                'price_diff_pct': f"{disc['price_diff_pct']:.2f}%" if disc['price_diff_pct'] is not None else 'N/A',
                                'discrepancy_type': disc['discrepancy_type']
                            })
                        
                        ui.table(columns=columns, rows=rows).classes('w-full')
                
                # Display orphaned orders if any
                if orphaned_orders:
                    ui.separator().classes('my-4')
                    ui.label('⚠️ Orphaned Orders (Executed but not bound to transactions)').classes('text-h6 mb-2 text-red')
                    
                    # Group orphaned orders by account
                    orphans_by_account = {}
                    for orphan in orphaned_orders:
                        acc_name = orphan['account']
                        if acc_name not in orphans_by_account:
                            orphans_by_account[acc_name] = []
                        orphans_by_account[acc_name].append(orphan)
                    
                    for account_name, account_orphans in orphans_by_account.items():
                        with ui.expansion(f"{account_name} - {len(account_orphans)} orphaned order(s)", icon='warning').classes('w-full mb-2 bg-red-50'):
                            orphan_columns = [
                                {'name': 'order_id', 'label': 'Order ID', 'field': 'order_id', 'sortable': True, 'align': 'left'},
                                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
                                {'name': 'side', 'label': 'Side', 'field': 'side', 'sortable': True, 'align': 'left'},
                                {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'sortable': True, 'align': 'right'},
                                {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
                                {'name': 'date', 'label': 'Date', 'field': 'date', 'sortable': True, 'align': 'left'},
                                {'name': 'broker_order_id', 'label': 'Broker Order ID', 'field': 'broker_order_id', 'sortable': True, 'align': 'left'}
                            ]
                            
                            orphan_rows = []
                            for orphan in account_orphans:
                                orphan_rows.append({
                                    'order_id': str(orphan['order_id']),
                                    'symbol': orphan['symbol'],
                                    'side': orphan['side'],
                                    'qty': f"{orphan['qty']:.2f}",
                                    'price': f"${orphan['price']:.2f}" if orphan['price'] else 'N/A',
                                    'date': orphan['date'],
                                    'broker_order_id': orphan['broker_order_id']
                                })
                            
                            ui.table(columns=orphan_columns, rows=orphan_rows).classes('w-full')
                            ui.label('These orders are executed but not associated with any transaction. Consider linking them or investigating why they exist.').classes('text-sm text-orange italic mt-2')
                
                # Add legend
                with ui.card().classes('w-full mt-4 bg-blue-50'):
                    ui.label('ℹ️ Legend').classes('text-subtitle2 font-bold mb-2')
                    ui.label('• Quantity Mismatch: Our transaction qty differs from broker qty').classes('text-sm')
                    ui.label('• Price Mismatch (>3%): Total cost basis differs by more than 3%').classes('text-sm')
                    ui.label('• Both: Both quantity and price mismatches detected').classes('text-sm')
                    if orphaned_orders:
                        ui.label('• Orphaned Orders: Executed orders not bound to any transaction').classes('text-sm text-red')
                
                with ui.row().classes('w-full justify-end mt-4'):
                    ui.button('Close', on_click=dialog.close).props('flat')
            
            dialog.open()
            
        except Exception as e:
            logger.error(f"Error showing broker comparison dialog: {e}", exc_info=True)
            ui.notify(f'Error comparing positions: {str(e)}', type='negative')
    
    def _compare_positions_with_broker(self):
        """
        Compare our transaction positions with broker positions.
        Works per-transaction for open positions, aggregates by symbol.
        Also detects orphaned orders (executed orders not bound to transactions).
        Returns dict with 'discrepancies' list and 'orphaned_orders' list.
        Returns None if an error occurs.
        """
        from ...core.models import Transaction, AccountDefinition, TradingOrder
        from ...core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
        from sqlmodel import select
        
        discrepancies = []
        orphaned_orders = []
        session = get_db()
        
        try:
            # Get all accounts
            accounts = get_all_instances(AccountDefinition)
            
            logger.info(f"Starting broker position comparison for {len(accounts)} account(s)")
            
            for account in accounts:
                try:
                    # Get broker positions
                    account_provider = get_account_instance_from_id(account.id)
                    if not account_provider:
                        logger.warning(f"Could not get account provider for {account.name}")
                        continue
                    
                    broker_positions = account_provider.get_positions()
                    
                    # Step 1: Check for orphaned orders (executed orders without transactions)
                    orphan_statement = (
                        select(TradingOrder)
                        .where(TradingOrder.account_id == account.id)
                        .where(TradingOrder.status.in_(OrderStatus.get_executed_statuses()))
                        .where(TradingOrder.transaction_id.is_(None))
                    )
                    orphaned = session.exec(orphan_statement).all()
                    
                    for orphan_order in orphaned:
                        orphaned_orders.append({
                            'account': account.name,
                            'order_id': orphan_order.id,
                            'symbol': orphan_order.symbol,
                            'side': orphan_order.side.value if orphan_order.side else 'UNKNOWN',
                            'qty': orphan_order.filled_qty or 0,
                            'price': orphan_order.open_price or 0,
                            'date': orphan_order.created_at.strftime('%Y-%m-%d %H:%M') if orphan_order.created_at else 'N/A',
                            'broker_order_id': orphan_order.broker_order_id or 'N/A'
                        })
                        logger.warning(
                            f"Orphaned order found: Order {orphan_order.id} - "
                            f"{orphan_order.symbol} {orphan_order.side.value if orphan_order.side else 'N/A'} "
                            f"qty={orphan_order.filled_qty} @ ${orphan_order.open_price or 0:.2f}"
                        )
                    
                    # Step 2: Get open transactions and aggregate by symbol
                    # Work per transaction to calculate positions correctly
                    statement = (
                        select(Transaction)
                        .where(Transaction.expert_id.in_(
                            select(TradingOrder.expert_recommendation_id)
                            .where(TradingOrder.account_id == account.id)
                            .distinct()
                        ) | (Transaction.expert_id.is_(None)))
                        .where(Transaction.status == TransactionStatus.OPENED)
                    )
                    
                    # Get all open transactions for this account through orders
                    open_transactions_statement = (
                        select(Transaction)
                        .join(TradingOrder, Transaction.id == TradingOrder.transaction_id)
                        .where(TradingOrder.account_id == account.id)
                        .where(Transaction.status == TransactionStatus.OPENED)
                        .distinct()
                    )
                    open_transactions = session.exec(open_transactions_statement).all()
                    
                    # Aggregate positions by symbol from open transactions
                    our_positions = {}
                    for transaction in open_transactions:
                        # Get all FILLED orders for this transaction (exclude pending/limit orders)
                        orders_statement = (
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == transaction.id)
                            .where(TradingOrder.account_id == account.id)
                            .where(TradingOrder.status.in_(OrderStatus.get_executed_statuses()))
                        )
                        orders = session.exec(orders_statement).all()
                        
                        for order in orders:
                            if not order.symbol or not order.filled_qty:
                                continue
                            
                            # Initialize position tracker for symbol
                            if order.symbol not in our_positions:
                                our_positions[order.symbol] = {
                                    'qty': 0.0,
                                    'cost_basis': 0.0,
                                    'avg_price': 0.0,
                                    'order_count': 0,
                                    'transaction_ids': set()
                                }
                            
                            # Calculate quantity (positive for buy, negative for sell)
                            qty_delta = order.filled_qty if order.side == OrderDirection.BUY else -order.filled_qty
                            
                            # Update cost basis - use open_price (the filled price)
                            if order.open_price and order.filled_qty:
                                cost_delta = abs(qty_delta) * order.open_price
                                our_positions[order.symbol]['cost_basis'] += cost_delta
                                logger.debug(
                                    f"  Txn {transaction.id}, Order {order.id}: {order.symbol} "
                                    f"{order.side.value} qty={order.filled_qty} @ ${order.open_price:.2f} "
                                    f"-> cost_delta=${cost_delta:.2f}"
                                )
                            
                            our_positions[order.symbol]['qty'] += qty_delta
                            our_positions[order.symbol]['order_count'] += 1
                            our_positions[order.symbol]['transaction_ids'].add(transaction.id)
                    
                    # Calculate average prices
                    for symbol, data in our_positions.items():
                        if abs(data['qty']) > 0.01:
                            data['avg_price'] = data['cost_basis'] / abs(data['qty'])
                            logger.debug(
                                f"Position summary for {symbol}: "
                                f"{data['order_count']} orders from {len(data['transaction_ids'])} transactions, "
                                f"net_qty={data['qty']:.2f}, "
                                f"cost_basis=${data['cost_basis']:.2f}, "
                                f"avg_price=${data['avg_price']:.2f}"
                            )
                    
                    # Compare broker positions with our positions
                    broker_symbols = set()
                    for broker_pos in broker_positions:
                        pos_dict = broker_pos if isinstance(broker_pos, dict) else dict(broker_pos)
                        symbol = pos_dict.get('symbol')
                        broker_qty = float(pos_dict.get('qty', 0))
                        broker_avg_price = float(pos_dict.get('avg_entry_price', 0))
                        
                        broker_symbols.add(symbol)
                        
                        # Get our position for this symbol
                        our_data = our_positions.get(symbol, {'qty': 0.0, 'avg_price': 0.0, 'cost_basis': 0.0})
                        our_qty = our_data['qty']
                        our_avg_price = our_data['avg_price']
                        our_cost_basis = our_data['cost_basis']
                        
                        # Check for discrepancies
                        qty_mismatch = abs(broker_qty - our_qty) > 0.01  # Tolerance for float comparison
                        price_mismatch = False
                        price_diff_pct = None
                        
                        # Only check price if both have non-zero quantities and prices
                        # Compare total cost basis (avg_price * qty) instead of just avg_price
                        if abs(broker_qty) > 0.01 and abs(our_qty) > 0.01 and broker_avg_price > 0 and our_avg_price > 0:
                            # Calculate cost basis for broker position
                            broker_cost_basis = broker_avg_price * abs(broker_qty)
                            # Compare cost basis percentage difference
                            price_diff_pct = abs(broker_cost_basis - our_cost_basis) / broker_cost_basis * 100
                            price_mismatch = price_diff_pct > 3.0  # 3% tolerance
                        
                        if qty_mismatch or price_mismatch:
                            discrepancy_type = []
                            if qty_mismatch:
                                discrepancy_type.append('Quantity Mismatch')
                            if price_mismatch:
                                discrepancy_type.append(f'Price Mismatch (>{price_diff_pct:.1f}%)')
                            
                            discrepancies.append({
                                'account': account.name,
                                'account_id': account.id,
                                'symbol': symbol,
                                'our_qty': our_qty,
                                'broker_qty': broker_qty,
                                'qty_diff': broker_qty - our_qty,
                                'our_avg_price': our_avg_price,
                                'broker_avg_price': broker_avg_price,
                                'price_diff_pct': price_diff_pct,
                                'discrepancy_type': ' + '.join(discrepancy_type)
                            })
                    
                    # Check for symbols we have but broker doesn't
                    for symbol, our_data in our_positions.items():
                        if symbol not in broker_symbols and abs(our_data['qty']) > 0.01:
                            discrepancies.append({
                                'account': account.name,
                                'account_id': account.id,
                                'symbol': symbol,
                                'our_qty': our_data['qty'],
                                'broker_qty': 0.0,
                                'qty_diff': -our_data['qty'],
                                'our_avg_price': our_data['avg_price'],
                                'broker_avg_price': 0.0,
                                'price_diff_pct': None,
                                'discrepancy_type': 'Position Not at Broker'
                            })
                    
                    # Log summary for this account
                    account_discrepancies = [d for d in discrepancies if d['account'] == account.name]
                    account_orphans = [o for o in orphaned_orders if o['account'] == account.name]
                    logger.info(
                        f"Account '{account.name}': {len(broker_positions)} broker positions, "
                        f"{len(our_positions)} our positions, "
                        f"{len(account_discrepancies)} discrepancies, "
                        f"{len(account_orphans)} orphaned orders found"
                    )
                
                except Exception as e:
                    logger.error(f"Error comparing positions for account {account.name}: {e}", exc_info=True)
                    # Return None to indicate error occurred
                    return None
            
            # Log final summary
            logger.info(
                f"Broker comparison complete: {len(accounts)} accounts checked, "
                f"{len(discrepancies)} total discrepancies, "
                f"{len(orphaned_orders)} orphaned orders found"
            )
            
            return {
                'discrepancies': discrepancies,
                'orphaned_orders': orphaned_orders
            }
            
        except Exception as e:
            logger.error(f"Error in _compare_positions_with_broker: {e}", exc_info=True)
            return None
        finally:
            session.close()
    
    def _clean_pending_orders_dialog(self):
        """Show confirmation dialog for cleaning pending orders."""
        logger.debug("User clicked 'Clean Pending Orders' button")
        
        def confirm_clean():
            """Execute the cleanup after confirmation."""
            logger.info("User confirmed pending orders cleanup")
            try:
                from ...core.TradeManager import get_trade_manager
                trade_manager = get_trade_manager()
                
                # Execute cleanup
                stats = trade_manager.clean_pending_orders()
                
                # Show results
                if stats['errors']:
                    error_msg = '\n'.join(stats['errors'][:5])  # Show first 5 errors
                    ui.notify(
                        f"Cleanup completed with issues:\n{error_msg}",
                        type='warning'
                    )
                else:
                    ui.notify(
                        f"✓ Cleaned {stats['orders_deleted']} pending orders\n"
                        f"✓ Deleted {stats['dependents_deleted']} dependent orders\n"
                        f"✓ Closed {stats['transactions_closed']} transactions",
                        type='positive'
                    )
                
                logger.info(
                    f"Pending orders cleanup completed: deleted {stats['orders_deleted']} orders, "
                    f"deleted {stats['dependents_deleted']} dependents, "
                    f"closed {stats['transactions_closed']} transactions"
                )
                
                # Refresh pending orders table
                if hasattr(self, 'pending_orders_container') and self.pending_orders_container:
                    self.pending_orders_container.clear()
                    with self.pending_orders_container:
                        self._render_pending_orders_content()
                        
            except Exception as e:
                logger.error(f"Error during pending order cleanup: {e}", exc_info=True)
                ui.notify(f'Error cleaning pending orders: {str(e)}', type='negative')
        
        # Show confirmation dialog
        with ui.dialog() as dialog:
            with ui.card().classes('w-96'):
                ui.label('Clean Pending Orders?').classes('text-lg font-bold')
                ui.label(
                    'This will delete all PENDING and ERROR orders (not submitted to broker), '
                    'plus their dependent orders. WAITING_TRIGGER orders whose parents are NOT being deleted will be preserved.'
                ).classes('text-sm text-gray-600')
                ui.label('⚠️ This action cannot be undone!').classes('text-sm font-bold text-orange-600')
                
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Confirm', on_click=lambda: [confirm_clean(), dialog.close()]).props('color=warning')
        
        dialog.open()

class TransactionsTab:
    """Comprehensive transactions management tab with full control over positions."""
    
    def __init__(self):
        self.transactions_container = None
        self.transactions_table = None
        self.selected_transaction = None
        self.selected_transactions = {}  # Dictionary to track selected transaction IDs
        self.batch_operations_container = None
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
        
        # Pre-populate expert options before creating the UI
        expert_options, expert_map = self._get_expert_options()
        self.expert_id_map = expert_map
        
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
                    
                    # Expert filter - populated with all experts
                    self.expert_filter = ui.select(
                        label='Expert',
                        options=expert_options,
                        value='All',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')
                    
                    self.symbol_filter = ui.input(
                        label='Symbol',
                        placeholder='Filter by symbol...',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-40')
                    
                    self.broker_order_id_filter = ui.input(
                        label='Broker Order ID',
                        placeholder='Search by broker order ID...',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')
                    
                    ui.button('Refresh', icon='refresh', on_click=lambda: self._refresh_transactions()).props('outline')
                    
                    ui.button('Force Refresh Account', icon='cloud_download', on_click=self._force_refresh_account_now).props('outline')
                    
                    # Batch operation buttons
                    self.batch_operations_container = ui.row().classes('gap-2 ml-4')
                    self.batch_select_all_btn = ui.button(
                        'Select All',
                        icon='done_all',
                        on_click=self._select_all_transactions
                    ).props('outline size=md').classes('hidden')
                    self.batch_select_all_btn.set_visibility(False)
                    
                    self.batch_clear_btn = ui.button(
                        'Clear',
                        icon='clear',
                        on_click=self._clear_selected_transactions
                    ).props('outline size=md').classes('hidden')
                    self.batch_clear_btn.set_visibility(False)
                    
                    self.batch_close_btn = ui.button(
                        'Batch Close',
                        icon='close',
                        on_click=self._batch_close_transactions
                    ).props('outline color=negative size=md').classes('hidden')
                    self.batch_close_btn.set_visibility(False)
                    
                    self.batch_adjust_tp_btn = ui.button(
                        'Batch Adjust TP',
                        icon='trending_up',
                        on_click=self._batch_adjust_tp_dialog
                    ).props('outline color=info size=md').classes('hidden')
                    self.batch_adjust_tp_btn.set_visibility(False)
            
            # Transactions table container
            self.transactions_container = ui.column().classes('w-full')
            self._render_transactions_table()
    
    def _get_expert_options(self):
        """Get list of expert options and ID mapping."""
        from ...core.models import ExpertInstance
        
        session = get_db()
        try:
            # Get ALL expert instances
            expert_statement = select(ExpertInstance)
            experts = list(session.exec(expert_statement).all())
            
            # Build expert options list with shortnames
            expert_options = ['All']
            expert_map = {'All': 'All'}
            for expert in experts:
                # Create shortname: use alias, user_description, or fallback to "expert_name-id"
                shortname = expert.alias or expert.user_description or f"{expert.expert}-{expert.id}"
                expert_options.append(shortname)
                expert_map[shortname] = expert.id
            
            logger.debug(f"[GET_EXPERT_OPTIONS] Built {len(expert_options)} expert options")
            return expert_options, expert_map
        
        except Exception as e:
            logger.error(f"Error getting expert options: {e}", exc_info=True)
            return ['All'], {'All': 'All'}
        finally:
            session.close()
    
    def _populate_expert_filter(self):
        """Populate the expert filter dropdown with all available experts."""
        # Get fresh expert options
        expert_options, expert_map = self._get_expert_options()
        self.expert_id_map = expert_map
        
        # Update expert filter options
        if hasattr(self, 'expert_filter'):
            current_value = self.expert_filter.value
            self.expert_filter.options = expert_options
            # Reset to 'All' if current value is not in the new options
            if current_value not in expert_options:
                self.expert_filter.value = 'All'
            # Force update of the select component
            self.expert_filter.update()
        
        logger.debug(f"[POPULATE] Populated expert filter with {len(expert_options)} options")
    
    def _refresh_transactions(self):
        """Refresh the transactions table."""
        logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")
        
        # Refresh expert filter options (in case new experts were added)
        self._populate_expert_filter()
        
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
    
    def _force_refresh_account_now(self):
        """Force an immediate account refresh (non-blocking)."""
        logger.info("[ACCOUNT_REFRESH] User clicked 'Force Refresh Account' button")
        try:
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()
            
            # Execute account refresh immediately as background task
            job_manager.execute_account_refresh_immediately()
            
            ui.notify('Account refresh started in background', type='info')
            logger.info("[ACCOUNT_REFRESH] Account refresh task queued successfully")
        except Exception as e:
            ui.notify(f'Error starting account refresh: {str(e)}', type='negative')
            logger.error(f"Error executing account refresh: {e}", exc_info=True)
    
    def _clean_pending_orders_dialog(self):
        """Show confirmation dialog for cleaning pending orders."""
        logger.debug("User clicked 'Clean Pending Orders' button")
        
        def confirm_clean():
            """Execute the cleanup after confirmation."""
            logger.info("User confirmed pending orders cleanup")
            try:
                from ...core.TradeManager import get_trade_manager
                trade_manager = get_trade_manager()
                
                # Execute cleanup
                stats = trade_manager.clean_pending_orders()
                
                # Show results
                if stats['errors']:
                    error_msg = '\n'.join(stats['errors'][:5])  # Show first 5 errors
                    ui.notify(
                        f"Cleanup completed with issues:\n{error_msg}",
                        type='warning'
                    )
                else:
                    ui.notify(
                        f"✓ Cleaned {stats['orders_deleted']} pending orders\n"
                        f"✓ Deleted {stats['dependents_deleted']} dependent orders\n"
                        f"✓ Closed {stats['transactions_closed']} transactions",
                        type='positive'
                    )
                
                logger.info(
                    f"Pending orders cleanup complete: "
                    f"orders={stats['orders_deleted']}, "
                    f"dependents={stats['dependents_deleted']}, "
                    f"transactions_closed={stats['transactions_closed']}, "
                    f"errors={len(stats['errors'])}"
                )
                
                # Refresh the table
                self._refresh_transactions()
                
            except Exception as e:
                ui.notify(f'Error cleaning pending orders: {str(e)}', type='negative')
                logger.error(f"Error during pending orders cleanup: {e}", exc_info=True)
        
        # Show confirmation dialog
        with ui.dialog() as dialog:
            with ui.card():
                ui.label('Clean Pending Orders?').classes('text-lg font-bold')
                ui.label(
                    'This will delete all PENDING, WAITING_TRIGGER, and ERROR orders, '
                    'along with their dependent orders. Any associated transactions will be marked as CLOSED.'
                ).classes('text-sm text-gray-600')
                ui.label(
                    '⚠️ This action cannot be undone!'
                ).classes('text-sm text-red-600 font-bold mt-2')
                
                with ui.row().classes('gap-2 mt-4'):
                    ui.button('Cancel', on_click=dialog.close).props('outline')
                    ui.button('Clean', on_click=lambda: [confirm_clean(), dialog.close()]).props('color=negative')
        
        dialog.open()
    
    def _get_transactions_data(self):
        """Get transactions data for the table.
        
        Returns:
            List of row dictionaries for the table
        """
        from ...core.models import Transaction, ExpertInstance
        from ...core.types import TransactionStatus
        from sqlmodel import col
        
        logger.debug("[TRANSACTIONS] _get_transactions_data() - START")
        
        session = get_db()
        try:
            # Build query based on filters - join with ExpertInstance for expert info
            statement = select(Transaction, ExpertInstance).outerjoin(
                ExpertInstance, Transaction.expert_id == ExpertInstance.id
            ).order_by(Transaction.created_at.desc())
            
            logger.debug(f"[TRANSACTIONS] Initial query built")
            
            # Apply status filter
            status_value = self.status_filter.value if hasattr(self, 'status_filter') else 'All'
            logger.debug(f"[TRANSACTIONS] Status filter: {status_value}")
            if status_value != 'All':
                status_map = {
                    'Open': TransactionStatus.OPENED,
                    'Closed': TransactionStatus.CLOSED,
                    'Waiting': TransactionStatus.WAITING
                }
                statement = statement.where(Transaction.status == status_map[status_value])
            
            # Apply expert filter
            if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
                # Ensure expert_id_map exists
                if hasattr(self, 'expert_id_map'):
                    expert_id = self.expert_id_map.get(self.expert_filter.value)
                    if expert_id and expert_id != 'All':
                        statement = statement.where(Transaction.expert_id == expert_id)
            
            # Apply symbol filter
            if hasattr(self, 'symbol_filter') and self.symbol_filter.value:
                logger.debug(f"[TRANSACTIONS] Symbol filter: {self.symbol_filter.value}")
                statement = statement.where(Transaction.symbol.contains(self.symbol_filter.value.upper()))
            
            # Apply broker order ID filter - requires joining with TradingOrder
            if hasattr(self, 'broker_order_id_filter') and self.broker_order_id_filter.value and self.broker_order_id_filter.value.strip():
                logger.debug(f"[TRANSACTIONS] Broker order ID filter: {self.broker_order_id_filter.value}")
                from ...core.models import TradingOrder
                # Join with TradingOrder and filter by broker_order_id
                statement = statement.join(
                    TradingOrder,
                    Transaction.id == TradingOrder.transaction_id
                ).where(
                    TradingOrder.broker_order_id.contains(self.broker_order_id_filter.value.strip())
                ).distinct()
            
            # Execute query and separate transaction and expert
            logger.debug("[TRANSACTIONS] Executing query...")
            results = list(session.exec(statement).all())
            logger.debug(f"[TRANSACTIONS] Query returned {len(results)} results")
            transactions = []
            transaction_experts = {}
            for txn, expert in results:
                transactions.append(txn)
                transaction_experts[txn.id] = expert
            
            logger.debug(f"[TRANSACTIONS] Processed {len(transactions)} transactions")
            
            if not transactions:
                # Return empty list instead of creating UI here - let the caller handle it
                logger.debug("[TRANSACTIONS] No transactions found, returning empty list")
                return []
            
            # BATCH PRICE FETCHING: Collect all symbols from open transactions grouped by account
            # This prevents individual API calls and uses batch fetching instead
            from ...core.types import TransactionStatus
            from ...core.models import TradingOrder
            from collections import defaultdict
            
            symbols_by_account = defaultdict(set)  # account_id -> set of symbols
            txn_to_account = {}  # transaction_id -> account_id mapping
            
            for txn in transactions:
                if txn.status == TransactionStatus.OPENED and txn.open_price and txn.quantity:
                    # Get account_id for this transaction
                    order_stmt = select(TradingOrder).where(TradingOrder.transaction_id == txn.id).limit(1)
                    first_order = session.exec(order_stmt).first()
                    if first_order:
                        symbols_by_account[first_order.account_id].add(txn.symbol)
                        txn_to_account[txn.id] = first_order.account_id
            
            # Fetch prices in batch for each account
            current_prices = {}  # symbol -> price mapping
            for account_id, symbols in symbols_by_account.items():
                try:
                    account_inst = get_account_instance_from_id(account_id)
                    if account_inst and symbols:
                        symbols_list = list(symbols)
                        logger.debug(f"[BATCH] Fetching {len(symbols_list)} symbols for account {account_id}: {symbols_list}")
                        # Batch fetch - returns dict {symbol: price}
                        prices_dict = account_inst.get_instrument_current_price(symbols_list)
                        if prices_dict:
                            current_prices.update(prices_dict)
                except Exception as e:
                    logger.warning(f"Batch price fetch failed for account {account_id}: {e}")
            
            # Prepare table data
            logger.debug(f"[RENDER] _render_transactions_table() - Building rows for {len(transactions)} transactions")
            rows = []
            for txn in transactions:
                # Calculate current P/L for open positions using cached/batch-fetched price
                current_pnl = ''
                current_pnl_numeric = 0  # Numeric value for sorting
                current_price_str = ''
                
                # Use batch-fetched prices for open transactions
                if txn.status == TransactionStatus.OPENED and txn.open_price and txn.quantity:
                    try:
                        current_price = current_prices.get(txn.symbol)
                        if current_price:
                            current_price_str = f"${current_price:.2f}"
                            # Calculate P/L: (current_price - open_price) * quantity
                            if txn.quantity > 0:  # Long position
                                pnl_current = (current_price - txn.open_price) * abs(txn.quantity)
                            else:  # Short position
                                pnl_current = (txn.open_price - current_price) * abs(txn.quantity)
                            current_pnl = f"${pnl_current:+.2f}"
                            current_pnl_numeric = pnl_current  # Store numeric value for sorting
                    except Exception as e:
                        logger.debug(f"Could not calculate P/L for {txn.symbol}: {e}")
                
                # Closed P/L - calculate from open/close prices
                closed_pnl = ''
                closed_pnl_numeric = 0  # Numeric value for sorting
                if txn.close_price and txn.open_price and txn.quantity:
                    if txn.quantity > 0:  # Long position
                        pnl_closed = (txn.close_price - txn.open_price) * abs(txn.quantity)
                    else:  # Short position
                        pnl_closed = (txn.open_price - txn.close_price) * abs(txn.quantity)
                    closed_pnl = f"${pnl_closed:+.2f}"
                    closed_pnl_numeric = pnl_closed  # Store numeric value for sorting
                
                # Status styling
                status_color = {
                    TransactionStatus.OPENED: 'green',
                    TransactionStatus.CLOSING: 'orange',
                    TransactionStatus.CLOSED: 'gray',
                    TransactionStatus.WAITING: 'orange'
                }.get(txn.status, 'gray')
                
                # Get expert shortname (alias + ID)
                expert = transaction_experts.get(txn.id)
                expert_shortname = ''
                if expert:
                    expert_shortname = f"{expert.alias}-{expert.id}" if expert.alias else f"Expert-{expert.id}"
                
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
                                # Dependent orders are TP/SL orders
                                # Determine if it's TP or SL by checking order type and comment
                                is_stop_order = 'stop' in order_type_display.lower()
                                is_limit_order = 'limit' in order_type_display.lower()
                                comment_lower = (order.comment or '').lower()
                                
                                # Check comment first for explicit indicators
                                if 'tp' in comment_lower or 'take_profit' in comment_lower or 'take profit' in comment_lower:
                                    order_category = 'Take Profit'
                                elif 'sl' in comment_lower or 'stop_loss' in comment_lower or 'stop loss' in comment_lower:
                                    order_category = 'Stop Loss'
                                # Fallback to order type heuristic
                                elif is_stop_order:
                                    order_category = 'Stop Loss'
                                elif is_limit_order:
                                    order_category = 'Take Profit'
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
                
                # Check if TP/SL are defined but have no valid orders
                # Valid means: order exists with correct type/price and is not CANCELED/REJECTED/ERROR
                has_missing_tpsl_orders = False
                if txn.status == TransactionStatus.OPENED:  # Only for open transactions
                    has_tp_defined = txn.take_profit is not None and txn.take_profit > 0
                    has_sl_defined = txn.stop_loss is not None and txn.stop_loss > 0
                    
                    if has_tp_defined or has_sl_defined:
                        # Check if we have valid TP/SL orders
                        from ...core.types import OrderStatus
                        
                        has_valid_tp_order = False
                        has_valid_sl_order = False
                        has_valid_bracket_order = False  # STOP_LIMIT order that covers both TP and SL
                        
                        # Invalid order statuses (terminal failed states)
                        invalid_statuses = {'canceled', 'rejected', 'error', 'expired'}
                        
                        for order in txn_orders:
                            if not order.depends_on_order:
                                continue  # Skip entry orders
                            
                            # Check if order is in valid state
                            order_status = order.status.value.lower() if hasattr(order.status, 'value') else str(order.status).lower()
                            is_valid_order = order_status not in invalid_statuses
                            
                            if not is_valid_order:
                                continue
                            
                            # Get order type
                            order_type = order.order_type.value.lower() if hasattr(order.order_type, 'value') else str(order.order_type).lower()
                            
                            # Round prices to 1 decimal for comparison
                            order_limit_price = round(order.limit_price, 1) if order.limit_price else None
                            order_stop_price = round(order.stop_price, 1) if order.stop_price else None
                            txn_tp = round(txn.take_profit, 1) if txn.take_profit else None
                            txn_sl = round(txn.stop_loss, 1) if txn.stop_loss else None
                            
                            # Check for bracket order (STOP_LIMIT that covers both TP and SL)
                            if has_tp_defined and has_sl_defined:
                                if 'stop_limit' in order_type:
                                    # For bracket orders, check if both TP (limit) and SL (stop) match
                                    if order_limit_price == txn_tp and order_stop_price == txn_sl:
                                        has_valid_bracket_order = True
                                        has_valid_tp_order = True
                                        has_valid_sl_order = True
                            
                            # Check for individual TP order (LIMIT)
                            if has_tp_defined and not has_valid_tp_order:
                                if 'limit' in order_type and 'stop' not in order_type:
                                    if order_limit_price == txn_tp:
                                        has_valid_tp_order = True
                            
                            # Check for individual SL order (STOP)
                            if has_sl_defined and not has_valid_sl_order:
                                if 'stop' in order_type and 'limit' not in order_type:
                                    if order_stop_price == txn_sl:
                                        has_valid_sl_order = True
                        
                        # Missing if TP/SL is defined but no valid order exists
                        has_missing_tpsl_orders = (has_tp_defined and not has_valid_tp_order) or (has_sl_defined and not has_valid_sl_order)
                
                row = {
                    'id': txn.id,
                    '_selected': txn.id in self.selected_transactions,  # Track selection state for checkbox
                    'symbol': txn.symbol,
                    'expert': expert_shortname,
                    'quantity': f"{txn.quantity:+.2f}",
                    'open_price': f"${txn.open_price:.2f}" if txn.open_price else '',
                    'current_price': current_price_str,
                    'close_price': f"${txn.close_price:.2f}" if txn.close_price else '',
                    'take_profit': f"${txn.take_profit:.2f}" if txn.take_profit else '',
                    'stop_loss': f"${txn.stop_loss:.2f}" if txn.stop_loss else '',
                    'current_pnl': current_pnl,
                    'current_pnl_numeric': current_pnl_numeric,  # Numeric value for sorting
                    'closed_pnl': closed_pnl,
                    'closed_pnl_numeric': closed_pnl_numeric,  # Numeric value for sorting
                    'status': txn.status.value,
                    'status_color': status_color,
                    'created_at': txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else '',
                    'closed_at': txn.close_date.strftime('%Y-%m-%d %H:%M') if txn.close_date else '',
                    'is_open': txn.status == TransactionStatus.OPENED,
                    'is_waiting': txn.status == TransactionStatus.WAITING,  # Track WAITING status
                    'is_closing': txn.status == TransactionStatus.CLOSING,  # Track CLOSING status
                    'has_missing_tpsl_orders': has_missing_tpsl_orders,  # Track if TP/SL defined but no valid orders
                    'orders': orders_data,  # Add orders for expansion
                    'order_count': len(orders_data),  # Show order count
                }
                rows.append(row)
            
            logger.debug(f"[TRANSACTIONS] Returning {len(rows)} rows")
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
        logger.debug("[RENDER] Calling _get_transactions_data()...")
        rows = self._get_transactions_data()
        logger.debug(f"[RENDER] Got {len(rows) if rows else 0} rows")
        
        if not rows:
            logger.debug("[RENDER] No rows, showing 'No transactions found' message")
            ui.label('No transactions found.').classes('text-gray-500')
            return
        
        # Table columns
        columns = [
            {'name': 'select', 'label': '', 'field': 'select', 'align': 'left', 'sortable': False},  # Selection checkbox column
            {'name': 'expand', 'label': '', 'field': 'expand', 'align': 'left'},  # Expand column
            {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'center', 'sortable': True},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert', 'align': 'left', 'sortable': True},
            {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'align': 'right', 'sortable': True},
            {'name': 'open_price', 'label': 'Open Price', 'field': 'open_price', 'align': 'right', 'sortable': True},
            {'name': 'current_price', 'label': 'Current', 'field': 'current_price', 'align': 'right'},
            {'name': 'close_price', 'label': 'Close Price', 'field': 'close_price', 'align': 'right'},
            {'name': 'take_profit', 'label': 'TP', 'field': 'take_profit', 'align': 'right'},
            {'name': 'stop_loss', 'label': 'SL', 'field': 'stop_loss', 'align': 'right'},
            {'name': 'current_pnl', 'label': 'Current P/L', 'field': 'current_pnl', 'align': 'right', 'sortable': True, 'sort_by': 'current_pnl_numeric'},
            {'name': 'closed_pnl', 'label': 'Closed P/L', 'field': 'closed_pnl', 'align': 'right', 'sortable': True, 'sort_by': 'closed_pnl_numeric'},
            {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center', 'sortable': True},
            {'name': 'order_count', 'label': 'Orders', 'field': 'order_count', 'align': 'center'},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left', 'sortable': True},
            {'name': 'closed_at', 'label': 'Closed', 'field': 'closed_at', 'align': 'left', 'sortable': True},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
        ]
        
        # Create table with expansion support
        logger.debug(f"[RENDER] _render_transactions_table() - Creating table with {len(rows)} rows")
        self.transactions_table = ui.table(
            columns=columns, 
            rows=rows, 
            row_key='id',
            pagination={'rowsPerPage': 20}
        ).classes('w-full').props('flat bordered')
        
        # Add row selection via click handler
        # Store reference to rows for selection tracking
        def on_row_click(row_data):
            """Handle row clicks to toggle selection."""
            row_id = row_data['id']
            if row_id in self.selected_transactions:
                del self.selected_transactions[row_id]
            else:
                self.selected_transactions[row_id] = True
            self._update_batch_buttons()
            # Refresh table to show updated styling
            self.transactions_table.update()
        
        # Attach click handler to table rows via Quasar's row-click event
        self.transactions_table.props('row-key="id" @row-click="(evt, row) => {}"')
        
        # We'll manually handle selection in the Vue template slot instead
        # Store the click handler for reference
        self._on_row_click = on_row_click
        
        # Add expand button in second column
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
                        <template v-if="col.name === 'select'">
                            <q-checkbox 
                                :model-value="props.row._selected || false"
                                @update:model-value="(val) => $parent.$emit('toggle_row_selection', props.row.id)"
                            />
                        </template>
                        <template v-else-if="col.name === 'expand'">
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
                            <q-btn v-if="props.row.has_missing_tpsl_orders"
                                   icon="warning"
                                   size="sm"
                                   flat
                                   round
                                   color="warning"
                                   @click="$parent.$emit('recreate_tpsl_orders', props.row.id)"
                                   title="TP/SL defined but no valid orders - Click to recreate"
                            >
                                <q-tooltip>TP/SL defined but no valid orders - Click to recreate</q-tooltip>
                            </q-btn>
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
        self.transactions_table.on('toggle_row_selection', self._toggle_row_selection)
        self.transactions_table.on('edit_transaction', self._show_edit_dialog)
        self.transactions_table.on('close_transaction', self._show_close_dialog)
        self.transactions_table.on('retry_close_transaction', self._show_retry_close_dialog)
        self.transactions_table.on('recreate_tpsl_orders', self._recreate_tpsl_orders)
        self.transactions_table.on('view_recommendation', self._show_recommendation_dialog)
        logger.debug("[RENDER] _render_transactions_table() - END (success)")
    
    def _toggle_row_selection(self, event_data):
        """Toggle selection state for a transaction row."""
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        if transaction_id in self.selected_transactions:
            del self.selected_transactions[transaction_id]
        else:
            self.selected_transactions[transaction_id] = True
        
        # Update the specific row's _selected flag in the table data
        for row in self.transactions_table.rows:
            if row['id'] == transaction_id:
                row['_selected'] = transaction_id in self.selected_transactions
                break
        
        self._update_batch_buttons()
        # Refresh table to show updated checkbox state
        self.transactions_table.update()
    
    def _recreate_tpsl_orders(self, event_data):
        """Recreate TP/SL orders for a transaction that has TP/SL defined but no valid orders."""
        from ...core.models import Transaction, TradingOrder
        from ...core.types import OrderStatus
        
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data
        
        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return
            
            # Verify transaction is open
            from ...core.types import TransactionStatus
            if txn.status != TransactionStatus.OPENED:
                ui.notify('Transaction is not open', type='negative')
                return
            
            # Verify TP or SL is defined
            if not ((txn.take_profit and txn.take_profit > 0) or (txn.stop_loss and txn.stop_loss > 0)):
                ui.notify('No TP/SL defined for this transaction', type='negative')
                return
            
            # Get the entry order to find account_id
            with get_db() as session:
                entry_order_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.depends_on_order == None  # Entry order has no dependencies
                ).limit(1)
                entry_order = session.exec(entry_order_stmt).first()
                
                if not entry_order:
                    ui.notify('Could not find entry order', type='negative')
                    return
                
                account_id = entry_order.account_id
            
            # Cancel any existing TP/SL orders that are still active
            with get_db() as session:
                existing_tpsl_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.depends_on_order != None  # TP/SL orders depend on entry
                )
                existing_tpsl_orders = list(session.exec(existing_tpsl_stmt).all())
                
                account_inst = get_account_instance_from_id(account_id)
                if not account_inst:
                    ui.notify('Could not load account instance', type='negative')
                    return
                
                for order in existing_tpsl_orders:
                    # Only cancel orders that are not already in terminal state
                    terminal_statuses = OrderStatus.get_terminal_statuses()
                    if order.status not in terminal_statuses:
                        try:
                            account_inst.cancel_order(order.id)
                            logger.info(f"Canceled existing TP/SL order {order.id} for transaction {transaction_id}")
                        except Exception as e:
                            logger.warning(f"Failed to cancel order {order.id}: {e}")
            
            # Recreate TP/SL orders using the new adjust methods (creates OCO/OTO orders)
            orders_created = []
            
            # Check if both TP and SL are defined - if so, use adjust_tp_sl for OCO order
            has_tp = txn.take_profit and txn.take_profit > 0
            has_sl = txn.stop_loss and txn.stop_loss > 0
            
            if has_tp and has_sl:
                # Both TP and SL defined - create as OCO order
                try:
                    success = account_inst.adjust_tp_sl(txn, txn.take_profit, txn.stop_loss)
                    if success:
                        orders_created.extend(['TP', 'SL'])
                        logger.info(
                            f"Created OCO order with TP at ${txn.take_profit:.2f} "
                            f"and SL at ${txn.stop_loss:.2f} "
                            f"for transaction {transaction_id}"
                        )
                    else:
                        logger.warning(f"Failed to create OCO order for transaction {transaction_id}")
                except Exception as e:
                    logger.error(f"Failed to create TP/SL OCO order: {e}", exc_info=True)
            else:
                # Only one of TP or SL defined - create separately
                if has_tp:
                    try:
                        success = account_inst.adjust_tp(txn, txn.take_profit)
                        if success:
                            orders_created.append('TP')
                            logger.info(f"Created TP order at ${txn.take_profit:.2f} for transaction {transaction_id}")
                        else:
                            logger.warning(f"Failed to create TP order for transaction {transaction_id}")
                    except Exception as e:
                        logger.error(f"Failed to create TP order: {e}", exc_info=True)
                
                if has_sl:
                    try:
                        success = account_inst.adjust_sl(txn, txn.stop_loss)
                        if success:
                            orders_created.append('SL')
                            logger.info(f"Created SL order at ${txn.stop_loss:.2f} for transaction {transaction_id}")
                        else:
                            logger.warning(f"Failed to create SL order for transaction {transaction_id}")
                    except Exception as e:
                        logger.error(f"Failed to create SL order: {e}", exc_info=True)
            
            if orders_created:
                orders_str = ' and '.join(orders_created)
                ui.notify(f'✓ {orders_str} order(s) recreated for {txn.symbol}', type='positive')
                logger.info(f"Successfully recreated {orders_str} orders for transaction {transaction_id}")
            else:
                ui.notify('No TP/SL orders were created', type='warning')
                logger.warning(f"No TP/SL orders were created for transaction {transaction_id}")
            
            # Refresh the table
            self._refresh_transactions()
            
        except Exception as e:
            logger.error(f"Error recreating TP/SL orders for transaction {transaction_id}: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')
    
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
            
            # Get account to use adjust_tp/adjust_sl methods
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
            
            # Use adjust_tp_sl if both changed, otherwise adjust individually
            tp_changed = tp_price and tp_price != txn.take_profit
            sl_changed = sl_price and sl_price != txn.stop_loss
            
            if tp_changed and sl_changed:
                # Both changed - use adjust_tp_sl for OCO order
                try:
                    success = account.adjust_tp_sl(txn, tp_price, sl_price)
                    if success:
                        ui.notify(f'TP/SL updated to ${tp_price:.2f}/${sl_price:.2f}', type='positive')
                    else:
                        ui.notify('Failed to update TP/SL', type='negative')
                except Exception as e:
                    ui.notify(f'Error updating TP/SL: {str(e)}', type='negative')
                    logger.error(f"Error updating TP/SL: {e}", exc_info=True)
            else:
                # Update TP if changed
                if tp_changed:
                    try:
                        success = account.adjust_tp(txn, tp_price)
                        if success:
                            ui.notify(f'Take Profit updated to ${tp_price:.2f}', type='positive')
                        else:
                            ui.notify('Failed to update Take Profit', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating TP: {str(e)}', type='negative')
                        logger.error(f"Error updating TP: {e}", exc_info=True)
                
                # Update SL if changed
                if sl_changed:
                    try:
                        success = account.adjust_sl(txn, sl_price)
                        if success:
                            ui.notify(f'Stop Loss updated to ${sl_price:.2f}', type='positive')
                        else:
                            ui.notify('Failed to update Stop Loss', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating SL: {str(e)}', type='negative')
                        logger.error(f"Error updating SL: {e}", exc_info=True)
            
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
    
    def _select_all_transactions(self):
        """Select all visible transactions."""
        if not self.transactions_table:
            return
        
        # Select all current rows
        for row in self.transactions_table.rows:
            self.selected_transactions[row['id']] = True
            row['_selected'] = True
        
        self._update_batch_buttons()
        # Update table to show checkboxes
        self.transactions_table.update()
    
    def _clear_selected_transactions(self):
        """Clear all selected transactions."""
        self.selected_transactions.clear()
        # Update all rows to reflect cleared selection
        if self.transactions_table:
            for row in self.transactions_table.rows:
                row['_selected'] = False
        self._update_batch_buttons()
        # Update table to hide checkboxes
        if self.transactions_table:
            self.transactions_table.update()
    
    def _update_batch_buttons(self):
        """Show/hide batch operation buttons based on selection."""
        if not hasattr(self, 'batch_close_btn'):
            return
        
        has_selection = len(self.selected_transactions) > 0
        
        self.batch_select_all_btn.set_visibility(True)
        self.batch_clear_btn.set_visibility(has_selection)
        self.batch_close_btn.set_visibility(has_selection)
        self.batch_adjust_tp_btn.set_visibility(has_selection)
    
    def _batch_close_transactions(self):
        """Show confirmation dialog and close all selected transactions."""
        if not self.selected_transactions:
            ui.notify('No transactions selected', type='warning')
            return
        
        count = len(self.selected_transactions)
        
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-sm'):
            ui.label('Confirm Batch Close').classes('text-h6 mb-4')
            
            ui.label(f'Are you sure you want to close {count} transaction{"s" if count != 1 else ""}?').classes('text-body1 mb-4')
            ui.label('This action cannot be undone.').classes('text-caption text-red-700 mb-4')
            
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Confirm Close', on_click=lambda: self._execute_batch_close(dialog)).props('color=negative')
        
        dialog.open()
    
    def _execute_batch_close(self, dialog):
        """Execute batch close operation (async, non-blocking)."""
        dialog.close()
        
        transaction_ids = list(self.selected_transactions.keys())
        if not transaction_ids:
            ui.notify('No transactions selected', type='warning')
            return
        
        # Capture context for background task
        from nicegui import context, background_tasks
        client = context.client
        
        async def batch_close_async():
            """Async batch close operation."""
            try:
                from ...core.db import get_instance
                from ...core.models import Transaction, ExpertInstance
                from ...core.utils import get_account_instance_from_id
                
                success_count = 0
                failed = []
                
                for txn_id in transaction_ids:
                    try:
                        txn = get_instance(Transaction, txn_id)
                        if not txn:
                            failed.append(txn_id)
                            continue
                        
                        # Get account from expert instance
                        account = None
                        if txn.expert_id:
                            expert_instance = get_instance(ExpertInstance, txn.expert_id)
                            if expert_instance and expert_instance.account_id:
                                account = get_account_instance_from_id(expert_instance.account_id)
                        
                        if account and hasattr(account, 'close_transaction_async'):
                            result = await account.close_transaction_async(txn_id)
                            if result.get('success'):
                                success_count += 1
                                logger.info(f"Batch close transaction {txn_id}: {result.get('message')}")
                            else:
                                failed.append(txn_id)
                                logger.warning(f"Batch close transaction {txn_id} failed: {result.get('message')}")
                        else:
                            failed.append(txn_id)
                            logger.warning(f"Cannot close transaction {txn_id}: no account found")
                    except Exception as e:
                        logger.error(f"Error closing transaction {txn_id}: {e}", exc_info=True)
                        failed.append(txn_id)
                
                # Schedule UI update
                def show_result():
                    if failed:
                        ui.notify(
                            f'Closed {success_count}/{len(transaction_ids)} transactions. {len(failed)} failed.',
                            type='warning'
                        )
                    else:
                        ui.notify(
                            f'Successfully closed {success_count} transaction{"s" if success_count != 1 else ""}',
                            type='positive'
                        )
                    self.selected_transactions.clear()
                    self._update_batch_buttons()
                    self._refresh_transactions()
                
                client.safe_invoke(show_result)
                
            except Exception as e:
                def show_error():
                    ui.notify(f'Error during batch close: {str(e)}', type='negative')
                client.safe_invoke(show_error)
                logger.error(f"Error in batch_close_async: {e}", exc_info=True)
        
        # Run async operation in background
        background_tasks.create(batch_close_async(), name=f'batch_close_{len(transaction_ids)}_txns')
        ui.notify(f'Closing {len(transaction_ids)} transaction{"s" if len(transaction_ids) != 1 else ""}...', type='info')
    
    def _batch_adjust_tp_dialog(self):
        """Show dialog to set TP percentage for batch of transactions."""
        if not self.selected_transactions:
            ui.notify('No transactions selected', type='warning')
            return
        
        count = len(self.selected_transactions)
        
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-sm'):
            ui.label('Batch Adjust Take Profit').classes('text-h6 mb-4')
            
            ui.label(f'Set TP for {count} transaction{"s" if count != 1 else ""}').classes('text-body2 mb-4')
            
            tp_percent_input = ui.number(
                label='TP % from Open Price',
                value=5.0,
                min=0.1,
                max=100.0,
                step=0.1,
                format='%.1f'
            ).classes('w-full mb-4')
            
            ui.label('Example: 5.0% means TP = Open Price × 1.05').classes('text-caption text-grey-7')
            
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Apply', on_click=lambda: self._execute_batch_adjust_tp(tp_percent_input.value, dialog)).props('color=info')
        
        dialog.open()
    
    def _execute_batch_adjust_tp(self, tp_percent: float, dialog):
        """Execute batch TP adjustment (async, non-blocking)."""
        dialog.close()
        
        transaction_ids = list(self.selected_transactions.keys())
        if not transaction_ids:
            ui.notify('No transactions selected', type='warning')
            return
        
        # Capture context for background task
        from nicegui import context, background_tasks
        client = context.client
        
        async def batch_adjust_tp_async():
            """Async batch TP adjustment."""
            try:
                from ...core.db import get_instance, update_instance, get_db
                from ...core.models import Transaction, TradingOrder, ExpertInstance
                from ...core.types import OrderType, OrderDirection
                from ...core.utils import get_account_instance_from_id
                from sqlmodel import Session, select
                
                success_count = 0
                failed = []
                existing_tp_modified = []
                new_tp_created = []
                
                for txn_id in transaction_ids:
                    try:
                        logger.info(f"[Batch TP] Processing transaction {txn_id}")
                        txn = get_instance(Transaction, txn_id)
                        if not txn:
                            logger.error(f"[Batch TP] Transaction {txn_id} not found")
                            failed.append(txn_id)
                            continue
                        
                        if not txn.open_price or txn.open_price <= 0:
                            logger.error(f"[Batch TP] Transaction {txn_id} has invalid open_price: {txn.open_price}")
                            failed.append(txn_id)
                            continue
                        
                        logger.info(f"[Batch TP] Transaction {txn_id}: symbol={txn.symbol}, open_price={txn.open_price}")
                        
                        # Get current open qty early (while txn is in session context)
                        try:
                            current_open_qty = txn.get_current_open_qty()
                            logger.info(f"[Batch TP] Transaction {txn_id}: current_open_qty={current_open_qty}")
                        except Exception as e:
                            logger.error(f"[Batch TP] Could not get current open qty for transaction {txn_id}: {e}", exc_info=True)
                            current_open_qty = 0
                        
                        # Calculate new TP price
                        new_tp_price = txn.open_price * (1 + tp_percent / 100)
                        logger.info(f"[Batch TP] Transaction {txn_id}: calculated new_tp_price={new_tp_price:.2f}")
                        
                        # Get account for order operations
                        # Transaction has expert_id, get account from expert instance
                        account = None
                        if txn.expert_id:
                            try:
                                expert_instance = get_instance(ExpertInstance, txn.expert_id)
                                if expert_instance and expert_instance.account_id:
                                    logger.info(f"[Batch TP] Transaction {txn_id}: found expert_id={txn.expert_id}, account_id={expert_instance.account_id}")
                                    account = get_account_instance_from_id(expert_instance.account_id)
                                else:
                                    logger.warning(f"[Batch TP] Transaction {txn_id}: expert {txn.expert_id} not found or has no account")
                            except Exception as e:
                                logger.error(f"[Batch TP] Transaction {txn_id}: error getting expert instance: {e}", exc_info=True)
                        else:
                            logger.warning(f"[Batch TP] Transaction {txn_id}: no expert_id")
                        
                        if not account:
                            logger.error(f"[Batch TP] Cannot adjust TP for transaction {txn_id}: no account found")
                            failed.append(txn_id)
                            continue
                        
                        logger.info(f"[Batch TP] Transaction {txn_id}: found account")
                        
                        # Use adjust_tp() to handle TP adjustment properly (creates OCO/OTO orders)
                        logger.info(f"[Batch TP] Transaction {txn_id}: calling adjust_tp with price ${new_tp_price:.2f}")
                        try:
                            success = account.adjust_tp(txn, new_tp_price)
                            if success:
                                success_count += 1
                                existing_tp_modified.append(txn_id)
                                logger.info(f"[Batch TP] Transaction {txn_id}: ✓ Successfully adjusted TP to ${new_tp_price:.2f}")
                            else:
                                failed.append(txn_id)
                                logger.error(f"[Batch TP] Transaction {txn_id}: ✗ Failed to adjust TP (adjust_tp returned False)")
                        except Exception as e:
                            logger.error(f"[Batch TP] Transaction {txn_id}: ✗ Error adjusting TP: {e}", exc_info=True)
                            failed.append(txn_id)
                    
                    except Exception as e:
                        logger.error(f"Error processing transaction {txn_id}: {e}", exc_info=True)
                        failed.append(txn_id)
                
                # Schedule UI update with detailed results
                def show_result():
                    msg_parts = [f'Updated {success_count}/{len(transaction_ids)} transactions']
                    if existing_tp_modified:
                        msg_parts.append(f'{len(existing_tp_modified)} modified existing orders')
                    if new_tp_created:
                        msg_parts.append(f'{len(new_tp_created)} created new TPs')
                    
                    message = ' • '.join(msg_parts)
                    
                    if failed:
                        message += f' • {len(failed)} failed'
                        ui.notify(message, type='warning')
                    else:
                        ui.notify(message + f' (+{tp_percent:.1f}%)', type='positive')
                    
                    self.selected_transactions.clear()
                    self._update_batch_buttons()
                    self._refresh_transactions()
                
                client.safe_invoke(show_result)
                
            except Exception as e:
                def show_error():
                    ui.notify(f'Error during batch TP adjustment: {str(e)}', type='negative')
                client.safe_invoke(show_error)
                logger.error(f"Error in batch_adjust_tp_async: {e}", exc_info=True)
        
        # Run async operation in background
        background_tasks.create(batch_adjust_tp_async(), name=f'batch_adjust_tp_{len(transaction_ids)}_txns')
        ui.notify(f'Adjusting TP for {len(transaction_ids)} transaction{"s" if len(transaction_ids) != 1 else ""}...', type='info')

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

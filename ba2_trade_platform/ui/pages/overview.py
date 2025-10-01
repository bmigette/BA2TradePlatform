from nicegui import ui
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func
from typing import Dict, Any
import requests
import aiohttp
import asyncio
import json

from ...core.db import get_all_instances, get_db, get_instance
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, AppSetting, TradingOrder
from ...core.types import MarketAnalysisStatus, OrderRecommendation, OrderStatus, OrderOpenType
from ...core.utils import get_expert_instance_from_id, get_market_analysis_id_from_order_id
from ...modules.accounts import providers
from ...logger import logger

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
                
                # Create alert banner
                with ui.card().classes('w-full bg-red-50 border-l-4 border-red-500 p-4 mb-4'):
                    with ui.row().classes('w-full items-center gap-4'):
                        ui.icon('error', size='lg').classes('text-red-600')
                        with ui.column().classes('flex-1'):
                            ui.label(f'⚠️ {error_count} Order{"s" if error_count > 1 else ""} Failed').classes('text-lg font-bold text-red-800')
                            ui.label(f'There {"are" if error_count > 1 else "is"} {error_count} order{"s" if error_count > 1 else ""} with ERROR status that need{"" if error_count > 1 else "s"} attention.').classes('text-sm text-red-700')
                        
                        # View details button
                        ui.button('View Orders', on_click=lambda: ui.navigate.to('/trading')).props('outline color=red')
                
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
                            ui.button('Run Risk Management', on_click=lambda: self._handle_risk_management_from_overview(pending_orders)).props('outline color=green')
                
                # Log the pending orders
                logger.info(f"Found {pending_count} pending orders on overview page")
                for order in pending_orders:
                    logger.debug(f"PENDING Order {order.id}: {order.symbol} - {order.comment}")
        
        except Exception as e:
            logger.error(f"Error checking for pending orders: {e}", exc_info=True)
        finally:
            session.close()
    
    def render(self):
        """Render the overview tab                row = {
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
                    'waited_status': 'Waiting for trigger' if order.status == OrderStatus.WAITING_TRIGGER else '',
                    'can_submit': order.status == OrderStatus.PENDING and not order.broker_order_id
                }."""
        
        # Check for ERROR orders and display alert
        self._check_and_display_error_orders()
        
        # Check for PENDING orders and display notification
        self._check_and_display_pending_orders(self.tabs_ref)
        
        with ui.grid(columns=2).classes('w-full gap-4'):
            # Row 1: OpenAI Spending and Analysis Jobs
            self._render_openai_spending_widget()
            self._render_analysis_jobs_widget()
            
            # Row 2: Order Statistics per Account and Order Recommendations
            self._render_order_statistics_widget()
            with ui.column().classes(''):
                self._render_order_recommendations_widget()
    
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
            print(f"Error getting recommendation counts: {e}")
        
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


class AccountOverviewTab:
    def __init__(self):
        self.render()

    def render(self):
        accounts = get_all_instances(AccountDefinition)
        all_positions = []
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
                        # Format all float values to 2 decimal places
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
            ui.label('Pending Orders (PENDING or WAITING_TRIGGER)').classes('text-h6 mb-4')
            self._render_pending_orders_table()
    
    def _render_live_orders_table(self):
        """Render table with recent orders from account providers (past 15 days)."""
        accounts = get_all_instances(AccountDefinition)
        all_orders = []
        
        # Calculate cutoff date (15 days ago) - use UTC to avoid timezone issues
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=15)
        
        for acc in accounts:
            provider_cls = providers.get(acc.provider)
            if provider_cls:
                try:
                    provider_obj = provider_cls(acc.id)
                    # Get recent orders - check if provider supports get_orders method
                    if hasattr(provider_obj, 'get_orders'):
                        orders = provider_obj.get_orders()
                        
                        # Filter and format orders for table display
                        for order in orders:
                            order_dict = order if isinstance(order, dict) else dict(order)
                            
                            # Filter by date - check for created_at or submitted_at fields
                            order_date = None
                            if hasattr(order, 'created_at') and order.created_at:
                                order_date = order.created_at
                            elif hasattr(order, 'submitted_at') and order.submitted_at:
                                order_date = order.submitted_at
                            elif 'created_at' in order_dict and order_dict['created_at']:
                                # Handle string dates if needed
                                if isinstance(order_dict['created_at'], str):
                                    try:
                                        # Try parsing ISO format datetime string
                                        order_date = datetime.fromisoformat(order_dict['created_at'].replace('Z', '+00:00'))
                                    except:
                                        continue  # Skip if can't parse date
                                else:
                                    order_date = order_dict['created_at']
                            elif 'submitted_at' in order_dict and order_dict['submitted_at']:
                                if isinstance(order_dict['submitted_at'], str):
                                    try:
                                        order_date = datetime.fromisoformat(order_dict['submitted_at'].replace('Z', '+00:00'))
                                    except:
                                        continue
                                else:
                                    order_date = order_dict['submitted_at']
                            
                            # Ensure both dates are timezone-aware for comparison
                            if order_date:
                                # If order_date is naive, assume it's UTC
                                if order_date.tzinfo is None:
                                    order_date = order_date.replace(tzinfo=timezone.utc)
                                
                                # Skip orders older than 15 days
                                if order_date < cutoff_date:
                                    continue
                            
                            order_dict['account'] = acc.name
                            order_dict['provider'] = acc.provider
                            
                            # Format float values
                            for k, v in order_dict.items():
                                if isinstance(v, float):
                                    order_dict[k] = f"{v:.2f}"
                            
                            # Add comment field for all orders
                            order_dict['comment'] = order_dict.get('comment', '')
                            
                            all_orders.append(order_dict)
                    else:
                        # Provider doesn't support orders - add info row
                        all_orders.append({
                            'account': acc.name,
                            'provider': acc.provider,
                            'symbol': 'N/A',
                            'side': 'Orders not supported',
                            'qty': '',
                            'order_type': '',
                            'status': '',
                            'submitted_at': '',
                            'filled_at': '',
                            'comment': ''
                        })
                except Exception as e:
                    # Add error row if can't fetch orders
                    logger.error(f"Error fetching orders for account {acc.name}: {e}", exc_info=True)
                    all_orders.append({
                        'account': acc.name,
                        'provider': acc.provider,
                        'symbol': 'ERROR',
                        'side': str(e),
                        'qty': '',
                        'order_type': '',
                        'status': '',
                        'submitted_at': '',
                        'filled_at': '',
                        'comment': ''
                    })
        
        # Define columns for orders table
        order_columns = [
            {'name': 'account', 'label': 'Account', 'field': 'account'},
            {'name': 'provider', 'label': 'Provider', 'field': 'provider'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol'},
            {'name': 'side', 'label': 'Side', 'field': 'side'},
            {'name': 'qty', 'label': 'Quantity', 'field': 'qty'},
            {'name': 'order_type', 'label': 'Order Type', 'field': 'order_type'},
            {'name': 'status', 'label': 'Status', 'field': 'status'},
            {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price'},
            {'name': 'filled_price', 'label': 'Filled Price', 'field': 'filled_price'},
            {'name': 'submitted_at', 'label': 'Submitted', 'field': 'submitted_at'},
            {'name': 'filled_at', 'label': 'Filled', 'field': 'filled_at'},
            {'name': 'comment', 'label': 'Comment', 'field': 'comment'}
        ]
        
        if all_orders:
            ui.table(columns=order_columns, rows=all_orders, row_key='account').classes('w-full')
        else:
            ui.label('No orders found or no accounts configured.').classes('text-gray-500')
    
    def _render_pending_orders_table(self):
        """Render table with pending orders from database (PENDING or WAITING_TRIGGER)."""
        session = get_db()
        try:
            # Get orders with PENDING or WAITING_TRIGGER status
            pending_statuses = [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]
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
            
            table = ui.table(columns=columns, rows=rows, row_key='order_id').classes('w-full')
            
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

class TradeHistoryTab:
    def __init__(self):
        self.render()
    
    def render(self):
        """Render the trade history tab with orders in OPEN, CLOSED, or FILLED states."""
        with ui.card():
            ui.label('📋 Trade History').classes('text-h6 mb-4')
            self._render_orders_table()
    
    def _render_orders_table(self):
        """Render table with orders in OPEN, CLOSED, or FILLED states."""
        session = get_db()
        try:
            # Get orders with the relevant statuses
            relevant_statuses = [OrderStatus.OPEN, OrderStatus.CLOSED, OrderStatus.FILLED]
            statement = (
                select(TradingOrder)
                .where(TradingOrder.status.in_(relevant_statuses))
                .order_by(TradingOrder.created_at.desc())
            )
            orders = list(session.exec(statement).all())
            
            if not orders:
                ui.label('No orders found with OPEN, CLOSED, or FILLED status.').classes('text-gray-500')
                return
            
            # Prepare data for table
            rows = []
            for order in orders:
                # Get expert information if order has linked recommendation
                expert_name = ""
                analysis_link = ""
                
                if order.expert_recommendation_id:
                    # Get market analysis ID for navigation
                    analysis_id = get_market_analysis_id_from_order_id(order.id)
                    if analysis_id:
                        analysis_link = f"/market_analysis/{analysis_id}"
                    
                    # Get expert shortname
                    try:
                        expert_recommendation = get_instance(ExpertRecommendation, order.expert_recommendation_id)
                        if expert_recommendation:
                            expert_instance = get_expert_instance_from_id(expert_recommendation.instance_id)
                            if expert_instance and hasattr(expert_instance, 'shortname'):
                                expert_name = expert_instance.shortname
                    except Exception:
                        pass
                
                # Format dates
                created_at_str = order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else ''
                
                # Status badge color
                status_color = {
                    OrderStatus.OPEN: 'orange',
                    OrderStatus.CLOSED: 'gray', 
                    OrderStatus.FILLED: 'green'
                }.get(order.status, 'gray')
                
                row = {
                    'id': order.id,
                    'symbol': order.symbol,
                    'side': order.side,
                    'quantity': f"{order.quantity:.2f}" if order.quantity else '',
                    'order_type': order.order_type,
                    'status': order.status,
                    'status_color': status_color,
                    'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                    'filled_qty': f"{order.filled_qty:.2f}" if order.filled_qty else '',
                    'expert_name': expert_name,
                    'created_at': created_at_str,
                    'analysis_link': analysis_link,
                    'has_analysis': bool(analysis_link)
                }
                rows.append(row)
            
            # Define table columns
            columns = [
                {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
                {'name': 'side', 'label': 'Side', 'field': 'side', 'align': 'left'},
                {'name': 'quantity', 'label': 'Quantity', 'field': 'quantity', 'align': 'right'},
                {'name': 'order_type', 'label': 'Type', 'field': 'order_type', 'align': 'left'},
                {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center'},
                {'name': 'limit_price', 'label': 'Limit Price', 'field': 'limit_price', 'align': 'right'},
                {'name': 'filled_qty', 'label': 'Filled', 'field': 'filled_qty', 'align': 'right'},
                {'name': 'expert_name', 'label': 'Expert', 'field': 'expert_name', 'align': 'left'},
                {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left'},
                {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
            ]
            
            # Create table with custom cell rendering
            table = ui.table(columns=columns, rows=rows, row_key='id').classes('w-full')
            
            # Add custom cell rendering for status badges and action buttons
            table.add_slot('body-cell-status', '''
                <q-td :props="props">
                    <q-badge :color="props.row.status_color" :label="props.row.status" />
                </q-td>
            ''')
            
            table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn v-if="props.row.has_analysis" 
                           icon="analytics" 
                           size="sm" 
                           flat 
                           round 
                           color="primary"
                           @click="$parent.$emit('navigate_to_analysis', props.row.analysis_link)"
                           :title="'View Market Analysis'"
                    />
                    <span v-else class="text-grey-5">—</span>
                </q-td>
            ''')
            
            # Handle navigation events
            def handle_navigation(e):
                analysis_link = e.args
                if analysis_link:
                    # Navigate to the market analysis page
                    ui.navigate.to(analysis_link)
            
            table.on('navigate_to_analysis', handle_navigation)
            
        except Exception as e:
            logger.error(f"Error rendering orders table: {e}", exc_info=True)
            ui.label(f'Error loading orders: {str(e)}').classes('text-red-500')
        finally:
            session.close()

class PerformanceTab:
    def __init__(self):
        self.render()
    def render(self):
        with ui.card():
            ui.label('Performance metrics and analytics will be displayed here.')

def content() -> None:
    # Tab configuration: (tab_name, tab_label)
    tab_config = [
        ('overview', 'Overview'),
        ('account', 'Account Overview'),
        ('history', 'Trade History'),
        ('performance', 'Performance')
    ]
    
    with ui.tabs() as tabs:
        tab_objects = {}
        for tab_name, tab_label in tab_config:
            tab_objects[tab_name] = ui.tab(tab_name, label=tab_label)

    with ui.tab_panels(tabs, value=tab_objects['overview']).classes('w-full'):
        with ui.tab_panel(tab_objects['overview']):
            OverviewTab(tabs_ref=tabs)
        with ui.tab_panel(tab_objects['account']):
            AccountOverviewTab()
        with ui.tab_panel(tab_objects['history']):
            TradeHistoryTab()
        with ui.tab_panel(tab_objects['performance']):
            PerformanceTab()
    
    # Setup HTML5 history navigation for tabs using timer for async compatibility
    async def setup_tab_navigation():
        await ui.run_javascript('''
            (function() {
                let isPopstateNavigation = false;
                
                // Map display labels to tab names
                const labelToName = {
                    'Overview': 'overview',
                    'Account Overview': 'account',
                    'Trade History': 'history',
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
                    const tabs = document.querySelectorAll('.q-tab[name]');
                    tabs.forEach(tab => {
                        if (tab.getAttribute('name') === hash) {
                            tab.click();
                        }
                    });
                } else if (!hash) {
                    // Set initial hash if none exists
                    history.replaceState({tab: 'overview'}, '', '#overview');
                }
                
                setupTabClickHandlers();
            })();
        ''')
    
    # Use timer to run JavaScript after page is loaded (increased delay to ensure tabs are rendered)
    ui.timer(0.5, setup_tab_navigation, once=True)

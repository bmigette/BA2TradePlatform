from nicegui import ui
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func
from typing import Dict, Any
import requests
import aiohttp
import asyncio
import json

from ...core.db import get_all_instances, get_db
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, AppSetting
from ...core.types import MarketAnalysisStatus, OrderRecommendation
from ...modules.accounts import providers
from ...logger import logger

class OverviewTab:
    def __init__(self):
        self.render()
    
    def render(self):
        """Render the overview tab with widgets."""
        with ui.grid(columns=2).classes('w-full gap-4'):
            # Row 1: OpenAI Spending and Analysis Jobs
            self._render_openai_spending_widget()
            self._render_analysis_jobs_widget()
            
            # Row 2: Order Recommendations (spanning both columns)
            with ui.column().classes('col-span-2'):
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
            
            # Clear loading message
            loading_label.delete()
            
            # Populate content
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
        except Exception as e:
            # Clear loading message and show error
            loading_label.delete()
            with content_container:
                ui.label('❌ Failed to load usage data').classes('text-sm text-red-600')
                ui.label(f'Error: {str(e)}').classes('text-xs text-gray-500')
    
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
                            logger.error(f'OpenAI API error {response.status}: {error_text}')
                            return {'error': f'OpenAI API error ({response.status}): {error_text[:100]}...'}
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling OpenAI costs API: {e}')
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
            logger.error(f'Error fetching OpenAI usage data: {e}')
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logger.error(f'Unexpected error fetching OpenAI usage data: {e}')
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
                    logger.error(f'OpenAI API error {response.status_code}: {error_text}')
                    return {'error': f'OpenAI API error ({response.status_code}): {error_text[:100]}...'}
                    
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling OpenAI costs API: {e}')
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
            logger.error(f'Error fetching OpenAI usage data: {e}')
            return {'error': f'Network error: {str(e)}'}
        except Exception as e:
            logger.error(f'Unexpected error fetching OpenAI usage data: {e}')
            return {'error': f'Unexpected error: {str(e)}'}

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
            ui.label('Open Positions Across All Accounts')
            ui.table(columns=columns, rows=all_positions, row_key='account').classes('w-full')
        
        # All Orders Table
        with ui.card().classes('mt-4'):
            ui.label('Recent Orders from All Accounts (Past 15 Days)').classes('text-h6 mb-4')
            self._render_account_orders_table()
    
    def _render_account_orders_table(self):
        """Render table with recent orders from all accounts (past 15 days)."""
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
                            'filled_at': ''
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
                        'filled_at': ''
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
            {'name': 'filled_at', 'label': 'Filled', 'field': 'filled_at'}
        ]
        
        if all_orders:
            ui.table(columns=order_columns, rows=all_orders, row_key='account').classes('w-full')
        else:
            ui.label('No orders found or no accounts configured.').classes('text-gray-500')

class TradeHistoryTab:
    def __init__(self):
        self.render()
    def render(self):
        with ui.card():
            ui.label('Trade History content goes here.')

class PerformanceTab:
    def __init__(self):
        self.render()
    def render(self):
        with ui.card():
            ui.label('Performance metrics and analytics will be displayed here.')

def content() -> None:
    with ui.tabs() as tabs:
        overview_tab = ui.tab('Overview')
        ui.tab('Account Overview')
        ui.tab('Trade History')
        ui.tab('Performance')

    with ui.tab_panels(tabs, value=overview_tab).classes('w-full'):
        with ui.tab_panel(overview_tab):
            OverviewTab()
        with ui.tab_panel('Account Overview'):
            AccountOverviewTab()
        with ui.tab_panel('Trade History'):
            TradeHistoryTab()
        with ui.tab_panel('Performance'):
            PerformanceTab()
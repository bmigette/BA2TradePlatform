"""
Model Billing and Usage Module - Centralized API usage tracking for all LLM providers.

This module provides functions to check API key usage, credits left, and billing
information for all supported LLM providers.

Usage:
    from ba2_trade_platform.core.ModelBillingUsage import ModelBillingUsage
    
    # Get OpenAI usage data
    openai_data = await ModelBillingUsage.get_openai_usage_async()
    
    # Get NagaAI usage data
    naga_data = await ModelBillingUsage.get_nagaai_usage_async()
    
    # Get all providers usage data
    all_data = await ModelBillingUsage.get_all_providers_usage_async()
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import asyncio
import aiohttp
import requests

from sqlmodel import select

from .models_registry import PROVIDER_CONFIG, PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_XAI, PROVIDER_MOONSHOT, PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE, PROVIDER_OPENROUTER
from ..logger import logger


class ModelBillingUsage:
    """
    Centralized class for checking API usage and billing information across all LLM providers.
    
    Provides both synchronous and asynchronous methods for fetching usage data.
    """
    
    # Cache for API keys to avoid repeated database lookups
    _api_key_cache: Dict[str, Optional[str]] = {}
    
    @classmethod
    def _get_app_setting(cls, key: str) -> Optional[str]:
        """Get an app setting value from the database.
        
        Args:
            key: The setting key to look up
            
        Returns:
            The setting value or None if not found
        """
        from .db import get_db
        from .models import AppSetting
        
        # Check cache first
        if key in cls._api_key_cache:
            return cls._api_key_cache[key]
        
        session = get_db()
        try:
            setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            value = setting.value_str if setting else None
            cls._api_key_cache[key] = value
            return value
        finally:
            session.close()
    
    @classmethod
    def clear_cache(cls):
        """Clear the API key cache. Call this after settings are updated."""
        cls._api_key_cache.clear()
    
    @classmethod
    def get_provider_api_key(cls, provider: str, admin: bool = False) -> Optional[str]:
        """Get the API key for a specific provider.
        
        Args:
            provider: The provider name (e.g., 'openai', 'nagaai')
            admin: If True, get the admin API key (for usage data access)
            
        Returns:
            The API key or None if not configured
        """
        provider_config = PROVIDER_CONFIG.get(provider)
        if not provider_config:
            return None
        
        base_key = provider_config.get("api_key_setting")
        if not base_key:
            return None
        
        if admin:
            # Try admin key first for providers that have one
            admin_key = base_key.replace("_api_key", "_admin_api_key")
            value = cls._get_app_setting(admin_key)
            if value:
                return value
        
        return cls._get_app_setting(base_key)
    
    # =========================================================================
    # OpenAI Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_openai_usage_async(cls) -> Dict[str, Any]:
        """Fetch real OpenAI usage data from the API asynchronously.
        
        Returns:
            Dictionary with usage data including:
            - week_cost: Cost for the past week
            - month_cost: Cost for the past month
            - remaining_credit: Remaining credit (if available)
            - last_updated: Timestamp of the data
            - error: Error message if any
        """
        try:
            # Get OpenAI API key (prefer admin key for usage data)
            admin_key = cls._get_app_setting('openai_admin_api_key')
            regular_key = cls._get_app_setting('openai_api_key')
            
            api_key = None
            key_type = None
            
            if admin_key:
                # Validate admin key format
                if not admin_key.startswith("sk-admin"):
                    return {
                        'error': 'Invalid admin key format. Admin keys should start with "sk-admin".',
                        'link': 'https://platform.openai.com/settings/organization/api-keys'
                    }
                api_key = admin_key
                key_type = 'admin'
            elif regular_key:
                api_key = regular_key
                key_type = 'regular'
            
            if not api_key:
                return {'error': 'OpenAI API key not configured in settings'}
            
            # Calculate date ranges
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            # Fetch usage data from OpenAI API
            headers = {
                'Authorization': f'Bearer {api_key}'
            }
            
            week_cost = 0
            month_cost = 0
            
            # Get costs for the past month using the correct API
            costs_url = 'https://api.openai.com/v1/organization/costs'
            params = {
                'start_time': int(month_ago.timestamp()),
                'end_time': int(now.timestamp()),
                'bucket_width': '1d',
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
                                daily_cost = 0.0
                                for result in cost_bucket.get('results', []):
                                    amount = result.get('amount', {})
                                    value = amount.get('value', 0)
                                    try:
                                        daily_cost += float(value) if value else 0.0
                                    except (ValueError, TypeError):
                                        logger.warning(f"[OpenAI Usage] Invalid amount value: {value}")
                                        continue
                                
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
                                'provider': 'openai',
                                'provider_display': 'OpenAI',
                                'week_cost': week_cost,
                                'month_cost': month_cost,
                                'remaining_credit': remaining_credit,
                                'hard_limit': hard_limit,
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        
                        elif response.status == 401:
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
                        elif response.status == 500:
                            return {'error': 'OpenAI server error (500) - their API may be experiencing issues. Try again later.'}
                        else:
                            error_text = await response.text()
                            logger.error(f'OpenAI API error {response.status}: {error_text}')
                            return {'error': f'OpenAI API error ({response.status}): {error_text[:150]}...'}
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling OpenAI costs API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
            
            return {
                'provider': 'openai',
                'provider_display': 'OpenAI',
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
    
    @classmethod
    def get_openai_usage_sync(cls) -> Dict[str, Any]:
        """Fetch real OpenAI usage data from the API synchronously.
        
        Returns:
            Dictionary with usage data (same format as async version)
        """
        try:
            # Get OpenAI API key (prefer admin key for usage data)
            admin_key = cls._get_app_setting('openai_admin_api_key')
            regular_key = cls._get_app_setting('openai_api_key')
            
            api_key = None
            key_type = None
            
            if admin_key:
                if not admin_key.startswith("sk-admin"):
                    return {
                        'error': 'Invalid admin key format. Admin keys should start with "sk-admin".',
                        'link': 'https://platform.openai.com/settings/organization/api-keys'
                    }
                api_key = admin_key
                key_type = 'admin'
            elif regular_key:
                api_key = regular_key
                key_type = 'regular'
            
            if not api_key:
                return {'error': 'OpenAI API key not configured in settings'}
            
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            headers = {'Authorization': f'Bearer {api_key}'}
            
            week_cost = 0
            month_cost = 0
            
            costs_url = 'https://api.openai.com/v1/organization/costs'
            params = {
                'start_time': int(month_ago.timestamp()),
                'end_time': int(now.timestamp()),
                'bucket_width': '1d',
                'limit': 35
            }
            
            try:
                response = requests.get(costs_url, headers=headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    costs_data = response.json()
                    
                    for cost_bucket in costs_data.get('data', []):
                        bucket_start_time = cost_bucket.get('start_time', 0)
                        bucket_date = datetime.fromtimestamp(bucket_start_time)
                        
                        daily_cost = 0
                        for result in cost_bucket.get('results', []):
                            amount = result.get('amount', {})
                            daily_cost += amount.get('value', 0)
                        
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
                        'provider': 'openai',
                        'provider_display': 'OpenAI',
                        'week_cost': week_cost,
                        'month_cost': month_cost,
                        'remaining_credit': remaining_credit,
                        'hard_limit': hard_limit,
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                    }
                
                elif response.status_code == 401:
                    try:
                        error_data = response.json()
                        error_message = error_data.get('error', {}).get('message', '')
                        
                        if 'insufficient permissions' in error_message.lower() and 'api.usage.read' in error_message:
                            if key_type == 'admin':
                                return {'error': 'Admin API key is invalid'}
                            else:
                                return {'error': 'Regular API key lacks usage permissions. Need OpenAI Admin API key.'}
                        else:
                            return {'error': f'Invalid OpenAI API key - {error_message}'}
                    except:
                        return {'error': 'Invalid OpenAI API key'}
                elif response.status_code == 403:
                    return {'error': 'API key does not have permission to access billing data'}
                elif response.status_code == 429:
                    return {'error': 'OpenAI API rate limit exceeded'}
                elif response.status_code == 500:
                    return {'error': 'OpenAI server error (500)'}
                else:
                    return {'error': f'OpenAI API error ({response.status_code})'}
                    
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling OpenAI costs API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except requests.exceptions.Timeout:
            return {'error': 'Request timeout'}
        except Exception as e:
            logger.error(f'Unexpected error fetching OpenAI usage data: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # NagaAI Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_nagaai_usage_async(cls) -> Dict[str, Any]:
        """Fetch Naga AI usage data from the API asynchronously.
        
        Returns:
            Dictionary with usage data including:
            - week_cost: Cost for the past week
            - month_cost: Cost for the past month
            - remaining_credit: Account balance
            - last_updated: Timestamp of the data
            - error: Error message if any
        """
        try:
            # Get Naga AI admin API key
            api_key = cls._get_app_setting('naga_ai_admin_api_key')
            
            if not api_key:
                return {'error': 'Naga AI Admin API key not configured in settings'}
            
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            headers = {'Authorization': f'Bearer {api_key}'}
            
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
                        daily_stats = activity_data.get('daily_stats', [])
                        
                        if daily_stats:
                            for day_stat in daily_stats:
                                date_str = day_stat.get('date')
                                if date_str:
                                    try:
                                        try:
                                            day_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                        except:
                                            day_date = datetime.strptime(date_str, '%Y-%m-%d')
                                        
                                        cost_str = day_stat.get('total_cost', '0')
                                        try:
                                            cost = float(cost_str)
                                        except (ValueError, TypeError):
                                            cost = 0
                                        
                                        if day_date >= week_ago:
                                            week_cost += abs(cost)
                                        if day_date >= month_ago:
                                            month_cost += abs(cost)
                                    except Exception as e:
                                        logger.debug(f"Error parsing daily stat: {e}")
                                        continue
                        else:
                            total_stats = activity_data.get('total_stats', {})
                            if total_stats:
                                total_cost_str = total_stats.get('total_cost', '0')
                                try:
                                    total_cost = float(total_cost_str)
                                    month_cost = abs(total_cost)
                                    week_cost = month_cost
                                except (ValueError, TypeError):
                                    pass
                    
                    return {
                        'provider': 'nagaai',
                        'provider_display': 'Naga AI',
                        'week_cost': week_cost,
                        'month_cost': month_cost,
                        'remaining_credit': remaining_credit,
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
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
    
    @classmethod
    def get_nagaai_usage_sync(cls) -> Dict[str, Any]:
        """Fetch Naga AI usage data from the API synchronously.
        
        Returns:
            Dictionary with usage data (same format as async version)
        """
        try:
            api_key = cls._get_app_setting('naga_ai_admin_api_key')
            
            if not api_key:
                return {'error': 'Naga AI Admin API key not configured in settings'}
            
            now = datetime.now()
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            headers = {'Authorization': f'Bearer {api_key}'}
            
            try:
                # Get account balance
                balance_url = 'https://api.naga.ac/v1/account/balance'
                balance_response = requests.get(balance_url, headers=headers, timeout=10)
                balance_data = None
                
                if balance_response.status_code == 200:
                    balance_data = balance_response.json()
                elif balance_response.status_code == 401:
                    return {'error': 'Invalid Naga AI Admin API key'}
                
                # Get account activity
                activity_url = 'https://api.naga.ac/v1/account/activity'
                activity_response = requests.get(activity_url, headers=headers, timeout=10)
                activity_data = None
                
                if activity_response.status_code == 200:
                    activity_data = activity_response.json()
                elif activity_response.status_code == 401:
                    return {'error': 'Invalid Naga AI Admin API key'}
                
                # Process the data
                week_cost = 0
                month_cost = 0
                remaining_credit = None
                
                if balance_data:
                    balance_str = balance_data.get('balance', '0')
                    try:
                        remaining_credit = float(balance_str)
                    except (ValueError, TypeError):
                        remaining_credit = 0
                
                if activity_data:
                    daily_stats = activity_data.get('daily_stats', [])
                    
                    if daily_stats:
                        for day_stat in daily_stats:
                            date_str = day_stat.get('date')
                            if date_str:
                                try:
                                    try:
                                        day_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                    except:
                                        day_date = datetime.strptime(date_str, '%Y-%m-%d')
                                    
                                    cost_str = day_stat.get('total_cost', '0')
                                    try:
                                        cost = float(cost_str)
                                    except (ValueError, TypeError):
                                        cost = 0
                                    
                                    if day_date >= week_ago:
                                        week_cost += abs(cost)
                                    if day_date >= month_ago:
                                        month_cost += abs(cost)
                                except Exception as e:
                                    logger.debug(f"Error parsing daily stat: {e}")
                                    continue
                    else:
                        total_stats = activity_data.get('total_stats', {})
                        if total_stats:
                            total_cost_str = total_stats.get('total_cost', '0')
                            try:
                                total_cost = float(total_cost_str)
                                month_cost = abs(total_cost)
                                week_cost = month_cost
                            except (ValueError, TypeError):
                                pass
                
                return {
                    'provider': 'nagaai',
                    'provider_display': 'Naga AI',
                    'week_cost': week_cost,
                    'month_cost': month_cost,
                    'remaining_credit': remaining_credit,
                    'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                }
                
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling Naga AI API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except requests.exceptions.Timeout:
            return {'error': 'Request timeout'}
        except Exception as e:
            logger.error(f'Unexpected error fetching Naga AI usage data: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # Anthropic Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_anthropic_usage_async(cls) -> Dict[str, Any]:
        """Fetch Anthropic usage data from the API asynchronously.
        
        Note: Anthropic's API doesn't provide direct usage/billing endpoints.
        This returns a placeholder indicating the API key status.
        
        Returns:
            Dictionary with usage status
        """
        try:
            api_key = cls._get_app_setting('anthropic_api_key')
            
            if not api_key:
                return {'error': 'Anthropic API key not configured in settings'}
            
            # Anthropic doesn't have a public usage API - just validate the key
            headers = {
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json'
            }
            
            try:
                async with aiohttp.ClientSession() as session:
                    # Try a minimal API call to verify the key works
                    test_url = 'https://api.anthropic.com/v1/messages'
                    test_payload = {
                        'model': 'claude-3-haiku-20240307',
                        'max_tokens': 1,
                        'messages': [{'role': 'user', 'content': 'test'}]
                    }
                    
                    async with session.post(test_url, headers=headers, json=test_payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200 or response.status == 400:  # 400 means key is valid but request may be malformed
                            return {
                                'provider': 'anthropic',
                                'provider_display': 'Anthropic',
                                'status': 'configured',
                                'note': 'Anthropic does not provide a usage API. Check console.anthropic.com for usage.',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        elif response.status == 401:
                            return {'error': 'Invalid Anthropic API key'}
                        else:
                            return {
                                'provider': 'anthropic',
                                'provider_display': 'Anthropic',
                                'status': 'unknown',
                                'note': 'Could not verify API key status',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling Anthropic API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking Anthropic API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # xAI (Grok) Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_xai_usage_async(cls) -> Dict[str, Any]:
        """Fetch xAI (Grok) usage data from the API asynchronously.
        
        Note: xAI's API may not provide direct usage/billing endpoints.
        This validates the API key status.
        
        Returns:
            Dictionary with usage status
        """
        try:
            api_key = cls._get_app_setting('xai_api_key')
            
            if not api_key:
                return {'error': 'xAI API key not configured in settings'}
            
            headers = {'Authorization': f'Bearer {api_key}'}
            
            try:
                async with aiohttp.ClientSession() as session:
                    # Try to get models list to verify key
                    test_url = 'https://api.x.ai/v1/models'
                    
                    async with session.get(test_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            return {
                                'provider': 'xai',
                                'provider_display': 'xAI (Grok)',
                                'status': 'configured',
                                'note': 'xAI usage details available at console.x.ai',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        elif response.status == 401:
                            return {'error': 'Invalid xAI API key'}
                        else:
                            return {
                                'provider': 'xai',
                                'provider_display': 'xAI (Grok)',
                                'status': 'unknown',
                                'note': 'Could not verify API key status',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling xAI API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking xAI API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # DeepSeek Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_deepseek_usage_async(cls) -> Dict[str, Any]:
        """Fetch DeepSeek usage data from the API asynchronously.
        
        DeepSeek provides a balance API endpoint at /user/balance.
        Returns balance info including available balance and currency.
        
        Returns:
            Dictionary with usage status and balance
        """
        try:
            api_key = cls._get_app_setting('deepseek_api_key')
            
            if not api_key:
                return {'error': 'DeepSeek API key not configured in settings'}
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json'
            }
            
            now = datetime.now()
            
            try:
                async with aiohttp.ClientSession() as session:
                    # DeepSeek balance endpoint: GET https://api.deepseek.com/user/balance
                    balance_url = 'https://api.deepseek.com/user/balance'
                    
                    async with session.get(balance_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        logger.debug(f'[DeepSeek Usage] Balance API response status: {response.status}')
                        
                        if response.status == 200:
                            data = await response.json()
                            logger.debug(f'[DeepSeek Usage] Balance response: {data}')
                            
                            # DeepSeek returns:
                            # {
                            #   "is_available": true,
                            #   "balance_infos": [
                            #     {"currency": "CNY", "total_balance": "10.00", "granted_balance": "0.00", "topped_up_balance": "10.00"}
                            #   ]
                            # }
                            is_available = data.get('is_available', True)
                            balance_infos = data.get('balance_infos', [])
                            
                            total_balance = 0.0
                            currency = 'CNY'
                            granted_balance = 0.0
                            topped_up_balance = 0.0
                            
                            if balance_infos:
                                # Usually just one entry, but handle multiple
                                for info in balance_infos:
                                    currency = info.get('currency', 'CNY')
                                    try:
                                        total_balance += float(info.get('total_balance', 0))
                                        granted_balance += float(info.get('granted_balance', 0))
                                        topped_up_balance += float(info.get('topped_up_balance', 0))
                                    except (ValueError, TypeError) as e:
                                        logger.warning(f'[DeepSeek Usage] Error parsing balance: {e}')
                            
                            return {
                                'provider': 'deepseek',
                                'provider_display': 'DeepSeek',
                                'remaining_credit': total_balance,
                                'granted_balance': granted_balance,
                                'topped_up_balance': topped_up_balance,
                                'currency': currency,
                                'is_available': is_available,
                                'status': 'configured' if is_available else 'insufficient_balance',
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        elif response.status == 401:
                            error_text = await response.text()
                            logger.error(f'[DeepSeek Usage] Auth error: {error_text}')
                            return {'error': 'Invalid DeepSeek API key'}
                        elif response.status == 403:
                            return {'error': 'DeepSeek API key lacks permission to check balance'}
                        else:
                            error_text = await response.text()
                            logger.error(f'[DeepSeek Usage] API error {response.status}: {error_text}')
                            
                            # Fallback: try to verify key with models endpoint
                            models_url = 'https://api.deepseek.com/v1/models'
                            async with session.get(models_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as models_response:
                                if models_response.status == 200:
                                    return {
                                        'provider': 'deepseek',
                                        'provider_display': 'DeepSeek',
                                        'status': 'configured',
                                        'note': 'Balance API returned error, but key is valid',
                                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                                    }
                            return {
                                'provider': 'deepseek',
                                'provider_display': 'DeepSeek',
                                'status': 'error',
                                'error': f'Balance API error ({response.status})',
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling DeepSeek API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking DeepSeek API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    @classmethod
    def get_deepseek_usage_sync(cls) -> Dict[str, Any]:
        """Fetch DeepSeek usage data from the API synchronously.
        
        Returns:
            Dictionary with usage data (same format as async version)
        """
        try:
            api_key = cls._get_app_setting('deepseek_api_key')
            
            if not api_key:
                return {'error': 'DeepSeek API key not configured in settings'}
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json'
            }
            
            now = datetime.now()
            
            try:
                balance_url = 'https://api.deepseek.com/user/balance'
                response = requests.get(balance_url, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    is_available = data.get('is_available', True)
                    balance_infos = data.get('balance_infos', [])
                    
                    total_balance = 0.0
                    currency = 'CNY'
                    granted_balance = 0.0
                    topped_up_balance = 0.0
                    
                    if balance_infos:
                        for info in balance_infos:
                            currency = info.get('currency', 'CNY')
                            try:
                                total_balance += float(info.get('total_balance', 0))
                                granted_balance += float(info.get('granted_balance', 0))
                                topped_up_balance += float(info.get('topped_up_balance', 0))
                            except (ValueError, TypeError):
                                pass
                    
                    return {
                        'provider': 'deepseek',
                        'provider_display': 'DeepSeek',
                        'remaining_credit': total_balance,
                        'granted_balance': granted_balance,
                        'topped_up_balance': topped_up_balance,
                        'currency': currency,
                        'is_available': is_available,
                        'status': 'configured' if is_available else 'insufficient_balance',
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                    }
                elif response.status_code == 401:
                    return {'error': 'Invalid DeepSeek API key'}
                else:
                    return {'error': f'DeepSeek API error ({response.status_code})'}
                    
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling DeepSeek API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking DeepSeek API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # Moonshot (Kimi) Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_moonshot_usage_async(cls) -> Dict[str, Any]:
        """Fetch Moonshot (Kimi) usage data from the API asynchronously.
        
        Moonshot provides a balance API endpoint at /v1/users/me/balance.
        Returns available_balance, voucher_balance, and cash_balance in CNY.
        
        Returns:
            Dictionary with usage status and balance
        """
        try:
            api_key = cls._get_app_setting('moonshot_api_key')
            
            if not api_key:
                return {'error': 'Moonshot API key not configured in settings'}
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json'
            }
            
            now = datetime.now()
            
            try:
                async with aiohttp.ClientSession() as session:
                    # Moonshot balance endpoint: GET https://api.moonshot.ai/v1/users/me/balance (international)
                    balance_url = 'https://api.moonshot.ai/v1/users/me/balance'
                    
                    async with session.get(balance_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        logger.debug(f'[Moonshot Usage] Balance API response status: {response.status}')
                        
                        if response.status == 200:
                            data = await response.json()
                            logger.debug(f'[Moonshot Usage] Balance response: {data}')
                            
                            # Moonshot returns:
                            # {
                            #   "data": {
                            #     "available_balance": 49.58,
                            #     "voucher_balance": 46.58,
                            #     "cash_balance": 3.00
                            #   }
                            # }
                            balance_data = data.get('data', data)
                            
                            available_balance = float(balance_data.get('available_balance', 0))
                            voucher_balance = float(balance_data.get('voucher_balance', 0))
                            cash_balance = float(balance_data.get('cash_balance', 0))
                            
                            return {
                                'provider': 'moonshot',
                                'provider_display': 'Moonshot (Kimi)',
                                'remaining_credit': available_balance,
                                'voucher_balance': voucher_balance,
                                'cash_balance': cash_balance,
                                'currency': 'CNY',
                                'status': 'configured' if available_balance > 0 else 'low_balance',
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        elif response.status == 401:
                            error_text = await response.text()
                            logger.error(f'[Moonshot Usage] Auth error: {error_text}')
                            return {'error': 'Invalid Moonshot API key'}
                        elif response.status == 403:
                            return {'error': 'Moonshot API key lacks permission to check balance'}
                        else:
                            error_text = await response.text()
                            logger.error(f'[Moonshot Usage] API error {response.status}: {error_text}')
                            
                            # Fallback: try to verify key with models endpoint
                            models_url = 'https://api.moonshot.ai/v1/models'
                            async with session.get(models_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as models_response:
                                if models_response.status == 200:
                                    return {
                                        'provider': 'moonshot',
                                        'provider_display': 'Moonshot (Kimi)',
                                        'status': 'configured',
                                        'note': 'Balance API returned error, but key is valid',
                                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                                    }
                            return {
                                'provider': 'moonshot',
                                'provider_display': 'Moonshot (Kimi)',
                                'status': 'error',
                                'error': f'Balance API error ({response.status})',
                                'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling Moonshot API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking Moonshot API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    @classmethod
    def get_moonshot_usage_sync(cls) -> Dict[str, Any]:
        """Fetch Moonshot (Kimi) usage data from the API synchronously.
        
        Returns:
            Dictionary with usage data (same format as async version)
        """
        try:
            api_key = cls._get_app_setting('moonshot_api_key')
            
            if not api_key:
                return {'error': 'Moonshot API key not configured in settings'}
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json'
            }
            
            now = datetime.now()
            
            try:
                balance_url = 'https://api.moonshot.ai/v1/users/me/balance'
                response = requests.get(balance_url, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    data = response.json()
                    balance_data = data.get('data', data)
                    
                    available_balance = float(balance_data.get('available_balance', 0))
                    voucher_balance = float(balance_data.get('voucher_balance', 0))
                    cash_balance = float(balance_data.get('cash_balance', 0))
                    
                    return {
                        'provider': 'moonshot',
                        'provider_display': 'Moonshot (Kimi)',
                        'remaining_credit': available_balance,
                        'voucher_balance': voucher_balance,
                        'cash_balance': cash_balance,
                        'currency': 'CNY',
                        'status': 'configured' if available_balance > 0 else 'low_balance',
                        'last_updated': now.strftime('%Y-%m-%d %H:%M:%S'),
                    }
                elif response.status_code == 401:
                    return {'error': 'Invalid Moonshot API key'}
                else:
                    return {'error': f'Moonshot API error ({response.status_code})'}
                    
            except requests.exceptions.RequestException as e:
                logger.error(f'Network error calling Moonshot API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking Moonshot API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # Google (Gemini) Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_google_usage_async(cls) -> Dict[str, Any]:
        """Fetch Google (Gemini) usage status asynchronously.
        
        Note: Google doesn't provide a direct usage API for Gemini.
        
        Returns:
            Dictionary with usage status
        """
        try:
            api_key = cls._get_app_setting('google_api_key')
            
            if not api_key:
                return {'error': 'Google API key not configured in settings'}
            
            try:
                async with aiohttp.ClientSession() as session:
                    # Try to list models to verify key
                    test_url = f'https://generativelanguage.googleapis.com/v1/models?key={api_key}'
                    
                    async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            return {
                                'provider': 'google',
                                'provider_display': 'Google (Gemini)',
                                'status': 'configured',
                                'note': 'Google usage details available at console.cloud.google.com',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                        elif response.status == 401 or response.status == 403:
                            return {'error': 'Invalid Google API key'}
                        else:
                            return {
                                'provider': 'google',
                                'provider_display': 'Google (Gemini)',
                                'status': 'unknown',
                                'note': 'Could not verify API key status',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling Google API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking Google API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # OpenRouter Usage Functions
    # =========================================================================
    
    @classmethod
    async def get_openrouter_usage_async(cls) -> Dict[str, Any]:
        """Fetch OpenRouter usage data from the API asynchronously.
        
        OpenRouter provides credit balance information.
        
        Returns:
            Dictionary with usage status and credits
        """
        try:
            api_key = cls._get_app_setting('openrouter_api_key')
            
            if not api_key:
                return {'error': 'OpenRouter API key not configured in settings'}
            
            headers = {'Authorization': f'Bearer {api_key}'}
            
            try:
                async with aiohttp.ClientSession() as session:
                    # OpenRouter has a credits endpoint
                    credits_url = 'https://openrouter.ai/api/v1/auth/key'
                    
                    async with session.get(credits_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            # Extract usage/limit info
                            limit = data.get('data', {}).get('limit')
                            usage = data.get('data', {}).get('usage', 0)
                            limit_remaining = data.get('data', {}).get('limit_remaining')
                            
                            result = {
                                'provider': 'openrouter',
                                'provider_display': 'OpenRouter',
                                'status': 'configured',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
                            if usage is not None:
                                result['month_cost'] = usage
                            if limit_remaining is not None:
                                result['remaining_credit'] = limit_remaining
                            elif limit is not None and usage is not None:
                                result['remaining_credit'] = max(0, limit - usage)
                            
                            return result
                        elif response.status == 401:
                            return {'error': 'Invalid OpenRouter API key'}
                        else:
                            return {
                                'provider': 'openrouter',
                                'provider_display': 'OpenRouter',
                                'status': 'unknown',
                                'note': 'Could not verify API key status',
                                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            }
                            
            except aiohttp.ClientError as e:
                logger.error(f'Network error calling OpenRouter API: {e}', exc_info=True)
                return {'error': f'Network error: {str(e)}'}
                
        except Exception as e:
            logger.error(f'Unexpected error checking OpenRouter API: {e}', exc_info=True)
            return {'error': f'Unexpected error: {str(e)}'}
    
    # =========================================================================
    # Aggregate Functions
    # =========================================================================
    
    @classmethod
    async def get_all_providers_usage_async(cls) -> Dict[str, Dict[str, Any]]:
        """Fetch usage data from all configured providers asynchronously.
        
        Returns:
            Dictionary mapping provider names to their usage data
        """
        tasks = {
            'openai': cls.get_openai_usage_async(),
            'nagaai': cls.get_nagaai_usage_async(),
            'anthropic': cls.get_anthropic_usage_async(),
            'google': cls.get_google_usage_async(),
            'openrouter': cls.get_openrouter_usage_async(),
            'xai': cls.get_xai_usage_async(),
            'moonshot': cls.get_moonshot_usage_async(),
            'deepseek': cls.get_deepseek_usage_async(),
        }
        
        results = {}
        
        # Run all tasks concurrently
        task_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        
        for provider, result in zip(tasks.keys(), task_results):
            if isinstance(result, Exception):
                results[provider] = {'error': str(result)}
            else:
                results[provider] = result
        
        return results
    
    @classmethod
    def get_configured_providers(cls) -> List[str]:
        """Get a list of providers that have API keys configured.
        
        Returns:
            List of provider names with configured API keys
        """
        configured = []
        
        provider_key_mapping = {
            'openai': 'openai_api_key',
            'nagaai': 'naga_ai_api_key',
            'anthropic': 'anthropic_api_key',
            'google': 'google_api_key',
            'openrouter': 'openrouter_api_key',
            'xai': 'xai_api_key',
            'moonshot': 'moonshot_api_key',
            'deepseek': 'deepseek_api_key',
        }
        
        for provider, key in provider_key_mapping.items():
            value = cls._get_app_setting(key)
            if value:
                configured.append(provider)
        
        return configured

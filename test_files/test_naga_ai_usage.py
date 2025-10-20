"""Test script to verify Naga AI usage API integration."""

import asyncio
import aiohttp
from datetime import datetime, timedelta
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AppSetting
from sqlmodel import select


async def test_naga_ai_api():
    """Test Naga AI API endpoints."""
    print("=" * 60)
    print("Testing Naga AI Usage API")
    print("=" * 60)
    
    # Get API key from database
    session = get_db()
    try:
        admin_key_setting = session.exec(
            select(AppSetting).where(AppSetting.key == 'naga_ai_admin_api_key')
        ).first()
        
        if not admin_key_setting or not admin_key_setting.value_str:
            print("❌ Error: Naga AI Admin API key not found in database")
            print("   Please add it in Settings > App Settings")
            return
        
        api_key = admin_key_setting.value_str
        print(f"✅ Found Naga AI Admin API key: {api_key[:10]}...")
        
    finally:
        session.close()
    
    print("\n" + "-" * 60)
    print("Testing Balance Endpoint")
    print("-" * 60)
    
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # Test balance endpoint
            balance_url = 'https://api.naga.ac/v1/account/balance'
            print(f"Fetching: {balance_url}")
            
            async with session.get(balance_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                print(f"Status Code: {response.status}")
                
                if response.status == 200:
                    balance_data = await response.json()
                    print(f"✅ Success!")
                    print(f"Response: {balance_data}")
                    
                    balance = float(balance_data.get('balance', 0))
                    print(f"\n💰 Current Balance: ${balance:.2f}")
                else:
                    error_text = await response.text()
                    print(f"❌ Error: {error_text}")
            
            print("\n" + "-" * 60)
            print("Testing Activity Endpoint")
            print("-" * 60)
            
            # Test activity endpoint
            activity_url = 'https://api.naga.ac/v1/account/activity'
            print(f"Fetching: {activity_url}")
            
            async with session.get(activity_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                print(f"Status Code: {response.status}")
                
                if response.status == 200:
                    activity_data = await response.json()
                    print(f"✅ Success!")
                    print(f"\nFull Response:")
                    import json
                    print(json.dumps(activity_data, indent=2))
                    
                    # Check for different possible response structures
                    activities = activity_data.get('activities', [])
                    daily_stats = activity_data.get('daily_stats', [])
                    total_stats = activity_data.get('total_stats', {})
                    
                    print(f"\n📊 Response Structure:")
                    print(f"  activities: {len(activities)} items")
                    print(f"  daily_stats: {len(daily_stats)} items")
                    print(f"  total_stats: {list(total_stats.keys()) if total_stats else 'None'}")
                    
                    if daily_stats:
                        print("\nDaily Stats (last 5):")
                        for i, stat in enumerate(daily_stats[:5]):
                            print(f"\n  Day {i+1}:")
                            for key, value in stat.items():
                                print(f"    {key}: {value}")
                        
                        # Calculate usage from daily_stats
                        now = datetime.now()
                        week_ago = now - timedelta(days=7)
                        month_ago = now - timedelta(days=30)
                        
                        week_cost = 0
                        month_cost = 0
                        
                        for day_stat in daily_stats:
                            date_str = day_stat.get('date')
                            if date_str:
                                try:
                                    day_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                except:
                                    try:
                                        day_date = datetime.strptime(date_str, '%Y-%m-%d')
                                    except:
                                        continue
                                
                                # Try different possible cost field names
                                cost = (day_stat.get('cost', 0) or 
                                       day_stat.get('amount', 0) or
                                       day_stat.get('total_cost', 0) or
                                       day_stat.get('spend', 0))
                                
                                if isinstance(cost, str):
                                    try:
                                        cost = float(cost)
                                    except:
                                        cost = 0
                                
                                if day_date >= week_ago:
                                    week_cost += abs(cost)
                                if day_date >= month_ago:
                                    month_cost += abs(cost)
                        
                        print(f"\n📈 Usage Statistics:")
                        print(f"  Last 7 days:  ${week_cost:.4f}")
                        print(f"  Last 30 days: ${month_cost:.4f}")
                    
                    if total_stats:
                        print(f"\n📊 Total Stats:")
                        for key, value in total_stats.items():
                            print(f"  {key}: {value}")
                    
                    if not daily_stats and not activities:
                        print("\n  No usage data found (you may not have used Naga AI yet)")
                else:
                    error_text = await response.text()
                    print(f"❌ Error: {error_text}")
                    
    except aiohttp.ClientError as e:
        print(f"❌ Network error: {e}")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("Test Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_naga_ai_api())

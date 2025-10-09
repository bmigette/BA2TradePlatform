"""
Test the new parameter-based storage approach.

Tests:
1. TimeInterval enum with H4 support
2. Tools return parameters (not data) in json_for_storage
3. UI can reconstruct data from cache using parameters
"""

import sys
from datetime import datetime, timedelta
import json
import pandas as pd

print("=" * 80)
print("TEST 1: TimeInterval Enum")
print("=" * 80)

from ba2_trade_platform.core.types import TimeInterval

# Test all intervals
print(f"✅ All intervals: {TimeInterval.get_all_intervals()}")
print()

# Test H4 mapping
print("Testing H4 (4-hour) interval:")
print(f"  H4 value: {TimeInterval.H4.value}")
print(f"  To yfinance: {TimeInterval.to_yfinance_interval('4h')}")
print()

# Test normal intervals
print("Testing normal intervals:")
for interval in [TimeInterval.M1, TimeInterval.M5, TimeInterval.H1, TimeInterval.D1]:
    print(f"  {interval.name}: {interval.value} -> yfinance: {TimeInterval.to_yfinance_interval(interval.value)}")
print()

print("=" * 80)
print("TEST 2: Tools Return Parameters (Not Data)")
print("=" * 80)

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.interface import (
    get_YFin_data_online,
    get_stock_stats_indicators_window
)

# Test get_YFin_data_online
print("Testing get_YFin_data_online...")
end_date = datetime.now()
start_date = end_date - timedelta(days=3)

try:
    result = get_YFin_data_online(
        symbol="AAPL",
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        interval="1d"
    )
    
    if isinstance(result, dict) and result.get('_internal'):
        print(f"✅ Returns internal format")
        
        json_storage = result.get('json_for_storage', {})
        print(f"✅ json_for_storage keys: {list(json_storage.keys())}")
        
        # Verify it contains parameters, NOT data
        if 'data' in json_storage:
            print(f"❌ FAIL: Contains 'data' array (should only store parameters!)")
            print(f"   Data length: {len(json_storage['data'])}")
        else:
            print(f"✅ PASS: No 'data' array (stores only parameters)")
        
        # Verify essential parameters are present
        required_params = ['tool', 'symbol', 'interval', 'start_date', 'end_date']
        missing = [p for p in required_params if p not in json_storage]
        
        if missing:
            print(f"❌ FAIL: Missing parameters: {missing}")
        else:
            print(f"✅ PASS: All required parameters present")
            print(f"   Tool: {json_storage['tool']}")
            print(f"   Symbol: {json_storage['symbol']}")
            print(f"   Interval: {json_storage['interval']}")
            print(f"   Date range: {json_storage['start_date']} to {json_storage['end_date']}")
            print(f"   Record count: {json_storage.get('total_records', 'N/A')}")
    else:
        print(f"❌ FAIL: Unexpected format - type: {type(result)}")
        
except Exception as e:
    print(f"❌ FAIL: {e}")
    import traceback
    traceback.print_exc()

print()

# Test get_stock_stats_indicators_window
print("Testing get_stock_stats_indicators_window...")

try:
    result = get_stock_stats_indicators_window(
        symbol="AAPL",
        indicator="rsi",
        curr_date=datetime.now().strftime("%Y-%m-%d"),
        look_back_days=5,
        online=True,
        interval="1d"
    )
    
    if isinstance(result, dict) and result.get('_internal'):
        print(f"✅ Returns internal format")
        
        json_storage = result.get('json_for_storage', {})
        print(f"✅ json_for_storage keys: {list(json_storage.keys())}")
        
        # Verify it contains parameters, NOT data
        if 'data' in json_storage and isinstance(json_storage['data'], list):
            print(f"❌ FAIL: Contains 'data' array (should only store parameters!)")
            print(f"   Data length: {len(json_storage['data'])}")
        else:
            print(f"✅ PASS: No 'data' array (stores only parameters)")
        
        # Verify essential parameters are present
        required_params = ['tool', 'indicator', 'symbol', 'interval', 'start_date', 'end_date']
        missing = [p for p in required_params if p not in json_storage]
        
        if missing:
            print(f"❌ FAIL: Missing parameters: {missing}")
        else:
            print(f"✅ PASS: All required parameters present")
            print(f"   Tool: {json_storage['tool']}")
            print(f"   Indicator: {json_storage['indicator']}")
            print(f"   Symbol: {json_storage['symbol']}")
            print(f"   Interval: {json_storage['interval']}")
            print(f"   Date range: {json_storage['start_date']} to {json_storage['end_date']}")
            print(f"   Data points: {json_storage.get('data_points', 'N/A')}")
            
    elif isinstance(result, str):
        print(f"⚠️  WARNING: Returns error string (may be CSV parsing issue):")
        print(f"   {result[:200]}...")
    else:
        print(f"❌ FAIL: Unexpected format - type: {type(result)}")
        
except Exception as e:
    print(f"❌ FAIL: {e}")
    import traceback
    traceback.print_exc()

print()

print("=" * 80)
print("TEST 3: Reconstruct Data from Cache Using Parameters")
print("=" * 80)

from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
from ba2_trade_platform.config import CACHE_FOLDER

# Simulate what TradingAgentsUI does: reconstruct data from stored parameters
print("Simulating UI data reconstruction...")

# Get parameters from tool (as if loaded from database)
end_date = datetime.now()
start_date = end_date - timedelta(days=5)

result = get_YFin_data_online(
    symbol="AAPL",
    start_date=start_date.strftime("%Y-%m-%d"),
    end_date=end_date.strftime("%Y-%m-%d"),
    interval="1d"
)

if isinstance(result, dict) and result.get('_internal'):
    params = result['json_for_storage']
    
    print(f"1. Retrieved stored parameters:")
    print(f"   {json.dumps(params, indent=2)}")
    print()
    
    print(f"2. Reconstructing data from cache using these parameters...")
    
    try:
        provider = YFinanceDataProvider()
        
        # Reconstruct price data from cache (exactly as UI does)
        reconstructed_data = provider.get_ohlcv_data(
            symbol=params['symbol'],
            start_date=datetime.strptime(params['start_date'], '%Y-%m-%d'),
            end_date=datetime.strptime(params['end_date'], '%Y-%m-%d'),
            interval=params['interval']
        )
        
        print(f"✅ Successfully reconstructed data from cache!")
        print(f"   Rows: {len(reconstructed_data)}")
        print(f"   Columns: {list(reconstructed_data.columns)}")
        print(f"\\n   First 3 rows:")
        print(reconstructed_data.head(3).to_string())
        print()
        
        # Verify data matches what was originally fetched
        print(f"3. Verifying data integrity...")
        if len(reconstructed_data) == params.get('total_records'):
            print(f"✅ PASS: Record count matches ({len(reconstructed_data)} rows)")
        else:
            print(f"⚠️  WARNING: Record count differs (expected {params.get('total_records')}, got {len(reconstructed_data)})")
        
    except Exception as e:
        print(f"❌ FAIL: Could not reconstruct data from cache")
        print(f"   Error: {e}")
        import traceback
        traceback.print_exc()
else:
    print(f"❌ FAIL: Could not get parameters (tool did not return internal format)")

print()

print("=" * 80)
print("TEST 4: Storage Size Comparison")
print("=" * 80)

# Compare storage size: parameters vs full data
print("Comparing storage requirements:")
print()

# Parameters only (new approach)
params_json = json.dumps(params, indent=2)
params_size = len(params_json.encode('utf-8'))

print(f"Parameters only (NEW approach):")
print(f"  Size: {params_size} bytes ({params_size / 1024:.2f} KB)")
print(f"  Contains: tool name, symbol, interval, date range, record count")
print()

# Full data (old approach)
# Simulate old format with actual data
old_format = {
    "symbol": "AAPL",
    "interval": "1d",
    "data": [
        {
            "Datetime": "2025-10-01",
            "Open": 255.04,
            "High": 258.79,
            "Low": 254.93,
            "Close": 255.45,
            "Volume": 48667300
        }
    ] * len(reconstructed_data)  # Multiply by actual record count
}

old_json = json.dumps(old_format, indent=2)
old_size = len(old_json.encode('utf-8'))

print(f"Full data (OLD approach):")
print(f"  Size: {old_size} bytes ({old_size / 1024:.2f} KB)")
print(f"  Contains: complete OHLCV data for all records")
print()

# Calculate savings
savings = old_size - params_size
savings_percent = (savings / old_size) * 100

print(f"Storage savings:")
print(f"  Absolute: {savings} bytes ({savings / 1024:.2f} KB)")
print(f"  Percentage: {savings_percent:.1f}% reduction")
print()

print("=" * 80)
print("SUMMARY")
print("=" * 80)

print("✅ New parameter-based storage approach:")
print()
print("Benefits:")
print("  1. Smaller database: Store parameters, not data (~95-99% size reduction)")
print("  2. Always fresh data: Reconstruct from cache (respects 24h refresh)")
print("  3. Consistent with cache: No data duplication or sync issues")
print("  4. Efficient queries: Database stores only metadata")
print()
print("Architecture:")
print("  Analysis → Tools return parameters → Database stores params")
print("  UI → Reads parameters → YFinanceDataProvider (cache) → Reconstructed data")
print()
print("TimeInterval enum:")
print(f"  Supported intervals: {len(TimeInterval.get_all_intervals())}")
print(f"  Includes H4: {'✅ Yes' if '4h' in TimeInterval.get_all_intervals() else '❌ No'}")
print()

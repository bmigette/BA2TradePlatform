"""
Test the new MarketDataProvider architecture with YFinanceDataProvider.

This script tests:
1. MarketDataPoint dataclass
2. YFinanceDataProvider with smart caching
3. Integration with TradingAgents tools (get_YFin_data_online, get_stock_stats_indicators_window)
"""

import sys
from datetime import datetime, timedelta
from ba2_trade_platform.config import CACHE_FOLDER
from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
from ba2_trade_platform.core.types import MarketDataPoint
import os

print("=" * 80)
print("TEST 1: MarketDataPoint dataclass")
print("=" * 80)

# Test MarketDataPoint creation
datapoint = MarketDataPoint(
    symbol="AAPL",
    timestamp=datetime(2024, 10, 1, 9, 30),
    open=225.50,
    high=228.75,
    low=224.25,
    close=227.00,
    volume=50000000,
    interval="1d"
)

print(f"✅ Created MarketDataPoint: {datapoint}")
print()

print("=" * 80)
print("TEST 2: YFinanceDataProvider - Basic Functionality")
print("=" * 80)

# Initialize data provider
print(f"Cache folder: {CACHE_FOLDER}")
provider = YFinanceDataProvider(CACHE_FOLDER)
print(f"✅ Initialized YFinanceDataProvider")
print()

# Test get_data (returns MarketDataPoint objects)
print("Testing get_data() with 5 days of AAPL data...")
end_date = datetime.now()
start_date = end_date - timedelta(days=5)

try:
    datapoints = provider.get_data(
        symbol="AAPL",
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    print(f"✅ Retrieved {len(datapoints)} MarketDataPoint objects")
    if datapoints:
        print(f"   First point: {datapoints[0]}")
        print(f"   Last point: {datapoints[-1]}")
    print()
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
    print()

print("=" * 80)
print("TEST 3: YFinanceDataProvider - Smart Caching")
print("=" * 80)

# Check if cache file was created
cache_file = os.path.join(CACHE_FOLDER, "AAPL_1d.csv")
if os.path.exists(cache_file):
    print(f"✅ Cache file created: {cache_file}")
    file_size = os.path.getsize(cache_file)
    print(f"   File size: {file_size / 1024:.2f} KB")
    
    # Get file age
    file_mtime = os.path.getmtime(cache_file)
    file_age = datetime.now() - datetime.fromtimestamp(file_mtime)
    print(f"   File age: {file_age.total_seconds():.1f} seconds")
else:
    print(f"❌ Cache file NOT found: {cache_file}")
print()

# Test cache reuse (should be instant)
print("Testing cache reuse (second call should be instant)...")
import time
start_time = time.time()

try:
    datapoints_cached = provider.get_data(
        symbol="AAPL",
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    elapsed = time.time() - start_time
    print(f"✅ Retrieved {len(datapoints_cached)} datapoints in {elapsed:.3f} seconds")
    print(f"   (Cache hit - should be < 0.1 seconds)")
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
print()

print("=" * 80)
print("TEST 4: YFinanceDataProvider - get_dataframe()")
print("=" * 80)

try:
    df = provider.get_dataframe(
        symbol="AAPL",
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    print(f"✅ Retrieved DataFrame with {len(df)} rows")
    print(f"   Columns: {list(df.columns)}")
    print(f"\\n   First 3 rows:")
    print(df.head(3).to_string())
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
print()

print("=" * 80)
print("TEST 5: YFinanceDataProvider - get_current_price()")
print("=" * 80)

try:
    current_price = provider.get_current_price("AAPL")
    
    if current_price:
        print(f"✅ Current AAPL price: ${current_price:.2f}")
    else:
        print(f"❌ Failed to get current price")
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
print()

print("=" * 80)
print("TEST 6: Integration with TradingAgents - get_YFin_data_online()")
print("=" * 80)

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.interface import get_YFin_data_online

try:
    result = get_YFin_data_online(
        symbol="AAPL",
        start_date=(datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
        end_date=datetime.now().strftime("%Y-%m-%d"),
        interval="1d"
    )
    
    if isinstance(result, dict) and result.get('_internal'):
        print(f"✅ get_YFin_data_online returns internal format")
        print(f"   Has text_for_agent: {len(result.get('text_for_agent', ''))} chars")
        print(f"   Has json_for_storage: {bool(result.get('json_for_storage'))}")
        
        json_data = result.get('json_for_storage', {})
        print(f"   Symbol: {json_data.get('symbol')}")
        print(f"   Total records: {json_data.get('total_records')}")
        print(f"   Data records: {len(json_data.get('data', []))}")
    else:
        print(f"❌ Unexpected format: {type(result)}")
        if isinstance(result, str):
            print(f"   Content: {result[:200]}...")
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
print()

print("=" * 80)
print("TEST 7: Integration with TradingAgents - get_stock_stats_indicators_window()")
print("=" * 80)

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.interface import get_stock_stats_indicators_window

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
        print(f"✅ get_stock_stats_indicators_window returns internal format")
        print(f"   Has text_for_agent: {len(result.get('text_for_agent', ''))} chars")
        print(f"   Has json_for_storage: {bool(result.get('json_for_storage'))}")
        
        json_data = result.get('json_for_storage', {})
        print(f"   Indicator: {json_data.get('indicator')}")
        print(f"   Data records: {len(json_data.get('data', []))}")
    elif isinstance(result, str):
        # Might be an error message
        print(f"⚠️  Returned string (possible error or old format):")
        print(f"   {result[:200]}...")
    else:
        print(f"❌ Unexpected format: {type(result)}")
except Exception as e:
    print(f"❌ FAILED: {e}")
    import traceback
    traceback.print_exc()
print()

print("=" * 80)
print("TEST 8: Cache Directory Contents")
print("=" * 80)

if os.path.exists(CACHE_FOLDER):
    cache_files = [f for f in os.listdir(CACHE_FOLDER) if f.endswith('.csv')]
    print(f"✅ Cache folder contains {len(cache_files)} CSV files:")
    for f in cache_files[:10]:  # Show first 10
        file_path = os.path.join(CACHE_FOLDER, f)
        size = os.path.getsize(file_path) / 1024
        print(f"   - {f} ({size:.2f} KB)")
    if len(cache_files) > 10:
        print(f"   ... and {len(cache_files) - 10} more files")
else:
    print(f"❌ Cache folder not found: {CACHE_FOLDER}")
print()

print("=" * 80)
print("SUMMARY")
print("=" * 80)
print("✅ All tests completed!")
print()
print("Architecture benefits:")
print("  - Clean MarketDataPoint dataclass for OHLC data")
print("  - YFinanceDataProvider with smart caching (symbol-based, 24h refresh)")
print("  - TradingAgents tools use data provider (no manual cache management)")
print("  - Cache files: {SYMBOL}_{INTERVAL}.csv format")
print(f"  - Cache location: {CACHE_FOLDER}")
print()

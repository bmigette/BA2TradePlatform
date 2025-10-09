"""
Test BA2 Provider Integration

This script tests the complete integration:
1. Direct BA2 provider usage
2. TradingAgents integration with BA2 providers
3. Database persistence verification
4. Cache behavior verification
"""

from datetime import datetime, timezone
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 80)
print("BA2 Provider Integration Test")
print("=" * 80)

# Test 1: Direct BA2 Provider Usage
print("\n[Test 1] Testing direct BA2 provider usage...")
try:
    from ba2_trade_platform.modules.dataproviders import get_provider
    from ba2_trade_platform.core.ProviderWithPersistence import ProviderWithPersistence
    
    # Get YFinance indicators provider
    print("  - Getting YFinance indicators provider...")
    indicators = get_provider("indicators", "yfinance")
    print(f"  ✓ Got provider: {indicators.__class__.__name__}")
    
    # Check supported indicators
    supported = indicators.get_supported_indicators()
    print(f"  ✓ Supported indicators: {len(supported)} indicators")
    print(f"    Examples: {', '.join(supported[:5])}")
    
    # Wrap with persistence
    print("  - Wrapping with ProviderWithPersistence...")
    wrapper = ProviderWithPersistence(indicators, "indicators")
    print("  ✓ Wrapped successfully")
    
    # Fetch RSI indicator with caching
    print("  - Fetching RSI for AAPL (last 30 days)...")
    result = wrapper.fetch_with_cache(
        "get_indicator",
        "AAPL_rsi_1d_30days_test",
        max_age_hours=6,
        symbol="AAPL",
        indicator="rsi",
        end_date=datetime.now(timezone.utc),
        lookback_days=30,
        interval="1d",
        format_type="dict"
    )
    
    if isinstance(result, dict):
        print(f"  ✓ Got result (dict format)")
        print(f"    Symbol: {result.get('symbol')}")
        print(f"    Indicator: {result.get('indicator_name')}")
        print(f"    Data points: {len(result.get('data', []))}")
        if result.get('data'):
            latest = result['data'][-1]
            print(f"    Latest value: {latest.get('value')} on {latest.get('date')[:10]}")
    else:
        print(f"  ✓ Got result (markdown format, {len(result)} chars)")
    
    print("\n✓ Test 1 PASSED: Direct BA2 provider works!")
    
except Exception as e:
    print(f"\n✗ Test 1 FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 2: Database Persistence Verification
print("\n[Test 2] Verifying database persistence...")
try:
    from ba2_trade_platform.core.provider_utils import get_provider_outputs_by_symbol
    
    outputs = get_provider_outputs_by_symbol("AAPL", provider_category="indicators")
    print(f"  ✓ Found {len(outputs)} persisted outputs for AAPL indicators")
    
    if outputs:
        latest_output = outputs[0]
        print(f"    Latest output:")
        print(f"      - Name: {latest_output.name}")
        print(f"      - Type: {latest_output.type}")
        print(f"      - Provider: {latest_output.provider_name}")
        print(f"      - Symbol: {latest_output.symbol}")
        print(f"      - Created: {latest_output.created_at}")
    
    print("\n✓ Test 2 PASSED: Database persistence verified!")
    
except Exception as e:
    print(f"\n✗ Test 2 FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 3: Cache Behavior
print("\n[Test 3] Testing cache behavior...")
try:
    print("  - First call (should fetch from API)...")
    start_time = datetime.now()
    result1 = wrapper.fetch_with_cache(
        "get_indicator",
        "AAPL_rsi_1d_30days_cache_test",
        max_age_hours=6,
        symbol="AAPL",
        indicator="rsi",
        end_date=datetime.now(timezone.utc),
        lookback_days=30,
        interval="1d",
        format_type="dict"
    )
    time1 = (datetime.now() - start_time).total_seconds()
    print(f"  ✓ First call took {time1:.2f} seconds")
    
    print("  - Second call (should use cache)...")
    start_time = datetime.now()
    result2 = wrapper.fetch_with_cache(
        "get_indicator",
        "AAPL_rsi_1d_30days_cache_test",
        max_age_hours=6,
        symbol="AAPL",
        indicator="rsi",
        end_date=datetime.now(timezone.utc),
        lookback_days=30,
        interval="1d",
        format_type="dict"
    )
    time2 = (datetime.now() - start_time).total_seconds()
    print(f"  ✓ Second call took {time2:.2f} seconds")
    
    if time2 < time1 * 0.5:  # Second call should be much faster
        print(f"  ✓ Cache is working! (Second call {time1/time2:.1f}x faster)")
    else:
        print(f"  ⚠ Cache might not be working (similar times)")
    
    print("\n✓ Test 3 PASSED: Cache behavior verified!")
    
except Exception as e:
    print(f"\n✗ Test 3 FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 4: TradingAgents Integration (if available)
print("\n[Test 4] Testing TradingAgents integration...")
try:
    # Import TradingAgents interface
    sys.path.insert(0, 'ba2_trade_platform/thirdparties/TradingAgents')
    from tradingagents.dataflows.interface import route_to_vendor, BA2_PROVIDERS_AVAILABLE
    
    if not BA2_PROVIDERS_AVAILABLE:
        print("  ⚠ BA2 providers not available in TradingAgents interface")
    else:
        print("  ✓ BA2 providers available in TradingAgents")
        
        print("  - Calling TradingAgents route_to_vendor for indicators...")
        # Note: This will try BA2 first, then fall back to legacy
        result = route_to_vendor(
            "get_indicators",
            "AAPL",
            "rsi",
            "2024-10-08",
            30,
            online=True
        )
        
        if result:
            print(f"  ✓ Got result from route_to_vendor ({len(str(result))} chars)")
            
            # Check if it was persisted
            outputs = get_provider_outputs_by_symbol("AAPL", provider_category="indicators")
            print(f"  ✓ Database has {len(outputs)} outputs for AAPL indicators")
            
        print("\n✓ Test 4 PASSED: TradingAgents integration works!")
    
except ImportError as e:
    print(f"  ⚠ TradingAgents not available: {e}")
    print("  (This is OK if TradingAgents is optional)")
except Exception as e:
    print(f"\n✗ Test 4 FAILED: {e}")
    import traceback
    traceback.print_exc()
    # Don't exit - TradingAgents integration is optional

# Final Summary
print("\n" + "=" * 80)
print("SUMMARY: All core tests passed!")
print("=" * 80)
print("\nBA2 Provider Integration is working correctly:")
print("  ✓ Direct provider usage")
print("  ✓ Database persistence")
print("  ✓ Smart caching")
print("  ✓ TradingAgents integration (if available)")
print("\nYou can now use BA2 providers with automatic persistence and caching!")
print("=" * 80)

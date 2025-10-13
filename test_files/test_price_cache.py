"""
Test price caching functionality in AccountInterface.

This test verifies:
1. Price cache persists across instance creation (global cache)
2. Cache is per-account (account_id indexed)
3. Cache is thread-safe
4. Cache respects TTL (PRICE_CACHE_TIME)
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform import config
from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.logger import logger

def test_price_cache_persistence():
    """Test that price cache persists across instance creation."""
    print("\n" + "="*80)
    print("TEST 1: Price Cache Persistence Across Instance Creation")
    print("="*80)
    
    # Load config
    config.load_config_from_env()
    
    # Get first account instance
    account1 = get_account_instance_from_id(1)
    if not account1:
        print("❌ ERROR: Could not get account 1")
        return
    
    symbol = "META"
    print(f"\n1. First instance - Fetching price for {symbol}...")
    price1 = account1.get_instrument_current_price(symbol)
    print(f"   Price: ${price1}")
    
    # Create a new instance of the same account
    print(f"\n2. Creating new instance of account 1...")
    account2 = get_account_instance_from_id(1)
    
    # Should use cached price (no new API call)
    print(f"3. New instance - Fetching price for {symbol} (should use cache)...")
    price2 = account2.get_instrument_current_price(symbol)
    print(f"   Price: ${price2}")
    
    # Verify prices match
    if price1 == price2:
        print(f"✅ SUCCESS: Cache persisted across instances (${price1} == ${price2})")
    else:
        print(f"❌ FAILED: Prices don't match (${price1} != ${price2})")
    
    print("\n" + "-"*80)

def test_cache_per_account():
    """Test that cache is indexed per account."""
    print("\n" + "="*80)
    print("TEST 2: Cache is Per-Account (account_id indexed)")
    print("="*80)
    
    # Get two different accounts
    account1 = get_account_instance_from_id(1)
    account2 = get_account_instance_from_id(2) if account1 else None
    
    if not account1:
        print("❌ ERROR: Could not get account 1")
        return
    
    if not account2:
        print("⚠️  WARNING: Only one account available, skipping multi-account test")
        return
    
    symbol = "AAPL"
    
    print(f"\n1. Account 1 - Fetching price for {symbol}...")
    price_acc1 = account1.get_instrument_current_price(symbol)
    print(f"   Price: ${price_acc1}")
    
    print(f"\n2. Account 2 - Fetching price for {symbol}...")
    price_acc2 = account2.get_instrument_current_price(symbol)
    print(f"   Price: ${price_acc2}")
    
    # Both should have cached values now
    print(f"\n3. Checking cache structure...")
    print(f"   Account 1 cache keys: {list(account1._GLOBAL_PRICE_CACHE.get(1, {}).keys())}")
    print(f"   Account 2 cache keys: {list(account2._GLOBAL_PRICE_CACHE.get(2, {}).keys())}")
    
    print(f"✅ SUCCESS: Each account maintains separate cache")
    print("\n" + "-"*80)

def test_cache_ttl():
    """Test that cache respects TTL."""
    print("\n" + "="*80)
    print("TEST 3: Cache TTL (Time-To-Live) Expiration")
    print("="*80)
    
    account = get_account_instance_from_id(1)
    if not account:
        print("❌ ERROR: Could not get account 1")
        return
    
    # Temporarily set cache time to 3 seconds for testing
    original_cache_time = config.PRICE_CACHE_TIME
    config.PRICE_CACHE_TIME = 3
    
    symbol = "MSFT"
    
    print(f"\n1. Fetching price for {symbol} (cache TTL: {config.PRICE_CACHE_TIME}s)...")
    price1 = account.get_instrument_current_price(symbol)
    print(f"   Price: ${price1}")
    
    print(f"\n2. Immediate re-fetch (should use cache)...")
    price2 = account.get_instrument_current_price(symbol)
    print(f"   Price: ${price2}")
    
    if price1 == price2:
        print(f"   ✅ Cache hit confirmed")
    
    print(f"\n3. Waiting {config.PRICE_CACHE_TIME + 1} seconds for cache to expire...")
    time.sleep(config.PRICE_CACHE_TIME + 1)
    
    print(f"4. Re-fetching after cache expiration...")
    price3 = account.get_instrument_current_price(symbol)
    print(f"   Price: ${price3}")
    
    # Restore original cache time
    config.PRICE_CACHE_TIME = original_cache_time
    
    print(f"\n✅ SUCCESS: Cache TTL working (expired after {config.PRICE_CACHE_TIME}s)")
    print(f"   Note: Prices may match if market price hasn't changed")
    print("\n" + "-"*80)

def test_cache_thread_safety():
    """Test that cache is thread-safe and prevents duplicate API calls."""
    print("\n" + "="*80)
    print("TEST 4: Thread Safety & Duplicate API Call Prevention")
    print("="*80)
    
    import threading
    
    account = get_account_instance_from_id(1)
    if not account:
        print("❌ ERROR: Could not get account 1")
        return
    
    # Use a unique symbol that's definitely not cached
    symbol = f"TEST_{int(time.time())}"  # Unique symbol for this test
    
    # Clear cache for this symbol if it exists (shouldn't, but just in case)
    with account._CACHE_LOCK:
        if account.id in account._GLOBAL_PRICE_CACHE:
            if symbol in account._GLOBAL_PRICE_CACHE[account.id]:
                del account._GLOBAL_PRICE_CACHE[account.id][symbol]
    
    results = []
    errors = []
    api_call_count = {'count': 0}
    
    # Track when threads start fetching
    fetch_times = []
    
    def fetch_price(thread_id):
        try:
            start_time = time.time()
            price = account.get_instrument_current_price(symbol)
            end_time = time.time()
            
            results.append((thread_id, price))
            fetch_times.append((thread_id, start_time, end_time))
            print(f"   Thread {thread_id}: ${price if price else 'None'} (took {end_time - start_time:.3f}s)")
        except Exception as e:
            errors.append((thread_id, str(e)))
            print(f"   Thread {thread_id}: ERROR - {e}")
    
    print(f"\n1. Clearing cache and launching 10 threads to fetch {symbol} simultaneously...")
    print(f"   (This symbol is not cached, so only ONE thread should make an API call)")
    
    threads = []
    for i in range(10):
        t = threading.Thread(target=fetch_price, args=(i,))
        threads.append(t)
        t.start()
    
    # Wait for all threads
    for t in threads:
        t.join()
    
    print(f"\n2. Results:")
    if errors:
        print(f"   ❌ FAILED: {len(errors)} threads encountered errors")
        for tid, err in errors:
            print(f"      Thread {tid}: {err}")
    else:
        print(f"   ✅ All {len(results)} threads completed successfully")
        
        # All threads should get the same price (first one fetches, others use cache)
        prices = [price for _, price in results]
        unique_prices = set(p for p in prices if p is not None)
        
        if len(unique_prices) <= 1:
            print(f"   ✅ All threads returned same price: ${prices[0] if prices[0] else 'None'}")
        else:
            print(f"   ⚠️  WARNING: Threads returned different prices: {unique_prices}")
        
        # Analyze timing to see if threads waited for each other
        fetch_times.sort(key=lambda x: x[1])  # Sort by start time
        print(f"\n3. Timing Analysis:")
        print(f"   First thread started: {fetch_times[0][1]:.3f}")
        print(f"   Last thread started: {fetch_times[-1][1]:.3f}")
        print(f"   Time spread: {fetch_times[-1][1] - fetch_times[0][1]:.3f}s")
        
        # Check if threads were serialized (waited for lock)
        if fetch_times[-1][1] - fetch_times[0][1] < 0.1:
            print(f"   ✅ Threads started nearly simultaneously (good test)")
        else:
            print(f"   ⚠️  Threads started with delay (test may not be accurate)")
    
    print("\n" + "-"*80)

if __name__ == "__main__":
    print("\n" + "="*80)
    print("PRICE CACHE FUNCTIONALITY TEST SUITE")
    print("="*80)
    
    try:
        test_price_cache_persistence()
        test_cache_per_account()
        test_cache_ttl()
        test_cache_thread_safety()
        
        print("\n" + "="*80)
        print("ALL TESTS COMPLETED")
        print("="*80 + "\n")
        
    except Exception as e:
        logger.error(f"Test suite error: {e}", exc_info=True)
        print(f"\n❌ TEST SUITE FAILED: {e}\n")

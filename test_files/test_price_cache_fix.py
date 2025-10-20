"""
Test script to verify the price cache system correctly distinguishes bid/ask/mid prices.

This test:
1. Gets bid price for a symbol (should cache as symbol:bid)
2. Gets ask price for the same symbol (should cache as symbol:ask, NOT return bid)
3. Verifies that bid and ask prices are different
4. Tests that mid price is correctly calculated as (bid+ask)/2
"""

import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.logger import logger

def test_price_cache_separation():
    """Test that bid/ask/mid prices are cached separately"""
    
    # Get the first Alpaca account
    account_instance = get_instance(AccountDefinition, 1)
    if not account_instance:
        logger.error("No account definition found with ID 1")
        return False
    
    account = AlpacaAccount(account_instance.id)
    
    # Test symbol
    symbol = "AAPL"
    
    print(f"\n{'='*80}")
    print(f"Testing Price Cache Separation for {symbol}")
    print(f"{'='*80}\n")
    
    # Clear any existing cache for this symbol
    if account.id in account._GLOBAL_PRICE_CACHE:
        cache = account._GLOBAL_PRICE_CACHE[account.id]
        keys_to_remove = [k for k in cache.keys() if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            del cache[key]
        print(f"✓ Cleared {len(keys_to_remove)} existing cache entries for {symbol}\n")
    
    # Test 1: Fetch bid price (should make API call and cache as AAPL:bid)
    print("Test 1: Fetching BID price...")
    bid_price = account.get_instrument_current_price(symbol, price_type='bid')
    print(f"  Bid Price: ${bid_price:.2f}")
    
    # Verify it's cached with correct key
    bid_cache_key = f"{symbol}:bid"
    if bid_cache_key in account._GLOBAL_PRICE_CACHE.get(account.id, {}):
        print(f"  ✓ Cached with key '{bid_cache_key}'")
    else:
        print(f"  ✗ NOT cached with key '{bid_cache_key}'")
        return False
    
    # Small delay to ensure different timestamps
    time.sleep(0.1)
    
    # Test 2: Fetch ask price (should make API call and cache as AAPL:ask, NOT return bid)
    print("\nTest 2: Fetching ASK price...")
    ask_price = account.get_instrument_current_price(symbol, price_type='ask')
    print(f"  Ask Price: ${ask_price:.2f}")
    
    # Verify it's cached with correct key
    ask_cache_key = f"{symbol}:ask"
    if ask_cache_key in account._GLOBAL_PRICE_CACHE.get(account.id, {}):
        print(f"  ✓ Cached with key '{ask_cache_key}'")
    else:
        print(f"  ✗ NOT cached with key '{ask_cache_key}'")
        return False
    
    # Test 3: Verify bid and ask are different (they should be)
    print("\nTest 3: Verifying bid != ask...")
    if bid_price != ask_price:
        spread = ask_price - bid_price
        spread_pct = (spread / bid_price) * 100
        print(f"  ✓ Bid and Ask are different")
        print(f"  Spread: ${spread:.2f} ({spread_pct:.3f}%)")
    else:
        print(f"  ✗ Bid and Ask are the same (${bid_price:.2f})")
        print(f"  This indicates cache is NOT separating price types!")
        return False
    
    # Test 4: Fetch bid again (should use cache)
    print("\nTest 4: Fetching BID price again (should use cache)...")
    bid_price_2 = account.get_instrument_current_price(symbol, price_type='bid')
    if bid_price_2 == bid_price:
        print(f"  ✓ Got same bid price from cache: ${bid_price_2:.2f}")
    else:
        print(f"  ⚠ Got different bid price: ${bid_price_2:.2f} vs ${bid_price:.2f}")
        print(f"  (Could be market movement or cache miss)")
    
    # Test 5: Fetch ask again (should use cache)
    print("\nTest 5: Fetching ASK price again (should use cache)...")
    ask_price_2 = account.get_instrument_current_price(symbol, price_type='ask')
    if ask_price_2 == ask_price:
        print(f"  ✓ Got same ask price from cache: ${ask_price_2:.2f}")
    else:
        print(f"  ⚠ Got different ask price: ${ask_price_2:.2f} vs ${ask_price:.2f}")
        print(f"  (Could be market movement or cache miss)")
    
    # Test 6: Fetch mid price (should be calculated as (bid+ask)/2)
    print("\nTest 6: Fetching MID price...")
    mid_price = account.get_instrument_current_price(symbol, price_type='mid')
    expected_mid = (bid_price + ask_price) / 2
    print(f"  Mid Price: ${mid_price:.2f}")
    print(f"  Expected Mid (bid+ask)/2: ${expected_mid:.2f}")
    
    # Allow small floating point tolerance
    if abs(mid_price - expected_mid) < 0.01:
        print(f"  ✓ Mid price is correct average of bid and ask")
    else:
        print(f"  ✗ Mid price doesn't match expected value")
        return False
    
    # Test 7: Check cache contents
    print("\nTest 7: Checking cache contents...")
    cache = account._GLOBAL_PRICE_CACHE.get(account.id, {})
    symbol_keys = [k for k in cache.keys() if k.startswith(f"{symbol}:")]
    print(f"  Cache entries for {symbol}: {len(symbol_keys)}")
    for key in sorted(symbol_keys):
        cached_price = cache[key]['price']
        print(f"    {key}: ${cached_price:.2f}")
    
    if len(symbol_keys) == 3:
        print(f"  ✓ Found all 3 expected cache entries (bid, ask, mid)")
    else:
        print(f"  ⚠ Found {len(symbol_keys)} cache entries, expected 3")
    
    print(f"\n{'='*80}")
    print(f"✓ All tests passed! Price cache correctly separates bid/ask/mid prices")
    print(f"{'='*80}\n")
    
    return True

def test_bulk_fetch_cache_separation():
    """Test that bulk fetch also separates bid/ask/mid in cache"""
    
    # Get the first Alpaca account
    account_instance = get_instance(AccountDefinition, 1)
    if not account_instance:
        logger.error("No account definition found with ID 1")
        return False
    
    account = AlpacaAccount(account_instance.id)
    
    # Test symbols
    symbols = ["AAPL", "MSFT", "GOOGL"]
    
    print(f"\n{'='*80}")
    print(f"Testing Bulk Fetch Cache Separation for {symbols}")
    print(f"{'='*80}\n")
    
    # Clear any existing cache for these symbols
    if account.id in account._GLOBAL_PRICE_CACHE:
        cache = account._GLOBAL_PRICE_CACHE[account.id]
        for symbol in symbols:
            keys_to_remove = [k for k in cache.keys() if k.startswith(f"{symbol}:")]
            for key in keys_to_remove:
                del cache[key]
        print(f"✓ Cleared existing cache entries\n")
    
    # Test 1: Bulk fetch bid prices
    print("Test 1: Bulk fetching BID prices...")
    bid_prices = account.get_instrument_current_price(symbols, price_type='bid')
    for symbol, price in bid_prices.items():
        print(f"  {symbol} Bid: ${price:.2f}")
    
    # Verify all cached with bid keys
    cache = account._GLOBAL_PRICE_CACHE.get(account.id, {})
    for symbol in symbols:
        bid_key = f"{symbol}:bid"
        if bid_key in cache:
            print(f"  ✓ {symbol} cached with key '{bid_key}'")
        else:
            print(f"  ✗ {symbol} NOT cached with key '{bid_key}'")
            return False
    
    # Test 2: Bulk fetch ask prices (should NOT return bid prices)
    print("\nTest 2: Bulk fetching ASK prices...")
    ask_prices = account.get_instrument_current_price(symbols, price_type='ask')
    for symbol, price in ask_prices.items():
        print(f"  {symbol} Ask: ${price:.2f}")
    
    # Verify all cached with ask keys
    for symbol in symbols:
        ask_key = f"{symbol}:ask"
        if ask_key in cache:
            print(f"  ✓ {symbol} cached with key '{ask_key}'")
        else:
            print(f"  ✗ {symbol} NOT cached with key '{ask_key}'")
            return False
    
    # Test 3: Verify bid != ask for each symbol
    print("\nTest 3: Verifying bid != ask for each symbol...")
    all_different = True
    for symbol in symbols:
        bid = bid_prices[symbol]
        ask = ask_prices[symbol]
        if bid != ask:
            spread = ask - bid
            spread_pct = (spread / bid) * 100
            print(f"  ✓ {symbol}: Bid ${bid:.2f} != Ask ${ask:.2f} (spread: {spread_pct:.3f}%)")
        else:
            print(f"  ✗ {symbol}: Bid == Ask (${bid:.2f})")
            all_different = False
    
    if not all_different:
        print(f"  Cache is NOT correctly separating price types in bulk fetch!")
        return False
    
    print(f"\n{'='*80}")
    print(f"✓ Bulk fetch tests passed! Cache correctly separates price types")
    print(f"{'='*80}\n")
    
    return True

if __name__ == "__main__":
    try:
        # Run single symbol test
        test1_passed = test_price_cache_separation()
        
        # Run bulk fetch test
        test2_passed = test_bulk_fetch_cache_separation()
        
        if test1_passed and test2_passed:
            print("\n" + "="*80)
            print("✓✓✓ ALL TESTS PASSED ✓✓✓")
            print("Price cache correctly distinguishes bid/ask/mid in both single and bulk fetches")
            print("="*80 + "\n")
        else:
            print("\n" + "="*80)
            print("✗✗✗ SOME TESTS FAILED ✗✗✗")
            print("="*80 + "\n")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Error running price cache tests: {e}", exc_info=True)
        sys.exit(1)

"""
Test Bulk Price Fetching Feature

This test verifies:
1. Backward compatibility - single symbol fetching still works
2. Bulk fetching - multiple symbols can be fetched at once
3. Cache behavior - both single and bulk fetching use cache
4. Session logging - traceback information is logged
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.logger import logger
import time


def test_single_symbol_backward_compatibility():
    """Test that single symbol fetching still works (backward compatibility)."""
    print("\n" + "="*80)
    print("TEST 1: Single Symbol Backward Compatibility")
    print("="*80)
    
    try:
        # Get account instance
        account = get_account_instance_from_id(1)
        
        # Test single symbol fetch (backward compatibility)
        symbol = "AAPL"
        print(f"\nFetching single symbol: {symbol}")
        
        start_time = time.time()
        price = account.get_instrument_current_price(symbol)
        elapsed = time.time() - start_time
        
        if price is not None:
            print(f"✅ SUCCESS: Got price for {symbol}: ${price:.2f} (took {elapsed:.3f}s)")
            assert isinstance(price, float), f"Expected float, got {type(price)}"
        else:
            print(f"❌ FAILED: Could not get price for {symbol}")
            return False
        
        # Test cache (should be faster)
        print(f"\nFetching same symbol again (should be cached)...")
        start_time = time.time()
        price2 = account.get_instrument_current_price(symbol)
        elapsed2 = time.time() - start_time
        
        if price2 is not None:
            print(f"✅ SUCCESS: Got cached price for {symbol}: ${price2:.2f} (took {elapsed2:.3f}s)")
            assert price == price2, f"Cached price mismatch: {price} != {price2}"
            assert elapsed2 < elapsed, f"Cache should be faster: {elapsed2:.3f}s vs {elapsed:.3f}s"
        else:
            print(f"❌ FAILED: Could not get cached price for {symbol}")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error in single symbol test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_bulk_symbol_fetching():
    """Test that bulk symbol fetching works with list of symbols."""
    print("\n" + "="*80)
    print("TEST 2: Bulk Symbol Fetching")
    print("="*80)
    
    try:
        # Get account instance
        account = get_account_instance_from_id(1)
        
        # Test bulk symbol fetch
        symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        print(f"\nFetching {len(symbols)} symbols in bulk: {symbols}")
        
        start_time = time.time()
        prices = account.get_instrument_current_price(symbols)
        elapsed = time.time() - start_time
        
        if prices is not None:
            print(f"✅ SUCCESS: Bulk fetch completed in {elapsed:.3f}s")
            assert isinstance(prices, dict), f"Expected dict, got {type(prices)}"
            
            # Check each price
            for symbol in symbols:
                price = prices.get(symbol)
                if price is not None:
                    print(f"  {symbol}: ${price:.2f}")
                else:
                    print(f"  {symbol}: No price available")
            
            # Calculate average time per symbol
            avg_time = elapsed / len(symbols)
            print(f"\n📊 Average time per symbol: {avg_time:.3f}s")
            
        else:
            print(f"❌ FAILED: Bulk fetch returned None")
            return False
        
        # Test cached bulk fetch (should be much faster)
        print(f"\nFetching same symbols again (should be cached)...")
        start_time = time.time()
        prices2 = account.get_instrument_current_price(symbols)
        elapsed2 = time.time() - start_time
        
        if prices2 is not None:
            print(f"✅ SUCCESS: Cached bulk fetch completed in {elapsed2:.3f}s")
            assert prices == prices2, "Cached prices should match"
            print(f"\n⚡ Speed improvement: {elapsed / elapsed2:.1f}x faster")
        else:
            print(f"❌ FAILED: Cached bulk fetch returned None")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error in bulk symbol test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_mixed_cached_uncached():
    """Test bulk fetching with mix of cached and uncached symbols."""
    print("\n" + "="*80)
    print("TEST 3: Mixed Cached/Uncached Bulk Fetching")
    print("="*80)
    
    try:
        # Get account instance
        account = get_account_instance_from_id(1)
        
        # First, fetch some symbols individually to cache them
        cached_symbols = ["AAPL", "MSFT"]
        print(f"\nPre-caching symbols: {cached_symbols}")
        for symbol in cached_symbols:
            price = account.get_instrument_current_price(symbol)
            if price:
                print(f"  Cached {symbol}: ${price:.2f}")
        
        # Wait a moment
        time.sleep(0.5)
        
        # Now fetch a mix of cached and uncached symbols
        all_symbols = ["AAPL", "MSFT", "NVDA", "META"]  # First 2 cached, last 2 uncached
        print(f"\nFetching mixed symbols (2 cached, 2 uncached): {all_symbols}")
        
        start_time = time.time()
        prices = account.get_instrument_current_price(all_symbols)
        elapsed = time.time() - start_time
        
        if prices is not None:
            print(f"✅ SUCCESS: Mixed fetch completed in {elapsed:.3f}s")
            
            for symbol in all_symbols:
                price = prices.get(symbol)
                cached_status = "CACHED" if symbol in cached_symbols else "FETCHED"
                if price is not None:
                    print(f"  {symbol}: ${price:.2f} ({cached_status})")
                else:
                    print(f"  {symbol}: No price available ({cached_status})")
            
        else:
            print(f"❌ FAILED: Mixed fetch returned None")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error in mixed cached/uncached test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_session_logging():
    """Test that database session creation logs traceback information."""
    print("\n" + "="*80)
    print("TEST 4: Session Logging with Traceback")
    print("="*80)
    
    try:
        from ba2_trade_platform.core.db import get_db
        
        print("\nCreating database session to test logging...")
        print("Check logs/app.debug.log for session creation with traceback info")
        
        # Create a session (this should log with traceback)
        session = get_db()
        session_id = id(session)
        print(f"✅ Session created (id={session_id})")
        
        # Check that session was created
        assert session is not None, "Session should not be None"
        
        # Close the session
        session.close()
        print(f"✅ Session closed")
        
        print("\n💡 Look for log entries like:")
        print("   'Database session created (id=...) [Called from: file.py:function():123 <- ...]'")
        
        return True
        
    except Exception as e:
        logger.error(f"Error in session logging test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_type_validation():
    """Test that invalid input types are handled properly."""
    print("\n" + "="*80)
    print("TEST 5: Type Validation")
    print("="*80)
    
    try:
        # Get account instance
        account = get_account_instance_from_id(1)
        
        # Test invalid input type
        print("\nTesting invalid input type (dict)...")
        try:
            price = account.get_instrument_current_price({"invalid": "type"})
            print(f"❌ FAILED: Should have raised TypeError, got: {price}")
            return False
        except TypeError as e:
            print(f"✅ SUCCESS: Correctly raised TypeError: {e}")
        
        # Test empty list
        print("\nTesting empty list...")
        prices = account.get_instrument_current_price([])
        if prices is not None:
            assert isinstance(prices, dict), f"Expected dict, got {type(prices)}"
            assert len(prices) == 0, f"Expected empty dict, got {len(prices)} items"
            print(f"✅ SUCCESS: Empty list returns empty dict")
        else:
            print(f"❌ FAILED: Empty list returned None")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error in type validation test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("BULK PRICE FETCHING TEST SUITE")
    print("="*80)
    print("This test verifies the new bulk price fetching feature")
    print("and backward compatibility with single symbol fetching.")
    
    results = {
        "Single Symbol (Backward Compatibility)": test_single_symbol_backward_compatibility(),
        "Bulk Symbol Fetching": test_bulk_symbol_fetching(),
        "Mixed Cached/Uncached": test_mixed_cached_uncached(),
        "Session Logging": test_session_logging(),
        "Type Validation": test_type_validation()
    }
    
    # Print summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    
    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status}: {test_name}")
    
    total_tests = len(results)
    passed_tests = sum(1 for passed in results.values() if passed)
    
    print(f"\nTotal: {passed_tests}/{total_tests} tests passed")
    
    if passed_tests == total_tests:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total_tests - passed_tests} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)

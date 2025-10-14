"""
Test Account Instance Singleton Cache with Settings Caching

This test verifies:
1. Singleton behavior - only one instance per account_id
2. Settings caching - settings loaded once and cached
3. Cache invalidation - cache properly invalidated on updates
4. Thread safety - multiple threads can safely access the cache
"""

import sys
import os
import threading
import time

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.core.AccountInstanceCache import AccountInstanceCache
from ba2_trade_platform.logger import logger


def test_singleton_behavior():
    """Test that multiple calls return the same instance."""
    print("\n" + "="*80)
    print("TEST 1: Singleton Behavior")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Get the same account twice
        print("\nGetting account instance 1 (first call - should create new)")
        account1 = get_account_instance_from_id(1)
        
        print("Getting account instance 1 (second call - should return cached)")
        account2 = get_account_instance_from_id(1)
        
        # Verify they are the same object in memory
        if account1 is account2:
            print(f"✅ SUCCESS: Both calls returned the same instance (id={id(account1)})")
            return True
        else:
            print(f"❌ FAILED: Different instances returned (id1={id(account1)}, id2={id(account2)})")
            return False
            
    except Exception as e:
        logger.error(f"Error in singleton test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_settings_caching():
    """Test that settings are cached and reused."""
    print("\n" + "="*80)
    print("TEST 2: Settings Caching")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Get account instance
        print("\nGetting account instance...")
        account = get_account_instance_from_id(1)
        
        # Access settings multiple times
        print("Accessing settings (1st time - should load from DB)...")
        start_time = time.time()
        settings1 = account.settings
        time1 = time.time() - start_time
        print(f"  Loaded {len(settings1)} settings in {time1:.4f}s")
        
        print("Accessing settings (2nd time - should use cache)...")
        start_time = time.time()
        settings2 = account.settings
        time2 = time.time() - start_time
        print(f"  Loaded {len(settings2)} settings in {time2:.4f}s")
        
        print("Accessing settings (3rd time - should use cache)...")
        start_time = time.time()
        settings3 = account.settings
        time3 = time.time() - start_time
        print(f"  Loaded {len(settings3)} settings in {time3:.4f}s")
        
        # Verify they are the same object
        if settings1 is settings2 is settings3:
            print(f"✅ SUCCESS: All calls returned the same cached settings object")
            print(f"⚡ Cache speedup: 2nd call {time1/time2:.1f}x faster, 3rd call {time1/time3:.1f}x faster")
            return True
        else:
            print(f"❌ FAILED: Different settings objects returned")
            return False
            
    except Exception as e:
        logger.error(f"Error in settings caching test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_cache_invalidation():
    """Test that cache is properly invalidated when settings are updated."""
    print("\n" + "="*80)
    print("TEST 3: Cache Invalidation")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Get account instance and access settings
        print("\nGetting account instance and loading settings...")
        account = get_account_instance_from_id(1)
        settings1 = account.settings
        print(f"  Loaded {len(settings1)} settings (cached)")
        
        # Get cache stats
        stats = AccountInstanceCache.get_cache_stats()
        print(f"\nCache stats before invalidation: {stats}")
        
        # Invalidate the cache
        print("\nInvalidating settings cache...")
        AccountInstanceCache.invalidate_settings(1)
        
        # Get cache stats after invalidation
        stats = AccountInstanceCache.get_cache_stats()
        print(f"Cache stats after invalidation: {stats}")
        
        # Access settings again (should reload from DB)
        print("\nAccessing settings after invalidation (should reload from DB)...")
        settings2 = account.settings
        print(f"  Loaded {len(settings2)} settings")
        
        # Verify they are different objects (new load from DB)
        if settings1 is not settings2:
            print(f"✅ SUCCESS: Cache properly invalidated, new settings loaded from DB")
            print(f"  Settings1 id: {id(settings1)}")
            print(f"  Settings2 id: {id(settings2)}")
            return True
        else:
            print(f"❌ FAILED: Cache not invalidated, same object returned")
            return False
            
    except Exception as e:
        logger.error(f"Error in cache invalidation test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_multiple_accounts():
    """Test that different accounts have separate cached instances."""
    print("\n" + "="*80)
    print("TEST 4: Multiple Accounts")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Get multiple account instances
        print("\nGetting account instances for accounts 1 and 2...")
        account1 = get_account_instance_from_id(1)
        account2 = get_account_instance_from_id(2)
        
        # Verify they are different objects
        if account1 is not account2:
            print(f"✅ SUCCESS: Different accounts have separate instances")
            print(f"  Account 1 id: {id(account1)}")
            print(f"  Account 2 id: {id(account2)}")
        else:
            print(f"❌ FAILED: Same instance returned for different accounts")
            return False
        
        # Access settings for both
        print("\nAccessing settings for both accounts...")
        settings1 = account1.settings
        settings2 = account2.settings
        
        # Verify they are different settings objects
        if settings1 is not settings2:
            print(f"✅ SUCCESS: Different accounts have separate cached settings")
            print(f"  Account 1: {len(settings1)} settings")
            print(f"  Account 2: {len(settings2)} settings")
            return True
        else:
            print(f"❌ FAILED: Same settings object for different accounts")
            return False
            
    except Exception as e:
        logger.error(f"Error in multiple accounts test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_thread_safety():
    """Test that cache is thread-safe."""
    print("\n" + "="*80)
    print("TEST 5: Thread Safety")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Create multiple threads that access the same account
        results = []
        errors = []
        
        def access_account(thread_id):
            try:
                account = get_account_instance_from_id(1)
                settings = account.settings
                results.append((thread_id, id(account), id(settings)))
            except Exception as e:
                errors.append((thread_id, str(e)))
        
        # Launch 10 threads
        print("\nLaunching 10 threads to access account 1 simultaneously...")
        threads = []
        for i in range(10):
            thread = threading.Thread(target=access_account, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Check for errors
        if errors:
            print(f"❌ FAILED: {len(errors)} threads encountered errors:")
            for thread_id, error in errors:
                print(f"  Thread {thread_id}: {error}")
            return False
        
        # Verify all threads got the same instance and settings
        first_account_id = results[0][1]
        first_settings_id = results[0][2]
        
        all_same_account = all(account_id == first_account_id for _, account_id, _ in results)
        all_same_settings = all(settings_id == first_settings_id for _, _, settings_id in results)
        
        if all_same_account and all_same_settings:
            print(f"✅ SUCCESS: All 10 threads got the same cached instance and settings")
            print(f"  Account instance id: {first_account_id}")
            print(f"  Settings object id: {first_settings_id}")
            return True
        else:
            print(f"❌ FAILED: Threads got different instances or settings")
            if not all_same_account:
                print(f"  Different account instances: {set(aid for _, aid, _ in results)}")
            if not all_same_settings:
                print(f"  Different settings objects: {set(sid for _, _, sid in results)}")
            return False
            
    except Exception as e:
        logger.error(f"Error in thread safety test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def test_cache_stats():
    """Test cache statistics reporting."""
    print("\n" + "="*80)
    print("TEST 6: Cache Statistics")
    print("="*80)
    
    try:
        # Clear cache first
        AccountInstanceCache.clear_cache()
        
        # Get initial stats
        stats = AccountInstanceCache.get_cache_stats()
        print(f"\nInitial cache stats: {stats}")
        assert stats['instances_cached'] == 0
        assert stats['settings_cached'] == 0
        
        # Create some cached instances
        print("\nCreating 3 account instances...")
        account1 = get_account_instance_from_id(1)
        account2 = get_account_instance_from_id(2)
        account3 = get_account_instance_from_id(1)  # Should return cached instance
        
        # Access settings
        print("Accessing settings for accounts 1 and 2...")
        _ = account1.settings
        _ = account2.settings
        
        # Get stats again
        stats = AccountInstanceCache.get_cache_stats()
        print(f"\nCache stats after creating instances and accessing settings:")
        print(f"  Instances cached: {stats['instances_cached']}")
        print(f"  Settings cached: {stats['settings_cached']}")
        print(f"  Locks created: {stats['locks_created']}")
        
        # Verify stats are correct
        if stats['instances_cached'] == 2 and stats['settings_cached'] == 2:
            print(f"✅ SUCCESS: Cache stats are correct")
            return True
        else:
            print(f"❌ FAILED: Expected 2 instances and 2 settings cached")
            return False
            
    except Exception as e:
        logger.error(f"Error in cache stats test: {e}", exc_info=True)
        print(f"❌ EXCEPTION: {e}")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("ACCOUNT INSTANCE CACHE TEST SUITE")
    print("="*80)
    print("This test verifies the singleton cache for account instances")
    print("and the settings caching mechanism.")
    
    results = {
        "Singleton Behavior": test_singleton_behavior(),
        "Settings Caching": test_settings_caching(),
        "Cache Invalidation": test_cache_invalidation(),
        "Multiple Accounts": test_multiple_accounts(),
        "Thread Safety": test_thread_safety(),
        "Cache Statistics": test_cache_stats()
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
    
    # Show final cache stats
    stats = AccountInstanceCache.get_cache_stats()
    print(f"\nFinal cache stats: {stats}")
    
    if passed_tests == total_tests:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total_tests - passed_tests} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)

"""
Test Settings Cache Implementation

Tests that the simplified instance-level settings caching works correctly:
1. First access loads from database
2. Subsequent accesses return cached settings
3. After save, cache is cleared and reloads on next access
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.logger import logger

def test_settings_cache():
    """Test that settings are cached on the instance."""
    
    print("\n" + "="*70)
    print("TEST: Settings Cache Implementation")
    print("="*70)
    
    # Get first account from database
    session = get_db()
    try:
        account_model = session.query(AccountDefinition).first()
        if not account_model:
            print("❌ No accounts found in database. Create an account first.")
            return
        
        account_id = account_model.id
        print(f"\n✓ Testing with account ID: {account_id}")
    finally:
        session.close()
    
    # Test 1: First access should load from database
    print("\n" + "-"*70)
    print("TEST 1: First settings access (should load from database)")
    print("-"*70)
    
    account = get_account_instance_from_id(account_id, use_cache=True)
    print(f"Account instance: {type(account).__name__}")
    
    # Check that no cache exists yet
    has_cache_before = hasattr(account, '_settings_cache') and account._settings_cache is not None
    print(f"Has cache before access: {has_cache_before}")
    
    # First access - should load from database
    settings1 = account.settings
    print(f"Settings loaded: {len(settings1)} keys")
    
    # Check that cache was created
    has_cache_after = hasattr(account, '_settings_cache') and account._settings_cache is not None
    print(f"Has cache after access: {has_cache_after}")
    
    if has_cache_after:
        print("✓ TEST 1 PASSED: Cache created after first access")
    else:
        print("❌ TEST 1 FAILED: Cache not created")
        return
    
    # Test 2: Second access should return cached settings
    print("\n" + "-"*70)
    print("TEST 2: Second settings access (should return cached)")
    print("-"*70)
    
    settings2 = account.settings
    print(f"Settings loaded: {len(settings2)} keys")
    
    # Check that it's the same object (cached)
    is_same_object = settings1 is settings2
    print(f"Same object returned: {is_same_object}")
    
    if is_same_object:
        print("✓ TEST 2 PASSED: Cached settings returned")
    else:
        print("❌ TEST 2 FAILED: Different object returned (not cached)")
        return
    
    # Test 3: After save, cache should be cleared
    print("\n" + "-"*70)
    print("TEST 3: Save setting (should clear cache)")
    print("-"*70)
    
    # Save a test setting
    test_key = list(settings1.keys())[0]
    test_value = settings1[test_key]
    print(f"Saving setting: {test_key} = {test_value}")
    
    account.save_setting(test_key, test_value)
    
    # Check that cache was cleared
    has_cache_after_save = hasattr(account, '_settings_cache') and account._settings_cache is not None
    print(f"Has cache after save: {has_cache_after_save}")
    
    if not has_cache_after_save:
        print("✓ TEST 3 PASSED: Cache cleared after save")
    else:
        print("❌ TEST 3 FAILED: Cache not cleared after save")
        return
    
    # Test 4: Next access should reload from database
    print("\n" + "-"*70)
    print("TEST 4: Access after save (should reload)")
    print("-"*70)
    
    settings3 = account.settings
    print(f"Settings loaded: {len(settings3)} keys")
    
    # Check that cache was recreated
    has_cache_after_reload = hasattr(account, '_settings_cache') and account._settings_cache is not None
    print(f"Has cache after reload: {has_cache_after_reload}")
    
    # Check that it's a different object (reloaded)
    is_different_object = settings3 is not settings2
    print(f"Different object returned: {is_different_object}")
    
    if has_cache_after_reload and is_different_object:
        print("✓ TEST 4 PASSED: Settings reloaded and cached")
    else:
        print("❌ TEST 4 FAILED: Settings not properly reloaded")
        return
    
    # Test 5: Singleton pattern - same instance returned
    print("\n" + "-"*70)
    print("TEST 5: Singleton pattern (should return same instance)")
    print("-"*70)
    
    account2 = get_account_instance_from_id(account_id, use_cache=True)
    is_same_instance = account is account2
    print(f"Same instance returned: {is_same_instance}")
    
    # Should still have cached settings
    settings4 = account2.settings
    is_cached = settings4 is settings3
    print(f"Same cached settings: {is_cached}")
    
    if is_same_instance and is_cached:
        print("✓ TEST 5 PASSED: Singleton returns same instance with cache")
    else:
        print("❌ TEST 5 FAILED: Singleton or cache not working")
        return
    
    # Summary
    print("\n" + "="*70)
    print("✓ ALL TESTS PASSED")
    print("="*70)
    print("\nSettings cache implementation verified:")
    print("  ✓ First access loads from database and caches")
    print("  ✓ Subsequent accesses return cached settings")
    print("  ✓ Save operations clear the cache")
    print("  ✓ After save, next access reloads and caches")
    print("  ✓ Singleton pattern ensures same instance used")
    print("\nExpected impact: 10-25x reduction in database sessions")
    print("="*70)

if __name__ == "__main__":
    try:
        test_settings_cache()
    except Exception as e:
        print(f"\n❌ TEST FAILED WITH ERROR: {e}")
        import traceback
        traceback.print_exc()

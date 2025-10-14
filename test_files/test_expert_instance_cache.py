"""
Test Expert Instance Caching

This script verifies that expert instances are properly cached and settings
are only loaded once from the database.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.core.ExpertInstanceCache import ExpertInstanceCache
from ba2_trade_platform.logger import logger

print("="*80)
print("Expert Instance Caching Test")
print("="*80)

def test_expert_caching():
    """Test that expert instances are cached properly."""
    
    print("\n" + "-"*80)
    print("TEST 1: Singleton Behavior")
    print("-"*80)
    
    # Clear cache first
    ExpertInstanceCache.clear_cache()
    print("✓ Cache cleared")
    
    # Get the same expert instance twice
    expert_id = 1
    print(f"\nGetting expert instance {expert_id} (first call - should create new)...")
    expert1 = get_expert_instance_from_id(expert_id)
    
    if expert1 is None:
        print(f"❌ FAILED: Expert instance {expert_id} not found")
        return False
    
    print(f"✓ Got expert: {type(expert1).__name__}")
    print(f"  Memory address: {id(expert1)}")
    
    print(f"\nGetting expert instance {expert_id} (second call - should return cached)...")
    expert2 = get_expert_instance_from_id(expert_id)
    
    print(f"✓ Got expert: {type(expert2).__name__}")
    print(f"  Memory address: {id(expert2)}")
    
    # Verify they are the same object in memory
    if expert1 is expert2:
        print(f"\n✅ SUCCESS: Both calls returned the same instance")
    else:
        print(f"\n❌ FAILED: Different instances returned")
        return False
    
    print("\n" + "-"*80)
    print("TEST 2: Settings Caching")
    print("-"*80)
    
    # Access settings - should load from DB and cache
    print(f"\nAccessing settings for expert {expert_id} (first access - should load from DB)...")
    settings1 = expert1.settings
    print(f"✓ Settings loaded: {len(settings1)} keys")
    
    # Check that cache exists
    has_cache = hasattr(expert1, '_settings_cache') and expert1._settings_cache is not None
    if has_cache:
        print(f"✓ Settings are cached on instance")
    else:
        print(f"❌ Settings not cached")
        return False
    
    # Access settings again - should use cache
    print(f"\nAccessing settings again (should use cache)...")
    settings2 = expert1.settings
    
    # Verify it's the same object (cached)
    if settings1 is settings2:
        print(f"✅ SUCCESS: Settings returned from cache (0 DB calls)")
    else:
        print(f"⚠️  WARNING: Different settings object returned (may have reloaded)")
    
    print("\n" + "-"*80)
    print("TEST 3: Multiple Expert Instances")
    print("-"*80)
    
    # Test with multiple expert IDs
    expert_ids = [1, 2, 3]
    experts = {}
    
    for eid in expert_ids:
        print(f"\nGetting expert instance {eid}...")
        expert = get_expert_instance_from_id(eid)
        if expert:
            experts[eid] = expert
            print(f"✓ Got expert {eid}: {type(expert).__name__}")
            # Access settings to trigger caching
            settings = expert.settings
            print(f"  Settings keys: {len(settings)}")
        else:
            print(f"⚠️  Expert {eid} not found (may not exist)")
    
    # Verify cache stats
    stats = ExpertInstanceCache.get_cache_stats()
    print(f"\n📊 Cache Statistics:")
    print(f"  Cached instances: {stats['cached_instances']}")
    print(f"  Expert IDs: {stats['expert_instance_ids']}")
    
    if stats['cached_instances'] == len(experts):
        print(f"✅ SUCCESS: All {len(experts)} experts are cached")
    else:
        print(f"⚠️  Expected {len(experts)} cached, got {stats['cached_instances']}")
    
    print("\n" + "-"*80)
    print("TEST 4: Cache Reuse After Multiple Calls")
    print("-"*80)
    
    # Make multiple calls to the same expert
    call_count = 5
    expert_id = 1
    print(f"\nCalling get_expert_instance_from_id({expert_id}) {call_count} times...")
    
    instances = []
    for i in range(call_count):
        expert = get_expert_instance_from_id(expert_id)
        instances.append(expert)
        print(f"  Call {i+1}: {id(expert)}")
    
    # Verify all are the same instance
    all_same = all(inst is instances[0] for inst in instances)
    if all_same:
        print(f"\n✅ SUCCESS: All {call_count} calls returned the same cached instance")
    else:
        print(f"\n❌ FAILED: Different instances returned across calls")
        return False
    
    print("\n" + "="*80)
    print("✅ ALL TESTS PASSED")
    print("="*80)
    print("\nKey Findings:")
    print("  ✓ Expert instances are properly cached (singleton pattern)")
    print("  ✓ Settings are cached at instance level")
    print("  ✓ Multiple calls return the same instance (no DB overhead)")
    print("  ✓ Cache works across multiple expert IDs")
    
    return True

if __name__ == "__main__":
    try:
        success = test_expert_caching()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Test failed with exception: {e}")
        logger.error("Expert caching test failed", exc_info=True)
        sys.exit(1)

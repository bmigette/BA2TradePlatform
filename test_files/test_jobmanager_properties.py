#!/usr/bin/env python3
"""
Test JobManager Expert Properties Integration

This script tests that JobManager correctly reads expert properties
instead of settings for should_expand_instrument_jobs.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.modules.experts.FMPSenateTraderCopy import FMPSenateTraderCopy
from ba2_trade_platform.modules.experts.FMPSenateTraderWeight import FMPSenateTraderWeight

def test_jobmanager_logic():
    """Test the logic that JobManager uses to check expert properties."""
    
    print("=== Testing JobManager Expert Properties Logic ===\n")
    
    experts = [
        ("FMPSenateTraderCopy", FMPSenateTraderCopy),
        ("FMPSenateTraderWeight", FMPSenateTraderWeight)
    ]
    
    for expert_name, expert_class in experts:
        print(f"Testing {expert_name}:")
        
        try:
            # Get expert properties (this is what JobManager now does)
            expert_properties = expert_class.get_expert_properties()
            can_select_instruments = expert_properties.get('can_select_instruments', False)
            can_recommend_instruments = expert_properties.get('can_recommend_instruments', False)
            
            # JobManager now checks for can_recommend_instruments
            can_select = can_recommend_instruments
            
            print(f"   Expert properties: {expert_properties}")
            print(f"   can_select_instruments: {can_select_instruments}")
            print(f"   can_recommend_instruments: {can_recommend_instruments}")
            print(f"   Combined capability check: {can_select}")
            
            if can_select:
                should_expand = expert_properties.get('should_expand_instrument_jobs', True)
                print(f"   should_expand_instrument_jobs: {should_expand}")
                
                if not should_expand:
                    print(f"   ✅ JobManager would skip instrument job expansion for {expert_name}")
                else:
                    print(f"   ℹ️  JobManager would expand instrument jobs for {expert_name}")
            else:
                print(f"   ℹ️  JobManager would proceed normally (expert can't select instruments)")
                
        except AttributeError:
            print(f"   ℹ️  {expert_name} has no get_expert_properties method (uses defaults)")
        except Exception as e:
            print(f"   ❌ Error testing {expert_name}: {e}")
        
        print()

def test_property_names_consistency():
    """Test that we're using consistent property names."""
    
    print("=== Testing Property Name Consistency ===\n")
    
    # JobManager currently checks for 'can_select_instruments' but our new expert uses 'can_recommend_instruments'
    # We should make sure these are aligned
    
    copy_properties = FMPSenateTraderCopy.get_expert_properties()
    
    has_can_select = 'can_select_instruments' in copy_properties
    has_can_recommend = 'can_recommend_instruments' in copy_properties
    
    print(f"FMPSenateTraderCopy properties: {copy_properties}")
    print(f"Has 'can_select_instruments': {has_can_select}")
    print(f"Has 'can_recommend_instruments': {has_can_recommend}")
    
    if has_can_recommend and not has_can_select:
        print("✅ Expert uses 'can_recommend_instruments' - matches updated JobManager")
    elif has_can_select and has_can_recommend:
        print("⚠️  Expert has both property names - should standardize on 'can_recommend_instruments'")
    elif has_can_select:
        print("⚠️  Expert uses deprecated 'can_select_instruments' - should use 'can_recommend_instruments'")

if __name__ == "__main__":
    test_jobmanager_logic()
    print()
    test_property_names_consistency()
#!/usr/bin/env python3
"""
Test Expert Properties

This script tests the expert properties system to ensure:
1. FMPSenateTraderCopy has can_recommend_instruments=True and should_expand_instrument_jobs=False
2. FMPSenateTraderWeight uses defaults (no properties defined)
3. JobManager checks expert properties correctly
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.modules.experts.FMPSenateTraderCopy import FMPSenateTraderCopy
from ba2_trade_platform.modules.experts.FMPSenateTraderWeight import FMPSenateTraderWeight

def test_expert_properties():
    """Test expert properties for both senate trader experts."""
    
    print("=== Testing Expert Properties ===\n")
    
    # Test FMPSenateTraderCopy
    print("1. FMPSenateTraderCopy Properties:")
    try:
        copy_properties = FMPSenateTraderCopy.get_expert_properties()
        print(f"   Properties: {copy_properties}")
        
        can_recommend = copy_properties.get('can_recommend_instruments', False)
        should_expand = copy_properties.get('should_expand_instrument_jobs', True)
        
        print(f"   can_recommend_instruments: {can_recommend}")
        print(f"   should_expand_instrument_jobs: {should_expand}")
        
        # Expected values
        if can_recommend and not should_expand:
            print("   ✅ FMPSenateTraderCopy properties are correct!")
        else:
            print("   ❌ FMPSenateTraderCopy properties are incorrect!")
            print(f"      Expected: can_recommend_instruments=True, should_expand_instrument_jobs=False")
            print(f"      Got: can_recommend_instruments={can_recommend}, should_expand_instrument_jobs={should_expand}")
            
    except Exception as e:
        print(f"   ❌ Error getting FMPSenateTraderCopy properties: {e}")
    
    print()
    
    # Test FMPSenateTraderWeight
    print("2. FMPSenateTraderWeight Properties:")
    try:
        weight_properties = FMPSenateTraderWeight.get_expert_properties()
        print(f"   Properties: {weight_properties}")
        
        can_recommend = weight_properties.get('can_recommend_instruments', False)
        should_expand = weight_properties.get('should_expand_instrument_jobs', True)
        
        print(f"   can_recommend_instruments: {can_recommend}")
        print(f"   should_expand_instrument_jobs: {should_expand}")
        
        # Expected values (defaults)
        if not can_recommend and should_expand:
            print("   ✅ FMPSenateTraderWeight properties are correct (using defaults)!")
        else:
            print("   ❌ FMPSenateTraderWeight properties are incorrect!")
            print(f"      Expected: can_recommend_instruments=False, should_expand_instrument_jobs=True (defaults)")
            print(f"      Got: can_recommend_instruments={can_recommend}, should_expand_instrument_jobs={should_expand}")
            
    except AttributeError:
        print("   ✅ FMPSenateTraderWeight has no get_expert_properties method (uses defaults)")
    except Exception as e:
        print(f"   ❌ Error getting FMPSenateTraderWeight properties: {e}")
    
    print()
    
    # Test settings definitions for FMPSenateTraderCopy (should not have should_expand_instrument_jobs)
    print("3. FMPSenateTraderCopy Settings (should NOT contain should_expand_instrument_jobs):")
    try:
        copy_settings = FMPSenateTraderCopy.get_settings_definitions()
        if 'should_expand_instrument_jobs' in copy_settings:
            print("   ❌ should_expand_instrument_jobs found in settings - it should be in properties!")
        else:
            print("   ✅ should_expand_instrument_jobs correctly moved from settings to properties!")
        
        print(f"   Available settings: {list(copy_settings.keys())}")
        
    except Exception as e:
        print(f"   ❌ Error getting FMPSenateTraderCopy settings: {e}")

if __name__ == "__main__":
    test_expert_properties()
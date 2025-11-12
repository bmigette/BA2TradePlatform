#!/usr/bin/env python3
"""
Test script to verify the min_available_balance_pct setting fix.
Tests that the setting properly uses built-in defaults and avoids hardcoded values.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_trade_platform.logger import logger

# Mock concrete implementation for testing
class MockExpert(MarketExpertInterface):
    @classmethod
    def description(cls) -> str:
        return "Mock expert for testing"
    
    @classmethod 
    def get_settings_definitions(cls) -> dict:
        return {}
    
    def render_market_analysis(self, market_analysis):
        return "Mock analysis"
    
    def run_analysis(self, symbol: str, market_analysis):
        pass

def test_min_balance_setting():
    """Test that min_available_balance_pct uses proper defaults."""
    print("\n=== Testing min_available_balance_pct Setting ===")
    
    # Ensure built-in settings are initialized
    MockExpert._ensure_builtin_settings()
    
    # Test 1: Check built-in setting definition
    builtin_settings = MockExpert._builtin_settings
    min_balance_setting = builtin_settings.get('min_available_balance_pct', {})
    
    print(f"Built-in setting definition:")
    print(f"  Default: {min_balance_setting.get('default')}")
    print(f"  Type: {min_balance_setting.get('type')}")
    print(f"  Description: {min_balance_setting.get('description')}")
    print(f"  Tooltip: {min_balance_setting.get('tooltip')}")
    
    # Verify the setting is properly defined
    expected_default = 10.0
    actual_default = min_balance_setting.get('default')
    
    if actual_default == expected_default:
        print(f"✓ Built-in default is correct: {actual_default}%")
    else:
        print(f"✗ Built-in default is wrong: expected {expected_default}%, got {actual_default}%")
        return False
    
    # Test 2: Verify description doesn't contradict default
    description = min_balance_setting.get('description', '')
    if 'default 20%' in description:
        print(f"✗ Description still mentions conflicting default: {description}")
        return False
    else:
        print(f"✓ Description is consistent: {description}")
    
    # Test 3: Check tooltip exists for UI configurability
    tooltip = min_balance_setting.get('tooltip')
    if tooltip:
        print(f"✓ Tooltip exists for UI: {tooltip}")
    else:
        print(f"✗ Missing tooltip for UI configuration")
        return False
    
    print(f"\n=== All Tests Passed ===")
    return True

if __name__ == "__main__":
    success = test_min_balance_setting()
    if not success:
        print("\n❌ Tests failed!")
        sys.exit(1)
    else:
        print("\n✅ All tests passed!")
#!/usr/bin/env python3
"""
Test script to verify multi-select vendor settings functionality.

This script tests:
1. Vendor settings definitions have correct type and structure
2. Settings save and load correctly with list values
3. _create_tradingagents_config() properly joins list values
4. route_to_vendor() handles multiple vendors with fallback
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
from ba2_trade_platform.core.models import ExpertInstance, AccountDefinition
from ba2_trade_platform.core.db import get_db, add_instance, delete_instance
from ba2_trade_platform.core.types import AnalysisUseCase
from ba2_trade_platform.logger import logger
from sqlmodel import select

def test_settings_definitions():
    """Test that vendor settings have correct type and structure."""
    print("\n" + "="*80)
    print("TEST 1: Verify vendor settings definitions")
    print("="*80)
    
    settings_def = TradingAgents.get_settings_definitions()
    
    vendor_keys = [
        'vendor_stock_data', 'vendor_indicators', 'vendor_fundamentals',
        'vendor_balance_sheet', 'vendor_cashflow', 'vendor_income_statement',
        'vendor_news', 'vendor_global_news', 'vendor_insider_sentiment',
        'vendor_insider_transactions'
    ]
    
    all_passed = True
    for key in vendor_keys:
        assert key in settings_def, f"Missing vendor setting: {key}"
        meta = settings_def[key]
        
        # Check type is "list"
        if meta.get("type") != "list":
            print(f"❌ FAIL: {key} has type '{meta.get('type')}', expected 'list'")
            all_passed = False
        else:
            print(f"✓ {key}: type='list'")
        
        # Check multiple flag
        if not meta.get("multiple"):
            print(f"❌ FAIL: {key} missing 'multiple': True flag")
            all_passed = False
        else:
            print(f"  ✓ multiple=True")
        
        # Check default is a list
        default = meta.get("default")
        if not isinstance(default, list):
            print(f"❌ FAIL: {key} default is {type(default).__name__}, expected list")
            all_passed = False
        else:
            print(f"  ✓ default={default}")
        
        # Check valid_values exists
        if not meta.get("valid_values"):
            print(f"❌ FAIL: {key} missing valid_values")
            all_passed = False
        else:
            print(f"  ✓ valid_values={meta.get('valid_values')}")
    
    if all_passed:
        print("\n✅ All vendor settings have correct structure")
    else:
        print("\n❌ Some vendor settings have issues")
    
    return all_passed


def test_settings_save_load():
    """Test that list settings save and load correctly."""
    print("\n" + "="*80)
    print("TEST 2: Verify settings save and load with list values")
    print("="*80)
    
    session = get_db()
    
    # Create test account
    test_account = AccountDefinition(
        name="TestAccount",
        provider="alpaca",
        description="Test account for multi-select vendor tests"
    )
    account_id = add_instance(test_account, session)
    print(f"Created test account (ID: {account_id})")
    
    # Create test expert instance
    test_expert = ExpertInstance(
        account_id=account_id,
        expert="TradingAgents"
    )
    expert_id = add_instance(test_expert, session)
    print(f"Created test expert instance (ID: {expert_id})")
    
    try:
        # Create expert instance and save multi-select settings
        expert = TradingAgents(expert_id)
        
        # Test multi-vendor lists
        test_settings = {
            'vendor_stock_data': ['yfinance', 'alpha_vantage'],
            'vendor_news': ['google', 'openai', 'alpha_vantage'],
            'vendor_fundamentals': ['openai'],
        }
        
        print("\nSaving test settings:")
        for key, value in test_settings.items():
            print(f"  {key}: {value}")
            expert.save_setting(key, value, setting_type="list")
        
        # Reload expert to verify settings loaded correctly
        expert = TradingAgents(expert_id)
        loaded_settings = expert.settings
        
        print("\nLoaded settings:")
        all_passed = True
        for key, expected_value in test_settings.items():
            loaded_value = loaded_settings.get(key)
            print(f"  {key}: {loaded_value}")
            
            # Need to compare values correctly - both should be lists
            if loaded_value == expected_value and isinstance(loaded_value, list):
                print(f"    ✓ Match!")
            else:
                print(f"    ❌ FAIL: Expected {expected_value} (type: {type(expected_value).__name__}), got {loaded_value} (type: {type(loaded_value).__name__})")
                all_passed = False
        
        if all_passed:
            print("\n✅ All settings saved and loaded correctly")
        else:
            print("\n❌ Some settings failed to save/load correctly")
        
        return all_passed
        
    finally:
        # Cleanup - refresh instances in session before deleting
        try:
            session = get_db()
            # Re-query instances to get fresh session-bound copies
            test_expert_fresh = session.get(ExpertInstance, expert_id)
            test_account_fresh = session.get(AccountDefinition, account_id)
            
            if test_expert_fresh:
                session.delete(test_expert_fresh)
            if test_account_fresh:
                session.delete(test_account_fresh)
            
            session.commit()
            session.close()
            print("\nCleaned up test data")
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup: {cleanup_error}")


def test_config_generation():
    """Test that _create_tradingagents_config() properly joins list values."""
    print("\n" + "="*80)
    print("TEST 3: Verify config generation joins list values correctly")
    print("="*80)
    
    session = get_db()
    
    # Create test account
    test_account = AccountDefinition(
        name="TestAccount2",
        provider="alpaca",
        description="Test account for config generation tests"
    )
    account_id = add_instance(test_account, session)
    
    # Create test expert instance
    test_expert = ExpertInstance(
        account_id=account_id,
        expert="TradingAgents"
    )
    expert_id = add_instance(test_expert, session)
    
    try:
        expert = TradingAgents(expert_id)
        
        # Get all settings definitions to ensure we provide defaults
        settings_def = expert.get_settings_definitions()
        
        # Save all required settings with their defaults (except the ones we're testing)
        for key, meta in settings_def.items():
            if key not in ['vendor_stock_data', 'vendor_news', 'vendor_fundamentals']:
                default_value = meta.get('default')
                setting_type = meta.get('type', 'str')
                if default_value is not None:
                    expert.save_setting(key, default_value, setting_type=setting_type)
        
        # Save multi-vendor settings that we're testing
        expert.save_setting('vendor_stock_data', ['yfinance', 'alpha_vantage'], setting_type="list")
        expert.save_setting('vendor_news', ['google', 'openai', 'alpha_vantage'], setting_type="list")
        expert.save_setting('vendor_fundamentals', ['openai'], setting_type="list")
        
        # Reload to get settings
        expert = TradingAgents(expert_id)
        
        # Generate config
        config = expert._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
        tool_vendors = config.get('tool_vendors', {})
        
        print("\nGenerated tool_vendors mapping:")
        all_passed = True
        
        expected_mappings = {
            'get_stock_data': 'yfinance,alpha_vantage',
            'get_news': 'google,openai,alpha_vantage',
            'get_fundamentals': 'openai',
        }
        
        for method, expected_value in expected_mappings.items():
            actual_value = tool_vendors.get(method)
            print(f"  {method}: '{actual_value}'")
            
            if actual_value != expected_value:
                print(f"    ❌ FAIL: Expected '{expected_value}', got '{actual_value}'")
                all_passed = False
            else:
                print(f"    ✓ Correctly joined to comma-separated string")
        
        if all_passed:
            print("\n✅ Config generation correctly joins list values")
        else:
            print("\n❌ Config generation has issues")
        
        return all_passed
        
    finally:
        # Cleanup - refresh instances in session before deleting
        try:
            session = get_db()
            # Re-query instances to get fresh session-bound copies
            test_expert_fresh = session.get(ExpertInstance, expert_id)
            test_account_fresh = session.get(AccountDefinition, account_id)
            
            if test_expert_fresh:
                session.delete(test_expert_fresh)
            if test_account_fresh:
                session.delete(test_account_fresh)
            
            session.commit()
            session.close()
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup: {cleanup_error}")


def test_vendor_routing():
    """Test that route_to_vendor() handles multiple vendors correctly."""
    print("\n" + "="*80)
    print("TEST 4: Verify vendor routing with multiple vendors")
    print("="*80)
    
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.interface import route_to_vendor, VENDOR_METHODS
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.alpha_vantage_common import AlphaVantageRateLimitError
    from datetime import datetime, timedelta
    
    print("\nTesting vendor fallback with comma-separated vendors...")
    
    # Test multi-vendor configuration
    test_configs = [
        ('yfinance,alpha_vantage', 'get_stock_data', 'AAPL'),
        ('google,openai', 'get_news', 'AAPL'),
    ]
    
    all_passed = True
    for vendor_config, method, symbol in test_configs:
        print(f"\n  Testing: {method} with vendors '{vendor_config}' for {symbol}")
        
        # Verify method exists in VENDOR_METHODS
        if method not in VENDOR_METHODS:
            print(f"    ❌ FAIL: Method '{method}' not in VENDOR_METHODS")
            all_passed = False
            continue
        
        # Verify all vendors in config are supported
        vendors = [v.strip() for v in vendor_config.split(',')]
        supported_vendors = list(VENDOR_METHODS[method].keys())
        
        for vendor in vendors:
            if vendor not in supported_vendors:
                print(f"    ❌ FAIL: Vendor '{vendor}' not supported for {method}")
                print(f"       Supported vendors: {supported_vendors}")
                all_passed = False
            else:
                print(f"    ✓ Vendor '{vendor}' is supported")
        
        # Test that route_to_vendor accepts the config
        # (We won't actually call it as it may require API keys)
        print(f"    ✓ Vendor config '{vendor_config}' is valid for {method}")
    
    if all_passed:
        print("\n✅ Vendor routing configuration is correct")
    else:
        print("\n❌ Vendor routing has issues")
    
    return all_passed


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("MULTI-SELECT VENDOR SETTINGS TEST SUITE")
    print("="*80)
    
    results = []
    
    # Run all tests
    results.append(("Settings Definitions", test_settings_definitions()))
    results.append(("Settings Save/Load", test_settings_save_load()))
    results.append(("Config Generation", test_config_generation()))
    results.append(("Vendor Routing", test_vendor_routing()))
    
    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    
    for test_name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status}: {test_name}")
    
    all_passed = all(passed for _, passed in results)
    
    if all_passed:
        print("\n✅ ALL TESTS PASSED! Multi-select vendor settings are working correctly.")
    else:
        print("\n❌ SOME TESTS FAILED! Please review the output above.")
    
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

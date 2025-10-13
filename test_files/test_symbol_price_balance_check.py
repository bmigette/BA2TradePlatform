#!/usr/bin/env python3
"""
Test script to verify the symbol price and balance analysis check functionality.
Tests the should_skip_analysis_for_symbol method with different scenarios.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.logger import logger

def test_symbol_price_balance_check():
    """Test the symbol price and balance check functionality."""
    
    print("=== Testing Symbol Price and Balance Analysis Check ===\n")
    
    try:
        # Get first available expert instance for testing
        expert_instance = get_instance(ExpertInstance, 1)
        if not expert_instance:
            print("❌ No expert instance found with ID 1. Please create an expert first.")
            return
        
        if not expert_instance.enabled:
            print(f"❌ Expert instance {expert_instance.id} is disabled. Please enable it first.")
            return
        
        # Get the expert class instance
        expert = get_expert_instance_from_id(expert_instance.id)
        if not expert:
            print(f"❌ Could not load expert instance {expert_instance.id}")
            return
        
        print(f"✅ Using expert: {expert_instance.expert} (ID: {expert_instance.id})")
        print(f"   Account ID: {expert_instance.account_id}")
        
        # Get expert balances for reference
        virtual_balance = expert.get_virtual_balance()
        available_balance = expert.get_available_balance()
        
        print(f"   Virtual Balance: ${virtual_balance:.2f}" if virtual_balance else "   Virtual Balance: N/A")
        print(f"   Available Balance: ${available_balance:.2f}" if available_balance else "   Available Balance: N/A")
        print()
        
        # Test symbols with different expected outcomes
        test_symbols = [
            ("AAPL", "Apple - typical stock price"),
            ("BRK.A", "Berkshire Hathaway Class A - very expensive stock"),
            ("TSLA", "Tesla - moderate price stock"),
            ("MSFT", "Microsoft - typical stock price"),
            ("GOOGL", "Google - moderate price stock")
        ]
        
        print("Testing symbols for analysis eligibility:")
        print("-" * 80)
        
        for symbol, description in test_symbols:
            try:
                should_skip, reason = expert.should_skip_analysis_for_symbol(symbol)
                
                status = "❌ SKIP" if should_skip else "✅ PROCEED"
                print(f"{status} {symbol:8} - {description}")
                
                if should_skip:
                    print(f"         Reason: {reason}")
                else:
                    print(f"         Analysis can proceed")
                
                print()
                
            except Exception as e:
                print(f"❌ ERROR {symbol:8} - Failed to check: {e}")
                print()
        
        # Test edge cases
        print("\nTesting edge cases:")
        print("-" * 80)
        
        # Test with invalid symbol
        try:
            should_skip, reason = expert.should_skip_analysis_for_symbol("INVALID_SYMBOL_12345")
            status = "❌ SKIP" if should_skip else "✅ PROCEED"
            print(f"{status} INVALID  - Invalid/non-existent symbol")
            if should_skip:
                print(f"         Reason: {reason}")
            print()
        except Exception as e:
            print(f"❌ ERROR INVALID  - Exception handling test: {e}")
            print()
        
        print("Test completed successfully!")
        
    except Exception as e:
        logger.error(f"Error during symbol price balance check test: {e}", exc_info=True)
        print(f"❌ Test failed with error: {e}")

if __name__ == "__main__":
    test_symbol_price_balance_check()
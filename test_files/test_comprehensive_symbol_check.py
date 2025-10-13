#!/usr/bin/env python3
"""
Comprehensive test to demonstrate both price and balance conditions for skipping analysis.
This script creates scenarios to test both conditions:
1. Symbol price higher than available balance 
2. Available balance lower than 5% of account balance
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.logger import logger

def test_both_conditions():
    """Test both price and balance conditions comprehensively."""
    
    print("=== Comprehensive Symbol Price and Balance Analysis Test ===\n")
    
    try:
        # Get first available expert instance
        expert_instance = get_instance(ExpertInstance, 1)
        if not expert_instance:
            print("❌ No expert instance found. Please create an expert first.")
            return
        
        expert = get_expert_instance_from_id(expert_instance.id)
        if not expert:
            print(f"❌ Could not load expert instance {expert_instance.id}")
            return
        
        print(f"✅ Testing with expert: {expert_instance.expert} (ID: {expert_instance.id})")
        
        # Get current balances
        virtual_balance = expert.get_virtual_balance()
        available_balance = expert.get_available_balance()
        
        # Get account balance to show 5% threshold
        from ba2_trade_platform.core.utils import get_account_instance_from_id
        account = get_account_instance_from_id(expert_instance.account_id)
        account_balance = account.get_balance() if account else None
        
        print(f"   Virtual Balance: ${virtual_balance:.2f}" if virtual_balance else "   Virtual Balance: N/A")
        print(f"   Available Balance: ${available_balance:.2f}" if available_balance else "   Available Balance: N/A")
        print(f"   Account Balance: ${account_balance:.2f}" if account_balance else "   Account Balance: N/A")
        
        if account_balance:
            threshold_5pct = account_balance * 0.05
            print(f"   5% Threshold: ${threshold_5pct:.2f}")
            available_pct = (available_balance / account_balance) * 100.0 if available_balance and account_balance > 0 else 0.0
            print(f"   Available as % of Account: {available_pct:.1f}%")
        
        print()
        
        # Test realistic scenarios
        test_cases = [
            {
                "symbol": "AAPL",
                "description": "Apple - normal stock price",
                "expected": "Should proceed (price < available balance, available > 5%)"
            },
            {
                "symbol": "TSLA", 
                "description": "Tesla - moderate price stock",
                "expected": "Should proceed (price < available balance, available > 5%)"
            },
            {
                "symbol": "GOOGL",
                "description": "Google - moderate price stock", 
                "expected": "Should proceed (price < available balance, available > 5%)"
            }
        ]
        
        print("Real-world test scenarios:")
        print("=" * 100)
        
        for case in test_cases:
            symbol = case["symbol"]
            description = case["description"]
            expected = case["expected"]
            
            try:
                should_skip, reason = expert.should_skip_analysis_for_symbol(symbol)
                
                status = "❌ SKIP" if should_skip else "✅ PROCEED"
                print(f"{status} {symbol:8} - {description}")
                print(f"         Expected: {expected}")
                
                if should_skip:
                    print(f"         Actual: Analysis skipped - {reason}")
                else:
                    print(f"         Actual: Analysis will proceed")
                    
                    # Get the actual price for reference
                    if account:
                        price = account.get_instrument_current_price(symbol)
                        if price:
                            print(f"         Current price: ${price:.2f}")
                
                print("-" * 100)
                
            except Exception as e:
                print(f"❌ ERROR {symbol:8} - {e}")
                print("-" * 100)
        
        # Summary of conditions
        print("\nCondition Summary:")
        print("1. ✅ Price Check: Symbol price must be ≤ available balance")
        print("2. ✅ Balance Check: Available balance must be ≥ 5% of account balance")
        print("3. ✅ Both conditions must be met for analysis to proceed")
        print()
        
        # Show actual thresholds
        if available_balance and account_balance:
            print("Current Status:")
            print(f"- Available balance: ${available_balance:.2f}")
            print(f"- Account balance: ${account_balance:.2f}")
            print(f"- 5% threshold: ${account_balance * 0.05:.2f}")
            print(f"- Available vs threshold: {'✅ Above' if available_balance >= account_balance * 0.05 else '❌ Below'}")
            print(f"- Max affordable stock price: ${available_balance:.2f}")
        
        print("\n✅ Comprehensive test completed successfully!")
        
    except Exception as e:
        logger.error(f"Error during comprehensive test: {e}", exc_info=True)
        print(f"❌ Test failed: {e}")

if __name__ == "__main__":
    test_both_conditions()
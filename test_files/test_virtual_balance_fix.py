"""
Test script to verify virtual balance calculation fix.
Tests that expert's available balance correctly uses get_virtual_balance() and get_available_balance()
independent of other experts' positions.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id, get_account_instance_from_id
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit

def test_virtual_balance_calculation():
    """Test that virtual balance is calculated correctly for expert 11."""
    
    print("\n" + "="*80)
    print("VIRTUAL BALANCE CALCULATION TEST")
    print("="*80)
    
    expert_id = 11
    
    # Get expert instance
    expert = get_expert_instance_from_id(expert_id)
    if not expert:
        print(f"ERROR: Expert {expert_id} not found")
        return
    
    # Get expert instance record for virtual equity percentage
    expert_record = get_instance(ExpertInstance, expert_id)
    if not expert_record:
        print(f"ERROR: Expert record {expert_id} not found")
        return
    
    print(f"\nExpert ID: {expert_id}")
    print(f"Account ID: {expert_record.account_id}")
    print(f"Virtual Equity %: {expert_record.virtual_equity_pct}%")
    
    # Get account info
    account = get_account_instance_from_id(expert_record.account_id)
    if not account:
        print(f"ERROR: Account {expert_record.account_id} not found")
        return
    
    account_info = account.get_account_info()
    print(f"\nAccount Total Equity: ${float(account_info.equity):,.2f}")
    print(f"Account Cash: ${float(account_info.cash):,.2f}")
    
    # Test expert methods directly
    print(f"\n--- Expert Method Results ---")
    virtual_balance = expert.get_virtual_balance()
    available_balance = expert.get_available_balance()
    
    print(f"Expert Virtual Balance (get_virtual_balance): ${virtual_balance:,.2f}")
    print(f"Expert Available Balance (get_available_balance): ${available_balance:,.2f}")
    
    # Test SmartRiskManagerToolkit
    print(f"\n--- SmartRiskManagerToolkit Results ---")
    toolkit = SmartRiskManagerToolkit(expert_id, expert_record.account_id)
    portfolio_status = toolkit.get_portfolio_status()
    
    print(f"Toolkit Virtual Equity: ${portfolio_status['account_virtual_equity']:,.2f}")
    print(f"Toolkit Available Balance: ${portfolio_status['account_available_balance']:,.2f}")
    print(f"Balance % Available: {portfolio_status['account_balance_pct_available']:.1f}%")
    print(f"Open Positions: {portfolio_status['risk_metrics']['num_positions']}")
    print(f"Total Position Value: ${portfolio_status['total_position_value']:,.2f}")
    
    # Verify consistency
    print(f"\n--- Verification ---")
    if portfolio_status['account_virtual_equity'] == virtual_balance:
        print("✓ Virtual equity matches expert.get_virtual_balance()")
    else:
        print(f"✗ Virtual equity mismatch: {portfolio_status['account_virtual_equity']} != {virtual_balance}")
    
    if portfolio_status['account_available_balance'] == available_balance:
        print("✓ Available balance matches expert.get_available_balance()")
    else:
        print(f"✗ Available balance mismatch: {portfolio_status['account_available_balance']} != {available_balance}")
    
    # Explain the calculation
    print(f"\n--- Balance Calculation Explanation ---")
    print(f"Virtual Balance = Account Equity × Virtual Equity %")
    print(f"                = ${float(account_info.equity):,.2f} × {expert_record.virtual_equity_pct}%")
    print(f"                = ${virtual_balance:,.2f}")
    print(f"\nAvailable Balance = Virtual Balance - This Expert's Position Value")
    print(f"                  = ${virtual_balance:,.2f} - ${portfolio_status['total_position_value']:,.2f}")
    print(f"                  = ${available_balance:,.2f}")
    print(f"\nNote: Available balance is independent of other experts' positions on the same account.")
    print(f"This expert has {portfolio_status['account_balance_pct_available']:.1f}% of its virtual balance available.")
    
    print("\n" + "="*80)
    print("TEST COMPLETE")
    print("="*80 + "\n")

if __name__ == "__main__":
    test_virtual_balance_calculation()

"""
Test script for get_trade_summary_by_symbol functionality.

This script tests the new trade summary aggregation feature that shows
buy/sell quantities across all experts on an account.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import ExpertInstance, Transaction
from sqlmodel import select


def test_trade_summary():
    """Test the get_trade_summary_by_symbol function."""
    
    print("=" * 80)
    print("Testing get_trade_summary_by_symbol() functionality")
    print("=" * 80)
    print()
    
    # Get first expert instance to test with
    with get_db() as session:
        expert = session.exec(select(ExpertInstance)).first()
        
        if not expert:
            print("❌ No expert instances found in database")
            return
        
        print(f"✅ Found expert instance: ID={expert.id}, Account ID={expert.account_id}")
        print()
        
        # Check for transactions
        transactions = session.exec(
            select(Transaction)
            .where(Transaction.expert_id == expert.id)
        ).all()
        
        print(f"Found {len(transactions)} transactions for expert {expert.id}")
        for trans in transactions[:5]:  # Show first 5
            print(f"  - Transaction {trans.id}: {trans.symbol} ({trans.status})")
        if len(transactions) > 5:
            print(f"  ... and {len(transactions) - 5} more")
        print()
    
    # Create toolkit and test the function
    try:
        toolkit = SmartRiskManagerToolkit(expert.id, expert.account_id)
        print(f"✅ Created SmartRiskManagerToolkit for expert {expert.id}")
        print()
        
        print("Calling get_trade_summary_by_symbol()...")
        print("-" * 80)
        
        summary = toolkit.get_trade_summary_by_symbol()
        
        if not summary:
            print("No positions or pending orders found across any experts.")
            print()
        else:
            print(f"Found {len(summary)} symbols with exposure:")
            print()
            print("SYMBOL: BUY QTY, SELL QTY")
            print("-" * 40)
            
            for symbol in sorted(summary.keys()):
                buy_qty = summary[symbol]["buy_qty"]
                sell_qty = summary[symbol]["sell_qty"]
                print(f"{symbol}: BUY QTY {buy_qty:.0f}, SELL QTY {sell_qty:.0f}")
            
            print()
            print(f"Total symbols with exposure: {len(summary)}")
        
        print("-" * 80)
        print()
        print("✅ Test completed successfully!")
        
    except Exception as e:
        print(f"❌ Error testing toolkit: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_trade_summary()

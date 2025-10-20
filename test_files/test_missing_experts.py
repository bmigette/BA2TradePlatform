"""
Check for transactions without expert_id to understand the discrepancy.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import TransactionStatus
from sqlmodel import select

def check_transactions_without_experts():
    """Check for open transactions that don't have an expert_id."""
    
    print("="*80)
    print("CHECKING TRANSACTIONS WITHOUT EXPERT_ID")
    print("="*80)
    
    session = get_db()
    try:
        # Get ALL open transactions
        all_open_transactions = session.exec(
            select(Transaction)
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        print(f"\nTotal OPEN/WAITING transactions: {len(all_open_transactions)}")
        
        # Get transactions WITH experts
        with_experts = session.exec(
            select(Transaction)
            .where(Transaction.expert_id.isnot(None))
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        print(f"Transactions WITH expert_id: {len(with_experts)}")
        
        # Get transactions WITHOUT experts
        without_experts = session.exec(
            select(Transaction)
            .where(Transaction.expert_id.is_(None))
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        print(f"Transactions WITHOUT expert_id: {len(without_experts)}")
        
        if without_experts:
            print("\n" + "="*80)
            print("TRANSACTIONS WITHOUT EXPERT_ID (DETAILED)")
            print("="*80)
            
            for trans in without_experts:
                print(f"\nTransaction ID: {trans.id}")
                print(f"  Symbol: {trans.symbol}")
                print(f"  Quantity: {trans.quantity}")
                print(f"  Open Price: ${trans.open_price:.2f}" if trans.open_price else "  Open Price: None")
                print(f"  Status: {trans.status}")
                print(f"  Expert ID: {trans.expert_id} (None)")
                print(f"  Created: {trans.created_at}")
                
                # Get orders for this transaction
                orders = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                ).all()
                
                print(f"  Orders: {len(orders)}")
                for order in orders:
                    print(f"    Order {order.id}: {order.side} {order.quantity} @ ${order.open_price if order.open_price else 'N/A'} - Status: {order.status}")
        
        # Also check if there are ANY transactions with expert_id set
        print("\n" + "="*80)
        print("SAMPLE TRANSACTIONS WITH EXPERT_ID")
        print("="*80)
        
        sample_with_experts = session.exec(
            select(Transaction)
            .where(Transaction.expert_id.isnot(None))
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            .limit(5)
        ).all()
        
        for trans in sample_with_experts:
            print(f"\nTransaction ID: {trans.id}")
            print(f"  Symbol: {trans.symbol}")
            print(f"  Expert ID: {trans.expert_id}")
            print(f"  Status: {trans.status}")
        
    finally:
        session.close()

if __name__ == "__main__":
    check_transactions_without_experts()

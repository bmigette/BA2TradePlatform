#!/usr/bin/env python3
"""Investigate SmartRiskManagerJob runs #21 and #22 - focus on transactions."""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import SmartRiskManagerJob, ExpertInstance, Transaction, AccountDefinition
from sqlalchemy import select

def investigate_runs():
    """Check run #21 and #22 for issues."""
    print("=" * 100)
    print("INVESTIGATING SMARTRISKMANAGERJOB RUNS #21 and #22")
    print("=" * 100)
    
    with get_db() as session:
        # Get run #22
        print("\n" + "=" * 100)
        print("RUN #22 - TA-Dynamic-grok (7% vs 5% allocation issue)")
        print("=" * 100)
        
        run22 = session.exec(select(SmartRiskManagerJob).where(SmartRiskManagerJob.id == 22)).first()
        if run22:
            # Get expert name
            expert = session.exec(select(ExpertInstance).where(ExpertInstance.id == run22.expert_instance_id)).first()
            expert_name = f"{expert.alias}" if expert else f"ID:{run22.expert_instance_id}"
            
            # Get account info
            account = session.exec(select(AccountDefinition).where(AccountDefinition.id == run22.account_id)).first()
            account_balance = account.last_known_balance if account else None
            
            print(f"\nRun Details:")
            print(f"  Expert: {expert_name} (ID: {expert.expert if expert else 'N/A'})")
            print(f"  Status: {run22.status}")
            print(f"  Run Date: {run22.run_date}")
            print(f"  Initial Portfolio Equity: ${run22.initial_portfolio_equity}")
            print(f"  Final Portfolio Equity: ${run22.final_portfolio_equity}")
            print(f"  Account Balance: ${account_balance}")
            print(f"  Actions Taken: {run22.actions_taken_count}")
            print(f"  Model Used: {run22.model_used}")
            
            # Get transactions from this job
            print(f"\n  Transactions from this job:")
            stmt = select(Transaction).where(Transaction.smart_risk_manager_job_id == 22)
            transactions = session.scalars(stmt).all()
            
            if transactions:
                print(f"    Total transactions: {len(transactions)}")
                total_qty = 0
                total_notional = 0
                for tx in transactions:
                    qty = tx.quantity if tx.quantity else 0
                    price = tx.entry_price if tx.entry_price else 0
                    notional = qty * price
                    total_qty += qty
                    total_notional += notional
                    direction = "BUY" if tx.direction.value == "BUY" else "SELL" if tx.direction else "?"
                    print(f"      {direction:4} {tx.symbol:6} x{qty:3} @ ${price:7.2f} = ${notional:10.2f}")
                
                print(f"\n    Total notional value (at entry): ${total_notional:,.2f}")
                print(f"    Account balance used: {(total_notional / account_balance * 100):.1f}%")
                print(f"    ERROR: Should be 5%, but is {(total_notional / account_balance * 100):.1f}%!")
            else:
                print("    No transactions found")
        else:
            print("Run #22 not found")
        
        # Get run #21
        print("\n" + "=" * 100)
        print("RUN #21 - TA-Dynamic-QwenMax (failure investigation)")
        print("=" * 100)
        
        run21 = session.exec(select(SmartRiskManagerJob).where(SmartRiskManagerJob.id == 21)).first()
        if run21:
            # Get expert name
            expert = session.exec(select(ExpertInstance).where(ExpertInstance.id == run21.expert_instance_id)).first()
            expert_name = f"{expert.alias}" if expert else f"ID:{run21.expert_instance_id}"
            
            # Get account info
            account = session.exec(select(AccountDefinition).where(AccountDefinition.id == run21.account_id)).first()
            account_balance = account.last_known_balance if account else None
            
            print(f"\nRun Details:")
            print(f"  Expert: {expert_name} (ID: {expert.expert if expert else 'N/A'})")
            print(f"  Status: {run21.status}")
            print(f"  Run Date: {run21.run_date}")
            print(f"  Error Message: {run21.error_message}")
            print(f"  Initial Portfolio Equity: ${run21.initial_portfolio_equity}")
            print(f"  Final Portfolio Equity: ${run21.final_portfolio_equity}")
            print(f"  Account Balance: ${account_balance}")
            print(f"  Actions Taken: {run21.actions_taken_count}")
            print(f"  Model Used: {run21.model_used}")
            
            # Get transactions from this job
            print(f"\n  Transactions from this job:")
            stmt = select(Transaction).where(Transaction.smart_risk_manager_job_id == 21)
            transactions = session.scalars(stmt).all()
            
            if transactions:
                print(f"    Total transactions: {len(transactions)}")
                for tx in transactions:
                    qty = tx.quantity if tx.quantity else 0
                    price = tx.entry_price if tx.entry_price else 0
                    direction = "BUY" if tx.direction.value == "BUY" else "SELL" if tx.direction else "?"
                    print(f"      {direction:4} {tx.symbol:6} x{qty:3} @ ${price:7.2f}")
            else:
                print("    No transactions found")
        else:
            print("Run #21 not found")

if __name__ == "__main__":
    investigate_runs()

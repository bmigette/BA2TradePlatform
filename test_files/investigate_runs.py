#!/usr/bin/env python3
"""Investigate SmartRiskManagerJob runs #21 and #22."""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import SmartRiskManagerJob, ExpertInstance, Transaction
from sqlalchemy import select

def investigate_runs():
    """Check run #21 and #22 for issues."""
    print("=" * 80)
    print("INVESTIGATING SMARTRISKMANAGERJOB RUNS")
    print("=" * 80)
    
    with get_db() as session:
        # Get run #22
        print("\n" + "=" * 80)
        print("RUN #22 - TA-Dynamic-grok (7% vs 5% issue)")
        print("=" * 80)
        
        run22 = session.query(SmartRiskManagerJob).filter_by(id=22).first()
        if run22:
            print(f"\nRun ID: {run22.id}")
            print(f"Expert Instance ID: {run22.expert_instance_id}")
            print(f"Account ID: {run22.account_id}")
            print(f"Status: {run22.status}")
            print(f"Created: {run22.run_date}")
            print(f"Error Message: {run22.error_message}")
            print(f"Duration: {run22.duration_seconds}s")
            print(f"Model Used: {run22.model_used}")
            print(f"Actions Taken: {run22.actions_taken_count}")
            print(f"Initial Equity: ${run22.initial_portfolio_equity or 'N/A'}")
            print(f"Final Equity: ${run22.final_portfolio_equity or 'N/A'}")
            print(f"Actions Summary: {run22.actions_summary}")
            
            # Get expert instance
            expert = session.query(ExpertInstance).filter_by(id=run22.expert_instance_id).first()
            if expert:
                print(f"\nExpert Instance:")
                print(f"  ID: {expert.id}")
                print(f"  Expert: {expert.expert}")
                print(f"  Alias: {expert.alias}")
                print(f"  Settings: {expert.settings}")
            
            # Check state
            if run22.graph_state:
                print(f"\nGraph State:")
                print(json.dumps(run22.graph_state, indent=2, default=str)[:1000] + "...")  # Truncate for readability
            
            # Get transactions from this job
            print(f"\nTransactions from this job:")
            stmt = select(Transaction).where(Transaction.smart_risk_manager_job_id == 22)
            transactions = session.scalars(stmt).all()
            
            if transactions:
                total_qty = 0
                for tx in transactions:
                    qty = tx.quantity if tx.quantity else 0
                    print(f"  - {tx.symbol}: {qty} shares @ ${tx.entry_price or 'N/A'}")
                    total_qty += qty
                print(f"Total shares opened: {total_qty}")
                
                # Check account balance
                if transactions:
                    first_tx = transactions[0]
                    print(f"\n  Account balance at transaction time: ${first_tx.account_balance or 'N/A'}")
                    if first_tx.account_balance and first_tx.entry_price:
                        pct_used = (total_qty * first_tx.entry_price / first_tx.account_balance * 100) if first_tx.account_balance else 0
                        print(f"  Percentage of account used: {pct_used:.1f}%")
            else:
                print("  No transactions found")
        else:
            print("Run #22 not found")
        
        # Get run #21
        print("\n" + "=" * 80)
        print("RUN #21 - TA-Dynamic-QwenMax (failure investigation)")
        print("=" * 80)
        
        run21 = session.query(SmartRiskManagerJob).filter_by(id=21).first()
        if run21:
            print(f"\nRun ID: {run21.id}")
            print(f"Expert Instance ID: {run21.expert_instance_id}")
            print(f"Account ID: {run21.account_id}")
            print(f"Status: {run21.status}")
            print(f"Created: {run21.run_date}")
            print(f"Error Message: {run21.error_message}")
            print(f"Duration: {run21.duration_seconds}s")
            print(f"Model Used: {run21.model_used}")
            print(f"Actions Taken: {run21.actions_taken_count}")
            print(f"Initial Equity: ${run21.initial_portfolio_equity or 'N/A'}")
            print(f"Final Equity: ${run21.final_portfolio_equity or 'N/A'}")
            print(f"Actions Summary: {run21.actions_summary}")
            
            # Get expert instance
            expert = session.query(ExpertInstance).filter_by(id=run21.expert_instance_id).first()
            if expert:
                print(f"\nExpert Instance:")
                print(f"  ID: {expert.id}")
                print(f"  Expert: {expert.expert}")
                print(f"  Alias: {expert.alias}")
                print(f"  Settings: {expert.settings}")
            
            # Check state
            if run21.graph_state:
                print(f"\nGraph State:")
                print(json.dumps(run21.graph_state, indent=2, default=str)[:1000] + "...")  # Truncate for readability
            
            # Get transactions from this job
            print(f"\nTransactions from this job:")
            stmt = select(Transaction).where(Transaction.smart_risk_manager_job_id == 21)
            transactions = session.scalars(stmt).all()
            
            if transactions:
                for tx in transactions:
                    print(f"  - {tx.symbol}: {tx.quantity} shares")
            else:
                print("  No transactions found")
        else:
            print("Run #21 not found")
        
        # Also list all recent runs to get context
        print("\n" + "=" * 80)
        print("RECENT SMARTRISKMANAGERJOB RUNS (last 15)")
        print("=" * 80)
        
        stmt = select(SmartRiskManagerJob).order_by(SmartRiskManagerJob.id.desc()).limit(15)
        recent_runs = session.scalars(stmt).all()
        
        print(f"\n{'ID':<5} {'Expert':<30} {'Status':<12} {'Run Date':<20}")
        print("-" * 70)
        
        for run in reversed(recent_runs):
            expert = session.query(ExpertInstance).filter_by(id=run.expert_instance_id).first()
            expert_name = f"{expert.alias or expert.expert}" if expert else f"ID:{run.expert_instance_id}"
            run_date = run.run_date.strftime('%Y-%m-%d %H:%M:%S') if run.run_date else 'N/A'
            print(f"{run.id:<5} {expert_name:<30} {run.status:<12} {run_date:<20}")

if __name__ == "__main__":
    investigate_runs()

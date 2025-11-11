#!/usr/bin/env python3
"""Investigate runs 21 and 22 - using direct SQL."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from sqlalchemy import text

def investigate_runs():
    """Check run #21 and #22 for issues."""
    print("=" * 100)
    print("INVESTIGATING SMARTRISKMANAGERJOB RUNS #21 and #22")
    print("=" * 100)
    
    with get_db() as session:
        # Use raw SQL to get info about SmartRiskManagerJob runs
        print("\n" + "=" * 100)
        print("ALL SmartRiskManagerJob runs (last 20):")
        print("=" * 100)
        
        result = session.execute(text("SELECT id, expert_instance_id, account_id, status, run_date, actions_taken_count, initial_portfolio_equity, final_portfolio_equity, error_message FROM smartriskmanagerjob ORDER BY id DESC LIMIT 20"))
        rows = result.fetchall()
        
        print(f"\n{'ID':<5} {'Expert ID':<12} {'Status':<12} {'Actions':<8} {'Initial Equity':<16} {'Final Equity':<16}")
        print("-" * 80)
        for row in rows:
            print(f"{row[0]:<5} {row[1]:<12} {row[3]:<12} {row[5]:<8} ${row[6]:<15.2f} ${row[7]:<15.2f}")
        
        # Now get detailed info for runs 21 and 22
        print("\n" + "=" * 100)
        print("RUN #22 - TA-Dynamic-grok (7% vs 5% allocation issue)")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT id, expert_instance_id, account_id, status, run_date, actions_taken_count, "
            "initial_portfolio_equity, final_portfolio_equity, actions_summary, error_message "
            "FROM smartriskmanagerjob WHERE id = 22"
        ))
        run22 = result.fetchone()
        
        if run22:
            print(f"\nRun ID: {run22[0]}")
            print(f"Expert Instance ID: {run22[1]}")
            print(f"Account ID: {run22[2]}")
            print(f"Status: {run22[3]}")
            print(f"Run Date: {run22[4]}")
            print(f"Actions Taken: {run22[5]}")
            print(f"Initial Portfolio Equity: ${run22[6]}")
            print(f"Final Portfolio Equity: ${run22[7]}")
            print(f"Error Message: {run22[9]}")
            print(f"\nActions Summary:")
            print(run22[8])
            
            # Get transactions
            print("\n\nTransactions from this job:")
            result = session.execute(text(
                'SELECT id, symbol, direction, quantity, entry_price, account_balance FROM "transaction" WHERE smart_risk_manager_job_id = 22'
            ))
            transactions = result.fetchall()
            
            if transactions:
                print(f"Total transactions: {len(transactions)}")
                total_notional = 0
                for tx in transactions:
                    tx_id, symbol, direction, qty, price, bal = tx
                    notional = qty * price
                    total_notional += notional
                    print(f"  {direction:6} {symbol:6} x{qty:3} @ ${price:8.2f} = ${notional:10.2f} (Balance: ${bal:10.2f})")
                
                if transactions:
                    account_balance = transactions[0][5]
                    pct_used = (total_notional / account_balance * 100) if account_balance else 0
                    print(f"\nTotal notional: ${total_notional:,.2f}")
                    print(f"Account balance: ${account_balance:,.2f}")
                    print(f"Percentage of account: {pct_used:.1f}%")
                    print(f"ERROR: Should be 5%, but is {pct_used:.1f}%!")
        else:
            print("Run #22 not found")
        
        # Now run 21
        print("\n" + "=" * 100)
        print("RUN #21 - TA-Dynamic-QwenMax (failure investigation)")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT id, expert_instance_id, account_id, status, run_date, actions_taken_count, "
            "initial_portfolio_equity, final_portfolio_equity, actions_summary, error_message "
            "FROM smartriskmanagerjob WHERE id = 21"
        ))
        run21 = result.fetchone()
        
        if run21:
            print(f"\nRun ID: {run21[0]}")
            print(f"Expert Instance ID: {run21[1]}")
            print(f"Account ID: {run21[2]}")
            print(f"Status: {run21[3]}")
            print(f"Run Date: {run21[4]}")
            print(f"Actions Taken: {run21[5]}")
            print(f"Initial Portfolio Equity: ${run21[6]}")
            print(f"Final Portfolio Equity: ${run21[7]}")
            print(f"Error Message: {run21[9]}")
            print(f"\nActions Summary:")
            print(run21[8])
            
            # Get transactions
            print("\n\nTransactions from this job:")
            result = session.execute(text(
                'SELECT id, symbol, direction, quantity, entry_price, account_balance FROM "transaction" WHERE smart_risk_manager_job_id = 21'
            ))
            transactions = result.fetchall()
            
            if transactions:
                print(f"Total transactions: {len(transactions)}")
                for tx in transactions:
                    tx_id, symbol, direction, qty, price, bal = tx
                    notional = qty * price
                    print(f"  {direction:6} {symbol:6} x{qty:3} @ ${price:8.2f} = ${notional:10.2f}")
            else:
                print("No transactions found")
        else:
            print("Run #21 not found")

if __name__ == "__main__":
    investigate_runs()

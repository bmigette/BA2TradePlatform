#!/usr/bin/env python3
"""Check available balance vs virtual equity for run #22."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from sqlalchemy import text

def check_balances():
    """Check account balance and position details at run time."""
    
    with get_db() as session:
        print("=" * 100)
        print("CHECKING ACCOUNT BALANCE VS VIRTUAL EQUITY FOR RUN #22")
        print("=" * 100)
        
        # Get all open transactions before run #22 to understand portfolio state
        print("\n" + "=" * 100)
        print("TRANSACTIONS BEFORE RUN #22 (for context)")
        print("=" * 100)
        
        result = session.execute(text(
            'SELECT id, symbol, quantity, open_price, close_price, status FROM "transaction" '
            'WHERE id < 320 ORDER BY id DESC LIMIT 20'
        ))
        
        rows = result.fetchall()
        print(f"\n{'ID':<5} {'Symbol':<8} {'Qty':<8} {'Open Price':<12} {'Close Price':<12} {'Status':<12}")
        print("-" * 60)
        
        total_open_value = 0
        for row in rows:
            tx_id, symbol, qty, open_price, close_price, status = row
            if qty and open_price:
                value = qty * open_price
                total_open_value += value
                close_str = f"{close_price:.2f}" if close_price else "None"
                print(f"{tx_id:<5} {symbol:<8} {qty:<8.1f} {open_price:<12.2f} {close_str:<12} {status:<12}")
        
        # Get the smart risk manager job details
        print("\n" + "=" * 100)
        print("RUN #22 PORTFOLIO STATE")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT initial_portfolio_equity, final_portfolio_equity FROM smartriskmanagerjob WHERE id = 22"
        ))
        
        row = result.fetchone()
        if row:
            initial_equity, final_equity = row
            print(f"\nInitial Virtual Equity: ${initial_equity:,.2f}")
            print(f"Final Virtual Equity: ${final_equity:,.2f}")
            print(f"Change: ${final_equity - initial_equity:,.2f}")
        
        # Now let's check the account's last_known_balance
        print("\n" + "=" * 100)
        print("ACCOUNT DEFINITION")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT id, account_name, last_known_balance FROM accountdefinition WHERE id = 1"
        ))
        
        row = result.fetchone()
        if row:
            acc_id, acc_name, last_balance = row
            print(f"\nAccount ID: {acc_id}")
            print(f"Account Name: {acc_name}")
            print(f"Last Known Balance: ${last_balance:,.2f}")
        
        # Check if there's a way to calculate available balance
        # Available balance = Last known balance - (sum of open position values)
        print("\n" + "=" * 100)
        print("AVAILABLE BALANCE CALCULATION")
        print("=" * 100)
        
        # Get all open positions (status != 'CLOSED')
        result = session.execute(text(
            'SELECT id, symbol, quantity, open_price, status FROM "transaction" '
            'WHERE status != \'CLOSED\' AND id < 320 ORDER BY id'
        ))
        
        rows = result.fetchall()
        
        if rows:
            total_open_value = 0
            print(f"\n{'ID':<5} {'Symbol':<8} {'Qty':<8} {'Price':<10} {'Value':<12}")
            print("-" * 50)
            for row in rows:
                tx_id, symbol, qty, price, status = row
                if qty and price:
                    value = qty * price
                    total_open_value += value
                    print(f"{tx_id:<5} {symbol:<8} {qty:<8.1f} ${price:<9.2f} ${value:<11.2f}")
            
            print(f"\nTotal Open Position Value: ${total_open_value:,.2f}")
            
            if last_balance:
                available = last_balance - total_open_value
                print(f"Last Known Balance: ${last_balance:,.2f}")
                print(f"Available Balance: ${available:,.2f}")
                print(f"Utilization: {(total_open_value / last_balance * 100):.1f}%")
        
        # Now check the settings for the expert
        print("\n" + "=" * 100)
        print("EXPERT SETTINGS FOR TA-DYNAMIC-GROK (Expert ID 11)")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT settings FROM expertinstance WHERE id = 11"
        ))
        
        row = result.fetchone()
        if row:
            import json
            settings_json = row[0]
            try:
                settings = json.loads(settings_json)
                print("\nExpert Settings:")
                print(json.dumps(settings, indent=2))
            except:
                print(f"Settings (raw): {settings_json}")

if __name__ == "__main__":
    check_balances()

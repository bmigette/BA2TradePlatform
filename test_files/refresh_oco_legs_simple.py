#!/usr/bin/env python
"""
Account refresh script to populate OCO leg orders.
This script triggers account.refresh_orders() which should:
1. Fetch current orders from Alpaca
2. Detect OCO orders 
3. Insert leg orders into database with parent_order_id linkage
"""

import logging
from ba2_trade_platform.core.models import AccountInstance, TradingOrder
from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.logger import logger
from sqlalchemy import select, text
from sqlalchemy.orm import Session

try:
    print("\n" + "=" * 80)
    print("ACCOUNT REFRESH - OCO LEG POPULATION (Simple)")
    print("=" * 80)
    
    # Get account
    with Session(get_db().bind) as session:
        account_def = session.exec(select(AccountInstance)).first()
        if not account_def:
            print("[ERROR] No account found in database")
            exit(1)
    
    print(f"\nFound account: {account_def.name} (ID: {account_def.id})")
    
    # Get account instance
    account = get_instance(AccountInstance, account_def.id)
    print(f"Account type: {account.__class__.__name__}")
    
    # Count OCO orders before
    print("\n[BEFORE REFRESH]")
    with Session(get_db().bind) as session:
        oco_count_before = len(session.execute(text("SELECT 1 FROM tradingorder WHERE order_type = 'OCO'")).fetchall())
        legs_count_before = len(session.execute(text("SELECT 1 FROM tradingorder WHERE parent_order_id IS NOT NULL")).fetchall())
        oco_with_legs_before = len(session.execute(text("SELECT 1 FROM tradingorder WHERE order_type = 'OCO' AND legs_broker_ids IS NOT NULL")).fetchall())
    
    print(f"  Total OCO orders: {oco_count_before}")
    print(f"  Leg orders inserted: {legs_count_before}")
    print(f"  OCO orders with legs_broker_ids: {oco_with_legs_before}")
    
    # Trigger refresh
    print("\n[RUNNING REFRESH...]")
    success = account.refresh_orders(fetch_all=True, heuristic_mapping=False)
    print(f"Refresh result: {'SUCCESS' if success else 'FAILED'}")
    
    # Count OCO orders after
    print("\n[AFTER REFRESH]")
    with Session(get_db().bind) as session:
        oco_count_after = len(session.execute(text("SELECT 1 FROM tradingorder WHERE order_type = 'OCO'")).fetchall())
        legs_count_after = len(session.execute(text("SELECT 1 FROM tradingorder WHERE parent_order_id IS NOT NULL")).fetchall())
        oco_with_legs_after = len(session.execute(text("SELECT 1 FROM tradingorder WHERE order_type = 'OCO' AND legs_broker_ids IS NOT NULL")).fetchall())
    
    print(f"  Total OCO orders: {oco_count_after}")
    print(f"  Leg orders inserted: {legs_count_after}")
    print(f"  OCO orders with legs_broker_ids: {oco_with_legs_after}")
    
    print("\n[RESULTS]")
    legs_inserted = legs_count_after - legs_count_before
    print(f"  New leg orders inserted: {legs_inserted}")
    
    if legs_inserted > 0:
        print("\n[SUCCESS] Legs were inserted!")
        with Session(get_db().bind) as session:
            legs = session.execute(text("SELECT id, broker_order_id, symbol, order_type, status FROM tradingorder WHERE parent_order_id IS NOT NULL LIMIT 5")).fetchall()
            print(f"  Sample legs: {len(legs)} found")
            for leg in legs:
                leg_id, broker_id, symbol, order_type, status = leg
                print(f"    Leg {leg_id}: {symbol} ({order_type}) - {status}")
    else:
        print("\n[WARNING] No new leg orders were inserted")
        
        # Check if any OCO orders have order_class=OCO
        with Session(get_db().bind) as session:
            # Check what order_class values are in the latest refresh log
            result = session.execute(text("""
                SELECT COUNT(*), order_type 
                FROM tradingorder 
                GROUP BY order_type 
                ORDER BY COUNT(*) DESC
            """)).fetchall()
            print(f"\n  Order type distribution:")
            for count, order_type in result:
                print(f"    {order_type}: {count}")
    
    print("\n" + "=" * 80)
    print("REFRESH COMPLETED")
    print("=" * 80)
    
except Exception as e:
    print(f"\n[ERROR] Error during refresh: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

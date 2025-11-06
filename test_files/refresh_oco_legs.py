#!/usr/bin/env python3
"""Trigger account refresh to populate OCO leg orders"""

import sys
sys.path.insert(0, '.')

from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import AccountDefinition, TradingOrder
from sqlmodel import select
import logging

# Setup logging
logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

print("=" * 80)
print("ACCOUNT REFRESH - OCO LEG POPULATION")
print("=" * 80)

try:
    # Get the Alpaca account
    session = get_db()
    accounts = session.exec(select(AccountDefinition)).all()
    
    if not accounts:
        print("❌ No accounts found in database")
        sys.exit(1)
    
    # Get the first account (assuming single account setup)
    account_def = accounts[0]
    print(f"\n✅ Found account: {account_def.name} (ID: {account_def.id})")
    
    # Get account instance from cache
    from ba2_trade_platform.core.utils import get_account_instance_from_id
    account = get_account_instance_from_id(account_def.id)
    
    if not account:
        print(f"❌ Could not instantiate account {account_def.id}")
        sys.exit(1)
    
    print(f"✅ Account instance created: {account.__class__.__name__}")
    
    # Get current OCO orders before refresh
    oco_before = session.exec(select(TradingOrder).where(TradingOrder.order_type == 'OCO')).all()
    legs_before = session.exec(select(TradingOrder).where(TradingOrder.parent_order_id != None)).all()
    
    print(f"\n📊 Before refresh:")
    print(f"   - Total OCO orders: {len(oco_before)}")
    print(f"   - Leg orders inserted: {len(legs_before)}")
    print(f"   - OCO orders with legs_broker_ids: {len([o for o in oco_before if o.legs_broker_ids])}")
    
    # Trigger refresh_orders
    print(f"\n🔄 Triggering account refresh...")
    account.refresh_orders()
    print(f"✅ Refresh completed")
    
    # Get updated counts
    session = get_db()  # Get fresh session
    oco_after = session.exec(select(TradingOrder).where(TradingOrder.order_type == 'OCO')).all()
    legs_after = session.exec(select(TradingOrder).where(TradingOrder.parent_order_id != None)).all()
    with_legs_broker_ids = [o for o in oco_after if o.legs_broker_ids]
    
    print(f"\n📊 After refresh:")
    print(f"   - Total OCO orders: {len(oco_after)}")
    print(f"   - Leg orders inserted: {len(legs_after)}")
    print(f"   - OCO orders with legs_broker_ids: {len(with_legs_broker_ids)}")
    
    # Show details
    print(f"\n📋 New leg orders inserted: {len(legs_after) - len(legs_before)}")
    
    if len(legs_after) > len(legs_before):
        print("\n✅ Leg Orders Created:")
        new_legs = legs_after[-min(5, len(legs_after) - len(legs_before)):]
        for leg in new_legs:
            parent = session.get(TradingOrder, leg.parent_order_id)
            if parent:
                print(f"   - Leg Order {leg.id}: {leg.symbol} {leg.order_type} -> Parent Order {parent.id} ({parent.symbol})")
    
    if len(with_legs_broker_ids) > 0:
        print("\n✅ OCO Orders with legs_broker_ids:")
        for oco in with_legs_broker_ids[:3]:
            print(f"   - OCO Order {oco.id}: {oco.symbol} ({len(oco.legs_broker_ids) if oco.legs_broker_ids else 0} legs)")
    
    print("\n" + "=" * 80)
    print("✅ REFRESH COMPLETED SUCCESSFULLY")
    print("=" * 80)

except Exception as e:
    logger.error(f"Error during refresh: {e}", exc_info=True)
    print(f"\n❌ Error: {e}")
    sys.exit(1)

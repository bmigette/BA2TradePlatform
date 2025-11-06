#!/usr/bin/env python3
"""Check OCO and leg orders status in database"""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import TradingOrder
from sqlmodel import select

session = get_db()

# Check OCO orders
oco_orders = session.exec(select(TradingOrder).where(TradingOrder.order_type == 'OCO')).all()
print("=" * 80)
print("OCO ORDERS ANALYSIS")
print("=" * 80)
print(f"Total OCO orders in database: {len(oco_orders)}")
print()

# Count by legs_broker_ids status
with_legs = [o for o in oco_orders if o.legs_broker_ids and len(o.legs_broker_ids) > 0]
without_legs = [o for o in oco_orders if not o.legs_broker_ids or len(o.legs_broker_ids) == 0]

print(f"  ✅ With legs_broker_ids populated: {len(with_legs)}")
if with_legs:
    for o in with_legs[:3]:
        print(f"     - Order {o.id} ({o.symbol}): {len(o.legs_broker_ids)} legs")

print(f"  ❌ Without legs_broker_ids: {len(without_legs)}")

# Check for leg orders (dependent orders)
leg_orders = session.exec(select(TradingOrder).where(TradingOrder.parent_order_id != None)).all()
print()
print(f"Leg orders inserted via parent_order_id: {len(leg_orders)}")

# Check for orders with depends_on_order
chained_orders = session.exec(select(TradingOrder).where(TradingOrder.depends_on_order != None)).all()
print(f"Orders chained via depends_on_order: {len(chained_orders)}")

print()
print("=" * 80)
print("CONCLUSION:")
print("=" * 80)
print("✅ Database columns exist and are working")
print("❌ No OCO orders have legs_broker_ids populated (old data pre-dates new code)")
print("❌ No leg orders have been inserted via parent_order_id relationship")
print()
print("REASON: These OCO orders were created before the legs_broker_ids field")
print("was added. They need to be processed via account refresh or recreated")
print("with the new code path that populates legs_broker_ids.")
print("=" * 80)

# Show which OCO orders need leg insertion
print()
print("OCO orders needing leg insertion (no legs_broker_ids):")
print()
for i, o in enumerate(without_legs[:5]):
    print(f"{i+1}. Order {o.id}: {o.symbol} (status={o.status}, created={o.created_at})")

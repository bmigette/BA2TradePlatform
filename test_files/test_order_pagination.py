"""
Test Alpaca order pagination with fetch_all=True
"""
import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.db import get_all_instances
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

# Get first Alpaca account
accounts = get_all_instances(AccountDefinition)
alpaca_account = None

for account_def in accounts:
    if account_def.provider == "AlpacaAccount":
        alpaca_account = AlpacaAccount(account_def.id)
        print(f"Found Alpaca account: {account_def.name} (ID: {account_def.id})")
        break

if not alpaca_account:
    print("No Alpaca account found")
    sys.exit(1)

print("\n" + "="*80)
print("Testing order fetch with fetch_all=False (first 500 orders only)")
print("="*80)

orders_limited = alpaca_account.get_orders(fetch_all=False)
print(f"Fetched {len(orders_limited)} orders with fetch_all=False")

print("\n" + "="*80)
print("Testing order fetch with fetch_all=True (all orders with pagination)")
print("="*80)

orders_all = alpaca_account.get_orders(fetch_all=True)
print(f"\nFetched {len(orders_all)} total unique orders with fetch_all=True")

# Show some statistics
if orders_all:
    print(f"\nFirst order broker_order_id: {orders_all[0].broker_order_id}")
    print(f"Last order broker_order_id: {orders_all[-1].broker_order_id}")
    
    # Check for duplicates
    broker_ids = [o.broker_order_id for o in orders_all if o.broker_order_id]
    unique_ids = set(broker_ids)
    
    print(f"\nTotal orders: {len(orders_all)}")
    print(f"Orders with broker_order_id: {len(broker_ids)}")
    print(f"Unique broker_order_ids: {len(unique_ids)}")
    
    if len(broker_ids) != len(unique_ids):
        print(f"⚠️  WARNING: Found {len(broker_ids) - len(unique_ids)} duplicate orders!")
    else:
        print("✅ No duplicate orders found")

print("\n" + "="*80)
print("Test complete")
print("="*80)

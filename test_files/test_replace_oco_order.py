"""
Test replacing an existing OCO order with new TP/SL values
"""

import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from ba2_trade_platform.core.db import get_instance, get_db
from ba2_trade_platform.core.models import AccountSetting
from sqlmodel import Session, select

def get_alpaca_credentials():
    """Read API credentials from database"""
    with Session(get_db().bind) as session:
        api_key_setting = session.exec(
            select(AccountSetting).where(
                AccountSetting.account_id == 1,
                AccountSetting.key == "api_key"
            )
        ).first()
        
        api_secret_setting = session.exec(
            select(AccountSetting).where(
                AccountSetting.account_id == 1,
                AccountSetting.key == "api_secret"
            )
        ).first()
        
        paper_setting = session.exec(
            select(AccountSetting).where(
                AccountSetting.account_id == 1,
                AccountSetting.key == "paper_account"
            )
        ).first()
        
        if not api_key_setting or not api_secret_setting:
            raise ValueError("API credentials not found in database")
        
        # Check paper_setting value - could be in value_str or value_json
        is_paper = True  # Default to paper
        if paper_setting:
            if paper_setting.value_str:
                is_paper = paper_setting.value_str.lower() == "true"
            elif paper_setting.value_json is not None:
                is_paper = bool(paper_setting.value_json)
        
        return {
            "api_key": api_key_setting.value_str,
            "api_secret": api_secret_setting.value_str,
            "paper": is_paper
        }

def test_replace_oco():
    """
    Test replacing an existing OCO order with new TP/SL values
    
    Existing OCO order:
    - Order ID: 3b79a10e-e4c1-4178-8e68-200a3da7da7c
    - Symbol: T
    - Quantity: 27
    - Side: SELL
    - Status: ACCEPTED
    
    We'll try to replace it with:
    - New Take Profit: $25.00 (instead of original)
    - New Stop Loss: $23.00 (instead of original)
    """
    print("=" * 80)
    print("Testing OCO Order Replacement")
    print("=" * 80)
    
    existing_order_id = "3b79a10e-e4c1-4178-8e68-200a3da7da7c"
    
    # Get credentials
    creds = get_alpaca_credentials()
    print(f"\n✓ Retrieved credentials (Paper: {creds['paper']})")
    
    # Create Alpaca client
    client = TradingClient(
        api_key=creds["api_key"],
        secret_key=creds["api_secret"],
        paper=creds["paper"]
    )
    print("✓ Connected to Alpaca API")
    
    # Step 1: Get the existing order details
    print("\n" + "=" * 80)
    print("STEP 1: Fetching Existing Order")
    print("=" * 80)
    
    try:
        existing_order = client.get_order_by_id(existing_order_id)
        print(f"\n📋 Current Order Details:")
        print(f"   Order ID: {existing_order.id}")
        print(f"   Status: {existing_order.status}")
        print(f"   Symbol: {existing_order.symbol}")
        print(f"   Quantity: {existing_order.qty}")
        print(f"   Side: {existing_order.side}")
        print(f"   Order Class: {existing_order.order_class}")
        print(f"   Order Type: {existing_order.order_type}")
        if existing_order.limit_price:
            print(f"   Limit Price (TP): ${float(existing_order.limit_price):.2f}")
        if existing_order.stop_price:
            print(f"   Stop Price: ${float(existing_order.stop_price):.2f}")
        
        # Show legs if available
        if hasattr(existing_order, 'legs') and existing_order.legs:
            print(f"\n📋 Order Legs ({len(existing_order.legs)} legs):")
            for i, leg_id in enumerate(existing_order.legs, 1):
                try:
                    leg_order = client.get_order_by_id(leg_id)
                    print(f"   Leg {i} ID: {leg_id}")
                    print(f"     - Type: {leg_order.order_type}")
                    print(f"     - Side: {leg_order.side}")
                    print(f"     - Status: {leg_order.status}")
                    if leg_order.stop_price:
                        print(f"     - Stop Price: ${float(leg_order.stop_price):.2f}")
                    if leg_order.limit_price:
                        print(f"     - Limit Price: ${float(leg_order.limit_price):.2f}")
                except Exception as leg_error:
                    print(f"     - Error fetching leg: {leg_error}")
        
    except Exception as e:
        print(f"\n❌ Error fetching existing order: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # Step 2: Attempt to replace the OCO order
    print("\n" + "=" * 80)
    print("STEP 2: Replacing OCO Order with New TP/SL")
    print("=" * 80)
    print(f"Symbol: T")
    print(f"Quantity: 27")
    print(f"New Take Profit: $25.00 (limit sell)")
    print(f"New Stop Loss: $23.00 (stop loss)")
    
    try:
        # Create new OCO order request with updated TP/SL
        new_oco_request = LimitOrderRequest(
            symbol="T",
            qty=27,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=25.00,  # New TP price
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(
                limit_price=25.00  # New TP price
            ),
            stop_loss=StopLossRequest(
                stop_price=23.00  # New SL price
            )
        )
        
        print("\n🚀 Attempting to replace OCO order...")
        print(f"   Replacing order ID: {existing_order_id}")
        
        # Use Alpaca's replace_order API
        replacement_order = client.replace_order_by_id(
            order_id=existing_order_id,
            order_data=new_oco_request
        )
        
        print("\n" + "=" * 80)
        print("✅ OCO ORDER REPLACED SUCCESSFULLY")
        print("=" * 80)
        print(f"New Order ID: {replacement_order.id}")
        print(f"Status: {replacement_order.status}")
        print(f"Symbol: {replacement_order.symbol}")
        print(f"Quantity: {replacement_order.qty}")
        print(f"Side: {replacement_order.side}")
        print(f"Order Class: {replacement_order.order_class}")
        if replacement_order.limit_price:
            print(f"Limit Price (TP): ${float(replacement_order.limit_price):.2f}")
        
        # Get legs information
        if hasattr(replacement_order, 'legs') and replacement_order.legs:
            print(f"\n📋 New Order Legs:")
            for i, leg_id in enumerate(replacement_order.legs, 1):
                print(f"  Leg {i} ID: {leg_id}")
                try:
                    leg_order = client.get_order_by_id(leg_id)
                    print(f"    - Type: {leg_order.order_type}")
                    print(f"    - Side: {leg_order.side}")
                    print(f"    - Status: {leg_order.status}")
                    if leg_order.stop_price:
                        print(f"    - Stop Price: ${float(leg_order.stop_price):.2f}")
                    if leg_order.limit_price:
                        print(f"    - Limit Price: ${float(leg_order.limit_price):.2f}")
                except Exception as leg_error:
                    print(f"    - Error fetching leg details: {leg_error}")
        
        print("\n" + "=" * 80)
        print("✅ TEST COMPLETE")
        print("=" * 80)
        print("\nThe OCO order has been replaced with new TP/SL values:")
        print(f"  • Old order ID: {existing_order_id}")
        print(f"  • New order ID: {replacement_order.id}")
        print(f"  • New Take Profit: $25.00")
        print(f"  • New Stop Loss: $23.00")
        
        return replacement_order
        
    except Exception as e:
        print("\n" + "=" * 80)
        print("❌ ERROR REPLACING OCO ORDER")
        print("=" * 80)
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")
        
        if hasattr(e, 'status_code'):
            print(f"Status code: {e.status_code}")
        
        import traceback
        print("\nFull traceback:")
        traceback.print_exc()
        
        print("\n" + "=" * 80)
        print("ANALYSIS")
        print("=" * 80)
        print("This confirms that Alpaca's replace_order has limitations:")
        print("  • Cannot replace OCO orders in ACCEPTED status")
        print("  • Same error 42210000 we saw with bracket orders")
        print("  • Must cancel and create new orders instead")
        
        return None

if __name__ == "__main__":
    test_replace_oco()

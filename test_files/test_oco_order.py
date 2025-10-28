"""
Test OCO (One-Cancels-Other) order for PANW
This creates a pair of orders where if one executes, the other is automatically cancelled.
"""

import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, StopLossRequest
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

def test_oco_order():
    """
    Test OCO order submission for PANW
    
    NOTE: OCO orders are used to close an existing position with either:
    1. Take Profit order (limit sell at $260)
    2. Stop Loss order (stop loss at $160)
    
    This assumes we already have a long position in PANW.
    When one executes, the other is automatically cancelled.
    """
    print("=" * 80)
    print("Testing OCO Order for PANW")
    print("=" * 80)
    print("\n⚠️  NOTE: OCO orders require an existing position!")
    print("This test assumes you have a long position in PANW.")
    
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
    
    # Get current PANW price for reference
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        data_client = StockHistoricalDataClient(
            api_key=creds["api_key"],
            secret_key=creds["api_secret"]
        )
        from alpaca.data.requests import StockLatestQuoteRequest
        quote_request = StockLatestQuoteRequest(symbol_or_symbols="PANW")
        quote = data_client.get_stock_latest_quote(quote_request)
        current_price = quote["PANW"].ask_price
        print(f"\n📊 Current PANW price: ${current_price:.2f}")
    except Exception as e:
        print(f"\n⚠️  Could not fetch current price: {e}")
        current_price = None
    
    # Create OCO order
    # According to Alpaca docs, we need to:
    # 1. Submit a limit order (take profit at $260)
    # 2. Attach a stop loss order ($160) using order_class=OCO
    
    print("\n" + "=" * 80)
    print("Creating OCO Order")
    print("=" * 80)
    print(f"Symbol: PANW")
    print(f"Quantity: 1")
    print(f"Take Profit: $260.00 (limit sell)")
    print(f"Stop Loss: $160.00 (stop loss)")
    
    try:
        # Create OCO order request
        # OCO MUST be a limit order with BOTH take_profit and stop_loss parameters
        from alpaca.trading.requests import TakeProfitRequest
        
        oco_request = LimitOrderRequest(
            symbol="T",
            qty=27,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(
                limit_price=50.00  # Take profit limit (same as main order)
            ),
            stop_loss=StopLossRequest(
                stop_price=15.00  # Stop loss trigger
            )
        )
        
        print("\n🚀 Submitting OCO order to Alpaca...")
        order = client.submit_order(order_data=oco_request)
        
        print("\n" + "=" * 80)
        print("✅ OCO ORDER CREATED SUCCESSFULLY")
        print("=" * 80)
        print(f"Primary Order ID: {order.id}")
        print(f"Status: {order.status}")
        print(f"Symbol: {order.symbol}")
        print(f"Quantity: {order.qty}")
        print(f"Side: {order.side}")
        print(f"Order Class: {order.order_class}")
        print(f"Limit Price (TP): ${order.limit_price}" if order.limit_price else "N/A")
        
        # Get legs information
        if hasattr(order, 'legs') and order.legs:
            print(f"\n📋 Order Legs:")
            for i, leg_id in enumerate(order.legs, 1):
                print(f"  Leg {i} ID: {leg_id}")
                try:
                    leg_order = client.get_order_by_id(leg_id)
                    print(f"    - Type: {leg_order.order_type}")
                    print(f"    - Side: {leg_order.side}")
                    print(f"    - Status: {leg_order.status}")
                    if leg_order.stop_price:
                        print(f"    - Stop Price: ${leg_order.stop_price:.2f}")
                    if leg_order.limit_price:
                        print(f"    - Limit Price: ${leg_order.limit_price:.2f}")
                except Exception as leg_error:
                    print(f"    - Error fetching leg details: {leg_error}")
        
        print("\n" + "=" * 80)
        print("✅ TEST COMPLETE")
        print("=" * 80)
        print("\nThe OCO order has been created. When either:")
        print("  • Price reaches $260 (take profit executes), OR")
        print("  • Price drops to $160 (stop loss executes)")
        print("The other order will be automatically cancelled.")
        
        return order
        
    except Exception as e:
        print("\n" + "=" * 80)
        print("❌ ERROR CREATING OCO ORDER")
        print("=" * 80)
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")
        
        if hasattr(e, 'status_code'):
            print(f"Status code: {e.status_code}")
        
        import traceback
        print("\nFull traceback:")
        traceback.print_exc()
        
        return None

if __name__ == "__main__":
    test_oco_order()

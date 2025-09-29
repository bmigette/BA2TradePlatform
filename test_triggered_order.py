#!/usr/bin/env python3
"""
Test script to create a triggered order scenario:
1. Buy 2 shares of MSFT at market price
2. Sell 2 shares of MSFT at limit price when the buy order is fulfilled
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from datetime import datetime, timezone

def create_triggered_order_test(submit_orders=False):
    """
    Create a test scenario with a buy order and a dependent sell order.
    
    Args:
        submit_orders: If True, actually submit the buy order to the broker. 
                      If False, just create the orders in the database.
    """
    print("Creating triggered order test...")
    print(f"Submit orders to broker: {submit_orders}")

    # Create an Alpaca account instance (assuming account ID 1 exists)
    account_id = 1  # This should be a valid account ID from your database
    account = AlpacaAccount(account_id)

    # Get current MSFT price
    symbol = "MSFT"
    current_price = account.get_instrument_current_price(symbol)

    if not current_price:
        print(f"Could not get current price for {symbol}")
        return

    print(f"Current {symbol} price: ${current_price:.2f}")

    # Create the buy order (market order for 2 shares)
    buy_order = TradingOrder(
        symbol=symbol,
        quantity=2.0,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        good_for="day",
        status=OrderStatus.PENDING,
        comment=f"Test buy order for {symbol} - 2 shares at market",
        open_type="manual",
        created_at=datetime.now(timezone.utc)
    )

    # Save the buy order to database
    buy_order_id = add_instance(buy_order)
    if not buy_order_id:
        print("Failed to save buy order to database")
        return

    print(f"Created buy order with ID: {buy_order_id}")

    # Set a limit price slightly above current price for the sell order
    # This simulates a take-profit scenario
    limit_price = current_price * 1.05  # 5% above current price

    # Create the dependent sell order (limit order that triggers when buy is fulfilled)
    sell_order = TradingOrder(
        symbol=symbol,
        quantity=2.0,
        side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT,
        good_for="gtc",  # Good till canceled
        status=OrderStatus.WAITING_TRIGGER,
        limit_price=limit_price,
        comment=f"Test sell order for {symbol} - triggered when buy order {buy_order_id} is fulfilled",
        open_type="manual",
        depends_on_order=buy_order_id,
        depends_order_status_trigger=OrderStatus.FULFILLED,
        created_at=datetime.now(timezone.utc)
    )

    # Save the sell order to database
    sell_order_id = add_instance(sell_order)
    if not sell_order_id:
        print("Failed to save sell order to database")
        return

    print(f"Created dependent sell order with ID: {sell_order_id}")
    print(f"Sell order will trigger when buy order {buy_order_id} reaches status: {OrderStatus.FULFILLED}")
    print(f"Sell limit price: ${limit_price:.2f}")

    # Submit the buy order to the broker if requested
    if submit_orders:
        print("\nSubmitting buy order to broker...")
        submitted_buy_order = account.submit_order(buy_order)

        if submitted_buy_order:
            print(f"Buy order submitted successfully. Broker order ID: {submitted_buy_order.broker_order_id}")
            print("The sell order will be automatically submitted when the buy order is fulfilled.")
            print("Monitor the TradeManager logs to see when the dependent order is triggered.")
        else:
            print("Failed to submit buy order to broker")
    else:
        print("\nBuy order NOT submitted to broker (use submit_orders=True to actually submit)")
        print("To test manually:")
        print("1. Submit the buy order manually through the UI or another script")
        print("2. Update the buy order status to FULFILLED in the database")
        print("3. Run the TradeManager refresh to trigger the dependent sell order")

    return buy_order_id, sell_order_id

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Create a test triggered order scenario')
    parser.add_argument('--submit', action='store_true', 
                       help='Actually submit the buy order to the broker (default: False)', default=True)
    
    args = parser.parse_args()
    
    try:
        result = create_triggered_order_test(submit_orders=args.submit)
        if result:
            buy_id, sell_id = result
            print("\nTest setup complete!")
            print(f"Buy Order ID: {buy_id}")
            print(f"Dependent Sell Order ID: {sell_id}")
            
            if args.submit:
                print("\nOrders have been submitted to the broker.")
                print("Monitor your Alpaca account and the TradeManager logs.")
            else:
                print("\nNext steps:")
                print("1. Review the orders in the database")
                print("2. Submit the buy order manually or run with --submit flag")
                print("3. When buy order is fulfilled, the sell order will auto-trigger")
                print("4. Run: python test_triggered_order.py --submit")
        else:
            print("Test setup failed!")
    except Exception as e:
        print(f"Error running test: {e}")
        import traceback
        traceback.print_exc()

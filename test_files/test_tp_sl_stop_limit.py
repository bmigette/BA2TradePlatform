"""
Test script for TP/SL and STOP_LIMIT order functionality.

This script tests:
1. Opening a market order on AAPL
2. Setting TP only
3. Setting SL only
4. Setting both TP and SL (STOP_LIMIT order)
5. Closing all orders

Run with: .venv\Scripts\python.exe test_files\test_tp_sl_stop_limit.py
"""

import sys
import os
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus, OrderOpenType
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_instance, add_instance
from ba2_trade_platform.logger import logger

def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")

def test_tp_sl_stop_limit():
    """Test TP/SL and STOP_LIMIT order functionality."""
    
    print_section("TP/SL and STOP_LIMIT Order Test")
    
    # Get the first Alpaca account (assuming it exists)
    try:
        account = AlpacaAccount(1)
        print(f"✓ Connected to Alpaca account {account.id}")
    except Exception as e:
        print(f"✗ Failed to connect to Alpaca account: {e}")
        return
    
    # Get current PANW price
    symbol = "PANW"  # Use Palo Alto Networks stock for testing
    print(f"\nFetching current {symbol} price...")
    current_price = account.get_instrument_current_price(symbol)
    
    if not current_price:
        print(f"✗ Failed to get current price for {symbol}")
        return
    
    print(f"✓ Current {symbol} price: ${current_price:.2f}")
    
    # Calculate TP and SL prices (5% away from current price)
    tp_price = current_price * 1.05  # 5% above
    sl_price = current_price * 0.95  # 5% below
    
    print(f"  Take Profit price: ${tp_price:.2f} (+5%)")
    print(f"  Stop Loss price: ${sl_price:.2f} (-5%)")
    
    order_ids = []
    
    # Test 1: Open market order with TP only
    print_section("Test 1: Market Order with TP Only")
    try:
        order1 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 1: Market order with TP only"
        )
        
        submitted_order1 = account.submit_order(order1, tp_price=tp_price)
        
        if submitted_order1:
            order_ids.append(submitted_order1.id)
            print(f"✓ Order 1 submitted: ID={submitted_order1.id}, broker_order_id={submitted_order1.broker_order_id}")
            print(f"  Status: {submitted_order1.status}")
            print(f"  TP price: ${tp_price:.2f}")
        else:
            print(f"✗ Order 1 failed to submit")
            
    except Exception as e:
        print(f"✗ Test 1 failed: {e}")
        logger.error(f"Test 1 failed", exc_info=True)
    
    # Cancel Test 1's TP order before Test 2 to avoid wash trade detection
    print("\nCanceling Test 1's TP order to avoid wash trade detection...")
    if order_ids:
        try:
            # Refresh to get latest order states
            account.refresh_orders()
            
            # Find and cancel TP orders from Test 1
            with Session(get_db().bind) as session:
                test1_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == 1,
                        TradingOrder.transaction_id == (submitted_order1.transaction_id if submitted_order1 else None),
                        TradingOrder.order_type.in_([OrderType.SELL_LIMIT, OrderType.BUY_LIMIT])
                    )
                ).all()
                
                for order in test1_orders:
                    if order.broker_order_id and order.status not in [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED]:
                        account.cancel_order(order.broker_order_id)
                        print(f"✓ Canceled Test 1 TP order {order.id} (broker_order_id={order.broker_order_id})")
        except Exception as e:
            print(f"✗ Failed to cancel Test 1 orders: {e}")
    
    # Test 2: Open market order with SL only
    print_section("Test 2: Market Order with SL Only")
    try:
        order2 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 2: Market order with SL only"
        )
        
        submitted_order2 = account.submit_order(order2, sl_price=sl_price)
        
        if submitted_order2:
            order_ids.append(submitted_order2.id)
            print(f"✓ Order 2 submitted: ID={submitted_order2.id}, broker_order_id={submitted_order2.broker_order_id}")
            print(f"  Status: {submitted_order2.status}")
            print(f"  SL price: ${sl_price:.2f}")
        else:
            print(f"✗ Order 2 failed to submit")
            
    except Exception as e:
        print(f"✗ Test 2 failed: {e}")
        logger.error(f"Test 2 failed", exc_info=True)
    
    # Test 3: Open market order with both TP and SL (STOP_LIMIT)
    print_section("Test 3: Market Order with TP + SL (STOP_LIMIT)")
    try:
        order3 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 3: Market order with TP+SL"
        )
        
        submitted_order3 = account.submit_order(order3, tp_price=tp_price, sl_price=sl_price)
        
        if submitted_order3:
            order_ids.append(submitted_order3.id)
            print(f"✓ Order 3 submitted: ID={submitted_order3.id}, broker_order_id={submitted_order3.broker_order_id}")
            print(f"  Status: {submitted_order3.status}")
            print(f"  TP price: ${tp_price:.2f}")
            print(f"  SL price: ${sl_price:.2f}")
            print(f"  Expected: STOP_LIMIT order should be created at Alpaca")
        else:
            print(f"✗ Order 3 failed to submit")
            
    except Exception as e:
        print(f"✗ Test 3 failed: {e}")
        logger.error(f"Test 3 failed", exc_info=True)
    
    # Wait a moment for orders to process
    import time
    print("\nWaiting 3 seconds for orders to process...")
    time.sleep(3)
    
    # Refresh orders from Alpaca
    print("\nRefreshing orders from Alpaca...")
    account.refresh_orders()
    print("✓ Orders refreshed")
    
    # Show all orders created
    print_section("Orders Created")
    for order_id in order_ids:
        order = get_instance(TradingOrder, order_id)
        if order:
            print(f"Order {order_id}:")
            print(f"  Symbol: {order.symbol}")
            print(f"  Side: {order.side}")
            print(f"  Type: {order.order_type}")
            print(f"  Status: {order.status}")
            print(f"  Broker Order ID: {order.broker_order_id}")
            print(f"  Comment: {order.comment}")
            
            # Check for associated TP/SL orders
            if order.transaction_id:
                from sqlmodel import Session, select
                from ba2_trade_platform.core.db import get_db
                
                with Session(get_db().bind) as session:
                    related_orders = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.transaction_id == order.transaction_id,
                            TradingOrder.id != order.id
                        )
                    ).all()
                    
                    if related_orders:
                        print(f"  Related TP/SL orders:")
                        for related in related_orders:
                            print(f"    - Order {related.id}: {related.order_type} (status: {related.status})")
            print()
    
    # Cancel all orders
    print_section("Canceling All Test Orders")
    
    # Get all orders for this account that are still active
    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    
    with Session(get_db().bind) as session:
        active_orders = session.exec(
            select(TradingOrder).where(
                TradingOrder.account_id == account.id,
                TradingOrder.symbol == symbol,
                TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
            )
        ).all()
        
        print(f"Found {len(active_orders)} active orders to cancel\n")
        
        for order in active_orders:
            if order.broker_order_id:
                try:
                    success = account.cancel_order(order.broker_order_id)
                    if success:
                        print(f"✓ Canceled order {order.id} (broker_order_id={order.broker_order_id})")
                    else:
                        print(f"✗ Failed to cancel order {order.id}")
                except Exception as e:
                    print(f"✗ Error canceling order {order.id}: {e}")
            else:
                print(f"⚠ Order {order.id} has no broker_order_id, skipping cancel")
    
    # Final refresh
    print("\nFinal refresh from Alpaca...")
    account.refresh_orders()
    print("✓ Final refresh complete")
    
    print_section("Test Complete")
    print("Summary:")
    print(f"  - Created {len(order_ids)} main orders")
    print(f"  - Test 1: Market order with TP only")
    print(f"  - Test 2: Market order with SL only")
    print(f"  - Test 3: Market order with TP+SL (STOP_LIMIT)")
    print(f"  - All orders canceled")
    print("\nCheck Alpaca dashboard to verify STOP_LIMIT orders were created correctly.")

if __name__ == "__main__":
    try:
        test_tp_sl_stop_limit()
    except Exception as e:
        print(f"\n✗ Test script failed: {e}")
        logger.error(f"Test script failed", exc_info=True)
        sys.exit(1)

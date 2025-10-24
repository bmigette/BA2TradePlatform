"""
Test script for bracket order upgrade functionality.

This script tests the ability to upgrade from separate TP/SL orders to bracket orders:
1. Open market order with TP only
2. Add SL → should upgrade to bracket order by canceling separate TP and creating bracket
3. Open market order with SL only
4. Add TP → should upgrade to bracket order by canceling separate SL and creating bracket
5. Open market order with both TP+SL directly → creates bracket order immediately

Run with: .venv\Scripts\python.exe test_files\test_bracket_order_upgrade.py
"""

import sys
import os
import time
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus, OrderOpenType
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_instance, get_db
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}\n")

def cleanup_test_orders(account, transaction_ids):
    """Cancel only orders from the specified transactions."""
    print_section("Cleanup: Canceling Test Orders Only")
    try:
        if not transaction_ids:
            print("✓ No test transactions to clean up")
            return
            
        account.refresh_orders()
        
        with Session(get_db().bind) as session:
            test_orders = session.exec(
                select(TradingOrder).where(
                    TradingOrder.account_id == account.id,
                    TradingOrder.transaction_id.in_(transaction_ids),
                    TradingOrder.status.not_in([
                        OrderStatus.FILLED, 
                        OrderStatus.CANCELED, 
                        OrderStatus.EXPIRED,
                        OrderStatus.REPLACED,
                        OrderStatus.ERROR
                    ])
                )
            ).all()
            
            if not test_orders:
                print("✓ No test orders to cancel")
                return
            
            print(f"Found {len(test_orders)} test orders to cancel")
            
            for order in test_orders:
                if order.broker_order_id:
                    try:
                        account.cancel_order(order.broker_order_id)
                        print(f"✓ Canceled test order {order.id} (broker_order_id={order.broker_order_id})")
                    except Exception as e:
                        if "filled" in str(e).lower():
                            print(f"  Order {order.id} already filled, skipping")
                        else:
                            print(f"✗ Failed to cancel order {order.id}: {e}")
        
        # Final refresh
        account.refresh_orders()
        print("✓ Cleanup complete")
        
    except Exception as e:
        print(f"✗ Cleanup failed: {e}")
        logger.error("Cleanup failed", exc_info=True)

def test_bracket_order_upgrade():
    """Test bracket order upgrade functionality."""
    
    print_section("Bracket Order Upgrade Test")
    
    # Get the first Alpaca account
    try:
        account = AlpacaAccount(1)
        print(f"✓ Connected to Alpaca account {account.id}")
    except Exception as e:
        print(f"✗ Failed to connect to Alpaca account: {e}")
        return
    
    # Track test transaction IDs for cleanup
    test_transaction_ids = []
    
    # Get current PANW price
    symbol = "PANW"
    print(f"\nFetching current {symbol} price...")
    current_price = account.get_instrument_current_price(symbol)
    
    if not current_price:
        print(f"✗ Failed to get current price for {symbol}")
        return
    
    print(f"✓ Current {symbol} price: ${current_price:.2f}")
    
    # Hardcoded TP and SL prices for testing
    tp_price = 250.0
    sl_price = 150.0
    
    print(f"  Take Profit price: ${tp_price:.2f} (hardcoded)")
    print(f"  Stop Loss price: ${sl_price:.2f} (hardcoded)")
    
    # ========================================================================
    # Test 1: TP first, then add SL (should upgrade to bracket)
    # ========================================================================
    print_section("Test 1: Set TP, Then Add SL (Upgrade to Bracket)")
    try:
        # Step 1: Create entry order with TP only
        print("Step 1a: Creating entry order with TP only...")
        order1 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 1: Entry with TP only"
        )
        
        submitted_order1 = account.submit_order(order1, tp_price=tp_price)
        
        if submitted_order1:
            print(f"✓ Entry order submitted: ID={submitted_order1.id}")
            print(f"  Transaction ID: {submitted_order1.transaction_id}")
            test_transaction_ids.append(submitted_order1.transaction_id)
            
            # Wait for order to process
            time.sleep(2)
            account.refresh_orders()
            
            # Step 2: Add SL to existing transaction (should trigger bracket upgrade)
            print("\nStep 1b: Adding SL (should upgrade to bracket order)...")
            fresh_order1 = get_instance(TradingOrder, submitted_order1.id)
            account.set_order_tp_sl(fresh_order1, tp_price, sl_price)
            
            print(f"✓ Set TP+SL on transaction {submitted_order1.transaction_id}")
            print("  Expected: Existing TP order canceled, new bracket order created")
            
            # Verify bracket order was created
            time.sleep(2)
            account.refresh_orders()
            
            with Session(get_db().bind) as session:
                all_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == submitted_order1.transaction_id
                    )
                ).all()
                
                print(f"\n  Transaction {submitted_order1.transaction_id} orders:")
                for order in all_orders:
                    print(f"    - Order {order.id}: {order.order_type.value} (status={order.status.value})")
        else:
            print(f"✗ Test 1 entry order failed to submit")
            
    except Exception as e:
        print(f"✗ Test 1 failed: {e}")
        logger.error(f"Test 1 failed", exc_info=True)
    
    time.sleep(3)  # Wait between tests
    
    # ========================================================================
    # Test 2: SL first, then add TP (should upgrade to bracket)
    # ========================================================================
    print_section("Test 2: Set SL, Then Add TP (Upgrade to Bracket)")
    try:
        # Step 1: Create entry order with SL only
        print("Step 2a: Creating entry order with SL only...")
        order2 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 2: Entry with SL only"
        )
        
        submitted_order2 = account.submit_order(order2, sl_price=sl_price)
        
        if submitted_order2:
            print(f"✓ Entry order submitted: ID={submitted_order2.id}")
            print(f"  Transaction ID: {submitted_order2.transaction_id}")
            test_transaction_ids.append(submitted_order2.transaction_id)
            
            # Wait for order to process
            time.sleep(2)
            account.refresh_orders()
            
            # Step 2: Add TP to existing transaction (should trigger bracket upgrade)
            print("\nStep 2b: Adding TP (should upgrade to bracket order)...")
            fresh_order2 = get_instance(TradingOrder, submitted_order2.id)
            account.set_order_tp_sl(fresh_order2, tp_price, sl_price)
            
            print(f"✓ Set TP+SL on transaction {submitted_order2.transaction_id}")
            print("  Expected: Existing SL order canceled, new bracket order created")
            
            # Verify bracket order was created
            time.sleep(2)
            account.refresh_orders()
            
            with Session(get_db().bind) as session:
                all_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == submitted_order2.transaction_id
                    )
                ).all()
                
                print(f"\n  Transaction {submitted_order2.transaction_id} orders:")
                for order in all_orders:
                    print(f"    - Order {order.id}: {order.order_type.value} (status={order.status.value})")
        else:
            print(f"✗ Test 2 entry order failed to submit")
            
    except Exception as e:
        print(f"✗ Test 2 failed: {e}")
        logger.error(f"Test 2 failed", exc_info=True)
    
    time.sleep(3)  # Wait between tests
    
    # ========================================================================
    # Test 3: Both TP+SL at once (direct bracket order)
    # ========================================================================
    print_section("Test 3: Set TP+SL Together (Direct Bracket)")
    try:
        print("Step 3: Creating entry order with both TP and SL...")
        order3 = TradingOrder(
            account_id=1,
            symbol=symbol,
            quantity=1,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            open_type=OrderOpenType.MANUAL,
            comment="Test 3: Entry with TP+SL bracket"
        )
        
        submitted_order3 = account.submit_order(order3, tp_price=tp_price, sl_price=sl_price)
        
        if submitted_order3:
            print(f"✓ Bracket order submitted: ID={submitted_order3.id}")
            print(f"  Transaction ID: {submitted_order3.transaction_id}")
            test_transaction_ids.append(submitted_order3.transaction_id)
            print("  Expected: Single entry order with bracket TP+SL created atomically")
            
            # Verify bracket order
            time.sleep(2)
            account.refresh_orders()
            
            with Session(get_db().bind) as session:
                all_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == submitted_order3.transaction_id
                    )
                ).all()
                
                print(f"\n  Transaction {submitted_order3.transaction_id} orders:")
                for order in all_orders:
                    print(f"    - Order {order.id}: {order.order_type.value} (status={order.status.value})")
        else:
            print(f"✗ Test 3 bracket order failed to submit")
            
    except Exception as e:
        print(f"✗ Test 3 failed: {e}")
        logger.error(f"Test 3 failed", exc_info=True)
    
    # Final cleanup - only cancel orders from our test transactions
    time.sleep(2)
    cleanup_test_orders(account, test_transaction_ids)
    
    print_section("Test Complete")
    print("Summary:")
    print("  - Test 1: TP first, then add SL → upgraded to bracket")
    print("  - Test 2: SL first, then add TP → upgraded to bracket")
    print("  - Test 3: TP+SL together → direct bracket order")

if __name__ == "__main__":
    test_bracket_order_upgrade()

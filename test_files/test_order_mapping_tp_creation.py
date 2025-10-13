#!/usr/bin/env python3
"""
Test Order Mapping TP Creation Issue

This script tests what happens when we map an order and whether it triggers
automatic TP creation.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.models import TradingOrder, AccountDefinition
from ba2_trade_platform.core.db import get_db, add_instance, get_instance, update_instance
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType as CoreOrderType
from ba2_trade_platform.logger import logger
from sqlmodel import select
from datetime import datetime, timezone

def test_order_mapping_tp_creation():
    """Test if order mapping triggers TP creation."""
    print("\n=== Testing Order Mapping TP Creation ===")
    
    try:
        # First, create a transaction with TP to test transaction-based TP creation
        from ba2_trade_platform.core.models import Transaction
        
        test_transaction = Transaction(
            account_id=1,
            symbol="TEST",
            side=OrderDirection.BUY,
            quantity=100,
            take_profit=155.0,  # Set a TP price
            created_at=datetime.now(timezone.utc)
        )
        
        transaction_id = add_instance(test_transaction)
        print(f"Created test transaction {transaction_id} with TP at $155.0")
        
        # Create a test order in ERROR status (typical mapping scenario)
        test_order = TradingOrder(
            account_id=1,  # Assuming account 1 exists
            symbol="TEST",
            quantity=100,
            side=OrderDirection.BUY,
            order_type=CoreOrderType.MARKET,
            status=OrderStatus.ERROR,
            broker_order_id=None,  # No broker ID initially
            transaction_id=transaction_id,  # Link to transaction with TP
            comment="Test order for mapping",
            created_at=datetime.now(timezone.utc)
        )
        
        # Add to database
        order_id = add_instance(test_order)
        print(f"Created test order {order_id}")
        
        # Refresh the order from database to get current status
        test_order = get_instance(TradingOrder, order_id)
        print(f"Test order status: {test_order.status.value}")
        
        # Count WAITING_TRIGGER orders before mapping
        with get_db() as session:
            stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
            waiting_orders_before = len(list(session.exec(stmt).all()))
            print(f"WAITING_TRIGGER orders before mapping: {waiting_orders_before}")
        
        # Simulate order mapping - update broker_order_id
        test_order.broker_order_id = "TEST_BROKER_123"
        print(f"Mapping order {order_id} to broker ID: {test_order.broker_order_id}")
        
        # Update the order (simulating the mapping process)
        if update_instance(test_order):
            print(f"Successfully mapped order {order_id}")
        else:
            print(f"Failed to map order {order_id}")
            return
        
        # Count WAITING_TRIGGER orders after mapping
        with get_db() as session:
            stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
            waiting_orders_after = list(session.exec(stmt).all())
            print(f"WAITING_TRIGGER orders after mapping: {len(waiting_orders_after)}")
            
            # Show details of any new WAITING_TRIGGER orders
            if len(waiting_orders_after) > waiting_orders_before:
                print(f"NEW WAITING_TRIGGER orders detected!")
                for order in waiting_orders_after[-1:]:  # Show the last one
                    print(f"  Order {order.id}: {order.symbol} {order.side.value} {order.quantity}")
                    print(f"    Depends on: {order.depends_on_order}")
                    print(f"    Trigger status: {order.depends_order_status_trigger}")
                    print(f"    Comment: {order.comment}")
            else:
                print("No new WAITING_TRIGGER orders created by mapping")
        
        # Now test what happens when we simulate a status change to FILLED
        print("\n--- Testing Status Change to FILLED ---")
        
        # Update order status to FILLED (simulating what happens during refresh)
        test_order.status = OrderStatus.FILLED
        print(f"Changing order {order_id} status to FILLED")
        
        # Count WAITING_TRIGGER orders before status change
        with get_db() as session:
            stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
            waiting_before_filled = len(list(session.exec(stmt).all()))
            print(f"WAITING_TRIGGER orders before FILLED status: {waiting_before_filled}")
        
        # Update the order status
        if update_instance(test_order):
            print(f"Successfully updated order {order_id} to FILLED")
        
        # Count WAITING_TRIGGER orders after status change
        with get_db() as session:
            stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
            waiting_after_filled = list(session.exec(stmt).all())
            print(f"WAITING_TRIGGER orders after FILLED status: {len(waiting_after_filled)}")
            
            # Show details of any new WAITING_TRIGGER orders
            if len(waiting_after_filled) > waiting_before_filled:
                print(f"NEW WAITING_TRIGGER orders created by status change!")
                for order in waiting_after_filled[-1:]:  # Show the last one
                    print(f"  Order {order.id}: {order.symbol} {order.side.value} {order.quantity}")
                    print(f"    Depends on: {order.depends_on_order}")
                    print(f"    Trigger status: {order.depends_order_status_trigger}")
                    print(f"    Comment: {order.comment}")
            else:
                print("No new WAITING_TRIGGER orders created by status change")
        
        # Now simulate what happens when we call account refresh
        print("\n--- Testing Account Refresh Impact ---")
        
        # Try to get the account and call refresh
        try:
            account = get_instance(AccountDefinition, test_order.account_id)
            if account:
                print(f"Found account: {account.name}")
                
                # Get the provider
                from ba2_trade_platform.modules.accounts import providers
                provider_cls = providers.get(account.provider)
                if provider_cls:
                    provider_obj = provider_cls(account.id)
                    print(f"Created provider: {provider_cls.__name__}")
                    
                    # Count WAITING_TRIGGER orders before refresh
                    with get_db() as session:
                        stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
                        waiting_before_refresh = len(list(session.exec(stmt).all()))
                        print(f"WAITING_TRIGGER orders before refresh: {waiting_before_refresh}")
                    
                    # Call refresh_orders
                    print("Calling refresh_orders...")
                    refresh_result = provider_obj.refresh_orders(heuristic_mapping=False)
                    print(f"Refresh result: {refresh_result}")
                    
                    # Count WAITING_TRIGGER orders after refresh
                    with get_db() as session:
                        stmt = select(TradingOrder).where(TradingOrder.status == OrderStatus.WAITING_TRIGGER)
                        waiting_after_refresh = list(session.exec(stmt).all())
                        print(f"WAITING_TRIGGER orders after refresh: {len(waiting_after_refresh)}")
                        
                        # Show details of any new WAITING_TRIGGER orders
                        if len(waiting_after_refresh) > waiting_before_refresh:
                            print(f"NEW WAITING_TRIGGER orders created by refresh!")
                            for order in waiting_after_refresh[-1:]:  # Show the last one
                                print(f"  Order {order.id}: {order.symbol} {order.side.value} {order.quantity}")
                                print(f"    Depends on: {order.depends_on_order}")
                                print(f"    Trigger status: {order.depends_order_status_trigger}")
                                print(f"    Comment: {order.comment}")
                        else:
                            print("No new WAITING_TRIGGER orders created by refresh")
                else:
                    print(f"No provider found for account type: {account.provider}")
            else:
                print("Account not found")
                
        except Exception as e:
            print(f"Error during account refresh test: {e}")
            logger.error(f"Error during account refresh test: {e}", exc_info=True)
        
        # Cleanup - delete the test order
        try:
            from ba2_trade_platform.core.db import delete_instance
            with get_db() as session:
                delete_instance(test_order, session)
                print(f"Cleaned up test order {order_id}")
        except Exception as e:
            print(f"Warning: Could not clean up test order {order_id}: {e}")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        logger.error(f"Order mapping TP creation test failed: {e}", exc_info=True)
        return False
    
    return True

def main():
    """Run the order mapping TP creation test."""
    print("🧪 Testing Order Mapping TP Creation Issue")
    print("=" * 50)
    
    success = test_order_mapping_tp_creation()
    
    if success:
        print("\n✅ Test completed successfully")
    else:
        print("\n❌ Test failed")
    
    return success

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
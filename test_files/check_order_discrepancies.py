"""
Check for order discrepancies between database and Alpaca.
Specifically investigates order 162 and finds all orders with status mismatches.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_instance, get_all_instances
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.logger import logger
from sqlmodel import select

def check_order_162():
    """Check specific order 162 in database and compare with Alpaca."""
    logger.info("=== Checking Order 162 ===")
    
    with get_db() as session:
        # Get order 162
        order = session.get(TradingOrder, 162)
        
        if not order:
            logger.error("Order 162 not found in database!")
            return
        
        logger.info(f"Database Order 162:")
        logger.info(f"  Symbol: {order.symbol}")
        logger.info(f"  Status: {order.status}")
        logger.info(f"  Direction: {order.side}")
        logger.info(f"  Type: {order.order_type}")
        logger.info(f"  Quantity: {order.quantity}")
        logger.info(f"  Broker Order ID: {order.broker_order_id}")
        logger.info(f"  Account ID: {order.account_id}")
        logger.info(f"  Created: {order.created_at}")
        
        # Get Alpaca account
        account = AlpacaAccount(order.account_id)
        
        if not order.broker_order_id:
            logger.error("Order has no broker_order_id, cannot check with Alpaca!")
            return
        
        # Get order from Alpaca
        try:
            alpaca_order = account.client.get_order_by_id(order.broker_order_id)
            
            logger.info(f"\nAlpaca Order {order.broker_order_id}:")
            logger.info(f"  Symbol: {alpaca_order.symbol}")
            logger.info(f"  Status: {alpaca_order.status}")
            logger.info(f"  Side: {alpaca_order.side}")
            logger.info(f"  Type: {alpaca_order.type}")
            logger.info(f"  Qty: {alpaca_order.qty}")
            logger.info(f"  Filled Qty: {alpaca_order.filled_qty}")
            logger.info(f"  Created: {alpaca_order.created_at}")
            logger.info(f"  Updated: {alpaca_order.updated_at}")
            logger.info(f"  Canceled: {alpaca_order.canceled_at}")
            
            # Compare statuses
            db_status = order.status.value if hasattr(order.status, 'value') else order.status
            alpaca_status = str(alpaca_order.status.value if hasattr(alpaca_order.status, 'value') else alpaca_order.status).upper()
            
            logger.info(f"\n📊 Status Comparison:")
            logger.info(f"  Database: {db_status}")
            logger.info(f"  Alpaca: {alpaca_status}")
            
            if db_status != alpaca_status:
                logger.error(f"❌ STATUS MISMATCH DETECTED!")
                logger.error(f"   DB shows '{db_status}' but Alpaca shows '{alpaca_status}'")
            else:
                logger.info(f"✅ Status matches between database and Alpaca")
                
        except Exception as e:
            logger.error(f"Error fetching order from Alpaca: {e}", exc_info=True)


def find_all_mismatched_orders():
    """Find all orders in database that might be mismatched with Alpaca."""
    logger.info("\n=== Scanning All Orders for Mismatches ===")
    
    with get_db() as session:
        # Get all orders that are marked as NEW or PENDING_NEW in database
        statement = select(TradingOrder).where(
            TradingOrder.status.in_([OrderStatus.NEW, OrderStatus.PENDING_NEW])
        ).where(
            TradingOrder.broker_order_id.is_not(None)
        )
        
        orders = session.exec(statement).all()
        
        logger.info(f"Found {len(orders)} orders with status NEW/PENDING_NEW and broker_order_id set")
        
        mismatches = []
        
        for order in orders:
            try:
                # Get account
                account = AlpacaAccount(order.account_id)
                
                # Get order from Alpaca
                alpaca_order = account.client.get_order_by_id(order.broker_order_id)
                
                db_status = order.status.value if hasattr(order.status, 'value') else order.status
                alpaca_status = str(alpaca_order.status.value if hasattr(alpaca_order.status, 'value') else alpaca_order.status).upper()
                
                if db_status != alpaca_status:
                    mismatch_info = {
                        "order_id": order.id,
                        "broker_order_id": order.broker_order_id,
                        "symbol": order.symbol,
                        "db_status": db_status,
                        "alpaca_status": alpaca_status,
                        "created": order.created_at,
                        "updated": getattr(order, 'updated_at', None)
                    }
                    mismatches.append(mismatch_info)
                    
                    logger.warning(f"❌ MISMATCH - Order {order.id} ({order.symbol}):")
                    logger.warning(f"   DB: {db_status} | Alpaca: {alpaca_status}")
                    logger.warning(f"   Broker Order ID: {order.broker_order_id}")
                    
            except Exception as e:
                logger.error(f"Error checking order {order.id}: {e}")
                continue
        
        logger.info(f"\n📊 Summary: Found {len(mismatches)} orders with status mismatches")
        
        if mismatches:
            logger.info("\n=== Detailed Mismatch Report ===")
            for m in mismatches:
                logger.info(f"\nOrder ID: {m['order_id']}")
                logger.info(f"  Symbol: {m['symbol']}")
                logger.info(f"  Broker Order ID: {m['broker_order_id']}")
                logger.info(f"  Database Status: {m['db_status']}")
                logger.info(f"  Alpaca Status: {m['alpaca_status']}")
                logger.info(f"  Created: {m['created']}")
                logger.info(f"  Last Updated: {m['updated']}")
        
        return mismatches


def main():
    """Main execution."""
    logger.info("Starting order discrepancy check...")
    
    # Check order 162 specifically
    check_order_162()
    
    # Find all mismatched orders
    mismatches = find_all_mismatched_orders()
    
    logger.info(f"\n{'='*60}")
    logger.info(f"Order discrepancy check complete.")
    logger.info(f"Total mismatches found: {len(mismatches)}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()

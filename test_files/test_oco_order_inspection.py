#!/usr/bin/env python3
"""
Test script to inspect a specific OCO order from Alpaca and database.
This helps debug why OCO legs aren't being created.
"""

import sys
import json
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, str(__file__).rsplit('\\', 2)[0])

import ba2_trade_platform.config as config
config.load_config_from_env()

from ba2_trade_platform.logger import logger
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from sqlmodel import Session, select
from ba2_trade_platform.core.db import get_db

def inspect_order(order_id: str):
    """Inspect a specific order from both Alpaca and database."""
    
    logger.info(f"Inspecting order: {order_id}")
    
    # Get account (assuming account_id=1 for now)
    account = AlpacaAccount(1)
    
    # Try to get the order from Alpaca directly
    logger.info("=" * 80)
    logger.info("ALPACA ORDER DETAILS")
    logger.info("=" * 80)
    
    try:
        # Fetch from Alpaca - use GetOrdersRequest
        from alpaca.trading.requests import GetOrdersRequest
        request = GetOrdersRequest(status='all', limit=100)
        alpaca_orders = account.client.get_orders(request)
        
        alpaca_order = None
        for order in alpaca_orders:
            if str(order.id) == order_id:
                alpaca_order = order
                break
        
        if alpaca_order:
            logger.info(f"Found order in Alpaca: {alpaca_order.id}")
            logger.info(f"  Symbol: {alpaca_order.symbol}")
            logger.info(f"  Type: {alpaca_order.type}")
            logger.info(f"  Order Class: {getattr(alpaca_order, 'order_class', 'N/A')}")
            logger.info(f"  Side: {alpaca_order.side}")
            logger.info(f"  Qty: {alpaca_order.qty}")
            logger.info(f"  Status: {alpaca_order.status}")
            logger.info(f"  Limit Price: {alpaca_order.limit_price}")
            logger.info(f"  Stop Price: {alpaca_order.stop_price}")
            logger.info(f"  Filled Qty: {alpaca_order.filled_qty}")
            logger.info(f"  Filled Avg Price: {alpaca_order.filled_avg_price}")
            logger.info(f"  Created At: {alpaca_order.created_at}")
            
            # Check for legs
            if hasattr(alpaca_order, 'legs') and alpaca_order.legs:
                logger.info(f"\n  LEGS: {len(alpaca_order.legs)} legs found")
                for i, leg in enumerate(alpaca_order.legs):
                    logger.info(f"\n  Leg {i}:")
                    logger.info(f"    ID: {leg.id}")
                    logger.info(f"    Symbol: {leg.symbol}")
                    logger.info(f"    Type: {leg.type}")
                    logger.info(f"    Side: {leg.side}")
                    logger.info(f"    Qty: {leg.qty}")
                    logger.info(f"    Status: {leg.status}")
                    logger.info(f"    Limit Price: {leg.limit_price}")
                    logger.info(f"    Stop Price: {leg.stop_price}")
                    logger.info(f"    Filled Qty: {leg.filled_qty}")
                    logger.info(f"    Filled Avg Price: {leg.filled_avg_price}")
            else:
                logger.info(f"\n  NO LEGS FOUND (has 'legs' attr: {hasattr(alpaca_order, 'legs')})")
        else:
            logger.warning(f"Order {order_id} not found in Alpaca")
    
    except Exception as e:
        logger.error(f"Error fetching from Alpaca: {e}", exc_info=True)
    
    # Now check database
    logger.info("\n" + "=" * 80)
    logger.info("DATABASE ORDER DETAILS")
    logger.info("=" * 80)
    
    try:
        with Session(get_db().bind) as session:
            # Find order by broker_order_id
            db_order = session.exec(
                select(TradingOrder).where(TradingOrder.broker_order_id == order_id)
            ).first()
            
            if db_order:
                logger.info(f"Found order in database: ID={db_order.id}")
                logger.info(f"  Broker Order ID: {db_order.broker_order_id}")
                logger.info(f"  Symbol: {db_order.symbol}")
                logger.info(f"  Order Type: {db_order.order_type}")
                logger.info(f"  Side: {db_order.side}")
                logger.info(f"  Quantity: {db_order.quantity}")
                logger.info(f"  Status: {db_order.status}")
                logger.info(f"  Limit Price: {db_order.limit_price}")
                logger.info(f"  Stop Price: {db_order.stop_price}")
                logger.info(f"  Filled Qty: {db_order.filled_qty}")
                logger.info(f"  Open Price: {db_order.open_price}")
                logger.info(f"  Transaction ID: {db_order.transaction_id}")
                logger.info(f"  Created At: {db_order.created_at}")
                
                # Check for dependent orders (legs)
                dependent_orders = session.exec(
                    select(TradingOrder).where(TradingOrder.depends_on_order == db_order.id)
                ).all()
                
                if dependent_orders:
                    logger.info(f"\n  LEGS: {len(dependent_orders)} dependent orders found")
                    for i, leg in enumerate(dependent_orders):
                        logger.info(f"\n  Leg {i}:")
                        logger.info(f"    ID: {leg.id}")
                        logger.info(f"    Broker Order ID: {leg.broker_order_id}")
                        logger.info(f"    Symbol: {leg.symbol}")
                        logger.info(f"    Order Type: {leg.order_type}")
                        logger.info(f"    Side: {leg.side}")
                        logger.info(f"    Quantity: {leg.quantity}")
                        logger.info(f"    Status: {leg.status}")
                        logger.info(f"    Limit Price: {leg.limit_price}")
                        logger.info(f"    Stop Price: {leg.stop_price}")
                        logger.info(f"    Comment: {leg.comment}")
                else:
                    logger.warning(f"\n  NO DEPENDENT LEGS FOUND in database")
            else:
                logger.warning(f"Order {order_id} not found in database")
    
    except Exception as e:
        logger.error(f"Error querying database: {e}", exc_info=True)

def main():
    # First, let's find ALL OCO orders and see if any have legs
    logger.info("=" * 80)
    logger.info("SEARCHING FOR ALL OCO ORDERS WITH LEGS")
    logger.info("=" * 80)
    
    account = AlpacaAccount(1)
    
    try:
        from alpaca.trading.requests import GetOrdersRequest
        request = GetOrdersRequest(status='all', limit=500)
        alpaca_orders = account.client.get_orders(request)
        
        oco_orders = []
        for order in alpaca_orders:
            if hasattr(order, 'order_class') and order.order_class:
                order_class_str = str(order.order_class).lower()
                logger.debug(f"Order {order.id}: order_class={order.order_class}, str={order_class_str}")
                if 'oco' in order_class_str:
                    oco_orders.append(order)
                logger.info(f"\nFound OCO order: {order.id}")
                logger.info(f"  Status: {order.status}")
                logger.info(f"  Has legs attr: {hasattr(order, 'legs')}")
                if hasattr(order, 'legs'):
                    legs = getattr(order, 'legs', None)
                    logger.info(f"  Legs value: {legs}")
                    logger.info(f"  Legs is None: {legs is None}")
                    logger.info(f"  Legs is empty list: {legs == []}")
                    if legs:
                        logger.info(f"  Legs count: {len(legs)}")
                        for i, leg in enumerate(legs):
                            logger.info(f"    Leg {i}: {leg.id} - {leg.side} {leg.qty} @ {leg.limit_price or leg.stop_price}")
        
        logger.info(f"\n\nTotal OCO orders found: {len(oco_orders)}")
        
        # Now check if ANY of these OCO orders have leg orders in the database
        logger.info("\nChecking database for legs of all OCO orders...")
        with Session(get_db().bind) as session:
            for order in oco_orders[:3]:  # Check first 3
                db_order = session.exec(
                    select(TradingOrder).where(TradingOrder.broker_order_id == str(order.id))
                ).first()
                
                if db_order:
                    legs = session.exec(
                        select(TradingOrder).where(TradingOrder.depends_on_order == db_order.id)
                    ).all()
                    logger.info(f"Alpaca order {order.id}: DB has {len(legs)} legs")
                else:
                    logger.info(f"Alpaca order {order.id}: NOT in database")
    
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    
    # Now inspect the specific order
    logger.info("\n" + "=" * 80)
    order_id = "9eabcdf4-a4bc-45a8-8b48-25666ddee8f9"
    inspect_order(order_id)

if __name__ == "__main__":
    main()

"""
Test script for legacy TP/SL order migration.

This script tests that existing LIMIT/STOP/STOP_LIMIT orders are properly
recognized as TP/SL orders and migrated to OTO/OCO when adjusted.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_instance, add_instance, update_instance, get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, AccountDefinition
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus
)
from ba2_trade_platform.modules.accounts import get_account_class
from ba2_trade_platform.logger import logger
from datetime import datetime, timezone
from sqlmodel import Session, select


def get_account(account_id: int):
    """Get account interface instance from account ID."""
    account_class = get_account_class("Alpaca")
    if not account_class:
        raise ValueError("Alpaca account provider not found")
    return account_class(account_id)


def setup_test_account():
    """Get test Alpaca account."""
    with Session(get_db().bind) as session:
        statement = select(AccountDefinition).where(
            (AccountDefinition.provider == "Alpaca") | 
            (AccountDefinition.provider == "AlpacaAccount")
        )
        result = session.exec(statement).first()
        
        if not result:
            logger.error("No Alpaca account found in database.")
            return None
        
        return result.id


def create_transaction_with_legacy_orders(account_id: int, symbol: str = "TSLA") -> int:
    """Create a transaction with legacy LIMIT/STOP orders."""
    logger.info(f"\n{'='*60}")
    logger.info(f"Creating transaction with legacy TP/SL orders for {symbol}")
    logger.info(f"{'='*60}")
    
    # Create transaction
    transaction = Transaction(
        symbol=symbol,
        quantity=10,
        side=OrderDirection.BUY,  # LONG position
        open_price=250.0,
        status=TransactionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        expert_id=None,
        take_profit=275.0,  # TP at 10% profit
        stop_loss=240.0,    # SL at 4% loss
    )
    
    transaction_id = add_instance(transaction)
    logger.info(f"Created transaction {transaction_id}: {symbol} x10 @ 250.0, TP=275.0, SL=240.0")
    
    # Create entry order (PENDING)
    entry_order = TradingOrder(
        symbol=symbol,
        quantity=10,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        transaction_id=transaction_id,
        account_id=account_id,
        open_price=250.0,
        created_at=datetime.now(timezone.utc),
        comment="Test entry order"
    )
    
    entry_id = add_instance(entry_order)
    logger.info(f"Created pending entry order {entry_id}")
    
    # Create legacy LIMIT order as TP (SELL_LIMIT would be the real type, but we use "limit" string)
    legacy_tp = TradingOrder(
        symbol=symbol,
        quantity=10,
        side=OrderDirection.SELL,
        order_type="sell_limit",  # Legacy order type
        status=OrderStatus.PENDING,
        transaction_id=transaction_id,
        account_id=account_id,
        limit_price=275.0,
        created_at=datetime.now(timezone.utc),
        comment="Legacy TP order (LIMIT)"
    )
    
    tp_id = add_instance(legacy_tp)
    logger.info(f"Created legacy LIMIT TP order {tp_id} @ 275.0")
    
    # Create legacy STOP order as SL
    legacy_sl = TradingOrder(
        symbol=symbol,
        quantity=10,
        side=OrderDirection.SELL,
        order_type="sell_stop",  # Legacy order type
        status=OrderStatus.PENDING,
        transaction_id=transaction_id,
        account_id=account_id,
        stop_price=240.0,
        created_at=datetime.now(timezone.utc),
        comment="Legacy SL order (STOP)"
    )
    
    sl_id = add_instance(legacy_sl)
    logger.info(f"Created legacy STOP SL order {sl_id} @ 240.0")
    
    return transaction_id


def test_legacy_tp_migration(account, transaction_id: int):
    """Test that legacy TP order is recognized and migrated to OTO/OCO."""
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST: Migrate legacy TP order to OTO/OCO")
    logger.info(f"{'='*60}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
        
        # Check existing orders
        orders = session.exec(
            select(TradingOrder).where(TradingOrder.transaction_id == transaction_id)
        ).all()
        
        logger.info(f"Orders before adjustment:")
        for order in orders:
            logger.info(f"  Order {order.id}: type={order.order_type}, side={order.side}, limit={order.limit_price}, stop={order.stop_price}")
    
    # Adjust TP - should detect legacy LIMIT order and upgrade it
    new_tp = 280.0
    logger.info(f"\nAdjusting TP to {new_tp} (should upgrade legacy order to OTO/OCO)")
    
    success = account.adjust_tp(transaction, new_tp)
    
    if success:
        logger.info(f"✅ Successfully adjusted TP")
        
        # Check orders after adjustment
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            orders = session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id == transaction_id)
            ).all()
            
            logger.info(f"\nOrders after adjustment:")
            tp_found = False
            for order in orders:
                logger.info(f"  Order {order.id}: type={order.order_type}, side={order.side}, limit={order.limit_price}, stop={order.stop_price}, status={order.status}")
                if order.limit_price == new_tp and order.side == OrderDirection.SELL:
                    tp_found = True
                    if order.order_type in ["oto", "oco"]:
                        logger.info(f"  ✅ TP order migrated to {order.order_type.upper()}")
                    else:
                        logger.warning(f"  ⚠️  TP order still using legacy type: {order.order_type}")
            
            if not tp_found:
                logger.error(f"  ❌ No TP order found with new price {new_tp}")
    else:
        logger.error(f"❌ Failed to adjust TP")
    
    return success


def test_legacy_sl_migration(account, transaction_id: int):
    """Test that legacy SL order is recognized and migrated to OTO/OCO."""
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST: Migrate legacy SL order to OTO/OCO")
    logger.info(f"{'='*60}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    # Adjust SL - should detect legacy STOP order and upgrade it
    new_sl = 235.0
    logger.info(f"Adjusting SL to {new_sl} (should upgrade legacy order to OTO/OCO)")
    
    success = account.adjust_sl(transaction, new_sl)
    
    if success:
        logger.info(f"✅ Successfully adjusted SL")
        
        # Check orders after adjustment
        with get_db() as session:
            orders = session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id == transaction_id)
            ).all()
            
            logger.info(f"\nOrders after adjustment:")
            sl_found = False
            for order in orders:
                logger.info(f"  Order {order.id}: type={order.order_type}, side={order.side}, limit={order.limit_price}, stop={order.stop_price}, status={order.status}")
                if order.stop_price == new_sl and order.side == OrderDirection.SELL:
                    sl_found = True
                    if order.order_type in ["oto", "oco"]:
                        logger.info(f"  ✅ SL order migrated to {order.order_type.upper()}")
                    else:
                        logger.warning(f"  ⚠️  SL order still using legacy type: {order.order_type}")
            
            if not sl_found:
                logger.error(f"  ❌ No SL order found with new price {new_sl}")
    else:
        logger.error(f"❌ Failed to adjust SL")
    
    return success


def main():
    """Run legacy order migration tests."""
    logger.info("="*60)
    logger.info("LEGACY TP/SL ORDER MIGRATION TESTS")
    logger.info("="*60)
    
    # Setup
    account_id = setup_test_account()
    if not account_id:
        return
    
    logger.info(f"Using Alpaca account ID: {account_id}")
    
    # Get account interface
    account = get_account(account_id)
    
    # Create transaction with legacy orders
    transaction_id = create_transaction_with_legacy_orders(account_id, symbol="TSLA")
    
    # Test TP migration
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    test_legacy_tp_migration(account, transaction_id)
    
    # Test SL migration
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    test_legacy_sl_migration(account, transaction_id)
    
    logger.info(f"\n{'='*60}")
    logger.info("ALL TESTS COMPLETED")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()

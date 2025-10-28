"""
Test script for stateless TP/SL adjustment logic in AlpacaAccount.

This script tests the new adjust_tp(), adjust_sl(), and adjust_tp_sl() methods
which implement the TP_SL_LOGIC.md requirements:
- Transaction as source of truth
- Stateless operation (determines action based on current state)
- Handles unsent orders (PENDING/WAITING_TRIGGER)
- Handles executed orders (FILLED/PARTIALLY_FILLED)
- OCO/OTO order creation
- PENDING_CANCEL fallback when replace fails
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
    """Get or create test Alpaca account."""
    with Session(get_db().bind) as session:
        # Find first Alpaca account (provider can be "Alpaca" or "AlpacaAccount")
        statement = select(AccountDefinition).where(
            (AccountDefinition.provider == "Alpaca") | 
            (AccountDefinition.provider == "AlpacaAccount")
        )
        result = session.exec(statement).first()
        
        if not result:
            logger.error("No Alpaca account found in database. Please create one first.")
            return None
        
        return result.id


def create_test_transaction(account_id: int, symbol: str = "AAPL") -> int:
    """Create a test transaction with a pending entry order."""
    logger.info(f"\n{'='*60}")
    logger.info(f"Creating test transaction for {symbol}")
    logger.info(f"{'='*60}")
    
    # Create transaction
    transaction = Transaction(
        symbol=symbol,
        quantity=10,
        open_price=150.0,
        status=TransactionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        expert_id=None,  # Not linked to expert
        take_profit=160.0,  # Initial TP
        stop_loss=145.0,    # Initial SL
    )
    
    transaction_id = add_instance(transaction)
    logger.info(f"Created transaction {transaction_id}: {symbol} x10 @ 150.0, TP=160.0, SL=145.0")
    
    # Create pending entry order
    entry_order = TradingOrder(
        symbol=symbol,
        quantity=10,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        transaction_id=transaction_id,
        account_id=account_id,
        created_at=datetime.now(timezone.utc),
        comment="Test entry order"
    )
    
    order_id = add_instance(entry_order)
    logger.info(f"Created pending entry order {order_id}")
    
    return transaction_id


def test_adjust_tp_unsent_order(account, transaction_id: int):
    """Test adjust_tp with unsent entry order (PENDING status)."""
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST 1: Adjust TP with unsent entry order")
    logger.info(f"{'='*60}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
        logger.info(f"Current transaction state: TP={transaction.take_profit}, SL={transaction.stop_loss}")
    
    # Adjust TP - should create/update PENDING TP order
    new_tp = 165.0
    logger.info(f"Calling account.adjust_tp(transaction, {new_tp})")
    
    success = account.adjust_tp(transaction, new_tp)
    
    if success:
        logger.info(f"✅ Successfully adjusted TP to {new_tp}")
        
        # Verify transaction updated
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            logger.info(f"Transaction take_profit updated to: {transaction.take_profit}")
            
            # Check if TP order created
            tp_orders = session.exec(
                select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.order_type.in_(["limit", "oto", "oco"]),
                    TradingOrder.side == OrderDirection.SELL,
                    TradingOrder.limit_price.isnot(None)
                )
            ).all()
            
            logger.info(f"Found {len(tp_orders)} TP order(s):")
            for order in tp_orders:
                logger.info(f"  Order {order.id}: status={order.status}, limit_price={order.limit_price}")
    else:
        logger.error(f"❌ Failed to adjust TP")
    
    return success


def test_adjust_sl_unsent_order(account, transaction_id: int):
    """Test adjust_sl with unsent entry order (PENDING status)."""
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST 2: Adjust SL with unsent entry order")
    logger.info(f"{'='*60}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
        logger.info(f"Current transaction state: TP={transaction.take_profit}, SL={transaction.stop_loss}")
    
    # Adjust SL - should create/update PENDING SL order
    new_sl = 140.0
    logger.info(f"Calling account.adjust_sl(transaction, {new_sl})")
    
    success = account.adjust_sl(transaction, new_sl)
    
    if success:
        logger.info(f"✅ Successfully adjusted SL to {new_sl}")
        
        # Verify transaction updated
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            logger.info(f"Transaction stop_loss updated to: {transaction.stop_loss}")
            
            # Check if SL order created
            sl_orders = session.exec(
                select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.order_type.in_(["stop", "oto", "oco"]),
                    TradingOrder.side == OrderDirection.SELL,
                    TradingOrder.stop_price.isnot(None)
                )
            ).all()
            
            logger.info(f"Found {len(sl_orders)} SL order(s):")
            for order in sl_orders:
                logger.info(f"  Order {order.id}: status={order.status}, stop_price={order.stop_price}")
    else:
        logger.error(f"❌ Failed to adjust SL")
    
    return success


def test_adjust_tp_sl_together(account, transaction_id: int):
    """Test adjust_tp_sl with both TP and SL."""
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST 3: Adjust both TP and SL together")
    logger.info(f"{'='*60}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
        logger.info(f"Current transaction state: TP={transaction.take_profit}, SL={transaction.stop_loss}")
    
    # Adjust both - should update transaction and handle orders
    new_tp = 170.0
    new_sl = 138.0
    logger.info(f"Calling account.adjust_tp_sl(transaction, TP={new_tp}, SL={new_sl})")
    
    success = account.adjust_tp_sl(transaction, new_tp, new_sl)
    
    if success:
        logger.info(f"✅ Successfully adjusted TP to {new_tp} and SL to {new_sl}")
        
        # Verify transaction updated
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            logger.info(f"Transaction updated: TP={transaction.take_profit}, SL={transaction.stop_loss}")
    else:
        logger.error(f"❌ Failed to adjust TP/SL")
    
    return success


def test_adjust_with_executed_order(account_id: int, symbol: str = "T"):
    """
    Test adjust_tp/adjust_sl with executed order (requires real broker interaction).
    
    NOTE: This test submits a REAL market order to Alpaca paper trading account.
    Make sure you have paper trading enabled.
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"TEST 4: Adjust TP/SL with executed order (REAL BROKER TEST)")
    logger.info(f"{'='*60}")
    logger.warning("⚠️  This test will submit a REAL market order to your Alpaca paper trading account!")
    
    response = input("Continue? (y/n): ")
    if response.lower() != 'y':
        logger.info("Test skipped by user")
        return False
    
    # Get account interface
    account = get_account(account_id)
    
    # Get current price
    current_price = account.get_instrument_current_price(symbol)
    if not current_price:
        logger.error(f"Failed to get current price for {symbol}")
        return False
    
    logger.info(f"Current price for {symbol}: ${current_price:.2f}")
    
    # Create transaction
    quantity = 10
    transaction = Transaction(
        symbol=symbol,
        quantity=quantity,
        open_price=current_price,
        status=TransactionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        expert_id=None,
        take_profit=current_price * 1.05,  # 5% above
        stop_loss=current_price * 0.97,     # 3% below
    )
    
    transaction_id = add_instance(transaction)
    logger.info(f"Created transaction {transaction_id}: {symbol} x{quantity} @ {current_price:.2f}")
    
    # Create and submit market order
    entry_order = TradingOrder(
        symbol=symbol,
        quantity=quantity,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        transaction_id=transaction_id,
        account_id=account_id,
        created_at=datetime.now(timezone.utc),
        comment="Test executed order"
    )
    
    order_id = add_instance(entry_order)
    logger.info(f"Created entry order {order_id}, submitting to broker...")
    
    # Submit order
    submitted_order = account.submit_order(entry_order)
    
    if not submitted_order:
        logger.error("Failed to submit order")
        return False
    
    logger.info(f"Order submitted successfully. Status: {submitted_order.status}")
    logger.info(f"Broker order ID: {submitted_order.broker_order_id}")
    
    # Wait for execution (market orders usually fill quickly)
    import time
    logger.info("Waiting 5 seconds for order execution...")
    time.sleep(5)
    
    # Refresh orders to get latest status
    account.refresh_orders()
    
    # Get updated order status
    with get_db() as session:
        updated_order = session.get(TradingOrder, submitted_order.id)
        logger.info(f"Order status after refresh: {updated_order.status}")
        
        if updated_order.status not in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
            logger.warning(f"Order not filled yet (status: {updated_order.status}). Test may not work as expected.")
    
    # Now test TP/SL adjustment - should create OCO/OTO orders at broker
    new_tp = current_price * 1.08  # 8% above
    new_sl = current_price * 0.95  # 5% below
    
    logger.info(f"\nAdjusting TP to ${new_tp:.2f} and SL to ${new_sl:.2f}")
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    success = account.adjust_tp_sl(transaction, new_tp, new_sl)
    
    if success:
        logger.info(f"✅ Successfully adjusted TP/SL for executed order")
        
        # Check created orders
        with get_db() as session:
            orders = session.exec(
                select(TradingOrder).where(TradingOrder.transaction_id == transaction_id)
            ).all()
            
            logger.info(f"\nAll orders for transaction {transaction_id}:")
            for order in orders:
                logger.info(f"  Order {order.id}: {order.order_type}, {order.side}, status={order.status}, broker_order_id={order.broker_order_id}")
    else:
        logger.error(f"❌ Failed to adjust TP/SL")
    
    return success


def main():
    """Run all tests."""
    logger.info("="*60)
    logger.info("STATELESS TP/SL ADJUSTMENT TESTS")
    logger.info("="*60)
    
    # Setup
    account_id = setup_test_account()
    if not account_id:
        return
    
    logger.info(f"Using Alpaca account ID: {account_id}")
    
    # Get account interface
    account = get_account(account_id)
    
    # Test 1-3: Unsent order tests (safe, no broker interaction)
    transaction_id = create_test_transaction(account_id, symbol="AAPL")
    
    # Refresh transaction for each test
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    test_adjust_tp_unsent_order(account, transaction_id)
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    test_adjust_sl_unsent_order(account, transaction_id)
    
    with get_db() as session:
        transaction = session.get(Transaction, transaction_id)
    
    test_adjust_tp_sl_together(account, transaction_id)
    
    # Test 4: Executed order test (requires broker interaction)
    logger.info(f"\n{'='*60}")
    response = input("\nRun executed order test (submits REAL market order)? (y/n): ")
    if response.lower() == 'y':
        test_adjust_with_executed_order(account_id, symbol="T")
    else:
        logger.info("Executed order test skipped")
    
    logger.info(f"\n{'='*60}")
    logger.info("ALL TESTS COMPLETED")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()

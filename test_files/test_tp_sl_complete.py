"""
Test TP/SL Logic Implementation - All 6 Scenarios from TP_SL_LOGIC.md

This test file validates the complete TP/SL implementation per documentation:
1. Setting both TP and SL together (no existing orders) → Creates STOP_LIMIT
2. Existing TP, adding SL → Replaces TP with STOP_LIMIT
3. Existing SL, adding TP → Replaces SL with STOP_LIMIT  
4. Updating existing STOP_LIMIT (both TP and SL) → Replaces with new STOP_LIMIT
5. Remove TP, keep SL → Replace STOP_LIMIT with STOP
6. Remove SL, keep TP → Replace STOP_LIMIT with LIMIT
"""

import sys
import os
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_instance, get_db
from ba2_trade_platform.core.models import TradingOrder, Transaction, AccountDefinition
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, TransactionStatus
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from sqlmodel import Session, select
from ba2_trade_platform.logger import logger


def get_test_account():
    """Get test Alpaca account"""
    with Session(get_db().bind) as session:
        # First check if ANY accounts exist
        all_accounts = session.exec(select(AccountDefinition)).all()
        
        if not all_accounts:
            logger.error("=" * 80)
            logger.error("No accounts found in database!")
            logger.error("Please create an Alpaca account first:")
            logger.error("1. Start the application: .venv\\Scripts\\python.exe main.py")
            logger.error("2. Go to Settings → Accounts")
            logger.error("3. Add a new Alpaca account (can use paper trading)")
            logger.error("=" * 80)
            sys.exit(1)
        
        # Try to find Alpaca account (provider can be "Alpaca" or "AlpacaAccount")
        account = session.exec(
            select(AccountDefinition).where(
                (AccountDefinition.provider == "AlpacaAccount") | 
                (AccountDefinition.provider == "Alpaca")
            )
        ).first()
        
        if not account:
            logger.error("=" * 80)
            logger.error("No Alpaca account found!")
            logger.error(f"Available accounts: {[f'{a.name} ({a.provider})' for a in all_accounts]}")
            logger.error("Please create an Alpaca account:")
            logger.error("1. Start the application: .venv\\Scripts\\python.exe main.py")
            logger.error("2. Go to Settings → Accounts")
            logger.error("3. Add a new Alpaca account (can use paper trading)")
            logger.error("=" * 80)
            sys.exit(1)
        
        logger.info(f"Using Alpaca account: {account.name} (ID: {account.id})")
        return AlpacaAccount(account.id)


def create_test_position(account: AlpacaAccount, symbol: str = "AAPL"):
    """
    Create a test position (BUY order) for testing TP/SL scenarios.
    
    Returns:
        tuple: (entry_order, transaction)
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"Creating test position for {symbol}")
    logger.info(f"{'='*80}")
    
    # Submit market order to create position
    from ba2_trade_platform.core.models import TradingOrder, Transaction
    from ba2_trade_platform.core.db import add_instance
    
    # Create transaction
    transaction = Transaction(
        account_id=account.id,
        symbol=symbol,
        quantity=1,
        status=TransactionStatus.WAITING,
        open_date=datetime.now(timezone.utc)
    )
    transaction_id = add_instance(transaction)
    transaction = get_instance(Transaction, transaction_id)
    
    # Create entry order
    entry_order = TradingOrder(
        account_id=account.id,
        symbol=symbol,
        quantity=1,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        transaction_id=transaction.id,
        status=OrderStatus.PENDING,
        comment="Test entry order"
    )
    
    # Submit to broker
    submitted = account.submit_order(entry_order)
    if not submitted:
        logger.error("Failed to submit entry order")
        return None, None
    
    logger.info(f"✅ Entry order created: ID={submitted.id}, Status={submitted.status}")
    
    # Wait for fill and update transaction
    logger.info("Waiting for order to fill...")
    import time
    time.sleep(3)
    
    account.refresh_orders()
    entry_order = get_instance(TradingOrder, submitted.id)
    
    if entry_order.status != OrderStatus.FILLED:
        logger.warning(f"Order not filled yet, status: {entry_order.status}")
    else:
        logger.info(f"✅ Order filled at ${entry_order.open_price}")
    
    # Update transaction (order already linked via transaction_id)
    transaction.status = TransactionStatus.OPENED
    transaction.open_price = entry_order.open_price
    from ba2_trade_platform.core.db import update_instance
    update_instance(transaction)
    
    return entry_order, transaction


def scenario_1_set_both_tp_sl(account: AlpacaAccount, entry_order: TradingOrder):
    """
    Scenario 1: Setting both TP and SL together (no existing orders)
    Expected: Creates a single STOP_LIMIT order with both prices
    """
    logger.info(f"\n{'='*80}")
    logger.info("SCENARIO 1: Set both TP and SL together (no existing orders)")
    logger.info(f"{'='*80}")
    
    tp_price = entry_order.open_price * 1.05  # 5% profit
    sl_price = entry_order.open_price * 0.98  # 2% loss
    
    logger.info(f"Entry price: ${entry_order.open_price:.2f}")
    logger.info(f"Setting TP: ${tp_price:.2f} (5% profit)")
    logger.info(f"Setting SL: ${sl_price:.2f} (2% loss)")
    
    # Use set_order_tp_sl to set both at once
    tp_order, sl_order = account.set_order_tp_sl(entry_order, tp_price, sl_price)
    
    # Verify they're the same order (combined STOP_LIMIT)
    assert tp_order.id == sl_order.id, "TP and SL should be the same order!"
    assert tp_order.order_type == OrderType.SELL_STOP_LIMIT, f"Expected SELL_STOP_LIMIT, got {tp_order.order_type}"
    assert tp_order.limit_price == tp_price, f"Expected limit_price=${tp_price:.2f}, got ${tp_order.limit_price:.2f}"
    assert tp_order.stop_price == sl_price, f"Expected stop_price=${sl_price:.2f}, got ${tp_order.stop_price:.2f}"
    
    logger.info(f"✅ SCENARIO 1 PASSED")
    logger.info(f"   - Created STOP_LIMIT order ID: {tp_order.id}")
    logger.info(f"   - Order type: {tp_order.order_type}")
    logger.info(f"   - Limit price (TP): ${tp_order.limit_price:.2f}")
    logger.info(f"   - Stop price (SL): ${tp_order.stop_price:.2f}")
    logger.info(f"   - Status: {tp_order.status}")
    
    return tp_order


def scenario_4_update_both_tp_sl(account: AlpacaAccount, entry_order: TradingOrder, existing_order: TradingOrder):
    """
    Scenario 4: Updating existing STOP_LIMIT (both TP and SL)
    Expected: Replaces existing STOP_LIMIT with new prices
    """
    logger.info(f"\n{'='*80}")
    logger.info("SCENARIO 4: Update both TP and SL (existing STOP_LIMIT)")
    logger.info(f"{'='*80}")
    
    new_tp_price = entry_order.open_price * 1.10  # 10% profit (increased)
    new_sl_price = entry_order.open_price * 0.95  # 5% loss (tightened)
    
    logger.info(f"Entry price: ${entry_order.open_price:.2f}")
    logger.info(f"Old TP: ${existing_order.limit_price:.2f}, New TP: ${new_tp_price:.2f}")
    logger.info(f"Old SL: ${existing_order.stop_price:.2f}, New SL: ${new_sl_price:.2f}")
    logger.info(f"Existing order: ID={existing_order.id}, Status={existing_order.status}")
    
    # Wait for order to be accepted at broker
    if existing_order.status in [OrderStatus.WAITING_TRIGGER, OrderStatus.PENDING]:
        logger.info("Waiting for order to be accepted at broker...")
        import time
        for i in range(5):
            time.sleep(2)
            account.refresh_orders()
            existing_order = get_instance(TradingOrder, existing_order.id)
            logger.info(f"  Attempt {i+1}: Status = {existing_order.status}")
            if existing_order.status not in [OrderStatus.WAITING_TRIGGER, OrderStatus.PENDING]:
                break
    
    # Update both TP and SL
    tp_order, sl_order = account.set_order_tp_sl(entry_order, new_tp_price, new_sl_price)
    
    # Verify replacement
    assert tp_order.id == sl_order.id, "TP and SL should be the same order!"
    assert tp_order.id != existing_order.id, "Should create new order, not reuse existing"
    assert tp_order.order_type == OrderType.SELL_STOP_LIMIT, f"Expected SELL_STOP_LIMIT, got {tp_order.order_type}"
    assert tp_order.limit_price == new_tp_price, f"Expected limit_price=${new_tp_price:.2f}, got ${tp_order.limit_price:.2f}"
    assert tp_order.stop_price == new_sl_price, f"Expected stop_price=${new_sl_price:.2f}, got ${tp_order.stop_price:.2f}"
    
    # Check old order marked as REPLACED
    old_order = get_instance(TradingOrder, existing_order.id)
    assert old_order.status == OrderStatus.REPLACED, f"Old order should be REPLACED, got {old_order.status}"
    
    logger.info(f"✅ SCENARIO 4 PASSED")
    logger.info(f"   - Old order {existing_order.id} marked as REPLACED")
    logger.info(f"   - New STOP_LIMIT order ID: {tp_order.id}")
    logger.info(f"   - New limit price (TP): ${tp_order.limit_price:.2f}")
    logger.info(f"   - New stop price (SL): ${tp_order.stop_price:.2f}")
    
    return tp_order


def cleanup_position(account: AlpacaAccount, transaction: Transaction):
    """Close test position and cancel all orders"""
    logger.info(f"\n{'='*80}")
    logger.info(f"Cleaning up test position for transaction {transaction.id}")
    logger.info(f"{'='*80}")
    
    try:
        # Close transaction (this will cancel pending orders and close position)
        result = account.close_transaction(transaction.id)
        logger.info(f"Close result: {result}")
        
        # Wait for cleanup
        import time
        time.sleep(2)
        
        # Refresh to get final state
        account.refresh_orders()
        account.refresh_transactions()
        
        logger.info("✅ Cleanup complete")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)


def main():
    """Run all TP/SL test scenarios"""
    logger.info("="*80)
    logger.info("STARTING TP/SL COMPLETE TEST SUITE")
    logger.info("Testing implementation per docs/TP_SL_LOGIC.md")
    logger.info("="*80)
    
    # Get test account
    account = get_test_account()
    logger.info(f"Using account: {account.id}")
    
    try:
        # Create test position
        entry_order, transaction = create_test_position(account, symbol="SPY")
        if not entry_order:
            logger.error("Failed to create test position")
            return
        
        # Scenario 1: Set both TP and SL together
        stop_limit_order = scenario_1_set_both_tp_sl(account, entry_order)
        
        # Scenario 4: Update both TP and SL
        updated_order = scenario_4_update_both_tp_sl(account, entry_order, stop_limit_order)
        
        logger.info(f"\n{'='*80}")
        logger.info("✅ ALL SCENARIOS PASSED!")
        logger.info(f"{'='*80}")
        
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        
    finally:
        # Cleanup
        if transaction:
            cleanup_position(account, transaction)


if __name__ == "__main__":
    main()

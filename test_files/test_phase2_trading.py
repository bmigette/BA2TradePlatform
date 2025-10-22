"""
Test Phase 2: Smart Risk Manager Toolkit Trading Functions

Tests all transaction-aware trading tools with AGNC symbol on paper account.
CRITICAL: These tests execute REAL orders on the paper trading account!

Test Coverage:
1. open_new_position - Opens new market order with optional TP/SL
2. close_position - Closes entire transaction
3. adjust_quantity - Partial close or add to position
4. update_stop_loss - Updates SL order for transaction
5. update_take_profit - Updates TP order for transaction

Prerequisites:
- Paper trading account configured and active
- AGNC symbol enabled in expert settings
- Expert instance with proper account linkage
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_db, get_instance, add_instance, delete_instance
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting, AccountDefinition, Transaction, TradingOrder
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus
from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.logger import logger
import time
import json

# Test configuration
TEST_SYMBOL = "AGNC"
TEST_QUANTITY = 1.0
PAPER_ACCOUNT_ID = 1  # Update if your paper account has different ID
EXPERT_NAME = "TradingAgents"  # Expert type to create instance for
TEST_EXPERT_INSTANCE_ID = None  # Will be set by setup_test_expert()

def print_separator(title: str):
    """Print a formatted separator"""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")

def setup_test_expert():
    """
    Create a dedicated test expert instance with TEST_SYMBOL enabled.
    Returns the expert_instance_id.
    """
    print_separator("SETTING UP TEST EXPERT")
    
    with get_db() as session:
        # Create expert instance
        expert_instance = ExpertInstance(
            account_id=PAPER_ACCOUNT_ID,
            expert=EXPERT_NAME,
            enabled=True
        )
        
        expert_id = add_instance(expert_instance)
        print(f"✅ Created test expert instance: {EXPERT_NAME} (ID: {expert_id})")
        
        # Enable TEST_SYMBOL in expert settings
        enabled_instruments_setting = ExpertSetting(
            instance_id=expert_id,
            key="enabled_instruments",
            value_json=[TEST_SYMBOL]  # Enable only AGNC for testing
        )
        add_instance(enabled_instruments_setting)
        print(f"✅ Enabled instrument: {TEST_SYMBOL}")
        
        # Add other required settings
        enable_buy_setting = ExpertSetting(
            instance_id=expert_id,
            key="enable_buy",
            value_json=True
        )
        add_instance(enable_buy_setting)
        
        enable_sell_setting = ExpertSetting(
            instance_id=expert_id,
            key="enable_sell",
            value_json=True
        )
        add_instance(enable_sell_setting)
        
        # Add position size limit setting
        max_position_pct_setting = ExpertSetting(
            instance_id=expert_id,
            key="max_virtual_equity_per_instrument_percent",
            value_float=100.0  # Allow up to 100% of equity per instrument
        )
        add_instance(max_position_pct_setting)
        
        print(f"✅ Configured buy/sell permissions")
        
        return expert_id

def cleanup_test_expert(expert_id: int):
    """
    Delete the test expert instance and all related data.
    """
    print_separator("CLEANING UP TEST EXPERT")
    
    with get_db() as session:
        # Get expert instance
        expert = session.get(ExpertInstance, expert_id)
        if not expert:
            print(f"⚠️  Expert instance {expert_id} not found (already deleted?)")
            return
        
        # Delete all transactions for this expert
        from sqlmodel import select
        transactions = session.exec(
            select(Transaction).where(Transaction.expert_id == expert_id)
        ).all()
        
        for trans in transactions:
            session.delete(trans)
            print(f"   Deleted transaction {trans.id}")
        
        # Delete expert settings
        settings = session.exec(
            select(ExpertSetting).where(ExpertSetting.instance_id == expert_id)
        ).all()
        
        for setting in settings:
            session.delete(setting)
        
        # Delete expert instance
        session.delete(expert)
        session.commit()
        
        print(f"✅ Deleted expert instance {expert_id} and all related data")

def verify_prerequisites(expert_id: int):
    """Verify test prerequisites are met"""
    print_separator("VERIFYING PREREQUISITES")
    
    with get_db() as session:
        # Check expert instance exists
        expert = session.get(ExpertInstance, expert_id)
        if not expert:
            raise ValueError(f"Expert instance {expert_id} not found!")
        print(f"✅ Found expert: {expert.expert} (ID: {expert.id})")
        print(f"   Account ID: {expert.account_id}")
        
        if expert.account_id != PAPER_ACCOUNT_ID:
            raise ValueError(f"Expert instance {expert_id} is not linked to account {PAPER_ACCOUNT_ID}!")
        
        # Check account exists via AccountDefinition
        account_def = session.get(AccountDefinition, PAPER_ACCOUNT_ID)
        if not account_def:
            raise ValueError(f"Account definition {PAPER_ACCOUNT_ID} not found!")
        print(f"✅ Found account: {account_def.name} (ID: {account_def.id})")
        
        # Get account instance to check balance
        account = get_account_instance_from_id(PAPER_ACCOUNT_ID)
        account_info = account.get_account_info()
        print(f"   Balance: ${float(account_info.cash):.2f}, Equity: ${float(account_info.equity):.2f}")
        
        # Check if symbol is enabled
        toolkit = SmartRiskManagerToolkit(expert_id, PAPER_ACCOUNT_ID)
        enabled_symbols = toolkit.expert.get_enabled_instruments()
        
        if not enabled_symbols:
            raise ValueError("No symbols are enabled in expert settings! Please enable at least one symbol.")
        
        # Verify TEST_SYMBOL is enabled
        if TEST_SYMBOL not in enabled_symbols:
            raise ValueError(f"Symbol {TEST_SYMBOL} is not enabled in expert settings!")
        
        print(f"✅ Symbol {TEST_SYMBOL} is enabled")
        
        # Get current price
        current_price = toolkit.get_current_price(TEST_SYMBOL)
        print(f"✅ Current {TEST_SYMBOL} price: ${current_price:.2f}")
        
        return toolkit, TEST_SYMBOL, current_price

def test_1_open_new_position(toolkit: SmartRiskManagerToolkit, symbol: str, current_price: float):
    """Test opening a new position with TP and SL"""
    print_separator("TEST 1: OPEN NEW POSITION")
    
    # Calculate TP/SL prices (conservative: TP=+10%, SL=-5%)
    tp_price = round(current_price * 1.10, 2)
    sl_price = round(current_price * 0.95, 2)
    
    print(f"Opening BUY position:")
    print(f"  Symbol: {symbol}")
    print(f"  Quantity: {TEST_QUANTITY}")
    print(f"  Current Price: ${current_price:.2f}")
    print(f"  Take Profit: ${tp_price:.2f} (+10%)")
    print(f"  Stop Loss: ${sl_price:.2f} (-5%)")
    
    result = toolkit.open_new_position(
        symbol=symbol,
        direction="BUY",
        quantity=TEST_QUANTITY,
        tp_price=tp_price,
        sl_price=sl_price,
        reason="Phase 2 test - open position with TP/SL"
    )
    
    print(f"\nResult: {result}")
    
    if result["success"]:
        transaction_id = result["transaction_id"]
        order_id = result["order_id"]
        
        print(f"✅ SUCCESS: Position opened")
        print(f"   Transaction ID: {transaction_id}")
        print(f"   Order ID: {order_id}")
        
        # Wait for order to process
        time.sleep(2)
        
        # Verify transaction and orders were created
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            if transaction:
                print(f"\n📊 Transaction Details:")
                print(f"   Status: {transaction.status}")
                print(f"   Quantity: {transaction.quantity}")
                open_price_display = f"${transaction.open_price:.2f}" if transaction.open_price else "Not filled yet"
                print(f"   Open Price: {open_price_display}")
                
                # List all orders
                print(f"\n📋 Associated Orders ({len(transaction.trading_orders)}):")
                for order in transaction.trading_orders:
                    order_desc = f"{order.side.value} {order.order_type.value}"
                    if order.limit_price:
                        order_desc += f" @ ${order.limit_price:.2f}"
                    if order.stop_price:
                        order_desc += f" @ ${order.stop_price:.2f}"
                    print(f"   - Order {order.id}: {order_desc} | Status: {order.status.value}")
        
        return transaction_id
    else:
        print(f"❌ FAILED: {result['message']}")
        return None

def test_2_update_stop_loss(toolkit: SmartRiskManagerToolkit, transaction_id: int, current_price: float):
    """Test updating stop loss for a transaction"""
    print_separator("TEST 2: UPDATE STOP LOSS")
    
    if not transaction_id:
        print("⚠️ SKIPPED: No transaction_id from previous test")
        return False
    
    # Move SL to breakeven (current price)
    new_sl_price = round(current_price, 2)
    
    print(f"Updating stop loss for transaction {transaction_id}")
    print(f"  New SL Price: ${new_sl_price:.2f} (breakeven)")
    
    result = toolkit.update_stop_loss(
        transaction_id=transaction_id,
        new_sl_price=new_sl_price,
        reason="Phase 2 test - move SL to breakeven"
    )
    
    print(f"\nResult: {result}")
    
    if result["success"]:
        print(f"✅ SUCCESS: Stop loss updated")
        print(f"   Old SL: ${result.get('old_sl_price', 'N/A')}")
        print(f"   New SL: ${result.get('new_sl_price', 'N/A')}")
        
        # Verify new SL order
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            if transaction:
                print(f"\n📋 Updated Orders:")
                for order in transaction.trading_orders:
                    if order.order_type.value in ["SELL_STOP", "BUY_STOP"]:
                        print(f"   - SL Order {order.id}: ${order.stop_price:.2f} | Status: {order.status.value}")
        
        return True
    else:
        print(f"❌ FAILED: {result['message']}")
        return False

def test_3_update_take_profit(toolkit: SmartRiskManagerToolkit, transaction_id: int, current_price: float):
    """Test updating take profit for a transaction"""
    print_separator("TEST 3: UPDATE TAKE PROFIT")
    
    if not transaction_id:
        print("⚠️ SKIPPED: No transaction_id from previous test")
        return False
    
    # Adjust TP to +5% instead of +10%
    new_tp_price = round(current_price * 1.05, 2)
    
    print(f"Updating take profit for transaction {transaction_id}")
    print(f"  New TP Price: ${new_tp_price:.2f} (+5%)")
    
    result = toolkit.update_take_profit(
        transaction_id=transaction_id,
        new_tp_price=new_tp_price,
        reason="Phase 2 test - adjust TP to +5%"
    )
    
    print(f"\nResult: {result}")
    
    if result["success"]:
        print(f"✅ SUCCESS: Take profit updated")
        print(f"   Old TP: ${result.get('old_tp_price', 'N/A')}")
        print(f"   New TP: ${result.get('new_tp_price', 'N/A')}")
        
        # Verify new TP order
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            if transaction:
                print(f"\n📋 Updated Orders:")
                for order in transaction.trading_orders:
                    if order.order_type.value in ["SELL_LIMIT", "BUY_LIMIT"]:
                        print(f"   - TP Order {order.id}: ${order.limit_price:.2f} | Status: {order.status.value}")
        
        return True
    else:
        print(f"❌ FAILED: {result['message']}")
        return False

def test_4_adjust_quantity_reduce(toolkit: SmartRiskManagerToolkit, transaction_id: int):
    """Test partial close (reducing quantity)"""
    print_separator("TEST 4: ADJUST QUANTITY (PARTIAL CLOSE)")
    
    if not transaction_id:
        print("⚠️ SKIPPED: No transaction_id from previous test")
        return False
    
    # Reduce quantity by half
    new_quantity = TEST_QUANTITY / 2.0
    
    print(f"Reducing position for transaction {transaction_id}")
    print(f"  Old Quantity: {TEST_QUANTITY}")
    print(f"  New Quantity: {new_quantity}")
    
    result = toolkit.adjust_quantity(
        transaction_id=transaction_id,
        new_quantity=new_quantity,
        reason="Phase 2 test - partial close (reduce by 50%)"
    )
    
    print(f"\nResult: {result}")
    
    if result["success"]:
        print(f"✅ SUCCESS: Quantity adjusted")
        print(f"   Old Quantity: {result.get('old_quantity', 'N/A')}")
        print(f"   New Quantity: {result.get('new_quantity', 'N/A')}")
        
        # Verify transaction quantity
        time.sleep(1)
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            if transaction:
                print(f"\n📊 Transaction Quantity: {transaction.quantity}")
        
        return True
    else:
        print(f"❌ FAILED: {result['message']}")
        return False

def test_5_close_position(toolkit: SmartRiskManagerToolkit, transaction_id: int):
    """Test closing the entire position"""
    print_separator("TEST 5: CLOSE POSITION")
    
    if not transaction_id:
        print("⚠️ SKIPPED: No transaction_id from previous test")
        return False
    
    print(f"Closing transaction {transaction_id}")
    
    result = toolkit.close_position(
        transaction_id=transaction_id,
        reason="Phase 2 test - close position"
    )
    
    print(f"\nResult: {result}")
    
    if result["success"]:
        print(f"✅ SUCCESS: Position closed")
        
        # Verify transaction status
        time.sleep(1)
        with get_db() as session:
            transaction = session.get(Transaction, transaction_id)
            if transaction:
                print(f"\n📊 Transaction Status: {transaction.status}")
                close_price_display = f"${transaction.close_price:.2f}" if transaction.close_price else "Not closed yet"
                pnl_display = f"${transaction.pnl:.2f}" if transaction.pnl else "$0.00"
                print(f"   Close Price: {close_price_display}")
                print(f"   P&L: {pnl_display}")
        
        return True
    else:
        print(f"❌ FAILED: {result['message']}")
        return False

def main():
    """Run all Phase 2 tests"""
    print("\n")
    print("╔" + "=" * 78 + "╗")
    print("║" + " " * 15 + "PHASE 2: SMART RISK TOOLKIT TRADING TESTS" + " " * 22 + "║")
    print("║" + " " * 30 + f"Symbol: {TEST_SYMBOL}" + " " * 36 + "║")
    print("╚" + "=" * 78 + "╝")
    
    expert_id = None
    
    try:
        # Set up dedicated test expert
        expert_id = setup_test_expert()
        
        # Verify prerequisites
        toolkit, test_symbol, current_price = verify_prerequisites(expert_id)
        
        # Run tests sequentially
        transaction_id = test_1_open_new_position(toolkit, test_symbol, current_price)
        
        if transaction_id:
            test_2_update_stop_loss(toolkit, transaction_id, current_price)
            test_3_update_take_profit(toolkit, transaction_id, current_price)
            test_4_adjust_quantity_reduce(toolkit, transaction_id)
            test_5_close_position(toolkit, transaction_id)
        
        print_separator("ALL TESTS COMPLETED")
        print("✅ Phase 2 trading tests finished successfully!")
        
    except Exception as e:
        print_separator("TEST EXECUTION ERROR")
        print(f"❌ Error: {e}")
        logger.error(f"Phase 2 test error: {e}", exc_info=True)
    
    finally:
        # Always cleanup, even if tests fail
        if expert_id is not None:
            try:
                cleanup_test_expert(expert_id)
            except Exception as cleanup_error:
                print(f"⚠️  Error during cleanup: {cleanup_error}")
                logger.error(f"Cleanup error: {cleanup_error}", exc_info=True)

if __name__ == "__main__":
    print("\n⚠️  WARNING: This test will execute REAL orders on your paper account!")
    print("⚠️  Make sure you are using a PAPER TRADING account, not live!")
    print("\nPress ENTER to continue or Ctrl+C to cancel...")
    input()
    
    main()

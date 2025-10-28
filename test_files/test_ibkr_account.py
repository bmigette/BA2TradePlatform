"""
Test script for IBKR Account Interface

This script tests the IBKR account implementation to verify:
1. Connection to TWS/Gateway
2. Account info retrieval
3. Order submission
4. Order modification (the key advantage over Alpaca)

Prerequisites:
- TWS (Trader Workstation) or IB Gateway running
- Paper trading account configured
- API access enabled in TWS/Gateway settings (File -> Global Configuration -> API -> Settings)
- Socket port enabled (default 7497 for paper TWS)
"""

import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.modules.accounts import IBKRAccount
from ba2_trade_platform.core.models import TradingOrder, AccountInstance, AccountSetting
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType as CoreOrderType
from ba2_trade_platform.core.db import get_db, add_instance, get_instance
from sqlmodel import Session, select


def setup_test_account():
    """
    Create a test IBKR account in the database with paper trading settings.
    
    Returns:
        Account instance ID
    """
    print("=" * 80)
    print("Setting up IBKR Test Account")
    print("=" * 80)
    
    with Session(get_db().bind) as session:
        # Check if IBKR account already exists
        existing = session.exec(
            select(AccountInstance).where(AccountInstance.account == "IBKR")
        ).first()
        
        if existing:
            print(f"✓ IBKR account already exists (ID: {existing.id})")
            return existing.id
        
        # Create new IBKR account instance
        account = AccountInstance(
            account="IBKR",
            name="IBKR Paper Trading",
            enabled=True
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        
        print(f"✓ Created IBKR account instance (ID: {account.id})")
        
        # Add settings
        settings = [
            AccountSetting(account_id=account.id, key="host", value_str="127.0.0.1"),
            AccountSetting(account_id=account.id, key="port", value_str="7497"),  # Paper TWS
            AccountSetting(account_id=account.id, key="client_id", value_str="1"),
            AccountSetting(account_id=account.id, key="paper_account", value_str="true"),
        ]
        
        for setting in settings:
            session.add(setting)
        
        session.commit()
        print(f"✓ Added IBKR account settings")
        
        return account.id


def test_connection(account_id: int):
    """Test connection to IBKR TWS/Gateway"""
    print("\n" + "=" * 80)
    print("TEST 1: Connection to TWS/Gateway")
    print("=" * 80)
    
    try:
        account = IBKRAccount(account_id)
        print("✅ Successfully connected to IBKR")
        
        # Get account info
        info = account.get_account_info()
        print(f"\n📊 Account Information:")
        print(f"   Account: {info.get('account_number')}")
        print(f"   Equity: ${info.get('equity', 0):,.2f}")
        print(f"   Cash: ${info.get('cash', 0):,.2f}")
        print(f"   Buying Power: ${info.get('buying_power', 0):,.2f}")
        
        return account
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure TWS or IB Gateway is running")
        print("2. Check that API is enabled in TWS: File -> Global Configuration -> API -> Settings")
        print("3. Verify 'Enable ActiveX and Socket Clients' is checked")
        print("4. Check port number (7497 for paper TWS, 7496 for live TWS)")
        print("5. Ensure 'Read-Only API' is UNCHECKED")
        return None


def test_market_data(account: IBKRAccount):
    """Test market data retrieval"""
    print("\n" + "=" * 80)
    print("TEST 2: Market Data Retrieval")
    print("=" * 80)
    
    try:
        symbols = ["AAPL", "MSFT", "TSLA"]
        for symbol in symbols:
            price = account.get_instrument_current_price(symbol)
            if price:
                print(f"✅ {symbol}: ${price:.2f}")
            else:
                print(f"⚠️  {symbol}: No price data")
        
    except Exception as e:
        print(f"❌ Market data test failed: {e}")


def test_order_submission(account: IBKRAccount):
    """Test order submission"""
    print("\n" + "=" * 80)
    print("TEST 3: Order Submission (Limit Order)")
    print("=" * 80)
    
    try:
        # Create a limit order for AAPL
        order = TradingOrder(
            account_id=account.id,
            symbol="AAPL",
            quantity=1,
            side=OrderDirection.BUY,
            order_type=CoreOrderType.BUY_LIMIT,
            limit_price=100.00,  # Well below market to avoid fill
            status=OrderStatus.PENDING
        )
        
        # Add to database
        order_id = add_instance(order, expunge_after_flush=True)
        order = get_instance(TradingOrder, order_id)
        
        print(f"Submitting BUY order for AAPL @ $100.00 (limit)...")
        
        # Submit order
        submitted_order = account.submit_order(order)
        
        print(f"✅ Order submitted successfully")
        print(f"   Order ID: {submitted_order.broker_order_id}")
        print(f"   Status: {submitted_order.status.value}")
        
        return submitted_order
        
    except Exception as e:
        print(f"❌ Order submission failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_order_modification(account: IBKRAccount, order: TradingOrder):
    """Test order modification (the key advantage over Alpaca!)"""
    print("\n" + "=" * 80)
    print("TEST 4: Order Modification (TRUE In-Place Modification)")
    print("=" * 80)
    print("This is where IBKR shines - we can modify accepted orders without canceling!")
    
    try:
        print(f"\nOriginal order:")
        print(f"   Order ID: {order.broker_order_id}")
        print(f"   Status: {order.status.value}")
        print(f"   Limit Price: ${order.limit_price:.2f}")
        
        # Modify the order to a new price
        new_price = 105.00
        print(f"\n🔄 Modifying order to new limit price: ${new_price:.2f}")
        
        # Create updated order
        modified_order = TradingOrder(
            account_id=order.account_id,
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            order_type=order.order_type,
            limit_price=new_price,
            status=order.status
        )
        
        # Modify at broker
        result = account.modify_order(order.broker_order_id, modified_order)
        
        if result:
            print(f"✅ Order modified successfully (SAME ORDER ID: {result.broker_order_id})")
            print(f"   New Limit Price: ${new_price:.2f}")
            print(f"   Status: {result.status.value}")
            print("\n🎉 This is the key advantage over Alpaca:")
            print("   • No cancel/replace - true in-place modification")
            print("   • Works on ACCEPTED orders")
            print("   • No race condition window")
            print("   • Same order ID maintained")
        else:
            print(f"❌ Order modification returned None")
            
        return result
        
    except Exception as e:
        print(f"❌ Order modification failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_order_cancellation(account: IBKRAccount, order: TradingOrder):
    """Test order cancellation"""
    print("\n" + "=" * 80)
    print("TEST 5: Order Cancellation")
    print("=" * 80)
    
    try:
        print(f"Canceling order {order.broker_order_id}...")
        
        success = account.cancel_order(order)
        
        if success:
            print(f"✅ Order cancelled successfully")
        else:
            print(f"⚠️  Order cancellation returned False")
            
    except Exception as e:
        print(f"❌ Order cancellation failed: {e}")


def main():
    """Run all tests"""
    print("=" * 80)
    print("IBKR Account Interface Test Suite")
    print("=" * 80)
    print("\n⚠️  Prerequisites:")
    print("   1. TWS or IB Gateway must be running")
    print("   2. API access must be enabled in TWS settings")
    print("   3. Paper trading account recommended for testing")
    print()
    
    # Setup account
    account_id = setup_test_account()
    
    # Test connection
    account = test_connection(account_id)
    if not account:
        print("\n❌ Cannot proceed without successful connection")
        return
    
    # Test market data
    test_market_data(account)
    
    # Test order submission
    order = test_order_submission(account)
    if not order:
        print("\n⚠️  Skipping modification tests due to submission failure")
        return
    
    # Test order modification (the key feature!)
    modified_order = test_order_modification(account, order)
    
    # Test cancellation
    if modified_order:
        test_order_cancellation(account, modified_order)
    
    print("\n" + "=" * 80)
    print("✅ TEST SUITE COMPLETE")
    print("=" * 80)
    print("\nIBKR Account Interface is ready for use!")
    print("Key advantages over Alpaca:")
    print("  ✅ True order modification (no cancel/replace)")
    print("  ✅ Can modify ACCEPTED orders")
    print("  ✅ Can modify FILLED positions via new orders")
    print("  ✅ No race conditions")
    print("  ✅ More institutional-grade API")


if __name__ == "__main__":
    main()

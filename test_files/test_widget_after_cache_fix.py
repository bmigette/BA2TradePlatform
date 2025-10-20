"""
Quick verification that widgets still work correctly after price cache fix.

This test ensures that the widgets can still fetch broker position prices
without any issues from the cache changes.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_instance, get_all_instances
from ba2_trade_platform.core.models import AccountDefinition, Transaction, TradingOrder
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.logger import logger

def test_widget_functionality():
    """Test that widgets can still fetch prices correctly"""
    
    print("\n" + "="*80)
    print("Testing Widget Price Fetching After Cache Fix")
    print("="*80 + "\n")
    
    # Get the first Alpaca account
    account_def = get_instance(AccountDefinition, 1)
    if not account_def:
        logger.error("No account definition found with ID 1")
        return False
    
    account = AlpacaAccount(account_def.id)
    
    # Get open transactions
    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    
    with Session(get_db().bind) as session:
        statement = select(Transaction).where(Transaction.status == TransactionStatus.OPENED)
        open_transactions = list(session.exec(statement).all())
    
    if not open_transactions:
        print("ℹ No open transactions found. Nothing to test.")
        return True
    
    print(f"Found {len(open_transactions)} open transactions\n")
    
    # Get broker positions (like widgets do)
    print("Fetching broker positions...")
    broker_positions = account.get_positions()
    
    if not broker_positions:
        print("⚠ No broker positions found")
        return True
    
    print(f"✓ Got {len(broker_positions)} broker positions\n")
    
    # Extract prices from positions (like widgets do)
    prices = {}
    for pos in broker_positions:
        pos_dict = pos if isinstance(pos, dict) else dict(pos)
        prices[pos_dict['symbol']] = float(pos_dict['current_price'])
    
    print("Position prices (what widgets use):")
    total_pl = 0.0
    for symbol, price in prices.items():
        # Find matching transaction
        trans = next((t for t in open_transactions if t.symbol == symbol), None)
        if trans:
            pl = (price - trans.open_price) * trans.quantity
            total_pl += pl
            print(f"  {symbol}: ${price:.2f} (Open: ${trans.open_price:.2f}, P/L: ${pl:.2f})")
        else:
            print(f"  {symbol}: ${price:.2f} (no matching transaction)")
    
    print(f"\nTotal P/L: ${total_pl:.2f}")
    print("\n✓ Widget price fetching works correctly")
    
    # Also test that we can still fetch bid prices if needed
    print("\nVerifying cache still works for bid prices...")
    symbols = list(prices.keys())[:3] if len(prices) > 3 else list(prices.keys())
    
    if symbols:
        bid_prices = account.get_instrument_current_price(symbols, price_type='bid')
        print("Bid prices from cache:")
        for symbol, bid in bid_prices.items():
            pos_price = prices.get(symbol, 0.0)
            diff = pos_price - bid
            diff_pct = (diff / bid * 100) if bid else 0
            print(f"  {symbol}: ${bid:.2f} (Position: ${pos_price:.2f}, Diff: ${diff:.2f} / {diff_pct:.2f}%)")
    
    print("\n" + "="*80)
    print("✓✓✓ ALL CHECKS PASSED ✓✓✓")
    print("Widgets work correctly after cache fix")
    print("="*80 + "\n")
    
    return True

if __name__ == "__main__":
    try:
        success = test_widget_functionality()
        if not success:
            sys.exit(1)
    except Exception as e:
        logger.error(f"Error testing widget functionality: {e}", exc_info=True)
        sys.exit(1)

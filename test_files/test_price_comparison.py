"""
Compare bid price, ask price, and position current_price from Alpaca to understand price discrepancies.
This helps explain why the widget showed -$1,689.82 with bid prices vs $144.56 with position prices.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.utils import get_account_instance_from_id
from sqlmodel import select

def compare_alpaca_prices():
    """Compare bid, ask, and position current_price from Alpaca."""
    
    print("="*100)
    print("ALPACA PRICE COMPARISON: Bid vs Ask vs Position Current Price")
    print("="*100)
    
    session = get_db()
    try:
        # Get all accounts
        accounts = session.exec(select(AccountDefinition)).all()
        
        for account_def in accounts:
            print(f"\n{'='*100}")
            print(f"ACCOUNT: {account_def.name} (ID: {account_def.id})")
            print(f"{'='*100}")
            
            # Get account interface
            account = get_account_instance_from_id(account_def.id, session=session)
            if not account:
                print("  ⚠️ Could not get account interface")
                continue
            
            # Get broker positions (with current_price)
            print("\n📊 Getting broker positions...")
            broker_positions = account.get_positions()
            if not broker_positions:
                print("  No broker positions")
                continue
            
            print(f"  Found {len(broker_positions)} positions")
            
            # Extract symbols and position prices
            symbols = []
            position_prices = {}
            for pos in broker_positions:
                pos_dict = pos if isinstance(pos, dict) else dict(pos)
                symbol = pos_dict['symbol']
                symbols.append(symbol)
                position_prices[symbol] = {
                    'current_price': float(pos_dict['current_price']),
                    'qty': float(pos_dict['qty']),
                    'avg_entry_price': float(pos_dict['avg_entry_price']),
                    'unrealized_pl': float(pos_dict['unrealized_pl'])
                }
            
            # Get bid/ask/mid prices using get_instrument_current_price with price_type
            print("\n📈 Fetching bid prices (what old widget was using)...")
            bid_prices = account.get_instrument_current_price(symbols, price_type='bid')
            
            print("📉 Fetching ask prices...")
            ask_prices = account.get_instrument_current_price(symbols, price_type='ask')
            
            print("📊 Fetching mid prices...")
            mid_prices = account.get_instrument_current_price(symbols, price_type='mid')
            
            print(f"  ✓ Fetched bid/ask/mid prices for {len(symbols)} symbols")
            
            # Compare prices
            print(f"\n{'='*100}")
            print("PRICE COMPARISON")
            print(f"{'='*100}")
            print(f"\n{'Symbol':<8} {'Bid Price':>12} {'Ask Price':>12} {'Mid Price':>12} {'Pos Price':>12} {'Spread':>10} {'Pos vs Bid':>12}")
            print(f"{'-'*100}")
            
            total_pl_with_bid = 0.0
            total_pl_with_ask = 0.0
            total_pl_with_mid = 0.0
            total_pl_with_pos = 0.0
            
            for symbol in sorted(symbols):
                pos_info = position_prices[symbol]
                bid_price = bid_prices.get(symbol, 0) or 0
                ask_price = ask_prices.get(symbol, 0) or 0
                mid_price = mid_prices.get(symbol, 0) or 0
                pos_price = pos_info['current_price']
                qty = pos_info['qty']
                avg_entry = pos_info['avg_entry_price']
                
                # Calculate spread
                spread = (ask_price - bid_price) if (bid_price and ask_price and bid_price > 0 and ask_price > 0) else 0
                spread_pct = (spread / bid_price * 100) if (bid_price and bid_price > 0) else 0
                
                # Calculate difference between position price and bid
                pos_vs_bid = pos_price - bid_price if bid_price else 0
                pos_vs_bid_pct = (pos_vs_bid / bid_price * 100) if bid_price else 0
                
                print(f"{symbol:<8} ${bid_price:>11.2f} ${ask_price:>11.2f} ${mid_price:>11.2f} ${pos_price:>11.2f} "
                      f"${spread:>8.2f} ({spread_pct:>4.1f}%) ${pos_vs_bid:>8.2f} ({pos_vs_bid_pct:>4.1f}%)")
                
                # Calculate P/L with different prices
                pl_bid = (bid_price - avg_entry) * qty if bid_price else 0
                pl_ask = (ask_price - avg_entry) * qty if ask_price else 0
                pl_mid = (mid_price - avg_entry) * qty if mid_price else 0
                pl_pos = (pos_price - avg_entry) * qty
                
                total_pl_with_bid += pl_bid
                total_pl_with_ask += pl_ask
                total_pl_with_mid += pl_mid
                total_pl_with_pos += pl_pos
            
            # Summary
            print(f"\n{'='*100}")
            print("P/L CALCULATION COMPARISON")
            print(f"{'='*100}")
            print(f"\nUsing BID prices:      ${total_pl_with_bid:>12,.2f}  (What old widget used)")
            print(f"Using ASK prices:      ${total_pl_with_ask:>12,.2f}")
            print(f"Using MID prices:      ${total_pl_with_mid:>12,.2f}  (Bid+Ask)/2")
            print(f"Using POS prices:      ${total_pl_with_pos:>12,.2f}  (What Alpaca uses - CORRECT)")
            print(f"\nBroker reported P/L:   ${sum(p['unrealized_pl'] for p in position_prices.values()):>12,.2f}")
            
            print(f"\n{'='*100}")
            print("DISCREPANCY ANALYSIS")
            print(f"{'='*100}")
            diff_bid = total_pl_with_bid - total_pl_with_pos
            diff_ask = total_pl_with_ask - total_pl_with_pos
            diff_mid = total_pl_with_mid - total_pl_with_pos
            
            print(f"\nBid vs Position:       ${diff_bid:>12,.2f}  {'❌' if abs(diff_bid) > 100 else '⚠️'}")
            print(f"Ask vs Position:       ${diff_ask:>12,.2f}  {'❌' if abs(diff_ask) > 100 else '⚠️'}")
            print(f"Mid vs Position:       ${diff_mid:>12,.2f}  {'✅' if abs(diff_mid) < 50 else '⚠️'}")
            
            print(f"\n💡 EXPLANATION:")
            print(f"   - Widget was using BID prices, causing ${diff_bid:,.2f} discrepancy")
            print(f"   - Updated widget now uses Position current_price (same as Alpaca)")
            print(f"   - Position price is typically close to mid-price or last trade price")
            print(f"   - Bid-ask spread causes the price difference")
            
    finally:
        session.close()

if __name__ == "__main__":
    compare_alpaca_prices()

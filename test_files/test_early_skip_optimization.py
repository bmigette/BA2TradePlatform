#!/usr/bin/env python3
"""
Test script to verify the early skip optimization in TradeRiskManagement.
This simulates the ASML scenario where order price exceeds per-instrument limit.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.TradeRiskManagement import TradeRiskManagement
from ba2_trade_platform.core.models import TradingOrder, ExpertRecommendation
from ba2_trade_platform.core.types import OrderDirection, OrderType, InstrumentType
from ba2_trade_platform.logger import logger

# Mock expert class
class MockExpert:
    def _get_enabled_instruments_config(self):
        return {}
    
    def get_instrument_settings(self):
        return {}

# Mock account class  
class MockAccount:
    def get_instrument_current_price(self, symbols):
        """Mock price fetcher that handles both single symbol and list of symbols."""
        prices = {
            "ASML": 1022.50,  # Expensive stock that should trigger early skip
            "AAPL": 150.00,   # Affordable stock
            "NVDA": 800.00,   # Moderately expensive
            "GOOGL": 200.00,  # Affordable stock  
            "TSLA": 250.00    # Affordable stock
        }
        
        # Handle bulk fetching (list of symbols)
        if isinstance(symbols, list):
            return {symbol: prices.get(symbol, 100.0) for symbol in symbols}
        
        # Handle single symbol
        return prices.get(symbols, 100.0)

def test_early_skip_optimization():
    """Test that expensive stocks are skipped early without processing."""
    print("\n=== Testing Early Skip Optimization ===")
    
    # Setup
    risk_mgmt = TradeRiskManagement()
    mock_expert = MockExpert()
    mock_account = MockAccount()
    
    # Create test orders with different price points
    orders = []
    recommendations = []
    
    # Order 1: ASML - expensive stock that should be early skipped (low ROI)
    asml_order = TradingOrder(
        id=1,
        symbol="ASML",
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        instrument_type=InstrumentType.STOCK
    )
    
    asml_rec = ExpertRecommendation(
        symbol="ASML",
        expected_profit_percent=5.0,  # LOW ROI - will NOT be in top 3
        confidence=85.0
    )
    
    # Order 2: AAPL - affordable stock with good ROI
    aapl_order = TradingOrder(
        id=2,
        symbol="AAPL", 
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        instrument_type=InstrumentType.STOCK
    )
    
    aapl_rec = ExpertRecommendation(
        symbol="AAPL",
        expected_profit_percent=12.0,
        confidence=75.0
    )
    
    # Order 3: NVDA - moderately expensive but high ROI
    nvda_order = TradingOrder(
        id=3,
        symbol="NVDA",
        direction=OrderDirection.BUY, 
        order_type=OrderType.MARKET,
        quantity=1,
        instrument_type=InstrumentType.STOCK
    )
    
    nvda_rec = ExpertRecommendation(
        symbol="NVDA",
        expected_profit_percent=18.0,  # High ROI - should be in top 3
        confidence=80.0
    )
    
    # Order 4: GOOGL - affordable with good ROI
    googl_order = TradingOrder(
        id=4,
        symbol="GOOGL",
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        instrument_type=InstrumentType.STOCK
    )
    
    googl_rec = ExpertRecommendation(
        symbol="GOOGL",
        expected_profit_percent=15.0,  # High ROI - should be in top 3
        confidence=78.0
    )
    
    # Order 5: TSLA - affordable with excellent ROI
    tsla_order = TradingOrder(
        id=5,
        symbol="TSLA",
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        instrument_type=InstrumentType.STOCK
    )
    
    tsla_rec = ExpertRecommendation(
        symbol="TSLA",
        expected_profit_percent=20.0,  # Highest ROI - should be in top 3
        confidence=82.0
    )
    
    # Create prioritized list (highest ROI first)
    prioritized_orders = [
        (tsla_order, tsla_rec),   # 20% ROI - Top 1
        (nvda_order, nvda_rec),   # 18% ROI - Top 2  
        (googl_order, googl_rec), # 15% ROI - Top 3
        (aapl_order, aapl_rec),   # 12% ROI - Not top 3
        (asml_order, asml_rec)    # 5% ROI  - Not top 3, expensive
    ]
    
    # Test parameters
    total_balance = 2000.0
    max_equity_per_instrument = 500.0  # ASML ($1022.50) and NVDA ($800) exceed this
    existing_allocations = {}
    
    print(f"Total balance: ${total_balance:.2f}")
    print(f"Max equity per instrument: ${max_equity_per_instrument:.2f}")
    print(f"ASML price: ${mock_account.get_instrument_current_price('ASML'):.2f} (expensive, low ROI)")
    print(f"NVDA price: ${mock_account.get_instrument_current_price('NVDA'):.2f} (expensive, high ROI - top 3)")
    print(f"GOOGL price: ${mock_account.get_instrument_current_price('GOOGL'):.2f} (affordable, high ROI - top 3)")
    print(f"TSLA price: ${mock_account.get_instrument_current_price('TSLA'):.2f} (affordable, highest ROI - top 3)")
    print(f"AAPL price: ${mock_account.get_instrument_current_price('AAPL'):.2f} (affordable, medium ROI)")
    
    print(f"\nExpected outcome:")
    print(f"- ASML: Early skip (expensive + not in top 3 ROI)")
    print(f"- NVDA: Top-3 exception (expensive but top 3 ROI)")  
    print(f"- Others: Normal allocation")
    
    # Execute risk management
    try:
        orders_to_update, orders_to_delete = risk_mgmt._calculate_order_quantities(
            prioritized_orders=prioritized_orders,
            total_virtual_balance=total_balance,
            max_equity_per_instrument=max_equity_per_instrument,
            existing_allocations=existing_allocations,
            account=mock_account,
            expert=mock_expert
        )
        
        # Combine both lists for analysis
        all_orders = orders_to_update + orders_to_delete
        
        print(f"\n=== Results ===")
        print(f"Orders to update: {len(orders_to_update)}")
        print(f"Orders to delete: {len(orders_to_delete)}")
        print(f"Total orders processed: {len(all_orders)}")
        
        for order in all_orders:
            price = mock_account.get_instrument_current_price(order.symbol)
            status = "✓ ALLOCATED" if order.quantity > 0 else "✗ EARLY SKIP"
            print(f"{status}: {order.symbol} - Quantity: {order.quantity}, Price: ${price:.2f}")
            
        # Verify expected behavior
        asml_qty = next((o.quantity for o in all_orders if o.symbol == "ASML"), None)
        nvda_qty = next((o.quantity for o in all_orders if o.symbol == "NVDA"), None)
        aapl_qty = next((o.quantity for o in all_orders if o.symbol == "AAPL"), None)
        googl_qty = next((o.quantity for o in all_orders if o.symbol == "GOOGL"), None)
        tsla_qty = next((o.quantity for o in all_orders if o.symbol == "TSLA"), None)
        
        print(f"\n=== Verification ===")
        
        # ASML should be early skipped (price > per-instrument limit, NOT in top 3 ROI)
        if asml_qty == 0:
            print("✓ ASML correctly early skipped (expensive + not in top 3 ROI)")
        else:
            print(f"✗ ASML should have been early skipped but got quantity {asml_qty}")
            
        # NVDA should get top-3 exception (high ROI despite price)
        if nvda_qty > 0:
            print("✓ NVDA correctly allocated (top-3 ROI exception)")
        else:
            print("✗ NVDA should have been allocated with top-3 ROI exception")
            
        # GOOGL should be allocated normally (affordable + top 3 ROI)
        if googl_qty > 0:
            print("✓ GOOGL correctly allocated (affordable + top 3 ROI)")
        else:
            print("✗ GOOGL should have been allocated")
            
        # TSLA should be allocated normally (affordable + highest ROI)
        if tsla_qty > 0:
            print("✓ TSLA correctly allocated (affordable + highest ROI)")
        else:
            print("✗ TSLA should have been allocated")
            
        # AAPL should be allocated normally (affordable)
        if aapl_qty > 0:
            print("✓ AAPL correctly allocated (affordable)")
        else:
            print("✗ AAPL should have been allocated but got quantity 0")
            
    except Exception as e:
        logger.error(f"Test failed with error: {e}", exc_info=True)
        print(f"✗ Test failed: {e}")
        return False
        
    print("\n=== Test Complete ===")
    return True

if __name__ == "__main__":
    test_early_skip_optimization()
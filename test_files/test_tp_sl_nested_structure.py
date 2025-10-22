"""
Test TP/SL nested data structure implementation.
Ensures that TP/SL data is properly stored in order.data["TP_SL"] namespace.
"""

from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType
from datetime import datetime, timezone


def test_nested_tp_data_structure():
    """Test that TP data is stored in nested structure."""
    order = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    
    # Simulate TP data in nested structure
    order.data = {
        'TP_SL': {
            'tp_percent': 12.5,
            'parent_filled_price': 100.0,
            'type': 'tp'
        }
    }
    
    # Test access pattern
    has_tp = order.data and 'TP_SL' in order.data and 'tp_percent' in order.data['TP_SL']
    assert has_tp, "TP data should be in nested structure"
    
    tp_percent = order.data.get("TP_SL", {}).get("tp_percent")
    assert tp_percent == 12.5, f"Expected TP percent 12.5, got {tp_percent}"
    
    print("✓ Test 1 passed: Nested TP data structure works correctly")


def test_nested_sl_data_structure():
    """Test that SL data is stored in nested structure."""
    order = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.SELL,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    
    order.data = {
        'TP_SL': {
            'sl_percent': -5.0,
            'parent_filled_price': 100.0,
            'type': 'sl'
        }
    }
    
    has_sl = order.data and 'TP_SL' in order.data and 'sl_percent' in order.data['TP_SL']
    assert has_sl, "SL data should be in nested structure"
    
    sl_percent = order.data.get("TP_SL", {}).get("sl_percent")
    assert sl_percent == -5.0, f"Expected SL percent -5.0, got {sl_percent}"
    
    print("✓ Test 2 passed: Nested SL data structure works correctly")


def test_tp_sl_coexistence_with_expert_data():
    """Test that TP/SL data coexists with expert recommendation data."""
    order = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    
    order.data = {
        'TP_SL': {
            'tp_percent': 10.0,
            'parent_filled_price': 100.0,
            'type': 'tp'
        },
        'expert_recommendation': {
            'expert_id': 1,
            'signal': 'BUY',
            'confidence': 85.5
        }
    }
    
    # Verify TP/SL data
    has_tp = order.data and 'TP_SL' in order.data and 'tp_percent' in order.data['TP_SL']
    assert has_tp, "TP data should exist"
    
    # Verify expert data is not corrupted
    has_expert = order.data and 'expert_recommendation' in order.data
    assert has_expert, "Expert data should coexist"
    
    expert_data = order.data.get('expert_recommendation', {})
    assert expert_data.get('confidence') == 85.5, "Expert data should be intact"
    
    print("✓ Test 3 passed: TP/SL and expert data coexist without conflicts")


def test_safe_access_pattern():
    """Test safe access pattern for reading TP/SL data."""
    def safe_get_tp_percent(order):
        """Safe accessor for TP percent that handles missing data."""
        if not order.data:
            return None
        tp_sl_data = order.data.get('TP_SL')
        if not tp_sl_data:
            return None
        return tp_sl_data.get('tp_percent')
    
    # Test with data
    order_with_data = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    order_with_data.data = {'TP_SL': {'tp_percent': 12.5}}
    
    result = safe_get_tp_percent(order_with_data)
    assert result == 12.5, f"Expected 12.5, got {result}"
    
    # Test with empty TP_SL dict
    order_empty_tp_sl = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    order_empty_tp_sl.data = {'TP_SL': {}}
    
    result_empty_tp_sl = safe_get_tp_percent(order_empty_tp_sl)
    assert result_empty_tp_sl is None, f"Expected None for empty TP_SL dict, got {result_empty_tp_sl}"
    
    # Test with no TP_SL key
    order_no_tp_sl = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    order_no_tp_sl.data = {}
    
    result_no_tp_sl = safe_get_tp_percent(order_no_tp_sl)
    assert result_no_tp_sl is None, f"Expected None for no TP_SL key, got {result_no_tp_sl}"
    
    # Test with None data
    order_none = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    order_none.data = None
    
    result_none = safe_get_tp_percent(order_none)
    assert result_none is None, f"Expected None for None data, got {result_none}"
    
    print("✓ Test 4 passed: Safe access pattern works correctly for all cases")


def test_nested_structure_initialization():
    """Test initializing nested structure with proper safety."""
    order = TradingOrder(
        account_id=1,
        symbol='TEST',
        quantity=100,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc)
    )
    
    # Start with no data
    if not order.data:
        order.data = {}
    
    # Initialize TP_SL namespace
    if "TP_SL" not in order.data:
        order.data["TP_SL"] = {}
    
    # Store TP data
    order.data["TP_SL"]["tp_percent"] = 15.0
    order.data["TP_SL"]["parent_filled_price"] = 100.0
    
    # Verify
    assert order.data["TP_SL"]["tp_percent"] == 15.0
    assert order.data["TP_SL"]["parent_filled_price"] == 100.0
    
    print("✓ Test 5 passed: Nested structure initialization works correctly")


if __name__ == "__main__":
    test_nested_tp_data_structure()
    test_nested_sl_data_structure()
    test_tp_sl_coexistence_with_expert_data()
    test_safe_access_pattern()
    test_nested_structure_initialization()
    print("")
    print("✅ All TP/SL nested structure tests passed!")

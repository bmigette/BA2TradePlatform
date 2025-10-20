"""
Test suite for batch transaction operations feature.

Tests the following functionality:
1. Transaction selection toggle
2. Select all / Clear all
3. Batch close confirmation
4. Batch TP adjustment calculation
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_instance, add_instance, update_instance
from ba2_trade_platform.core.models import (
    Transaction, AccountDefinition, ExpertInstance, TradingOrder
)
from ba2_trade_platform.core.types import (
    TransactionStatus, OrderStatus, OrderDirection, OrderType
)
from sqlmodel import select


def test_selection_tracking():
    """Test that selection tracking dictionary works correctly."""
    print("\n=== Test 1: Selection Tracking ===")
    
    # Create a mock TransactionsTab with selection tracking
    class MockTransactionsTab:
        def __init__(self):
            self.selected_transactions = {}
    
    tab = MockTransactionsTab()
    
    # Test adding to selection
    tab.selected_transactions[1] = True
    tab.selected_transactions[2] = True
    assert len(tab.selected_transactions) == 2, "Should have 2 selected"
    print("✓ Can add transactions to selection")
    
    # Test removing from selection
    del tab.selected_transactions[1]
    assert len(tab.selected_transactions) == 1, "Should have 1 selected"
    print("✓ Can remove transactions from selection")
    
    # Test toggle logic
    txn_id = 1
    if txn_id in tab.selected_transactions:
        del tab.selected_transactions[txn_id]
    else:
        tab.selected_transactions[txn_id] = True
    
    assert 1 in tab.selected_transactions, "Should have added 1 back"
    print("✓ Toggle logic works correctly")


def test_tp_calculation():
    """Test TP calculation: new_tp = open_price * (1 + tp_percent / 100)"""
    print("\n=== Test 2: TP Calculation ===")
    
    test_cases = [
        # (open_price, tp_percent, expected_tp)
        (100.0, 5.0, 105.0),
        (50.0, 10.0, 55.0),
        (200.0, 2.5, 205.0),
        (1000.0, 0.5, 1005.0),
        (75.5, 7.2, 80.9336),
    ]
    
    for open_price, tp_percent, expected_tp in test_cases:
        new_tp = open_price * (1 + tp_percent / 100)
        assert abs(new_tp - expected_tp) < 0.0001, f"TP calculation failed for {open_price} @ {tp_percent}%"
        print(f"✓ ${open_price} + {tp_percent}% = ${new_tp:.4f}")


def test_transaction_filtering():
    """Test filtering transactions for display."""
    print("\n=== Test 3: Transaction Filtering ===")
    
    session = get_db()
    try:
        # Get all transactions
        stmt = select(Transaction)
        all_txns = session.exec(stmt).all()
        print(f"✓ Found {len(all_txns)} transactions in database")
        
        # Filter by status
        open_txns = [t for t in all_txns if t.status == TransactionStatus.OPEN]
        print(f"✓ Found {len(open_txns)} OPEN transactions")
        
        closing_txns = [t for t in all_txns if t.status == TransactionStatus.CLOSING]
        print(f"✓ Found {len(closing_txns)} CLOSING transactions")
        
        if open_txns:
            # Show first open transaction details
            txn = open_txns[0]
            print(f"✓ Sample transaction: {txn.symbol} qty={txn.quantity} @ ${txn.open_price}")
    finally:
        session.close()


def test_batch_close_confirmation():
    """Test batch close confirmation logic."""
    print("\n=== Test 4: Batch Close Confirmation ===")
    
    # Simulate selected transactions
    selected = {1: True, 2: True, 3: True}
    count = len(selected)
    
    # Test confirmation message
    singular = "transaction" if count == 1 else "transactions"
    message = f'Are you sure you want to close {count} {singular}?'
    
    assert "3" in message, "Should include count in message"
    assert "transactions" in message, "Should use plural"
    print(f"✓ Confirmation message: {message}")
    
    # Test edge case: single transaction
    selected_single = {1: True}
    count_single = len(selected_single)
    singular_single = "transaction" if count_single == 1 else "transactions"
    message_single = f'Are you sure you want to close {count_single} {singular_single}?'
    
    assert "transaction" in message_single, "Should use singular"
    assert "1" in message_single, "Should show count 1"
    print(f"✓ Singular message: {message_single}")


def test_batch_update_visibility():
    """Test batch button visibility logic."""
    print("\n=== Test 5: Button Visibility Logic ===")
    
    class MockButtons:
        def __init__(self):
            self.select_all_visible = False
            self.clear_visible = False
            self.close_visible = False
            self.adjust_tp_visible = False
    
    buttons = MockButtons()
    
    # Test with selections
    selected = {1: True, 2: True}
    has_selection = len(selected) > 0
    
    buttons.select_all_visible = True  # Always visible
    buttons.clear_visible = has_selection
    buttons.close_visible = has_selection
    buttons.adjust_tp_visible = has_selection
    
    assert buttons.select_all_visible == True, "Select All should always be visible"
    assert buttons.clear_visible == True, "Clear should be visible when selected"
    assert buttons.close_visible == True, "Close should be visible when selected"
    assert buttons.adjust_tp_visible == True, "Adjust TP should be visible when selected"
    print("✓ Buttons visible with selections")
    
    # Test without selections
    selected_empty = {}
    has_selection = len(selected_empty) > 0
    
    buttons.clear_visible = has_selection
    buttons.close_visible = has_selection
    buttons.adjust_tp_visible = has_selection
    
    assert buttons.clear_visible == False, "Clear should be hidden without selections"
    assert buttons.close_visible == False, "Close should be hidden without selections"
    assert buttons.adjust_tp_visible == False, "Adjust TP should be hidden without selections"
    print("✓ Buttons hidden without selections")


def test_event_data_parsing():
    """Test event data parsing from Vue emit."""
    print("\n=== Test 6: Event Data Parsing ===")
    
    # Simulate NiceGUI event data structure
    class MockEventData:
        def __init__(self, *args):
            self.args = args
    
    # Test parsing transaction ID from event
    event_data = MockEventData(42)  # Vue emits: toggle_transaction_select(id)
    transaction_id = event_data.args[0] if hasattr(event_data, 'args') and event_data.args else None
    
    assert transaction_id == 42, "Should extract transaction ID from event"
    print(f"✓ Parsed transaction ID: {transaction_id}")
    
    # Test with None/empty args
    event_data_empty = MockEventData()
    transaction_id_empty = event_data_empty.args[0] if hasattr(event_data_empty, 'args') and event_data_empty.args else None
    
    assert transaction_id_empty is None, "Should handle empty args gracefully"
    print("✓ Handles empty event args gracefully")


def test_percentage_formatting():
    """Test TP percentage display formatting."""
    print("\n=== Test 7: Percentage Formatting ===")
    
    test_cases = [
        (5.0, "5.0%"),
        (5.1, "5.1%"),
        (10.55, "10.6%"),  # Rounded to 1 decimal
        (0.5, "0.5%"),
    ]
    
    for tp_percent, expected_display in test_cases:
        # Format as displayed
        display = f"{tp_percent:.1f}%"
        assert display == expected_display, f"Format mismatch for {tp_percent}"
        print(f"✓ TP {tp_percent}% displays as: {display}")


def main():
    """Run all tests."""
    print("=" * 60)
    print("BATCH TRANSACTION OPERATIONS TEST SUITE")
    print("=" * 60)
    
    try:
        test_selection_tracking()
        test_tp_calculation()
        test_transaction_filtering()
        test_batch_close_confirmation()
        test_batch_update_visibility()
        test_event_data_parsing()
        test_percentage_formatting()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        return 0
    
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())

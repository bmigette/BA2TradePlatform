"""
Test script for symbols_exist and filter_supported_symbols methods.

Tests the new AccountInterface.symbols_exist() method and the 
filter_supported_symbols() helper method with various symbol types.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.utils import get_account_instance_from_id
from sqlmodel import select


def test_symbols_exist():
    """Test the symbols_exist method with various symbol types."""
    print("=" * 60)
    print("Testing symbols_exist() method")
    print("=" * 60)
    
    # Get the first available account
    with get_db() as session:
        account_instance = session.exec(select(AccountDefinition).limit(1)).first()
        
    if not account_instance:
        print("ERROR: No account found in database")
        return False
    
    print(f"\nUsing account: {account_instance.name} (ID: {account_instance.id})")
    
    # Get account interface instance
    account = get_account_instance_from_id(account_instance.id)
    if not account:
        print("ERROR: Could not get account interface instance")
        return False
    
    # Test cases: mix of valid, invalid, and edge case symbols
    test_symbols = [
        # Standard valid symbols
        "AAPL",
        "MSFT", 
        "GOOGL",
        "NVDA",
        "TSLA",
        
        # Berkshire Hathaway variants (known issue from logs)
        "BRK.B",   # Alpaca format
        "BRK/B",   # Alternative format that should be normalized
        "BRK.A",   # Class A shares
        
        # Invalid/Non-existent symbols
        "INVALID123",
        "NOTREAL",
        "ZZZZZ",
        
        # Other potential edge cases
        "CMG",     # Chipotle (from log)
        "COF",     # Capital One (from log)
        "DRQ",     # Dril-Quip (from log - may or may not exist)
        "ZTS",     # Zoetis (from log)
    ]
    
    print(f"\nTesting {len(test_symbols)} symbols: {test_symbols}")
    print("-" * 60)
    
    # Test symbols_exist
    results = account.symbols_exist(test_symbols)
    
    print("\nResults from symbols_exist():")
    print("-" * 40)
    
    supported = []
    unsupported = []
    
    for symbol in test_symbols:
        status = results.get(symbol, False)
        status_str = "✓ SUPPORTED" if status else "✗ NOT SUPPORTED"
        print(f"  {symbol:12} : {status_str}")
        
        if status:
            supported.append(symbol)
        else:
            unsupported.append(symbol)
    
    print("-" * 40)
    print(f"Supported: {len(supported)} symbols: {supported}")
    print(f"Unsupported: {len(unsupported)} symbols: {unsupported}")
    
    return True


def test_filter_supported_symbols():
    """Test the filter_supported_symbols helper method."""
    print("\n" + "=" * 60)
    print("Testing filter_supported_symbols() method")
    print("=" * 60)
    
    # Get the first available account
    with get_db() as session:
        account_instance = session.exec(select(AccountDefinition).limit(1)).first()
        
    if not account_instance:
        print("ERROR: No account found in database")
        return False
    
    account = get_account_instance_from_id(account_instance.id)
    if not account:
        print("ERROR: Could not get account interface instance")
        return False
    
    # Simulate symbols from FMPSenateTraderCopy (from the log message)
    test_symbols = ['BRK.B', 'BRK/B', 'CMG', 'COF', 'CSX', 'DRQ', 'FI', 'MSFT', 'PNR', 'TMUS', 'ZTS']
    
    print(f"\nInput symbols (from FMP Senate data): {test_symbols}")
    print("-" * 60)
    
    # Test filter_supported_symbols
    supported = account.filter_supported_symbols(test_symbols, log_prefix="TEST")
    
    print(f"\nFiltered result: {supported}")
    print(f"Filtered out: {set(test_symbols) - set(supported)}")
    
    return True


def test_empty_and_edge_cases():
    """Test edge cases like empty lists."""
    print("\n" + "=" * 60)
    print("Testing edge cases")
    print("=" * 60)
    
    # Get the first available account
    with get_db() as session:
        account_instance = session.exec(select(AccountDefinition).limit(1)).first()
        
    if not account_instance:
        print("ERROR: No account found in database")
        return False
    
    account = get_account_instance_from_id(account_instance.id)
    if not account:
        print("ERROR: Could not get account interface instance")
        return False
    
    # Test 1: Empty list
    print("\nTest 1: Empty symbol list")
    result = account.symbols_exist([])
    print(f"  symbols_exist([]) = {result}")
    assert result == {}, f"Expected empty dict, got {result}"
    print("  ✓ PASSED")
    
    # Test 2: Empty filter
    print("\nTest 2: Empty filter list")
    result = account.filter_supported_symbols([])
    print(f"  filter_supported_symbols([]) = {result}")
    assert result == [], f"Expected empty list, got {result}"
    print("  ✓ PASSED")
    
    # Test 3: Single valid symbol
    print("\nTest 3: Single valid symbol")
    result = account.symbols_exist(["AAPL"])
    print(f"  symbols_exist(['AAPL']) = {result}")
    assert "AAPL" in result and result["AAPL"] == True, f"Expected AAPL: True, got {result}"
    print("  ✓ PASSED")
    
    # Test 4: Single invalid symbol
    print("\nTest 4: Single invalid symbol")
    result = account.symbols_exist(["NOTAREALSYMBOL123"])
    print(f"  symbols_exist(['NOTAREALSYMBOL123']) = {result}")
    assert "NOTAREALSYMBOL123" in result and result["NOTAREALSYMBOL123"] == False, f"Expected NOTAREALSYMBOL123: False, got {result}"
    print("  ✓ PASSED")
    
    return True


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("SYMBOL VALIDATION TEST SUITE")
    print("=" * 60)
    
    all_passed = True
    
    try:
        if not test_symbols_exist():
            all_passed = False
    except Exception as e:
        print(f"ERROR in test_symbols_exist: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    try:
        if not test_filter_supported_symbols():
            all_passed = False
    except Exception as e:
        print(f"ERROR in test_filter_supported_symbols: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    try:
        if not test_empty_and_edge_cases():
            all_passed = False
    except Exception as e:
        print(f"ERROR in test_empty_and_edge_cases: {e}")
        import traceback
        traceback.print_exc()
        all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED ✓")
    else:
        print("SOME TESTS FAILED ✗")
    print("=" * 60)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

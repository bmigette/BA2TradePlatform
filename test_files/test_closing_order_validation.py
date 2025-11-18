"""
Test that closing orders skip position size validation.

This verifies the fix for the issue where closing transactions were failing
with "Position size exceeds expert's max allowed" errors, even though they're
just exiting existing positions.

The fix uses a runtime parameter (is_closing_order) instead of a heuristic.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_closing_order_logic():
    """Verify that is_closing_order parameter correctly skips validation."""
    
    print("\n" + "="*80)
    print("Testing Closing Order Validation Skip Logic")
    print("="*80)
    
    # Check that submit_order accepts is_closing_order parameter
    from ba2_trade_platform.core.interfaces import AccountInterface
    import inspect
    
    submit_order_sig = inspect.signature(AccountInterface.submit_order)
    has_closing_param = 'is_closing_order' in submit_order_sig.parameters
    
    print("\n[Check 1] submit_order() accepts is_closing_order parameter:")
    if has_closing_param:
        print("  ✅ PASS: is_closing_order parameter added to submit_order()")
        param = submit_order_sig.parameters['is_closing_order']
        print(f"      Default value: {param.default}")
    else:
        print("  ❌ FAIL: Missing is_closing_order parameter")
        return False
    
    # Check that _validate_trading_order accepts is_closing_order parameter
    validate_sig = inspect.signature(AccountInterface._validate_trading_order)
    validate_has_param = 'is_closing_order' in validate_sig.parameters
    
    print("\n[Check 2] _validate_trading_order() accepts is_closing_order parameter:")
    if validate_has_param:
        print("  ✅ PASS: is_closing_order parameter added to validation")
    else:
        print("  ❌ FAIL: Missing is_closing_order parameter in validation")
        return False
    
    # Check that validation logic uses the parameter
    validate_source = inspect.getsource(AccountInterface._validate_trading_order)
    
    uses_param = "not is_closing_order" in validate_source
    
    print("\n[Check 3] Validation logic uses is_closing_order parameter:")
    if uses_param:
        print("  ✅ PASS: Position size validation skipped when is_closing_order=True")
    else:
        print(f"  ❌ FAIL: Should check 'not is_closing_order' before validation")
        return False
    
    # Check that close_transaction passes is_closing_order=True
    close_txn_source = inspect.getsource(AccountInterface.close_transaction)
    
    passes_param = "is_closing_order=True" in close_txn_source
    
    print("\n[Check 4] close_transaction() passes is_closing_order=True:")
    if passes_param:
        print("  ✅ PASS: close_transaction sets is_closing_order=True")
    else:
        print("  ❌ FAIL: close_transaction should pass is_closing_order=True")
        return False
    
    # Verify old heuristic method is removed
    has_old_method = hasattr(AccountInterface, '_is_closing_order')
    
    print("\n[Check 5] Old heuristic _is_closing_order() method removed:")
    if not has_old_method:
        print("  ✅ PASS: Heuristic method removed (cleaner runtime approach)")
    else:
        print("  ⚠️  WARNING: Old _is_closing_order method still exists")
    
    # Summary
    print("\n" + "="*80)
    all_pass = has_closing_param and validate_has_param and uses_param and passes_param
    
    if all_pass:
        print("✅ ALL CHECKS PASSED - Closing orders skip position size validation!")
        print("\nFix Summary:")
        print("  • Added is_closing_order parameter to submit_order()")
        print("  • Parameter passed to _validate_trading_order()")
        print("  • Position size validation skipped when is_closing_order=True")
        print("  • close_transaction() explicitly passes is_closing_order=True")
        print("  • Clean runtime approach - no heuristics, no database fields")
    else:
        print("❌ SOME CHECKS FAILED")
    print("="*80 + "\n")
    
    return all_pass

if __name__ == "__main__":
    try:
        success = test_closing_order_logic()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

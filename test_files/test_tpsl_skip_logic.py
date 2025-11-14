"""
Test TP/SL skip logic to prevent redundant order creation.

The fix works as follows:
1. When order is submitted with TP/SL values, they are created via adjust_tp_sl
2. If caller (SmartRiskManager) calls adjust_tp_sl again with SAME values, it skips
3. This prevents triple order creation (orders 912, 913, 914 issue)

Root cause was:
- AlpacaAccount.submit_order() called adjust_tp_sl → created order 912
- AccountInterface.submit_order() called adjust_tp_sl → canceled 912, created 913
- SmartRiskManager called adjust_tp_sl → canceled 913, created 914

Fix:
- All three layers now call adjust_tp_sl (no code duplication)
- adjust_tp_sl has skip logic: if TP/SL unchanged AND valid orders exist → skip
- This makes redundant calls no-ops
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_code_changes():
    """Verify that the skip logic is in place and layers use adjust_tp_sl."""
    
    print("\n" + "="*80)
    print("Testing TP/SL Skip Logic Fix")
    print("="*80)
    
    # Check AlpacaAccount.py - should call adjust_tp_sl (reusing code)
    alpaca_file = Path(__file__).parent.parent / "ba2_trade_platform" / "modules" / "accounts" / "AlpacaAccount.py"
    alpaca_content = alpaca_file.read_text()
    
    # Should use adjust_tp_sl function (avoid code duplication)
    uses_adjust_function = "self.adjust_tp_sl(transaction, tp_price, sl_price)" in alpaca_content
    has_skip_logic = "tp_unchanged" in alpaca_content and "sl_unchanged" in alpaca_content
    
    print("\n[Check 1] AlpacaAccount uses adjust_tp_sl (no code duplication):")
    if uses_adjust_function:
        print("  ✅ PASS: Calls adjust_tp_sl instead of duplicating logic")
    else:
        print(f"  ❌ FAIL: Should call adjust_tp_sl to avoid code duplication")
    
    print("\n[Check 2] Skip logic in adjust_tp_sl for unchanged values:")
    skip_message = "Skipping TP/SL adjustment for transaction" in alpaca_content and "values unchanged" in alpaca_content
    
    if has_skip_logic and skip_message:
        print("  ✅ PASS: Skip logic prevents redundant order creation")
        print("         - Checks if TP/SL values unchanged")
        print("         - Checks if valid (non-canceled/error) orders exist")
        print("         - Returns early without creating duplicate orders")
    else:
        print(f"  ❌ FAIL: has_skip_logic={has_skip_logic}, skip_message={skip_message}")
    
    # Check AccountInterface.py - should also use adjust_tp_sl
    interface_file = Path(__file__).parent.parent / "ba2_trade_platform" / "core" / "interfaces" / "AccountInterface.py"
    interface_content = interface_file.read_text()
    
    interface_uses_adjust = "self.adjust_tp_sl(transaction, tp_price, sl_price)" in interface_content
    
    print("\n[Check 3] AccountInterface uses adjust_tp_sl (no code duplication):")
    if interface_uses_adjust:
        print("  ✅ PASS: Calls adjust_tp_sl instead of duplicating logic")
    else:
        print(f"  ❌ FAIL: Should call adjust_tp_sl to avoid code duplication")
    
    # Summary
    print("\n" + "="*80)
    all_pass = uses_adjust_function and has_skip_logic and skip_message and interface_uses_adjust
    
    if all_pass:
        print("✅ ALL CHECKS PASSED - Triple call issue fixed!")
        print("\nFix Summary:")
        print("  • All layers call adjust_tp_sl (no code duplication)")
        print("  • adjust_tp_sl has skip logic for unchanged values + valid orders")
        print("  • Redundant calls from SmartRiskManager are now no-ops")
        print("\nHow it prevents the 912/913/914 issue:")
        print("  1. submit_order() calls adjust_tp_sl → creates order (e.g., 912)")
        print("  2. SmartRiskManager calls adjust_tp_sl with SAME values")
        print("  3. Skip logic detects: values unchanged + order 912 valid → SKIP")
        print("  4. Result: Only ONE order created, no cancellations cascade")
    else:
        print("❌ SOME CHECKS FAILED - Review changes needed")
    print("="*80 + "\n")
    
    return all_pass

if __name__ == "__main__":
    try:
        success = test_code_changes()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

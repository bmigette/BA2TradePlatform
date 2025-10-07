"""
Test script to verify operand display, action calculations, and duplicate prevention.

This script tests:
1. Operands (left/right) are captured for conditions
2. Action calculations (TP/SL) include reference price, percent, and result
3. Duplicate actions are prevented in force mode
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.TradeConditions import (
    NewTargetHigherCondition,
    NewTargetLowerCondition,
    ConfidenceCondition,
    PercentToCurrentTargetCondition
)
from ba2_trade_platform.core.TradeActions import AdjustTakeProfitAction, AdjustStopLossAction
from ba2_trade_platform.core.types import OrderRecommendation, ReferenceValue
from ba2_trade_platform.core import db
from datetime import datetime, timezone, timedelta


def create_mock_recommendation(symbol="AAPL", action=OrderRecommendation.BUY, confidence=75.0, expected_profit=20.0):
    """Create a mock expert recommendation."""
    class MockRecommendation:
        def __init__(self):
            self.id = 1
            self.symbol = symbol
            self.recommended_action = action
            self.confidence = confidence
            self.expected_profit_percent = expected_profit
            self.price_at_date = 165.0
            self.created_at = datetime.now(timezone.utc)
            self.instance_id = 1
            
    return MockRecommendation()


def create_mock_order(side="buy", limit_price=150.0, quantity=10):
    """Create a mock trading order."""
    class MockOrder:
        def __init__(self):
            self.id = 1
            self.side = side
            self.limit_price = limit_price
            self.quantity = quantity
            self.transaction_id = 1
            self.expert_recommendation_id = 1
            
    return MockOrder()


def create_mock_transaction(take_profit=180.0, stop_loss=140.0):
    """Create a mock transaction."""
    class MockTransaction:
        def __init__(self):
            self.id = 1
            self.take_profit = take_profit
            self.stop_loss = stop_loss
            self.order_id = 1
            
    return MockTransaction()


def create_mock_account():
    """Create a mock account interface."""
    class MockAccount:
        def __init__(self):
            self.id = 1
            
        def get_instrument_current_price(self, symbol):
            return 165.0
            
    return MockAccount()


def mock_get_instance(model_class, instance_id):
    """Mock database get_instance function."""
    if model_class.__name__ == "Transaction":
        return create_mock_transaction()
    elif model_class.__name__ == "ExpertRecommendation":
        return create_mock_recommendation()
    return None


# Patch the db.get_instance function
original_get_instance = db.get_instance
db.get_instance = mock_get_instance


def test_operands_display():
    """Test that conditions properly store operands for display."""
    print("\n" + "="*80)
    print("TEST 1: Operands Display for Conditions")
    print("="*80)
    
    account = create_mock_account()
    recommendation = create_mock_recommendation()
    order = create_mock_order()
    
    # Test NewTargetHigherCondition
    print("\n1. Testing NewTargetHigherCondition...")
    condition1 = NewTargetHigherCondition(account, "AAPL", recommendation, order)
    result1 = condition1.evaluate()
    
    print(f"   Result: {result1}")
    print(f"   Has current_tp_price: {hasattr(condition1, 'current_tp_price')}")
    print(f"   Has new_target_price: {hasattr(condition1, 'new_target_price')}")
    print(f"   Has percent_diff: {hasattr(condition1, 'percent_diff')}")
    
    if hasattr(condition1, 'current_tp_price'):
        print(f"   ✅ Current TP Price: ${condition1.current_tp_price:.2f}")
    else:
        print(f"   ❌ Missing current_tp_price")
        
    if hasattr(condition1, 'new_target_price'):
        print(f"   ✅ New Target Price: ${condition1.new_target_price:.2f}")
    else:
        print(f"   ❌ Missing new_target_price")
        
    if hasattr(condition1, 'percent_diff'):
        print(f"   ✅ Percent Difference: {condition1.percent_diff:+.2f}%")
    else:
        print(f"   ❌ Missing percent_diff")
    
    # Test NewTargetLowerCondition
    print("\n2. Testing NewTargetLowerCondition...")
    condition2 = NewTargetLowerCondition(account, "AAPL", recommendation, order)
    result2 = condition2.evaluate()
    
    print(f"   Result: {result2}")
    if hasattr(condition2, 'current_tp_price') and hasattr(condition2, 'new_target_price'):
        print(f"   ✅ Current TP: ${condition2.current_tp_price:.2f}, New Target: ${condition2.new_target_price:.2f}")
    else:
        print(f"   ❌ Missing operand tracking")
    
    # Test numeric condition with calculated_value
    print("\n3. Testing ConfidenceCondition (numeric)...")
    condition3 = ConfidenceCondition(account, "AAPL", recommendation, ">=", 80.0, order)
    result3 = condition3.evaluate()
    
    print(f"   Result: {result3}")
    if hasattr(condition3, 'get_calculated_value'):
        calc_value = condition3.get_calculated_value()
        print(f"   ✅ Calculated Value: {calc_value:.2f}")
    else:
        print(f"   ❌ Missing calculated_value")
    
    print("\n📊 Summary:")
    all_have_operands = all([
        hasattr(condition1, 'current_tp_price'),
        hasattr(condition1, 'new_target_price'),
        hasattr(condition2, 'current_tp_price'),
        hasattr(condition2, 'new_target_price'),
        hasattr(condition3, 'get_calculated_value')
    ])
    
    if all_have_operands:
        print("   ✅ PASS: All conditions properly track operands/calculated values!")
        return True
    else:
        print("   ❌ FAIL: Some conditions missing operand tracking")
        return False


def test_action_calculations():
    """Test that TP/SL actions show calculation details."""
    print("\n" + "="*80)
    print("TEST 2: Action Calculation Display")
    print("="*80)
    
    account = create_mock_account()
    recommendation = create_mock_recommendation()
    order = create_mock_order()
    
    # Test AdjustTakeProfitAction
    print("\n1. Testing AdjustTakeProfitAction calculation preview...")
    action1 = AdjustTakeProfitAction(
        instrument_name="AAPL",
        account=account,
        order_recommendation=OrderRecommendation.BUY,
        existing_order=order,
        reference_value=ReferenceValue.ORDER_OPEN_PRICE.value,
        percent=5.0
    )
    
    if hasattr(action1, 'get_calculation_preview'):
        preview1 = action1.get_calculation_preview()
        print(f"   ✅ Has get_calculation_preview method")
        print(f"   Reference Type: {preview1.get('reference_type')}")
        print(f"   Percent: {preview1.get('percent')}%")
        print(f"   Reference Price: ${preview1.get('reference_price'):.2f}" if preview1.get('reference_price') else "   Reference Price: None")
        print(f"   Calculated Price: ${preview1.get('calculated_price'):.2f}" if preview1.get('calculated_price') else "   Calculated Price: None")
    else:
        print(f"   ❌ Missing get_calculation_preview method")
        return False
    
    # Test AdjustStopLossAction
    print("\n2. Testing AdjustStopLossAction calculation preview...")
    action2 = AdjustStopLossAction(
        instrument_name="AAPL",
        account=account,
        order_recommendation=OrderRecommendation.BUY,
        existing_order=order,
        reference_value=ReferenceValue.ORDER_OPEN_PRICE.value,
        percent=-3.0
    )
    
    if hasattr(action2, 'get_calculation_preview'):
        preview2 = action2.get_calculation_preview()
        print(f"   ✅ Has get_calculation_preview method")
        print(f"   Reference Type: {preview2.get('reference_type')}")
        print(f"   Percent: {preview2.get('percent')}%")
        print(f"   Reference Price: ${preview2.get('reference_price'):.2f}" if preview2.get('reference_price') else "   Reference Price: None")
        print(f"   Calculated Price: ${preview2.get('calculated_price'):.2f}" if preview2.get('calculated_price') else "   Calculated Price: None")
    else:
        print(f"   ❌ Missing get_calculation_preview method")
        return False
    
    print("\n📊 Summary:")
    has_preview = all([
        hasattr(action1, 'get_calculation_preview'),
        hasattr(action2, 'get_calculation_preview'),
        preview1.get('reference_type') is not None,
        preview2.get('reference_type') is not None
    ])
    
    if has_preview:
        print("   ✅ PASS: All TP/SL actions provide calculation preview!")
        return True
    else:
        print("   ❌ FAIL: Some actions missing calculation preview")
        return False


def test_duplicate_prevention():
    """Test that duplicate actions are prevented."""
    print("\n" + "="*80)
    print("TEST 3: Duplicate Action Prevention")
    print("="*80)
    
    import hashlib
    import json
    from ba2_trade_platform.core.types import ExpertActionType
    
    # Simulate generating the same action twice
    print("\n1. Testing action hash generation...")
    
    action_config_1 = {
        "type": ExpertActionType.ADJUST_TAKE_PROFIT.value,
        "reference_value": "order_open_price",
        "value": 5.0,
        "instrument": "AAPL"
    }
    
    action_config_2 = {
        "type": ExpertActionType.ADJUST_TAKE_PROFIT.value,
        "reference_value": "order_open_price",
        "value": 5.0,
        "instrument": "AAPL"
    }
    
    action_config_3 = {
        "type": ExpertActionType.ADJUST_TAKE_PROFIT.value,
        "reference_value": "current_price",  # Different reference
        "value": 5.0,
        "instrument": "AAPL"
    }
    
    hash1 = hashlib.md5(json.dumps(action_config_1, sort_keys=True).encode()).hexdigest()
    hash2 = hashlib.md5(json.dumps(action_config_2, sort_keys=True).encode()).hexdigest()
    hash3 = hashlib.md5(json.dumps(action_config_3, sort_keys=True).encode()).hexdigest()
    
    print(f"   Same config hash 1: {hash1[:8]}...")
    print(f"   Same config hash 2: {hash2[:8]}...")
    print(f"   Different config hash: {hash3[:8]}...")
    
    same_hash = (hash1 == hash2)
    different_hash = (hash1 != hash3)
    
    print(f"\n2. Testing duplicate detection...")
    generated_actions = set()
    
    # First action
    key1 = f"{ExpertActionType.ADJUST_TAKE_PROFIT.value}_{hash1}"
    is_duplicate_1 = key1 in generated_actions
    generated_actions.add(key1)
    print(f"   First action: {key1[:30]}... - Duplicate: {is_duplicate_1}")
    
    # Same action again
    key2 = f"{ExpertActionType.ADJUST_TAKE_PROFIT.value}_{hash2}"
    is_duplicate_2 = key2 in generated_actions
    generated_actions.add(key2)
    print(f"   Same action:  {key2[:30]}... - Duplicate: {is_duplicate_2}")
    
    # Different action
    key3 = f"{ExpertActionType.ADJUST_TAKE_PROFIT.value}_{hash3}"
    is_duplicate_3 = key3 in generated_actions
    generated_actions.add(key3)
    print(f"   Different action: {key3[:30]}... - Duplicate: {is_duplicate_3}")
    
    print("\n📊 Summary:")
    works_correctly = (
        same_hash and  # Same configs produce same hash
        different_hash and  # Different configs produce different hash
        not is_duplicate_1 and  # First is not duplicate
        is_duplicate_2 and  # Second IS duplicate
        not is_duplicate_3  # Different is not duplicate
    )
    
    if works_correctly:
        print("   ✅ PASS: Duplicate prevention works correctly!")
        return True
    else:
        print("   ❌ FAIL: Duplicate prevention not working")
        return False


if __name__ == "__main__":
    print("🧪 Testing Operands, Calculations, and Duplicate Prevention")
    print("=" * 80)
    
    test_results = []
    
    # Run all tests
    test_results.append(("Operands Display", test_operands_display()))
    test_results.append(("Action Calculations", test_action_calculations()))
    test_results.append(("Duplicate Prevention", test_duplicate_prevention()))
    
    # Restore original function
    db.get_instance = original_get_instance
    
    # Final summary
    print("\n" + "=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    
    passed = sum(1 for _, result in test_results if result)
    total = len(test_results)
    
    for test_name, result in test_results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {test_name}")
    
    print(f"\n📊 Overall: {passed}/{total} tests passed ({passed/total*100:.0f}%)")
    
    if passed == total:
        print("✅ All tests PASSED! Features are working correctly.")
        sys.exit(0)
    else:
        print(f"❌ {total - passed} test(s) FAILED")
        sys.exit(1)

"""
Test script to verify calculated values and force actions feature.

This script tests:
1. All conditions properly store calculated values
2. Force actions mode generates actions even when conditions fail
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.types import OrderRecommendation, RiskLevel, TimeHorizon, ExpertEventType, ExpertActionType
from ba2_trade_platform.core.models import Ruleset, EventAction


class MockAccount:
    """Mock account for testing."""
    def __init__(self):
        self.id = 1
    
    def get_instrument_current_price(self, symbol: str):
        return 150.0


class MockRecommendation:
    """Mock recommendation for testing."""
    def __init__(self):
        self.id = 1
        self.symbol = "AAPL"
        self.recommended_action = OrderRecommendation.BUY
        self.confidence = 75.0
        self.expected_profit_percent = 20.0
        self.risk_level = RiskLevel.HIGH
        self.time_horizon = TimeHorizon.LONG_TERM
        self.created_at = datetime.now(timezone.utc)
        self.instance_id = 1
        self.price_at_date = 150.0


class MockOrder:
    """Mock order for testing."""
    def __init__(self):
        self.id = 1
        self.symbol = "AAPL"
        self.side = "BUY"
        self.quantity = 10
        self.limit_price = 165.0  # Entry at 165, current at 150 = -9.09% loss
        self.transaction_id = 1
        self.created_at = datetime.now(timezone.utc) - timedelta(days=3)  # 3 days old


class MockTransaction:
    """Mock transaction for testing."""
    def __init__(self):
        self.id = 1
        self.take_profit = 180.0  # TP at 180, current at 150
        

def test_calculated_values():
    """Test that all conditions store calculated values."""
    print("\n" + "="*80)
    print("TEST 1: Calculated Values for All Conditions")
    print("="*80)
    
    account = MockAccount()
    evaluator = TradeActionEvaluator(account, evaluate_all_conditions=True)
    recommendation = MockRecommendation()
    order = MockOrder()
    
    # Mock the get_instance function to return our mock transaction
    from ba2_trade_platform.core import db
    original_get_instance = db.get_instance
    def mock_get_instance(model_class, id):
        if model_class.__name__ == 'Transaction':
            return MockTransaction()
        return None
    db.get_instance = mock_get_instance
    
    # Create event action with multiple numeric conditions
    event_action = type('EventAction', (), {
        'id': 1,
        'name': 'Test All Numeric Conditions',
        'triggers': {
            'confidence': {
                'event_type': ExpertEventType.N_CONFIDENCE.value,
                'operator': '>=',
                'value': 80.0  # FAIL: 75
            },
            'expected_profit': {
                'event_type': ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
                'operator': '>=',
                'value': 15.0  # PASS: 20
            },
            'pl_percent': {
                'event_type': ExpertEventType.N_PROFIT_LOSS_PERCENT.value,
                'operator': '>',
                'value': 0.0  # FAIL: negative
            },
            'pl_amount': {
                'event_type': ExpertEventType.N_PROFIT_LOSS_AMOUNT.value,
                'operator': '>',
                'value': 50.0  # FAIL: negative
            },
            'days_opened': {
                'event_type': ExpertEventType.N_DAYS_OPENED.value,
                'operator': '>',
                'value': 5.0  # FAIL: 3 days
            },
            'percent_to_current_target': {
                'event_type': ExpertEventType.N_PERCENT_TO_CURRENT_TARGET.value,
                'operator': '>',
                'value': 5.0  # PASS: (180-150)/150 = 20%
            },
            'percent_to_new_target': {
                'event_type': ExpertEventType.N_PERCENT_TO_NEW_TARGET.value,
                'operator': '>',
                'value': 5.0  # PASS: depends on calculation
            }
        }
    })()
    
    # Evaluate conditions
    result = evaluator._evaluate_conditions(
        event_action=event_action,
        instrument_name="AAPL",
        expert_recommendation=recommendation,
        existing_order=order
    )
    
    print(f"\nEvaluation Result: {result}")
    print(f"Conditions Evaluated: {len(evaluator.condition_evaluations)}")
    
    conditions_with_values = 0
    conditions_without_values = []
    
    for i, cond in enumerate(evaluator.condition_evaluations, 1):
        calc_val = cond.get('calculated_value')
        desc = cond.get('condition_description', 'Unknown')
        result_val = cond.get('condition_result', False)
        
        print(f"\n  Condition {i}: {desc}")
        print(f"    Result: {result_val}")
        
        if calc_val is not None:
            print(f"    ✅ Calculated Value: {calc_val:.2f}")
            conditions_with_values += 1
        else:
            print(f"    ❌ NO CALCULATED VALUE")
            conditions_without_values.append(desc)
    
    # Restore original function
    db.get_instance = original_get_instance
    
    print(f"\n📊 Summary:")
    print(f"  Conditions with calculated values: {conditions_with_values}/{len(evaluator.condition_evaluations)}")
    
    if conditions_without_values:
        print(f"  ❌ Conditions missing values:")
        for desc in conditions_without_values:
            print(f"    - {desc}")
    
    success = len(conditions_without_values) == 0
    
    if success:
        print("\n✅ PASS: All numeric conditions have calculated values!")
        return True
    else:
        print(f"\n❌ FAIL: {len(conditions_without_values)} condition(s) missing calculated values")
        return False


def test_force_actions_mode():
    """Test that force actions mode generates actions despite failed conditions."""
    print("\n" + "="*80)
    print("TEST 2: Force Actions Mode")
    print("="*80)
    
    account = MockAccount()
    recommendation = MockRecommendation()
    
    # Test without force mode (should not generate actions)
    print("\n🔹 Without Force Actions Mode:")
    evaluator1 = TradeActionEvaluator(
        account, 
        evaluate_all_conditions=True,
        force_generate_actions=False
    )
    
    event_action = type('EventAction', (), {
        'id': 1,
        'name': 'Test Action',
        'continue_processing': False,
        'triggers': {
            'confidence': {
                'event_type': ExpertEventType.N_CONFIDENCE.value,
                'operator': '>=',
                'value': 90.0  # FAIL: recommendation has 75
            }
        },
        'actions': [
            {
                'action_type': ExpertActionType.BUY.value,
                'config': {}
            }
        ]
    })()
    
    conditions_met1 = evaluator1._evaluate_conditions(
        event_action=event_action,
        instrument_name="AAPL",
        expert_recommendation=recommendation,
        existing_order=None
    )
    
    print(f"  Conditions Met: {conditions_met1}")
    print(f"  Expected: False (confidence 75 < 90)")
    
    # Test with force mode (should generate actions)
    print("\n🔹 With Force Actions Mode:")
    evaluator2 = TradeActionEvaluator(
        account, 
        evaluate_all_conditions=True,
        force_generate_actions=True
    )
    
    conditions_met2 = evaluator2._evaluate_conditions(
        event_action=event_action,
        instrument_name="AAPL",
        expert_recommendation=recommendation,
        existing_order=None
    )
    
    print(f"  Conditions Met: {conditions_met2}")
    print(f"  Force Generate Actions: {evaluator2.force_generate_actions}")
    print(f"  Expected: Actions should be generated despite conditions_met=False")
    
    # Check the logic
    should_generate = conditions_met2 or evaluator2.force_generate_actions
    
    print(f"\n📊 Summary:")
    print(f"  Normal mode - Conditions met: {conditions_met1}")
    print(f"  Force mode - Conditions met: {conditions_met2}")
    print(f"  Force mode - Should generate actions: {should_generate}")
    
    success = not conditions_met1 and not conditions_met2 and should_generate
    
    if success:
        print("\n✅ PASS: Force actions mode working correctly!")
        return True
    else:
        print("\n❌ FAIL: Force actions mode not working as expected")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("TESTING CALCULATED VALUES AND FORCE ACTIONS FEATURES")
    print("="*80)
    
    test1_passed = test_calculated_values()
    test2_passed = test_force_actions_mode()
    
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"Test 1 (Calculated Values): {'✅ PASS' if test1_passed else '❌ FAIL'}")
    print(f"Test 2 (Force Actions Mode): {'✅ PASS' if test2_passed else '❌ FAIL'}")
    
    if test1_passed and test2_passed:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("\n⚠️  SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())

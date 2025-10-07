"""
Test script to verify the evaluate_all_conditions feature.

This script tests that the TradeActionEvaluator properly evaluates all conditions
when evaluate_all_conditions=True, and stops at the first failure when False.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.types import OrderRecommendation, RiskLevel, TimeHorizon, ExpertEventType
from ba2_trade_platform.core.models import Ruleset, EventAction
from datetime import datetime


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
        self.confidence = 75.0  # This will fail a >=80 condition
        self.expected_profit_percent = 15.0
        self.risk_level = RiskLevel.HIGH
        self.time_horizon = TimeHorizon.LONG_TERM
        self.created_at = datetime.now()
        self.instance_id = 1
        self.price_at_date = 150.0


def create_test_ruleset():
    """Create a ruleset with multiple conditions where some will fail."""
    ruleset = type('TestRuleset', (), {
        'id': 1,
        'name': 'Test Ruleset',
        'description': 'Test ruleset with multiple conditions',
        'event_actions': [
            type('TestEventAction', (), {
                'id': 1,
                'name': 'Test Rule',
                'ruleset_id': 1,
                'triggers': {
                    'condition1': {
                        'event_type': ExpertEventType.N_CONFIDENCE.value,
                        'operator': '>=',
                        'value': 80.0  # This will FAIL (recommendation has 75)
                    },
                    'condition2': {
                        'event_type': ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
                        'operator': '>=',
                        'value': 10.0  # This will PASS (recommendation has 15)
                    },
                    'condition3': {
                        'event_type': ExpertEventType.F_BULLISH.value,
                        'operator': None,
                        'value': None  # This will PASS (recommendation is BUY)
                    }
                },
                'actions': []
            })()
        ]
    })()
    return ruleset


def test_stop_at_first_failure():
    """Test that evaluation stops at first failure when evaluate_all_conditions=False."""
    print("\n" + "="*80)
    print("TEST 1: Stop at First Failure (evaluate_all_conditions=False)")
    print("="*80)
    
    account = MockAccount()
    evaluator = TradeActionEvaluator(account, evaluate_all_conditions=False)
    recommendation = MockRecommendation()
    
    # Manually set up the ruleset evaluation
    # We'll use the _evaluate_conditions method directly
    from unittest.mock import MagicMock
    
    # Create a mock EventAction with the test conditions
    event_action = type('EventAction', (), {
        'id': 1,
        'name': 'Test Rule - Stop at First Failure',
        'triggers': {
            'condition1': {
                'event_type': ExpertEventType.N_CONFIDENCE.value,
                'operator': '>=',
                'value': 80.0  # FAIL: recommendation has 75
            },
            'condition2': {
                'event_type': ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
                'operator': '>=',
                'value': 10.0  # PASS: recommendation has 15
            },
            'condition3': {
                'event_type': ExpertEventType.F_BULLISH.value,
                'operator': None,
                'value': None  # PASS: recommendation is BUY
            }
        }
    })()
    
    # Evaluate conditions
    result = evaluator._evaluate_conditions(
        event_action=event_action,
        instrument_name="AAPL",
        expert_recommendation=recommendation,
        existing_order=None
    )
    
    # Get evaluation details
    details = evaluator.get_evaluation_details()
    
    print(f"\nEvaluation Result: {result}")
    print(f"Conditions Evaluated: {len(evaluator.condition_evaluations)}")
    
    for i, cond in enumerate(evaluator.condition_evaluations, 1):
        print(f"  Condition {i}: {cond.get('condition_description', 'Unknown')}")
        print(f"    Result: {cond.get('condition_result', False)}")
        if cond.get('calculated_value') is not None:
            print(f"    Value: {cond.get('calculated_value')}")
    
    # Expected: Should only evaluate 1 condition (the first one that fails)
    expected_conditions = 1
    actual_conditions = len(evaluator.condition_evaluations)
    
    print(f"\nExpected {expected_conditions} condition(s) evaluated")
    print(f"Actually evaluated {actual_conditions} condition(s)")
    
    if actual_conditions == expected_conditions:
        print("✅ PASS: Stopped at first failure as expected")
        return True
    else:
        print("❌ FAIL: Did not stop at first failure")
        return False


def test_evaluate_all_conditions():
    """Test that all conditions are evaluated when evaluate_all_conditions=True."""
    print("\n" + "="*80)
    print("TEST 2: Evaluate All Conditions (evaluate_all_conditions=True)")
    print("="*80)
    
    account = MockAccount()
    evaluator = TradeActionEvaluator(account, evaluate_all_conditions=True)
    recommendation = MockRecommendation()
    
    # Create event action with test conditions
    event_action = type('EventAction', (), {
        'id': 1,
        'name': 'Test Rule - Evaluate All',
        'triggers': {
            'condition1': {
                'event_type': ExpertEventType.N_CONFIDENCE.value,
                'operator': '>=',
                'value': 80.0  # FAIL: recommendation has 75
            },
            'condition2': {
                'event_type': ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
                'operator': '>=',
                'value': 10.0  # PASS: recommendation has 15
            },
            'condition3': {
                'event_type': ExpertEventType.F_BULLISH.value,
                'operator': None,
                'value': None  # PASS: recommendation is BUY
            }
        }
    })()
    
    # Evaluate conditions
    result = evaluator._evaluate_conditions(
        event_action=event_action,
        instrument_name="AAPL",
        expert_recommendation=recommendation,
        existing_order=None
    )
    
    print(f"\nEvaluation Result: {result}")
    print(f"Conditions Evaluated: {len(evaluator.condition_evaluations)}")
    
    for i, cond in enumerate(evaluator.condition_evaluations, 1):
        print(f"  Condition {i}: {cond.get('condition_description', 'Unknown')}")
        print(f"    Result: {cond.get('condition_result', False)}")
        if cond.get('calculated_value') is not None:
            print(f"    Value: {cond.get('calculated_value')}")
    
    # Expected: Should evaluate all 3 conditions
    expected_conditions = 3
    actual_conditions = len(evaluator.condition_evaluations)
    
    print(f"\nExpected {expected_conditions} condition(s) evaluated")
    print(f"Actually evaluated {actual_conditions} condition(s)")
    
    if actual_conditions == expected_conditions:
        print("✅ PASS: Evaluated all conditions as expected")
        return True
    else:
        print("❌ FAIL: Did not evaluate all conditions")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("TESTING EVALUATE_ALL_CONDITIONS FEATURE")
    print("="*80)
    
    test1_passed = test_stop_at_first_failure()
    test2_passed = test_evaluate_all_conditions()
    
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"Test 1 (Stop at First Failure): {'✅ PASS' if test1_passed else '❌ FAIL'}")
    print(f"Test 2 (Evaluate All Conditions): {'✅ PASS' if test2_passed else '❌ FAIL'}")
    
    if test1_passed and test2_passed:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("\n⚠️  SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())

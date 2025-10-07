"""
Comprehensive Rules and Actions Testing

This module tests all combinations of events/conditions and actions in a dry-run mode
without relying on a database. It creates mock objects and validates that the evaluation
logic works correctly.

Run this file standalone: python test_files/test_rules_actions.py
"""

import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from decimal import Decimal

# Add parent directory to path to import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock objects to avoid database dependency
class MockExpertRecommendation:
    """Mock ExpertRecommendation for testing."""
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.instance_id = kwargs.get('instance_id', 1)
        self.symbol = kwargs.get('symbol', 'AAPL')
        self.recommended_action = kwargs.get('recommended_action')
        self.confidence = kwargs.get('confidence', 75.0)
        self.expected_profit_percent = kwargs.get('expected_profit_percent', 10.0)
        self.price_at_date = kwargs.get('price_at_date', 150.0)
        self.risk_level = kwargs.get('risk_level')
        self.time_horizon = kwargs.get('time_horizon')
        self.created_at = kwargs.get('created_at', datetime.now(timezone.utc))


class MockTradingOrder:
    """Mock TradingOrder for testing."""
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.symbol = kwargs.get('symbol', 'AAPL')
        self.side = kwargs.get('side', 'buy')
        self.quantity = kwargs.get('quantity', 10)
        self.limit_price = kwargs.get('limit_price', 150.0)
        self.open_price = kwargs.get('open_price', 150.0)
        self.created_at = kwargs.get('created_at', datetime.now(timezone.utc) - timedelta(days=5))
        self.transaction_id = kwargs.get('transaction_id', 1)


class MockTransaction:
    """Mock Transaction for testing."""
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.symbol = kwargs.get('symbol', 'AAPL')
        self.quantity = kwargs.get('quantity', 10)
        self.open_price = kwargs.get('open_price', 150.0)
        self.take_profit = kwargs.get('take_profit', 165.0)
        self.stop_loss = kwargs.get('stop_loss', 140.0)
        self.order_id = kwargs.get('order_id', 1)


class MockExpertInstance:
    """Mock ExpertInstance for testing."""
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 1)
        self.account_id = kwargs.get('account_id', 1)
        self.enabled = kwargs.get('enabled', True)
        self.settings = kwargs.get('settings', {})
        
    def get_virtual_equity(self, account):
        """Mock virtual equity calculation."""
        return 10000.0  # $10,000 virtual equity


class MockAccount:
    """Mock AccountInterface for testing."""
    def __init__(self):
        self.id = 1
        self._price_cache = {}
        self._positions = {}  # {symbol: quantity}
        
    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """Mock price fetching."""
        # Default prices for common symbols - using 150.0 for AAPL to match order price
        prices = {
            'AAPL': 150.0,  # Same as limit_price in MockTradingOrder for 0% P/L
            'MSFT': 350.0,
            'GOOGL': 140.0,
            'TSLA': 200.0,
            'NVDA': 450.0
        }
        return prices.get(symbol, 100.0)
    
    def set_position(self, symbol: str, quantity: float):
        """Set position for testing."""
        self._positions[symbol] = quantity
        
    def get_positions(self):
        """Get all positions."""
        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty
        return [Position(s, q) for s, q in self._positions.items()]


# Import after mocks are defined
from ba2_trade_platform.core.types import (
    ExpertEventType, ExpertActionType, OrderRecommendation,
    RiskLevel, TimeHorizon
)
from ba2_trade_platform.core.TradeConditions import create_condition
from ba2_trade_platform.core.TradeActions import create_action


def test_condition(condition_name: str, event_type: ExpertEventType, 
                   operator_str: Optional[str], value: Optional[float],
                   expert_recommendation: MockExpertRecommendation,
                   existing_order: Optional[MockTradingOrder] = None,
                   expected_result: Optional[bool] = None) -> Dict[str, Any]:
    """
    Test a single condition.
    
    Args:
        condition_name: Name of the condition being tested
        event_type: Type of event/condition
        operator_str: Comparison operator (for numeric conditions)
        value: Comparison value (for numeric conditions)
        expert_recommendation: Mock expert recommendation
        existing_order: Optional mock order
        expected_result: Expected evaluation result (None = don't check)
        
    Returns:
        Test result dictionary
    """
    try:
        account = MockAccount()
        
        # If we have an existing order, set up a position for that symbol
        if existing_order:
            account.set_position(expert_recommendation.symbol, existing_order.quantity)
        
        # Create condition
        condition = create_condition(
            event_type=event_type,
            account=account,
            instrument_name=expert_recommendation.symbol,
            expert_recommendation=expert_recommendation,
            existing_order=existing_order,
            operator_str=operator_str,
            value=value
        )
        
        if not condition:
            return {
                'condition': condition_name,
                'success': False,
                'error': 'Failed to create condition'
            }
        
        # Evaluate condition
        result = condition.evaluate()
        description = condition.get_description()
        
        # Get calculated value if available
        calculated_value = None
        if hasattr(condition, 'get_calculated_value'):
            calculated_value = condition.get_calculated_value()
        
        # Check expected result if provided
        matches_expected = True
        if expected_result is not None and result != expected_result:
            matches_expected = False
        
        return {
            'condition': condition_name,
            'success': True,
            'result': result,
            'description': description,
            'calculated_value': calculated_value,
            'matches_expected': matches_expected,
            'expected': expected_result
        }
        
    except Exception as e:
        return {
            'condition': condition_name,
            'success': False,
            'error': str(e)
        }


def test_action(action_name: str, action_type: ExpertActionType,
               action_config: Dict[str, Any],
               expert_recommendation: MockExpertRecommendation,
               order_recommendation: OrderRecommendation,
               existing_order: Optional[MockTradingOrder] = None) -> Dict[str, Any]:
    """
    Test a single action creation (dry run, no execution).
    
    Args:
        action_name: Name of the action being tested
        action_type: Type of action
        action_config: Action configuration (kwargs to pass to create_action)
        expert_recommendation: Mock expert recommendation
        order_recommendation: Order recommendation
        existing_order: Optional mock order
        
    Returns:
        Test result dictionary
    """
    try:
        account = MockAccount()
        
        # Create action - pass action_config as **kwargs
        action = create_action(
            action_type=action_type,
            instrument_name=expert_recommendation.symbol,
            account=account,
            order_recommendation=order_recommendation,
            existing_order=existing_order,
            expert_recommendation=expert_recommendation,
            **action_config  # Unpack the config dict
        )
        
        if not action:
            return {
                'action': action_name,
                'success': False,
                'error': 'Failed to create action'
            }
        
        # Get description (don't execute to avoid database dependencies)
        description = action.get_description()
        
        return {
            'action': action_name,
            'success': True,
            'description': description,
            'action_type': action_type.value
        }
        
    except Exception as e:
        return {
            'action': action_name,
            'success': False,
            'error': str(e)
        }


def run_comprehensive_tests():
    """Run comprehensive tests on all conditions and actions."""
    
    print("=" * 80)
    print("COMPREHENSIVE RULES AND ACTIONS TESTING")
    print("=" * 80)
    print()
    
    results = {
        'conditions': [],
        'actions': [],
        'total_tests': 0,
        'passed': 0,
        'failed': 0
    }
    
    # ========================================================================
    # TEST CONDITIONS
    # ========================================================================
    print("TESTING CONDITIONS")
    print("-" * 80)
    
    # Test data for conditions
    high_confidence_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.BUY,
        confidence=85.0,
        expected_profit_percent=15.0,
        price_at_date=150.0,
        risk_level=RiskLevel.LOW,
        time_horizon=TimeHorizon.LONG_TERM
    )
    
    low_confidence_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.SELL,
        confidence=45.0,
        expected_profit_percent=5.0,
        price_at_date=150.0,
        risk_level=RiskLevel.HIGH,
        time_horizon=TimeHorizon.SHORT_TERM
    )
    
    existing_order = MockTradingOrder(
        side='buy',
        quantity=10,
        limit_price=150.0,
        created_at=datetime.now(timezone.utc) - timedelta(days=7)
    )
    
    # Numeric conditions tests
    condition_tests = [
        # Confidence tests
        ('Confidence >= 80 (should PASS)', ExpertEventType.N_CONFIDENCE, '>=', 80.0, high_confidence_rec, None, True),
        ('Confidence >= 90 (should FAIL)', ExpertEventType.N_CONFIDENCE, '>=', 90.0, high_confidence_rec, None, False),
        ('Confidence < 50 (should PASS)', ExpertEventType.N_CONFIDENCE, '<', 50.0, low_confidence_rec, None, True),
        ('Confidence > 70 (should FAIL)', ExpertEventType.N_CONFIDENCE, '>', 70.0, low_confidence_rec, None, False),
        
        # Expected profit tests
        ('Expected Profit >= 10 (should PASS)', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '>=', 10.0, high_confidence_rec, None, True),
        ('Expected Profit >= 20 (should FAIL)', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '>=', 20.0, high_confidence_rec, None, False),
        
        # Flag conditions (operator and value are None for flags)
        ('Bullish (should PASS)', ExpertEventType.F_BULLISH, None, None, high_confidence_rec, None, True),
        ('Bearish (should FAIL with BUY rec)', ExpertEventType.F_BEARISH, None, None, high_confidence_rec, None, False),
        ('Bearish (should PASS with SELL rec)', ExpertEventType.F_BEARISH, None, None, low_confidence_rec, None, True),
        ('High Risk (should PASS)', ExpertEventType.F_HIGHRISK, None, None, low_confidence_rec, None, True),
        ('Low Risk (should PASS)', ExpertEventType.F_LOWRISK, None, None, high_confidence_rec, None, True),
        ('Long Term (should PASS)', ExpertEventType.F_LONG_TERM, None, None, high_confidence_rec, None, True),
        ('Short Term (should PASS)', ExpertEventType.F_SHORT_TERM, None, None, low_confidence_rec, None, True),
    ]
    
    for test_params in condition_tests:
        result = test_condition(*test_params)
        results['conditions'].append(result)
        results['total_tests'] += 1
        
        if result['success']:
            status = '✅ PASS' if result['matches_expected'] else '❌ FAIL'
            if result['matches_expected']:
                results['passed'] += 1
            else:
                results['failed'] += 1
                
            calc_str = f" [actual: {result['calculated_value']:.2f}]" if result['calculated_value'] is not None else ""
            print(f"{status} | {result['condition']}{calc_str}")
            if not result['matches_expected']:
                print(f"         Expected: {result['expected']}, Got: {result['result']}")
        else:
            results['failed'] += 1
            print(f"❌ ERROR | {result['condition']}: {result.get('error', 'Unknown error')}")
    
    print()
    
    # ========================================================================
    # TEST ACTIONS
    # ========================================================================
    print("TESTING ACTIONS")
    print("-" * 80)
    
    # Test data for actions
    buy_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.BUY,
        confidence=80.0,
        expected_profit_percent=12.0,
        price_at_date=150.0
    )
    
    sell_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.SELL,
        confidence=70.0,
        expected_profit_percent=8.0,
        price_at_date=150.0
    )
    
    # Action tests
    action_tests = [
        # Buy actions
        ('Simple BUY', ExpertActionType.BUY, {}, buy_rec, OrderRecommendation.BUY, None),
        # Note: BUY/SELL actions don't accept TP/SL in constructor - those are managed separately
        
        # Sell actions
        ('Simple SELL', ExpertActionType.SELL, {}, sell_rec, OrderRecommendation.SELL, None),
        
        # Close actions
        ('CLOSE position', ExpertActionType.CLOSE, {}, buy_rec, OrderRecommendation.SELL, existing_order),
        
        # Adjustment actions - use 'percent' parameter, not 'value'
        ('ADJUST_TAKE_PROFIT +5%', ExpertActionType.ADJUST_TAKE_PROFIT, {
            'percent': 5.0,
            'reference_value': 'current_price'
        }, buy_rec, OrderRecommendation.BUY, existing_order),
        ('ADJUST_STOP_LOSS -3%', ExpertActionType.ADJUST_STOP_LOSS, {
            'percent': -3.0,
            'reference_value': 'order_open_price'
        }, buy_rec, OrderRecommendation.BUY, existing_order),
        
        # Share adjustment actions
        ('INCREASE_INSTRUMENT_SHARE to 12%', ExpertActionType.INCREASE_INSTRUMENT_SHARE, {
            'target_percent': 12.0
        }, buy_rec, OrderRecommendation.BUY, None),
        ('DECREASE_INSTRUMENT_SHARE to 5%', ExpertActionType.DECREASE_INSTRUMENT_SHARE, {
            'target_percent': 5.0
        }, sell_rec, OrderRecommendation.SELL, existing_order),
        ('DECREASE_INSTRUMENT_SHARE to 0% (should close to 1 qty)', ExpertActionType.DECREASE_INSTRUMENT_SHARE, {
            'target_percent': 0.0
        }, sell_rec, OrderRecommendation.SELL, existing_order),
    ]
    
    for test_params in action_tests:
        result = test_action(*test_params)
        results['actions'].append(result)
        results['total_tests'] += 1
        
        if result['success']:
            results['passed'] += 1
            print(f"✅ PASS | {result['action']}")
            print(f"         {result['description']}")
        else:
            results['failed'] += 1
            print(f"❌ ERROR | {result['action']}: {result.get('error', 'Unknown error')}")
    
    print()
    
    # ========================================================================
    # COMPREHENSIVE CONDITION TESTS WITH VALUE VALIDATION
    # ========================================================================
    print("COMPREHENSIVE CONDITION TESTS (with value validation)")
    print("-" * 80)
    
    # Create additional test recommendations
    medium_term_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.BUY,
        confidence=75.0,
        expected_profit_percent=10.0,
        price_at_date=150.0,
        time_horizon=TimeHorizon.MEDIUM_TERM
    )
    
    medium_risk_rec = MockExpertRecommendation(
        recommended_action=OrderRecommendation.BUY,
        confidence=75.0,
        expected_profit_percent=10.0,
        price_at_date=150.0,
        risk_level=RiskLevel.MEDIUM
    )
    
    # Test all numeric conditions with different operators
    numeric_condition_tests = [
        # Confidence tests
        ('Confidence == 85', ExpertEventType.N_CONFIDENCE, '==', 85.0, high_confidence_rec, None, True, 85.0),
        ('Confidence != 90', ExpertEventType.N_CONFIDENCE, '!=', 90.0, high_confidence_rec, None, True, 85.0),
        ('Confidence <= 85', ExpertEventType.N_CONFIDENCE, '<=', 85.0, high_confidence_rec, None, True, 85.0),
        ('Confidence > 80', ExpertEventType.N_CONFIDENCE, '>', 80.0, high_confidence_rec, None, True, 85.0),
        
        # Expected profit tests
        ('Profit == 15', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '==', 15.0, high_confidence_rec, None, True, 15.0),
        ('Profit != 10', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '!=', 10.0, high_confidence_rec, None, True, 15.0),
        ('Profit <= 20', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '<=', 20.0, high_confidence_rec, None, True, 15.0),
        ('Profit > 10', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '>', 10.0, high_confidence_rec, None, True, 15.0),
        
        # Profit/Loss percent tests (requires existing order)
        ('P/L >= -5%', ExpertEventType.N_PROFIT_LOSS_PERCENT, '>=', -5.0, high_confidence_rec, existing_order, True, 0.0),
        ('P/L < 10%', ExpertEventType.N_PROFIT_LOSS_PERCENT, '<', 10.0, high_confidence_rec, existing_order, True, 0.0),
    ]
    
    for test_data in numeric_condition_tests:
        cond_name, event_type, operator, value, rec, order, expected, expected_value = test_data
        result = test_condition(cond_name, event_type, operator, value, rec, order, expected)
        results['conditions'].append(result)
        results['total_tests'] += 1
        
        if result['success']:
            # Check if result matches expected
            calc_val = result.get('calculated_value')
            value_match = calc_val is not None and abs(calc_val - expected_value) < 0.01
            
            if result['matches_expected'] and value_match:
                results['passed'] += 1
                print(f"✅ PASS | {result['condition']} [actual: {calc_val:.2f}, expected: {expected_value:.2f}]")
            else:
                results['failed'] += 1
                if not result['matches_expected']:
                    print(f"❌ FAIL | {result['condition']} - Result mismatch (got: {result['result']}, expected: {expected})")
                else:
                    if calc_val is not None:
                        print(f"❌ FAIL | {result['condition']} - Value mismatch (got: {calc_val:.2f}, expected: {expected_value:.2f})")
                    else:
                        print(f"❌ FAIL | {result['condition']} - No calculated value available (expected: {expected_value:.2f})")
        else:
            results['failed'] += 1
            print(f"❌ ERROR | {result['condition']}: {result.get('error', 'Unknown error')}")
    
    # Test additional flag conditions
    additional_flag_tests = [
        ('Has Position', ExpertEventType.F_HAS_POSITION, None, None, buy_rec, existing_order, True),
        ('Has No Position', ExpertEventType.F_HAS_NO_POSITION, None, None, buy_rec, None, True),
        ('Medium Term', ExpertEventType.F_MEDIUM_TERM, None, None, medium_term_rec, None, True),
        ('Medium Risk', ExpertEventType.F_MEDIUMRISK, None, None, medium_risk_rec, None, True),
    ]
    
    for test_data in additional_flag_tests:
        cond_name, event_type, operator, value, rec, order, expected = test_data
        result = test_condition(cond_name, event_type, operator, value, rec, order, expected)
        results['conditions'].append(result)
        results['total_tests'] += 1
        
        if result['success'] and result['matches_expected']:
            results['passed'] += 1
            print(f"✅ PASS | {result['condition']}")
        else:
            results['failed'] += 1
            if not result.get('matches_expected', False):
                print(f"❌ FAIL | {result['condition']} - Expected: {expected}, Got: {result.get('result')}")
            else:
                print(f"❌ ERROR | {result['condition']}: {result.get('error', 'Unknown error')}")
    
    print()
    
    # ========================================================================
    # TEST COMBINED SCENARIOS
    # ========================================================================
    print("TESTING COMBINED SCENARIOS")
    print("-" * 80)
    
    # Scenario 1: High confidence BUY with position sizing
    print("\n📊 Scenario 1: High Confidence Entry with Position Sizing")
    print("  Conditions:")
    r1 = test_condition('Confidence >= 85', ExpertEventType.N_CONFIDENCE, '>=', 85.0, high_confidence_rec, None, True)
    results['conditions'].append(r1)
    results['total_tests'] += 1
    if r1['success'] and r1['matches_expected']:
        results['passed'] += 1
        print(f"    ✅ {r1['condition']} [actual: {r1['calculated_value']:.2f}]")
    else:
        results['failed'] += 1
        print(f"    ❌ {r1['condition']}")
    
    r2 = test_condition('Expected Profit >= 12', ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT, '>=', 12.0, high_confidence_rec, None, True)
    results['conditions'].append(r2)
    results['total_tests'] += 1
    if r2['success'] and r2['matches_expected']:
        results['passed'] += 1
        print(f"    ✅ {r2['condition']} [actual: {r2['calculated_value']:.2f}]")
    else:
        results['failed'] += 1
        print(f"    ❌ {r2['condition']}")
    
    print("  Actions:")
    a1 = test_action('INCREASE to 15%', ExpertActionType.INCREASE_INSTRUMENT_SHARE, {'target_percent': 15.0}, high_confidence_rec, OrderRecommendation.BUY, None)
    results['actions'].append(a1)
    results['total_tests'] += 1
    if a1['success']:
        results['passed'] += 1
        print(f"    ✅ {a1['action']}")
    else:
        results['failed'] += 1
        print(f"    ❌ {a1['action']}")
    
    # Scenario 2: Low confidence exit
    print("\n📊 Scenario 2: Low Confidence Exit")
    print("  Conditions:")
    r3 = test_condition('Confidence < 50', ExpertEventType.N_CONFIDENCE, '<', 50.0, low_confidence_rec, None, True)
    results['conditions'].append(r3)
    results['total_tests'] += 1
    if r3['success'] and r3['matches_expected']:
        results['passed'] += 1
        print(f"    ✅ {r3['condition']} [actual: {r3['calculated_value']:.2f}]")
    else:
        results['failed'] += 1
        print(f"    ❌ {r3['condition']}")
    
    print("  Actions:")
    a2 = test_action('DECREASE to 0%', ExpertActionType.DECREASE_INSTRUMENT_SHARE, {'target_percent': 0.0}, low_confidence_rec, OrderRecommendation.SELL, existing_order)
    results['actions'].append(a2)
    results['total_tests'] += 1
    if a2['success']:
        results['passed'] += 1
        print(f"    ✅ {a2['action']}")
    else:
        results['failed'] += 1
        print(f"    ❌ {a2['action']}")
    
    # ========================================================================
    # SUMMARY
    # ========================================================================
    print()
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"Total Tests:  {results['total_tests']}")
    print(f"Passed:       {results['passed']} ✅")
    print(f"Failed:       {results['failed']} ❌")
    print(f"Success Rate: {(results['passed'] / results['total_tests'] * 100):.1f}%")
    print()
    
    if results['failed'] == 0:
        print("🎉 ALL TESTS PASSED! 🎉")
    else:
        print(f"⚠️  {results['failed']} test(s) failed. Review output above.")
    
    print("=" * 80)
    
    return results


if __name__ == '__main__':
    try:
        results = run_comprehensive_tests()
        
        # Exit with appropriate code
        sys.exit(0 if results['failed'] == 0 else 1)
        
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

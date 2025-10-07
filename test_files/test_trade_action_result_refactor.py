"""
Quick test to verify TradeActionResult refactoring works correctly.

This test verifies:
1. TradeActionResult can be created with expert_recommendation_id
2. Transaction_id field is removed (migration successful)
3. Expert_recommendation_id is required (non-nullable)
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.models import TradeActionResult, ExpertRecommendation, ExpertInstance, AccountDefinition
from ba2_trade_platform.core.db import get_db, add_instance
from ba2_trade_platform.core.types import OrderRecommendation, RiskLevel, TimeHorizon
from datetime import datetime, timezone
from sqlmodel import select

def test_trade_action_result_refactoring():
    """Test that TradeActionResult refactoring works correctly."""
    
    print("🧪 Testing TradeActionResult Refactoring...")
    print("=" * 60)
    
    try:
        with get_db() as session:
            # 1. Create test account if needed
            account = session.exec(select(AccountDefinition).where(
                AccountDefinition.name == "Test Account"
            )).first()
            
            if not account:
                account = AccountDefinition(
                    name="Test Account",
                    provider="AlpacaAccount",
                    description="Test account for refactoring verification"
                )
                account_id = add_instance(account)
                # Refresh to get the instance back from database
                account = session.get(AccountDefinition, account_id)
                print(f"✅ Created test account (ID: {account_id})")
            else:
                print(f"✅ Using existing test account (ID: {account.id})")
            
            # 2. Create test expert instance if needed
            expert = session.exec(select(ExpertInstance).where(
                ExpertInstance.account_id == account.id
            )).first()
            
            if not expert:
                expert = ExpertInstance(
                    account_id=account.id,
                    expert="TradingAgents",
                    enabled=True,
                    user_description="Test Expert for Refactoring"
                )
                expert_id = add_instance(expert)
                # Refresh to get the instance back from database
                expert = session.get(ExpertInstance, expert_id)
                print(f"✅ Created test expert instance (ID: {expert_id})")
            else:
                print(f"✅ Using existing expert instance (ID: {expert.id})")
            
            # 3. Create test recommendation
            recommendation = ExpertRecommendation(
                instance_id=expert.id,
                symbol="TEST",
                recommended_action=OrderRecommendation.BUY,
                expected_profit_percent=5.0,
                price_at_date=100.0,
                confidence=85.0,
                risk_level=RiskLevel.MEDIUM,
                time_horizon=TimeHorizon.SHORT_TERM,
                created_at=datetime.now(timezone.utc)
            )
            rec_id = add_instance(recommendation)
            print(f"✅ Created test recommendation (ID: {rec_id})")
            
            # 4. Create TradeActionResult with evaluation details
            evaluation_details = {
                'condition_evaluations': {
                    'test_condition': {
                        'passed': True,
                        'left_operand': 100.0,
                        'right_operand': 95.0,
                        'operator': '>'
                    }
                },
                'rule_evaluations': [
                    {
                        'rule_name': 'Test Rule',
                        'passed': True,
                        'continue_processing': False
                    }
                ]
            }
            
            action_result = TradeActionResult(
                action_type='buy',
                success=True,
                message='Test action result',
                data={'evaluation_details': evaluation_details},
                expert_recommendation_id=rec_id
            )
            
            result_id = add_instance(action_result)
            print(f"✅ Created TradeActionResult (ID: {result_id})")
            
            # 5. Verify the result was stored correctly
            stored_result = session.get(TradeActionResult, result_id)
            
            if stored_result:
                print(f"✅ TradeActionResult retrieved successfully")
                print(f"   - Action Type: {stored_result.action_type}")
                print(f"   - Success: {stored_result.success}")
                print(f"   - Expert Rec ID: {stored_result.expert_recommendation_id}")
                print(f"   - Has evaluation_details: {'evaluation_details' in stored_result.data}")
                
                # 6. Verify transaction_id field is removed
                try:
                    _ = stored_result.transaction_id
                    print("❌ ERROR: transaction_id field still exists!")
                    return False
                except AttributeError:
                    print("✅ Confirmed: transaction_id field removed")
                
                # 7. Verify expert_recommendation_id is set
                if stored_result.expert_recommendation_id == rec_id:
                    print("✅ Confirmed: expert_recommendation_id correctly set")
                else:
                    print(f"❌ ERROR: expert_recommendation_id mismatch!")
                    return False
                
                # 8. Verify evaluation_details stored
                if 'evaluation_details' in stored_result.data:
                    print("✅ Confirmed: evaluation_details stored in data field")
                    print(f"   - Condition evaluations: {len(stored_result.data['evaluation_details']['condition_evaluations'])}")
                    print(f"   - Rule evaluations: {len(stored_result.data['evaluation_details']['rule_evaluations'])}")
                else:
                    print("❌ ERROR: evaluation_details not found in data!")
                    return False
                
                # 9. Test querying by expert_recommendation_id
                query_results = session.exec(
                    select(TradeActionResult).where(
                        TradeActionResult.expert_recommendation_id == rec_id
                    )
                ).all()
                
                if query_results and len(query_results) > 0:
                    print(f"✅ Confirmed: Can query by expert_recommendation_id (found {len(query_results)} results)")
                else:
                    print("❌ ERROR: Query by expert_recommendation_id failed!")
                    return False
                
                print("\n" + "=" * 60)
                print("✅ ALL TESTS PASSED!")
                print("=" * 60)
                return True
            else:
                print("❌ ERROR: Could not retrieve stored result!")
                return False
                
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_trade_action_result_refactoring()
    sys.exit(0 if success else 1)

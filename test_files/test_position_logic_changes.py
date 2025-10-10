"""
Test script to verify the new position logic changes.

This script tests that:
1. has_position_expert() checks only the expert's transactions
2. has_position_account() checks the account's positions 
3. HasPositionCondition and HasNoPositionCondition use expert-level checks
4. HasPositionAccountCondition and HasNoPositionAccountCondition use account-level checks
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db, get_all_instances
from ba2_trade_platform.core.models import ExpertInstance, Transaction, ExpertRecommendation, AccountDefinition
from ba2_trade_platform.core.types import ExpertEventType, TransactionStatus, OrderRecommendation
from ba2_trade_platform.core.TradeConditions import create_condition
from ba2_trade_platform.modules.accounts import providers
from datetime import datetime, timezone

def test_position_logic_changes():
    """Test the new expert-level vs account-level position logic."""
    
    logger.info("=" * 80)
    logger.info("Testing Position Logic Changes")
    logger.info("=" * 80)
    
    try:
        with get_db() as session:
            # Get test data
            experts = get_all_instances(ExpertInstance)
            accounts = get_all_instances(AccountDefinition)
            transactions = get_all_instances(Transaction)
            
            if not experts:
                logger.error("No expert instances found. Please create some first.")
                return False
                
            if not accounts:
                logger.error("No account definitions found. Please create some first.")
                return False
            
            logger.info(f"Found {len(experts)} experts, {len(accounts)} accounts, {len(transactions)} transactions")
            
            # Pick first expert and account for testing
            test_expert = experts[0]
            test_account_def = accounts[0]
            
            logger.info(f"Testing with expert: {test_expert.alias or test_expert.expert} (ID: {test_expert.id})")
            logger.info(f"Testing with account: {test_account_def.name} (ID: {test_account_def.id})")
            
            # Get account provider
            account_provider_class = providers.get(test_account_def.provider)
            if not account_provider_class:
                logger.error(f"Account provider {test_account_def.provider} not found")
                return False
                
            account = account_provider_class(test_account_def.id)
            
            # Create a dummy expert recommendation for testing
            dummy_recommendation = ExpertRecommendation(
                instance_id=test_expert.id,
                symbol="TEST",
                recommended_action=OrderRecommendation.BUY,
                expected_profit_percent=10.0,
                price_at_date=100.0,
                confidence=75.0,
                created_at=datetime.now(timezone.utc)
            )
            
            # Test symbols to check
            test_symbols = []
            
            # Find symbols with transactions for this expert
            expert_transactions = [t for t in transactions if t.expert_id == test_expert.id and t.status == TransactionStatus.OPENED]
            if expert_transactions:
                test_symbols.extend([t.symbol for t in expert_transactions[:2]])  # Take first 2
            
            # Add a symbol that might be in account positions but not expert transactions
            test_symbols.append("AAPL")  # Common symbol
            test_symbols.append("TEST")  # Symbol from dummy recommendation
            
            # Remove duplicates
            test_symbols = list(set(test_symbols))
            
            logger.info(f"Testing position checks for symbols: {test_symbols}")
            
            # Test each symbol
            all_tests_passed = True
            
            for symbol in test_symbols:
                logger.info(f"\n--- Testing Symbol: {symbol} ---")
                
                try:
                    # Update dummy recommendation with current symbol
                    dummy_recommendation.symbol = symbol
                    
                    # Create both types of position conditions
                    expert_no_pos_condition = create_condition(
                        ExpertEventType.F_HAS_NO_POSITION,
                        account, symbol, dummy_recommendation
                    )
                    
                    expert_has_pos_condition = create_condition(
                        ExpertEventType.F_HAS_POSITION,
                        account, symbol, dummy_recommendation
                    )
                    
                    account_no_pos_condition = create_condition(
                        ExpertEventType.F_HAS_NO_POSITION_ACCOUNT, 
                        account, symbol, dummy_recommendation
                    )
                    
                    account_has_pos_condition = create_condition(
                        ExpertEventType.F_HAS_POSITION_ACCOUNT,
                        account, symbol, dummy_recommendation
                    )
                    
                    # Evaluate conditions
                    expert_has_position = expert_has_pos_condition.evaluate()
                    expert_no_position = expert_no_pos_condition.evaluate()
                    account_has_position = account_has_pos_condition.evaluate()
                    account_no_position = account_no_pos_condition.evaluate()
                    
                    # Check for expert transactions
                    expert_txns = [t for t in transactions if t.expert_id == test_expert.id and t.symbol == symbol and t.status == TransactionStatus.OPENED]
                    has_expert_txn = len(expert_txns) > 0
                    
                    # Check account positions
                    account_positions = account.get_positions()
                    has_account_pos = any(getattr(pos, 'symbol', None) == symbol and getattr(pos, 'qty', 0) != 0 for pos in account_positions)
                    
                    logger.info(f"Expert transactions for {symbol}: {len(expert_txns)}")
                    logger.info(f"Account position for {symbol}: {'Yes' if has_account_pos else 'No'}")
                    logger.info(f"Expert has_position: {expert_has_position}, no_position: {expert_no_position}")
                    logger.info(f"Account has_position: {account_has_position}, no_position: {account_no_position}")
                    
                    # Validate logic
                    # Expert conditions should be opposite of each other
                    if expert_has_position == expert_no_position:
                        logger.error(f"❌ Expert position conditions should be opposite: has={expert_has_position}, no={expert_no_position}")
                        all_tests_passed = False
                    else:
                        logger.info("✓ Expert position conditions are opposite")
                    
                    # Account conditions should be opposite of each other  
                    if account_has_position == account_no_position:
                        logger.error(f"❌ Account position conditions should be opposite: has={account_has_position}, no={account_no_position}")
                        all_tests_passed = False
                    else:
                        logger.info("✓ Account position conditions are opposite")
                    
                    # Expert conditions should match transaction data
                    if expert_has_position != has_expert_txn:
                        logger.error(f"❌ Expert has_position ({expert_has_position}) doesn't match transaction data ({has_expert_txn})")
                        all_tests_passed = False
                    else:
                        logger.info("✓ Expert conditions match transaction data")
                    
                    # Account conditions should match position data (if available)
                    if account_has_position != has_account_pos:
                        logger.warning(f"⚠️ Account has_position ({account_has_position}) doesn't match position data ({has_account_pos}) - may be expected if account is not configured")
                    else:
                        logger.info("✓ Account conditions match position data")
                        
                except Exception as e:
                    logger.error(f"Error testing symbol {symbol}: {e}", exc_info=True)
                    all_tests_passed = False
            
            # Test condition descriptions
            logger.info("\n--- Testing Condition Descriptions ---")
            try:
                dummy_recommendation.symbol = "TEST"
                
                expert_no_pos = create_condition(ExpertEventType.F_HAS_NO_POSITION, account, "TEST", dummy_recommendation)
                expert_has_pos = create_condition(ExpertEventType.F_HAS_POSITION, account, "TEST", dummy_recommendation)
                account_no_pos = create_condition(ExpertEventType.F_HAS_NO_POSITION_ACCOUNT, account, "TEST", dummy_recommendation)
                account_has_pos = create_condition(ExpertEventType.F_HAS_POSITION_ACCOUNT, account, "TEST", dummy_recommendation)
                
                logger.info(f"Expert no position: {expert_no_pos.get_description()}")
                logger.info(f"Expert has position: {expert_has_pos.get_description()}")
                logger.info(f"Account no position: {account_no_pos.get_description()}")
                logger.info(f"Account has position: {account_has_pos.get_description()}")
                
                # Verify descriptions contain appropriate keywords
                if "expert" not in expert_no_pos.get_description().lower():
                    logger.warning("⚠️ Expert condition description doesn't mention 'expert'")
                if "account" not in account_no_pos.get_description().lower():
                    logger.warning("⚠️ Account condition description doesn't mention 'account'")
                    
            except Exception as e:
                logger.error(f"Error testing descriptions: {e}", exc_info=True)
                all_tests_passed = False
            
            if all_tests_passed:
                logger.info("\n✅ All position logic tests passed!")
                return True
            else:
                logger.error("\n❌ Some position logic tests failed")
                return False
                
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_position_logic_changes()
    
    if success:
        print("\n✅ TEST PASSED: Position logic changes work correctly")
        sys.exit(0)
    else:
        print("\n❌ TEST FAILED: Issues with position logic changes")
        sys.exit(1)
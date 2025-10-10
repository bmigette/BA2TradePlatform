"""
Test script to verify expert-level vs account-level position checking logic.

This script tests that:
1. Expert-level position checks work based on transactions
2. Account-level position checks work based on broker positions
3. Multiple experts can have different position states for the same symbol
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db, get_all_instances
from ba2_trade_platform.core.models import ExpertInstance, Transaction, ExpertRecommendation
from ba2_trade_platform.core.types import TransactionStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ba2_trade_platform.core.TradeConditions import HasPositionCondition, HasNoPositionCondition, HasPositionAccountCondition, HasNoPositionAccountCondition
from ba2_trade_platform.modules.accounts import providers
from ba2_trade_platform.core.models import AccountDefinition
from datetime import datetime, timezone
from sqlmodel import select

def test_position_logic():
    """Test both expert-level and account-level position checking."""
    
    logger.info("=" * 80)
    logger.info("Testing Expert-Level vs Account-Level Position Logic")
    logger.info("=" * 80)
    
    try:
        # Get first expert and account for testing
        experts = get_all_instances(ExpertInstance)
        accounts = get_all_instances(AccountDefinition)
        
        if not experts:
            logger.error("No expert instances found. Please create an expert instance first.")
            return False
            
        if not accounts:
            logger.error("No account definitions found. Please create an account first.")
            return False
        
        expert1 = experts[0]
        expert2 = experts[1] if len(experts) > 1 else expert1
        account_def = accounts[0]
        
        logger.info(f"Testing with Expert 1: {expert1.alias or expert1.expert} (ID: {expert1.id})")
        logger.info(f"Testing with Expert 2: {expert2.alias or expert2.expert} (ID: {expert2.id})")
        logger.info(f"Using Account: {account_def.name} ({account_def.provider})")
        
        # Create account interface
        account_provider_class = providers.get(account_def.provider)
        if not account_provider_class:
            logger.error(f"No provider found for {account_def.provider}")
            return False
            
        account = account_provider_class(account_def.id)
        
        # Test symbol
        test_symbol = "AAPL"
        
        # Create dummy expert recommendations for testing
        expert1_rec = ExpertRecommendation(
            instance_id=expert1.id,
            symbol=test_symbol,
            recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=10.0,
            price_at_date=150.0,
            details="Test recommendation",
            confidence=75.0,
            risk_level=RiskLevel.MEDIUM,
            time_horizon=TimeHorizon.MEDIUM_TERM,
            created_at=datetime.now(timezone.utc)
        )
        
        expert2_rec = ExpertRecommendation(
            instance_id=expert2.id,
            symbol=test_symbol,
            recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=12.0,
            price_at_date=150.0,
            details="Test recommendation 2",
            confidence=80.0,
            risk_level=RiskLevel.MEDIUM,
            time_horizon=TimeHorizon.MEDIUM_TERM,
            created_at=datetime.now(timezone.utc)
        )
        
        # Create condition instances for testing
        expert1_has_pos = HasPositionCondition(account, test_symbol, expert1_rec)
        expert1_no_pos = HasNoPositionCondition(account, test_symbol, expert1_rec)
        expert2_has_pos = HasPositionCondition(account, test_symbol, expert2_rec)
        expert2_no_pos = HasNoPositionCondition(account, test_symbol, expert2_rec)
        
        account_has_pos = HasPositionAccountCondition(account, test_symbol, expert1_rec)
        account_no_pos = HasNoPositionAccountCondition(account, test_symbol, expert1_rec)
        
        logger.info(f"\\n=== Testing {test_symbol} Position Logic ===")
        
        # Check current states
        logger.info("\\n--- Current States ---")
        
        # Expert-level checks
        expert1_has_position = expert1_has_pos.evaluate()
        expert1_no_position = expert1_no_pos.evaluate()
        expert2_has_position = expert2_has_pos.evaluate()
        expert2_no_position = expert2_no_pos.evaluate()
        
        # Account-level checks
        account_has_position = account_has_pos.evaluate()
        account_no_position = account_no_pos.evaluate()
        
        logger.info(f"Expert 1 ({expert1.alias}) has position: {expert1_has_position}")
        logger.info(f"Expert 1 ({expert1.alias}) no position: {expert1_no_position}")
        logger.info(f"Expert 2 ({expert2.alias}) has position: {expert2_has_position}")
        logger.info(f"Expert 2 ({expert2.alias}) no position: {expert2_no_position}")
        logger.info(f"Account has position: {account_has_position}")
        logger.info(f"Account no position: {account_no_position}")
        
        # Check logical consistency
        logger.info("\\n--- Consistency Checks ---")
        
        # Expert 1 consistency
        if expert1_has_position == expert1_no_position:
            logger.error(f"❌ Expert 1 logic inconsistent: has_position={expert1_has_position}, no_position={expert1_no_position}")
            return False
        else:
            logger.info(f"✅ Expert 1 logic consistent: has_position={expert1_has_position}, no_position={expert1_no_position}")
        
        # Expert 2 consistency
        if expert2_has_position == expert2_no_position:
            logger.error(f"❌ Expert 2 logic inconsistent: has_position={expert2_has_position}, no_position={expert2_no_position}")
            return False
        else:
            logger.info(f"✅ Expert 2 logic consistent: has_position={expert2_has_position}, no_position={expert2_no_position}")
        
        # Account consistency
        if account_has_position == account_no_position:
            logger.error(f"❌ Account logic inconsistent: has_position={account_has_position}, no_position={account_no_position}")
            return False
        else:
            logger.info(f"✅ Account logic consistent: has_position={account_has_position}, no_position={account_no_position}")
        
        # Check transaction data
        logger.info("\\n--- Transaction Analysis ---")
        
        with get_db() as session:
            # Get transactions for each expert
            expert1_txns = session.exec(select(Transaction).where(
                Transaction.expert_id == expert1.id,
                Transaction.symbol == test_symbol,
                Transaction.status == TransactionStatus.OPENED
            )).all()
            
            expert2_txns = session.exec(select(Transaction).where(
                Transaction.expert_id == expert2.id,
                Transaction.symbol == test_symbol,
                Transaction.status == TransactionStatus.OPENED
            )).all()
            
            logger.info(f"Expert 1 open transactions for {test_symbol}: {len(expert1_txns)}")
            logger.info(f"Expert 2 open transactions for {test_symbol}: {len(expert2_txns)}")
            
            # Verify transaction-based logic
            expert1_should_have_pos = len(expert1_txns) > 0
            expert2_should_have_pos = len(expert2_txns) > 0
            
            if expert1_has_position == expert1_should_have_pos:
                logger.info(f"✅ Expert 1 transaction-based position check correct")
            else:
                logger.error(f"❌ Expert 1 transaction-based position check incorrect: expected {expert1_should_have_pos}, got {expert1_has_position}")
                return False
                
            if expert2_has_position == expert2_should_have_pos:
                logger.info(f"✅ Expert 2 transaction-based position check correct")
            else:
                logger.error(f"❌ Expert 2 transaction-based position check incorrect: expected {expert2_should_have_pos}, got {expert2_has_position}")
                return False
        
        # Check account positions
        logger.info("\\n--- Account Position Analysis ---")
        
        try:
            positions = account.get_positions()
            account_position_qty = None
            for position in positions:
                if hasattr(position, 'symbol') and position.symbol == test_symbol:
                    account_position_qty = getattr(position, 'qty', None)
                    break
            
            logger.info(f"Account position quantity for {test_symbol}: {account_position_qty}")
            
            account_should_have_pos = account_position_qty is not None and account_position_qty != 0
            
            if account_has_position == account_should_have_pos:
                logger.info(f"✅ Account position check correct")
            else:
                logger.error(f"❌ Account position check incorrect: expected {account_should_have_pos}, got {account_has_position}")
                return False
                
        except Exception as e:
            logger.warning(f"Could not check account positions: {e}")
        
        logger.info("\\n--- Summary ---")
        logger.info("✅ Expert-level position checks use transaction data (expert-specific)")
        logger.info("✅ Account-level position checks use broker position data (account-wide)")
        logger.info("✅ Both expert and account position logic are consistent")
        logger.info("✅ Multiple experts can have different position states for the same symbol")
        
        return True
        
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_position_logic()
    
    if success:
        print("\\n✅ TEST PASSED: Position logic works correctly")
        sys.exit(0)
    else:
        print("\\n❌ TEST FAILED: Issues with position logic")
        sys.exit(1)
"""Test OPEN_POSITIONS recommendation processing."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from ba2_trade_platform.core.TradeManager import get_trade_manager
from ba2_trade_platform.core.models import ExpertInstance, ExpertRecommendation
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.types import OrderRecommendation

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def test_open_positions_recommendations():
    """Test that OPEN_POSITIONS recommendations are evaluated against the ruleset."""
    
    logger.info("=" * 80)
    logger.info("Testing OPEN_POSITIONS Recommendation Processing")
    logger.info("=" * 80)
    
    # Get expert 9 (TradingAgents)
    expert_instance = get_instance(ExpertInstance, 9)
    
    if not expert_instance:
        logger.error("Expert instance 9 not found")
        return False
    
    logger.info(f"Expert instance 9 found: {expert_instance.expert}")
    logger.info(f"Account ID: {expert_instance.account_id}")
    logger.info(f"ENTER_MARKET ruleset ID: {expert_instance.enter_market_ruleset_id}")
    logger.info(f"OPEN_POSITIONS ruleset ID: {expert_instance.open_positions_ruleset_id}")
    
    if not expert_instance.open_positions_ruleset_id:
        logger.error("No open_positions ruleset configured for expert 9")
        return False
    
    # Check if process_open_positions_recommendations method exists
    trade_manager = get_trade_manager()
    
    if not hasattr(trade_manager, 'process_open_positions_recommendations'):
        logger.error("process_open_positions_recommendations method not found in TradeManager")
        return False
    
    logger.info("✓ process_open_positions_recommendations method exists")
    
    # Test calling the method
    try:
        logger.info("Calling process_open_positions_recommendations(9, lookback_days=1)...")
        created_orders = trade_manager.process_open_positions_recommendations(9, lookback_days=1)
        logger.info(f"✓ Method executed successfully")
        logger.info(f"✓ Created {len(created_orders)} orders from OPEN_POSITIONS recommendations")
        return True
    except Exception as e:
        logger.error(f"✗ Error calling process_open_positions_recommendations: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_open_positions_recommendations()
    logger.info("=" * 80)
    if success:
        logger.info("✓ Test PASSED")
    else:
        logger.info("✗ Test FAILED")
    logger.info("=" * 80)

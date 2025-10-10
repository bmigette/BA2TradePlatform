"""
Test script to verify FMPSenateTrade always returns positive expected profit.

This script tests that:
1. BUY signals have positive expected profit
2. SELL signals have positive expected profit (not negative)
3. Expected profit is always >= 0
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_instance, get_all_instances
from ba2_trade_platform.core.models import ExpertInstance, ExpertRecommendation
from ba2_trade_platform.core.types import OrderRecommendation

def test_expected_profit_always_positive():
    """Test that FMPSenateTrade returns positive expected profit for all signals."""
    
    logger.info("=" * 80)
    logger.info("Testing FMPSenateTrade Expected Profit")
    logger.info("=" * 80)
    
    try:
        # Get FMPSenateTrade expert instance
        expert_instances = get_all_instances(ExpertInstance)
        fmp_instance = None
        
        for instance in expert_instances:
            if 'FMPSenate' in instance.expert:
                fmp_instance = instance
                break
        
        if not fmp_instance:
            logger.error("No FMPSenateTrade expert instance found. Please create one first.")
            return False
        
        logger.info(f"Using FMP expert instance: {fmp_instance.alias or fmp_instance.expert} (ID: {fmp_instance.id})")
        
        # Get recent recommendations from this expert
        from sqlmodel import select
        from ba2_trade_platform.core.db import get_db
        from datetime import datetime, timedelta, timezone
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
        
        with get_db() as session:
            statement = select(ExpertRecommendation).where(
                ExpertRecommendation.instance_id == fmp_instance.id,
                ExpertRecommendation.created_at >= cutoff_date
            ).limit(10)
            
            recommendations = session.exec(statement).all()
            
            if not recommendations:
                logger.warning(f"No recent recommendations found for expert {fmp_instance.id}")
                return False
            
            logger.info(f"Found {len(recommendations)} recent recommendations to test")
            
            all_positive = True
            buy_count = sell_count = hold_count = 0
            
            for rec in recommendations:
                expected_profit = rec.expected_profit_percent
                action = rec.recommended_action
                
                if action == OrderRecommendation.BUY:
                    buy_count += 1
                elif action == OrderRecommendation.SELL:
                    sell_count += 1
                else:
                    hold_count += 1
                
                if expected_profit < 0:
                    logger.error(f"❌ Recommendation {rec.id}: {action.value} signal has NEGATIVE expected profit: {expected_profit:.1f}%")
                    all_positive = False
                else:
                    logger.info(f"✓ Recommendation {rec.id}: {action.value} signal has positive expected profit: {expected_profit:.1f}%")
            
            logger.info(f"\nSignal breakdown:")
            logger.info(f"- BUY signals: {buy_count}")
            logger.info(f"- SELL signals: {sell_count}")
            logger.info(f"- HOLD signals: {hold_count}")
            
            if all_positive:
                logger.info("✅ All recommendations have positive expected profit values")
                return True
            else:
                logger.error("❌ Some recommendations have negative expected profit values")
                return False
    
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_expected_profit_always_positive()
    
    if success:
        print("\n✅ TEST PASSED: All expected profit values are positive")
        sys.exit(0)
    else:
        print("\n❌ TEST FAILED: Some expected profit values are negative")
        sys.exit(1)
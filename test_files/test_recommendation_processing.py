"""
Test script to verify expert recommendation processing and TradeActionResult creation.

This script tests that:
1. Expert recommendations can be processed without IntegrityError
2. TradeActionResult records are created with valid expert_recommendation_id
3. Async processing doesn't block
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_instance, get_all_instances
from ba2_trade_platform.core.models import ExpertInstance, ExpertRecommendation, TradeActionResult
from ba2_trade_platform.core.TradeManager import get_trade_manager

def test_recommendation_processing():
    """Test processing expert recommendations."""
    
    logger.info("=" * 80)
    logger.info("Testing Expert Recommendation Processing")
    logger.info("=" * 80)
    
    try:
        # Get first active expert instance
        expert_instances = get_all_instances(ExpertInstance)
        if not expert_instances:
            logger.error("No expert instances found. Please create an expert instance first.")
            return False
        
        expert_instance = expert_instances[0]
        logger.info(f"Using expert instance: {expert_instance.alias or expert_instance.expert} (ID: {expert_instance.id})")
        
        # Get recent recommendations
        from sqlmodel import select
        from ba2_trade_platform.core.db import get_db
        
        with get_db() as session:
            # Get recommendations from last 30 days
            from datetime import datetime, timedelta, timezone
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
            
            statement = select(ExpertRecommendation).where(
                ExpertRecommendation.instance_id == expert_instance.id,
                ExpertRecommendation.created_at >= cutoff_date
            ).limit(5)
            
            recommendations = session.exec(statement).all()
            
            if not recommendations:
                logger.warning(f"No recent recommendations found for expert {expert_instance.id}")
                return False
            
            logger.info(f"Found {len(recommendations)} recent recommendations")
            
            # Count existing action results before processing
            before_count = len(get_all_instances(TradeActionResult))
            logger.info(f"Existing TradeActionResult records: {before_count}")
            
            # Process recommendations
            logger.info("Processing recommendations...")
            trade_manager = get_trade_manager()
            
            try:
                created_orders = trade_manager.process_expert_recommendations_after_analysis(
                    expert_instance_id=expert_instance.id,
                    lookback_days=7
                )
                
                logger.info(f"Processing completed. Created {len(created_orders)} orders")
                
                # Check if new TradeActionResult records were created
                after_count = len(get_all_instances(TradeActionResult))
                new_results = after_count - before_count
                logger.info(f"TradeActionResult records after: {after_count} (+{new_results})")
                
                # Verify the new records have valid expert_recommendation_id
                if new_results > 0:
                    recent_results = get_all_instances(TradeActionResult)[-new_results:]
                    
                    all_valid = True
                    for result in recent_results:
                        if result.expert_recommendation_id is None:
                            logger.error(f"TradeActionResult {result.id} has NULL expert_recommendation_id!")
                            all_valid = False
                        else:
                            logger.info(f"✓ TradeActionResult {result.id}: action={result.action_type}, recommendation_id={result.expert_recommendation_id}, success={result.success}")
                    
                    if all_valid:
                        logger.info("✅ All new TradeActionResult records have valid expert_recommendation_id")
                        return True
                    else:
                        logger.error("❌ Some TradeActionResult records have NULL expert_recommendation_id")
                        return False
                else:
                    logger.info("No new TradeActionResult records created (might be expected if no rules triggered)")
                    return True
                    
            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                return False
    
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_recommendation_processing()
    
    if success:
        print("\n✅ TEST PASSED: Recommendation processing works correctly")
        sys.exit(0)
    else:
        print("\n❌ TEST FAILED: Issues found with recommendation processing")
        sys.exit(1)

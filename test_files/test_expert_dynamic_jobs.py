#!/usr/bin/env python3
"""
Test script to verify EXPERT and DYNAMIC symbol job creation functionality
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform import config
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.JobManager import get_job_manager
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.db import get_db, add_instance, get_instance, delete_instance, get_all_instances
from ba2_trade_platform.core.interfaces.MarketExpertInterface import MarketExpertInterface
from typing import Dict, Any, List, Optional

class TestExpertWithRecommendations(MarketExpertInterface):
    """Test expert that can recommend instruments."""
    
    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        return {
            'can_recommend_instruments': True,
            'should_expand_instrument_jobs': True,
            'description': 'Test expert with instrument recommendation capability'
        }
    
    def get_recommended_instruments(self) -> List[str]:
        """Return a list of recommended instruments."""
        return ['AAPL', 'GOOGL', 'MSFT', 'NVDA', 'TSLA']
    
    @property
    def description(self) -> str:
        """Get expert description."""
        return "Test expert for EXPERT/DYNAMIC job creation"
    
    def run_analysis(self, symbol: str, subtype: str = None) -> dict:
        """Run market analysis (stub for testing)."""
        return {"symbol": symbol, "recommendation": "hold", "confidence": 50.0}
    
    def render_market_analysis(self, recommendations: list) -> str:
        """Render analysis results (stub for testing)."""
        return "Test analysis results"

def test_expert_symbol_job_creation():
    """Test that EXPERT symbol jobs are created correctly."""
    logger.info("🧪 Testing EXPERT Symbol Job Creation")
    logger.info("=" * 60)
    
    # Load configuration
    config.load_config_from_env()
    
    # Create test expert instance
    test_instance = ExpertInstance(
        expert='TestExpertWithRecommendations',
        alias='Test Expert',
        enabled=True,
        virtual_equity_pct=100.0,
        account_id=1  # Assuming account 1 exists
    )
    
    expert_id = None
    try:
        expert_id = add_instance(test_instance)
        logger.info(f"Created test expert instance with ID: {expert_id}")
        
        # Update the instance with test settings
        expert_instance = get_instance(ExpertInstance, expert_id)
        
        # Test expert method: Set instrument_selection_method to 'expert'
        logger.info("Testing 'expert' selection method...")
        
        # Create the expert object to save settings
        expert = TestExpertWithRecommendations(expert_id)
        expert.save_setting('instrument_selection_method', 'expert', setting_type='str')
        
        # Create a proper schedule configuration
        schedule_config = {
            'days': {
                'monday': True,
                'tuesday': True,
                'wednesday': True,
                'thursday': True,
                'friday': True,
                'saturday': False,
                'sunday': False
            },
            'times': ['09:30']
        }
        expert.save_setting('execution_schedule_enter_market', schedule_config, setting_type='json')
        
        logger.info("Saved expert settings with 'expert' selection method")
        
        # Test JobManager's _get_enabled_instruments method
        job_manager = get_job_manager()
        enabled_instruments = job_manager._get_enabled_instruments(expert_id)
        
        logger.info(f"JobManager returned enabled instruments: {enabled_instruments}")
        
        if enabled_instruments == ["EXPERT"]:
            logger.info("✅ Expert selection method correctly returns ['EXPERT'] symbol")
        else:
            logger.error(f"❌ Expected ['EXPERT'], got {enabled_instruments}")
        
        # Test dynamic method: Set instrument_selection_method to 'dynamic'
        logger.info("\nTesting 'dynamic' selection method...")
        expert.save_setting('instrument_selection_method', 'dynamic', setting_type='str')
        expert.save_setting('ai_instrument_prompt', 'Select tech stocks with high growth potential', setting_type='str')
        
        enabled_instruments = job_manager._get_enabled_instruments(expert_id)
        logger.info(f"JobManager returned enabled instruments: {enabled_instruments}")
        
        if enabled_instruments == ["DYNAMIC"]:
            logger.info("✅ Dynamic selection method correctly returns ['DYNAMIC'] symbol")
        else:
            logger.error(f"❌ Expected ['DYNAMIC'], got {enabled_instruments}")
        
        # Test job scheduling
        logger.info("\nTesting job scheduling...")
        
        # Clear existing jobs first
        job_manager.clear_expert_jobs(expert_id)
        
        # Schedule jobs for this expert
        job_manager._schedule_expert_jobs(expert_instance)
        
        # Check if jobs were created
        scheduled_jobs = job_manager.get_scheduled_jobs()
        expert_jobs = [job for job in scheduled_jobs if f"expert_{expert_id}" in job['id']]
        
        logger.info(f"Found {len(expert_jobs)} scheduled jobs for expert {expert_id}")
        for job in expert_jobs:
            logger.info(f"  - Job ID: {job['id']}")
            logger.info(f"  - Next run: {job.get('next_run_time', 'Unknown')}")
        
        if expert_jobs:
            logger.info("✅ Scheduled jobs created successfully")
            
            # Check if DYNAMIC symbol is used in job names
            dynamic_jobs = [job for job in expert_jobs if "DYNAMIC" in job['id']]
            if dynamic_jobs:
                logger.info("✅ DYNAMIC symbol found in scheduled job names")
            else:
                logger.warning("⚠️ No DYNAMIC symbol found in job names")
        else:
            logger.error("❌ No scheduled jobs created")
        
        # Test job execution (dry run)
        logger.info("\nTesting job execution (dry run)...")
        try:
            # This would normally be called by the scheduler
            job_manager._execute_scheduled_analysis(expert_id, "DYNAMIC", "enter_market")
            logger.info("✅ Dynamic analysis execution completed without errors")
        except Exception as e:
            logger.warning(f"⚠️ Dynamic analysis execution failed (expected if no OpenAI API key): {e}")
        
    except Exception as e:
        logger.error(f"❌ Test failed: {e}", exc_info=True)
    
    finally:
        # Clean up test data
        if expert_id:
            try:
                # Clean up by deleting from database directly
                from sqlmodel import Session, select, delete
                engine = get_db()
                with Session(engine.bind) as session:
                    # Delete expert settings first
                    from ba2_trade_platform.core.models import ExpertSetting
                    delete_settings = delete(ExpertSetting).where(ExpertSetting.expert_instance_id == expert_id)
                    session.exec(delete_settings)
                    
                    # Delete expert instance
                    delete_expert = delete(ExpertInstance).where(ExpertInstance.id == expert_id)
                    session.exec(delete_expert)
                    session.commit()
                
                logger.info(f"Cleaned up test expert instance {expert_id}")
            except Exception as e:
                logger.warning(f"Failed to clean up test expert: {e}")
    
    logger.info("=" * 60)
    logger.info("🏁 EXPERT Symbol Job Creation Test Complete")

if __name__ == "__main__":
    test_expert_symbol_job_creation()
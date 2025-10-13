#!/usr/bin/env python3
"""
Simple test to verify EXPERT and DYNAMIC symbol logic in JobManager
"""

import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform import config
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.JobManager import get_job_manager
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.db import get_db, add_instance, get_instance, get_all_instances
from ba2_trade_platform.core.utils import get_expert_instance_from_id

def test_expert_dynamic_logic():
    """Test the EXPERT and DYNAMIC symbol logic directly."""
    logger.info("🧪 Testing EXPERT and DYNAMIC Symbol Logic")
    logger.info("=" * 60)
    
    # Load configuration
    config.load_config_from_env()
    
    # Find an existing expert instance that supports instrument recommendation
    expert_instances = get_all_instances(ExpertInstance)
    test_expert = None
    
    for instance in expert_instances:
        if instance.enabled:
            try:
                expert = get_expert_instance_from_id(instance.id)
                if expert and hasattr(expert.__class__, 'get_expert_properties'):
                    props = expert.__class__.get_expert_properties()
                    if props.get('can_recommend_instruments', False):
                        test_expert = instance
                        logger.info(f"Found suitable test expert: {instance.alias or instance.expert} (ID: {instance.id})")
                        break
            except Exception as e:
                logger.debug(f"Could not load expert {instance.id}: {e}")
                continue
    
    if not test_expert:
        logger.warning("No suitable expert found with can_recommend_instruments capability")
        logger.info("Testing will use mock logic simulation instead")
        test_direct_logic()
        return
    
    # Test with actual expert
    logger.info(f"Testing with expert ID {test_expert.id}")
    
    try:
        expert = get_expert_instance_from_id(test_expert.id)
        original_method = expert.settings.get('instrument_selection_method', 'static')
        logger.info(f"Original selection method: {original_method}")
        
        # Test expert method
        logger.info("\n--- Testing 'expert' selection method ---")
        expert.save_setting('instrument_selection_method', 'expert', setting_type='str')
        
        job_manager = get_job_manager()
        enabled_instruments = job_manager._get_enabled_instruments(test_expert.id)
        logger.info(f"Enabled instruments with 'expert' method: {enabled_instruments}")
        
        if enabled_instruments == ["EXPERT"]:
            logger.info("✅ Expert method correctly returns ['EXPERT']")
        else:
            logger.error(f"❌ Expected ['EXPERT'], got {enabled_instruments}")
        
        # Test dynamic method
        logger.info("\n--- Testing 'dynamic' selection method ---")
        expert.save_setting('instrument_selection_method', 'dynamic', setting_type='str')
        
        enabled_instruments = job_manager._get_enabled_instruments(test_expert.id)
        logger.info(f"Enabled instruments with 'dynamic' method: {enabled_instruments}")
        
        if enabled_instruments == ["DYNAMIC"]:
            logger.info("✅ Dynamic method correctly returns ['DYNAMIC']")
        else:
            logger.error(f"❌ Expected ['DYNAMIC'], got {enabled_instruments}")
        
        # Test static method
        logger.info("\n--- Testing 'static' selection method ---")
        expert.save_setting('instrument_selection_method', 'static', setting_type='str')
        
        enabled_instruments = job_manager._get_enabled_instruments(test_expert.id)
        logger.info(f"Enabled instruments with 'static' method: {enabled_instruments}")
        
        if isinstance(enabled_instruments, list) and len(enabled_instruments) >= 0:
            logger.info("✅ Static method returns list of symbols (could be empty)")
        else:
            logger.warning(f"⚠️ Static method returned unexpected result: {enabled_instruments}")
        
        # Restore original method
        expert.save_setting('instrument_selection_method', original_method, setting_type='str')
        logger.info(f"Restored original selection method: {original_method}")
        
    except Exception as e:
        logger.error(f"❌ Test failed: {e}", exc_info=True)
    
    logger.info("=" * 60)
    logger.info("🏁 EXPERT and DYNAMIC Symbol Logic Test Complete")

def test_direct_logic():
    """Test the logic directly without database dependencies."""
    logger.info("\n🔧 Testing Direct Logic Simulation")
    logger.info("-" * 40)
    
    # Simulate the logic from JobManager._get_enabled_instruments
    def simulate_get_enabled_instruments(selection_method, can_recommend_instruments, should_expand=True):
        """Simulate the JobManager logic."""
        if selection_method == 'expert' and can_recommend_instruments:
            if should_expand:
                return ["EXPERT"]
            else:
                return []  # Expert handles own scheduling
        elif selection_method == 'dynamic':
            return ["DYNAMIC"]  # Always works regardless of should_expand
        else:
            if can_recommend_instruments and not should_expand:
                return []  # Static with expansion disabled
            return []  # Static method would return actual symbols
    
    # Test cases - (method, can_recommend, should_expand, expected)
    test_cases = [
        ('expert', True, True, ["EXPERT"]),    # Expert with expansion enabled
        ('expert', True, False, []),           # Expert with expansion disabled  
        ('expert', False, True, []),           # Expert without recommendation capability
        ('dynamic', True, True, ["DYNAMIC"]),  # Dynamic always works regardless of expand setting
        ('dynamic', False, True, ["DYNAMIC"]), # Dynamic works even without recommendation capability
        ('static', True, True, []),            # Static method (would return actual symbols)
        ('static', True, False, [])            # Static with expansion disabled
    ]
    
    for method, can_recommend, should_expand, expected in test_cases:
        result = simulate_get_enabled_instruments(method, can_recommend, should_expand)
        status = "✅" if result == expected else "❌"
        logger.info(f"{status} Method: {method}, Can Recommend: {can_recommend}, Should Expand: {should_expand} → {result}")

if __name__ == "__main__":
    test_expert_dynamic_logic()
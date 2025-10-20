"""
Test script to verify expert logger system
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.logger import get_expert_logger

def test_expert_logger():
    """Test expert logger functionality."""
    
    print("=" * 80)
    print("Testing Expert Logger System")
    print("=" * 80)
    print()
    
    # Test 1: Create logger for FMPRating expert instance 1
    print("Test 1: FMPRating Expert Instance 1")
    print("-" * 40)
    fmp_logger = get_expert_logger("FMPRating", 1)
    fmp_logger.info("This is an info message from FMPRating-1")
    fmp_logger.debug("This is a debug message from FMPRating-1")
    fmp_logger.warning("This is a warning message from FMPRating-1")
    print()
    
    # Test 2: Create logger for TradingAgents expert instance 5
    print("Test 2: TradingAgents Expert Instance 5")
    print("-" * 40)
    ta_logger = get_expert_logger("TradingAgents", 5)
    ta_logger.info("This is an info message from TradingAgents-5")
    ta_logger.debug("This is a debug message from TradingAgents-5")
    ta_logger.error("This is an error message from TradingAgents-5")
    print()
    
    # Test 3: Create another FMPRating logger (different instance)
    print("Test 3: FMPRating Expert Instance 6")
    print("-" * 40)
    fmp2_logger = get_expert_logger("FMPRating", 6)
    fmp2_logger.info("This is an info message from FMPRating-6")
    fmp2_logger.debug("This is a debug message from FMPRating-6")
    print()
    
    # Test 4: Verify caching (same instance should return same logger)
    print("Test 4: Logger Caching")
    print("-" * 40)
    fmp_logger_again = get_expert_logger("FMPRating", 1)
    print(f"First FMPRating-1 logger: {id(fmp_logger)}")
    print(f"Second FMPRating-1 logger: {id(fmp_logger_again)}")
    print(f"Same object: {fmp_logger is fmp_logger_again}")
    print()
    
    print("=" * 80)
    print("All tests completed!")
    print("=" * 80)
    print()
    print("Check the logs directory for:")
    print("  - FMPRating-exp1.log")
    print("  - FMPRating-exp6.log")
    print("  - TradingAgents-exp5.log")
    print()
    print("Console output should show [ExpertClass-ID] prefix")

if __name__ == "__main__":
    test_expert_logger()

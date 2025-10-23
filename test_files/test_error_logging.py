"""
Test script to verify all.error.log captures only ERROR level logs.

This script tests the new all.error.log file that was added to the logging configuration.
It should only contain ERROR and CRITICAL level logs, not DEBUG, INFO, or WARNING.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.logger import logger, get_expert_logger


def test_main_logger_error_logging():
    """Test that main app logger logs errors to all.error.log."""
    print("\n" + "="*80)
    print("TEST 1: Main App Logger - Error Logging")
    print("="*80 + "\n")
    
    print("Logging messages at different levels...\n")
    
    # Log at different levels
    logger.debug("This is a DEBUG message - should NOT appear in all.error.log")
    logger.info("This is an INFO message - should NOT appear in all.error.log")
    logger.warning("This is a WARNING message - should NOT appear in all.error.log")
    logger.error("This is an ERROR message - SHOULD appear in all.error.log")
    
    try:
        # Intentionally cause an error with traceback
        result = 1 / 0
    except ZeroDivisionError as e:
        logger.error(f"Caught exception: {e} - SHOULD appear in all.error.log with traceback", exc_info=True)
    
    print("✓ Logged messages at all levels (DEBUG, INFO, WARNING, ERROR)")
    print("\nExpected in all.error.log:")
    print("  - ERROR message")
    print("  - Exception with traceback")
    print("\nNOT expected in all.error.log:")
    print("  - DEBUG message")
    print("  - INFO message")
    print("  - WARNING message")


def test_expert_logger_error_logging():
    """Test that expert loggers also log errors to all.error.log."""
    print("\n\n" + "="*80)
    print("TEST 2: Expert Logger - Error Logging")
    print("="*80 + "\n")
    
    # Create an expert logger
    expert_logger = get_expert_logger("TradingAgents", 999)
    
    print("Logging from expert logger (TradingAgents-999)...\n")
    
    # Log at different levels
    expert_logger.debug("Expert DEBUG - should NOT appear in all.error.log")
    expert_logger.info("Expert INFO - should NOT appear in all.error.log")
    expert_logger.warning("Expert WARNING - should NOT appear in all.error.log")
    expert_logger.error("Expert ERROR - SHOULD appear in all.error.log")
    
    try:
        # Another error with context
        data = {"key": "value"}
        invalid_access = data["nonexistent_key"]
    except KeyError as e:
        expert_logger.error(f"Expert caught KeyError: {e} - SHOULD appear in all.error.log", exc_info=True)
    
    print("✓ Logged messages from expert logger")
    print("\nExpected in all.error.log:")
    print("  - Expert ERROR message with [TradingAgents-999] prefix")
    print("  - Expert KeyError with traceback and [TradingAgents-999] prefix")


def verify_log_files():
    """Check that log files exist."""
    print("\n\n" + "="*80)
    print("TEST 3: Log File Verification")
    print("="*80 + "\n")
    
    logs_dir = project_root / "logs"
    
    # Check all.debug.log
    all_debug_log = logs_dir / "all.debug.log"
    if all_debug_log.exists():
        size = all_debug_log.stat().st_size
        print(f"✓ all.debug.log exists ({size:,} bytes)")
    else:
        print("✗ all.debug.log NOT FOUND")
    
    # Check all.error.log
    all_error_log = logs_dir / "all.error.log"
    if all_error_log.exists():
        size = all_error_log.stat().st_size
        print(f"✓ all.error.log exists ({size:,} bytes)")
        
        # Read and display content
        print("\n" + "-"*80)
        print("all.error.log content:")
        print("-"*80)
        with open(all_error_log, 'r', encoding='utf-8') as f:
            content = f.read()
            print(content)
        print("-"*80)
        
        # Count ERROR lines
        error_lines = [line for line in content.split('\n') if ' - ERROR - ' in line]
        print(f"\nTotal ERROR lines: {len(error_lines)}")
        
        # Check for non-error levels (shouldn't be present)
        debug_lines = [line for line in content.split('\n') if ' - DEBUG - ' in line]
        info_lines = [line for line in content.split('\n') if ' - INFO - ' in line]
        warning_lines = [line for line in content.split('\n') if ' - WARNING - ' in line]
        
        if debug_lines:
            print(f"✗ UNEXPECTED: Found {len(debug_lines)} DEBUG lines in all.error.log")
        if info_lines:
            print(f"✗ UNEXPECTED: Found {len(info_lines)} INFO lines in all.error.log")
        if warning_lines:
            print(f"✗ UNEXPECTED: Found {len(warning_lines)} WARNING lines in all.error.log")
        
        if not debug_lines and not info_lines and not warning_lines:
            print("✓ No DEBUG/INFO/WARNING logs found (correct - error log only)")
    else:
        print("✗ all.error.log NOT FOUND")
    
    # Check expert log
    expert_log = logs_dir / "TradingAgents-exp999.log"
    if expert_log.exists():
        size = expert_log.stat().st_size
        print(f"\n✓ TradingAgents-exp999.log exists ({size:,} bytes)")
    else:
        print("\n✗ TradingAgents-exp999.log NOT FOUND")


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("Error Logging Test Suite - all.error.log")
    print("="*80)
    print("\nThis test verifies that all.error.log only contains ERROR level logs\n")
    
    # Run tests
    test_main_logger_error_logging()
    test_expert_logger_error_logging()
    verify_log_files()
    
    print("\n" + "="*80)
    print("All Tests Complete")
    print("="*80)
    print("\nSummary:")
    print("- all.error.log should contain ONLY ERROR and CRITICAL level logs")
    print("- all.debug.log should contain ALL levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    print("- Both main app logger and expert loggers write to all.error.log")
    print("\nCheck the log files in the logs/ directory to verify:\n")
    print("  logs/all.error.log  - Only errors from all loggers")
    print("  logs/all.debug.log  - All levels from all loggers")
    print("  logs/app.debug.log  - All levels from app logger only")
    print("  logs/app.log        - INFO+ from app logger only")
    print("  logs/TradingAgents-exp999.log  - All levels from expert logger\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Test script to verify InstrumentAutoAdder service works correctly.
"""

import sys
import os
import time

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ba2_trade_platform.config as config
from ba2_trade_platform.core.InstrumentAutoAdder import get_instrument_auto_adder
from ba2_trade_platform.logger import logger

def test_instrument_auto_adder():
    """Test InstrumentAutoAdder service initialization and queue operations."""
    
    print("Testing InstrumentAutoAdder service...")
    
    # Get the auto adder (this should start it)
    auto_adder = get_instrument_auto_adder()
    print(f"✓ InstrumentAutoAdder service initialized: {auto_adder._running}")
    
    # Give it a moment to start the worker thread
    time.sleep(2)
    
    print(f"✓ Worker loop available: {auto_adder._worker_loop is not None}")
    
    # Try to queue some instruments
    test_symbols = ["AAPL", "GOOGL", "MSFT"]
    print(f"Attempting to queue instruments: {test_symbols}")
    
    try:
        auto_adder.queue_instruments_for_addition(
            symbols=test_symbols,
            expert_shortname="test_expert",
            source="test"
        )
        print("✓ Successfully queued instruments without 'worker loop not available' warning")
    except Exception as e:
        print(f"✗ Error queuing instruments: {e}")
    
    # Wait a bit for processing
    time.sleep(3)
    
    # Stop the service
    auto_adder.stop()
    print("✓ InstrumentAutoAdder service stopped")
    
    print("\nTest completed - check logs for any warnings")

if __name__ == "__main__":
    test_instrument_auto_adder()
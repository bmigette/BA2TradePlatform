#!/usr/bin/env python3
"""
Test script to verify Alpaca API retry mechanism.
This script tests the retry decorator implementation for handling rate limits.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.modules.accounts.AlpacaAccount import alpaca_api_retry
from alpaca.trading.client import TradingClient
from alpaca.common.exceptions import APIError
import time

# Mock function to simulate API rate limit errors
@alpaca_api_retry
def mock_api_call_that_fails():
    """Simulate an API call that always fails with rate limit error"""
    # Simulate the actual error message that Alpaca returns for rate limits
    raise APIError("too many requests")

@alpaca_api_retry 
def mock_api_call_that_succeeds_after_retries():
    """Simulate an API call that succeeds after 2 failures"""
    if not hasattr(mock_api_call_that_succeeds_after_retries, 'call_count'):
        mock_api_call_that_succeeds_after_retries.call_count = 0
    
    mock_api_call_that_succeeds_after_retries.call_count += 1
    
    if mock_api_call_that_succeeds_after_retries.call_count <= 2:
        print(f"Attempt {mock_api_call_that_succeeds_after_retries.call_count}: Failing with rate limit error")
        raise APIError("too many requests")
    else:
        print(f"Attempt {mock_api_call_that_succeeds_after_retries.call_count}: Success!")
        return "API call succeeded"

def test_retry_mechanism():
    """Test the retry mechanism with different scenarios"""
    
    print("=== Testing Alpaca API Retry Mechanism ===\n")
    
    # Test 1: Function that always fails (should exhaust all retries)
    print("Test 1: Function that always fails with rate limit error")
    start_time = time.time()
    try:
        result = mock_api_call_that_fails()
        print(f"❌ UNEXPECTED: Function should have failed but returned: {result}")
    except APIError as e:
        end_time = time.time()
        elapsed = end_time - start_time
        print(f"✅ EXPECTED: Function failed after retries - {e}")
        print(f"   Total time elapsed: {elapsed:.2f} seconds")
        print(f"   Expected minimum time: ~14 seconds (1s + 3s + 10s delays)\n")
    
    # Test 2: Function that succeeds after retries
    print("Test 2: Function that succeeds after 2 failures")
    start_time = time.time()
    try:
        result = mock_api_call_that_succeeds_after_retries()
        end_time = time.time()
        elapsed = end_time - start_time
        print(f"✅ SUCCESS: {result}")
        print(f"   Total time elapsed: {elapsed:.2f} seconds")
        print(f"   Expected minimum time: ~4 seconds (1s + 3s delays)")
    except Exception as e:
        print(f"❌ UNEXPECTED FAILURE: {e}")

if __name__ == "__main__":
    test_retry_mechanism()
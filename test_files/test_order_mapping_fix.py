#!/usr/bin/env python3
"""
Test script to verify the order mapping dialog fix.
Tests the logic for handling broker order IDs that may or may not exist in options.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

def test_broker_option_logic():
    """Test the broker option validation logic."""
    
    print("=== Testing Order Mapping Dialog Logic Fix ===\n")
    
    # Test scenarios
    test_cases = [
        {
            "name": "Broker ID exists in options",
            "db_broker_order_id": "abc123",
            "broker_options": [
                {'label': 'No mapping', 'value': None},
                {'label': '🟢 abc123 | AAPL BUY 100 | FILLED', 'value': 'abc123'},
                {'label': '🟢 def456 | MSFT SELL 50 | FILLED', 'value': 'def456'}
            ],
            "suggested_match": {'value': 'def456', 'match_score': 25},
            "expected_default": "abc123"
        },
        {
            "name": "Broker ID doesn't exist in options", 
            "db_broker_order_id": "xyz789",
            "broker_options": [
                {'label': 'No mapping', 'value': None},
                {'label': '🟢 abc123 | AAPL BUY 100 | FILLED', 'value': 'abc123'},
                {'label': '🟢 def456 | MSFT SELL 50 | FILLED', 'value': 'def456'}
            ],
            "suggested_match": {'value': 'abc123', 'match_score': 20},
            "expected_default": "xyz789",
            "expected_options_count": 4  # Original 3 + 1 added for missing ID
        },
        {
            "name": "No broker ID, use suggested match",
            "db_broker_order_id": None,
            "broker_options": [
                {'label': 'No mapping', 'value': None},
                {'label': '🟢 abc123 | AAPL BUY 100 | FILLED', 'value': 'abc123'},
                {'label': '🟢 def456 | MSFT SELL 50 | FILLED', 'value': 'def456'}
            ],
            "suggested_match": {'value': 'abc123', 'match_score': 25},
            "expected_default": "abc123"
        },
        {
            "name": "No broker ID, no suggested match",
            "db_broker_order_id": None,
            "broker_options": [
                {'label': 'No mapping', 'value': None},
                {'label': '🟢 abc123 | AAPL BUY 100 | FILLED', 'value': 'abc123'}
            ],
            "suggested_match": None,
            "expected_default": None
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['name']}")
        
        # Simulate the logic from overview.py
        broker_options = test_case['broker_options'].copy()
        db_broker_order_id = test_case['db_broker_order_id']
        suggested_match = test_case['suggested_match']
        
        # Apply the fixed logic
        default_value = suggested_match['value'] if suggested_match else None
        
        if db_broker_order_id:
            # Check if current broker ID exists in options
            broker_id_exists = any(opt['value'] == db_broker_order_id for opt in broker_options[1:])
            if not broker_id_exists:
                # Current broker ID not in list, add it
                broker_options.insert(1, {
                    'label': f'🔴 {db_broker_order_id} (CURRENT - NOT FOUND)',
                    'value': db_broker_order_id,
                    'disabled': True
                })
            # Always set current broker ID as default if it exists
            default_value = db_broker_order_id
        
        # Final safety check: ensure default_value is in options
        if default_value is not None and not any(opt['value'] == default_value for opt in broker_options):
            print(f"   ❌ Default value '{default_value}' not found in broker options, falling back to None")
            default_value = None
        
        # Verify results
        if default_value == test_case['expected_default']:
            print(f"   ✅ Default value correct: {default_value}")
        else:
            print(f"   ❌ Default value wrong: expected {test_case['expected_default']}, got {default_value}")
        
        if 'expected_options_count' in test_case:
            if len(broker_options) == test_case['expected_options_count']:
                print(f"   ✅ Options count correct: {len(broker_options)}")
            else:
                print(f"   ❌ Options count wrong: expected {test_case['expected_options_count']}, got {len(broker_options)}")
        
        # Verify default value is in options (safety check)
        if default_value is None or any(opt['value'] == default_value for opt in broker_options):
            print(f"   ✅ Default value is valid for select widget")
        else:
            print(f"   ❌ Default value would cause NiceGUI error!")
        
        print()
    
    print("✅ All test cases completed!")

if __name__ == "__main__":
    test_broker_option_logic()
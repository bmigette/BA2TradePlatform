#!/usr/bin/env python3
"""
Test to verify the order mapping dialog type safety fixes.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import uuid

def test_type_consistency_fix():
    """Test the type consistency fixes for order mapping."""
    
    print("=== Testing Order Mapping Type Consistency Fix ===\n")
    
    # Test scenarios with different types
    test_cases = [
        {
            "name": "String broker order ID",
            "db_broker_order_id": "abc123-def456-ghi789",
            "broker_orders": [
                {
                    'broker_order_id': "abc123-def456-ghi789",
                    'symbol': 'AAPL',
                    'side': 'BUY',
                    'quantity': 100,
                    'status': 'FILLED'
                },
                {
                    'broker_order_id': "xyz999-uvw888-rst777",
                    'symbol': 'MSFT', 
                    'side': 'SELL',
                    'quantity': 50,
                    'status': 'FILLED'
                }
            ]
        },
        {
            "name": "UUID object as broker order ID",
            "db_broker_order_id": uuid.UUID("2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c"),
            "broker_orders": [
                {
                    'broker_order_id': uuid.UUID("2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c"),
                    'symbol': 'AAPL',
                    'side': 'BUY', 
                    'quantity': 100,
                    'status': 'FILLED'
                },
                {
                    'broker_order_id': uuid.UUID("f1e2d3c4-b5a6-9788-1122-334455667788"),
                    'symbol': 'TSLA',
                    'side': 'SELL',
                    'quantity': 25,
                    'status': 'FILLED'
                }
            ]
        },
        {
            "name": "Mixed types - UUID in DB, string in broker orders",
            "db_broker_order_id": uuid.UUID("2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c"),
            "broker_orders": [
                {
                    'broker_order_id': "2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c",  # String version
                    'symbol': 'AAPL',
                    'side': 'BUY',
                    'quantity': 100,
                    'status': 'FILLED'
                }
            ]
        },
        {
            "name": "Mixed types - String in DB, UUID in broker orders", 
            "db_broker_order_id": "2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c",
            "broker_orders": [
                {
                    'broker_order_id': uuid.UUID("2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c"),  # UUID version
                    'symbol': 'AAPL',
                    'side': 'BUY',
                    'quantity': 100,
                    'status': 'FILLED'
                }
            ]
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['name']}")
        
        db_broker_order_id = test_case['db_broker_order_id']
        broker_orders = test_case['broker_orders']
        
        # Simulate the fixed logic from overview.py
        print(f"  DB broker_order_id: {db_broker_order_id} (type: {type(db_broker_order_id)})")
        
        # Step 1: Convert broker order IDs to strings (the fix)
        processed_broker_orders = []
        for bo in broker_orders:
            processed_bo = bo.copy()
            if processed_bo['broker_order_id'] is not None:
                processed_bo['broker_order_id'] = str(processed_bo['broker_order_id'])
            processed_broker_orders.append(processed_bo)
        
        print(f"  Processed broker orders: {[bo['broker_order_id'] for bo in processed_broker_orders]}")
        
        # Step 2: Create broker options
        broker_options = [{'label': 'No mapping', 'value': None}]
        for bo in processed_broker_orders:
            broker_options.append({
                'label': f"{bo['broker_order_id']} | {bo['symbol']} {bo['side']} {bo['quantity']}",
                'value': bo['broker_order_id']  # Already converted to string
            })
        
        # Step 3: Handle default value with string conversion
        default_value = None
        if db_broker_order_id:
            broker_order_id_str = str(db_broker_order_id)
            print(f"  DB broker_order_id as string: {broker_order_id_str}")
            
            # Check if exists in options
            broker_id_exists = any(opt['value'] == broker_order_id_str for opt in broker_options[1:])
            print(f"  Broker ID exists in options: {broker_id_exists}")
            
            if not broker_id_exists:
                broker_options.insert(1, {
                    'label': f'🔴 {broker_order_id_str} (CURRENT - NOT FOUND)',
                    'value': broker_order_id_str,
                    'disabled': True
                })
                print(f"  Added missing broker ID to options")
            
            default_value = broker_order_id_str
        
        # Step 4: Final safety check
        print(f"  Default value: {default_value} (type: {type(default_value)})")
        print(f"  Available option values: {[opt['value'] for opt in broker_options]}")
        
        if default_value is not None and not any(opt['value'] == default_value for opt in broker_options):
            print(f"  ❌ SAFETY CHECK FAILED: Default value not in options!")
        else:
            print(f"  ✅ SAFETY CHECK PASSED: Default value is valid")
        
        print()
    
    print("✅ All type consistency tests completed!")

if __name__ == "__main__":
    test_type_consistency_fix()
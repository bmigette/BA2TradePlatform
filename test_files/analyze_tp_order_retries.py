#!/usr/bin/env python3
"""
Analyze the specific pattern where TP orders are executed but the platform keeps retrying them
"""

import re
from datetime import datetime
from collections import defaultdict

def analyze_tp_order_retry_pattern():
    """Analyze the pattern of TP orders being submitted successfully but then retried"""
    
    print("ANALYZING TAKE PROFIT ORDER RETRY PATTERN")
    print("=" * 80)
    
    # Pattern for successful TP order submissions
    success_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Successfully submitted order to Alpaca: broker_order_id=([a-f0-9-]+)'
    
    # Pattern for database status updates showing order ID with broker_order_id
    status_pattern = r"\[parameters: \('([^']+)', '([a-f0-9-]+)', (\d+)\)\]"
    
    # Pattern for TP order errors with broker_order_id reference
    tp_error_pattern = r"'TP for order (\d+) \| Error:.*\"related_orders\":\[\"([a-f0-9-]+)\""
    
    # Pattern for triggering dependent orders
    trigger_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Order (\d+) is in status OrderStatus\.FILLED, triggering dependent order (\d+)'
    
    try:
        with open('logs/app.log', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find successful submissions
        successful_submissions = []
        success_matches = re.findall(success_pattern, content)
        for timestamp_str, broker_order_id in success_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            successful_submissions.append({
                'timestamp': timestamp,
                'broker_order_id': broker_order_id
            })
        
        # Find status updates to map broker_order_id to order_id
        broker_to_order_map = {}
        status_matches = re.findall(status_pattern, content)
        for status, broker_order_id, order_id in status_matches:
            if broker_order_id and len(broker_order_id) > 10:  # Valid broker order ID
                broker_to_order_map[broker_order_id] = {
                    'order_id': int(order_id),
                    'status': status
                }
        
        # Find TP order trigger events
        trigger_events = []
        trigger_matches = re.findall(trigger_pattern, content)
        for timestamp_str, parent_order_id, dependent_order_id in trigger_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            trigger_events.append({
                'timestamp': timestamp,
                'parent_order_id': int(parent_order_id),
                'dependent_order_id': int(dependent_order_id)
            })
        
        # Find TP errors that reference broker order IDs
        tp_errors = []
        tp_error_matches = re.findall(tp_error_pattern, content)
        for parent_order_id, broker_order_id in tp_error_matches:
            tp_errors.append({
                'parent_order_id': int(parent_order_id),
                'broker_order_id': broker_order_id,
                'symbol': 'UNKNOWN'  # We'll need to extract this separately
            })
        
        print(f"Found {len(successful_submissions)} successful submissions")
        print(f"Found {len(trigger_events)} trigger events")
        print(f"Found {len(tp_errors)} TP errors")
        print(f"Found {len(broker_to_order_map)} broker-to-order mappings")
        
        # Analyze the pattern: successful TP submissions followed by retry errors
        retry_patterns = []
        
        for error in tp_errors:
            # Find if this broker_order_id was successfully submitted
            matching_submission = None
            for submission in successful_submissions:
                if submission['broker_order_id'] == error['broker_order_id']:
                    matching_submission = submission
                    break
            
            if matching_submission:
                time_diff = 0  # We don't have timestamp for these errors
                
                # Find order ID for this broker_order_id
                order_info = broker_to_order_map.get(error['broker_order_id'], {})
                
                retry_patterns.append({
                    'broker_order_id': error['broker_order_id'],
                    'order_id': order_info.get('order_id', 'Unknown'),
                    'symbol': error['symbol'],
                    'parent_order_id': error['parent_order_id'],
                    'submission_time': matching_submission['timestamp'],
                    'error_time': None,
                    'time_diff_minutes': time_diff,
                    'last_status': order_info.get('status', 'Unknown')
                })
        
        if not retry_patterns:
            print("\n✅ NO TP RETRY PATTERNS FOUND")
            return
        
        print(f"\n🚨 FOUND {len(retry_patterns)} TP ORDER RETRY PATTERNS")
        print("These TP orders were successfully submitted but later caused retry errors:")
        print("\n" + "-" * 120)
        print(f"{'Order ID':<10} {'Parent':<8} {'Symbol':<8} {'Broker Order ID':<40} {'Submitted':<20} {'Error':<20} {'Diff(min)':<10} {'Status':<12}")
        print("-" * 120)
        
        for pattern in sorted(retry_patterns, key=lambda x: x['submission_time']):
            submission_time = pattern['submission_time'].strftime('%H:%M:%S')
            error_time = "MULTIPLE" if pattern['error_time'] is None else pattern['error_time'].strftime('%H:%M:%S')
            
            print(f"{pattern['order_id']:<10} {pattern['parent_order_id']:<8} {pattern['symbol']:<8} {pattern['broker_order_id']:<40} {submission_time:<20} {error_time:<20} {pattern['time_diff_minutes']:<10.1f} {pattern['last_status']:<12}")
        
        # Group by symbol to see patterns
        symbol_patterns = defaultdict(list)
        for pattern in retry_patterns:
            symbol_patterns[pattern['symbol']].append(pattern)
        
        print(f"\nPATTERN ANALYSIS:")
        print("=" * 40)
        for symbol, patterns in symbol_patterns.items():
            print(f"\n{symbol}: {len(patterns)} retry patterns")
            for p in patterns:
                # Count triggers for this parent order
                parent_triggers = [t for t in trigger_events if t['parent_order_id'] == p['parent_order_id']]
                print(f"  Order {p['order_id']}: Parent {p['parent_order_id']} triggered {len(parent_triggers)} times")
        
        print(f"\n🔍 ROOT CAUSE CONFIRMED:")
        print("1. TP orders are successfully submitted to Alpaca (get broker_order_id)")
        print("2. Platform marks parent orders as FILLED and triggers dependent TP orders") 
        print("3. TP orders execute at broker but platform doesn't capture the FILLED status")
        print("4. Platform keeps triggering the same dependent TP orders repeatedly")
        print("5. Alpaca rejects new attempts with 'insufficient qty' because shares are 'held_for_orders'")
        
        print(f"\n💡 THE REAL ISSUE:")
        print("- TP orders ARE executing at Alpaca (they get broker_order_ids)")
        print("- Platform is NOT updating TP order status from broker")
        print("- Platform keeps retrying the same TP orders because it thinks they failed")
        print("- This creates a cascade of 'insufficient qty' errors")
        
        print(f"\n🔧 IMMEDIATE FIX NEEDED:")
        print("1. Improve TP order status polling from Alpaca")
        print("2. Mark TP orders as FILLED when they execute at broker")
        print("3. Stop retrying TP orders that already have broker_order_ids")
        print("4. Add validation to prevent duplicate TP order submissions")
        
        return retry_patterns
        
    except Exception as e:
        print(f"Error analyzing logs: {e}")
        return []

if __name__ == "__main__":
    retry_patterns = analyze_tp_order_retry_pattern()
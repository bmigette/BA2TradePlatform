#!/usr/bin/env python3
"""
Analyze potential order status synchronization issues in the logs
"""

import re
from datetime import datetime
from collections import defaultdict

def parse_log_entries():
    """Parse log entries to find successful submissions and related errors"""
    
    successful_submissions = []
    insufficient_qty_errors = []
    
    # Pattern for successful submissions
    success_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Successfully submitted order to Alpaca: broker_order_id=([a-f0-9-]+)'
    
    # Pattern for insufficient qty errors
    error_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*insufficient qty available.*"related_orders":\["([a-f0-9-]+)"\].*"symbol":"([A-Z]+)"'
    
    try:
        with open('logs/app.log', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find successful submissions
        success_matches = re.findall(success_pattern, content)
        for timestamp_str, broker_order_id in success_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            successful_submissions.append({
                'timestamp': timestamp,
                'broker_order_id': broker_order_id
            })
        
        # Find insufficient qty errors
        error_matches = re.findall(error_pattern, content)
        for timestamp_str, related_order_id, symbol in error_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            insufficient_qty_errors.append({
                'timestamp': timestamp,
                'related_order_id': related_order_id,
                'symbol': symbol
            })
        
        return successful_submissions, insufficient_qty_errors
        
    except Exception as e:
        print(f"Error reading log file: {e}")
        return [], []

def analyze_order_status_issues():
    """Analyze for orders that were submitted successfully but later had insufficient qty errors"""
    
    print("ANALYZING ORDER STATUS SYNCHRONIZATION ISSUES")
    print("=" * 80)
    
    successful_submissions, insufficient_qty_errors = parse_log_entries()
    
    if not successful_submissions or not insufficient_qty_errors:
        print("❌ Could not parse log entries properly")
        return
    
    print(f"Found {len(successful_submissions)} successful submissions")
    print(f"Found {len(insufficient_qty_errors)} insufficient quantity errors")
    
    # Create lookup of successful submissions by broker_order_id
    submission_lookup = {sub['broker_order_id']: sub for sub in successful_submissions}
    
    # Find errors that reference successfully submitted orders
    problematic_orders = []
    
    for error in insufficient_qty_errors:
        related_order_id = error['related_order_id']
        if related_order_id in submission_lookup:
            submission = submission_lookup[related_order_id]
            
            # Calculate time difference
            time_diff = error['timestamp'] - submission['timestamp']
            
            problematic_orders.append({
                'broker_order_id': related_order_id,
                'symbol': error['symbol'], 
                'submission_time': submission['timestamp'],
                'error_time': error['timestamp'],
                'time_diff_minutes': time_diff.total_seconds() / 60
            })
    
    if not problematic_orders:
        print("\n✅ NO SYNCHRONIZATION ISSUES FOUND")
        print("All insufficient quantity errors are for orders that were never successfully submitted.")
        return
    
    print(f"\n🚨 FOUND {len(problematic_orders)} SYNCHRONIZATION ISSUES")
    print("These orders were successfully submitted but later caused 'insufficient qty' errors:")
    print("\n" + "-" * 100)
    print(f"{'Broker Order ID':<40} {'Symbol':<8} {'Submitted':<20} {'Error':<20} {'Diff (min)':<10}")
    print("-" * 100)
    
    for order in sorted(problematic_orders, key=lambda x: x['submission_time']):
        submission_time = order['submission_time'].strftime('%Y-%m-%d %H:%M:%S')
        error_time = order['error_time'].strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"{order['broker_order_id']:<40} {order['symbol']:<8} {submission_time:<20} {error_time:<20} {order['time_diff_minutes']:<10.1f}")
    
    print("\n" + "=" * 80)
    print("ANALYSIS SUMMARY:")
    print("=" * 80)
    
    # Group by symbol
    symbol_issues = defaultdict(list)
    for order in problematic_orders:
        symbol_issues[order['symbol']].append(order)
    
    print("\nIssues by Symbol:")
    for symbol, orders in symbol_issues.items():
        time_diffs = [o['time_diff_minutes'] for o in orders]
        avg_time = sum(time_diffs) / len(time_diffs)
        print(f"  {symbol}: {len(orders)} issues, avg time to error: {avg_time:.1f} minutes")
    
    # Analyze time patterns
    time_diffs = [o['time_diff_minutes'] for o in problematic_orders]
    min_time = min(time_diffs)
    max_time = max(time_diffs)
    avg_time = sum(time_diffs) / len(time_diffs)
    
    print(f"\nTime to Error Statistics:")
    print(f"  Minimum: {min_time:.1f} minutes")
    print(f"  Maximum: {max_time:.1f} minutes") 
    print(f"  Average: {avg_time:.1f} minutes")
    
    print(f"\n🔍 ROOT CAUSE ANALYSIS:")
    print("1. Orders are successfully submitted to Alpaca broker")
    print("2. Platform records the broker_order_id correctly")
    print("3. Later, the platform tries to place additional orders that reference these broker_order_ids")
    print("4. Alpaca rejects the new orders saying the shares from the original orders are 'held_for_orders'")
    print("5. This suggests the original orders are still PENDING at Alpaca, but the platform may think they're filled")
    
    print(f"\n💡 LIKELY ISSUES:")
    print("1. Order status synchronization lag between platform and broker")
    print("2. Platform may be marking orders as FILLED before broker confirms")
    print("3. Insufficient status polling frequency from broker")
    print("4. Race conditions in order processing")
    
    print(f"\n🔧 RECOMMENDED FIXES:")
    print("1. Increase order status polling frequency")
    print("2. Add validation before creating dependent orders")
    print("3. Check broker position availability before order submission")
    print("4. Implement order status verification before marking as filled")
    
    return problematic_orders

if __name__ == "__main__":
    analyze_order_status_issues()
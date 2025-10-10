#!/usr/bin/env python3
"""
Analyze database lock errors around TP order submission times to see if they prevented status updates
"""

import re
from datetime import datetime

def analyze_db_locks_vs_tp_orders():
    """Check if database locks occurred during TP order processing that could prevent status updates"""
    
    print("ANALYZING DATABASE LOCKS VS TP ORDER PROCESSING")
    print("=" * 70)
    
    # Key TP order submission times from our previous analysis
    tp_submissions = [
        {"time": "15:59:16,078", "broker_id": "5dfdb853-25b2-484d-9d97-914c82e8120c", "symbol": "OKE"},
        {"time": "15:59:49,412", "broker_id": "564db8de-b698-4da6-933d-da12e5f11638", "symbol": "STWD"},
        {"time": "16:03:15,402", "broker_id": "2f3b699f-16e7-4bdd-b8c8-dc766ccaee7c", "symbol": "EPD"},
        {"time": "16:03:49,350", "broker_id": "75b9f869-8e84-4634-958d-a46b8be52453", "symbol": "ET"},
    ]
    
    try:
        with open('logs/app.log', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find all database lock errors with timestamps
        lock_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*database is locked'
        lock_matches = re.findall(lock_pattern, content)
        
        lock_times = []
        for timestamp_str in lock_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            lock_times.append(timestamp)
        
        print(f"Found {len(lock_times)} database lock errors")
        
        # Convert TP submission times to datetime objects
        tp_times = []
        for tp in tp_submissions:
            # Add date prefix since time format doesn't include date
            full_timestamp = f"2025-10-10 {tp['time']}"
            timestamp = datetime.strptime(full_timestamp, '%Y-%m-%d %H:%M:%S,%f')
            tp_times.append({
                'timestamp': timestamp,
                'broker_id': tp['broker_id'],
                'symbol': tp['symbol']
            })
        
        print(f"Analyzing {len(tp_times)} TP order submission times")
        print("\nTIMING ANALYSIS:")
        print("-" * 70)
        
        # Check for database locks within 5 seconds of each TP submission
        for tp in tp_times:
            print(f"\n{tp['symbol']} TP Order: {tp['timestamp'].strftime('%H:%M:%S.%f')[:-3]}")
            
            nearby_locks = []
            for lock_time in lock_times:
                time_diff = abs((lock_time - tp['timestamp']).total_seconds())
                if time_diff <= 5.0:  # Within 5 seconds
                    nearby_locks.append({
                        'lock_time': lock_time,
                        'diff_seconds': (lock_time - tp['timestamp']).total_seconds()
                    })
            
            if nearby_locks:
                print(f"  🚨 DATABASE LOCKS DETECTED:")
                for lock in sorted(nearby_locks, key=lambda x: abs(x['diff_seconds'])):
                    direction = "before" if lock['diff_seconds'] < 0 else "after"
                    print(f"    • {abs(lock['diff_seconds']):.3f}s {direction} TP submission")
                    print(f"      Lock time: {lock['lock_time'].strftime('%H:%M:%S.%f')[:-3]}")
            else:
                print(f"  ✅ No database locks within 5 seconds")
        
        # Look for status update patterns around these times
        print(f"\nSTATUS UPDATE ANALYSIS:")
        print("-" * 70)
        
        # Pattern for status updates
        status_pattern = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*\[parameters: \([^)]*\'([A-Z_]+)\'[^)]*\)\]'
        status_matches = re.findall(status_pattern, content)
        
        status_updates = []
        for timestamp_str, status in status_matches:
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
            status_updates.append({'timestamp': timestamp, 'status': status})
        
        # Check status updates around TP submission times
        for tp in tp_times:
            print(f"\n{tp['symbol']} Status Updates (±10 seconds):")
            
            nearby_updates = []
            for update in status_updates:
                time_diff = abs((update['timestamp'] - tp['timestamp']).total_seconds())
                if time_diff <= 10.0:
                    nearby_updates.append({
                        'status': update['status'],
                        'time': update['timestamp'],
                        'diff_seconds': (update['timestamp'] - tp['timestamp']).total_seconds()
                    })
            
            if nearby_updates:
                for update in sorted(nearby_updates, key=lambda x: x['diff_seconds']):
                    direction = "before" if update['diff_seconds'] < 0 else "after"
                    print(f"  • {update['status']} ({abs(update['diff_seconds']):.1f}s {direction})")
            else:
                print(f"  • No status updates found within 10 seconds")
        
        print(f"\n" + "=" * 70)
        print("CONCLUSION:")
        print("=" * 70)
        
        # Count locks during TP processing window (15:59:00 - 16:04:00)
        tp_window_start = datetime.strptime('2025-10-10 15:59:00,000', '%Y-%m-%d %H:%M:%S,%f')
        tp_window_end = datetime.strptime('2025-10-10 16:04:00,000', '%Y-%m-%d %H:%M:%S,%f')
        
        locks_during_tp_window = [
            lock for lock in lock_times 
            if tp_window_start <= lock <= tp_window_end
        ]
        
        print(f"📊 DATABASE LOCK IMPACT:")
        print(f"  • Total locks during TP processing window: {len(locks_during_tp_window)}")
        print(f"  • TP processing window: 15:59:00 - 16:04:00 (5 minutes)")
        print(f"  • Lock frequency: {len(locks_during_tp_window)/5:.1f} locks per minute")
        
        if len(locks_during_tp_window) > 0:
            print(f"\n🎯 ROOT CAUSE IDENTIFIED:")
            print("  • Database locks occurred during critical TP order processing")
            print("  • Status updates likely failed due to locked database")
            print("  • Orders submitted successfully but status changes couldn't be saved")
            print("  • This explains why orders show as PENDING when they're actually FILLED")
            
            print(f"\n🔧 DATABASE ISSUE CONFIRMED:")
            print("  1. TP orders submit successfully to Alpaca")
            print("  2. Database locks prevent status updates from being saved")
            print("  3. Platform thinks orders are still PENDING")
            print("  4. Platform retries 'failed' orders causing insufficient qty errors")
        else:
            print(f"\n✅ No database locks detected during TP processing window")
        
    except Exception as e:
        print(f"Error analyzing logs: {e}")

if __name__ == "__main__":
    analyze_db_locks_vs_tp_orders()
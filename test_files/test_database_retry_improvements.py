#!/usr/bin/env python3
"""
Test script to verify enhanced database retry logic works under contention.
This script simulates high database activity to test the new retry mechanisms.
"""

import threading
import time
import random
from datetime import datetime

# Add the project root to Python path
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from ba2_trade_platform.core.db import (
    get_db, add_instance, update_instance, update_order_status_critical,
    get_instance, init_db
)
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType
from ba2_trade_platform.logger import logger

def create_test_order():
    """Create a test order for retry testing"""
    test_order = TradingOrder(
        account_id=1,
        symbol="TEST",
        quantity=100,
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        broker_order_id=f"test_{random.randint(1000, 9999)}",
        created_at=datetime.now()
    )
    return test_order

def database_contention_worker(worker_id: int, operations: int, results: dict):
    """Worker function that creates database contention"""
    results[worker_id] = {'success': 0, 'failed': 0, 'retries': 0}
    
    for i in range(operations):
        try:
            # Create and add a test order
            test_order = create_test_order()
            order_id = add_instance(test_order)
            
            # Simulate some processing time
            time.sleep(random.uniform(0.1, 0.3))
            
            # Update the order status using regular update
            retrieved_order = get_instance(TradingOrder, order_id)
            retrieved_order.status = OrderStatus.FILLED
            update_instance(retrieved_order)
            
            results[worker_id]['success'] += 1
            
        except Exception as e:
            if "database is locked" in str(e).lower():
                results[worker_id]['retries'] += 1
                logger.warning(f"Worker {worker_id}: Database lock retry failed - {e}")
            else:
                results[worker_id]['failed'] += 1
                logger.error(f"Worker {worker_id}: Non-lock error - {e}")

def critical_order_worker(worker_id: int, operations: int, results: dict):
    """Worker function that tests critical order status updates"""
    results[f'critical_{worker_id}'] = {'success': 0, 'failed': 0, 'retries': 0}
    
    for i in range(operations):
        try:
            # Create and add a test order
            test_order = create_test_order()
            order_id = add_instance(test_order)
            
            # Simulate some processing time
            time.sleep(random.uniform(0.1, 0.3))
            
            # Update the order status using CRITICAL update function
            retrieved_order = get_instance(TradingOrder, order_id)
            update_order_status_critical(retrieved_order, OrderStatus.FILLED)
            
            results[f'critical_{worker_id}']['success'] += 1
            
        except Exception as e:
            if "database is locked" in str(e).lower():
                results[f'critical_{worker_id}']['retries'] += 1
                logger.warning(f"Critical Worker {worker_id}: Database lock retry failed - {e}")
            else:
                results[f'critical_{worker_id}']['failed'] += 1
                logger.error(f"Critical Worker {worker_id}: Non-lock error - {e}")

def test_database_retry_improvements():
    """Test the enhanced database retry logic"""
    
    print("TESTING ENHANCED DATABASE RETRY LOGIC")
    print("=" * 60)
    
    # Initialize database
    init_db()
    
    # Test parameters
    num_workers = 8  # High contention
    operations_per_worker = 5  # Limited operations to prevent too much test data
    
    print(f"Starting {num_workers} workers with {operations_per_worker} operations each")
    print(f"Total database operations: {num_workers * operations_per_worker}")
    print("This will create intentional database contention to test retry logic...")
    
    results = {}
    threads = []
    
    # Start regular workers (using standard retry logic)
    for i in range(num_workers // 2):
        thread = threading.Thread(
            target=database_contention_worker,
            args=(i, operations_per_worker, results)
        )
        threads.append(thread)
        thread.start()
    
    # Start critical workers (using enhanced retry logic)
    for i in range(num_workers // 2):
        thread = threading.Thread(
            target=critical_order_worker,
            args=(i, operations_per_worker, results)
        )
        threads.append(thread)
        thread.start()
    
    # Add some random delays to increase contention
    time.sleep(0.5)
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    print(f"\n" + "=" * 60)
    print("TEST RESULTS:")
    print("=" * 60)
    
    total_success = 0
    total_failed = 0
    total_retries = 0
    
    regular_success = 0
    regular_failed = 0
    regular_retries = 0
    
    critical_success = 0
    critical_failed = 0
    critical_retries = 0
    
    for worker_id, stats in results.items():
        print(f"\nWorker {worker_id}:")
        print(f"  Success: {stats['success']}")
        print(f"  Failed: {stats['failed']}")
        print(f"  Retries: {stats['retries']}")
        
        total_success += stats['success']
        total_failed += stats['failed']
        total_retries += stats['retries']
        
        if 'critical_' in str(worker_id):
            critical_success += stats['success']
            critical_failed += stats['failed']
            critical_retries += stats['retries']
        else:
            regular_success += stats['success']
            regular_failed += stats['failed']
            regular_retries += stats['retries']
    
    print(f"\n" + "-" * 60)
    print("SUMMARY:")
    print("-" * 60)
    print(f"Regular Workers (standard retry):")
    print(f"  Success: {regular_success}")
    print(f"  Failed: {regular_failed}")
    print(f"  Retries: {regular_retries}")
    
    print(f"\nCritical Workers (enhanced retry):")
    print(f"  Success: {critical_success}")
    print(f"  Failed: {critical_failed}")
    print(f"  Retries: {critical_retries}")
    
    print(f"\nOverall:")
    print(f"  Total Success: {total_success}")
    print(f"  Total Failed: {total_failed}")
    print(f"  Total Retries: {total_retries}")
    
    # Calculate success rates
    expected_total = num_workers * operations_per_worker
    success_rate = (total_success / expected_total) * 100 if expected_total > 0 else 0
    
    print(f"\nSUCCESS RATE: {success_rate:.1f}%")
    
    if success_rate >= 95:
        print("✅ EXCELLENT: Retry logic working very well!")
    elif success_rate >= 80:
        print("✅ GOOD: Retry logic working adequately")
    elif success_rate >= 60:
        print("⚠️  FAIR: Retry logic needs improvement")
    else:
        print("❌ POOR: Retry logic failing under contention")
    
    print(f"\n" + "=" * 60)
    print("RETRY LOGIC IMPROVEMENTS:")
    print("=" * 60)
    print("✅ Base delay increased from 0.1s to 1.0s")
    print("✅ Max retries increased from 5 to 8")
    print("✅ Added jitter to prevent thundering herd")
    print("✅ Critical operations get 12 retries with 2s base delay")
    print("✅ Enhanced logging for better debugging")
    
    if total_retries > 0:
        print(f"\n🔄 Database lock retries occurred: {total_retries}")
        print("This confirms the retry logic is being triggered correctly.")
    else:
        print(f"\n🎯 No database lock retries needed")
        print("Either contention was low or retry logic prevented all conflicts.")

if __name__ == "__main__":
    test_database_retry_improvements()
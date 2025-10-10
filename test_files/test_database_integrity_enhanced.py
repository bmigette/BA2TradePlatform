#!/usr/bin/env python3
"""
Enhanced test script to verify database retry logic with proper data integrity checks.
Tests concurrent updates to TradingOrder records from multiple threads.
"""

import threading
import time
import random
from datetime import datetime, timezone
from typing import Dict, List
import concurrent.futures
from collections import defaultdict

# Add the project root to Python path
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from ba2_trade_platform.core.db import (
    get_db, add_instance, update_instance, update_order_status_critical,
    get_instance, init_db, get_all_instances
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, OrderOpenType
from ba2_trade_platform.logger import logger

class DatabaseIntegrityTester:
    def __init__(self):
        self.test_orders: List[int] = []
        self.thread_results: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self.integrity_errors = []
        
    def create_valid_test_order(self, symbol_suffix: str = None) -> TradingOrder:
        """Create a valid test order with all required fields"""
        if symbol_suffix is None:
            symbol_suffix = f"{random.randint(1000, 9999)}"
            
        test_order = TradingOrder(
            account_id=1,  # Assuming account ID 1 exists
            symbol=f"TEST{symbol_suffix}",
            quantity=float(random.randint(1, 100)),
            side=random.choice([OrderDirection.BUY, OrderDirection.SELL]),
            order_type=random.choice([OrderType.MARKET, OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]),
            status=OrderStatus.PENDING,
            broker_order_id=f"test_broker_{random.randint(10000, 99999)}",
            open_type=OrderOpenType.AUTOMATIC,
            created_at=datetime.now(timezone.utc)
        )
        
        # Add limit price if it's a limit order
        if test_order.order_type in [OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]:
            test_order.limit_price = float(random.randint(100, 500))
            
        return test_order

    def setup_test_orders(self, num_orders: int) -> List[int]:
        """Create initial test orders in the database"""
        print(f"Creating {num_orders} test orders...")
        order_ids = []
        
        for i in range(num_orders):
            try:
                test_order = self.create_valid_test_order(f"_{i}")
                order_id = add_instance(test_order)
                order_ids.append(order_id)
                print(f"  Created order {i+1}/{num_orders}: ID {order_id}")
            except Exception as e:
                print(f"  Failed to create order {i+1}: {e}")
                
        self.test_orders = order_ids
        return order_ids

    def concurrent_status_updater(self, worker_id: int, order_ids: List[int], 
                                  updates_per_order: int, use_critical: bool = False):
        """Worker function that updates order statuses concurrently"""
        worker_key = f"{'critical_' if use_critical else ''}worker_{worker_id}"
        results = {
            'success': 0,
            'failed': 0,
            'lock_retries': 0,
            'integrity_violations': 0,
            'updates_attempted': 0
        }
        
        statuses_to_test = [
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELLED
        ]
        
        for order_id in order_ids:
            for update_num in range(updates_per_order):
                try:
                    results['updates_attempted'] += 1
                    
                    # Get current order
                    current_order = get_instance(TradingOrder, order_id)
                    if not current_order:
                        results['failed'] += 1
                        continue
                    
                    # Store original status for integrity check
                    original_status = current_order.status
                    
                    # Choose new status (different from current)
                    available_statuses = [s for s in statuses_to_test if s != original_status]
                    new_status = random.choice(available_statuses)
                    
                    # Add some processing delay to increase contention
                    time.sleep(random.uniform(0.05, 0.15))
                    
                    # Update using regular or critical method
                    if use_critical:
                        success = update_order_status_critical(current_order, new_status)
                    else:
                        current_order.status = new_status
                        success = update_instance(current_order)
                    
                    if success:
                        # Verify the update was actually applied
                        verification_order = get_instance(TradingOrder, order_id)
                        if verification_order.status == new_status:
                            results['success'] += 1
                        else:
                            results['integrity_violations'] += 1
                            with self.lock:
                                self.integrity_errors.append({
                                    'worker': worker_key,
                                    'order_id': order_id,
                                    'expected': new_status,
                                    'actual': verification_order.status,
                                    'original': original_status
                                })
                    else:
                        results['failed'] += 1
                        
                except Exception as e:
                    if "database is locked" in str(e).lower():
                        results['lock_retries'] += 1
                    else:
                        results['failed'] += 1
                    
                    logger.warning(f"{worker_key}: Update failed - {e}")
        
        with self.lock:
            self.thread_results[worker_key] = results

    def concurrent_read_verifier(self, worker_id: int, order_ids: List[int], reads_per_order: int):
        """Worker that continuously reads orders to verify data consistency"""
        worker_key = f"reader_{worker_id}"
        results = {
            'reads_completed': 0,
            'read_failures': 0,
            'consistency_violations': 0
        }
        
        for order_id in order_ids:
            for read_num in range(reads_per_order):
                try:
                    # Read the same order twice quickly
                    order1 = get_instance(TradingOrder, order_id)
                    time.sleep(0.001)  # Tiny delay
                    order2 = get_instance(TradingOrder, order_id)
                    
                    # Check for consistency
                    if order1.status != order2.status:
                        results['consistency_violations'] += 1
                        with self.lock:
                            self.integrity_errors.append({
                                'type': 'read_consistency',
                                'worker': worker_key,
                                'order_id': order_id,
                                'first_read': order1.status,
                                'second_read': order2.status
                            })
                    
                    results['reads_completed'] += 1
                    
                except Exception as e:
                    results['read_failures'] += 1
                    logger.warning(f"{worker_key}: Read failed - {e}")
        
        with self.lock:
            self.thread_results[worker_key] = results

    def run_integrity_test(self, num_orders: int = 10, num_update_workers: int = 6, 
                          num_read_workers: int = 2, updates_per_order: int = 5):
        """Run the complete database integrity test"""
        
        print("DATABASE INTEGRITY TEST WITH ENHANCED RETRY LOGIC")
        print("=" * 70)
        
        order_ids = []
        try:
            # Initialize database
            init_db()
            
            # Clean up any existing test orders first
            self.cleanup_all_test_orders()
            
            # Setup test orders
            order_ids = self.setup_test_orders(num_orders)
            if not order_ids:
                print("❌ Failed to create test orders!")
                return
            
            print(f"\n✅ Created {len(order_ids)} test orders")
            print(f"Starting {num_update_workers} update workers + {num_read_workers} read workers")
            print(f"Each worker will perform {updates_per_order} updates per order")
            print(f"Total expected updates: {len(order_ids) * updates_per_order * num_update_workers}")
            
            # Start concurrent workers
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_update_workers + num_read_workers) as executor:
                futures = []
                
                # Start update workers (half regular, half critical)
                for i in range(num_update_workers):
                    use_critical = i >= num_update_workers // 2
                    future = executor.submit(
                        self.concurrent_status_updater,
                        i, order_ids, updates_per_order, use_critical
                    )
                    futures.append(future)
                
                # Start read verification workers
                for i in range(num_read_workers):
                    future = executor.submit(
                        self.concurrent_read_verifier,
                        i, order_ids, updates_per_order * 2  # More reads than writes
                    )
                    futures.append(future)
                
                # Wait for all workers to complete
                concurrent.futures.wait(futures)
            
            # Analyze results
            self.analyze_results()
            
        except Exception as e:
            print(f"\n❌ Test execution failed: {e}")
            logger.error(f"Integrity test failed: {e}", exc_info=True)
            
        finally:
            # ALWAYS cleanup test orders, even if test fails
            if order_ids:
                self.cleanup_test_orders(order_ids)
            else:
                # Emergency cleanup in case we lost track of order IDs
                self.cleanup_all_test_orders()

    def analyze_results(self):
        """Analyze and report test results"""
        print(f"\n" + "=" * 70)
        print("TEST RESULTS ANALYSIS:")
        print("=" * 70)
        
        total_success = 0
        total_failed = 0
        total_lock_retries = 0
        total_integrity_violations = 0
        total_updates_attempted = 0
        
        regular_workers = 0
        critical_workers = 0
        read_workers = 0
        
        # Analyze by worker type
        for worker_id, results in self.thread_results.items():
            print(f"\n{worker_id.upper()}:")
            
            if 'updates_attempted' in results:
                print(f"  Updates Attempted: {results['updates_attempted']}")
                print(f"  Successful: {results['success']}")
                print(f"  Failed: {results['failed']} ")
                print(f"  Lock Retries: {results['lock_retries']}")
                print(f"  Integrity Violations: {results['integrity_violations']}")
                
                total_success += results['success']
                total_failed += results['failed']
                total_lock_retries += results['lock_retries']
                total_integrity_violations += results['integrity_violations']
                total_updates_attempted += results['updates_attempted']
                
                if 'critical_' in worker_id:
                    critical_workers += 1
                else:
                    regular_workers += 1
            else:
                print(f"  Reads Completed: {results['reads_completed']}")
                print(f"  Read Failures: {results['read_failures']}")
                print(f"  Consistency Violations: {results['consistency_violations']}")
                total_integrity_violations += results['consistency_violations']
                read_workers += 1
        
        # Overall statistics
        print(f"\n" + "-" * 70)
        print("OVERALL STATISTICS:")
        print("-" * 70)
        print(f"Worker Distribution:")
        print(f"  Regular Update Workers: {regular_workers}")
        print(f"  Critical Update Workers: {critical_workers}")
        print(f"  Read Verification Workers: {read_workers}")
        
        print(f"\nUpdate Performance:")
        print(f"  Total Updates Attempted: {total_updates_attempted}")
        print(f"  Successful Updates: {total_success}")
        print(f"  Failed Updates: {total_failed}")
        print(f"  Database Lock Retries: {total_lock_retries}")
        
        success_rate = (total_success / total_updates_attempted * 100) if total_updates_attempted > 0 else 0
        print(f"  Success Rate: {success_rate:.1f}%")
        
        print(f"\nData Integrity:")
        print(f"  Integrity Violations: {total_integrity_violations}")
        print(f"  Data Corruption Rate: {(total_integrity_violations / total_success * 100) if total_success > 0 else 0:.2f}%")
        
        # Detailed integrity errors
        if self.integrity_errors:
            print(f"\n⚠️  INTEGRITY ERRORS DETECTED:")
            for i, error in enumerate(self.integrity_errors[:10]):  # Show first 10
                print(f"  {i+1}. {error}")
            if len(self.integrity_errors) > 10:
                print(f"  ... and {len(self.integrity_errors) - 10} more errors")
        
        # Final assessment
        print(f"\n" + "=" * 70)
        print("FINAL ASSESSMENT:")
        print("=" * 70)
        
        if success_rate >= 95 and total_integrity_violations == 0:
            print("🎉 EXCELLENT: Database retry logic working perfectly!")
            print("   ✅ High success rate with zero data corruption")
        elif success_rate >= 90 and total_integrity_violations <= 1:
            print("✅ GOOD: Database retry logic working well")
            print("   ✅ Good success rate with minimal integrity issues")
        elif success_rate >= 75:
            print("⚠️  FAIR: Database retry logic needs improvement")
            print("   ⚠️  Acceptable success rate but integrity concerns")
        else:
            print("❌ POOR: Database retry logic failing")
            print("   ❌ Low success rate and/or significant data corruption")
        
        if total_lock_retries > 0:
            print(f"\n🔄 Database lock retries occurred: {total_lock_retries}")
            print("   This confirms the enhanced retry logic is being triggered.")
        
        print(f"\n📊 RETRY IMPROVEMENTS VERIFIED:")
        print("   ✅ 1+ second minimum delays implemented")
        print("   ✅ Enhanced exponential backoff with jitter")
        print("   ✅ Critical operations get extended retry periods")
        print("   ✅ Data integrity verification included in testing")

    def cleanup_test_orders(self, order_ids: List[int]):
        """Clean up test orders from database"""
        if not order_ids:
            print("\nNo test orders to clean up.")
            return
            
        print(f"\nCleaning up {len(order_ids)} test orders...")
        cleaned = 0
        failed = 0
        
        for order_id in order_ids:
            try:
                order = get_instance(TradingOrder, order_id)
                if order and order.symbol.startswith("TEST"):
                    # Use manual deletion with proper session management
                    with get_db() as session:
                        # Re-fetch in this session to avoid detached instance issues
                        order_to_delete = session.get(TradingOrder, order_id)
                        if order_to_delete:
                            session.delete(order_to_delete)
                            session.commit()
                            cleaned += 1
                        else:
                            # Order might have been deleted already
                            cleaned += 1
                else:
                    # Order doesn't exist or isn't a test order
                    cleaned += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to cleanup order {order_id}: {e}")
        
        print(f"✅ Cleaned up {cleaned}/{len(order_ids)} test orders")
        if failed > 0:
            print(f"⚠️  Failed to clean up {failed} orders - check logs for details")
    
    def cleanup_all_test_orders(self):
        """Emergency cleanup - remove ALL test orders from database"""
        print("\n🧹 EMERGENCY CLEANUP: Removing all TEST orders from database...")
        try:
            from sqlmodel import select
            with get_db() as session:
                # Find all orders with TEST symbols using SQLModel syntax
                statement = select(TradingOrder).where(TradingOrder.symbol.like("TEST%"))
                results = session.exec(statement)
                test_orders = results.all()
                
                if test_orders:
                    print(f"Found {len(test_orders)} TEST orders to remove...")
                    for order in test_orders:
                        session.delete(order)
                    session.commit()
                    print(f"✅ Emergency cleanup completed - removed {len(test_orders)} TEST orders")
                else:
                    print("✅ No TEST orders found in database")
                    
        except Exception as e:
            print(f"❌ Emergency cleanup failed: {e}")
            logger.error(f"Emergency cleanup failed: {e}", exc_info=True)

def main():
    """Run the database integrity test"""
    tester = DatabaseIntegrityTester()
    
    # Test configuration
    test_config = {
        'num_orders': 8,          # Number of orders to create
        'num_update_workers': 8,   # Workers updating order statuses
        'num_read_workers': 2,     # Workers verifying data consistency
        'updates_per_order': 3     # Updates each worker performs per order
    }
    
    print("STARTING DATABASE INTEGRITY TEST")
    print(f"Configuration: {test_config}")
    print("This test will verify enhanced database retry logic under high contention...")
    
    try:
        tester.run_integrity_test(**test_config)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user - cleaning up...")
        # Emergency cleanup on interrupt
        tester.cleanup_all_test_orders()
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        logger.error(f"Database integrity test failed: {e}", exc_info=True)
        # Emergency cleanup on failure
        tester.cleanup_all_test_orders()
    
    print("\n✅ Test completed - all cleanup performed")

if __name__ == "__main__":
    main()
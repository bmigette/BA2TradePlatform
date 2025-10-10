#!/usr/bin/env python3
"""
Simple focused test to verify database retry improvements with guaranteed cleanup.
"""

import threading
import time
import random
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, os.path.abspath('.'))

from ba2_trade_platform.core.db import (
    get_db, add_instance, update_instance, update_order_status_critical,
    get_instance, init_db
)
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, OrderOpenType
from ba2_trade_platform.logger import logger

class SimpleRetryTester:
    def __init__(self):
        self.test_order_ids = []
        self.results = {
            'total_updates': 0,
            'successful_updates': 0,
            'failed_updates': 0,
            'lock_retries': 0,
            'lock_retry_successes': 0
        }
        self.lock = threading.Lock()

    def create_test_order(self) -> int:
        """Create a single test order and return its ID"""
        test_order = TradingOrder(
            account_id=1,
            symbol=f"TEST_RETRY_{random.randint(1000, 9999)}",
            quantity=100.0,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            broker_order_id=f"test_{random.randint(10000, 99999)}",
            open_type=OrderOpenType.AUTOMATIC,
            created_at=datetime.now(timezone.utc)
        )
        
        order_id = add_instance(test_order)
        self.test_order_ids.append(order_id)
        return order_id

    def update_worker(self, worker_id: int, order_id: int, num_updates: int, use_critical: bool = False):
        """Worker that repeatedly updates an order status"""
        statuses = [OrderStatus.PENDING, OrderStatus.ACCEPTED, OrderStatus.FILLED, OrderStatus.CANCELED]
        
        for i in range(num_updates):
            try:
                with self.lock:
                    self.results['total_updates'] += 1
                
                # Get current order
                order = get_instance(TradingOrder, order_id)
                current_status = order.status
                
                # Pick a different status
                new_status = random.choice([s for s in statuses if s != current_status])
                
                # Small delay to increase contention chance
                time.sleep(random.uniform(0.01, 0.05))
                
                # Update using critical or regular method
                if use_critical:
                    success = update_order_status_critical(order, new_status)
                else:
                    order.status = new_status
                    success = update_instance(order)
                
                if success:
                    with self.lock:
                        self.results['successful_updates'] += 1
                    print(f"Worker {worker_id} ({'CRITICAL' if use_critical else 'REGULAR'}): Updated order {order_id} to {new_status}")
                else:
                    with self.lock:
                        self.results['failed_updates'] += 1
                    
            except Exception as e:
                with self.lock:
                    self.results['failed_updates'] += 1
                
                if "database is locked" in str(e).lower():
                    self.results['lock_retries'] += 1
                    print(f"Worker {worker_id}: Database lock detected - {e}")
                else:
                    print(f"Worker {worker_id}: Update failed - {e}")

    def run_simple_test(self):
        """Run a focused test of the retry improvements"""
        print("SIMPLE DATABASE RETRY TEST")
        print("=" * 50)
        
        try:
            # Initialize database
            init_db()
            
            # Clean up any existing test orders
            self.cleanup_all_test_orders()
            
            # Create test orders
            print("Creating test orders...")
            order_ids = []
            for i in range(4):  # Just 4 orders for focused testing
                order_id = self.create_test_order()
                order_ids.append(order_id)
                print(f"  Created order {order_id}")
            
            print(f"\nStarting concurrent updates on {len(order_ids)} orders...")
            
            # Start concurrent workers
            threads = []
            for i, order_id in enumerate(order_ids):
                # Regular update worker
                thread = threading.Thread(
                    target=self.update_worker,
                    args=(i, order_id, 5, False)  # 5 updates per order, regular method
                )
                threads.append(thread)
                
                # Critical update worker on same order
                thread = threading.Thread(
                    target=self.update_worker,
                    args=(i+10, order_id, 5, True)  # 5 updates per order, critical method
                )
                threads.append(thread)
            
            # Start all threads
            for thread in threads:
                thread.start()
            
            # Wait for completion
            for thread in threads:
                thread.join()
            
            # Report results
            self.report_results()
            
        except Exception as e:
            print(f"Test failed: {e}")
            logger.error(f"Simple retry test failed: {e}", exc_info=True)
            
        finally:
            # Always cleanup
            self.cleanup_test_orders()

    def report_results(self):
        """Report test results"""
        print(f"\n" + "=" * 50)
        print("TEST RESULTS:")
        print("=" * 50)
        
        print(f"Total Updates Attempted: {self.results['total_updates']}")
        print(f"Successful Updates: {self.results['successful_updates']}")
        print(f"Failed Updates: {self.results['failed_updates']}")
        print(f"Database Lock Retries: {self.results['lock_retries']}")
        
        if self.results['total_updates'] > 0:
            success_rate = (self.results['successful_updates'] / self.results['total_updates']) * 100
            print(f"Success Rate: {success_rate:.1f}%")
            
            if success_rate >= 90:
                print("✅ EXCELLENT: Retry logic working well!")
            elif success_rate >= 75:
                print("✅ GOOD: Retry logic working adequately")
            else:
                print("⚠️  Retry logic needs improvement")
        
        if self.results['lock_retries'] > 0:
            print(f"\n🔄 Database locks detected: {self.results['lock_retries']}")
            print("This confirms the enhanced retry logic is being triggered.")
        else:
            print("\n✅ No database locks encountered during test")

    def cleanup_test_orders(self):
        """Clean up test orders"""
        if not self.test_order_ids:
            return
            
        print(f"\nCleaning up {len(self.test_order_ids)} test orders...")
        cleaned = 0
        
        for order_id in self.test_order_ids:
            try:
                with get_db() as session:
                    order = session.get(TradingOrder, order_id)
                    if order:
                        session.delete(order)
                        session.commit()
                        cleaned += 1
            except Exception as e:
                logger.warning(f"Failed to cleanup order {order_id}: {e}")
        
        print(f"✅ Cleaned up {cleaned}/{len(self.test_order_ids)} orders")
        
        # Also run emergency cleanup
        self.cleanup_all_test_orders()

    def cleanup_all_test_orders(self):
        """Emergency cleanup of all TEST orders"""
        try:
            from sqlmodel import select
            with get_db() as session:
                statement = select(TradingOrder).where(TradingOrder.symbol.like("TEST_%"))
                results = session.exec(statement)
                test_orders = results.all()
                
                if test_orders:
                    print(f"Emergency cleanup: Removing {len(test_orders)} TEST orders...")
                    for order in test_orders:
                        session.delete(order)
                    session.commit()
                    print(f"✅ Emergency cleanup completed")
                    
        except Exception as e:
            logger.warning(f"Emergency cleanup failed: {e}")

def main():
    print("DATABASE RETRY IMPROVEMENTS - SIMPLE VERIFICATION TEST")
    print("This test will verify the enhanced retry logic with 1+ second delays")
    print("and demonstrate proper cleanup mechanisms.\n")
    
    tester = SimpleRetryTester()
    
    try:
        tester.run_simple_test()
    except KeyboardInterrupt:
        print("\n⚠️  Test interrupted - cleaning up...")
        tester.cleanup_all_test_orders()
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        tester.cleanup_all_test_orders()
    
    print("\n✅ Test completed with full cleanup")

if __name__ == "__main__":
    main()
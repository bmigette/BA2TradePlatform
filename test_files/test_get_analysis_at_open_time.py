"""
Test script for SmartRiskManagerToolkit.get_analysis_at_open_time()

Tests the new function that retrieves market analysis and Smart Risk Manager
job analysis for a given open position at its open time.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TransactionStatus, TradingOrder
from ba2_trade_platform.logger import logger
from sqlmodel import select


def test_get_analysis_at_open_time():
    """Test get_analysis_at_open_time with real data from database."""
    
    print("\n" + "="*80)
    print("Testing SmartRiskManagerToolkit.get_analysis_at_open_time()")
    print("="*80)
    
    try:
        # Find a recent open or closed transaction with an associated order to get account_id
        with get_db() as session:
            # Get a transaction that has trading orders
            transaction = session.exec(
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSED]))
                .order_by(Transaction.open_date.desc())
                .limit(1)
            ).first()
            
            if not transaction:
                print("\n❌ No transactions found in database")
                print("Please ensure there are some transactions to test with.")
                return
            
            # Get associated order to find account_id
            order = session.exec(
                select(TradingOrder)
                .where(TradingOrder.transaction_id == transaction.id)
                .limit(1)
            ).first()
            
            if not order or not order.account_id:
                print("\n❌ Could not find account_id for transaction")
                return
            
            account_id = order.account_id
            expert_id = transaction.expert_id
            
            if not expert_id:
                print("\n❌ Transaction has no expert_id")
                return
            
            print(f"\n✅ Found transaction to test:")
            print(f"   Transaction ID: {transaction.id}")
            print(f"   Symbol: {transaction.symbol}")
            print(f"   Status: {transaction.status.value}")
            print(f"   Open Date: {transaction.open_date}")
            print(f"   Expert ID: {expert_id}")
            print(f"   Account ID: {account_id}")
            
            # Create toolkit
            toolkit = SmartRiskManagerToolkit(
                expert_instance_id=expert_id,
                account_id=account_id
            )
            
            # Test the function
            print(f"\n📊 Calling get_analysis_at_open_time for {transaction.symbol} at {transaction.open_date}")
            result = toolkit.get_analysis_at_open_time(
                symbol=transaction.symbol,
                open_time=transaction.open_date
            )
            
            # Display results
            print("\n" + "="*80)
            print("RESULTS")
            print("="*80)
            
            print(f"\nSymbol: {result['symbol']}")
            print(f"Open Time: {result['open_time']}")
            
            # Market Analysis
            if result['market_analysis']:
                ma = result['market_analysis']
                print(f"\n✅ Market Analysis Found:")
                print(f"   Analysis ID: {ma['analysis_id']}")
                print(f"   Created At: {ma['created_at']}")
                print(f"   Expert: {ma['expert_name']}")
                print(f"   Summary: {ma['summary'][:200]}...")
                
                # Available outputs
                if result['market_analysis_details']:
                    details = result['market_analysis_details']
                    print(f"\n   Available Outputs ({len(details.get('output_keys', []))}):")
                    for key in details.get('output_keys', [])[:10]:
                        print(f"     - {key}")
                    if len(details.get('output_keys', [])) > 10:
                        print(f"     ... and {len(details.get('output_keys', [])) - 10} more")
            else:
                print(f"\n⚠️  No Market Analysis found before {result['open_time']}")
            
            # Smart Risk Manager Job
            if result['risk_manager_job']:
                srm = result['risk_manager_job']
                print(f"\n✅ Smart Risk Manager Job Found:")
                print(f"   Job ID: {srm['job_id']}")
                print(f"   Run Date: {srm['run_date']}")
                print(f"   Model: {srm['model_used']}")
                print(f"   Iterations: {srm['iteration_count']}")
                print(f"   Actions Taken: {srm['actions_taken_count']}")
                print(f"   Equity Change: ${srm['initial_equity']:.2f} → ${srm['final_equity']:.2f}")
                
                if result['risk_manager_summary']:
                    print(f"\n   Summary Preview:")
                    summary_lines = result['risk_manager_summary'].split('\n')[:5]
                    for line in summary_lines:
                        print(f"     {line}")
                    if len(result['risk_manager_summary'].split('\n')) > 5:
                        print(f"     ... (truncated)")
            else:
                print(f"\n⚠️  No Smart Risk Manager Job found before {result['open_time']}")
            
            print("\n" + "="*80)
            print("✅ Test completed successfully!")
            print("="*80)
            
    except Exception as e:
        print(f"\n❌ Test failed with error:")
        print(f"   {type(e).__name__}: {e}")
        logger.error(f"Test failed: {e}", exc_info=True)
        raise


def test_multiple_positions():
    """Test with multiple positions to verify function works consistently."""
    
    print("\n" + "="*80)
    print("Testing with Multiple Positions")
    print("="*80)
    
    try:
        with get_db() as session:
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSED]))
                .order_by(Transaction.open_date.desc())
                .limit(5)
            ).all()
            
            if not transactions:
                print("\n⚠️  No transactions found")
                return
            
            print(f"\nTesting with {len(transactions)} recent transactions...")
            
            for i, txn in enumerate(transactions, 1):
                if not txn.expert_id:
                    continue
                
                # Get account_id from order
                order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == txn.id)
                    .limit(1)
                ).first()
                
                if not order or not order.account_id:
                    continue
                
                print(f"\n[{i}/{len(transactions)}] {txn.symbol} (ID: {txn.id})")
                
                toolkit = SmartRiskManagerToolkit(
                    expert_instance_id=txn.expert_id,
                    account_id=order.account_id
                )
                
                result = toolkit.get_analysis_at_open_time(
                    symbol=txn.symbol,
                    open_time=txn.open_date
                )
                
                has_ma = "✅" if result['market_analysis'] else "❌"
                has_srm = "✅" if result['risk_manager_job'] else "❌"
                
                print(f"   Market Analysis: {has_ma}")
                print(f"   SRM Job: {has_srm}")
            
            print("\n✅ Multi-position test completed!")
            
    except Exception as e:
        print(f"\n❌ Multi-position test failed: {e}")
        logger.error(f"Multi-position test failed: {e}", exc_info=True)


if __name__ == "__main__":
    try:
        # Run main test
        test_get_analysis_at_open_time()
        
        # Run multi-position test
        test_multiple_positions()
        
        print("\n" + "="*80)
        print("All tests completed!")
        print("="*80 + "\n")
        
    except Exception as e:
        print(f"\n❌ Test suite failed: {e}")
        sys.exit(1)

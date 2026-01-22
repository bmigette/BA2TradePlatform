"""
Diagnostic Script: Dump Account Positions and Orders

This script fetches and displays:
1. All positions from broker (via account interface)
2. All OPENED transactions from database
3. All orders for each transaction
4. Comparison to identify mismatches

Usage:
    python test_files/dump_account_positions_orders.py [--account-id ID]
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db, get_instance, get_all_instances
from ba2_trade_platform.core.models import AccountDefinition, Transaction, TradingOrder, ExpertInstance
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus
from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.logger import logger
from sqlmodel import select
from datetime import datetime
import argparse
import json


def format_datetime(dt):
    """Format datetime for display."""
    if dt is None:
        return "N/A"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def dump_broker_positions(account_instance):
    """Get all positions from broker."""
    print("\n" + "="*80)
    print("BROKER POSITIONS (from account interface)")
    print("="*80)
    
    try:
        positions = account_instance.get_positions()
        
        if not positions:
            print("No positions found at broker")
            return []
        
        print(f"\nFound {len(positions)} positions:\n")
        
        broker_symbols = set()
        for i, pos in enumerate(positions, 1):
            # Handle both dict and object formats
            if isinstance(pos, dict):
                symbol = pos.get('symbol')
                qty = pos.get('qty', pos.get('quantity', 0))
                side = pos.get('side')
                market_value = pos.get('market_value', 0)
                unrealized_pl = pos.get('unrealized_pl', 0)
                avg_entry = pos.get('avg_entry_price', 0)
            else:
                symbol = getattr(pos, 'symbol', None)
                qty = getattr(pos, 'qty', getattr(pos, 'quantity', 0))
                side = getattr(pos, 'side', None)
                market_value = getattr(pos, 'market_value', 0)
                unrealized_pl = getattr(pos, 'unrealized_pl', 0)
                avg_entry = getattr(pos, 'avg_entry_price', 0)
            
            broker_symbols.add(symbol)
            
            print(f"{i}. {symbol}")
            print(f"   Quantity: {qty}")
            print(f"   Side: {side}")
            print(f"   Avg Entry: ${avg_entry:.2f}")
            print(f"   Market Value: ${market_value:.2f}")
            print(f"   Unrealized P/L: ${unrealized_pl:.2f}")
            print()
        
        return broker_symbols
        
    except Exception as e:
        logger.error(f"Error fetching broker positions: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return set()


def dump_database_transactions(account_id):
    """Get all OPENED transactions from database."""
    print("\n" + "="*80)
    print("DATABASE TRANSACTIONS (OPENED status)")
    print("="*80)
    
    try:
        with get_db() as session:
            stmt = select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.status == TransactionStatus.OPENED
            ).order_by(Transaction.id)
            
            transactions = list(session.exec(stmt).all())
            
            if not transactions:
                print("No open transactions in database")
                return {}
            
            print(f"\nFound {len(transactions)} open transactions:\n")
            
            db_transactions = {}
            for txn in transactions:
                print(f"Transaction #{txn.id}")
                print(f"   Symbol: {txn.symbol}")
                print(f"   Side: {txn.side.value}")
                print(f"   Quantity: {txn.quantity}")
                print(f"   Status: {txn.status.value}")
                print(f"   Open Price: ${txn.open_price:.2f}" if txn.open_price else "   Open Price: N/A")
                print(f"   TP: ${txn.take_profit:.2f}" if txn.take_profit else "   TP: N/A")
                print(f"   SL: ${txn.stop_loss:.2f}" if txn.stop_loss else "   SL: N/A")
                print(f"   Created: {format_datetime(txn.created_at)}")
                print(f"   Opened: {format_datetime(txn.open_date)}")
                
                # Get expert info
                if txn.expert_instance_id:
                    expert = session.get(ExpertInstance, txn.expert_instance_id)
                    if expert:
                        expert_name = expert.alias or expert.expert
                        print(f"   Expert: {expert_name} (ID: {expert.id})")
                
                # Get orders
                orders_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == txn.id
                ).order_by(TradingOrder.created_at)
                orders = list(session.exec(orders_stmt).all())
                
                print(f"\n   Orders ({len(orders)}):")
                for order in orders:
                    print(f"      Order #{order.id}")
                    print(f"         Type: {order.order_type.value}")
                    print(f"         Side: {order.side.value}")
                    print(f"         Quantity: {order.quantity}")
                    print(f"         Status: {order.status.value}")
                    print(f"         Broker ID: {order.broker_order_id or 'N/A'}")
                    if order.limit_price:
                        print(f"         Limit: ${order.limit_price:.2f}")
                    if order.stop_price:
                        print(f"         Stop: ${order.stop_price:.2f}")
                    print(f"         Created: {format_datetime(order.created_at)}")
                    if order.comment:
                        print(f"         Comment: {order.comment[:50]}")
                    print()
                
                db_transactions[txn.symbol] = {
                    'transaction': txn,
                    'orders': orders
                }
                
                print("-" * 80)
                print()
            
            return db_transactions
            
    except Exception as e:
        logger.error(f"Error fetching database transactions: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return {}


def compare_positions(broker_symbols, db_transactions):
    """Compare broker positions with database transactions."""
    print("\n" + "="*80)
    print("COMPARISON ANALYSIS")
    print("="*80)
    
    db_symbols = set(db_transactions.keys())
    
    # Positions at broker but not in DB
    broker_only = broker_symbols - db_symbols
    if broker_only:
        print(f"\n⚠️  POSITIONS AT BROKER BUT NOT IN DATABASE ({len(broker_only)}):")
        for symbol in sorted(broker_only):
            print(f"   - {symbol}")
    else:
        print("\n✅ No orphaned positions at broker")
    
    # Transactions in DB but not at broker
    db_only = db_symbols - broker_symbols
    if db_only:
        print(f"\n⚠️  TRANSACTIONS IN DATABASE BUT NOT AT BROKER ({len(db_only)}):")
        for symbol in sorted(db_only):
            txn = db_transactions[symbol]['transaction']
            print(f"   - {symbol} (Transaction #{txn.id}, Qty: {txn.quantity})")
    else:
        print("\n✅ No stale transactions in database")
    
    # Matching positions
    matching = broker_symbols & db_symbols
    if matching:
        print(f"\n✅ MATCHING POSITIONS ({len(matching)}):")
        for symbol in sorted(matching):
            txn = db_transactions[symbol]['transaction']
            print(f"   - {symbol} (Transaction #{txn.id})")


def analyze_close_failures(account_id):
    """Analyze transactions that are stuck in CLOSING status."""
    print("\n" + "="*80)
    print("CLOSING STATUS ANALYSIS")
    print("="*80)
    
    try:
        with get_db() as session:
            stmt = select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.status == TransactionStatus.CLOSING
            ).order_by(Transaction.id)
            
            closing_transactions = list(session.exec(stmt).all())
            
            if not closing_transactions:
                print("\n✅ No transactions stuck in CLOSING status")
                return
            
            print(f"\n⚠️  Found {len(closing_transactions)} transactions in CLOSING status:\n")
            
            for txn in closing_transactions:
                print(f"Transaction #{txn.id} - {txn.symbol}")
                print(f"   Created: {format_datetime(txn.created_at)}")
                print(f"   Status changed to CLOSING: (check last update)")
                
                # Get closing orders
                orders_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == txn.id,
                    TradingOrder.comment.like('%closing%')
                ).order_by(TradingOrder.created_at.desc())
                
                closing_orders = list(session.exec(orders_stmt).all())
                
                if closing_orders:
                    print(f"   Closing Orders ({len(closing_orders)}):")
                    for order in closing_orders:
                        print(f"      Order #{order.id}")
                        print(f"         Status: {order.status.value}")
                        print(f"         Broker ID: {order.broker_order_id or 'N/A'}")
                        print(f"         Created: {format_datetime(order.created_at)}")
                        if order.status == OrderStatus.ERROR:
                            print(f"         ⚠️  ORDER IN ERROR STATUS - NEEDS RETRY")
                else:
                    print(f"   ⚠️  NO CLOSING ORDER FOUND - NEEDS INVESTIGATION")
                
                print()
            
    except Exception as e:
        logger.error(f"Error analyzing close failures: {e}", exc_info=True)
        print(f"ERROR: {e}")


def main():
    parser = argparse.ArgumentParser(description='Dump account positions and orders for diagnostics')
    parser.add_argument('--account-id', type=int, help='Account ID to analyze (default: first account)')
    args = parser.parse_args()
    
    # Get account
    if args.account_id:
        account_def = get_instance(AccountDefinition, args.account_id)
        if not account_def:
            print(f"ERROR: Account {args.account_id} not found")
            return 1
    else:
        accounts = get_all_instances(AccountDefinition)
        if not accounts:
            print("ERROR: No accounts found in database")
            return 1
        account_def = accounts[0]
        print(f"Using first account: {account_def.name} (ID: {account_def.id})")
    
    print(f"\nAccount: {account_def.name} ({account_def.provider})")
    print(f"Account ID: {account_def.id}")
    
    # Get account instance
    try:
        account_instance = get_account_instance_from_id(account_def.id)
        if not account_instance:
            print(f"ERROR: Could not load account instance for account {account_def.id}")
            return 1
    except Exception as e:
        logger.error(f"Error loading account instance: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return 1
    
    # Dump broker positions
    broker_symbols = dump_broker_positions(account_instance)
    
    # Dump database transactions
    db_transactions = dump_database_transactions(account_def.id)
    
    # Compare
    compare_positions(broker_symbols, db_transactions)
    
    # Analyze close failures
    analyze_close_failures(account_def.id)
    
    print("\n" + "="*80)
    print("DIAGNOSTIC COMPLETE")
    print("="*80)
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        print(f"\nFATAL ERROR: {e}")
        sys.exit(1)

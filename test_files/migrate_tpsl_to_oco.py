"""
Migration script to convert existing separate TP/SL orders to OCO orders.

This script:
1. Finds all transactions with active (non-terminal) separate TP or SL orders
2. Cancels those separate orders
3. Creates new OCO orders with the same TP/SL values (using defaults if missing)

Run with --dry-run to see what would be changed without making changes.
Run without --dry-run to apply changes.

Usage:
    .venv\Scripts\python.exe test_files\migrate_tpsl_to_oco.py --dry-run
    .venv\Scripts\python.exe test_files\migrate_tpsl_to_oco.py --apply
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from sqlmodel import Session, select
from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType as CoreOrderType, OrderOpenType
from ba2_trade_platform.modules.accounts.AlpacaAccount import DEFAULT_TP_PRICE, DEFAULT_SL_PRICE
from ba2_trade_platform.logger import logger

# Terminal statuses that indicate order is done
TERMINAL_STATUSES = OrderStatus.get_terminal_statuses()


def find_transactions_with_separate_tpsl(session: Session) -> list[dict]:
    """Find all transactions that have separate TP or SL orders (not OCO)."""
    from ba2_trade_platform.core.types import TransactionStatus
    
    results = []
    
    # Get all transactions with active orders
    # Check for TransactionStatus.OPENED (enum) or string equivalents
    transactions = session.exec(
        select(Transaction).where(
            Transaction.status.in_([TransactionStatus.OPENED, "OPEN", "open", "OPENED", "opened"])
        )
    ).all()
    
    for transaction in transactions:
        # Get all active orders for this transaction
        orders = session.exec(
            select(TradingOrder).where(
                TradingOrder.transaction_id == transaction.id,
                TradingOrder.status.notin_(TERMINAL_STATUSES)
            )
        ).all()
        
        # Categorize orders
        entry_order = None
        tp_order = None
        sl_order = None
        oco_order = None
        other_orders = []
        
        for order in orders:
            order_type_value = order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type)
            order_type_lower = order_type_value.lower()
            
            # Check for OCO first
            if order_type_lower == 'oco':
                oco_order = order
                continue
            
            # Check for entry order (MARKET or first limit without depends_on)
            if order_type_lower == 'market':
                if entry_order is None:
                    entry_order = order
                continue
            
            # Check for separate TP orders (limit orders that are NOT the entry)
            # TP orders typically have limit_price set and are SELL_LIMIT (for long) or BUY_LIMIT (for short)
            if order_type_lower in ['sell_limit', 'buy_limit']:
                if not order.depends_on_order and entry_order is None:
                    # This could be entry order
                    entry_order = order
                else:
                    # This is a TP order (limit order that depends on entry or is after entry)
                    tp_order = order
                continue
            
            # Check for separate SL orders (stop orders)
            if order_type_lower in ['sell_stop', 'buy_stop']:
                sl_order = order
                continue
            
            # Everything else
            other_orders.append(order)
        
        # Check if we have separate TP or SL but no OCO
        has_separate_tpsl = (tp_order or sl_order) and not oco_order
        
        if has_separate_tpsl:
            results.append({
                'transaction': transaction,
                'entry_order': entry_order,
                'tp_order': tp_order,
                'sl_order': sl_order,
                'oco_order': oco_order,
                'other_orders': other_orders
            })
    
    return results


def get_account_for_transaction(transaction: Transaction):
    """Get the account instance for a transaction."""
    from ba2_trade_platform.core.utils import get_account_instance_from_id
    return get_account_instance_from_id(transaction.account_id)


def migrate_to_oco(session: Session, data: dict, dry_run: bool = True) -> bool:
    """
    Migrate a transaction's separate TP/SL orders to a single OCO order.
    
    Args:
        session: Database session
        data: Dict with transaction, entry_order, tp_order, sl_order, etc.
        dry_run: If True, just log what would happen without making changes
        
    Returns:
        True if successful (or would be successful in dry run)
    """
    transaction = data['transaction']
    entry_order = data['entry_order']
    tp_order = data['tp_order']
    sl_order = data['sl_order']
    
    # Determine TP and SL prices
    tp_price = None
    sl_price = None
    
    if tp_order and tp_order.limit_price:
        tp_price = tp_order.limit_price
    elif transaction.take_profit:
        tp_price = transaction.take_profit
    
    if sl_order and sl_order.stop_price:
        sl_price = sl_order.stop_price
    elif transaction.stop_loss:
        sl_price = transaction.stop_loss
    
    # Apply defaults if missing
    effective_tp = tp_price if (tp_price and tp_price > 0) else DEFAULT_TP_PRICE
    effective_sl = sl_price if (sl_price and sl_price > 0) else DEFAULT_SL_PRICE
    
    # Format prices for display
    tp_price_str = f"${tp_order.limit_price:.2f}" if (tp_order and tp_order.limit_price) else "None"
    sl_price_str = f"${sl_order.stop_price:.2f}" if (sl_order and sl_order.stop_price) else "None"
    
    print(f"\nTransaction {transaction.id} ({transaction.symbol}):")
    print(f"  Entry order: {entry_order.id if entry_order else 'None'} (status: {entry_order.status if entry_order else 'N/A'})")
    print(f"  Current TP order: {tp_order.id if tp_order else 'None'} (price: {tp_price_str})")
    print(f"  Current SL order: {sl_order.id if sl_order else 'None'} (price: {sl_price_str})")
    print(f"  -> Will create OCO with TP=${effective_tp:.2f}, SL=${effective_sl:.2f}")
    
    if effective_tp == DEFAULT_TP_PRICE:
        print(f"     (TP is default - effectively no take profit)")
    if effective_sl == DEFAULT_SL_PRICE:
        print(f"     (SL is default - effectively no stop loss)")
    
    if dry_run:
        print("  [DRY RUN] Would cancel existing orders and create OCO")
        return True
    
    try:
        account = get_account_for_transaction(transaction)
        if not account:
            print(f"  ERROR: Could not get account for transaction {transaction.id}")
            return False
        
        # Cancel existing separate orders
        orders_to_cancel = []
        if tp_order:
            orders_to_cancel.append(tp_order)
        if sl_order:
            orders_to_cancel.append(sl_order)
        
        for order in orders_to_cancel:
            if order.broker_order_id:
                # Cancel at broker
                try:
                    account.cancel_order(order.id)
                    print(f"  Cancelled broker order {order.id} (broker_id={order.broker_order_id})")
                except Exception as e:
                    print(f"  WARNING: Failed to cancel broker order {order.id}: {e}")
            else:
                # Just mark as cancelled
                order.status = OrderStatus.CANCELED
                session.add(order)
                print(f"  Cancelled pending order {order.id}")
        
        session.commit()
        
        # Now use the account's adjust_tp_sl method which will create the OCO
        # This handles all the complexity of entry order state
        success = account.adjust_tp_sl(transaction, effective_tp, effective_sl)
        
        if success:
            print(f"  SUCCESS: Created OCO order for transaction {transaction.id}")
            return True
        else:
            print(f"  ERROR: Failed to create OCO order for transaction {transaction.id}")
            return False
            
    except Exception as e:
        print(f"  ERROR: Exception during migration: {e}")
        logger.error(f"Migration error for transaction {transaction.id}: {e}", exc_info=True)
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Migrate separate TP/SL orders to OCO orders')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be changed without making changes')
    parser.add_argument('--apply', action='store_true', help='Apply the changes')
    args = parser.parse_args()
    
    if not args.dry_run and not args.apply:
        print("Please specify either --dry-run or --apply")
        print("\nUsage:")
        print("  --dry-run  Show what would be changed without making changes")
        print("  --apply    Apply the changes")
        sys.exit(1)
    
    dry_run = args.dry_run
    
    print("=" * 60)
    print("TP/SL to OCO Migration Script")
    print("=" * 60)
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY CHANGES'}")
    print(f"Default TP: ${DEFAULT_TP_PRICE:.2f}")
    print(f"Default SL: ${DEFAULT_SL_PRICE:.2f}")
    print()
    
    with Session(get_db().bind) as session:
        # Find transactions that need migration
        transactions_to_migrate = find_transactions_with_separate_tpsl(session)
        
        print(f"Found {len(transactions_to_migrate)} transactions with separate TP/SL orders")
        
        if not transactions_to_migrate:
            print("\nNo migrations needed - all transactions already use OCO or have no TP/SL orders.")
            return
        
        success_count = 0
        error_count = 0
        
        for data in transactions_to_migrate:
            if migrate_to_oco(session, data, dry_run=dry_run):
                success_count += 1
            else:
                error_count += 1
        
        print("\n" + "=" * 60)
        print("Summary:")
        print(f"  Transactions processed: {len(transactions_to_migrate)}")
        print(f"  Successful: {success_count}")
        print(f"  Errors: {error_count}")
        
        if dry_run:
            print("\nThis was a dry run. Run with --apply to make actual changes.")
        else:
            print("\nChanges have been applied.")


if __name__ == "__main__":
    main()

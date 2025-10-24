"""Check which expert and what created transactions 190-203."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertRecommendation
from ba2_trade_platform.logger import logger
from sqlmodel import select

def main():
    with get_db() as session:
        # Get transactions 190-203
        logger.info("TRANSACTIONS 190-203 DETAILS:")
        logger.info("="*80)
        
        for txn_id in range(190, 204):
            txn = session.get(Transaction, txn_id)
            if not txn:
                continue
            
            # Get related orders
            orders = txn.trading_orders
            
            logger.info(f"\nTransaction {txn_id}: {txn.symbol} qty={txn.quantity}")
            logger.info(f"  Expert ID: {txn.expert_id}")
            logger.info(f"  Status: {txn.status.value if hasattr(txn.status, 'value') else txn.status}")
            logger.info(f"  Created: {txn.created_at}")
            logger.info(f"  Related Orders: {len(orders)}")
            
            for order in orders:
                logger.info(f"    Order {order.id}: {order.side} {order.quantity} @ ${order.limit_price}")
                logger.info(f"      Status: {order.status}")
                logger.info(f"      Broker Order ID: {order.broker_order_id or 'None'}")
                
                # Check if order has recommendation
                if order.expert_recommendation_id:
                    rec = session.get(ExpertRecommendation, order.expert_recommendation_id)
                    if rec:
                        action = rec.recommended_action.value if hasattr(rec.recommended_action, 'value') else rec.recommended_action
                        logger.info(f"      Recommendation {rec.id}: {action} confidence={rec.confidence}")
                        logger.info(f"      Rec created: {rec.created_at}")

if __name__ == "__main__":
    main()

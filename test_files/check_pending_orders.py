"""Check pending orders for expert taQuickGrok-ndq30-9"""

import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import TradingOrder, ExpertInstance, AccountDefinition, ExpertRecommendation
from ba2_trade_platform.logger import logger
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
from sqlmodel import select
from datetime import datetime

def main():
    with get_db() as session:
        # Find the expert by alias
        expert = session.exec(
            select(ExpertInstance).where(ExpertInstance.alias == "taQuickGrok-ndq30-9")
        ).first()
        
        if not expert:
            logger.error("Expert taQuickGrok-ndq30-9 not found")
            # Try finding by ID 9
            expert = session.get(ExpertInstance, 9)
            if not expert:
                logger.error("Expert ID 9 not found either")
                return
            logger.info("Found expert by ID 9 instead")
        
        logger.info(f"Found expert: ID={expert.id}, Alias={expert.alias}, Expert={expert.expert}")
        logger.info(f"Account ID: {expert.account_id}")
        
        # Get account info
        account = session.get(AccountDefinition, expert.account_id)
        if account:
            logger.info(f"Account: {account.name} (Provider: {account.provider})")
        
        # Load expert instance to access settings
        trading_agents = TradingAgents(expert.id)
        
        # Check expert settings
        logger.info(f"\nExpert Settings:")
        logger.info(f"  allow_automated_trade_opening: {trading_agents.settings.get('allow_automated_trade_opening', 'NOT SET')}")
        logger.info(f"  allow_automated_trade_modification: {trading_agents.settings.get('allow_automated_trade_modification', 'NOT SET')}")
        logger.info(f"  enable_buy: {trading_agents.settings.get('enable_buy', 'NOT SET')}")
        logger.info(f"  enable_sell: {trading_agents.settings.get('enable_sell', 'NOT SET')}")
        
        # Get pending orders through ExpertRecommendation join
        pending_orders = session.exec(
            select(TradingOrder)
            .join(ExpertRecommendation, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
            .where(ExpertRecommendation.instance_id == expert.id)
            .where(TradingOrder.status == "PENDING")
            .order_by(TradingOrder.id)
        ).all()
        
        logger.info(f"\n{'='*80}")
        logger.info(f"Found {len(pending_orders)} PENDING orders for expert {expert.alias or expert.id}")
        logger.info(f"{'='*80}")
        
        for order in pending_orders:
            logger.info(f"\nOrder ID: {order.id}")
            logger.info(f"  Symbol: {order.symbol}")
            logger.info(f"  Side: {order.side}")
            logger.info(f"  Quantity: {order.quantity}")
            logger.info(f"  Order Type: {order.order_type}")
            logger.info(f"  Limit Price: {order.limit_price}")
            logger.info(f"  Stop Price: {order.stop_price}")
            logger.info(f"  Created: {order.created_at}")
            logger.info(f"  Status: {order.status}")
            logger.info(f"  Broker Order ID: {order.broker_order_id}")
            logger.info(f"  Comment: {order.comment}")
            
            # Get associated recommendation
            if order.expert_recommendation_id:
                rec = session.get(ExpertRecommendation, order.expert_recommendation_id)
                if rec:
                    logger.info(f"  From Recommendation ID: {rec.id}")
                    logger.info(f"    Recommended Action: {rec.recommended_action}")
                    logger.info(f"    Confidence: {rec.confidence}")
                    logger.info(f"    Created: {rec.created_at}")
        
        # Check if there are any completed orders
        completed_orders = session.exec(
            select(TradingOrder)
            .join(ExpertRecommendation, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
            .where(ExpertRecommendation.instance_id == expert.id)
            .where(TradingOrder.status != "PENDING")
            .order_by(TradingOrder.id.desc())
        ).all()
        
        logger.info(f"\n{'='*80}")
        logger.info(f"Found {len(completed_orders)} NON-PENDING orders (for comparison)")
        logger.info(f"{'='*80}")
        
        if completed_orders:
            logger.info("\nMost recent 5 non-pending orders:")
            for order in completed_orders[:5]:
                logger.info(f"  Order {order.id}: {order.symbol} {order.side} - Status: {order.status}")

if __name__ == "__main__":
    main()

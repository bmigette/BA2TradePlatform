"""Check expert 9 pending orders and settings."""
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance, TradingOrder, ExpertRecommendation
from sqlmodel import select

with get_db() as session:
    # Get expert info
    expert = session.exec(select(ExpertInstance).where(ExpertInstance.id == 9)).first()
    if expert:
        print(f"\n=== Expert ID 9 ===")
        print(f"Name: {expert.expert}")
        print(f"Account ID: {expert.account_id}")
        print(f"\nSettings:")
        for key, value in expert.settings.items():
            if "allow_automated" in key or "enable_" in key:
                print(f"  {key}: {value}")
        
        # Get pending orders
        print(f"\n=== Pending Orders ===")
        orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.expert_instance_id == 9, TradingOrder.status == "PENDING")
            .order_by(TradingOrder.id)
        ).all()
        
        print(f"Found {len(orders)} PENDING orders")
        for order in orders[:10]:
            print(f"\nOrder {order.id}:")
            print(f"  Symbol: {order.instrument_symbol}")
            print(f"  Direction: {order.order_direction}")
            print(f"  Quantity: {order.quantity}")
            print(f"  Status: {order.status}")
            print(f"  Created: {order.created_at}")
            
            # Get recommendation
            if order.expert_recommendation_id:
                rec = session.get(ExpertRecommendation, order.expert_recommendation_id)
                if rec:
                    print(f"  Recommendation: {rec.recommended_action} (conf: {rec.confidence}%)")
    else:
        print("Expert ID 9 not found!")

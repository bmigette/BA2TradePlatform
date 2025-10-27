"""Check details of order 457 in transaction 80 (LRCX)"""
import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import TradingOrder, Transaction, MarketAnalysis, ExpertRecommendation
from sqlmodel import select

# Get database session
with get_db() as session:
    # Get order 457
    order = session.exec(select(TradingOrder).where(TradingOrder.id == 457)).first()
    if order:
        print(f"\n{'='*80}")
        print(f"ORDER 457 DETAILS")
        print(f"{'='*80}")
        print(f"Order ID: {order.id}")
        print(f"Broker Order ID: {order.broker_order_id}")
        print(f"Transaction ID: {order.transaction_id}")
        print(f"Symbol: {order.symbol}")
        print(f"Quantity: {order.quantity}")
        print(f"Side: {order.side}")
        print(f"Order Type: {order.order_type}")
        print(f"Status: {order.status}")
        print(f"Created At (DB): {order.created_at}")
        print(f"Open Price: {order.open_price}")
        print(f"Limit Price: {order.limit_price}")
        print(f"Stop Price: {order.stop_price}")
        print(f"Expert Recommendation ID: {order.expert_recommendation_id}")
        print(f"Open Type: {order.open_type}")
        print(f"Comment: {order.comment}")
        
        # Get transaction
        if order.transaction_id:
            transaction = session.exec(select(Transaction).where(Transaction.id == order.transaction_id)).first()
            if transaction:
                print(f"\n{'='*80}")
                print(f"TRANSACTION 80 DETAILS")
                print(f"{'='*80}")
                print(f"Transaction ID: {transaction.id}")
                print(f"Expert ID: {transaction.expert_id}")
                print(f"Symbol: {transaction.symbol}")
                print(f"Quantity: {transaction.quantity}")
                print(f"Status: {transaction.status}")
                print(f"Open Price: {transaction.open_price}")
                print(f"Close Price: {transaction.close_price}")
                print(f"Open Date: {transaction.open_date}")
                print(f"Close Date: {transaction.close_date}")
                print(f"Created At: {transaction.created_at}")
                print(f"Stop Loss: {transaction.stop_loss}")
                print(f"Take Profit: {transaction.take_profit}")
        
        # Get expert recommendation
        if order.expert_recommendation_id:
            recommendation = session.exec(select(ExpertRecommendation).where(ExpertRecommendation.id == order.expert_recommendation_id)).first()
            if recommendation:
                print(f"\n{'='*80}")
                print(f"EXPERT RECOMMENDATION DETAILS")
                print(f"{'='*80}")
                print(f"Recommendation ID: {recommendation.id}")
                print(f"Market Analysis ID: {recommendation.market_analysis_id}")
                print(f"Action Type: {recommendation.action_type}")
                print(f"Symbol: {recommendation.symbol}")
                print(f"Quantity: {recommendation.quantity}")
                print(f"Direction: {recommendation.direction}")
                print(f"Order Type: {recommendation.order_type}")
                print(f"Confidence: {recommendation.confidence}%")
                print(f"Reason: {recommendation.reason}")
                print(f"TP Price: {recommendation.tp_price}")
                print(f"SL Price: {recommendation.sl_price}")
                print(f"Created At: {recommendation.created_at}")
                
                # Get market analysis
                if recommendation.market_analysis_id:
                    analysis = session.exec(select(MarketAnalysis).where(MarketAnalysis.id == recommendation.market_analysis_id)).first()
                    if analysis:
                        print(f"\n{'='*80}")
                        print(f"MARKET ANALYSIS DETAILS")
                        print(f"{'='*80}")
                        print(f"Analysis ID: {analysis.id}")
                        print(f"Expert ID: {analysis.expert_id}")
                        print(f"Symbol: {analysis.symbol}")
                        print(f"Analysis Type: {analysis.analysis_type}")
                        print(f"Recommendation: {analysis.recommendation}")
                        print(f"Created At: {analysis.created_at}")
                        print(f"Completed At: {analysis.completed_at}")
                        if analysis.summary:
                            print(f"\nAnalysis Summary (first 800 chars):")
                            print(f"{analysis.summary[:800]}...")
    else:
        print("Order 457 not found!")


"""
Debug script for market analysis 9457 - Investigate suspicious target price of $103.

This script loads analysis 9457, displays all recommendations and their calculations,
and performs a dry run rule evaluation to diagnose the target price issue.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, ExpertRecommendation, ExpertInstance, TradingOrder
from ba2_trade_platform.logger import logger
from sqlmodel import select


def main():
    """Test market analysis 9457 with detailed output."""
    
    logger.info("="*80)
    logger.info("Testing Market Analysis 9457 - Target Price Investigation")
    logger.info("="*80)
    
    try:
        with get_db() as session:
            # Load market analysis 9457
            analysis = session.exec(
                select(MarketAnalysis).where(MarketAnalysis.id == 9457)
            ).first()
            
            if not analysis:
                logger.error("Analysis 9457 not found!")
                return
            
            logger.info(f"\nMarket Analysis Details:")
            logger.info(f"  ID: {analysis.id}")
            logger.info(f"  Symbol: {analysis.symbol}")
            logger.info(f"  Status: {analysis.status}")
            logger.info(f"  Created: {analysis.created_at}")
            
            # Load expert instance
            expert_instance = session.exec(
                select(ExpertInstance).where(ExpertInstance.id == analysis.expert_instance_id)
            ).first()
            
            if expert_instance:
                logger.info(f"  Expert: {expert_instance.expert} (ID: {expert_instance.id})")
                logger.info(f"  Expert Alias: {expert_instance.alias or 'N/A'}")
            
            # Load all recommendations for this analysis
            recommendations = session.exec(
                select(ExpertRecommendation)
                .where(ExpertRecommendation.market_analysis_id == 9457)
                .order_by(ExpertRecommendation.created_at.desc())
            ).all()
            
            logger.info(f"\n{len(recommendations)} Recommendations Found:")
            logger.info("-"*80)
            
            for i, rec in enumerate(recommendations, 1):
                logger.info(f"\n[Recommendation {i}] ID: {rec.id}")
                logger.info(f"  Symbol: {rec.symbol}")
                logger.info(f"  Action: {rec.recommended_action}")
                logger.info(f"  Confidence: {rec.confidence:.1f}%")
                logger.info(f"  Expected Profit: {rec.expected_profit_percent:.2f}%")
                logger.info(f"  Price at Date: ${rec.price_at_date:.2f}")
                logger.info(f"  Created: {rec.created_at}")
                
                # Calculate target price based on recommendation
                if rec.recommended_action.value == 'BUY':
                    calculated_target = rec.price_at_date * (1 + rec.expected_profit_percent / 100)
                    logger.info(f"  Calculated Target (BUY): ${rec.price_at_date:.2f} * (1 + {rec.expected_profit_percent:.2f}% / 100) = ${calculated_target:.2f}")
                elif rec.recommended_action.value == 'SELL':
                    calculated_target = rec.price_at_date * (1 - rec.expected_profit_percent / 100)
                    logger.info(f"  Calculated Target (SELL): ${rec.price_at_date:.2f} * (1 - {rec.expected_profit_percent:.2f}% / 100) = ${calculated_target:.2f}")
                else:
                    logger.info(f"  Calculated Target: N/A (action is {rec.recommended_action})")
                
                # Show recommendation data if available
                if rec.data:
                    logger.info(f"  Recommendation Data Keys: {list(rec.data.keys())}")
                    if 'target_price' in rec.data:
                        logger.info(f"  Data Target Price: ${rec.data['target_price']:.2f}")
                    if 'consensus_target' in rec.data:
                        logger.info(f"  Data Consensus Target: ${rec.data['consensus_target']:.2f}")
                    if 'high_target' in rec.data:
                        logger.info(f"  Data High Target: ${rec.data['high_target']:.2f}")
                    if 'low_target' in rec.data:
                        logger.info(f"  Data Low Target: ${rec.data['low_target']:.2f}")
                
                # Check for orders linked to this recommendation
                orders = session.exec(
                    select(TradingOrder).where(TradingOrder.expert_recommendation_id == rec.id)
                ).all()
                
                if orders:
                    logger.info(f"  Linked Orders: {len(orders)}")
                    for order in orders:
                        logger.info(f"    - Order {order.id}: {order.order_type} {order.direction} qty={order.quantity} @ ${order.limit_price:.2f if order.limit_price else 0:.2f} (status: {order.status})")
            
            # Perform dry run rule evaluation if expert instance exists
            if expert_instance and recommendations:
                logger.info("\n" + "="*80)
                logger.info("Additional Analysis Information")
                logger.info("="*80)
                
                try:
                    from ba2_trade_platform.core.utils import get_account_instance_from_id
                    
                    # Get account instance for this expert
                    account = get_account_instance_from_id(expert_instance.account_id)
                    
                    if not account:
                        logger.warning(f"Could not load account for expert instance {expert_instance.id}")
                    else:
                        logger.info(f"Loaded account: {account.__class__.__name__} (ID: {account.id})")
                        
                        # Get current price for the symbol
                        current_price = account.get_instrument_current_price(analysis.symbol)
                        logger.info(f"Current Price for {analysis.symbol}: ${current_price:.2f}")
                        
                        # Compare with target prices
                        for rec in recommendations:
                            if rec.recommended_action.value == 'BUY':
                                calculated_target = rec.price_at_date * (1 + rec.expected_profit_percent / 100)
                                logger.info(f"\nTarget Analysis for Recommendation {rec.id}:")
                                logger.info(f"  Base Price (at analysis): ${rec.price_at_date:.2f}")
                                logger.info(f"  Current Price: ${current_price:.2f}")
                                logger.info(f"  Target Price: ${calculated_target:.2f}")
                                logger.info(f"  Expected Profit: {rec.expected_profit_percent:.2f}%")
                                
                                if current_price < calculated_target:
                                    pct_to_target = ((calculated_target - current_price) / current_price) * 100
                                    logger.info(f"  Status: BELOW TARGET by {pct_to_target:.2f}%")
                                else:
                                    pct_over_target = ((current_price - calculated_target) / calculated_target) * 100
                                    logger.info(f"  Status: ABOVE TARGET by {pct_over_target:.2f}%")
                        
                        logger.info("\nTo troubleshoot ruleset:")
                        logger.info(f"  Navigate to: http://localhost:8080/rulesettest?market_analysis_id={analysis.id}")
                        
                except ImportError as e:
                    logger.error(f"Could not import required modules: {e}")
                except Exception as e:
                    logger.error(f"Additional analysis failed: {e}", exc_info=True)
            
            logger.info("\n" + "="*80)
            logger.info("Analysis Complete")
            logger.info("="*80)
            
    except Exception as e:
        logger.error(f"Error testing analysis 9457: {e}", exc_info=True)


if __name__ == "__main__":
    main()

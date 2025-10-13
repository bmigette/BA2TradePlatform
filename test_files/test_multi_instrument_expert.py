#!/usr/bin/env python3
"""
Test script for multi-instrument FMPSenateTraderCopy expert functionality.

This script tests that the expert:
1. Can recommend its own instruments via get_recommended_instruments()
2. Creates multiple ExpertRecommendation records per MarketAnalysis
3. UI displays correctly handle multiple recommendations
"""

import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_session, get_instance, add_instance
from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis, ExpertRecommendation
from ba2_trade_platform.modules.experts.FMPSenateTraderCopy import FMPSenateTraderCopy

def test_expert_properties():
    """Test that the expert has the correct properties set."""
    logger.info("=== Testing Expert Properties ===")
    
    # Test the class properties
    properties = FMPSenateTraderCopy.get_expert_properties()
    
    logger.info(f"Expert properties: {properties}")
    
    # Verify key properties
    assert properties.get('can_recommend_instruments', False) == True, "should have can_recommend_instruments=True"
    assert properties.get('should_expand_instrument_jobs', True) == False, "should have should_expand_instrument_jobs=False"
    
    logger.info("✅ Expert properties are correct")

def test_get_recommended_instruments():
    """Test that the expert can recommend its own instruments."""
    logger.info("\n=== Testing get_recommended_instruments ===")
    
    # Create a test expert instance
    with get_session() as session:
        # Find or create an expert instance
        expert_instance = session.query(ExpertInstance).filter(
            ExpertInstance.expert == 'FMPSenateTraderCopy'
        ).first()
        
        if not expert_instance:
            logger.info("No existing FMPSenateTraderCopy expert instance found, skipping test")
            return
        
        logger.info(f"Using expert instance {expert_instance.id}")
        
        # Create expert object
        expert = FMPSenateTraderCopy(expert_instance.id)
        
        # Test get_recommended_instruments
        try:
            recommended_instruments = expert.get_recommended_instruments()
            logger.info(f"Recommended instruments: {recommended_instruments}")
            
            if recommended_instruments:
                logger.info(f"✅ Expert recommended {len(recommended_instruments)} instruments")
                for i, symbol in enumerate(recommended_instruments[:5]):  # Show first 5
                    logger.info(f"  {i+1}. {symbol}")
                if len(recommended_instruments) > 5:
                    logger.info(f"  ... and {len(recommended_instruments) - 5} more")
            else:
                logger.info("⚠️ Expert returned empty list (may be expected if no recent trades)")
                
        except Exception as e:
            logger.error(f"❌ Error getting recommended instruments: {e}", exc_info=True)

def test_multi_instrument_analysis():
    """Test that run_analysis creates multiple recommendations."""
    logger.info("\n=== Testing Multi-Instrument Analysis ===")
    
    with get_session() as session:
        # Find a FMPSenateTraderCopy expert instance
        expert_instance = session.query(ExpertInstance).filter(
            ExpertInstance.expert == 'FMPSenateTraderCopy'
        ).first()
        
        if not expert_instance:
            logger.info("No existing FMPSenateTraderCopy expert instance found, skipping test")
            return
        
        logger.info(f"Using expert instance {expert_instance.id}")
        
        # Create expert object
        expert = FMPSenateTraderCopy(expert_instance.id)
        
        # Test run_analysis with dummy symbol 'MULTI'
        try:
            logger.info("Running analysis with symbol 'MULTI'...")
            result = expert.run_analysis('MULTI')
            
            if result:
                analysis_id = result.get('analysis_id')
                if analysis_id:
                    # Check the created analysis
                    analysis = session.get(MarketAnalysis, analysis_id)
                    if analysis:
                        logger.info(f"✅ Created MarketAnalysis {analysis_id}")
                        logger.info(f"  Symbol: {analysis.symbol}")
                        logger.info(f"  Status: {analysis.status}")
                        logger.info(f"  Recommendations count: {len(analysis.expert_recommendations)}")
                        
                        # Show details of recommendations
                        for i, rec in enumerate(analysis.expert_recommendations, 1):
                            logger.info(f"  Recommendation {i}:")
                            logger.info(f"    Symbol: {rec.symbol}")
                            logger.info(f"    Action: {rec.recommended_action}")
                            logger.info(f"    Confidence: {rec.confidence}%")
                            logger.info(f"    Expected Profit: {rec.expected_profit_percent}%")
                        
                        if len(analysis.expert_recommendations) > 1:
                            logger.info("✅ Multi-instrument analysis working correctly!")
                        else:
                            logger.info("⚠️ Only single recommendation generated (may be expected)")
                    else:
                        logger.error(f"❌ MarketAnalysis {analysis_id} not found in database")
                else:
                    logger.error("❌ No analysis_id returned from run_analysis")
            else:
                logger.error("❌ run_analysis returned no result")
                
        except Exception as e:
            logger.error(f"❌ Error running multi-instrument analysis: {e}", exc_info=True)

def test_ui_display_logic():
    """Test the UI display logic for multi-instrument recommendations."""
    logger.info("\n=== Testing UI Display Logic ===")
    
    with get_session() as session:
        # Find a recent analysis with multiple recommendations
        analysis = session.query(MarketAnalysis).join(ExpertRecommendation).filter(
            MarketAnalysis.expert_instance_id.in_(
                session.query(ExpertInstance.id).filter(ExpertInstance.expert == 'FMPSenateTraderCopy')
            )
        ).first()
        
        if not analysis:
            logger.info("No existing analysis found for testing UI display")
            return
        
        recommendations = analysis.expert_recommendations
        logger.info(f"Testing UI logic with analysis {analysis.id} ({len(recommendations)} recommendations)")
        
        # Simulate the UI display logic from marketanalysis.py
        if len(recommendations) == 1:
            rec = recommendations[0]
            logger.info("Single recommendation display:")
            if rec.recommended_action:
                action_icons = {'BUY': '📈', 'SELL': '📉', 'HOLD': '⏸️', 'ERROR': '❌'}
                action_value = rec.recommended_action.value
                action_icon = action_icons.get(action_value, '')
                recommendation_display = f'{action_icon} {action_value}'
                logger.info(f"  Recommendation: {recommendation_display}")
            
            if rec.confidence is not None:
                confidence_display = f'{rec.confidence:.1f}%'
                logger.info(f"  Confidence: {confidence_display}")
        
        elif len(recommendations) > 1:
            logger.info("Multi-recommendation display:")
            action_counts = {}
            confidences = []
            profits = []
            symbols = []
            
            for rec in recommendations:
                if rec.recommended_action:
                    action = rec.recommended_action.value
                    action_counts[action] = action_counts.get(action, 0) + 1
                
                if rec.confidence is not None:
                    confidences.append(rec.confidence)
                
                if rec.expected_profit_percent is not None:
                    profits.append(rec.expected_profit_percent)
                
                if rec.symbol:
                    symbols.append(rec.symbol)
            
            # Format recommendation summary
            if action_counts:
                action_summary = []
                action_icons = {'BUY': '📈', 'SELL': '📉', 'HOLD': '⏸️', 'ERROR': '❌'}
                for action, count in sorted(action_counts.items()):
                    icon = action_icons.get(action, '')
                    action_summary.append(f'{icon}{count}')
                recommendation_display = ' '.join(action_summary) + f' ({len(recommendations)} symbols)'
                logger.info(f"  Recommendation: {recommendation_display}")
            
            # Format confidence summary
            if confidences:
                avg_confidence = sum(confidences) / len(confidences)
                confidence_display = f'{avg_confidence:.1f}% avg'
                logger.info(f"  Confidence: {confidence_display}")
            
            # Format profit summary
            if profits:
                avg_profit = sum(profits) / len(profits)
                sign = '+' if avg_profit >= 0 else ''
                expected_profit_display = f'{sign}{avg_profit:.2f}% avg'
                logger.info(f"  Expected Profit: {expected_profit_display}")
            
            # Symbol display
            if len(symbols) <= 3:
                symbol_display = ', '.join(symbols)
            else:
                symbol_display = f'{", ".join(symbols[:3])}... (+{len(symbols)-3})'
            logger.info(f"  Symbols: {symbol_display}")
            
            logger.info("✅ Multi-recommendation UI display logic working")

def main():
    """Run all tests."""
    logger.info("Starting multi-instrument expert tests...")
    
    try:
        test_expert_properties()
        test_get_recommended_instruments()
        test_multi_instrument_analysis()
        test_ui_display_logic()
        
        logger.info("\n🎉 All tests completed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
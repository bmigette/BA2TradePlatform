"""Check the stored data for MarketAnalysis ID 788."""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import MarketAnalysis
import json

def check_analysis():
    """Check analysis 788."""
    analysis = get_instance(MarketAnalysis, 788)
    
    if not analysis:
        print("Analysis 788 not found!")
        return
    
    print(f"Analysis ID: {analysis.id}")
    print(f"Symbol: {analysis.symbol}")
    print(f"Status: {analysis.status}")
    print(f"\nState data:")
    
    if analysis.state and 'fmp_rating' in analysis.state:
        fmp_state = analysis.state['fmp_rating']
        
        # Recommendation
        rec = fmp_state.get('recommendation', {})
        print(f"\nRecommendation:")
        print(f"  Signal: {rec.get('signal')}")
        print(f"  Confidence: {rec.get('confidence')}%")
        print(f"  Expected Profit: {rec.get('expected_profit_percent')}%")
        
        # Confidence Breakdown
        breakdown = fmp_state.get('confidence_breakdown', {})
        print(f"\nConfidence Breakdown:")
        print(f"  Base Confidence: {breakdown.get('base_confidence')}%")
        print(f"  Price Target Boost: {breakdown.get('price_target_boost')}%")
        print(f"  Boost to Lower: {breakdown.get('boost_to_lower')}%")
        print(f"  Boost to Consensus: {breakdown.get('boost_to_consensus')}%")
        print(f"  Buy Score: {breakdown.get('buy_score')}")
        print(f"  Sell Score: {breakdown.get('sell_score')}")
        print(f"  Hold Score: {breakdown.get('hold_score')}")
        
        # Calculate what it should be
        base = breakdown.get('base_confidence', 0)
        boost = breakdown.get('price_target_boost', 0)
        calculated = base + boost
        clamped = max(0.0, min(100.0, calculated))
        stored = rec.get('confidence', 0)
        
        print(f"\nCalculation Check:")
        print(f"  Base + Boost = {base} + {boost} = {calculated}%")
        print(f"  Clamped [0-100]: {clamped}%")
        print(f"  Stored in rec: {stored}%")
        print(f"  Match: {'✓ YES' if abs(stored - clamped) < 0.1 else '✗ NO - MISMATCH!'}")
        
        # Price targets
        targets = fmp_state.get('price_targets', {})
        print(f"\nPrice Targets:")
        print(f"  Consensus: ${targets.get('consensus')}")
        print(f"  Low: ${targets.get('low')}")
        print(f"  High: ${targets.get('high')}")
        print(f"  Median: ${targets.get('median')}")
        
        # Current price
        current = fmp_state.get('current_price')
        print(f"\nCurrent Price: ${current}")
        
    else:
        print("No FMP rating state found!")
        print(f"Full state: {json.dumps(analysis.state, indent=2)}")

if __name__ == "__main__":
    check_analysis()

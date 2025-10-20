"""
Migration script to update MarketAnalysis.state confidence_breakdown for FMPRating.

This script recalculates and updates the confidence breakdown in Market Analysis state
records to use the new calculation methodology (analyst ratings base + price target boost)
instead of the old methodology (target spread base + analyst coverage boost).

The script:
1. Finds all completed FMPRating MarketAnalysis records
2. Extracts stored API data (consensus + upgrade_downgrade) from state
3. Recalculates confidence breakdown using new formula
4. Updates MarketAnalysis.state with new confidence_breakdown structure
"""

import sys
import json
from pathlib import Path
from typing import Dict, Any, Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db, update_instance
from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis
from ba2_trade_platform.core.types import MarketAnalysisStatus
from sqlmodel import select


def recalculate_confidence_breakdown(
    consensus_data: Dict[str, Any],
    upgrade_data: Dict[str, Any],
    current_price: float
) -> Dict[str, Any]:
    """
    Recalculate confidence breakdown using new methodology.
    
    Args:
        consensus_data: FMP price target consensus API response
        upgrade_data: FMP upgrade/downgrade consensus API response (first record)
        current_price: Current stock price at time of analysis
    
    Returns:
        Dict with new confidence breakdown components
    """
    # Extract analyst ratings
    strong_buy = upgrade_data.get('strongBuy', 0)
    buy = upgrade_data.get('buy', 0)
    hold = upgrade_data.get('hold', 0)
    sell = upgrade_data.get('sell', 0)
    strong_sell = upgrade_data.get('strongSell', 0)
    
    # Extract price targets
    target_consensus = consensus_data.get('targetConsensus', 0)
    target_low = consensus_data.get('targetLow', 0)
    target_high = consensus_data.get('targetHigh', 0)
    
    # Step 1: Calculate weighted scores (FinnHub methodology)
    strong_factor = 2.0
    buy_score = (strong_buy * strong_factor) + buy
    sell_score = (strong_sell * strong_factor) + sell
    hold_score = hold
    
    total_weighted = buy_score + sell_score + hold_score
    
    if total_weighted == 0:
        return None  # Cannot calculate without analyst data
    
    # Step 2: Base confidence from analyst ratings
    dominant_score = max(buy_score, sell_score, hold_score)
    base_confidence = (dominant_score / total_weighted) * 100
    
    # Step 3: Price target boost
    if current_price == 0:
        return None  # Cannot calculate without current price
    
    boost_to_lower = ((target_low - current_price) / current_price) * 100
    boost_to_consensus = ((target_consensus - current_price) / current_price) * 100
    price_target_boost = (boost_to_lower + boost_to_consensus) / 2.0
    
    # Step 4: Final confidence (clamped to 0-100%)
    confidence = max(0.0, min(100.0, base_confidence + price_target_boost))
    
    return {
        'base_confidence': round(base_confidence, 1),
        'price_target_boost': round(price_target_boost, 1),
        'boost_to_lower': round(boost_to_lower, 1),
        'boost_to_consensus': round(boost_to_consensus, 1),
        'buy_score': round(buy_score, 1),
        'sell_score': round(sell_score, 1),
        'hold_score': round(hold_score, 1),
    }


def migrate_fmprating_state():
    """
    Main migration function to update all FMPRating MarketAnalysis state records.
    """
    print("=" * 80)
    print("FMPRating MarketAnalysis State Migration")
    print("=" * 80)
    print()
    print("This script will update the confidence_breakdown in MarketAnalysis.state")
    print("for all completed FMPRating analyses.")
    print()
    
    # Confirmation prompt
    response = input("Continue with migration? (yes/no): ")
    if response.lower() != 'yes':
        print("Migration cancelled.")
        return
    
    print()
    print("=" * 80)
    print()
    
    # Find all FMPRating expert instances
    with get_db() as session:
        instances = session.exec(
            select(ExpertInstance).where(ExpertInstance.expert == "FMPRating")
        ).all()
        
        if not instances:
            print("No FMPRating expert instances found.")
            return
        
        print(f"Found {len(instances)} FMPRating expert instance(s)")
        print()
        
        total_analyses = 0
        total_updated = 0
        total_skipped = 0
        total_errors = 0
        
        for instance in instances:
            print(f"Processing Expert Instance ID: {instance.id} (Alias: {instance.alias})")
            
            # Find all completed analyses for this expert
            analyses = session.exec(
                select(MarketAnalysis)
                .where(MarketAnalysis.expert_instance_id == instance.id)
                .where(MarketAnalysis.status == MarketAnalysisStatus.COMPLETED)
            ).all()
            
            print(f"  Found {len(analyses)} completed analyses")
            total_analyses += len(analyses)
            
            for analysis in analyses:
                try:
                    # Check if state has fmp_rating data
                    if not analysis.state or 'fmp_rating' not in analysis.state:
                        print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): No fmp_rating state")
                        logger.warning(f"Analysis {analysis.id} has no fmp_rating state")
                        total_skipped += 1
                        continue
                    
                    fmp_state = analysis.state['fmp_rating']
                    
                    # Extract stored API data
                    consensus_data = fmp_state.get('consensus_data')
                    upgrade_data_list = fmp_state.get('upgrade_data')
                    current_price = fmp_state.get('current_price')
                    
                    if not consensus_data or not upgrade_data_list or not current_price:
                        print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): Missing API data")
                        logger.warning(f"Analysis {analysis.id} missing API data in state")
                        total_skipped += 1
                        continue
                    
                    # Get first upgrade record
                    upgrade_data = upgrade_data_list[0] if upgrade_data_list else {}
                    
                    if not upgrade_data:
                        print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): No upgrade data")
                        total_skipped += 1
                        continue
                    
                    # Recalculate confidence breakdown
                    new_breakdown = recalculate_confidence_breakdown(
                        consensus_data,
                        upgrade_data,
                        current_price
                    )
                    
                    if not new_breakdown:
                        print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): Could not recalculate breakdown")
                        total_skipped += 1
                        continue
                    
                    # Update state with new confidence breakdown
                    analysis.state['fmp_rating']['confidence_breakdown'] = new_breakdown
                    
                    # Also update the recommendation confidence to match
                    base_confidence = new_breakdown.get('base_confidence', 0)
                    price_target_boost = new_breakdown.get('price_target_boost', 0)
                    calculated_confidence = base_confidence + price_target_boost
                    final_confidence = max(0.0, min(100.0, calculated_confidence))
                    
                    if 'recommendation' in analysis.state['fmp_rating']:
                        analysis.state['fmp_rating']['recommendation']['confidence'] = round(final_confidence, 1)
                    
                    # Mark object as modified (SQLAlchemy will track the change)
                    from sqlalchemy.orm import attributes
                    attributes.flag_modified(analysis, "state")
                    
                    print(f"    ✓ Analysis {analysis.id} ({analysis.symbol}): Updated (confidence: {final_confidence:.1f}%)")
                    total_updated += 1
                    
                except Exception as e:
                    print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): Error - {e}")
                    logger.error(f"Error migrating analysis {analysis.id}: {e}", exc_info=True)
                    total_errors += 1
            
            # Commit all changes for this expert instance
            if total_updated > 0:
                session.commit()
                print(f"  ✓ Committed {total_updated} updates for this expert")
            
            print()
    
    print("=" * 80)
    print("Migration Summary")
    print("=" * 80)
    print(f"Total Analyses: {total_analyses}")
    print(f"Updated: {total_updated}")
    print(f"Skipped: {total_skipped}")
    print(f"Errors: {total_errors}")
    print("=" * 80)
    
    if total_updated > 0:
        print()
        print(f"Successfully updated {total_updated} MarketAnalysis state record(s).")
        print("The UI will now display the new confidence breakdown for these analyses.")
    else:
        print()
        print("No state records were updated.")


if __name__ == "__main__":
    try:
        migrate_fmprating_state()
    except Exception as e:
        print(f"Migration failed: {e}")
        logger.error(f"State migration failed: {e}", exc_info=True)
        sys.exit(1)

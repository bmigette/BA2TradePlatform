"""
Migration Script: Update FMPRating Confidence Calculation

This script recalculates and updates all existing FMPRating MarketAnalysis records
with the new confidence calculation methodology (FinnHub style + price target boost).

The new formula:
1. Calculate base score from analyst buy/sell ratings (FinnHub methodology)
2. Calculate price target boost from current price to lower/consensus targets
3. Average the boosts and add to base confidence
4. Clamp final confidence to 0-100%

Run with:
    .venv\Scripts\python.exe test_files\migrate_fmprating_confidence.py  (Windows)
    .venv/bin/python test_files/migrate_fmprating_confidence.py          (Unix)
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from typing import Dict, Any, Optional
import json

from ba2_trade_platform.config import load_config_from_env
load_config_from_env()

from ba2_trade_platform.core.db import get_db, update_instance, get_instance
from ba2_trade_platform.core.models import MarketAnalysis, ExpertRecommendation, ExpertInstance, AnalysisOutput
from ba2_trade_platform.core.types import OrderRecommendation, MarketAnalysisStatus
from ba2_trade_platform.logger import logger
from ba2_trade_platform.modules.experts.FMPRating import FMPRating
from sqlmodel import Session, select


def extract_data_from_analysis(market_analysis: MarketAnalysis) -> Optional[Dict[str, Any]]:
    """
    Extract necessary data from existing MarketAnalysis to recalculate confidence.
    
    Returns dict with:
        - consensus_data (price targets)
        - upgrade_data (analyst ratings)
        - current_price
        - profit_ratio
        - min_analysts
    """
    try:
        engine = get_db()
        with Session(engine.bind) as session:
            # Get all analysis outputs for this market analysis
            outputs = session.exec(
                select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == market_analysis.id)
            ).all()
            
            consensus_data = None
            upgrade_data = None
            
            # Find the API response outputs
            for output in outputs:
                if output.type == "fmp_price_target_api_response":
                    try:
                        consensus_data = json.loads(output.text)
                    except:
                        pass
                elif output.type == "fmp_upgrade_downgrade_api_response":
                    try:
                        upgrade_data = json.loads(output.text)
                    except:
                        pass
            
            if not consensus_data or not upgrade_data:
                logger.warning(f"Missing API data for MarketAnalysis {market_analysis.id}")
                return None
            
            # Get expert instance to get settings
            expert_instance = get_instance(ExpertInstance, market_analysis.expert_instance_id)
            if not expert_instance:
                logger.warning(f"Expert instance not found for MarketAnalysis {market_analysis.id}")
                return None
            
            # Get settings
            settings = expert_instance.settings or {}
            settings_def = FMPRating.get_settings_definitions()
            
            profit_ratio = float(settings.get('profit_ratio', settings_def['profit_ratio']['default']))
            min_analysts = int(settings.get('min_analysts', settings_def['min_analysts']['default']))
            
            # Get current price from recommendation
            recommendation = session.exec(
                select(ExpertRecommendation).where(
                    ExpertRecommendation.market_analysis_id == market_analysis.id
                )
            ).first()
            
            current_price = recommendation.price_at_date if recommendation else None
            
            if not current_price:
                logger.warning(f"Current price not found for MarketAnalysis {market_analysis.id}")
                return None
            
            return {
                'consensus_data': consensus_data,
                'upgrade_data': upgrade_data,
                'current_price': current_price,
                'profit_ratio': profit_ratio,
                'min_analysts': min_analysts
            }
            
    except Exception as e:
        logger.error(f"Error extracting data from MarketAnalysis {market_analysis.id}: {e}")
        return None


def recalculate_confidence(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Recalculate confidence using the new FMPRating._calculate_recommendation method.
    """
    try:
        # Create a temporary FMPRating instance (we'll use a dummy ID)
        # We just need the _calculate_recommendation method
        consensus_data = data['consensus_data']
        upgrade_data = data['upgrade_data']
        current_price = data['current_price']
        profit_ratio = data['profit_ratio']
        min_analysts = data['min_analysts']
        
        # Use the static calculation method (we'll need to access it properly)
        # For now, replicate the logic here
        
        if not consensus_data:
            return None
        
        # Extract consensus data
        target_consensus = consensus_data.get('targetConsensus')
        target_high = consensus_data.get('targetHigh')
        target_low = consensus_data.get('targetLow')
        
        # Get analyst ratings
        analyst_count = 0
        strong_buy = 0
        buy = 0
        hold = 0
        sell = 0
        strong_sell = 0
        
        if upgrade_data and len(upgrade_data) > 0:
            latest_grade = upgrade_data[0]
            strong_buy = latest_grade.get('strongBuy', 0)
            buy = latest_grade.get('buy', 0)
            hold = latest_grade.get('hold', 0)
            sell = latest_grade.get('sell', 0)
            strong_sell = latest_grade.get('strongSell', 0)
            analyst_count = strong_buy + buy + hold + sell + strong_sell
        
        if analyst_count < min_analysts:
            return {
                'confidence': 20.0,
                'analyst_count': analyst_count
            }
        
        # Calculate base score from analyst ratings (FinnHub style)
        strong_factor = 2.0
        buy_score = (strong_buy * strong_factor) + buy
        sell_score = (strong_sell * strong_factor) + sell
        hold_score = hold
        
        total_weighted = buy_score + sell_score + hold_score
        
        # Determine signal and base confidence
        if buy_score > sell_score and buy_score > hold_score:
            dominant_score = buy_score
        elif sell_score > buy_score and sell_score > hold_score:
            dominant_score = sell_score
        else:
            dominant_score = hold_score
        
        base_confidence = (dominant_score / total_weighted * 100) if total_weighted > 0 else 0.0
        
        # Calculate price target boost
        boost_to_lower = 0.0
        boost_to_consensus = 0.0
        
        if current_price and target_low and target_consensus:
            boost_to_lower = ((target_low - current_price) / current_price) * 100
            boost_to_consensus = ((target_consensus - current_price) / current_price) * 100
            price_target_boost = (boost_to_lower + boost_to_consensus) / 2.0
        else:
            price_target_boost = 0.0
        
        # Apply boost and clamp to 0-100%
        confidence = base_confidence + price_target_boost
        confidence = max(0.0, min(100.0, confidence))
        
        return {
            'confidence': confidence,
            'base_confidence': base_confidence,
            'price_target_boost': price_target_boost,
            'analyst_count': analyst_count
        }
        
    except Exception as e:
        logger.error(f"Error recalculating confidence: {e}")
        return None


def migrate_fmprating_recommendations():
    """
    Main migration function to update all FMPRating recommendations.
    """
    print("\n" + "="*80)
    print("FMPRating Confidence Migration Script")
    print("="*80 + "\n")
    
    engine = get_db()
    
    try:
        with Session(engine.bind) as session:
            # Find all FMPRating expert instances
            fmp_instances = session.exec(
                select(ExpertInstance).where(ExpertInstance.expert == "FMPRating")
            ).all()
            
            if not fmp_instances:
                print("No FMPRating expert instances found.")
                return
            
            print(f"Found {len(fmp_instances)} FMPRating expert instance(s)\n")
            
            total_analyses = 0
            updated_count = 0
            skipped_count = 0
            error_count = 0
            
            for instance in fmp_instances:
                print(f"Processing Expert Instance ID: {instance.id} (Alias: {instance.alias})")
                
                # Get all market analyses for this instance
                analyses = session.exec(
                    select(MarketAnalysis).where(
                        MarketAnalysis.expert_instance_id == instance.id,
                        MarketAnalysis.status == MarketAnalysisStatus.COMPLETED
                    )
                ).all()
                
                print(f"  Found {len(analyses)} completed analyses")
                total_analyses += len(analyses)
                
                for analysis in analyses:
                    try:
                        # Extract data from analysis
                        data = extract_data_from_analysis(analysis)
                        if not data:
                            print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): Missing data")
                            skipped_count += 1
                            continue
                        
                        # Recalculate confidence
                        new_calc = recalculate_confidence(data)
                        if not new_calc:
                            print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): Calculation failed")
                            error_count += 1
                            continue
                        
                        # Get the recommendation to update
                        recommendation = session.exec(
                            select(ExpertRecommendation).where(
                                ExpertRecommendation.market_analysis_id == analysis.id
                            )
                        ).first()
                        
                        if not recommendation:
                            print(f"    ✗ Analysis {analysis.id} ({analysis.symbol}): No recommendation found")
                            skipped_count += 1
                            continue
                        
                        old_confidence = recommendation.confidence
                        new_confidence = new_calc['confidence']
                        
                        # Update the recommendation
                        recommendation.confidence = round(new_confidence, 1)
                        session.add(recommendation)
                        
                        print(f"    ✓ Analysis {analysis.id} ({analysis.symbol}): "
                              f"{old_confidence:.1f}% → {new_confidence:.1f}% "
                              f"(base: {new_calc['base_confidence']:.1f}%, boost: {new_calc['price_target_boost']:.1f}%)")
                        
                        updated_count += 1
                        
                    except Exception as e:
                        logger.error(f"Error processing Analysis {analysis.id}: {e}")
                        print(f"    ✗ Analysis {analysis.id}: Error - {e}")
                        error_count += 1
                
                print()
            
            # Commit all changes
            session.commit()
            
            # Print summary
            print("="*80)
            print("Migration Summary")
            print("="*80)
            print(f"Total Analyses: {total_analyses}")
            print(f"Updated: {updated_count}")
            print(f"Skipped: {skipped_count}")
            print(f"Errors: {error_count}")
            print("="*80 + "\n")
            
            if updated_count > 0:
                print(f"✓ Successfully updated {updated_count} recommendations!")
            else:
                print("No recommendations were updated.")
            
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        print(f"\n✗ Migration failed: {e}")
        return False
    
    return True


if __name__ == "__main__":
    try:
        print("\nWARNING: This script will update all existing FMPRating recommendations.")
        print("Make sure you have a database backup before proceeding.\n")
        
        response = input("Continue with migration? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print("Migration cancelled.")
            sys.exit(0)
        
        success = migrate_fmprating_recommendations()
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        print("\n\nMigration interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        logger.error(f"Fatal error in migration script: {e}", exc_info=True)
        sys.exit(1)

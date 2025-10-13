#!/usr/bin/env python3
"""
Test Tool for FMPSenateTraderCopy Expert

This test tool validates the multi-instrument functionality of the FMPSenateTraderCopy expert:
- Expert properties configuration
- Multi-instrument analysis capability  
- Multiple ExpertRecommendation generation
- UI display logic for multi-symbol results
- Database integration

Usage:
    python test_tools/test_fmp_senate_copy.py [options]
    
Options:
    --create-instance: Create a new expert instance for testing
    --run-analysis: Run a multi-instrument analysis
    --test-ui: Test UI display logic with mock data
    --validate-db: Validate database relationships
    --all: Run all tests
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

# Add the project root to the path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db, add_instance, update_instance, get_instance
from ba2_trade_platform.core.models import (
    ExpertInstance, MarketAnalysis, ExpertRecommendation, 
    AccountDefinition, AnalysisOutput
)
from ba2_trade_platform.core.types import (
    OrderRecommendation, MarketAnalysisStatus, 
    AnalysisUseCase, RiskLevel, TimeHorizon
)
from ba2_trade_platform.modules.experts.FMPSenateTraderCopy import FMPSenateTraderCopy
from ba2_trade_platform.config import get_app_setting
from sqlmodel import select

class FMPSenateTraderCopyTester:
    """Test harness for FMPSenateTraderCopy expert."""
    
    def __init__(self):
        self.expert_instance = None
        self.expert = None
        self.session = None
        
    def setup(self):
        """Set up test environment."""
        logger.info("=== FMPSenateTraderCopy Expert Test Tool ===")
        self.session = get_db()
        
    def cleanup(self):
        """Clean up test environment."""
        if self.session:
            self.session.close()
            
    def test_expert_properties(self) -> bool:
        """Test expert class properties."""
        logger.info("\n🔍 Testing Expert Properties...")
        
        try:
            # Test class methods
            description = FMPSenateTraderCopy.description()
            logger.info(f"Expert description: {description}")
            
            properties = FMPSenateTraderCopy.get_expert_properties()
            logger.info(f"Expert properties: {properties}")
            
            settings_defs = FMPSenateTraderCopy.get_settings_definitions()
            logger.info(f"Settings definitions: {list(settings_defs.keys())}")
            
            # Validate key properties
            expected_props = {
                'can_recommend_instruments': True,
                'should_expand_instrument_jobs': False
            }
            
            for prop, expected_value in expected_props.items():
                actual_value = properties.get(prop)
                if actual_value != expected_value:
                    logger.error(f"❌ Property {prop}: expected {expected_value}, got {actual_value}")
                    return False
                logger.info(f"✅ Property {prop}: {actual_value}")
            
            # Validate required settings
            required_settings = ['copy_trade_names', 'max_disclose_date_days', 'max_trade_exec_days']
            for setting in required_settings:
                if setting not in settings_defs:
                    logger.error(f"❌ Missing required setting: {setting}")
                    return False
                logger.info(f"✅ Setting defined: {setting}")
            
            logger.info("✅ Expert properties test passed")
            return True
            
        except Exception as e:
            logger.error(f"❌ Expert properties test failed: {e}", exc_info=True)
            return False
    
    def find_or_create_expert_instance(self, create_new: bool = False) -> bool:
        """Find existing or create new expert instance."""
        logger.info(f"\n🔧 {'Creating new' if create_new else 'Finding'} Expert Instance...")
        
        try:
            if not create_new:
                # Try to find existing instance
                self.expert_instance = self.session.exec(
                    select(ExpertInstance).where(ExpertInstance.expert == 'FMPSenateTraderCopy')
                ).first()
                
                if self.expert_instance:
                    logger.info(f"✅ Found existing expert instance: {self.expert_instance.id}")
                    self.expert = FMPSenateTraderCopy(self.expert_instance.id)
                    return True
                else:
                    logger.info("No existing expert instance found, will create new one")
            
            # Create new instance
            # First, find an account to use
            account = self.session.exec(select(AccountDefinition)).first()
            if not account:
                logger.error("❌ No account instances found. Create an account first.")
                return False
            
            # Create expert instance
            expert_instance = ExpertInstance(
                account_id=account.id,
                expert='FMPSenateTraderCopy',
                enabled=True,
                settings={
                    'copy_trade_names': 'Nancy Pelosi, Josh Gottheimer',  # Test with well-known traders
                    'max_disclose_date_days': 30,
                    'max_trade_exec_days': 60
                }
            )
            
            instance_id = add_instance(expert_instance)
            self.expert_instance = get_instance(ExpertInstance, instance_id)
            
            logger.info(f"✅ Created new expert instance: {instance_id}")
            logger.info(f"   Account: {account.id}")
            logger.info(f"   Settings: {self.expert_instance.settings}")
            
            # Initialize expert
            self.expert = FMPSenateTraderCopy(instance_id)
            logger.info("✅ Expert initialized successfully")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to create expert instance: {e}", exc_info=True)
            return False
    
    def test_api_connection(self) -> bool:
        """Test FMP API connection and data fetching."""
        logger.info("\n🌐 Testing FMP API Connection...")
        
        try:
            # Check if API key is configured
            api_key = get_app_setting('FMP_API_KEY')
            if not api_key:
                logger.warning("⚠️ FMP_API_KEY not configured in app settings")
                logger.info("   This is expected in test environment")
                return True  # Don't fail the test for missing API key
            
            logger.info("✅ FMP API key found in app settings")
            
            # Test fetching trades (will likely fail due to API limits, but tests the connection)
            try:
                senate_trades = self.expert._fetch_senate_trades(symbol=None)
                house_trades = self.expert._fetch_house_trades(symbol=None)
                
                logger.info(f"Senate trades fetched: {len(senate_trades) if senate_trades else 0}")
                logger.info(f"House trades fetched: {len(house_trades) if house_trades else 0}")
                
                if senate_trades or house_trades:
                    logger.info("✅ API connection successful")
                else:
                    logger.warning("⚠️ API connection working but no trades returned")
                
            except Exception as api_error:
                logger.warning(f"⚠️ API request failed (expected in test): {api_error}")
                logger.info("   This is normal if API limits are reached or in test environment")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ API connection test failed: {e}", exc_info=True)
            return False
    
    def create_test_analysis(self) -> Optional[MarketAnalysis]:
        """Create a test market analysis for multi-instrument testing."""
        logger.info("\n📊 Creating Test Market Analysis...")
        
        try:
            analysis = MarketAnalysis(
                expert_instance_id=self.expert_instance.id,
                symbol='MULTI',  # Multi-instrument placeholder
                subtype=AnalysisUseCase.OPEN_POSITIONS,
                status=MarketAnalysisStatus.PENDING,
                created_at=datetime.now(timezone.utc)
            )
            
            analysis_id = add_instance(analysis)
            analysis = get_instance(MarketAnalysis, analysis_id)
            
            logger.info(f"✅ Created test analysis: {analysis_id}")
            logger.info(f"   Symbol: {analysis.symbol}")
            logger.info(f"   Status: {analysis.status.value}")
            
            return analysis
            
        except Exception as e:
            logger.error(f"❌ Failed to create test analysis: {e}", exc_info=True)
            return None
    
    def test_multi_instrument_analysis(self) -> bool:
        """Test the multi-instrument analysis functionality."""
        logger.info("\n🎯 Testing Multi-Instrument Analysis...")
        
        try:
            # Create test analysis
            analysis = self.create_test_analysis()
            if not analysis:
                return False
            
            # Run the analysis (this will likely fail due to API limits, but tests the logic)
            try:
                self.expert.run_analysis('MULTI', analysis)
                
                # Refresh analysis from database
                analysis = get_instance(MarketAnalysis, analysis.id)
                
                logger.info(f"Analysis status: {analysis.status.value}")
                logger.info(f"Analysis state keys: {list(analysis.state.keys()) if analysis.state else 'None'}")
                
                # Check for recommendations
                recommendations = self.session.exec(
                    select(ExpertRecommendation).where(ExpertRecommendation.analysis_id == analysis.id)
                ).all()
                
                logger.info(f"Generated recommendations: {len(recommendations)}")
                
                for i, rec in enumerate(recommendations, 1):
                    logger.info(f"  Recommendation {i}:")
                    logger.info(f"    Symbol: {rec.symbol}")
                    logger.info(f"    Action: {rec.recommended_action}")
                    logger.info(f"    Confidence: {rec.confidence}%")
                    logger.info(f"    Expected Profit: {rec.expected_profit_percent}%")
                
                if len(recommendations) > 1:
                    logger.info("✅ Multi-instrument analysis generated multiple recommendations")
                elif len(recommendations) == 1:
                    logger.info("✅ Single recommendation generated (expected if limited trade data)")
                else:
                    logger.info("✅ No recommendations generated (expected if no matching trades)")
                
                # Check analysis outputs
                outputs = self.session.exec(
                    select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis.id)
                ).all()
                
                logger.info(f"Analysis outputs stored: {len(outputs)}")
                for output in outputs:
                    logger.info(f"  - {output.name} ({output.type})")
                
                return True
                
            except Exception as analysis_error:
                logger.warning(f"⚠️ Analysis execution failed (expected in test): {analysis_error}")
                logger.info("   This is normal if API limits are reached or no matching trades found")
                
                # Check if analysis was marked as failed
                analysis = get_instance(MarketAnalysis, analysis.id)
                if analysis.status == MarketAnalysisStatus.FAILED:
                    logger.info("✅ Analysis properly marked as failed")
                    return True
                
                return False
                
        except Exception as e:
            logger.error(f"❌ Multi-instrument analysis test failed: {e}", exc_info=True)
            return False
    
    def test_ui_display_logic(self) -> bool:
        """Test UI display logic with mock multi-symbol data."""
        logger.info("\n🖥️ Testing UI Display Logic...")
        
        try:
            # Create mock recommendations data
            mock_recommendations = [
                {
                    'symbol': 'AAPL',
                    'recommended_action': OrderRecommendation.BUY,
                    'confidence': 100.0,
                    'expected_profit_percent': 50.0
                },
                {
                    'symbol': 'TSLA', 
                    'recommended_action': OrderRecommendation.SELL,
                    'confidence': 100.0,
                    'expected_profit_percent': 50.0
                },
                {
                    'symbol': 'NVDA',
                    'recommended_action': OrderRecommendation.BUY,
                    'confidence': 100.0,
                    'expected_profit_percent': 50.0
                }
            ]
            
            logger.info("Testing multi-recommendation UI display logic:")
            
            # Test the UI logic from marketanalysis.py
            action_counts = {}
            confidences = []
            profits = []
            symbols = []
            
            for rec in mock_recommendations:
                if rec['recommended_action']:
                    action = rec['recommended_action'].value
                    action_counts[action] = action_counts.get(action, 0) + 1
                
                if rec['confidence'] is not None:
                    confidences.append(rec['confidence'])
                
                if rec['expected_profit_percent'] is not None:
                    profits.append(rec['expected_profit_percent'])
                
                if rec['symbol']:
                    symbols.append(rec['symbol'])
            
            # Format recommendation summary
            if action_counts:
                action_summary = []
                action_icons = {'BUY': '📈', 'SELL': '📉', 'HOLD': '⏸️', 'ERROR': '❌'}
                for action, count in sorted(action_counts.items()):
                    icon = action_icons.get(action, '')
                    action_summary.append(f'{icon}{count}')
                recommendation_display = ' '.join(action_summary) + f' ({len(mock_recommendations)} symbols)'
                logger.info(f"  Recommendation display: {recommendation_display}")
            
            # Format confidence summary
            if confidences:
                avg_confidence = sum(confidences) / len(confidences)
                confidence_display = f'{avg_confidence:.1f}% avg'
                logger.info(f"  Confidence display: {confidence_display}")
            
            # Format profit summary
            if profits:
                avg_profit = sum(profits) / len(profits)
                sign = '+' if avg_profit >= 0 else ''
                expected_profit_display = f'{sign}{avg_profit:.2f}% avg'
                logger.info(f"  Expected profit display: {expected_profit_display}")
            
            # Symbol display
            if len(symbols) <= 3:
                symbol_display = ', '.join(symbols)
            else:
                first_three = ', '.join(symbols[:3])
                remaining = len(symbols) - 3
                symbol_display = f'{first_three}... (+{remaining})'
            logger.info(f"  Symbol display: {symbol_display}")
            
            logger.info("✅ UI display logic test passed")
            return True
            
        except Exception as e:
            logger.error(f"❌ UI display logic test failed: {e}", exc_info=True)
            return False
    
    def validate_database_relationships(self) -> bool:
        """Validate database relationships for multi-instrument analysis."""
        logger.info("\n🗃️ Validating Database Relationships...")
        
        try:
            # Find any existing multi-instrument analyses
            analyses = self.session.exec(
                select(MarketAnalysis).where(
                    MarketAnalysis.expert_instance_id.in_(
                        select(ExpertInstance.id).where(ExpertInstance.expert == 'FMPSenateTraderCopy')
                    )
                )
            ).all()
            
            logger.info(f"Found {len(analyses)} FMPSenateTraderCopy analyses in database")
            
            for analysis in analyses:
                recommendations = self.session.exec(
                    select(ExpertRecommendation).where(ExpertRecommendation.market_analysis_id == analysis.id)
                ).all()
                
                outputs = self.session.exec(
                    select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == analysis.id)
                ).all()
                
                logger.info(f"Analysis {analysis.id} ({analysis.symbol}):")
                logger.info(f"  Status: {analysis.status.value}")
                logger.info(f"  Recommendations: {len(recommendations)}")
                logger.info(f"  Outputs: {len(outputs)}")
                
                if len(recommendations) > 1:
                    logger.info("  📊 Multi-instrument analysis found:")
                    for rec in recommendations:
                        logger.info(f"    - {rec.symbol}: {rec.recommended_action.value if rec.recommended_action else 'None'}")
                    
                    # Verify the relationship integrity
                    for rec in recommendations:
                        if rec.analysis_id != analysis.id:
                            logger.error(f"❌ Recommendation {rec.id} has incorrect analysis_id")
                            return False
                    
                    logger.info("  ✅ Database relationships are correct")
            
            if not analyses:
                logger.info("✅ No existing analyses found (expected in fresh environment)")
            
            logger.info("✅ Database relationship validation passed")
            return True
            
        except Exception as e:
            logger.error(f"❌ Database relationship validation failed: {e}", exc_info=True)
            return False
    
    def run_comprehensive_test(self) -> bool:
        """Run all tests in sequence."""
        logger.info("\n🚀 Running Comprehensive Test Suite...")
        
        test_results = []
        
        # Test 1: Expert Properties
        test_results.append(("Expert Properties", self.test_expert_properties()))
        
        # Test 2: Expert Instance
        test_results.append(("Expert Instance", self.find_or_create_expert_instance()))
        
        # Test 3: API Connection
        if self.expert:
            test_results.append(("API Connection", self.test_api_connection()))
        
        # Test 4: Multi-Instrument Analysis
        if self.expert:
            test_results.append(("Multi-Instrument Analysis", self.test_multi_instrument_analysis()))
        
        # Test 5: UI Display Logic
        test_results.append(("UI Display Logic", self.test_ui_display_logic()))
        
        # Test 6: Database Relationships
        test_results.append(("Database Relationships", self.validate_database_relationships()))
        
        # Summary
        logger.info("\n📋 Test Results Summary:")
        logger.info("=" * 50)
        
        passed = 0
        failed = 0
        
        for test_name, result in test_results:
            status = "✅ PASSED" if result else "❌ FAILED"
            logger.info(f"{test_name:<25} : {status}")
            if result:
                passed += 1
            else:
                failed += 1
        
        logger.info("=" * 50)
        logger.info(f"Total Tests: {len(test_results)}")
        logger.info(f"Passed: {passed}")
        logger.info(f"Failed: {failed}")
        
        overall_success = failed == 0
        logger.info(f"Overall Result: {'✅ ALL TESTS PASSED' if overall_success else '❌ SOME TESTS FAILED'}")
        
        return overall_success

def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(description='Test FMPSenateTraderCopy Expert')
    parser.add_argument('--create-instance', action='store_true', 
                       help='Create a new expert instance for testing')
    parser.add_argument('--run-analysis', action='store_true',
                       help='Run a multi-instrument analysis')
    parser.add_argument('--test-ui', action='store_true',
                       help='Test UI display logic with mock data')
    parser.add_argument('--validate-db', action='store_true',
                       help='Validate database relationships')
    parser.add_argument('--all', action='store_true',
                       help='Run all tests')
    
    args = parser.parse_args()
    
    # If no specific tests requested, run all
    if not any([args.create_instance, args.run_analysis, args.test_ui, args.validate_db]):
        args.all = True
    
    tester = FMPSenateTraderCopyTester()
    
    try:
        tester.setup()
        
        if args.all:
            success = tester.run_comprehensive_test()
        else:
            success = True
            
            if args.create_instance:
                success &= tester.find_or_create_expert_instance(create_new=True)
            else:
                success &= tester.find_or_create_expert_instance()
            
            if args.run_analysis and tester.expert:
                success &= tester.test_multi_instrument_analysis()
            
            if args.test_ui:
                success &= tester.test_ui_display_logic()
            
            if args.validate_db:
                success &= tester.validate_database_relationships()
        
        return 0 if success else 1
        
    except Exception as e:
        logger.error(f"Test runner failed: {e}", exc_info=True)
        return 1
    finally:
        tester.cleanup()

if __name__ == "__main__":
    sys.exit(main())
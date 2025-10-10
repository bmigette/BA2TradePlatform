"""
Test script for FMPSenateTrade expert
Tests the expert directly without queue system
"""

import sys
import os
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db, add_instance, get_instance
from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis, AccountDefinition, ExpertSetting
from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.modules.experts.FMPSenateTrade import FMPSenateTrade
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import load_config_from_env

# Load configuration
load_config_from_env()

def create_default_settings(expert_id: int):
    """Create default settings for FMPSenateTrade expert."""
    from sqlmodel import select
    session = get_db()
    
    try:
        # Get settings definitions from expert class
        settings_def = FMPSenateTrade.get_settings_definitions()
        
        # Check if settings already exist
        statement = select(ExpertSetting).where(
            ExpertSetting.instance_id == expert_id
        )
        existing_settings = session.exec(statement).all()
        
        if existing_settings:
            logger.debug(f"Settings already exist for expert {expert_id}, skipping creation")
            return
        
        # Create default settings
        for key, config in settings_def.items():
            if 'default' in config:
                value_type = config.get('type')
                default_value = config['default']
                
                # Determine which column to use based on type
                if value_type == 'str':
                    setting = ExpertSetting(
                        instance_id=expert_id,
                        key=key,
                        value_str=str(default_value)
                    )
                elif value_type in ['float', 'int']:
                    # Both int and float go into value_float column
                    setting = ExpertSetting(
                        instance_id=expert_id,
                        key=key,
                        value_float=float(default_value)
                    )
                elif value_type == 'bool':
                    # Booleans are stored as JSON
                    setting = ExpertSetting(
                        instance_id=expert_id,
                        key=key,
                        value_json=default_value
                    )
                else:
                    # Default to string
                    setting = ExpertSetting(
                        instance_id=expert_id,
                        key=key,
                        value_str=str(default_value)
                    )
                
                session.add(setting)
        
        session.commit()
        logger.info(f"Created default settings for expert {expert_id}")
        
    except Exception as e:
        logger.error(f"Error creating default settings: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()

def create_test_expert_instance():
    """Create or get a test FMPSenateTrade expert instance."""
    from sqlmodel import select
    session = get_db()
    
    try:
        # Get first available account (or create a dummy one)
        account = session.exec(select(AccountDefinition)).first()
        if not account:
            logger.error("No account found. Please create an account first.")
            return None
        
        # Check if test expert already exists
        statement = select(ExpertInstance).where(
            ExpertInstance.expert == 'FMPSenateTrade',
            ExpertInstance.alias == 'Test Senate Trade'
        )
        expert = session.exec(statement).first()
        
        if expert:
            logger.info(f"Using existing test expert instance: {expert.id}")
            expert_id = expert.id
            session.close()
            
            # Ensure default settings exist
            create_default_settings(expert_id)
            
            return expert_id
        
        # Create new test expert instance
        expert = ExpertInstance(
            account_id=account.id,
            expert='FMPSenateTrade',
            alias='Test Senate Trade',
            enabled=True,
            created_at=datetime.now(timezone.utc)
        )
        
        session.add(expert)
        session.commit()
        expert_id = expert.id
        session.close()
        
        # Create default settings for the new expert
        create_default_settings(expert_id)
        
        logger.info(f"Created test expert instance: {expert_id}")
        return expert_id
        
    except Exception as e:
        logger.error(f"Error creating test expert instance: {e}", exc_info=True)
        session.rollback()
        return None
    finally:
        session.close()

def create_test_market_analysis(expert_id: int, symbol: str):
    """Create a test MarketAnalysis record."""
    session = get_db()
    
    try:
        analysis = MarketAnalysis(
            expert_instance_id=expert_id,
            symbol=symbol,
            status=MarketAnalysisStatus.PENDING,
            created_at=datetime.now(timezone.utc)
        )
        
        session.add(analysis)
        session.commit()
        analysis_id = analysis.id
        
        logger.info(f"Created test market analysis: {analysis_id} for {symbol}")
        return analysis_id
        
    except Exception as e:
        logger.error(f"Error creating test market analysis: {e}", exc_info=True)
        session.rollback()
        return None
    finally:
        session.close()

def test_fmp_senate_trade(symbol: str = "AAPL"):
    """Test FMPSenateTrade expert for a given symbol."""
    
    logger.info("="*80)
    logger.info(f"Testing FMPSenateTrade Expert for {symbol}")
    logger.info("="*80)
    
    # Step 1: Create or get test expert instance
    logger.info("\n[Step 1] Creating/Getting test expert instance...")
    expert_id = create_test_expert_instance()
    if not expert_id:
        logger.error("Failed to create expert instance. Aborting test.")
        return
    
    # Step 2: Create test market analysis
    logger.info(f"\n[Step 2] Creating test market analysis for {symbol}...")
    analysis_id = create_test_market_analysis(expert_id, symbol)
    if not analysis_id:
        logger.error("Failed to create market analysis. Aborting test.")
        return
    
    # Step 3: Initialize expert
    logger.info(f"\n[Step 3] Initializing FMPSenateTrade expert (ID: {expert_id})...")
    try:
        expert = FMPSenateTrade(expert_id)
        logger.info(f"Expert initialized successfully")
        logger.info(f"Expert settings: {expert.settings}")
    except Exception as e:
        logger.error(f"Failed to initialize expert: {e}", exc_info=True)
        return
    
    # Step 4: Get market analysis
    logger.info(f"\n[Step 4] Loading market analysis (ID: {analysis_id})...")
    market_analysis = get_instance(MarketAnalysis, analysis_id)
    if not market_analysis:
        logger.error("Failed to load market analysis. Aborting test.")
        return
    
    # Step 5: Run analysis
    logger.info(f"\n[Step 5] Running FMPSenateTrade analysis for {symbol}...")
    logger.info("-"*80)
    
    try:
        expert.run_analysis(symbol, market_analysis)
        logger.info("-"*80)
        logger.info(f"✅ Analysis completed successfully!")
        
        # Step 6: Display results
        logger.info(f"\n[Step 6] Analysis Results:")
        logger.info("-"*80)
        
        # Reload market analysis to get updated state
        from sqlmodel import select
        session = get_db()
        statement = select(MarketAnalysis).where(MarketAnalysis.id == analysis_id)
        market_analysis = session.exec(statement).first()
        
        if market_analysis and market_analysis.state:
            state = market_analysis.state.get('senate_trade', {})
            rec = state.get('recommendation', {})
            stats = state.get('trade_statistics', {})
            
            logger.info(f"\nRecommendation:")
            logger.info(f"  Signal: {rec.get('signal', 'N/A')}")
            logger.info(f"  Confidence: {rec.get('confidence', 0):.1f}%")
            logger.info(f"  Expected Profit: {rec.get('expected_profit_percent', 0):.1f}%")
            
            logger.info(f"\nTrade Statistics:")
            logger.info(f"  Total Trades Found: {stats.get('total_trades', 0)}")
            logger.info(f"  Filtered Trades: {stats.get('filtered_trades', 0)}")
            logger.info(f"  Buy Trades: {stats.get('buy_count', 0)}")
            logger.info(f"  Sell Trades: {stats.get('sell_count', 0)}")
            logger.info(f"  Total Buy Amount: ${stats.get('total_buy_amount', 0):,.0f}")
            logger.info(f"  Total Sell Amount: ${stats.get('total_sell_amount', 0):,.0f}")
            
            # Show individual trades if any
            trades = state.get('trades', [])
            if trades:
                logger.info(f"\nIndividual Trades ({len(trades)}):")
                for i, trade in enumerate(trades[:5], 1):  # Show first 5
                    logger.info(f"\n  Trade #{i}:")
                    logger.info(f"    Trader: {trade.get('trader', 'Unknown')}")
                    logger.info(f"    Type: {trade.get('type', 'N/A')}")
                    logger.info(f"    Amount: {trade.get('amount', 'N/A')}")
                    logger.info(f"    Exec Date: {trade.get('exec_date', 'N/A')} ({trade.get('days_since_exec', 0)} days ago)")
                    logger.info(f"    Confidence: {trade.get('confidence', 0):.1f}%")
                    logger.info(f"    Symbol Focus: {trade.get('symbol_focus_pct', 0):.1f}% (of trader's portfolio)")
                    logger.info(f"    Trader Recent Activity: {trade.get('trader_recent_buys', 'N/A')} buys, {trade.get('trader_recent_sells', 'N/A')} sells")
                    logger.info(f"    Trader Yearly Activity: {trade.get('trader_yearly_buys', 'N/A')} buys, {trade.get('trader_yearly_sells', 'N/A')} sells")
                    logger.info(f"    Yearly Symbol Activity: {trade.get('yearly_symbol_buys', 'N/A')} buys, {trade.get('yearly_symbol_sells', 'N/A')} sells")
                
                if len(trades) > 5:
                    logger.info(f"\n  ... and {len(trades) - 5} more trades")
        
        session.close()
        
        logger.info("\n" + "="*80)
        logger.info("✅ Test completed successfully!")
        logger.info(f"Market Analysis ID: {analysis_id}")
        logger.info(f"You can view full results in the database or UI")
        logger.info("="*80)
        
    except Exception as e:
        logger.error(f"\n❌ Analysis failed: {e}", exc_info=True)
        logger.info("\n" + "="*80)
        logger.info("❌ Test failed!")
        logger.info("="*80)

if __name__ == "__main__":
    # Test with AAPL by default, or use command line argument
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    
    print("\n")
    print("╔════════════════════════════════════════════════════════════╗")
    print("║         FMPSenateTrade Expert Test Script                 ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print(f"\nTesting symbol: {symbol}")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n")
    
    test_fmp_senate_trade(symbol)


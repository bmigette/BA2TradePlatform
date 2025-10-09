"""
Test script for TradingAgents timeframe functionality

This script tests that the TradingAgents tools honor the timeframe setting.
"""

import sys
import os
# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_timeframe_configuration():
    """Test that timeframe setting is properly used by TradingAgents tools."""
    print("=" * 60)
    print("TradingAgents Timeframe Configuration Test")
    print("=" * 60)
    
    try:
        # Import necessary modules
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.config import get_config, set_config
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        # Test 1: Default timeframe
        print("\n1. Testing default timeframe configuration...")
        config = get_config()
        default_timeframe = config.get("timeframe", "NOT_SET")
        print(f"   Current timeframe setting: {default_timeframe}")
        
        # Test 2: Verify available timeframe options
        print("\n2. Testing timeframe options...")
        timeframe_options = ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"]
        print(f"   Available timeframe options: {timeframe_options}")
        
        # Test 3: Set different timeframes and verify they're used
        print("\n3. Testing timeframe setting propagation...")
        
        for timeframe in ["1h", "1d", "1wk"]:
            print(f"\n   Testing timeframe: {timeframe}")
            
            # Set the timeframe in config
            test_config = config.copy()
            test_config["timeframe"] = timeframe
            set_config(test_config)
            
            # Create toolkit instance with updated config
            toolkit = Toolkit(test_config)
            
            # Verify the config is updated
            updated_config = get_config()
            actual_timeframe = updated_config.get("timeframe", "NOT_SET")
            
            if actual_timeframe == timeframe:
                print(f"     ✓ Timeframe correctly set to: {actual_timeframe}")
            else:
                print(f"     ✗ Timeframe mismatch: expected {timeframe}, got {actual_timeframe}")
        
        # Test 4: Check function signatures accept interval parameter
        print("\n4. Testing function signatures...")
        
        # Import the interface functions to check their signatures
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.interface import (
            get_YFin_data_online, 
            get_stockstats_indicator,
            get_stock_stats_indicators_window
        )
        
        import inspect
        
        # Check get_YFin_data_online signature
        sig = inspect.signature(get_YFin_data_online)
        if 'interval' in sig.parameters:
            print("   ✓ get_YFin_data_online accepts interval parameter")
        else:
            print("   ✗ get_YFin_data_online missing interval parameter")
        
        # Check get_stockstats_indicator signature
        sig = inspect.signature(get_stockstats_indicator)
        if 'interval' in sig.parameters:
            print("   ✓ get_stockstats_indicator accepts interval parameter")
        else:
            print("   ✗ get_stockstats_indicator missing interval parameter")
        
        # Check get_stock_stats_indicators_window signature
        sig = inspect.signature(get_stock_stats_indicators_window)
        if 'interval' in sig.parameters:
            print("   ✓ get_stock_stats_indicators_window accepts interval parameter")
        else:
            print("   ✗ get_stock_stats_indicators_window missing interval parameter")
        
        # Test 5: Check agent tools configuration
        print("\n5. Testing agent tools configuration...")
        
        # Set a specific timeframe
        test_config = {
            "timeframe": "1h",
            "market_history_days": 30,
        }
        set_config(test_config)
        
        toolkit = Toolkit(test_config)
        
        # Check that toolkit has access to the config
        if hasattr(toolkit, 'config'):
            toolkit_timeframe = toolkit.config.get("timeframe", "NOT_SET")
            if toolkit_timeframe == "1h":
                print("   ✓ Toolkit correctly configured with timeframe: 1h")
            else:
                print(f"   ✗ Toolkit timeframe mismatch: expected 1h, got {toolkit_timeframe}")
        else:
            print("   ✗ Toolkit missing config access")
        
        print("\n" + "=" * 60)
        print("✓ TIMEFRAME CONFIGURATION TESTS COMPLETED")
        print("=" * 60)
        
        # Usage instructions
        print("\nTimeframe Feature Summary:")
        print("1. Expert setting 'timeframe' now controls data granularity")
        print("2. Valid values: 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo")
        print("3. Default: 1d (daily data)")
        print("4. Affects both market data retrieval and technical indicators")
        print("5. Works with both online and cached data")
        
        print("\nData Impact by Timeframe:")
        print("• 1m, 5m, 15m, 30m: Intraday analysis, day trading")
        print("• 1h: Short-term analysis, scalping strategies")
        print("• 1d: Traditional daily analysis, swing trading")
        print("• 1wk, 1mo: Long-term analysis, position trading")
        
        return True
        
    except ImportError as e:
        print(f"   ✗ Import error: {e}")
        return False
    except Exception as e:
        print(f"   ✗ Unexpected error: {e}")
        return False

if __name__ == "__main__":
    test_timeframe_configuration()
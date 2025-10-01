#!/usr/bin/env python3
"""
TradingAgents Tool Test Script

This script allows testing individual TradingAgents tools with custom parameters.
Usage: python test_tool.py <tool_name> <timeframe> <lookback_days> [symbol] [date]

Examples:
    python test_tool.py get_YFin_data_online 1h 7 AAPL 2024-10-01
    python test_tool.py get_stockstats_indicators_report 5m 30 MSFT 2024-10-01
    python test_tool.py get_finnhub_news 1d 7 TSLA 2024-10-01
"""

import sys
import argparse
from datetime import datetime, timedelta
import json
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def setup_config(timeframe, lookback_days):
    """Setup TradingAgents configuration with specified parameters."""
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.dataflows.config import set_config
        
        config = {
            "timeframe": timeframe,
            "market_history_days": lookback_days,
            "social_sentiment_days": min(lookback_days, 7),  # Social data usually limited to 7 days
            "news_lookback_days": min(lookback_days, 14),    # News usually limited to 14 days
            "economic_data_days": lookback_days
        }
        
        set_config(config)
        print(f"✓ Configuration set: timeframe={timeframe}, lookback={lookback_days} days")
        return True
        
    except Exception as e:
        print(f"✗ Failed to setup configuration: {e}")
        return False


def test_yfin_data_tool(symbol, date_str, timeframe, lookback_days):
    """Test get_YFin_data_online tool."""
    print(f"\n{'='*60}")
    print(f"TESTING: get_YFin_data_online")
    print(f"Symbol: {symbol}, Date: {date_str}, Timeframe: {timeframe}, Lookback: {lookback_days} days")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        # Calculate date range
        end_date = datetime.strptime(date_str, "%Y-%m-%d")
        start_date = end_date - timedelta(days=lookback_days)
        
        toolkit = Toolkit()
        
        result = toolkit.get_YFin_data_online.invoke({
            "symbol": symbol,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d")
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_stockstats_tool(symbol, date_str, timeframe, lookback_days, indicator="rsi", online=False):
    """Test get_stockstats_indicators_report tool."""
    tool_name = "get_stockstats_indicators_report_online" if online else "get_stockstats_indicators_report"
    
    print(f"\n{'='*60}")
    print(f"TESTING: {tool_name}")
    print(f"Symbol: {symbol}, Date: {date_str}, Indicator: {indicator}")
    print(f"Timeframe: {timeframe}, Lookback: {lookback_days} days, Online: {online}")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        toolkit = Toolkit()
        
        if online:
            tool = toolkit.get_stockstats_indicators_report_online
        else:
            tool = toolkit.get_stockstats_indicators_report
        
        result = tool.invoke({
            "symbol": symbol,
            "indicator": indicator,
            "curr_date": date_str,
            "look_back_days": lookback_days
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        if "No such file or directory" in str(e):
            print(f"  NOTE: This tool requires cached data files. Try using online=True mode")
            print(f"        or ensure market data cache is populated first.")
        import traceback
        traceback.print_exc()
        return False


def test_finnhub_news_tool(symbol, date_str, lookback_days):
    """Test get_finnhub_news tool."""
    print(f"\n{'='*60}")
    print(f"TESTING: get_finnhub_news")
    print(f"Symbol: {symbol}, Date: {date_str}, Lookback: {lookback_days} days")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        # Calculate date range
        end_date = datetime.strptime(date_str, "%Y-%m-%d")
        start_date = end_date - timedelta(days=lookback_days)
        
        toolkit = Toolkit()
        
        result = toolkit.get_finnhub_news.invoke({
            "ticker": symbol,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d")
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_reddit_news_tool(date_str):
    """Test get_reddit_news tool."""
    print(f"\n{'='*60}")
    print(f"TESTING: get_reddit_news")
    print(f"Date: {date_str}")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        toolkit = Toolkit()
        
        result = toolkit.get_reddit_news.invoke({
            "curr_date": date_str
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_insider_sentiment_tool(symbol, date_str):
    """Test get_finnhub_company_insider_sentiment tool."""
    print(f"\n{'='*60}")
    print(f"TESTING: get_finnhub_company_insider_sentiment")
    print(f"Symbol: {symbol}, Date: {date_str}")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        toolkit = Toolkit()
        
        result = toolkit.get_finnhub_company_insider_sentiment.invoke({
            "ticker": symbol,
            "curr_date": date_str
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_insider_transactions_tool(symbol, date_str):
    """Test get_finnhub_company_insider_transactions tool."""
    print(f"\n{'='*60}")
    print(f"TESTING: get_finnhub_company_insider_transactions")
    print(f"Symbol: {symbol}, Date: {date_str}")
    print(f"{'='*60}")
    
    try:
        from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils import Toolkit
        
        toolkit = Toolkit()
        
        result = toolkit.get_finnhub_company_insider_transactions.invoke({
            "ticker": symbol,
            "curr_date": date_str
        })
        
        print(f"✓ Tool execution successful")
        print(f"\nResult:")
        print("-" * 40)
        print(result)
        
        return True
        
    except Exception as e:
        print(f"✗ Tool execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def list_available_tools():
    """List all available tools for testing."""
    tools = {
        "get_YFin_data_online": "Retrieve stock price data from Yahoo Finance",
        "get_stockstats_indicators_report": "Get technical indicators analysis",
        "get_stockstats_indicators_report_online": "Get technical indicators analysis (online)",
        "get_finnhub_news": "Get company news from Finnhub",
        "get_reddit_news": "Get global news from Reddit",
        "get_finnhub_company_insider_sentiment": "Get insider sentiment data",
        "get_finnhub_company_insider_transactions": "Get insider transaction data"
    }
    
    print("\nAvailable Tools:")
    print("=" * 50)
    for tool, description in tools.items():
        print(f"• {tool}")
        print(f"  {description}")
    
    print(f"\nSupported Timeframes:")
    print("• 1m, 5m, 15m, 30m (intraday)")
    print("• 1h (hourly)")
    print("• 1d (daily)")
    print("• 1wk, 1mo (weekly, monthly)")


def main():
    """Main function to handle command line arguments and execute tests."""
    parser = argparse.ArgumentParser(
        description="Test TradingAgents tools with custom parameters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_tool.py get_YFin_data_online 1h 7 AAPL 2024-10-01
  python test_tool.py get_stockstats_indicators_report 5m 30 MSFT 2024-10-01
  python test_tool.py get_finnhub_news 1d 7 TSLA 2024-10-01
  python test_tool.py get_reddit_news 1d 3 - 2024-10-01
  python test_tool.py --list-tools
        """
    )
    
    parser.add_argument("tool_name", nargs="?", help="Name of the tool to test")
    parser.add_argument("timeframe", nargs="?", help="Timeframe (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo)")
    parser.add_argument("lookback_days", nargs="?", type=int, help="Number of lookback days")
    parser.add_argument("symbol", nargs="?", default="AAPL", help="Stock symbol (default: AAPL)")
    parser.add_argument("date", nargs="?", help="Date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--list-tools", action="store_true", help="List available tools")
    parser.add_argument("--indicator", default="rsi", help="Technical indicator for stockstats tools (default: rsi)")
    parser.add_argument("--online", action="store_true", help="Use online mode for stockstats tools")
    
    args = parser.parse_args()
    
    # Handle list tools option
    if args.list_tools:
        list_available_tools()
        return
    
    # Validate required arguments
    if not args.tool_name or not args.timeframe or args.lookback_days is None:
        parser.print_help()
        print("\nError: tool_name, timeframe, and lookback_days are required")
        return
    
    # Set default date if not provided
    if not args.date:
        args.date = datetime.now().strftime("%Y-%m-%d")
    
    # Validate timeframe
    valid_timeframes = ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"]
    if args.timeframe not in valid_timeframes:
        print(f"Error: Invalid timeframe '{args.timeframe}'. Valid options: {valid_timeframes}")
        return
    
    # Validate date format
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"Error: Invalid date format '{args.date}'. Use YYYY-MM-DD format.")
        return
    
    print(f"TradingAgents Tool Test")
    print(f"Tool: {args.tool_name}")
    print(f"Timeframe: {args.timeframe}")
    print(f"Lookback Days: {args.lookback_days}")
    print(f"Symbol: {args.symbol}")
    print(f"Date: {args.date}")
    
    # Setup configuration
    if not setup_config(args.timeframe, args.lookback_days):
        return
    
    # Execute the appropriate test
    success = False
    
    if args.tool_name == "get_YFin_data_online":
        success = test_yfin_data_tool(args.symbol, args.date, args.timeframe, args.lookback_days)
        
    elif args.tool_name in ["get_stockstats_indicators_report", "get_stockstats_indicators_report_online"]:
        online_mode = args.online or args.tool_name == "get_stockstats_indicators_report_online"
        success = test_stockstats_tool(args.symbol, args.date, args.timeframe, args.lookback_days, args.indicator, online_mode)
        
    elif args.tool_name == "get_finnhub_news":
        success = test_finnhub_news_tool(args.symbol, args.date, args.lookback_days)
        
    elif args.tool_name == "get_reddit_news":
        success = test_reddit_news_tool(args.date)
        
    elif args.tool_name == "get_finnhub_company_insider_sentiment":
        success = test_insider_sentiment_tool(args.symbol, args.date)
        
    elif args.tool_name == "get_finnhub_company_insider_transactions":
        success = test_insider_transactions_tool(args.symbol, args.date)
        
    else:
        print(f"Error: Unknown tool '{args.tool_name}'")
        list_available_tools()
        return
    
    # Print final result
    print(f"\n{'='*60}")
    if success:
        print(f"✓ TEST COMPLETED SUCCESSFULLY")
    else:
        print(f"✗ TEST FAILED")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

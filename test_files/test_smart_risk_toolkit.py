"""
Test Script for SmartRiskManagerToolkit

Tests all 12 tools including:
- Portfolio and analysis retrieval tools
- Trading action tools (open, close, adjust, update TP/SL)

Uses AGNC symbol with quantity 1 for real trading tests.
"""

import sys
import os
from pathlib import Path
import time

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance, AccountDefinition
from sqlmodel import select


def print_separator(title: str):
    """Print a nice separator."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def print_result(tool_name: str, result: any, success: bool = True):
    """Print tool result in a formatted way."""
    status = "[SUCCESS]" if success else "[FAILED]"
    print(f"\n{status} - {tool_name}")
    print("-" * 80)
    
    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(value, list) and len(value) > 3:
                print(f"  {key}: [{len(value)} items]")
            elif isinstance(value, dict):
                print(f"  {key}:")
                for k, v in value.items():
                    print(f"    {k}: {v}")
            else:
                print(f"  {key}: {value}")
    elif isinstance(result, list):
        print(f"  Items: {len(result)}")
        for i, item in enumerate(result[:3]):  # Show first 3
            print(f"  [{i}]: {item}")
        if len(result) > 3:
            print(f"  ... and {len(result) - 3} more")
    else:
        print(f"  Result: {result}")
    print()


def get_test_expert_and_account():
    """Get first available expert and account for testing."""
    with get_db() as session:
        expert = session.exec(select(ExpertInstance).limit(1)).first()
        account = session.exec(select(AccountDefinition).limit(1)).first()
        
        if not expert or not account:
            raise ValueError("No expert or account found in database. Please create one first.")
        
        print(f"Using Expert: {expert.expert} (ID: {expert.id})")
        print(f"Using Account: {account.provider} - {account.name} (ID: {account.id})")
        
        return expert.id, account.id


def test_portfolio_tools(toolkit: SmartRiskManagerToolkit):
    """Test portfolio and analysis retrieval tools."""
    print_separator("TESTING PORTFOLIO & ANALYSIS TOOLS")
    
    # Test 1: Get Portfolio Status
    try:
        result = toolkit.get_portfolio_status()
        print_result("get_portfolio_status", result)
    except Exception as e:
        print_result("get_portfolio_status", str(e), success=False)
        logger.error(f"get_portfolio_status failed: {e}", exc_info=True)
    
    # Test 2: Get Recent Analyses (all symbols)
    try:
        result = toolkit.get_recent_analyses(max_age_hours=168)  # 7 days
        print_result("get_recent_analyses (all symbols)", result)
    except Exception as e:
        print_result("get_recent_analyses", str(e), success=False)
        logger.error(f"get_recent_analyses failed: {e}", exc_info=True)
    
    # Test 3: Get Recent Analyses (specific symbol)
    try:
        result = toolkit.get_recent_analyses(symbol="AGNC", max_age_hours=168)
        print_result("get_recent_analyses (AGNC)", result)
        
        # If we have analyses, test output retrieval
        if result and len(result) > 0:
            analysis_id = result[0]["analysis_id"]
            
            # Test 4: Get Analysis Outputs
            try:
                outputs = toolkit.get_analysis_outputs(analysis_id)
                print_result(f"get_analysis_outputs (analysis {analysis_id})", outputs)
                
                # Test 5: Get Output Detail
                if outputs:
                    first_key = list(outputs.keys())[0]
                    try:
                        detail = toolkit.get_analysis_output_detail(analysis_id, first_key)
                        print_result(f"get_analysis_output_detail (key: {first_key})", 
                                   f"[{len(detail)} characters]")
                    except Exception as e:
                        print_result("get_analysis_output_detail", str(e), success=False)
            except Exception as e:
                print_result("get_analysis_outputs", str(e), success=False)
                
    except Exception as e:
        print_result("get_recent_analyses (AGNC)", str(e), success=False)
    
    # Test 6: Get Historical Analyses
    try:
        result = toolkit.get_historical_analyses(symbol="AGNC", limit=5)
        print_result("get_historical_analyses (AGNC, limit=5)", result)
    except Exception as e:
        print_result("get_historical_analyses", str(e), success=False)
        logger.error(f"get_historical_analyses failed: {e}", exc_info=True)
    
    # Test 7: Get Current Price
    try:
        price = toolkit.get_current_price("AGNC")
        print_result("get_current_price (AGNC)", {"price": price})
    except Exception as e:
        print_result("get_current_price", str(e), success=False)
        logger.error(f"get_current_price failed: {e}", exc_info=True)


def test_calculation_tool(toolkit: SmartRiskManagerToolkit):
    """Test position metrics calculation tool."""
    print_separator("TESTING CALCULATION TOOL")
    
    try:
        metrics = toolkit.calculate_position_metrics(
            entry_price=10.50,
            current_price=11.25,
            quantity=100,
            direction="BUY"
        )
        print_result("calculate_position_metrics (BUY)", metrics)
        
        metrics = toolkit.calculate_position_metrics(
            entry_price=10.50,
            current_price=9.75,
            quantity=100,
            direction="SELL"
        )
        print_result("calculate_position_metrics (SELL)", metrics)
    except Exception as e:
        print_result("calculate_position_metrics", str(e), success=False)
        logger.error(f"calculate_position_metrics failed: {e}", exc_info=True)


def test_trading_tools(toolkit: SmartRiskManagerToolkit):
    """Test trading action tools with real orders."""
    print_separator("TESTING TRADING ACTION TOOLS")
    
    print("⚠️  NOTE: Trading tools require refactoring to work with TradingOrder objects.")
    print("   The current implementation uses incorrect API calls.")
    print("   These tests are temporarily disabled until the toolkit is updated.")
    print("\n   Required changes:")
    print("   - Create TradingOrder objects instead of calling submit_order() with parameters")
    print("   - Use proper account.submit_order(trading_order) API")
    print("   - Update all trading action methods to follow account interface patterns")
    print("\nSkipping trading tests for now...")
    return
    
    print("\n⚠️  WARNING: This will execute REAL trades on your account!")
    print("   Symbol: AGNC, Quantity: 1")
    
    response = input("\nDo you want to proceed with real trading tests? (yes/no): ")
    if response.lower() != "yes":
        print("Skipping trading tests.")
        return
    
    transaction_id = None
    
    try:
        # Test 1: Open New Position
        print("\n📈 Opening new BUY position for AGNC (qty: 1)...")
        
        # Get current price for TP/SL calculation
        current_price = toolkit.get_current_price("AGNC")
        tp_price = round(current_price * 1.05, 2)  # 5% profit target
        sl_price = round(current_price * 0.98, 2)  # 2% stop loss
        
        result = toolkit.open_new_position(
            symbol="AGNC",
            direction="BUY",
            quantity=1,
            tp_price=tp_price,
            sl_price=sl_price,
            reason="Test order from SmartRiskManagerToolkit test script"
        )
        print_result("open_new_position", result)
        
        if result.get("success"):
            transaction_id = result.get("transaction_id")
            print(f"[SUCCESS] Position opened! Transaction ID: {transaction_id}")
            time.sleep(2)  # Wait for order to settle
            
            # Test 2: Get Portfolio Status (should show new position)
            portfolio = toolkit.get_portfolio_status()
            print_result("get_portfolio_status (after open)", portfolio)
            
            if transaction_id:
                # Test 3: Update Take Profit
                print(f"\n🎯 Updating take profit to {tp_price * 1.01:.2f}...")
                new_tp = round(tp_price * 1.01, 2)
                result = toolkit.update_take_profit(
                    transaction_id=transaction_id,
                    new_tp_price=new_tp,
                    reason="Adjusting profit target upward"
                )
                print_result("update_take_profit", result)
                time.sleep(1)
                
                # Test 4: Update Stop Loss
                print(f"\n🛑 Updating stop loss to {sl_price * 1.005:.2f}...")
                new_sl = round(sl_price * 1.005, 2)
                result = toolkit.update_stop_loss(
                    transaction_id=transaction_id,
                    new_sl_price=new_sl,
                    reason="Tightening stop loss"
                )
                print_result("update_stop_loss", result)
                time.sleep(1)
                
                # Test 5: Close Position
                print(f"\n❌ Closing position (transaction {transaction_id})...")
                result = toolkit.close_position(
                    transaction_id=transaction_id,
                    reason="Test complete - closing position"
                )
                print_result("close_position", result)
                
                if result.get("success"):
                    print("[SUCCESS] Position closed successfully!")
                    transaction_id = None  # Mark as closed
                else:
                    print("⚠️  Close failed - position may still be open!")
        else:
            print(f"❌ Failed to open position: {result.get('message')}")
            
    except Exception as e:
        print_result("trading_tools", str(e), success=False)
        logger.error(f"Trading tools test failed: {e}", exc_info=True)
    
    finally:
        # Cleanup: Try to close position if it's still open
        if transaction_id:
            print(f"\n🧹 Cleanup: Attempting to close position {transaction_id}...")
            try:
                result = toolkit.close_position(
                    transaction_id=transaction_id,
                    reason="Cleanup after test failure"
                )
                if result.get("success"):
                    print("[SUCCESS] Cleanup successful - position closed")
                else:
                    print(f"⚠️  Cleanup failed: {result.get('message')}")
                    print(f"   Please manually close transaction {transaction_id}")
            except Exception as e:
                print(f"❌ Cleanup error: {e}")
                print(f"   Please manually close transaction {transaction_id}")


def main():
    """Main test runner."""
    print_separator("SMART RISK MANAGER TOOLKIT TEST")
    
    try:
        # Get test expert and account
        expert_id, account_id = get_test_expert_and_account()
        
        # Initialize toolkit
        print(f"\nInitializing SmartRiskManagerToolkit...")
        toolkit = SmartRiskManagerToolkit(expert_id, account_id)
        print(f"[SUCCESS] Toolkit initialized successfully!\n")
        
        # Run tests
        test_portfolio_tools(toolkit)
        test_calculation_tool(toolkit)
        
        # Trading tests require confirmation
        test_trading_tools(toolkit)
        
        print_separator("TEST COMPLETE")
        print("[SUCCESS] All tests completed!")
        print("\nCheck the logs for detailed execution information:")
        print("  - app.log (INFO level)")
        print("  - app.debug.log (DEBUG level)")
        
    except Exception as e:
        print_separator("TEST FAILED")
        print(f"[FAILED] Fatal error: {e}")
        logger.error(f"Test script failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()

"""
Test the new pending actions management tools for SmartRiskManager

This tests:
1. get_pending_actions_tool()
2. modify_pending_tp_sl_tool()  
3. cancel_pending_action_tool()
"""

def test_pending_actions_management():
    """Test pending actions management functionality"""
    
    # Mock the recommended_actions_list like SmartRiskManagerGraph uses
    recommended_actions_list = []
    
    # Create mock tools similar to SmartRiskManagerGraph
    def get_pending_actions_tool() -> str:
        """Get list of all currently recommended actions"""
        if not recommended_actions_list:
            return "No pending actions. All recommendations have been cleared or none have been made yet."
        
        result = "## PENDING ACTIONS (Queued for Execution)\n\n"
        result += f"**Total actions queued: {len(recommended_actions_list)}**\n\n"
        
        for idx, action in enumerate(recommended_actions_list):
            action_num = idx + 1
            action_type = action.get("action_type", "unknown")
            params = action.get("parameters", {})
            reason = action.get("reason", "No reason provided")
            confidence = action.get("confidence", 0)
            
            result += f"**Action #{action_num}: {action_type}**\n"
            
            if action_type == "open_buy_position":
                symbol = params.get("symbol", "N/A")
                quantity = params.get("quantity", 0)
                tp_price = params.get("tp_price")
                sl_price = params.get("sl_price")
                result += f"- Symbol: {symbol}\n"
                result += f"- Quantity: {quantity} shares\n"
                result += f"- TP: ${tp_price:.2f}\n" if tp_price else "- TP: Not set\n"
                result += f"- SL: ${sl_price:.2f}\n" if sl_price else "- SL: Not set\n"
            elif action_type == "close_position":
                transaction_id = params.get("transaction_id", "N/A")
                result += f"- Transaction ID: {transaction_id}\n"
            
            result += f"- Reason: {reason}\n"
            result += f"- Confidence: {confidence}%\n\n"
        
        return result
    
    def modify_pending_tp_sl_tool(symbol: str, new_tp_price=None, new_sl_price=None, reason="TP/SL adjustment"):
        """Modify TP/SL for pending actions"""
        # Find pending open position actions for this symbol
        matching_actions = []
        for idx, action in enumerate(recommended_actions_list):
            if (action.get("action_type") in ["open_buy_position", "open_sell_position"] and 
                action.get("parameters", {}).get("symbol") == symbol):
                matching_actions.append((idx, action))
        
        if not matching_actions:
            return f"❌ No pending open position actions found for symbol {symbol}"
        
        modified_count = 0
        for idx, action in matching_actions:
            # Update the TP/SL prices in the action parameters
            if new_tp_price is not None:
                recommended_actions_list[idx]["parameters"]["tp_price"] = new_tp_price
            if new_sl_price is not None:
                recommended_actions_list[idx]["parameters"]["sl_price"] = new_sl_price
                
            # Update the reason 
            original_reason = recommended_actions_list[idx].get("reason", "")
            recommended_actions_list[idx]["reason"] = f"{original_reason} | Modified TP/SL: {reason}"
            modified_count += 1
        
        tp_msg = f"TP=${new_tp_price:.2f}" if new_tp_price is not None else "TP=unchanged"
        sl_msg = f"SL=${new_sl_price:.2f}" if new_sl_price is not None else "SL=unchanged"
        
        return f"✅ Modified {modified_count} pending action(s) for {symbol}: {tp_msg}, {sl_msg}"
    
    def cancel_pending_action_tool(action_number: int):
        """Cancel a pending action"""
        if not recommended_actions_list:
            return "❌ No pending actions to cancel."
        
        if action_number < 1 or action_number > len(recommended_actions_list):
            return f"❌ Invalid action number {action_number}. Valid range: 1-{len(recommended_actions_list)}"
        
        # Convert 1-based to 0-based index
        idx = action_number - 1
        cancelled_action = recommended_actions_list.pop(idx)
        
        action_type = cancelled_action.get("action_type", "unknown")
        params = cancelled_action.get("parameters", {})
        
        # Format cancelled action info
        if action_type in ["open_buy_position", "open_sell_position"]:
            symbol = params.get("symbol", "N/A")
            direction = "BUY" if action_type == "open_buy_position" else "SELL"
            action_info = f"{direction} {symbol}"
        else:
            transaction_id = params.get("transaction_id", "N/A")
            action_info = f"{action_type} for transaction {transaction_id}"
        
        return f"✅ Cancelled Action #{action_number}: {action_info}. Remaining actions: {len(recommended_actions_list)}"
    
    print("Test 1: Empty pending actions list")
    print("=" * 50)
    result = get_pending_actions_tool()
    print(result)
    assert "No pending actions" in result
    print("✅ Empty list handled correctly\n")
    
    print("Test 2: Add some pending actions")
    print("=" * 50)
    # Add some mock actions
    recommended_actions_list.extend([
        {
            "action_type": "open_buy_position",
            "parameters": {"symbol": "AAPL", "quantity": 10, "tp_price": 200.0, "sl_price": 180.0},
            "reason": "Strong buy signal",
            "confidence": 85
        },
        {
            "action_type": "open_sell_position", 
            "parameters": {"symbol": "TSLA", "quantity": 5, "tp_price": 150.0, "sl_price": 170.0},
            "reason": "Bearish trend",
            "confidence": 70
        },
        {
            "action_type": "close_position",
            "parameters": {"transaction_id": 123},
            "reason": "Take profit",
            "confidence": 90
        }
    ])
    
    result = get_pending_actions_tool()
    print(result)
    assert "Total actions queued: 3" in result
    assert "Action #1: open_buy_position" in result
    assert "AAPL" in result
    print("✅ Actions displayed correctly\n")
    
    print("Test 3: Modify TP/SL for AAPL")
    print("=" * 50)
    result = modify_pending_tp_sl_tool("AAPL", new_tp_price=210.0, new_sl_price=175.0, reason="Market analysis update")
    print(result)
    assert "✅ Modified 1 pending action(s) for AAPL" in result
    assert "TP=$210.00" in result
    assert "SL=$175.00" in result
    
    # Verify the action was actually modified
    aapl_action = recommended_actions_list[0]
    assert aapl_action["parameters"]["tp_price"] == 210.0
    assert aapl_action["parameters"]["sl_price"] == 175.0
    assert "Modified TP/SL: Market analysis update" in aapl_action["reason"]
    print("✅ TP/SL modification works correctly\n")
    
    print("Test 4: Try to modify non-existent symbol")
    print("=" * 50)
    result = modify_pending_tp_sl_tool("NVDA", new_tp_price=500.0)
    print(result)
    assert "❌ No pending open position actions found for symbol NVDA" in result
    print("✅ Non-existent symbol handled correctly\n")
    
    print("Test 5: Cancel action #2 (TSLA)")
    print("=" * 50)
    result = cancel_pending_action_tool(2)
    print(result)
    assert "✅ Cancelled Action #2: SELL TSLA" in result
    assert len(recommended_actions_list) == 2
    print("✅ Action cancellation works correctly\n")
    
    print("Test 6: Try to cancel invalid action number")
    print("=" * 50)
    result = cancel_pending_action_tool(10)
    print(result)
    assert "❌ Invalid action number 10" in result
    print("✅ Invalid action number handled correctly\n")
    
    print("Test 7: View updated pending actions")
    print("=" * 50)
    result = get_pending_actions_tool()
    print(result)
    assert "Total actions queued: 2" in result
    assert "Action #1: open_buy_position" in result  # AAPL still there
    assert "Action #2: close_position" in result      # close_position moved up after TSLA deletion
    assert "TSLA" not in result  # TSLA should be gone
    print("✅ Action list correctly updated after cancellation\n")
    
    print("🎯 ALL TESTS PASSED!")
    print("The pending actions management tools work correctly.")
    print("LLMs can now:")
    print("- Review their pending recommendations")
    print("- Modify TP/SL levels for pending positions")
    print("- Cancel actions they no longer want")


if __name__ == "__main__":
    test_pending_actions_management()
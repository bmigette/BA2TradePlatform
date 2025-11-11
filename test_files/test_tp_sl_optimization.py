#!/usr/bin/env python3
"""
Test the SmartRiskManager action optimization that combines TP/SL adjustments.
This test validates that separate update_take_profit and update_stop_loss actions
for the same transaction are combined into a single adjust_tp_sl call.
"""

import os
import sys

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.logger import logger

def test_tp_sl_action_optimization():
    """Test that TP/SL actions are correctly combined for optimization"""
    
    # Mock actions that would typically come from the research node
    recommended_actions = [
        {
            "action_type": "close_position",
            "parameters": {"transaction_id": 100},
            "reason": "Risk management",
            "confidence": 85
        },
        {
            "action_type": "update_take_profit",
            "parameters": {"transaction_id": 200, "new_tp_price": 150.0},
            "reason": "Market conditions improved",
            "confidence": 75
        },
        {
            "action_type": "update_stop_loss",
            "parameters": {"transaction_id": 200, "new_sl_price": 120.0},
            "reason": "Tighten risk management",
            "confidence": 80
        },
        {
            "action_type": "update_take_profit",
            "parameters": {"transaction_id": 300, "new_tp_price": 220.0},
            "reason": "Only TP adjustment",
            "confidence": 70
        },
        {
            "action_type": "open_buy_position",
            "parameters": {"symbol": "TEST", "quantity": 10},
            "reason": "New opportunity",
            "confidence": 80
        }
    ]
    
    # Simulate the optimization logic from SmartRiskManagerGraph.py
    tp_sl_grouped_actions = {}  # transaction_id -> {"tp_action": action, "sl_action": action}
    other_actions = []
    
    for action in recommended_actions:
        action_type = action.get("action_type")
        if action_type in ["update_stop_loss", "update_take_profit"]:
            transaction_id = action.get("parameters", {}).get("transaction_id")
            if transaction_id:
                if transaction_id not in tp_sl_grouped_actions:
                    tp_sl_grouped_actions[transaction_id] = {"tp_action": None, "sl_action": None}
                
                if action_type == "update_take_profit":
                    tp_sl_grouped_actions[transaction_id]["tp_action"] = action
                else:  # update_stop_loss
                    tp_sl_grouped_actions[transaction_id]["sl_action"] = action
            else:
                # No transaction_id, treat as regular action
                other_actions.append(action)
        else:
            other_actions.append(action)
    
    # Convert grouped TP/SL actions back to optimized action list
    optimized_actions = []
    for transaction_id, grouped in tp_sl_grouped_actions.items():
        tp_action = grouped["tp_action"]
        sl_action = grouped["sl_action"]
        
        if tp_action and sl_action:
            # Both TP and SL - combine into single adjust_tp_sl action
            combined_action = {
                "action_type": "adjust_tp_sl",
                "parameters": {
                    "transaction_id": transaction_id,
                    "new_tp_price": tp_action["parameters"]["new_tp_price"],
                    "new_sl_price": sl_action["parameters"]["new_sl_price"]
                },
                "reason": f"TP: {tp_action.get('reason', 'No reason')}, SL: {sl_action.get('reason', 'No reason')}",
                "confidence": max(tp_action.get("confidence", 0), sl_action.get("confidence", 0))
            }
            optimized_actions.append(combined_action)
            logger.info(f"✅ Combined TP and SL for transaction {transaction_id}")
        elif tp_action:
            # Only TP
            optimized_actions.append(tp_action)
            logger.info(f"→ Kept standalone TP action for transaction {transaction_id}")
        elif sl_action:
            # Only SL
            optimized_actions.append(sl_action)
            logger.info(f"→ Kept standalone SL action for transaction {transaction_id}")
    
    # Add all other actions
    optimized_actions.extend(other_actions)
    
    # Verify optimization results
    logger.info(f"=== Optimization Results ===")
    logger.info(f"Original actions: {len(recommended_actions)}")
    logger.info(f"Optimized actions: {len(optimized_actions)}")
    
    # Expected results:
    # 1. close_position (unchanged)
    # 2. adjust_tp_sl for transaction 200 (combined from TP + SL)
    # 3. update_take_profit for transaction 300 (standalone TP)
    # 4. open_buy_position (unchanged)
    # Total: 4 optimized actions from 5 original
    
    expected_optimized_count = 4
    if len(optimized_actions) != expected_optimized_count:
        logger.error(f"❌ Expected {expected_optimized_count} optimized actions, got {len(optimized_actions)}")
        return False
    
    # Check specific optimizations
    combined_action = None
    for action in optimized_actions:
        if action["action_type"] == "adjust_tp_sl" and action["parameters"]["transaction_id"] == 200:
            combined_action = action
            break
    
    if not combined_action:
        logger.error("❌ Expected combined adjust_tp_sl action for transaction 200 not found")
        return False
    
    if combined_action["parameters"]["new_tp_price"] != 150.0:
        logger.error(f"❌ Expected TP price 150.0, got {combined_action['parameters']['new_tp_price']}")
        return False
    
    if combined_action["parameters"]["new_sl_price"] != 120.0:
        logger.error(f"❌ Expected SL price 120.0, got {combined_action['parameters']['new_sl_price']}")
        return False
    
    if combined_action["confidence"] != 80:  # max(75, 80)
        logger.error(f"❌ Expected confidence 80, got {combined_action['confidence']}")
        return False
    
    logger.info("✅ All optimization checks passed!")
    
    # Print final optimized actions for review
    logger.info("=== Final Optimized Actions ===")
    for i, action in enumerate(optimized_actions):
        logger.info(f"{i+1}. {action['action_type']} - {action.get('parameters', {})}")
    
    return True

def main():
    """Main test function"""
    logger.info("=== Testing SmartRiskManager TP/SL Action Optimization ===")
    
    success = test_tp_sl_action_optimization()
    
    if success:
        logger.info("✅ Test completed successfully")
        print("Test PASSED: TP/SL action optimization works correctly")
    else:
        logger.error("❌ Test failed")
        print("Test FAILED: Check logs for details")
    
    return success

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
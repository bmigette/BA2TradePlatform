"""
Documentation and tooltips for rules system (Event Triggers and Actions).

This module provides comprehensive documentation for:
- ExpertEventType: Conditions/triggers that can be checked
- ExpertActionType: Actions that can be taken when conditions are met
"""

from .types import ExpertEventType, ExpertActionType


def get_event_type_documentation() -> dict:
    """
    Get comprehensive documentation for all ExpertEventType values.
    
    Returns:
        Dictionary mapping event type values to their documentation.
    """
    return {
        # Boolean/Flag Events (F_ prefix)
        ExpertEventType.F_BEARISH.value: {
            "name": "Bearish Market Sentiment",
            "description": "Triggers when the expert's analysis indicates bearish (negative/downward) market sentiment for the symbol.",
            "type": "boolean",
            "example": "Use with SELL or CLOSE actions when market turns bearish"
        },
        ExpertEventType.F_BULLISH.value: {
            "name": "Bullish Market Sentiment",
            "description": "Triggers when the expert's analysis indicates bullish (positive/upward) market sentiment for the symbol.",
            "type": "boolean",
            "example": "Use with BUY actions when market turns bullish"
        },
        ExpertEventType.F_HAS_NO_POSITION.value: {
            "name": "No Position Exists",
            "description": "Triggers when there is NO open position for this symbol and expert combination.",
            "type": "boolean",
            "example": "Useful for enter_market rules to prevent duplicate entries"
        },
        ExpertEventType.F_HAS_POSITION.value: {
            "name": "Position Exists",
            "description": "Triggers when there IS an open position for this symbol and expert combination.",
            "type": "boolean",
            "example": "Useful for open_positions rules to manage existing holdings"
        },
        
        # Rating Change Events
        ExpertEventType.F_RATING_NEGATIVE_TO_NEUTRAL.value: {
            "name": "Rating: Negative → Neutral",
            "description": "Triggers when the expert's rating changes from negative (SELL) to neutral (HOLD).",
            "type": "boolean",
            "example": "May indicate a selling opportunity is weakening"
        },
        ExpertEventType.F_RATING_NEGATIVE_TO_POSITIVE.value: {
            "name": "Rating: Negative → Positive",
            "description": "Triggers when the expert's rating changes from negative (SELL) to positive (BUY).",
            "type": "boolean",
            "example": "Strong reversal signal - consider closing shorts or entering long"
        },
        ExpertEventType.F_RATING_NEUTRAL_TO_NEGATIVE.value: {
            "name": "Rating: Neutral → Negative",
            "description": "Triggers when the expert's rating changes from neutral (HOLD) to negative (SELL).",
            "type": "boolean",
            "example": "Weakening signal - consider defensive actions"
        },
        ExpertEventType.F_RATING_NEUTRAL_TO_POSITIVE.value: {
            "name": "Rating: Neutral → Positive",
            "description": "Triggers when the expert's rating changes from neutral (HOLD) to positive (BUY).",
            "type": "boolean",
            "example": "Strengthening signal - consider entering position"
        },
        ExpertEventType.F_RATING_POSITIVE_TO_NEGATIVE.value: {
            "name": "Rating: Positive → Negative",
            "description": "Triggers when the expert's rating changes from positive (BUY) to negative (SELL).",
            "type": "boolean",
            "example": "Major reversal - consider closing longs immediately"
        },
        ExpertEventType.F_RATING_POSITIVE_TO_NEUTRAL.value: {
            "name": "Rating: Positive → Neutral",
            "description": "Triggers when the expert's rating changes from positive (BUY) to neutral (HOLD).",
            "type": "boolean",
            "example": "Buy signal weakening - consider taking profits"
        },
        
        # Current Rating States
        ExpertEventType.F_CURRENT_RATING_POSITIVE.value: {
            "name": "Current Rating is Positive",
            "description": "Triggers when the expert's current rating is BUY (positive).",
            "type": "boolean",
            "example": "Filter to only act when expert maintains bullish view"
        },
        ExpertEventType.F_CURRENT_RATING_NEUTRAL.value: {
            "name": "Current Rating is Neutral",
            "description": "Triggers when the expert's current rating is HOLD (neutral).",
            "type": "boolean",
            "example": "Filter to only act during neutral market conditions"
        },
        ExpertEventType.F_CURRENT_RATING_NEGATIVE.value: {
            "name": "Current Rating is Negative",
            "description": "Triggers when the expert's current rating is SELL (negative).",
            "type": "boolean",
            "example": "Filter to only act when expert maintains bearish view"
        },
        
        # Time Horizon Flags
        ExpertEventType.F_SHORT_TERM.value: {
            "name": "Short-Term Investment Horizon",
            "description": "Triggers when the expert recommendation has a SHORT_TERM time horizon (days to weeks).",
            "type": "boolean",
            "example": "Filter for quick trades vs. long-term holds"
        },
        ExpertEventType.F_MEDIUM_TERM.value: {
            "name": "Medium-Term Investment Horizon",
            "description": "Triggers when the expert recommendation has a MEDIUM_TERM time horizon (weeks to months).",
            "type": "boolean",
            "example": "Filter for swing trading opportunities"
        },
        ExpertEventType.F_LONG_TERM.value: {
            "name": "Long-Term Investment Horizon",
            "description": "Triggers when the expert recommendation has a LONG_TERM time horizon (months to years).",
            "type": "boolean",
            "example": "Filter for position/buy-and-hold investments"
        },
        
        # Risk Level Flags
        ExpertEventType.F_HIGHRISK.value: {
            "name": "High Risk Level",
            "description": "Triggers when the expert rates the recommendation as HIGH risk.",
            "type": "boolean",
            "example": "Filter to avoid high-risk opportunities or allocate smaller positions"
        },
        ExpertEventType.F_MEDIUMRISK.value: {
            "name": "Medium Risk Level",
            "description": "Triggers when the expert rates the recommendation as MEDIUM risk.",
            "type": "boolean",
            "example": "Balanced risk/reward opportunities"
        },
        ExpertEventType.F_LOWRISK.value: {
            "name": "Low Risk Level",
            "description": "Triggers when the expert rates the recommendation as LOW risk.",
            "type": "boolean",
            "example": "Conservative, safer investment opportunities"
        },
        
        # Numeric Events (N_ prefix)
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value: {
            "name": "Expected Profit Target %",
            "description": "The expert's expected profit percentage for this recommendation. Used with numeric comparisons (>, <, >=, <=, ==).",
            "type": "numeric",
            "example": "Trigger when expected profit >= 10% for high-conviction trades"
        },
        ExpertEventType.N_PERCENT_TO_TARGET.value: {
            "name": "Percent to Price Target",
            "description": "For open positions: percentage distance from current price to the expert's target price. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Close position when percent_to_target <= 5% (near target)"
        },
        ExpertEventType.N_PROFIT_LOSS_AMOUNT.value: {
            "name": "Profit/Loss Amount",
            "description": "For open positions: absolute dollar profit or loss. Positive values = profit, negative = loss. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Take profits when profit_loss_amount >= $1000"
        },
        ExpertEventType.N_PROFIT_LOSS_PERCENT.value: {
            "name": "Profit/Loss Percentage",
            "description": "For open positions: percentage profit or loss relative to entry price. Positive values = profit, negative = loss. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Stop loss when profit_loss_percent <= -10%"
        },
        ExpertEventType.N_DAYS_OPENED.value: {
            "name": "Days Position Open",
            "description": "For open positions: number of calendar days since the position was opened. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Review positions when days_opened >= 90 for rebalancing"
        },
        ExpertEventType.N_CONFIDENCE.value: {
            "name": "Confidence Score",
            "description": "The expert's confidence level in this recommendation, typically 0.0-1.0 (0-100%). Higher = more confident. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Only enter trades when confidence >= 0.75 (75%)"
        }
    }


def get_action_type_documentation() -> dict:
    """
    Get comprehensive documentation for all ExpertActionType values.
    
    Returns:
        Dictionary mapping action type values to their documentation.
    """
    return {
        ExpertActionType.SELL.value: {
            "name": "SELL",
            "description": "Create a SELL order for the symbol. Can be used to open a short position or close a long position.",
            "use_cases": [
                "Enter a short position when bearish signals detected",
                "Close an existing long position to take profits or cut losses",
                "Exit the market on negative sentiment change"
            ],
            "parameters": "Typically combined with quantity and order type settings",
            "example": "When rating changes to NEGATIVE and confidence > 70%, action: SELL"
        },
        ExpertActionType.BUY.value: {
            "name": "BUY",
            "description": "Create a BUY order for the symbol. Can be used to open a long position or close a short position.",
            "use_cases": [
                "Enter a long position when bullish signals detected",
                "Close an existing short position to take profits or cut losses",
                "Enter the market on positive sentiment change"
            ],
            "parameters": "Typically combined with quantity and order type settings",
            "example": "When rating is POSITIVE and confidence > 75% and no_position, action: BUY"
        },
        ExpertActionType.CLOSE.value: {
            "name": "CLOSE",
            "description": "Close the existing open position for this symbol, regardless of whether it's long or short. This is a convenience action that automatically determines the correct side.",
            "use_cases": [
                "Exit position when target price reached",
                "Close position when rating changes to NEUTRAL",
                "Close position after holding for maximum time period",
                "Emergency exit on major rating reversals"
            ],
            "parameters": "No additional parameters needed - automatically closes the position",
            "example": "When percent_to_target <= 2% or days_opened >= 180, action: CLOSE"
        },
        ExpertActionType.ADJUST_TAKE_PROFIT.value: {
            "name": "Adjust Take Profit",
            "description": "Modify the take-profit price for an existing open position. Used to lock in gains or adjust profit targets based on changing market conditions.",
            "use_cases": [
                "Raise take-profit target when price moves favorably (trailing profit)",
                "Lower take-profit target when volatility increases",
                "Set take-profit based on updated expert price targets"
            ],
            "parameters": "Requires reference value (order_open_price, current_price, expert_target_price) and percentage/amount adjustment",
            "example": "When profit_loss_percent >= 15%, adjust_take_profit to current_price + 5%"
        },
        ExpertActionType.ADJUST_STOP_LOSS.value: {
            "name": "Adjust Stop Loss",
            "description": "Modify the stop-loss price for an existing open position. Used to protect profits or limit losses based on price movement and market conditions.",
            "use_cases": [
                "Raise stop-loss as price moves up (trailing stop)",
                "Tighten stop-loss when approaching target",
                "Loosen stop-loss if conviction increases",
                "Move stop-loss to breakeven after certain profit threshold"
            ],
            "parameters": "Requires reference value (order_open_price, current_price, expert_target_price) and percentage/amount adjustment",
            "example": "When profit_loss_percent >= 10%, adjust_stop_loss to entry price (breakeven)"
        }
    }


def get_rules_overview_html() -> str:
    """
    Generate comprehensive HTML documentation for the rules system.
    
    Returns:
        HTML string with formatted documentation.
    """
    event_docs = get_event_type_documentation()
    action_docs = get_action_type_documentation()
    
    html = """
    <div class="rules-documentation" style="max-height: 600px; overflow-y: auto; padding: 16px;">
        <h3>📋 Rules System Documentation</h3>
        <p>Rules allow you to automate trading decisions based on expert recommendations and position status.</p>
        
        <h4>🎯 Event Triggers (Conditions)</h4>
        <p>These are the conditions that can trigger actions:</p>
        
        <h5>Boolean Events (True/False)</h5>
        <ul>
    """
    
    # Add boolean events
    for event_value, doc in event_docs.items():
        if doc["type"] == "boolean":
            html += f"""
            <li>
                <strong>{doc['name']}</strong>: {doc['description']}
                <br/><em style="color: gray; font-size: 0.9em;">Example: {doc['example']}</em>
            </li>
            """
    
    html += """
        </ul>
        
        <h5>Numeric Events (Comparisons)</h5>
        <p>These events use numeric comparisons (>, <, >=, <=, ==) with a threshold value:</p>
        <ul>
    """
    
    # Add numeric events
    for event_value, doc in event_docs.items():
        if doc["type"] == "numeric":
            html += f"""
            <li>
                <strong>{doc['name']}</strong>: {doc['description']}
                <br/><em style="color: gray; font-size: 0.9em;">Example: {doc['example']}</em>
            </li>
            """
    
    html += """
        </ul>
        
        <h4>⚡ Actions</h4>
        <p>These are the actions that can be taken when triggers are met:</p>
        <ul>
    """
    
    # Add actions
    for action_value, doc in action_docs.items():
        html += f"""
        <li>
            <strong>{doc['name']}</strong>: {doc['description']}
            <ul style="font-size: 0.9em; color: gray;">
        """
        for use_case in doc["use_cases"]:
            html += f"<li>{use_case}</li>"
        html += f"""
            </ul>
            <em style="color: gray; font-size: 0.9em;">Example: {doc['example']}</em>
        </li>
        """
    
    html += """
        </ul>
        
        <h4>💡 How to Use Rules</h4>
        <ol>
            <li><strong>Create a Ruleset</strong>: Group related rules together (e.g., "Conservative Entry Rules", "Profit Protection Rules")</li>
            <li><strong>Add Event-Action Pairs</strong>: Each rule consists of one or more event conditions and a resulting action</li>
            <li><strong>Assign to Experts</strong>: Link rulesets to expert instances for:
                <ul>
                    <li><strong>Enter Market</strong>: Rules for entering new positions</li>
                    <li><strong>Open Positions</strong>: Rules for managing existing positions</li>
                </ul>
            </li>
            <li><strong>Test and Refine</strong>: Monitor rule performance and adjust conditions as needed</li>
        </ol>
        
        <h4>📌 Best Practices</h4>
        <ul>
            <li>Combine multiple conditions for more precise triggers (e.g., confidence > 75% AND no_position AND bullish)</li>
            <li>Use numeric thresholds appropriate for your risk tolerance</li>
            <li>Always include stop-loss protection in open_positions rulesets</li>
            <li>Test new rules with small positions before scaling up</li>
            <li>Review and update rules regularly based on market conditions</li>
        </ul>
    </div>
    """
    
    return html

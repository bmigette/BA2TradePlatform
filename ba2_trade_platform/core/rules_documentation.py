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
            "name": "No Expert Position Exists",
            "description": "Triggers when this expert has NO open position for this symbol (based on transactions).",
            "type": "boolean",
            "example": "Useful for enter_market rules to prevent duplicate expert entries"
        },
        ExpertEventType.F_HAS_POSITION.value: {
            "name": "Expert Position Exists",
            "description": "Triggers when this expert HAS an open position for this symbol (based on transactions).",
            "type": "boolean",
            "example": "Useful for open_positions rules to manage this expert's existing holdings"
        },
        ExpertEventType.F_HAS_NO_POSITION_ACCOUNT.value: {
            "name": "No Account Position Exists",
            "description": "Triggers when the account has NO open position for this symbol (any expert).",
            "type": "boolean",
            "example": "Useful to prevent any new position when account already holds the symbol"
        },
        ExpertEventType.F_HAS_POSITION_ACCOUNT.value: {
            "name": "Account Position Exists",
            "description": "Triggers when the account HAS an open position for this symbol (any expert).",
            "type": "boolean",
            "example": "Useful for account-level position management across all experts"
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
        ExpertEventType.F_RATING_UPGRADED.value: {
            "name": "Rating: Upgraded",
            "description": "Triggers when the expert's grade moved UP the 5-grade scale (SELL < UNDERWEIGHT < HOLD < OVERWEIGHT < BUY) vs the previous recommendation - e.g. HOLD→OVERWEIGHT or OVERWEIGHT→BUY. Covers OVERWEIGHT/UNDERWEIGHT moves the 3-bucket events cannot express.",
            "type": "boolean",
            "example": "Analysts turning more bullish - consider entering or adding"
        },
        ExpertEventType.F_RATING_DOWNGRADED.value: {
            "name": "Rating: Downgraded",
            "description": "Triggers when the expert's grade moved DOWN the 5-grade scale vs the previous recommendation - e.g. BUY→OVERWEIGHT or HOLD→SELL.",
            "type": "boolean",
            "example": "Analysts turning less bullish - consider trimming or exiting"
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
        
        # Target Comparison Flags
        ExpertEventType.F_NEW_TARGET_HIGHER.value: {
            "name": "New Target Higher Than Current TP",
            "description": "Triggers when the expert's new recommended target price is higher than the current take profit price (with 2% tolerance). Indicates expert is more bullish.",
            "type": "boolean",
            "example": "Increase TP when new_target_higher to capture more upside"
        },
        ExpertEventType.F_NEW_TARGET_LOWER.value: {
            "name": "New Target Lower Than Current TP",
            "description": "Triggers when the expert's new recommended target price is lower than the current take profit price (with 2% tolerance). Indicates expert is less optimistic.",
            "type": "boolean",
            "example": "Close position early when new_target_lower (reduced expectations)"
        },

        # Option Position Flags
        ExpertEventType.F_HAS_OPTION_POSITION.value: {
            "name": "Has Option Position",
            "description": "Triggers when this expert already holds an open option position on the underlying symbol.",
            "type": "boolean",
            "example": "Guard to avoid stacking option entries (require NOT has_option_position before buy_call)"
        },
        ExpertEventType.F_HAS_COVERED_CALL.value: {
            "name": "Has Covered Call",
            "description": "Triggers when a short call (covered call) is currently open on the underlying symbol for this expert.",
            "type": "boolean",
            "example": "Avoid writing a second covered call (require NOT has_covered_call before sell_covered_call)"
        },
        ExpertEventType.F_HAS_PROTECTIVE_PUT.value: {
            "name": "Has Protective Put",
            "description": "Triggers when a long put (protective put) is currently open on the underlying symbol for this expert.",
            "type": "boolean",
            "example": "Avoid stacking a second hedge (require NOT has_protective_put before buy_protective_put)"
        },

        # Numeric Events (N_ prefix)
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value: {
            "name": "Expected Profit Target %",
            "description": "The expert's expected profit percentage for this recommendation. Used with numeric comparisons (>, <, >=, <=, ==).",
            "type": "numeric",
            "example": "Trigger when expected profit >= 10% for high-conviction trades"
        },
        ExpertEventType.N_PERCENT_TO_CURRENT_TARGET.value: {
            "name": "Percent to Current Take Profit Target",
            "description": "For open positions: percentage distance from current price to the current take profit price. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Close position when percent_to_current_target <= 5% (near current TP)"
        },
        ExpertEventType.N_PERCENT_TO_NEW_TARGET.value: {
            "name": "Percent to New Expert Target",
            "description": "Percentage distance from current price to the expert's recommended target price. Works for both enter_market and open_positions rules. Positive = target above current (BUY upside), negative = target below current. Use >= 2 to require at least 2% upside before entering.",
            "type": "numeric",
            "example": "Only enter when percent_to_new_target >= 2% (target at least 2% above current price)"
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
        },
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE.value: {
            "name": "Instrument Account Share",
            "description": "Current market value of the instrument position as a percentage of the expert's virtual equity (available balance). Useful for portfolio rebalancing and position sizing.",
            "type": "numeric",
            "example": "Rebalance when instrument_account_share > 15% (position too large) or < 5% (position too small)"
        },
        ExpertEventType.N_PERCENT_OPEN_TO_NEW_TARGET.value: {
            "name": "Percent Open Price to New Expert Target",
            "description": "For open positions: percentage from the position's open (entry) price to the expert's new target price. Positive = target above entry (profit potential for longs). Use to gate TP adjustments: only adjust if the expert target represents enough profit from your entry.",
            "type": "numeric",
            "example": "Only adjust TP when percent_open_to_new_target >= 2% (expert target at least 2% above entry)"
        },

        # Option / Price-Extreme Numeric Events
        ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH.value: {
            "name": "Percent Below Recent High",
            "description": "Drawdown percentage of the current price below the ~20-day high (i.e. how deep the dip is). Higher values = deeper dip. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Buy the dip when percent_below_recent_high >= 15 (price 15%+ below its recent high)"
        },
        ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW.value: {
            "name": "Percent Above Recent Low",
            "description": "Percentage the current price has rebounded above the ~20-day low. Higher values = stronger bounce off the lows. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Confirm a rebound when percent_above_recent_low >= 15 (price 15%+ above its recent low)"
        },
        ExpertEventType.N_IV_RANK.value: {
            "name": "IV Rank",
            "description": "Implied-volatility percentile (0-100) over the stored trailing ATM-IV window. Low IV rank favors buying premium (long calls/spreads); high IV rank favors selling premium (covered calls). Used with numeric comparisons.",
            "type": "numeric",
            "example": "Buy calls only in cheap volatility: iv_rank <= 30"
        },
        ExpertEventType.N_DAYS_TO_EARNINGS.value: {
            "name": "Days to Earnings",
            "description": "Calendar days until the underlying's next earnings announcement (best-effort, FMP-backed). Lower values mean earnings are imminent. Use it to TIME long-volatility entries just before earnings (straddle/strangle) or to AVOID opening positions that would straddle the event. If no upcoming earnings date is available the condition does not fire. Used with numeric comparisons.",
            "type": "numeric",
            "example": "Enter a straddle into earnings: days_to_earnings <= 5"
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
            "name": "Bearish (Sell)",
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
            "name": "Bullish (Buy)",
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
        },
        ExpertActionType.INCREASE_INSTRUMENT_SHARE.value: {
            "name": "Increase Instrument Share",
            "description": "Increase position size to reach a target percentage of virtual equity. Respects max_virtual_equity_per_instrument_percent setting and available balance constraints.",
            "use_cases": [
                "Scale into high-conviction positions",
                "Rebalance portfolio when instrument share is too low",
                "Increase allocation when confidence improves",
                "Build position gradually over time"
            ],
            "parameters": "Requires target_percent (e.g., 15.0 for 15% of virtual equity). Automatically calculates required quantity.",
            "example": "When confidence >= 85% and instrument_account_share < 10%, increase_instrument_share to 12%"
        },
        ExpertActionType.DECREASE_INSTRUMENT_SHARE.value: {
            "name": "Decrease Instrument Share",
            "description": "Decrease position size to reach a target percentage of virtual equity. Maintains minimum of 1 share unless fully closing (target 0%). Used for risk management and rebalancing.",
            "use_cases": [
                "Reduce overweight positions for diversification",
                "Take partial profits while maintaining position",
                "Rebalance when instrument share exceeds target",
                "Reduce exposure when confidence decreases"
            ],
            "parameters": "Requires target_percent (e.g., 5.0 for 5% of virtual equity). Keeps minimum 1 share if target > 0%. Automatically calculates quantity to sell.",
            "example": "When instrument_account_share > 15%, decrease_instrument_share to 10% (rebalance)"
        },

        # Option Actions
        ExpertActionType.BUY_CALL.value: {
            "name": "Buy Call",
            "description": "Open a long call option on the underlying - a directional, leveraged bullish play where the maximum loss is the premium paid (premium-at-risk).",
            "use_cases": [
                "Take leveraged bullish exposure with capped downside (premium only)",
                "Buy the dip in cheap volatility (low iv_rank) for upside convexity",
                "Express a high-conviction bullish thesis without tying up full share notional"
            ],
            "parameters": "strike_method (delta | percent_otm | consensus_target), strike_param, dte_min, dte_max, sizing (pct_equity), min_open_interest, max_spread_pct",
            "example": "When bullish and iv_rank <= 30 and not has_option_position, buy_call (delta ~0.40, 30-60 DTE, 2% equity)"
        },
        ExpertActionType.OPEN_BULL_CALL_SPREAD.value: {
            "name": "Open Bull Call Spread",
            "description": "Open a debit call spread (buy a lower-strike call, sell a higher-strike call). Defined-risk bullish structure where maximum loss equals the net debit paid and upside is capped at the short strike.",
            "use_cases": [
                "Bullish exposure with strictly defined, smaller cost than an outright call",
                "Reduce premium outlay by selling upside when targeting a specific price level",
                "Trade a moderate up-move while capping risk to the net debit"
            ],
            "parameters": "long/short strike params (e.g. long delta or percent_otm, short strike width), dte_min, dte_max, max_risk sizing (pct_equity / net debit), min_open_interest, max_spread_pct",
            "example": "When bullish and percent_to_new_target >= 5%, open_bull_call_spread (long ~0.40 delta / short +5% OTM, 30-60 DTE)"
        },
        ExpertActionType.SELL_COVERED_CALL.value: {
            "name": "Sell Covered Call",
            "description": "Write (sell) a call against a held equity long as an income overlay. Used in open_positions rules; collects premium while capping upside at the short strike on the covered shares.",
            "use_cases": [
                "Generate income on a long equity position during sideways/neutral conditions",
                "Sell premium when iv_rank is high (expensive volatility)",
                "Set a soft exit target by writing calls at a strike you'd be happy to sell at"
            ],
            "parameters": "strike_method/param (OTM, e.g. percent_otm or delta), dte_min, dte_max, min_open_interest, max_spread_pct",
            "example": "When has_position and iv_rank >= 60 and not has_covered_call, sell_covered_call (5% OTM, 30-45 DTE)"
        },
        ExpertActionType.BUY_PUT.value: {
            "name": "Buy Put",
            "description": "Open a long put option on the underlying - a directional, leveraged bearish play where the maximum loss is the premium paid (premium-at-risk).",
            "use_cases": [
                "Take leveraged bearish exposure with capped downside (premium only)",
                "Hedge or speculate on a decline when iv_rank is low (cheap volatility)",
                "Express a high-conviction bearish thesis without shorting shares"
            ],
            "parameters": "strike_method (delta | percent_otm | consensus_target), strike_param, dte_min, dte_max, sizing (pct_equity), min_open_interest, max_spread_pct",
            "example": "When bearish and iv_rank <= 30 and not has_option_position, buy_put (delta ~0.40, 30-60 DTE, 2% equity)"
        },
        ExpertActionType.OPEN_BEAR_PUT_SPREAD.value: {
            "name": "Open Bear Put Spread",
            "description": "Open a debit put spread (buy a higher-strike put, sell a lower-strike put). Defined-risk bearish structure where maximum loss equals the net debit paid and profit is capped at the short strike.",
            "use_cases": [
                "Bearish exposure with strictly defined, smaller cost than an outright put",
                "Reduce premium outlay by selling downside when targeting a specific price level",
                "Trade a moderate down-move while capping risk to the net debit"
            ],
            "parameters": "long/short strike params (e.g. long delta or percent_otm, short strike width), dte_min, dte_max, max_risk sizing (pct_equity / net debit), min_open_interest, max_spread_pct",
            "example": "When bearish and percent_to_new_target >= 5%, open_bear_put_spread (long ~0.40 delta / short -5% OTM, 30-60 DTE)"
        },
        ExpertActionType.BUY_PROTECTIVE_PUT.value: {
            "name": "Buy Protective Put",
            "description": "Buy a put against a held equity long as a downside hedge (one contract per 100 shares). Used in open_positions rules; caps losses below the put strike while keeping full upside, at the cost of the premium paid.",
            "use_cases": [
                "Hedge a long equity position against a drawdown while keeping upside",
                "Insure gains ahead of an uncertain catalyst (e.g. earnings) without selling shares",
                "Set a defined floor on a position you want to hold through volatility"
            ],
            "parameters": "strike_method/param (OTM, e.g. percent_otm or delta), dte_min, dte_max, min_open_interest, max_spread_pct",
            "example": "When has_position and percent_below_recent_high >= 5% and not has_protective_put, buy_protective_put (5% OTM, 30-45 DTE)"
        },
        ExpertActionType.SELL_CASH_SECURED_PUT.value: {
            "name": "Sell Cash-Secured Put",
            "description": "Write (sell) a put while reserving cash equal to the assignment cost (strike * 100 per contract). Short-premium income/entry strategy: collect the put premium now; if the underlying closes below the strike at expiry the shares are assigned (put to you) at the strike. The reserved cash is held against available buying power until the position is closed/expires.",
            "use_cases": [
                "Generate income from premium while willing to own the underlying at the strike",
                "Enter a long-equity position at a discount (effective cost = strike - premium) if assigned",
                "Sell premium when iv_rank is high (expensive volatility) on a name you'd buy"
            ],
            "parameters": "strike_method (delta | percent_otm | consensus_target), strike_param, dte_min, dte_max, sizing (pct_equity sized against the strike*100 cash reserve), min_open_interest, max_spread_pct",
            "example": "When neutral-to-bullish and iv_rank >= 60, sell_cash_secured_put (delta ~0.30, 30-45 DTE). Reserves strike*100 cash; assignment risk if it goes ITM."
        },
        ExpertActionType.OPEN_BEAR_CALL_SPREAD.value: {
            "name": "Open Bear Call Spread",
            "description": "Open a credit call spread (sell a lower-strike call, buy a higher-strike call). Short-premium, defined-risk bearish/neutral structure: you collect a net credit (short.bid - long.ask) up front and the maximum loss equals (spread width - net credit), which is reserved against buying power. The short leg carries assignment risk if it finishes in the money.",
            "use_cases": [
                "Bearish/neutral exposure with strictly defined risk and an up-front credit",
                "Sell premium above resistance when iv_rank is high (expensive volatility)",
                "Profit from time decay while the underlying stays below the short strike"
            ],
            "parameters": "long/short strike params (short = closer/lower-strike sold leg, long = further-OTM/higher-strike bought leg), dte_min, dte_max, max-loss sizing (pct_equity / (width - net credit)), min_open_interest, max_spread_pct",
            "example": "When bearish and iv_rank >= 60, open_bear_call_spread (short ~0.30 delta / long +5 strikes, 30-45 DTE). Net credit limit is negative; max-loss (width - credit) is reserved."
        },
        ExpertActionType.OPEN_STRADDLE.value: {
            "name": "Open Long Straddle",
            "description": "Buy an at-the-money call AND an at-the-money put at the SAME strike (the strike nearest spot) and SAME expiry. Long-volatility debit structure that profits from a large move in EITHER direction. Net debit = call.ask + put.ask is paid up front; the position loses if the underlying stays near the strike. Commonly used to play an expected volatility expansion (e.g. ahead of earnings).",
            "use_cases": [
                "Play an expected big move ahead of a catalyst (earnings, FDA, ruling) when direction is unknown",
                "Buy volatility when iv_rank is low (cheap) and days_to_earnings is small",
                "Express a market-neutral, long-gamma view"
            ],
            "parameters": "dte_min, dte_max, net-debit sizing (pct_equity / (call.ask + put.ask)), min_open_interest, max_spread_pct. The strike is chosen ATM automatically (same strike for both legs).",
            "example": "When iv_rank <= 30 and days_to_earnings <= 5, open_straddle (ATM call + put, 20-45 DTE, 10% equity by net debit)."
        },
        ExpertActionType.OPEN_STRANGLE.value: {
            "name": "Open Long Strangle",
            "description": "Buy an out-of-the-money call (above spot) AND an out-of-the-money put (below spot) at DIFFERENT strikes, both OTM by a configurable percent (default 5%). Cheaper long-volatility variant of the straddle: lower net debit but it needs a larger move to pay off. Net debit = call.ask + put.ask is paid up front.",
            "use_cases": [
                "Cheaper long-volatility play when you expect an outsized move",
                "Buy volatility when iv_rank is low (cheap) into a catalyst",
                "Express a market-neutral, long-gamma view with a wider break-even than a straddle"
            ],
            "parameters": "strike_param = OTM distance percent for BOTH legs (default 5%), dte_min, dte_max, net-debit sizing (pct_equity / (call.ask + put.ask)), min_open_interest, max_spread_pct.",
            "example": "When iv_rank <= 30 and days_to_earnings <= 5, open_strangle (5% OTM call + put, 20-45 DTE, 10% equity by net debit)."
        },
        ExpertActionType.CLOSE_OPTION.value: {
            "name": "Close Option",
            "description": "Close a held option position (long call, spread, or short covered call). Used for take-profit, stop-loss, time-stop, or thesis-flip exits.",
            "use_cases": [
                "Take profit when the option's gain reaches a target",
                "Stop loss when the option's loss exceeds a threshold",
                "Time-stop to avoid theta decay / assignment risk as expiration nears",
                "Exit on a thesis flip (e.g. rating downgraded or turns bearish)"
            ],
            "parameters": "No additional parameters needed - closes the held option position. Typically gated by profit/loss or days/DTE conditions.",
            "example": "When profit_loss_percent >= 50% or days_opened >= 21, close_option"
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

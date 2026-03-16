"""
LLM prompt templates for PennyMomentumTrader pipeline.

Each function builds a prompt string for a specific stage of the trading
pipeline. The caller is responsible for invoking the LLM with the returned
prompt.
"""

import json
from typing import Any, Dict, List


_SYSTEM_PREAMBLE = (
    "You are a professional penny-stock momentum trader with deep experience "
    "in small-cap and micro-cap equities. You think in terms of catalysts, "
    "volume surges, and technical setups. You are disciplined about risk "
    "management and always define clear entry, stop-loss, and take-profit "
    "levels. Respond ONLY with valid JSON as specified."
)


def build_quick_filter_prompt(
    candidates: List[Dict[str, Any]],
    max_survivors: int = 20,
) -> str:
    """
    Build the prompt for the quick-filter stage that narrows ~50 candidates
    down to the top picks based on surface-level attributes.

    Args:
        candidates: List of candidate dicts with keys: symbol, company_name,
            price, volume, market_cap, sector, industry, exchange.
        max_survivors: Maximum number of candidates to keep.

    Returns:
        Prompt string for the LLM.
    """
    candidates_json = json.dumps(candidates, indent=2)

    return f"""{_SYSTEM_PREAMBLE}

You are filtering penny-stock momentum candidates. From the list below, select the top {max_survivors} stocks most likely to produce a profitable momentum trade today or this week.

FILTER CRITERIA (apply all):
1. Sector quality: Avoid biotech/pharma stocks that appear to be pre-FDA (high risk binary events). Avoid energy stocks unless oil prices are trending up. Favor technology, consumer, and industrial sectors with clear momentum drivers.
2. Volume/price action: Prefer stocks with unusually high volume relative to their typical levels. Higher volume signals institutional interest or catalyst-driven activity.
3. Market cap sweet spot: Favor $50M-$500M market cap. Too small (<$10M) means illiquid and manipulable; too large (>$1B) means less explosive moves.
4. Exchange quality: Prefer NASDAQ and NYSE over OTC/pink sheets.
5. Price range: Ideal range is $0.50-$10.00. Avoid sub-penny stocks.

CANDIDATES:
{candidates_json}

RESPOND with a JSON array of the top {max_survivors} candidates. Each element must be an object with exactly two keys:
- "symbol": the stock ticker (string)
- "reasoning": one sentence explaining why this candidate was selected (string)

Example response format:
[
  {{"symbol": "ABCD", "reasoning": "High volume surge in tech sector with $120M market cap in the momentum sweet spot"}},
  {{"symbol": "EFGH", "reasoning": "Consumer sector breakout with 3x average volume and strong price action"}}
]

Return ONLY the JSON array, no other text."""


def build_deep_triage_prompt(
    symbol: str,
    news: str,
    insider: str,
    fundamentals: str,
    social: str,
) -> str:
    """
    Build the prompt for deep triage analysis of a single stock.

    Args:
        symbol: Ticker symbol.
        news: Aggregated news data as a formatted string.
        insider: Insider trading data as a formatted string.
        fundamentals: Fundamental data as a formatted string.
        social: Social media sentiment data as a formatted string.

    Returns:
        Prompt string for the LLM.
    """
    return f"""{_SYSTEM_PREAMBLE}

Perform a deep triage analysis of {symbol} for a potential penny-stock momentum trade. Evaluate all the data below and determine whether this stock has a tradeable setup.

ANALYSIS FRAMEWORK:
- Catalyst identification: Is there a clear, actionable catalyst (earnings beat, contract win, product launch, short squeeze setup)?
- News quality: Is the news fresh (last 24-48h) and material, or stale/irrelevant?
- Insider activity: Are insiders buying (bullish) or selling (bearish)?
- Fundamental support: Does revenue/cash position support the current price, or is this purely speculative?
- Social momentum: Is there growing retail interest that could drive a momentum wave?
- Risk factors: Dilution risk, reverse split history, SEC issues, or other red flags?

--- NEWS ---
{news}

--- INSIDER ACTIVITY ---
{insider}

--- FUNDAMENTALS ---
{fundamentals}

--- SOCIAL SENTIMENT ---
{social}

RESPOND with a single JSON object with exactly these keys:
- "confidence": integer from 1-100 representing how confident you are this is a good trade (1=terrible, 100=exceptional setup)
- "catalyst": brief description of the primary catalyst driving this stock (string)
- "strategy": either "intraday" (day trade, close by EOD) or "swing" (hold 2-5 days) based on the catalyst type and expected move timeline (string)
- "expected_profit_pct": realistic expected profit percentage if the trade works (float, e.g. 8.5 for 8.5%)
- "risk_assessment": brief description of the main risks (string)
- "reasoning": 2-3 sentence explanation of your overall assessment (string)

Example response:
{{
  "confidence": 72,
  "catalyst": "Q3 earnings beat with revenue up 40% YoY, raised guidance",
  "strategy": "swing",
  "expected_profit_pct": 12.0,
  "risk_assessment": "Low float could cause sharp reversal; company has history of secondary offerings",
  "reasoning": "Strong earnings catalyst with genuine revenue growth. Social sentiment is building but not yet peaked. The low float amplifies both upside and downside risk."
}}

Return ONLY the JSON object, no other text."""


def build_entry_conditions_prompt(
    symbol: str,
    analysis_summary: str,
    condition_types_str: str,
) -> str:
    """
    Build the prompt for generating structured entry/exit conditions.

    Args:
        symbol: Ticker symbol.
        analysis_summary: Summary from the deep triage analysis.
        condition_types_str: Formatted string of available condition types
            from get_condition_types_for_llm().

    Returns:
        Prompt string for the LLM.
    """
    return f"""{_SYSTEM_PREAMBLE}

Define structured entry, stop-loss, and take-profit conditions for a momentum trade on {symbol}.

ANALYSIS SUMMARY:
{analysis_summary}

{condition_types_str}

GUIDELINES:
- Entry conditions should confirm momentum is real before entering (e.g., price above VWAP + volume surge + price above a key EMA).
- Stop-loss should use "percent_below_entry" with a tight stop (5-8% for penny stocks) OR a technical level. Use "any" logic so any single stop condition triggers an exit.
- Take-profit should be TIERED with 3 levels to lock in gains progressively:
  - Tier 1: Conservative target, exit 33% of position
  - Tier 2: Moderate target, exit 50% of remaining position
  - Tier 3: Aggressive target, exit 100% of remaining position
- For intraday trades, include a time-based stop (e.g., exit before 15:45 market close).
- Each condition is a dict with a "type" key plus the required parameters for that type.

RESPOND with a single JSON object with this exact structure:
{{
  "entry": {{"all": [<list of condition dicts>]}},
  "stop_loss": {{"any": [<list of condition dicts>]}},
  "take_profit": [
    {{"condition": <condition dict or composite>, "exit_pct": 33}},
    {{"condition": <condition dict or composite>, "exit_pct": 50}},
    {{"condition": <condition dict or composite>, "exit_pct": 100}}
  ]
}}

Example response:
{{
  "entry": {{
    "all": [
      {{"type": "price_above_vwap", "timeframe": "5m"}},
      {{"type": "volume_above_avg", "multiplier": 2.0, "window": 20}},
      {{"type": "price_above_ema", "period": 9, "timeframe": "5m"}}
    ]
  }},
  "stop_loss": {{
    "any": [
      {{"type": "percent_below_entry", "percent": 6.0}},
      {{"type": "price_below_vwap", "timeframe": "5m"}}
    ]
  }},
  "take_profit": [
    {{"condition": {{"type": "percent_above_entry", "percent": 5.0}}, "exit_pct": 33}},
    {{"condition": {{"type": "percent_above_entry", "percent": 10.0}}, "exit_pct": 50}},
    {{"condition": {{"type": "percent_above_entry", "percent": 18.0}}, "exit_pct": 100}}
  ]
}}

Return ONLY the JSON object, no other text."""


def build_exit_update_prompt(
    symbol: str,
    current_conditions: Dict[str, Any],
    new_data: str,
) -> str:
    """
    Build the prompt for updating exit conditions based on new market data.

    Args:
        symbol: Ticker symbol.
        current_conditions: Existing exit conditions dict (JSON-serializable)
            containing stop_loss and take_profit sections.
        new_data: New market data or news as a formatted string.

    Returns:
        Prompt string for the LLM.
    """
    conditions_json = json.dumps(current_conditions, indent=2)

    return f"""{_SYSTEM_PREAMBLE}

Review the current exit conditions for an active momentum trade on {symbol} in light of new market data. Determine whether the exit strategy should be adjusted.

CURRENT EXIT CONDITIONS:
{conditions_json}

NEW MARKET DATA / NEWS:
{new_data}

ADJUSTMENT GUIDELINES:
- Tighten stop-loss if the stock has moved significantly in your favor (trail the stop up).
- Widen take-profit targets if a new positive catalyst has emerged.
- Tighten take-profit or add time-based exits if negative news appears.
- Add a stop-loss condition if a key technical level has been broken.
- Do NOT change conditions just for the sake of changing them. Only adjust if the new data materially changes the risk/reward profile.

RESPOND with one of two options:

OPTION 1 - If conditions should be updated, return a JSON object with the same structure as the current conditions (with "stop_loss" and "take_profit" keys):
{{
  "stop_loss": {{"any": [<updated condition dicts>]}},
  "take_profit": [
    {{"condition": <condition>, "exit_pct": 33}},
    {{"condition": <condition>, "exit_pct": 50}},
    {{"condition": <condition>, "exit_pct": 100}}
  ]
}}

OPTION 2 - If no changes are needed, return exactly:
"NO_CHANGE"

Return ONLY the JSON object or the string "NO_CHANGE", no other text."""

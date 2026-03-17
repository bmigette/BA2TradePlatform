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
    max_survivors: int = 15,
) -> str:
    """
    Build the prompt for the quick-filter stage that narrows ~50 screener
    candidates down to the top picks based on surface-level attributes
    and StockTwits social sentiment data (when available).

    Args:
        candidates: List of candidate dicts. Core keys: symbol, company_name,
            price, volume, market_cap, sector, industry, exchange.
            Optional StockTwits keys: st_watchlist, st_bullish_pct,
            st_bearish_pct, st_trending, st_trending_score.
        max_survivors: Maximum number of candidates to keep.

    Returns:
        Prompt string for the LLM.
    """
    # Build a concise per-candidate summary to keep the prompt token-efficient
    candidate_lines = []
    for c in candidates:
        symbol = c.get("symbol", "?")
        price = c.get("price")
        volume = c.get("volume")
        mktcap = c.get("market_cap")
        sector = c.get("sector", "")
        exchange = c.get("exchange", "")

        price_str = f"${price:.2f}" if price else "N/A"
        vol_str = f"{volume:,}" if volume else "N/A"
        cap_str = f"${mktcap/1e6:.0f}M" if mktcap else "N/A"

        # StockTwits fields (may be absent)
        st_wl = c.get("st_watchlist")
        st_bull = c.get("st_bullish_pct")
        st_bear = c.get("st_bearish_pct")
        st_trend = c.get("st_trending")
        st_tscore = c.get("st_trending_score")

        # RVOL and change % (enriched during phase 1 screening)
        rvol = c.get("rvol")
        chg_pct = c.get("change_percent")

        industry = c.get("industry", "")
        sector_industry = f"{sector}/{industry}" if industry and industry != sector else (sector or "?")
        line = (
            f'{symbol}: price={price_str}, vol={vol_str}, mktcap={cap_str}, '
            f'sector={sector_industry}, exchange={exchange or "?"}'
        )
        if rvol is not None and rvol > 0:
            line += f", rvol={rvol:.1f}x"
        if chg_pct is not None and chg_pct != 0:
            line += f", chg={chg_pct:+.1f}%"
        if st_wl is not None:
            wl_str = f"{st_wl:,}"
            line += f", st_watchlist={wl_str}"
        if st_bull is not None:
            line += f", st_bull={st_bull}% st_bear={st_bear}%"
        if st_trend is not None:
            trend_str = "YES" if st_trend else "no"
            line += f", trending={trend_str}(score={st_tscore:.2f})" if st_tscore is not None else f", trending={trend_str}"
        candidate_lines.append(line)

    candidates_text = "\n".join(candidate_lines)

    # Conditionally explain StockTwits fields if any candidate has them
    has_stocktwits = any(c.get("st_watchlist") is not None for c in candidates)
    stocktwits_note = ""
    if has_stocktwits:
        stocktwits_note = """
STOCKTWITS DATA EXPLANATION:
- st_watchlist: number of StockTwits users watching this stock (higher = more retail interest)
- st_bull / st_bear: % of tagged messages that are Bullish vs Bearish (tagged messages only)
- trending: whether the stock is currently trending on StockTwits
- trending_score: positive = gaining attention, negative = losing attention
Use these as confirmation signals — high watchlist + high bull% + trending strongly favor selection.
"""

    return f"""{_SYSTEM_PREAMBLE}

You are filtering penny-stock momentum candidates. From the list below, select up to {max_survivors} stocks most likely to produce a profitable momentum trade today or this week. Only include stocks that genuinely meet the criteria — do NOT pad to reach {max_survivors} if fewer stocks qualify.
{stocktwits_note}
FILTER CRITERIA (apply all):
1. Sector quality: The "sector" field may show "Healthcare/Biotechnology" or "Healthcare/Pharmaceuticals" — treat these as binary-event risk and DROP them unless there is a confirmed catalyst (earnings, not FDA trial). Avoid energy stocks unless oil prices are trending up. Favor Technology, Consumer, and Industrial sectors with clear momentum drivers.
2. Volume/momentum: Use the "rvol" field (relative volume vs 20-day average) when available. rvol >= 2.0 is a strong signal; rvol < 1.0 means below-average activity — deprioritize. Also factor in "chg" (price change %) as a momentum indicator.
3. Market cap sweet spot: Favor $50M–$500M market cap. Too small (<$10M) means illiquid and manipulable; too large (>$1B) means less explosive moves.
4. Exchange quality: Prefer NASDAQ and NYSE over OTC/pink sheets.
5. Price range: Ideal range is $0.50–$10.00. Avoid sub-penny stocks.
6. Social signal (when available): High StockTwits watchlist count + strong bullish sentiment + trending score > 0 indicate growing retail momentum. Bearish-dominant sentiment or negative trending score is a warning sign.

CANDIDATES:
{candidates_text}

RESPOND with a JSON object containing two keys:
- "selected": array of up to {max_survivors} candidates to keep (only include genuinely strong setups), each with:
  - "symbol": the stock ticker (string)
  - "reasoning": one sentence explaining why this candidate was selected (string)
- "dropped": array of ALL remaining candidates that were NOT selected, each with:
  - "symbol": the stock ticker (string)
  - "reason": one sentence explaining why this candidate was dropped (string)

Example response format:
{{
  "selected": [
    {{"symbol": "ABCD", "reasoning": "High volume surge in tech sector, 85% bullish on StockTwits with 45k watchers"}},
    {{"symbol": "EFGH", "reasoning": "Consumer sector breakout with 3x average volume and strong price action"}}
  ],
  "dropped": [
    {{"symbol": "IJKL", "reason": "Biotech with pending FDA decision - binary risk too high"}},
    {{"symbol": "MNOP", "reason": "Below-average volume and bearish social sentiment (72% bearish)"}}
  ]
}}

Return ONLY the JSON object, no other text."""


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

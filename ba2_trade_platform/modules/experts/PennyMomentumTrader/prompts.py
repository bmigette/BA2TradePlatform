"""
LLM prompt templates for PennyMomentumTrader pipeline.

Each function builds a prompt string for a specific stage of the trading
pipeline. The caller is responsible for invoking the LLM with the returned
prompt.
"""

import re
import json
from typing import Any, Dict, List


_SYSTEM_PREAMBLE = (
    "You are a professional penny-stock momentum trader with deep experience "
    "in small-cap and micro-cap equities. You think in terms of catalysts, "
    "volume surges, and technical setups. You are disciplined about risk "
    "management and always define clear entry, stop-loss, and take-profit "
    "levels. Respond ONLY with valid JSON as specified."
)

_DEEP_TRIAGE_PREAMBLE = (
    "You are a professional penny-stock momentum trader specializing in "
    "catalyst-driven small-cap and micro-cap equities. You evaluate stocks "
    "based on news catalysts, fundamental support, insider activity, and "
    "social momentum. Respond ONLY with valid JSON as specified."
)

# Max chars allowed per data section before truncation in deep triage
_MAX_SECTION_CHARS = 6000


def _clean_section(text: str, max_chars: int = _MAX_SECTION_CHARS) -> str:
    """
    Strip AI search-loop noise and truncate oversized sections.

    Removes repetitive LLM self-dialogue patterns like:
      "I notice the search results... Let me search more specifically..."
    that appear when websearch agents fail to find useful results.
    """
    # Remove lines that are search-loop noise (LLM talking to itself)
    noise_patterns = [
        r"(?m)^.*\bLet me search\b.*$",
        r"(?m)^.*\bI notice the search results\b.*$",
        r"(?m)^.*\bLet me try\b.*(?:search|query|look).*$",
        r"(?m)^.*\bSearching for\b.*$",
        r"(?m)^.*\bI'll search\b.*$",
        r"(?m)^.*\bI need to search\b.*$",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text)

    # Collapse runs of blank lines left behind
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Truncate if still too long
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[... truncated at {max_chars} chars ...]"

    return text


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
1. Sector quality: The "sector" field may show "Healthcare/Biotechnology" or "Healthcare/Pharmaceuticals" — these carry binary-event risk and warrant extra scrutiny. FDA trial readouts and clinical data events are high-risk. However, use your judgment: Healthcare/Biotech stocks with clear business catalysts (such as earnings beats, strategic transactions, M&A, partnership agreements, contract wins, or product launches) can be strong momentum setups. This list of example catalysts is not exhaustive — if a confirmed, material catalyst exists, weigh it accordingly. Avoid energy stocks unless oil prices are trending up. Favor Technology, Consumer, and Industrial sectors with clear momentum drivers.
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
    market_context: str = "",
) -> str:
    """
    Build the prompt for deep triage analysis of a single stock.

    Args:
        symbol: Ticker symbol.
        news: Aggregated news data as a formatted string.
        insider: Insider trading data as a formatted string.
        fundamentals: Fundamental data as a formatted string.
        social: Social media sentiment data as a formatted string.
        market_context: Optional string describing current date/time and market
            state (e.g. "Pre-market, 2026-03-17 08:45 ET. Regular session opens
            in 45 min."). Helps the LLM choose intraday vs swing strategy.

    Returns:
        Prompt string for the LLM.
    """
    news_clean = _clean_section(news)
    insider_clean = _clean_section(insider)
    fundamentals_clean = _clean_section(fundamentals)
    social_clean = _clean_section(social)

    market_context_block = (
        f"\nMARKET CONTEXT:\n{market_context}\n" if market_context else ""
    )

    return f"""{_DEEP_TRIAGE_PREAMBLE}
{market_context_block}
Perform a deep triage analysis of {symbol} for a potential penny-stock momentum trade. Evaluate all the data below and determine whether this stock has a tradeable setup.

ANALYSIS FRAMEWORK:
- Catalyst identification: Is there a clear, actionable catalyst driving this move? Common examples include earnings beats, contract wins, product launches, short squeeze setups, M&A/strategic transactions, regulatory approvals, or partnership announcements — but this list is not exhaustive. Exercise your own judgment to identify any material catalyst.
- News quality: Is the news fresh and material, or stale/irrelevant? Pay particular attention to after-hours and pre-market releases from the prior evening, as these often drive gap-up moves that may not yet appear in standard news feeds. Translate and synthesize any non-English headlines.
- Insider activity: Distinguish between open-market purchases (bullish signal), open-market sales (bearish signal), and scheduled stock awards/grants such as A-Award transactions (neutral — these are corporate compensation, not a directional bet).
- Fundamental support: Does revenue/cash position support the current price, or is this purely speculative?
- Social momentum: Is there growing retail interest that could drive a momentum wave?
- Risk factors: Dilution risk, reverse split history, SEC issues, or other red flags?
- Strategy timing: Use the market context above to gauge whether the catalyst favors an intraday gap-and-run or a multi-day swing setup.

--- NEWS ---
{news_clean}

--- INSIDER ACTIVITY ---
{insider_clean}

--- FUNDAMENTALS ---
{fundamentals_clean}

--- SOCIAL SENTIMENT ---
{social_clean}

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
    current_price: float = None,
    current_rvol: float = None,
) -> str:
    """
    Build the prompt for generating structured entry/exit conditions.

    Args:
        symbol: Ticker symbol.
        analysis_summary: Summary from the deep triage analysis.
        condition_types_str: Formatted string of available condition types
            from get_condition_types_for_llm().
        current_price: Current market price of the symbol (for context).
        current_rvol: Current RVOL from Phase 1 screening (for context).

    Returns:
        Prompt string for the LLM.
    """
    # Build market context section
    market_context_lines = []
    if current_price is not None:
        market_context_lines.append(f"Current price: ${current_price:.4f}")
        pcts = [5, 10, 15, 25, 50, 100]
        levels = "  |  ".join(f"+{p}% = ${current_price * (1 + p / 100):.4f}" for p in pcts)
        market_context_lines.append(f"Price levels:  {levels}")
    if current_rvol is not None:
        market_context_lines.append(f"Current RVOL: {current_rvol:.1f}x (relative to 20-day avg)")
    market_context = "\n".join(market_context_lines)
    market_context_block = f"\nCURRENT MARKET DATA:\n{market_context}\n" if market_context_lines else ""

    return f"""{_SYSTEM_PREAMBLE}

Define structured entry, stop-loss, and take-profit conditions for a momentum trade on {symbol}.

ANALYSIS SUMMARY:
{analysis_summary}
{market_context_block}
{condition_types_str}

PARAMETER CONSTRAINTS — you MUST pick values from these ranges:

VOLUME CONDITIONS (prefer rvol_above over volume_above_avg):
  rvol_above.threshold: pick from [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    - 1.5x = mild interest, 2.0x = solid, 3.0x+ = strong surge
    - Use the current RVOL above as context: set threshold BELOW the current RVOL so the condition is achievable
  volume_above_avg: AVOID — compares intraday cumulative volume against full-day averages, making it nearly impossible to meet early in the session. Use rvol_above instead.
  volume_spike.multiplier: pick from [1.5, 2.0, 3.0]; minutes: pick from [3, 5, 10]

MOVING AVERAGES:
  EMA/SMA period: pick from [5, 9, 13, 20, 50]
  timeframe: pick from ["1m", "5m", "15m"]
    - 1m = ultra short-term noise, use sparingly
    - 5m = good for intraday momentum confirmation
    - 15m = stronger trend signal, slower to react

VWAP:
  timeframe: pick from ["1m", "5m"]

RSI:
  period: pick from [7, 14]
  timeframe: pick from ["5m", "15m"]
  threshold for rsi_above: pick from [50, 55, 60]  (momentum confirmation, NOT overbought filter)
  threshold for rsi_below: pick from [40, 45, 50]

MACD / EMA CROSSOVER:
  timeframe: pick from ["5m", "15m"]
  ema_cross fast_period/slow_period: pick from [5/13, 9/21, 8/20]

PRICE THRESHOLDS:
  price_above / price_below: set relative to the CURRENT PRICE above, e.g. within 1-3% of current price
  Do NOT set price_above thresholds more than 5% above current price for entry conditions

STOP-LOSS (percent_below_entry):
  percent: pick from [4.0, 5.0, 6.0, 8.0, 10.0]
    - 4-5% = tight (intraday), 6-8% = moderate (swing), 10% = wide

TAKE-PROFIT (percent_above_entry):
  Tier 1: pick from [3.0, 5.0, 7.0]
  Tier 2: pick from [8.0, 10.0, 12.0]
  Tier 3: pick from [15.0, 18.0, 25.0]

OPENING RANGE BREAKOUT:
  minutes: pick from [5, 10, 15, 30]

TIME CONDITIONS:
  time_after: optional gate based on the catalyst and strategy — choose the time that best fits:
    - "09:30" = enter immediately at market open (pre-market catalyst already confirmed)
    - "09:45" / "10:00" = wait for opening volatility to settle before entering
    - "10:30" / "11:00" = wait for broader market direction to establish
    Omit entirely if a price/volume condition already provides sufficient timing control.
  time_before: use for intraday exit deadline (e.g. "15:30", "15:45")

GUIDELINES:
- Entry conditions should confirm momentum is real before entering. Use 2-4 conditions with "all" logic.
- PREFERRED entry pattern: rvol_above (volume confirmation) + price_above_vwap (trend) + price_above_ema (momentum)
- Stop-loss: use "any" logic so any single stop condition triggers an exit.
- Take-profit should be TIERED with 3 levels to lock in gains progressively:
  - Tier 1: Conservative target, exit 33% of position
  - Tier 2: Moderate target, exit 50% of remaining position
  - Tier 3: Aggressive target, exit 100% of remaining position
- For intraday trades, include a time_before stop (exit before 15:45).
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
      {{"type": "rvol_above", "threshold": 2.0}},
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

VALID CONDITION TYPES (use ONLY these exact type names and required params):
  {{"type": "percent_below_entry", "percent": <float>}}   — stop loss: X% below entry
  {{"type": "percent_above_entry", "percent": <float>}}   — take profit: X% above entry
  {{"type": "price_above", "value": <float>}}             — price > specific value (value is REQUIRED)
  {{"type": "price_below", "value": <float>}}             — price < specific value (value is REQUIRED)
  {{"type": "price_below_vwap", "timeframe": "<1m|5m>"}} — price drops below VWAP
  {{"type": "price_above_vwap", "timeframe": "<1m|5m>"}} — price breaks above VWAP
  {{"type": "rsi_above", "threshold": <float>, "period": <int>, "timeframe": "<5m|15m>"}}
  {{"type": "rsi_below", "threshold": <float>, "period": <int>, "timeframe": "<5m|15m>"}}
  {{"type": "time_before", "time": "<HH:MM>"}}            — exit before this time (e.g. "15:45")
  {{"type": "rvol_above", "threshold": <float>}}          — relative volume above threshold

DO NOT invent condition types. "price_at_or_above" does not exist — use "price_above".
Every condition must have ALL required params listed above.

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


# Sorted list of all valid condition type names for fix prompts
_VALID_CONDITION_TYPES = (
    "price_above, price_below, price_above_ema, price_below_ema, "
    "price_above_sma, price_below_sma, price_above_vwap, price_below_vwap, "
    "opening_range_breakout, volume_above_avg, rvol_above, volume_spike, "
    "rsi_above, rsi_below, rsi_between, macd_bullish_cross, macd_bearish_cross, "
    "ema_cross_above, ema_cross_below, percent_above_entry, percent_below_entry, "
    "time_after, time_before"
)

_CONDITION_PARAMS_REFERENCE = """\
Required params per type:
  price_above / price_below                        → value (float)
  price_above_ema / price_below_ema                → period (int), timeframe (str)
  price_above_sma / price_below_sma                → period (int), timeframe (str)
  price_above_vwap / price_below_vwap              → timeframe (str)
  opening_range_breakout                           → minutes (int)
  volume_above_avg                                 → multiplier (float), window (int)
  rvol_above                                       → threshold (float)
  volume_spike                                     → multiplier (float), minutes (int)
  rsi_above / rsi_below                            → threshold (float), period (int), timeframe (str)
  rsi_between                                      → min (float), max (float), period (int), timeframe (str)
  macd_bullish_cross / macd_bearish_cross          → timeframe (str)
  ema_cross_above / ema_cross_below                → fast_period (int), slow_period (int), timeframe (str)
  percent_above_entry / percent_below_entry        → percent (float)
  time_after / time_before                         → time (str, "HH:MM")"""


def build_conditions_fix_prompt(
    previous_response: str,
    errors: List[str],
) -> str:
    """
    Build a correction prompt fed back to the LLM when generated conditions
    fail JSON parsing or schema validation.

    Args:
        previous_response: The raw LLM output that was invalid.
        errors: List of validation error messages from validate_condition_set().

    Returns:
        Prompt string asking the LLM to fix the specific errors.
    """
    errors_text = "\n".join(f"  - {e}" for e in errors)

    return f"""{_SYSTEM_PREAMBLE}

Your previous response contained invalid trading conditions. Fix ONLY the errors listed below and return the corrected JSON.

PREVIOUS RESPONSE:
{previous_response}

VALIDATION ERRORS TO FIX:
{errors_text}

VALID CONDITION TYPE NAMES (use EXACTLY these strings, no others):
  {_VALID_CONDITION_TYPES}

{_CONDITION_PARAMS_REFERENCE}

Return ONLY the corrected JSON object, no other text."""

"""
Realistic test for NagaAI tool calling bug - mimics TradingAgents market analyst.

This test creates the EXACT same setup as the market analyst:
1. Same tools (get_ohlcv_data, get_indicator_data) 
2. Same prompt structure (asking to choose 8 indicators)
3. Same LangChain bind_tools() call

Usage:
    .venv\Scripts\python.exe test_files\test_nagaai_realistic.py
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / "creds.env"
if env_path.exists():
    load_dotenv(env_path)

# Import database helpers
from ba2_trade_platform.core.db import Session, engine
from ba2_trade_platform.core.models import AppSetting
from sqlmodel import select


def get_app_setting(key: str) -> str | None:
    """Get an app setting value from the database."""
    try:
        with Session(engine) as session:
            setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            return setting.value_str if setting else None
    except Exception as e:
        print(f"Error loading app setting '{key}': {e}")
        return None


NAGA_API_KEY = get_app_setting('naga_ai_api_key')
NAGA_BASE_URL = "https://api.naga.ac/v1"

if not NAGA_API_KEY:
    print("ERROR: No naga_ai_api_key found in AppSettings")
    sys.exit(1)


# =============================================================================
# EXACT Market Analyst System Prompt from TradingAgents
# =============================================================================

MARKET_ANALYST_SYSTEM_PROMPT = """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. 

**IMPORTANT:** Your analysis will use the configured timeframe for all data. Consider how the timeframe affects indicator behavior:
- **Shorter timeframes (1m-30m)**: Focus on momentum and volume indicators for quick signals; expect more noise
- **Medium timeframes (1h-1d)**: Balance between responsiveness and noise; traditional indicator thresholds apply well
- **Longer timeframes (1wk-1mo)**: Emphasize trend indicators; signals are stronger but less frequent

Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context and timeframe. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_YFin_data first to retrieve the CSV that is needed to generate indicators. Write a very detailed and nuanced report of the trends you observe, considering the timeframe context. Do not simply state the trends are mixed, provide detailed and finegrained analysis and insights that may help traders make decisions. Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""

ANALYST_COLLABORATION_PROMPT = """You are a helpful AI assistant, collaborating with other assistants. Use the provided tools to progress towards answering the question. If you are unable to fully answer, that's OK; another assistant with different tools will help where you left off. Execute what you can to make progress. If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable, prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop. You have access to the following tools: {tool_names}.
{system_message}
For your reference, the current date is {current_date}. {context_info}

**ANALYSIS TIMEFRAME CONFIGURATION:**
Your analysis is configured to use **{timeframe}** timeframe data. This affects all market data and technical indicators:
- **1m, 5m, 15m, 30m**: Intraday analysis for day trading and scalping strategies
- **1h**: Short-term analysis for swing trading 
- **1d**: Traditional daily analysis for position trading
- **1wk, 1mo**: Long-term analysis for trend following and position trading"""


def test_realistic_market_analyst():
    """Test with the EXACT same prompt and tools as TradingAgents market analyst."""
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_core.messages import HumanMessage
    
    print("\n" + "=" * 80)
    print("REALISTIC MARKET ANALYST TEST")
    print("=" * 80)
    
    # Define tools EXACTLY like TradingAgents
    @tool
    def get_ohlcv_data(symbol: str = None) -> str:
        """Retrieve OHLCV (Open, High, Low, Close, Volume) price data for the company being analyzed.
        
        Args:
            symbol: Stock ticker symbol (optional, uses company being analyzed if not provided)
            
        Returns:
            OHLCV data as text
        """
        return f"[MOCK] OHLCV data for {symbol or 'NVDA'}: Open=130.50, High=135.20, Low=129.80, Close=134.10, Volume=45M"
    
    @tool
    def get_indicator_data(indicator: str, symbol: str = None) -> str:
        """Calculate and retrieve a technical indicator for the company being analyzed.
        
        Args:
            indicator: The indicator to calculate. Must be one of: rsi, macd, macds, macdh, 
                      boll, boll_ub, boll_lb, atr, close_50_sma, close_200_sma, close_10_ema, vwma
            symbol: Stock ticker symbol (optional)
            
        Returns:
            Indicator data as text
        """
        return f"[MOCK] {indicator} indicator for {symbol or 'NVDA'}: value=65.5"
    
    tools = [get_ohlcv_data, get_indicator_data]
    
    # Create LLM - EXACTLY like TradingAgents
    model_name = "grok-4.1-fast-reasoning"  # Extract from NagaAC/grok-4.1-fast-reasoning
    
    llm = ChatOpenAI(
        model=model_name,
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
        temperature=0,
    )
    
    # Format the prompt EXACTLY like TradingAgents
    tool_names_str = ", ".join([t.name for t in tools])
    current_date = "2025-12-10"
    ticker = "NVDA"
    timeframe = "1d"
    
    formatted_system = ANALYST_COLLABORATION_PROMPT.format(
        tool_names=tool_names_str,
        system_message=MARKET_ANALYST_SYSTEM_PROMPT,
        current_date=current_date,
        context_info=f"You are analyzing {ticker}.",
        timeframe=timeframe
    )
    
    # Create prompt template EXACTLY like TradingAgents  
    prompt = ChatPromptTemplate.from_messages([
        ("system", formatted_system),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    # Test with parallel_tool_calls=False
    print(f"\nTest 1: parallel_tool_calls=False")
    print("-" * 60)
    
    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
    chain = prompt | llm_with_tools
    
    messages = [HumanMessage(content=f"Analyze {ticker} stock for trading.")]
    
    try:
        result = chain.invoke({"messages": messages})
        
        print(f"Content: {result.content[:200] if result.content else '(none)'}...")
        print(f"Tool calls: {len(result.tool_calls) if result.tool_calls else 0}")
        
        if result.tool_calls:
            for i, tc in enumerate(result.tool_calls, 1):
                name = tc.get('name', 'Unknown')
                call_id = tc.get('id', 'Unknown')
                args = tc.get('args', {})
                print(f"  {i}. name: '{name}' (len={len(name)})")
                print(f"     id: '{call_id}' (len={len(call_id)})")
                print(f"     args: {args}")
                
                # Check for concatenation
                if len(name) > 30:
                    print(f"     ⚠️ WARNING: Tool name looks concatenated!")
    except Exception as e:
        print(f"ERROR: {e}")
    
    # Test with parallel_tool_calls=True for comparison
    print(f"\nTest 2: parallel_tool_calls=True")
    print("-" * 60)
    
    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=True)
    chain = prompt | llm_with_tools
    
    try:
        result = chain.invoke({"messages": messages})
        
        print(f"Content: {result.content[:200] if result.content else '(none)'}...")
        print(f"Tool calls: {len(result.tool_calls) if result.tool_calls else 0}")
        
        if result.tool_calls:
            for i, tc in enumerate(result.tool_calls, 1):
                name = tc.get('name', 'Unknown')
                call_id = tc.get('id', 'Unknown')
                args = tc.get('args', {})
                print(f"  {i}. name: '{name}' (len={len(name)})")
                print(f"     id: '{call_id}' (len={len(call_id)})")
                print(f"     args: {args}")
                
                # Check for concatenation
                if len(name) > 30:
                    print(f"     ⚠️ WARNING: Tool name looks concatenated!")
    except Exception as e:
        print(f"ERROR: {e}")


def test_with_conversation_history():
    """Test with conversation history containing previous tool calls (like in a graph iteration)."""
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    
    print("\n" + "=" * 80)
    print("TEST WITH CONVERSATION HISTORY (LIKE GRAPH ITERATION)")
    print("=" * 80)
    
    @tool
    def get_ohlcv_data(symbol: str = None) -> str:
        """Retrieve OHLCV data."""
        return f"[MOCK] OHLCV data for {symbol or 'NVDA'}"
    
    @tool
    def get_indicator_data(indicator: str, symbol: str = None) -> str:
        """Get technical indicator."""
        return f"[MOCK] {indicator} for {symbol or 'NVDA'}: 65.5"
    
    tools = [get_ohlcv_data, get_indicator_data]
    
    llm = ChatOpenAI(
        model="grok-4.1-fast-reasoning",
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
        temperature=0,
    )
    
    # Simulate conversation history WITH tool calls (like after a graph iteration)
    # This is what the state looks like when the analyst has already called some tools
    messages = [
        HumanMessage(content="Analyze NVDA stock for trading."),
        # Previous AI message with tool calls
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_001", "name": "get_ohlcv_data", "args": {"symbol": "NVDA"}},
            ]
        ),
        # Tool response
        ToolMessage(content="[MOCK] OHLCV data for NVDA", tool_call_id="call_001", name="get_ohlcv_data"),
        # Human continues
        HumanMessage(content="Now get the RSI and MACD indicators too."),
    ]
    
    print(f"\nConversation history contains {len(messages)} messages including previous tool calls")
    
    # Test with parallel_tool_calls=False
    print(f"\nTest: parallel_tool_calls=False with history")
    print("-" * 60)
    
    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
    
    try:
        result = llm_with_tools.invoke(messages)
        
        print(f"Content: {result.content[:200] if result.content else '(none)'}...")
        print(f"Tool calls: {len(result.tool_calls) if result.tool_calls else 0}")
        
        if result.tool_calls:
            for i, tc in enumerate(result.tool_calls, 1):
                name = tc.get('name', 'Unknown')
                call_id = tc.get('id', 'Unknown')
                args = tc.get('args', {})
                print(f"  {i}. name: '{name}' (len={len(name)})")
                print(f"     id: '{call_id}' (len={len(call_id)})")
                print(f"     args: {args}")
                
                if len(name) > 30:
                    print(f"     ⚠️ WARNING: Tool name looks concatenated!")
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    test_realistic_market_analyst()
    test_with_conversation_history()
    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)

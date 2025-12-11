"""
Test NagaAI tool calling with LangGraph - mimics EXACT TradingAgents setup.

This test creates a LangGraph StateGraph with:
1. Analyst node (like market_analyst)
2. ToolNode (like tools_market)
3. Conditional routing based on tool_calls
4. Same prompt structure

This should help reproduce the concatenation bug.

Usage:
    .venv\Scripts\python.exe test_files\test_nagaai_langgraph.py
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Annotated, Sequence, TypedDict, List, Any
import operator

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
# EXACT same prompts from TradingAgents
# =============================================================================

MARKET_ANALYST_SYSTEM_PROMPT = """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. 

Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator.
- close_200_sma: 200 SMA: A long-term trend benchmark.
- close_10_ema: 10 EMA: A responsive short-term average.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs.
- macds: MACD Signal: An EMA smoothing of the MACD line.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line.
- atr: ATR: Averages true range to measure volatility.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume.

Please make sure to call get_ohlcv_data first to retrieve price data, then select appropriate indicators."""

ANALYST_COLLABORATION_PROMPT = """You are a helpful AI assistant, collaborating with other assistants. Use the provided tools to progress towards answering the question. Execute what you can to make progress. You have access to the following tools: {tool_names}.
{system_message}
For your reference, the current date is {current_date}."""


# =============================================================================
# Agent State (exactly like TradingAgents)
# =============================================================================

class AgentState(TypedDict):
    """State for the agent graph."""
    messages: Annotated[Sequence[Any], operator.add]
    company_of_interest: str
    trade_date: str
    market_report: str


# =============================================================================
# Main test using LangGraph
# =============================================================================

def test_with_langgraph():
    """Test with LangGraph StateGraph - exact same setup as TradingAgents."""
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langgraph.graph import END, StateGraph, START
    from langgraph.prebuilt import ToolNode
    
    print("\n" + "=" * 80)
    print("LANGGRAPH TEST - EXACT TRADINGAGENTS SETUP")
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
    tool_names = [t.name for t in tools]
    
    # Create LLM - EXACTLY like TradingAgents
    model_name = "grok-4.1-fast-reasoning"
    
    # Check TradingAgents uses streaming=True by default
    llm = ChatOpenAI(
        model=model_name,
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
        temperature=0,
        streaming=True,  # TradingAgents uses streaming!
    )
    
    # Also test without streaming
    llm_no_stream = ChatOpenAI(
        model=model_name,
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
        temperature=0,
        streaming=False,
    )
    
    # Test both streaming and non-streaming with parallel_tool_calls settings
    for streaming, test_llm in [(True, llm), (False, llm_no_stream)]:
      for parallel_tool_calls in [False]:
        print(f"\n{'='*60}")
        print(f"Testing with streaming={streaming}, parallel_tool_calls={parallel_tool_calls}")
        print(f"{'='*60}")
        
        # Create the analyst node (like create_market_analyst)
        def market_analyst_node(state: AgentState):
            """Market analyst node - exactly like TradingAgents."""
            current_date = state["trade_date"]
            ticker = state["company_of_interest"]
            
            # Format the prompt exactly like TradingAgents
            system_message = ANALYST_COLLABORATION_PROMPT.format(
                tool_names=", ".join(tool_names),
                system_message=MARKET_ANALYST_SYSTEM_PROMPT,
                current_date=current_date
            )
            
            prompt = ChatPromptTemplate.from_messages([
                ("system", system_message),
                MessagesPlaceholder(variable_name="messages"),
            ])
            
            # Bind tools with parallel_tool_calls parameter
            chain = prompt | test_llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)
            
            # Invoke the chain with the messages from state
            result = chain.invoke({"messages": state["messages"]})
            
            report = ""
            if len(result.tool_calls) == 0:
                report = result.content
            
            return {
                "messages": [result],
                "market_report": report,
            }
        
        # Create ToolNode
        tool_node = ToolNode(tools)
        
        # Conditional logic - exactly like TradingAgents
        def should_continue(state: AgentState):
            """Determine if analysis should continue."""
            messages = state["messages"]
            last_message = messages[-1]
            if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
                return "tools"
            return END
        
        # Create the graph
        workflow = StateGraph(AgentState)
        
        # Add nodes
        workflow.add_node("analyst", market_analyst_node)
        workflow.add_node("tools", tool_node)
        
        # Add edges
        workflow.add_edge(START, "analyst")
        workflow.add_conditional_edges(
            "analyst",
            should_continue,
            ["tools", END]
        )
        workflow.add_edge("tools", "analyst")
        
        # Compile the graph
        graph = workflow.compile()
        
        # Initial state
        init_state = {
            "messages": [HumanMessage(content="Analyze NVDA stock for trading. Get OHLCV data and calculate RSI, MACD, and Bollinger bands.")],
            "company_of_interest": "NVDA",
            "trade_date": "2025-12-10",
            "market_report": "",
        }
        
        # Run the graph with streaming (like TradingAgents debug mode)
        print("\nRunning graph with streaming...")
        step_count = 0
        
        try:
            for chunk in graph.stream(init_state):
                step_count += 1
                
                # Get the node name and its output
                for node_name, node_output in chunk.items():
                    print(f"\n--- Step {step_count}: {node_name} ---")
                    
                    if "messages" in node_output and node_output["messages"]:
                        last_msg = node_output["messages"][-1]
                        
                        if isinstance(last_msg, AIMessage):
                            print(f"AI Message content: {last_msg.content[:100] if last_msg.content else '(none)'}...")
                            
                            if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                                print(f"Tool calls: {len(last_msg.tool_calls)}")
                                for i, tc in enumerate(last_msg.tool_calls, 1):
                                    name = tc.get('name', 'Unknown')
                                    call_id = tc.get('id', 'Unknown')
                                    args = tc.get('args', {})
                                    print(f"  {i}. name: '{name}' (len={len(name)})")
                                    print(f"     id: '{call_id}' (len={len(call_id)})")
                                    print(f"     args: {args}")
                                    
                                    # Check for concatenation
                                    if len(name) > 30:
                                        print(f"     ⚠️ WARNING: Tool name looks concatenated!")
                        else:
                            msg_type = type(last_msg).__name__
                            content = str(last_msg.content)[:100] if hasattr(last_msg, 'content') else str(last_msg)[:100]
                            print(f"{msg_type}: {content}...")
                
                # Safety limit
                if step_count > 20:
                    print("\n⚠️ Safety limit reached (20 steps)")
                    break
                    
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
        
        print(f"\nCompleted {step_count} steps")


if __name__ == "__main__":
    test_with_langgraph()
    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)

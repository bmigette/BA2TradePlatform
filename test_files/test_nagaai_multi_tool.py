"""
Test multi-tool calling with conversation history - mimics TradingAgents behavior.

This test reproduces the exact scenario that causes tool name concatenation:
1. Multiple tools available (like market analyst)
2. System prompt asks to call multiple tools
3. Conversation history with previous tool calls

Usage:
    .venv\Scripts\python.exe test_files\test_nagaai_multi_tool.py
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
# Multiple Tool Definitions (similar to market analyst)
# =============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_ohlcv_data",
            "description": "Retrieve OHLCV (Open, High, Low, Close, Volume) price data for a stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol (optional, defaults to company being analyzed)"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_indicator_data",
            "description": "Calculate and retrieve technical indicators for a stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string", 
                        "description": "Stock ticker symbol (optional)"
                    },
                    "indicator": {
                        "type": "string",
                        "description": "Technical indicator name (e.g., 'rsi', 'macd', 'boll')"
                    }
                },
                "required": ["indicator"]
            }
        }
    }
]


def test_multi_tool_call(model: str, parallel_tool_calls: bool = False):
    """Test calling multiple tools in one response."""
    from openai import OpenAI
    
    print(f"\n{'='*80}")
    print(f"Testing: {model}")
    print(f"parallel_tool_calls: {parallel_tool_calls}")
    print(f"{'='*80}")
    
    model_name = model.split("/", 1)[1] if "/" in model else model
    
    client = OpenAI(
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
    )
    
    # System prompt that asks for MULTIPLE tool calls (like market analyst)
    system_prompt = """You are a Market Analyst. Your job is to analyze stock market data.

You have access to these tools:
- get_ohlcv_data: Get price data
- get_indicator_data: Get technical indicators (requires 'indicator' parameter)

IMPORTANT: You should retrieve BOTH price data AND at least 3 technical indicators (rsi, macd, boll) to do a complete analysis.

Call the tools now to gather the data you need."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Analyze NVDA stock."}
    ]
    
    try:
        # Make API call
        kwargs = {
            "model": model_name,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        
        # Add parallel_tool_calls if supported
        if parallel_tool_calls:
            kwargs["parallel_tool_calls"] = True
        
        response = client.chat.completions.create(**kwargs)
        
        message = response.choices[0].message
        
        print(f"\nResponse content: {message.content[:200] if message.content else '(none)'}...")
        
        if message.tool_calls:
            print(f"\nTool calls received: {len(message.tool_calls)}")
            for i, tc in enumerate(message.tool_calls, 1):
                print(f"  {i}. name: '{tc.function.name}'")
                print(f"     id: '{tc.id}'")
                print(f"     args: {tc.function.arguments}")
                
                # Check for concatenation
                if len(tc.function.name) > 30:
                    print(f"     ⚠️ WARNING: Tool name is suspiciously long!")
                    if "get_ohlcv_data" in tc.function.name and "get_indicator_data" in tc.function.name:
                        print(f"     🔴 CONCATENATION DETECTED!")
        else:
            print("\nNo tool calls in response")
            
    except Exception as e:
        print(f"\nError: {e}")


def test_with_langchain(model: str, parallel_tool_calls: bool = False):
    """Test with LangChain (same as TradingAgents uses)."""
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage, SystemMessage
    
    print(f"\n{'='*80}")
    print(f"Testing with LangChain: {model}")
    print(f"parallel_tool_calls: {parallel_tool_calls}")
    print(f"{'='*80}")
    
    model_name = model.split("/", 1)[1] if "/" in model else model
    
    # Create LangChain tools
    @tool
    def get_ohlcv_data(symbol: str = None) -> str:
        """Retrieve OHLCV price data for a stock."""
        return f"OHLCV data for {symbol or 'default'}"
    
    @tool
    def get_indicator_data(indicator: str, symbol: str = None) -> str:
        """Calculate technical indicators. indicator is REQUIRED."""
        return f"Indicator {indicator} for {symbol or 'default'}"
    
    tools = [get_ohlcv_data, get_indicator_data]
    
    llm = ChatOpenAI(
        model=model_name,
        api_key=NAGA_API_KEY,
        base_url=NAGA_BASE_URL,
        temperature=0,
    )
    
    # Bind tools with parallel_tool_calls setting
    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)
    
    system_prompt = """You are a Market Analyst. Your job is to analyze stock market data.

You have access to these tools:
- get_ohlcv_data: Get price data
- get_indicator_data: Get technical indicators (requires 'indicator' parameter)

IMPORTANT: You should retrieve BOTH price data AND at least 3 technical indicators (rsi, macd, boll) to do a complete analysis.

Call the tools now to gather the data you need."""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content="Analyze NVDA stock."),
    ]
    
    try:
        response = llm_with_tools.invoke(messages)
        
        print(f"\nResponse content: {response.content[:200] if response.content else '(none)'}...")
        
        if response.tool_calls:
            print(f"\nTool calls received: {len(response.tool_calls)}")
            for i, tc in enumerate(response.tool_calls, 1):
                name = tc.get("name", "?")
                tc_id = tc.get("id", "?")
                args = tc.get("args", {})
                print(f"  {i}. name: '{name}'")
                print(f"     id: '{tc_id}'")
                print(f"     args: {args}")
                
                # Check for concatenation
                if len(name) > 30:
                    print(f"     ⚠️ WARNING: Tool name is suspiciously long!")
                    if "get_ohlcv_data" in name and "get_indicator_data" in name:
                        print(f"     🔴 CONCATENATION DETECTED!")
        else:
            print("\nNo tool calls in response")
            
    except Exception as e:
        print(f"\nError: {e}")


def main():
    # Test with grok-4.1-fast-reasoning (the model causing issues)
    model = "NagaAC/grok-4.1-fast-reasoning"
    
    print("\n" + "=" * 80)
    print("MULTI-TOOL CALL TEST")
    print("Testing if NagaAI concatenates tool names when multiple tools are called")
    print("=" * 80)
    
    # Test 1: OpenAI API without parallel_tool_calls
    test_multi_tool_call(model, parallel_tool_calls=False)
    
    # Test 2: OpenAI API with parallel_tool_calls
    test_multi_tool_call(model, parallel_tool_calls=True)
    
    # Test 3: LangChain without parallel_tool_calls
    test_with_langchain(model, parallel_tool_calls=False)
    
    # Test 4: LangChain with parallel_tool_calls
    test_with_langchain(model, parallel_tool_calls=True)
    
    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()

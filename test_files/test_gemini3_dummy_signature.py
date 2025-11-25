"""
Test Gemini 3 with dummy thought_signature bypass.

This tests that our patch correctly adds the dummy "skip_thought_signature_validator"
signature to AIMessages with tool_calls, which allows Gemini 3 to work even when
we don't have real thought signatures from a previous Gemini response.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import Session, engine
from ba2_trade_platform.core.models import AppSetting
from sqlmodel import select

def get_app_setting(key: str) -> str | None:
    try:
        with Session(engine) as session:
            setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            return setting.value_str if setting else None
    except Exception as e:
        return None

naga_api_key = get_app_setting('naga_ai_api_key')

if not naga_api_key:
    print("ERROR: No naga_ai_api_key found in AppSettings")
    sys.exit(1)

# Apply the patch BEFORE importing LangChain
from ba2_trade_platform.core.gemini_patch import apply_gemini_toolmessage_patch, is_patch_applied
print("Applying Gemini patch...")
apply_gemini_toolmessage_patch()
print(f"Patch applied: {is_patch_applied()}")

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool

@tool
def get_stock_price(symbol: str) -> str:
    """Get the current stock price for a given symbol."""
    prices = {"AAPL": "$150.25", "GOOGL": "$2800.50", "MSFT": "$380.75"}
    return prices.get(symbol.upper(), f"Price for {symbol}: $100.00")

@tool
def calculate_moving_average(symbol: str, days: int) -> str:
    """Calculate the moving average for a stock."""
    return f"{days}-day MA for {symbol}: $145.00"

tools = [get_stock_price, calculate_moving_average]

print("\n" + "="*80)
print("Testing Gemini 3 Pro with Dummy Thought Signature")
print("="*80)

try:
    # Create Gemini 3 Pro model
    llm = ChatOpenAI(
        model="gemini-3-pro-preview",
        temperature=0,
        api_key=naga_api_key,
        base_url="https://api.naga.ac/v1"
    )
    llm_with_tools = llm.bind_tools(tools)
    
    # Test multi-turn conversation with function calling
    print("\n--- Turn 1: Initial request ---")
    messages = [HumanMessage(content="What's the stock price for AAPL?")]
    result1 = llm_with_tools.invoke(messages)
    
    print(f"Response content: {result1.content[:100] if result1.content else '(empty)'}...")
    print(f"Tool calls: {len(result1.tool_calls)}")
    
    if not result1.tool_calls:
        print("WARNING: Model didn't make a tool call, test may not be valid")
    else:
        # Execute the tool
        tool_call = result1.tool_calls[0]
        print(f"Tool: {tool_call['name']}, Args: {tool_call['args']}")
        tool_output = get_stock_price.invoke(tool_call['args'])
        
        # Create ToolMessage
        tool_msg = ToolMessage(
            content=str(tool_output),
            tool_call_id=tool_call['id'],
            name=tool_call['name']
        )
        
        print(f"Tool output: {tool_output}")
        
        # Turn 2: Send tool result back
        # This is where the dummy thought_signature should be added by our patch
        print("\n--- Turn 2: Sending tool result ---")
        messages = messages + [result1, tool_msg]
        messages.append(HumanMessage(content="Thanks! Should I buy it?"))
        
        print("Invoking model with conversation history (patch should add dummy signature)...")
        result2 = llm_with_tools.invoke(messages)
        
        print(f"Response: {result2.content[:200]}...")
        
        print("\n" + "="*80)
        print("[SUCCESS] Gemini 3 Pro worked with dummy thought_signature!")
        print("="*80)

except Exception as e:
    print("\n" + "="*80)
    print(f"[FAILED] Error: {str(e)[:500]}")
    print("="*80)
    import traceback
    traceback.print_exc()
    sys.exit(1)

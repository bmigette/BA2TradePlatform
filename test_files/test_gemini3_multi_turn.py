"""
Test Gemini 3 with multiple tool calls to debug the position 3 error.

This simulates the real scenario where multiple tools are called.
"""

import os
import sys
import json
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
print(f"Patch applied: {is_patch_applied()}\n")

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool

@tool
def get_ohlcv_data(symbol: str, period: str = "1d") -> str:
    """Get OHLCV data for a symbol."""
    return f"OHLCV data for {symbol} ({period}): Open=150, High=152, Low=149, Close=151"

@tool
def get_indicator_data(symbol: str, indicator: str) -> str:
    """Get technical indicator data."""
    return f"{indicator} for {symbol}: 45.2"

@tool
def get_news(symbol: str) -> str:
    """Get latest news for a symbol."""
    return f"Latest news for {symbol}: Company announces strong earnings"

tools = [get_ohlcv_data, get_indicator_data, get_news]

print("="*80)
print("Testing Gemini 3 Pro with Multiple Tool Calls")
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
    
    # Test 1: Initial request that should trigger multiple tool calls
    print("\n--- Turn 1: Request that may trigger multiple tools ---")
    messages = [HumanMessage(content="Get me OHLCV data, RSI indicator, and latest news for AAPL")]
    
    print("Invoking model...")
    result1 = llm_with_tools.invoke(messages)
    
    print(f"Response content: {result1.content[:100] if result1.content else '(empty)'}...")
    print(f"Tool calls: {len(result1.tool_calls)}")
    
    if result1.tool_calls:
        print("\nTool calls made:")
        for i, tc in enumerate(result1.tool_calls):
            print(f"  {i+1}. {tc['name']}({tc['args']})")
        
        # Execute all tools
        messages.append(result1)
        
        for tool_call in result1.tool_calls:
            # Execute the tool
            if tool_call['name'] == 'get_ohlcv_data':
                output = get_ohlcv_data.invoke(tool_call['args'])
            elif tool_call['name'] == 'get_indicator_data':
                output = get_indicator_data.invoke(tool_call['args'])
            elif tool_call['name'] == 'get_news':
                output = get_news.invoke(tool_call['args'])
            else:
                output = f"Unknown tool: {tool_call['name']}"
            
            tool_msg = ToolMessage(
                content=str(output),
                tool_call_id=tool_call['id'],
                name=tool_call['name']
            )
            messages.append(tool_msg)
        
        print(f"\nExecuted {len(result1.tool_calls)} tools")
        
        # Turn 2: Continue conversation
        print("\n--- Turn 2: Asking follow-up question ---")
        messages.append(HumanMessage(content="Based on this data, what's your analysis?"))
        
        print(f"Conversation has {len(messages)} messages")
        print("Message structure:")
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls'):
                print(f"  {i}: {msg_type} with {len(msg.tool_calls)} tool_calls")
            else:
                print(f"  {i}: {msg_type}")
        
        print("\nInvoking model with full conversation history...")
        result2 = llm_with_tools.invoke(messages)
        
        print(f"\nResponse: {result2.content[:200]}...")
        
        print("\n" + "="*80)
        print("[SUCCESS] Multi-turn conversation with multiple tools worked!")
        print("="*80)
    else:
        print("WARNING: Model didn't make tool calls, trying simpler request...")
        # Fallback: sequential tool calls
        print("\n--- Trying sequential tool calls ---")
        messages = [HumanMessage(content="Get OHLCV data for AAPL")]
        result1 = llm_with_tools.invoke(messages)
        
        if result1.tool_calls:
            tc1 = result1.tool_calls[0]
            output1 = get_ohlcv_data.invoke(tc1['args'])
            messages.extend([
                result1,
                ToolMessage(content=output1, tool_call_id=tc1['id'], name=tc1['name']),
                HumanMessage(content="Now get RSI for AAPL")
            ])
            
            result2 = llm_with_tools.invoke(messages)
            if result2.tool_calls:
                tc2 = result2.tool_calls[0]
                output2 = get_indicator_data.invoke(tc2['args'])
                messages.extend([
                    result2,
                    ToolMessage(content=output2, tool_call_id=tc2['id'], name=tc2['name']),
                    HumanMessage(content="What do you think?")
                ])
                
                print(f"Now have {len(messages)} messages in conversation")
                result3 = llm_with_tools.invoke(messages)
                print(f"Final response: {result3.content[:200]}...")
                print("\n[SUCCESS] Sequential multi-turn worked!")

except Exception as e:
    print("\n" + "="*80)
    print(f"[FAILED] Error: {str(e)[:500]}")
    print("="*80)
    import traceback
    traceback.print_exc()
    sys.exit(1)

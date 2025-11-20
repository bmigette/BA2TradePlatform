"""
Minimal test case to reproduce Gemini 3 Pro Preview ToolMessage 'name' field issue.

This test demonstrates the error:
"GenerateContentRequest.contents[N].parts[0].function_response.name: Name cannot be empty"

The issue occurs because LangChain's ToolNode creates ToolMessage objects without
the 'name' field, which Gemini requires.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

# Add parent directory to path to import from ba2_trade_platform
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables from creds.env
env_path = Path(__file__).parent.parent / "creds.env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"Loaded environment from: {env_path}")
else:
    load_dotenv()
    print("Loaded environment from .env")

# Import database helpers to get app settings
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

# Get API keys from app settings (database) or fall back to environment
naga_api_key = get_app_setting('naga_ai_api_key') or os.getenv("OPENAI_API_KEY")
openai_api_key = get_app_setting('openai_api_key') or os.getenv("OPENAI_API_KEY")
naga_base_url = "https://api.naga.ac/v1"

print(f"NagaAI API Key loaded: {'Yes' if naga_api_key else 'No'}")
print(f"OpenAI API Key loaded: {'Yes' if openai_api_key else 'No'}")

# Define a simple tool
@tool
def get_stock_price(symbol: str) -> str:
    """Get the current stock price for a given symbol."""
    # Mock implementation
    prices = {
        "AAPL": "$150.25",
        "GOOGL": "$2800.50",
        "MSFT": "$380.75"
    }
    return prices.get(symbol.upper(), f"Price for {symbol}: $100.00")

@tool
def calculate_moving_average(symbol: str, days: int) -> str:
    """Calculate the moving average for a stock over N days."""
    # Mock implementation
    return f"{days}-day moving average for {symbol}: $145.30"

# Set up tools
tools = [get_stock_price, calculate_moving_average]

print("=" * 80)
print("Testing Gemini 3 Pro Preview with Function Calling")
print("=" * 80)

# Test 1: Using a regular OpenAI model (works fine)
print("\n--- Test 1: OpenAI GPT-4o (Control - Should Work) ---")
try:
    if not openai_api_key:
        print("[WARN]  OpenAI API key not configured, skipping OpenAI test")
    else:
        llm_openai = ChatOpenAI(
            model="gpt-4o",
            temperature=0,
            api_key=openai_api_key,
        )
    llm_with_tools_openai = llm_openai.bind_tools(tools)
    
    # Invoke with a request that triggers tool use
    messages = [HumanMessage(content="What's the stock price for AAPL?")]
    result = llm_with_tools_openai.invoke(messages)
    
    print(f"AI Response: {result.content}")
    print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
    
    if result.tool_calls:
        # Execute tools manually instead of using ToolNode
        tool_messages = []
        for tool_call in result.tool_calls:
            tool_name = tool_call['name']
            tool_args = tool_call['args']
            tool_id = tool_call['id']
            
            # Find and execute the tool
            tool_func = None
            for t in tools:
                if t.name == tool_name:
                    tool_func = t
                    break
            
            if tool_func:
                tool_output = tool_func.invoke(tool_args)
                # Create ToolMessage WITHOUT name field (this is what LangChain's ToolNode does)
                tool_msg = ToolMessage(
                    content=str(tool_output),
                    tool_call_id=tool_id
                    # Note: 'name' field is NOT set here
                )
                tool_messages.append(tool_msg)
                print(f"  - ToolMessage.name: '{tool_msg.name if hasattr(tool_msg, 'name') and tool_msg.name else 'MISSING'}'")
                print(f"  - ToolMessage.content: {tool_output}")
        
        print(f"Tool Results: {len(tool_messages)} message(s)")
        print("[OK] OpenAI test passed (tools executed)")
    
except Exception as e:
    print(f"[FAIL] OpenAI test failed: {e}")

# Test 2: Using Gemini 3 Pro Preview via NagaAI (should fail with name error)
print("\n--- Test 2: Gemini 3 Pro Preview via NagaAI (Expected to Fail) ---")
try:
    if not naga_api_key:
        print("[WARN]  NagaAI API key not configured, skipping Gemini test")
    else:
        llm_gemini = ChatOpenAI(
            model="gemini-3-pro-preview",
            temperature=0,
            api_key=naga_api_key,
            base_url=naga_base_url,
        )
        llm_with_tools_gemini = llm_gemini.bind_tools(tools)
        
        # Invoke with a request that triggers tool use
        messages = [HumanMessage(content="What's the stock price for GOOGL?")]
        result = llm_with_tools_gemini.invoke(messages)
        
        print(f"AI Response: {result.content}")
        print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
        
        if result.tool_calls:
            # Execute tools manually
            tool_messages = []
            for tool_call in result.tool_calls:
                tool_name = tool_call['name']
                tool_args = tool_call['args']
                tool_id = tool_call['id']
                
                # Find and execute the tool
                tool_func = None
                for t in tools:
                    if t.name == tool_name:
                        tool_func = t
                        break
                
                if tool_func:
                    tool_output = tool_func.invoke(tool_args)
                    # Create ToolMessage WITHOUT name field
                    tool_msg = ToolMessage(
                        content=str(tool_output),
                        tool_call_id=tool_id
                    )
                    tool_messages.append(tool_msg)
                    print(f"  - Tool executed: {tool_name} -> {tool_output}")
                    print(f"  - ToolMessage.name: '{tool_msg.name if hasattr(tool_msg, 'name') and tool_msg.name else 'MISSING'}'")
            
            print(f"\nTool Results: {len(tool_messages)} message(s)")
            
            # Now try to send the ToolMessages back to Gemini (this is where it fails)
            print("\nSending ToolMessages back to Gemini...")
            conversation = messages + [result] + tool_messages
            
            # Add follow-up message
            conversation.append(HumanMessage(content="Based on the price, should I buy?"))
            
            final_result = llm_with_tools_gemini.invoke(conversation)
            print(f"Final Response: {final_result.content}")
            print("[OK] Gemini test passed (unexpected!)")
        
except Exception as e:
    import traceback
    print(f"[FAIL] Gemini test failed with error:")
    print(f"Error type: {type(e).__name__}")
    print(f"Error message: {str(e)}")
    print("\nFull traceback:")
    traceback.print_exc()
    
    if "Name cannot be empty" in str(e):
        print("\n[INFO] ISSUE CONFIRMED: Gemini requires 'name' field in ToolMessages!")
        print("   LangChain's ToolNode doesn't populate the 'name' field by default.")

# Test 3: Gemini with manually fixed ToolMessages (workaround)
print("\n--- Test 3: Gemini with Fixed ToolMessages (Workaround) ---")
try:
    if not naga_api_key:
        print("[WARN]  NagaAI API key not configured, skipping Gemini workaround test")
    else:
        llm_gemini = ChatOpenAI(
            model="gemini-3-pro-preview",
            temperature=0,
            api_key=naga_api_key,
            base_url=naga_base_url,
        )
        llm_with_tools_gemini = llm_gemini.bind_tools(tools)
        
        # Invoke with a request that triggers tool use
        messages = [HumanMessage(content="What's the stock price for MSFT?")]
        result = llm_with_tools_gemini.invoke(messages)
        
        print(f"AI Response: {result.content}")
        print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
        
        if result.tool_calls:
            # Execute tools manually
            tool_messages = []
            for tool_call in result.tool_calls:
                tool_name = tool_call['name']
                tool_args = tool_call['args']
                tool_id = tool_call['id']
                
                # Find and execute the tool
                tool_func = None
                for t in tools:
                    if t.name == tool_name:
                        tool_func = t
                        break
                
                if tool_func:
                    tool_output = tool_func.invoke(tool_args)
                    # Create ToolMessage WITHOUT name field (simulating LangChain ToolNode)
                    tool_msg = ToolMessage(
                        content=str(tool_output),
                        tool_call_id=tool_id
                    )
                    tool_messages.append(tool_msg)
            
            print(f"Tool Results: {len(tool_messages)} message(s) WITHOUT 'name' field")
            
            # FIX: Manually add 'name' field to ToolMessages
            fixed_messages = []
            for msg in tool_messages:
                if isinstance(msg, ToolMessage):
                    # Find the tool name from the original AIMessage
                    tool_name = None
                    for tc in result.tool_calls:
                        if tc.get('id') == msg.tool_call_id:
                            tool_name = tc.get('name', 'unknown_tool')
                            break
                    
                    # Create new ToolMessage with name
                    fixed_msg = ToolMessage(
                        content=msg.content,
                        tool_call_id=msg.tool_call_id,
                        name=tool_name,
                    )
                    fixed_messages.append(fixed_msg)
                    print(f"  - Fixed ToolMessage.name: '{tool_name}' (has name attr: {hasattr(fixed_msg, 'name')}, value: '{getattr(fixed_msg, 'name', 'N/A')}')")
                else:
                    fixed_messages.append(msg)
            
            # Build conversation with fixed messages
            conversation = messages + [result] + fixed_messages
            conversation.append(HumanMessage(content="Based on the price, should I buy?"))
            
            print("\nSending fixed ToolMessages back to Gemini...")
            final_result = llm_with_tools_gemini.invoke(conversation)
            print(f"Final Response: {final_result.content[:200]}...")
            print("[OK] Gemini workaround test passed!")
        
except Exception as e:
    import traceback
    print(f"[FAIL] Gemini workaround test failed:")
    print(f"Error: {str(e)}")
    traceback.print_exc()

print("\n" + "=" * 80)
print("Test Complete")
print("=" * 80)


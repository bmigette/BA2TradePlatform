"""
Minimal test case to reproduce Gemini 3 Pro Preview ToolMessage 'name' field issue.

This test demonstrates the error:
"GenerateContentRequest.contents[N].parts[0].function_response.name: Name cannot be empty"

The issue occurs because LangChain's ToolNode creates ToolMessage objects without
the 'name' field, which Gemini requires.
"""

import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

# Load environment variables
load_dotenv()

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
    llm_openai = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
    )
    llm_with_tools_openai = llm_openai.bind_tools(tools)
    
    # Invoke with a request that triggers tool use
    messages = [HumanMessage(content="What's the stock price for AAPL?")]
    result = llm_with_tools_openai.invoke(messages)
    
    print(f"AI Response: {result.content}")
    print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
    
    if result.tool_calls:
        # Use ToolNode to execute tools
        tool_node = ToolNode(tools)
        tool_result = tool_node.invoke({"messages": [result]})
        
        print(f"Tool Results: {len(tool_result['messages'])} message(s)")
        for msg in tool_result['messages']:
            if isinstance(msg, ToolMessage):
                print(f"  - ToolMessage.name: '{msg.name}'")
                print(f"  - ToolMessage.content: {msg.content[:100]}...")
        print("✅ OpenAI test passed")
    
except Exception as e:
    print(f"❌ OpenAI test failed: {e}")

# Test 2: Using Gemini 3 Pro Preview via NagaAI (should fail with name error)
print("\n--- Test 2: Gemini 3 Pro Preview via NagaAI (Expected to Fail) ---")
try:
    # Get NagaAI API key and base URL
    naga_api_key = os.getenv("NAGAAI_API_KEY") or os.getenv("NAGAI_API_KEY")
    if not naga_api_key:
        print("⚠️  NAGAAI_API_KEY not found in environment, skipping Gemini test")
    else:
        llm_gemini = ChatOpenAI(
            model="gemini-3-pro-preview",
            temperature=0,
            api_key=naga_api_key,
            base_url="https://api.naga.ac/v1",
        )
        llm_with_tools_gemini = llm_gemini.bind_tools(tools)
        
        # Invoke with a request that triggers tool use
        messages = [HumanMessage(content="What's the stock price for GOOGL?")]
        result = llm_with_tools_gemini.invoke(messages)
        
        print(f"AI Response: {result.content}")
        print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
        
        if result.tool_calls:
            # Use ToolNode to execute tools
            tool_node = ToolNode(tools)
            
            # This should fail with Gemini's name error
            print("\nExecuting tools via ToolNode...")
            tool_result = tool_node.invoke({"messages": [result]})
            
            print(f"Tool Results: {len(tool_result['messages'])} message(s)")
            for msg in tool_result['messages']:
                if isinstance(msg, ToolMessage):
                    print(f"  - ToolMessage.name: '{msg.name}'")
                    print(f"  - ToolMessage.content: {msg.content[:100]}...")
            
            # Now try to send the ToolMessages back to Gemini (this is where it fails)
            print("\nSending ToolMessages back to Gemini...")
            conversation = messages + [result] + tool_result['messages']
            
            # Add follow-up message
            conversation.append(HumanMessage(content="Based on the price, should I buy?"))
            
            final_result = llm_with_tools_gemini.invoke(conversation)
            print(f"Final Response: {final_result.content}")
            print("✅ Gemini test passed (unexpected!)")
        
except Exception as e:
    import traceback
    print(f"❌ Gemini test failed with error:")
    print(f"Error type: {type(e).__name__}")
    print(f"Error message: {str(e)}")
    print("\nFull traceback:")
    traceback.print_exc()
    
    if "Name cannot be empty" in str(e):
        print("\n🔍 ISSUE CONFIRMED: Gemini requires 'name' field in ToolMessages!")
        print("   LangChain's ToolNode doesn't populate the 'name' field by default.")

# Test 3: Gemini with manually fixed ToolMessages (workaround)
print("\n--- Test 3: Gemini with Fixed ToolMessages (Workaround) ---")
try:
    naga_api_key = os.getenv("NAGAAI_API_KEY") or os.getenv("NAGAI_API_KEY")
    if not naga_api_key:
        print("⚠️  NAGAAI_API_KEY not found in environment, skipping Gemini workaround test")
    else:
        llm_gemini = ChatOpenAI(
            model="gemini-3-pro-preview",
            temperature=0,
            api_key=naga_api_key,
            base_url="https://api.naga.ac/v1",
        )
        llm_with_tools_gemini = llm_gemini.bind_tools(tools)
        
        # Invoke with a request that triggers tool use
        messages = [HumanMessage(content="What's the stock price for MSFT?")]
        result = llm_with_tools_gemini.invoke(messages)
        
        print(f"AI Response: {result.content}")
        print(f"Tool Calls: {len(result.tool_calls)} tool(s) called")
        
        if result.tool_calls:
            # Use ToolNode to execute tools
            tool_node = ToolNode(tools)
            tool_result = tool_node.invoke({"messages": [result]})
            
            # FIX: Manually add 'name' field to ToolMessages
            fixed_messages = []
            for msg in tool_result['messages']:
                if isinstance(msg, ToolMessage) and not msg.name:
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
                    print(f"  - Fixed ToolMessage.name: '{tool_name}'")
                else:
                    fixed_messages.append(msg)
            
            # Build conversation with fixed messages
            conversation = messages + [result] + fixed_messages
            conversation.append(HumanMessage(content="Based on the price, should I buy?"))
            
            print("\nSending fixed ToolMessages back to Gemini...")
            final_result = llm_with_tools_gemini.invoke(conversation)
            print(f"Final Response: {final_result.content[:200]}...")
            print("✅ Gemini workaround test passed!")
        
except Exception as e:
    import traceback
    print(f"❌ Gemini workaround test failed:")
    print(f"Error: {str(e)}")
    traceback.print_exc()

print("\n" + "=" * 80)
print("Test Complete")
print("=" * 80)

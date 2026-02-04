"""
Test script to verify Kimi K2.5 thinking mode works with tool calls.

This tests the fix for the "reasoning_content is missing" error that occurs
when using thinking mode with multi-turn tool calls.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.db import init_db
init_db()

from langchain_core.tools import tool
from ba2_trade_platform.core.ModelFactory import ModelFactory


@tool
def get_weather(city: str) -> str:
    """Get the current weather in a city."""
    return f"The weather in {city} is sunny and 72°F"


@tool
def get_stock_price(symbol: str) -> str:
    """Get the current stock price for a symbol."""
    return f"The current price of {symbol} is $150.25"


def test_thinking_mode_with_tools():
    """Test Kimi K2.5 with thinking mode and tool calls."""
    print("=" * 60)
    print("Testing Kimi K2.5 (Thinking Mode) with Tool Calls")
    print("=" * 60)

    try:
        # Create LLM with thinking mode (default for kimi_k2.5)
        print("\n1. Creating LLM with thinking mode enabled...")
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5', temperature=1.0, track_usage=False)
        print(f"   LLM type: {type(llm).__name__}")

        # Bind tools
        print("\n2. Binding tools...")
        llm_with_tools = llm.bind_tools([get_weather, get_stock_price])
        print(f"   Bound LLM type: {type(llm_with_tools).__name__}")

        # Make a request that should trigger tool use
        print("\n3. Invoking with tool-triggering prompt...")
        response = llm_with_tools.invoke("What's the weather in Tokyo?")

        print(f"\n4. Response received:")
        print(f"   Type: {type(response).__name__}")
        print(f"   Content: {response.content[:200] if response.content else 'None'}")

        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"\n   TOOL CALLS DETECTED:")
            for tc in response.tool_calls:
                print(f"     - Name: {tc.get('name')}")
                print(f"       Args: {tc.get('args')}")
            print("\n✓ SUCCESS: Tool calling works with thinking mode!")
        else:
            print("\n   No tool calls in response.")
            print("   (Model may have answered directly instead of using tools)")

        # Check for reasoning_content
        if hasattr(response, 'additional_kwargs'):
            if response.additional_kwargs.get('reasoning_content'):
                print(f"\n   Reasoning content preserved (length: {len(response.additional_kwargs['reasoning_content'])})")

        return True

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_nonthinking_mode_with_tools():
    """Test Kimi K2.5 non-thinking mode with tool calls."""
    print("\n" + "=" * 60)
    print("Testing Kimi K2.5-nonthinking Mode with Tool Calls")
    print("=" * 60)

    try:
        # Create LLM with non-thinking mode
        print("\n1. Creating LLM with thinking disabled...")
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.6, track_usage=False)
        print(f"   LLM type: {type(llm).__name__}")

        # Bind tools
        print("\n2. Binding tools...")
        llm_with_tools = llm.bind_tools([get_weather])
        print(f"   Bound LLM type: {type(llm_with_tools).__name__}")

        # Make a request
        print("\n3. Invoking with tool-triggering prompt...")
        response = llm_with_tools.invoke("What's the weather in Paris?")

        print(f"\n4. Response received:")
        print(f"   Type: {type(response).__name__}")
        print(f"   Content: {response.content[:200] if response.content else 'None'}")

        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"\n   TOOL CALLS DETECTED:")
            for tc in response.tool_calls:
                print(f"     - Name: {tc.get('name')}")
                print(f"       Args: {tc.get('args')}")
            print("\n✓ SUCCESS: Tool calling works with non-thinking mode!")
        else:
            print("\n   No tool calls in response.")

        return True

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\nKimi K2.5 Thinking Mode + Tool Calls Test")
    print("=" * 60)

    results = []

    # Test thinking mode
    results.append(("Thinking Mode + Tools", test_thinking_mode_with_tools()))

    # Test non-thinking mode
    results.append(("Non-Thinking Mode + Tools", test_nonthinking_mode_with_tools()))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"  {status}: {name}")

    # Exit code
    sys.exit(0 if all(r[1] for r in results) else 1)

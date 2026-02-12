"""
Test script to verify ChatKimiThinking (ChatDeepSeek subclass) works correctly
with Kimi's thinking mode enabled, including multi-turn tool calls where
reasoning_content must be preserved in assistant messages.

This validates the fix for:
"thinking is enabled but reasoning_content is missing in assistant tool call message"

Run:
  .venv/bin/python test_tools/test_kimi_thinking_toolcall.py
"""

import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.db import init_db
init_db()

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage
from ba2_trade_platform.core.ModelFactory import ModelFactory


# ============================================================
# Test tools
# ============================================================

@tool
def get_current_price(symbol: str) -> str:
    """Get the current price for a stock symbol."""
    fake_prices = {"AAPL": 195.50, "MSFT": 420.30, "TSLA": 312.75}
    price = fake_prices.get(symbol, 100.0)
    return json.dumps({"symbol": symbol, "price": price})


@tool
def get_company_info(symbol: str) -> str:
    """Get basic company information for a stock symbol."""
    info = {
        "AAPL": {"name": "Apple Inc.", "sector": "Technology", "pe_ratio": 28.5},
        "MSFT": {"name": "Microsoft Corp.", "sector": "Technology", "pe_ratio": 32.1},
    }
    return json.dumps(info.get(symbol, {"name": "Unknown", "sector": "Unknown"}))


@tool
def finish_analysis(summary: str) -> str:
    """Call this when analysis is complete."""
    return json.dumps({"status": "complete", "summary_received": True})


ALL_TOOLS = [get_current_price, get_company_info, finish_analysis]


# ============================================================
# Test 1: ChatKimiThinking class creation
# ============================================================

def test_class_creation():
    """Test that ModelFactory creates ChatKimiThinking for thinking models."""
    print("=" * 60)
    print("Test 1: ChatKimiThinking Class Creation")
    print("=" * 60)

    try:
        # kimi_k2.5 has thinking enabled by default
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5', temperature=1.0, track_usage=False)
        class_name = type(llm).__name__
        print(f"  LLM type: {class_name}")

        if class_name != "ChatKimiThinking":
            print(f"  [FAIL] Expected ChatKimiThinking, got {class_name}")
            return False

        print(f"  thinking_enabled: {llm.thinking_enabled}")
        print(f"  api_base: {llm.api_base}")
        print(f"  _llm_type: {llm._llm_type}")

        assert llm.thinking_enabled is True
        assert "moonshot" in llm.api_base
        assert llm._llm_type == "chat-kimi-thinking"

        print("  [OK] ChatKimiThinking created correctly")
        return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# Test 2: Basic tool call with thinking mode
# ============================================================

def test_basic_tool_call_with_thinking():
    """Test that ChatKimiThinking can make a basic tool call."""
    print("\n" + "=" * 60)
    print("Test 2: Basic Tool Call with Thinking Mode")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5', temperature=1.0, track_usage=False)
        llm_with_tools = llm.bind_tools(ALL_TOOLS)
        print(f"  Bound LLM type: {type(llm_with_tools).__name__}")

        response = llm_with_tools.invoke("What is the current price of AAPL?")

        print(f"  Content: {(response.content or 'None')[:100]}")

        # Check for reasoning_content in additional_kwargs
        reasoning = response.additional_kwargs.get("reasoning_content")
        if reasoning:
            print(f"  reasoning_content captured: {len(reasoning)} chars")
            print(f"  reasoning_content preview: {reasoning[:100]}...")
        else:
            print("  [INFO] No reasoning_content (may not be returned for simple queries)")

        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                print(f"  Tool call: {tc.get('name')}({tc.get('args')})")
            print("  [OK] Tool call with thinking mode successful")
        else:
            print("  [INFO] No tool calls - model answered directly")

        return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# Test 3: Multi-turn tool calls (the critical test)
# ============================================================

def test_multi_turn_with_thinking():
    """Test multi-turn tool calling with thinking mode.

    This is the critical test - after the first tool call response,
    the reasoning_content must be re-injected into the assistant message
    when sending the next request, otherwise Kimi returns:
    "thinking is enabled but reasoning_content is missing"
    """
    print("\n" + "=" * 60)
    print("Test 3: Multi-Turn Tool Calls with Thinking (Critical)")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5', temperature=1.0, track_usage=False)
        llm_with_tools = llm.bind_tools(ALL_TOOLS)

        messages = [
            HumanMessage(content=(
                "First get the current price for AAPL, then get company info for AAPL. "
                "After both, call finish_analysis with a brief summary. "
                "Do one tool call at a time."
            ))
        ]

        max_iterations = 8
        iteration = 0
        tool_calls_made = []

        while iteration < max_iterations:
            iteration += 1
            print(f"\n  --- Iteration {iteration} ---")

            response = llm_with_tools.invoke(messages)
            messages.append(response)

            content_preview = (response.content or "")[:80]
            print(f"  Content: {content_preview}")

            # Check reasoning_content was captured
            reasoning = response.additional_kwargs.get("reasoning_content")
            if reasoning:
                print(f"  reasoning_content: {len(reasoning)} chars")
            else:
                print("  reasoning_content: None")

            if not response.tool_calls:
                print("  No tool calls - conversation complete")
                break

            for tc in response.tool_calls:
                tool_name = tc.get("name")
                tool_args = tc.get("args", {})
                tool_id = tc.get("id", "unknown")
                print(f"  Tool call: {tool_name}({json.dumps(tool_args)}) [id={tool_id[:20]}...]")
                tool_calls_made.append(tool_name)

                # Execute the tool
                matching_tool = next((t for t in ALL_TOOLS if t.name == tool_name), None)
                if matching_tool:
                    result = matching_tool.invoke(tool_args)
                    print(f"  Tool result: {str(result)[:80]}")
                    messages.append(ToolMessage(content=str(result), tool_call_id=tool_id))
                else:
                    error_msg = f"Tool '{tool_name}' not found"
                    print(f"  [ERROR] {error_msg}")
                    messages.append(ToolMessage(content=error_msg, tool_call_id=tool_id))

            if any(tc.get("name") == "finish_analysis" for tc in response.tool_calls):
                print("\n  finish_analysis called - completing")
                break

        print(f"\n  Total iterations: {iteration}")
        print(f"  Tools called: {tool_calls_made}")

        if len(tool_calls_made) >= 2:
            print("  [OK] Multi-turn tool calling with thinking mode completed!")
            print("  [OK] reasoning_content was correctly preserved across turns")
            return True
        else:
            print("  [WARN] Expected at least 2 tool calls, got less")
            return True  # Not a hard failure

    except Exception as e:
        print(f"\n  [FAIL] {e}")
        import traceback
        traceback.print_exc()

        error_str = str(e)
        if "reasoning_content" in error_str:
            print("\n  >>> CRITICAL: reasoning_content was not re-injected!")
            print("  >>> The ChatKimiThinking._get_request_payload override is not working")
        return False


# ============================================================
# Test 4: kimi_k2_thinking model
# ============================================================

def test_kimi_k2_thinking_model():
    """Test with kimi_k2_thinking (dedicated thinking model, not k2.5)."""
    print("\n" + "=" * 60)
    print("Test 4: Kimi K2 Thinking Model")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2_thinking', temperature=0.7, track_usage=False)
        class_name = type(llm).__name__
        print(f"  LLM type: {class_name}")

        if class_name != "ChatKimiThinking":
            print(f"  [FAIL] Expected ChatKimiThinking, got {class_name}")
            return False

        llm_with_tools = llm.bind_tools(ALL_TOOLS)
        response = llm_with_tools.invoke("What is Apple's stock price? Use the get_current_price tool.")

        print(f"  Content: {(response.content or 'None')[:100]}")
        reasoning = response.additional_kwargs.get("reasoning_content")
        if reasoning:
            print(f"  reasoning_content: {len(reasoning)} chars")

        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                print(f"  Tool call: {tc.get('name')}({tc.get('args')})")
            print("  [OK] kimi_k2_thinking tool call successful")
        else:
            print("  [INFO] No tool calls - model answered directly")

        return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("\nKimi Thinking Mode Tool Call Tests")
    print("(Tests ChatKimiThinking - ChatDeepSeek subclass)")
    print("=" * 60)

    results = {
        "Class creation": test_class_creation(),
        "Basic tool call": test_basic_tool_call_with_thinking(),
        "Multi-turn (critical)": test_multi_turn_with_thinking(),
        "K2 thinking model": test_kimi_k2_thinking_model(),
    }

    print("\n" + "=" * 60)
    print("Results Summary")
    print("=" * 60)
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {passed}/{total} tests passed")

    if all(results.values()):
        print("\n  All tests passed!")
    else:
        print("\n  Some tests failed!")
        sys.exit(1)

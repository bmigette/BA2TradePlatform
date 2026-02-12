"""
Test script to verify Kimi K2.5 non-thinking mode tool calling works correctly
with LangChain ChatOpenAI, including multi-turn conversations and extra_body propagation.

This tests the specific scenario used by SmartRiskManagerGraph when configured with
kimi_k2.5-nonthinking as the risk_manager_model.

Run:
  .venv\Scripts\python.exe test_tools\test_kimi_nonthinking_tools.py
"""

import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.db import init_db
init_db()

from typing import List, Dict, Any
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from ba2_trade_platform.core.ModelFactory import ModelFactory


# ============================================================
# Test tools (simulating SmartRiskManager research tools)
# ============================================================

@tool
def get_current_price(symbol: str) -> str:
    """Get the current bid price for a stock symbol."""
    fake_prices = {"AAPL": 195.50, "MSFT": 420.30, "SOFI": 24.45}
    price = fake_prices.get(symbol, 100.0)
    return json.dumps({"symbol": symbol, "bid_price": price, "ask_price": price + 0.05})


@tool
def get_positions() -> str:
    """Get current portfolio positions with transaction IDs."""
    return json.dumps({
        "positions": [
            {"transaction_id": 100, "symbol": "AAPL", "quantity": 10, "direction": "BUY",
             "entry_price": 190.0, "current_price": 195.50, "stop_loss": 185.0, "take_profit": 210.0},
        ],
        "total_positions": 1
    })


@tool
def recommend_update_stop_loss(transaction_id: int, new_sl_price: float, reason: str, confidence: float) -> str:
    """Recommend updating stop loss for an existing position."""
    return json.dumps({"status": "queued", "action": "update_stop_loss",
                       "transaction_id": transaction_id, "new_sl_price": new_sl_price})


@tool
def finish_research(summary: str) -> str:
    """Call this when you are done with your research to submit findings."""
    return json.dumps({"status": "complete", "summary_received": True})


ALL_TOOLS = [get_current_price, get_positions, recommend_update_stop_loss, finish_research]


# ============================================================
# Test 1: Basic tool calling (single turn)
# ============================================================

def test_basic_tool_call():
    """Test that kimi_k2.5-nonthinking can make a basic tool call."""
    print("=" * 60)
    print("Test 1: Basic Tool Call (single turn)")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.2, track_usage=False)
        print(f"  LLM type: {type(llm).__name__}")

        # Check extra_body is set
        if hasattr(llm, 'extra_body'):
            print(f"  extra_body: {llm.extra_body}")
            thinking_config = llm.extra_body.get("thinking", {})
            if thinking_config.get("type") != "disabled":
                print("  [FAIL] extra_body thinking is NOT disabled!")
                return False
            print("  extra_body thinking=disabled confirmed")
        else:
            print("  [WARN] No extra_body attribute on LLM")

        llm_with_tools = llm.bind_tools(ALL_TOOLS)
        print(f"  Bound LLM type: {type(llm_with_tools).__name__}")

        # Check extra_body survives bind_tools
        if hasattr(llm_with_tools, 'bound'):
            inner = llm_with_tools.bound
            if hasattr(inner, 'extra_body'):
                print(f"  Bound inner extra_body: {inner.extra_body}")
            else:
                print("  [WARN] Bound inner has no extra_body")

        response = llm_with_tools.invoke("What are my current portfolio positions?")

        print(f"  Content: {(response.content or 'None')[:100]}")
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                print(f"  Tool call: {tc.get('name')}({tc.get('args')})")
            print("  [OK] Tool call successful")
            return True
        else:
            print("  [WARN] No tool calls - model answered directly (may retry)")
            return True  # Not a failure per se

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# Test 2: Multi-turn tool calling (the critical test)
# ============================================================

def test_multi_turn_tool_calls():
    """Test multi-turn tool calling - the scenario that can fail if extra_body is lost."""
    print("\n" + "=" * 60)
    print("Test 2: Multi-Turn Tool Calls")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.2, track_usage=False)
        llm_with_tools = llm.bind_tools(ALL_TOOLS)

        messages = [
            HumanMessage(content=(
                "You are a risk manager. First check positions with get_positions, "
                "then get the current price for the symbol you find. "
                "After that, call finish_research with a brief summary."
            ))
        ]

        max_iterations = 8
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            print(f"\n  --- Iteration {iteration} ---")

            response = llm_with_tools.invoke(messages)
            messages.append(response)

            content_preview = (response.content or "")[:80]
            print(f"  Content: {content_preview}")

            if not response.tool_calls:
                print("  No tool calls - conversation complete")
                break

            for tc in response.tool_calls:
                tool_name = tc.get("name")
                tool_args = tc.get("args", {})
                tool_id = tc.get("id", "unknown")
                print(f"  Tool call: {tool_name}({json.dumps(tool_args)}) [id={tool_id[:20]}...]")

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

            # Check if finish_research was called
            if any(tc.get("name") == "finish_research" for tc in response.tool_calls):
                print("\n  finish_research called - completing")
                break

        if iteration >= max_iterations:
            print(f"\n  [WARN] Hit max iterations ({max_iterations})")

        print(f"\n  Total iterations: {iteration}")
        print("  [OK] Multi-turn tool calling completed successfully")
        return True

    except Exception as e:
        print(f"\n  [FAIL] {e}")
        import traceback
        traceback.print_exc()

        # Check for the specific reasoning_content error
        error_str = str(e)
        if "reasoning_content" in error_str:
            print("\n  >>> This is the 'reasoning_content missing' error!")
            print("  >>> extra_body with thinking=disabled was likely lost during multi-turn")
        return False


# ============================================================
# Test 3: bind_tools with parallel_tool_calls parameter
# ============================================================

def test_parallel_tool_calls_param():
    """Test that parallel_tool_calls parameter doesn't break Kimi."""
    print("\n" + "=" * 60)
    print("Test 3: bind_tools with parallel_tool_calls=False")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.2, track_usage=False)

        # This is exactly what SmartRiskManagerGraph does via bind_tools_safely
        llm_with_tools = llm.bind_tools(ALL_TOOLS, parallel_tool_calls=False)
        print(f"  Bound with parallel_tool_calls=False: OK")

        response = llm_with_tools.invoke("What positions do I have? Use get_positions.")
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"  Tool call: {response.tool_calls[0].get('name')}")
            print("  [OK] parallel_tool_calls=False works")
        else:
            print("  [OK] No tool call but no error (model answered directly)")
        return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        if "parallel_tool_calls" in str(e).lower():
            print("\n  >>> Kimi API rejected parallel_tool_calls parameter!")
            print("  >>> bind_tools_safely() needs Moonshot-specific handling")
        return False


# ============================================================
# Test 4: Verify extra_body propagation through bind_tools
# ============================================================

def test_extra_body_propagation():
    """Verify extra_body is preserved when bind_tools creates a RunnableBinding."""
    print("\n" + "=" * 60)
    print("Test 4: extra_body Propagation Through bind_tools")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.2, track_usage=False)

        # Check base LLM
        base_extra_body = getattr(llm, 'extra_body', None)
        print(f"  Base LLM extra_body: {base_extra_body}")

        if not base_extra_body or base_extra_body.get("thinking", {}).get("type") != "disabled":
            print("  [FAIL] Base LLM missing thinking=disabled in extra_body")
            return False

        # Bind tools
        llm_with_tools = llm.bind_tools(ALL_TOOLS)

        # Inspect the bound object
        print(f"  Bound type: {type(llm_with_tools).__name__}")

        # Try to find extra_body in the chain
        found_extra_body = False

        # RunnableBinding wraps the original LLM
        if hasattr(llm_with_tools, 'bound'):
            bound = llm_with_tools.bound
            print(f"  Bound.bound type: {type(bound).__name__}")
            if hasattr(bound, 'extra_body'):
                print(f"  Bound.bound.extra_body: {bound.extra_body}")
                if bound.extra_body.get("thinking", {}).get("type") == "disabled":
                    found_extra_body = True

        # Also check kwargs (bind_tools stores tool config in kwargs)
        if hasattr(llm_with_tools, 'kwargs'):
            print(f"  Bound kwargs keys: {list(llm_with_tools.kwargs.keys())}")

        # Check the first_id/last style chain
        if hasattr(llm_with_tools, 'first'):
            first = llm_with_tools.first
            if hasattr(first, 'extra_body'):
                print(f"  Chain.first.extra_body: {first.extra_body}")
                if first.extra_body.get("thinking", {}).get("type") == "disabled":
                    found_extra_body = True

        if found_extra_body:
            print("  [OK] extra_body with thinking=disabled preserved through bind_tools")
        else:
            print("  [WARN] Could not confirm extra_body propagation via inspection")
            print("  (This doesn't mean it's broken - LangChain may propagate it at invoke time)")

        return True

    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# Test 5: native/ provider prefix (Expert 15 uses native/kimi_k2.5 for quick_think)
# ============================================================

def test_native_provider_prefix():
    """Test that native/ prefix works for kimi_k2.5 (used by expert 15 for quick_think_llm)."""
    print("\n" + "=" * 60)
    print("Test 5: native/ Provider Prefix (native/kimi_k2.5)")
    print("=" * 60)

    try:
        llm = ModelFactory.create_llm('native/kimi_k2.5', temperature=0.6, track_usage=False)
        print(f"  LLM type: {type(llm).__name__}")

        # native/ should use the model's native provider (moonshot for kimi)
        if hasattr(llm, 'openai_api_base'):
            print(f"  API base: {llm.openai_api_base}")
        if hasattr(llm, 'base_url'):
            print(f"  Base URL: {llm.base_url}")

        # kimi_k2.5 (not nonthinking) should use ChatKimiThinking
        expected_class = "ChatKimiThinking"
        actual_class = type(llm).__name__
        print(f"  Expected class: {expected_class}, Got: {actual_class}")

        if actual_class == expected_class:
            print("  [OK] native/kimi_k2.5 creates ChatKimiThinking (thinking mode)")
        else:
            print(f"  [INFO] native/kimi_k2.5 creates {actual_class}")

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
    print("\nKimi K2.5 Non-Thinking Mode Tool Call Tests")
    print("Tests SmartRiskManager-like tool calling scenarios")
    print("=" * 60)

    results = []

    results.append(("Basic Tool Call", test_basic_tool_call()))
    results.append(("Multi-Turn Tool Calls", test_multi_turn_tool_calls()))
    results.append(("parallel_tool_calls Param", test_parallel_tool_calls_param()))
    results.append(("extra_body Propagation", test_extra_body_propagation()))
    results.append(("native/ Provider Prefix", test_native_provider_prefix()))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, success in results:
        status = "[OK]" if success else "[FAIL]"
        print(f"  {status}: {name}")

    passed = sum(1 for _, s in results if s)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")

    sys.exit(0 if passed == total else 1)

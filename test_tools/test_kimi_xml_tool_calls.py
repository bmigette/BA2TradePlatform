"""
Test script to verify Kimi K2.5 XML tool call parsing fallback.

Tests two scenarios:
1. Unit test: XML parsing logic works correctly on known XML patterns
2. Live API test: Kimi K2.5 tool calls work (with fallback if XML is returned)

The XML bug: Kimi K2.5 (especially in thinking mode) intermittently returns
tool calls as XML text in the content field instead of structured tool_calls:

  <function_calls>
  <invoke name="get_ohlcv_data">
  <parameter name="ticker">NET</parameter>
  </invoke>
  </function_calls>

Usage:
    .venv\Scripts\python.exe test_tools/test_kimi_xml_tool_calls.py
    .venv\Scripts\python.exe test_tools/test_kimi_xml_tool_calls.py --unit-only   # Skip API calls
    .venv\Scripts\python.exe test_tools/test_kimi_xml_tool_calls.py --live-only   # Only API calls
"""

import sys
import os
import json
import re

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Unit Tests - XML Parsing Logic (no API calls needed)
# ============================================================================

def test_parse_single_xml_tool_call():
    """Test parsing a single XML tool call from content."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    content = (
        'I\'ll analyze NET (Cloudflare). Let me retrieve the data: '
        '<function_calls>\n'
        '<invoke name="get_ohlcv_data">\n'
        '<parameter name="ticker">NET</parameter>\n'
        '</invoke>\n'
        '</function_calls>'
    )

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 1, f"Expected 1 tool call, got {len(tool_calls)}"
    assert tool_calls[0]["name"] == "get_ohlcv_data"
    assert tool_calls[0]["args"] == {"ticker": "NET"}
    assert "<function_calls>" not in clean_content
    assert "Cloudflare" in clean_content
    print("  PASS: Single XML tool call parsed correctly")
    return True


def test_parse_multiple_xml_tool_calls():
    """Test parsing multiple XML tool calls from content."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    content = (
        'Let me get the data: <function_calls>\n'
        '<invoke name="get_ohlcv_data">\n'
        '<parameter name="ticker">NET</parameter>\n'
        '</invoke>\n'
        '</function_calls> <function_calls>\n'
        '<invoke name="get_indicator_data">\n'
        '<parameter name="ticker">NET</parameter>\n'
        '<parameter name="indicators">["close_10_ema", "macd", "rsi"]</parameter>\n'
        '</invoke>\n'
        '</function_calls>'
    )

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 2, f"Expected 2 tool calls, got {len(tool_calls)}"
    assert tool_calls[0]["name"] == "get_ohlcv_data"
    assert tool_calls[0]["args"] == {"ticker": "NET"}
    assert tool_calls[1]["name"] == "get_indicator_data"
    assert tool_calls[1]["args"]["ticker"] == "NET"
    # JSON list parameter should be parsed
    assert isinstance(tool_calls[1]["args"]["indicators"], list)
    assert "<function_calls>" not in clean_content
    print("  PASS: Multiple XML tool calls parsed correctly")
    return True


def test_parse_numeric_parameters():
    """Test parsing numeric parameter values."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    content = (
        '<function_calls>\n'
        '<invoke name="get_yield_curve">\n'
        '<parameter name="lookback_periods">4</parameter>\n'
        '</invoke>\n'
        '</function_calls>'
    )

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "get_yield_curve"
    assert tool_calls[0]["args"]["lookback_periods"] == 4  # Should be parsed as int
    print("  PASS: Numeric parameters parsed correctly")
    return True


def test_parse_no_xml_returns_empty():
    """Test that normal content without XML returns no tool calls."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    content = "The weather in Tokyo is sunny and 72F."

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 0
    assert clean_content == content
    print("  PASS: No XML content returns empty tool calls")
    return True


def test_parse_real_log_pattern():
    """Test parsing the exact pattern seen in production logs."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    # Exact pattern from all.debug.log lines ~28756
    content = (
        "I'll analyze NET (Cloudflare) as a potential trading opportunity.\n\n"
        "Let me retrieve the necessary data:\n\n"
        '<function_calls>\n'
        '<invoke name="get_ohlcv_data">\n'
        '<parameter name="ticker">NET</parameter>\n'
        '</invoke>\n'
        '</function_calls> <function_calls>\n'
        '<invoke name="get_indicator_data">\n'
        '<parameter name="ticker">NET</parameter>\n'
        '<parameter name="indicators">["close_10_ema", "close_50_sma", "macd", "macdh", "rsi", "boll_ub", "boll_lb", "atr"]</parameter>\n'
        '</invoke>\n'
        '</function_calls> <function_calls>\n'
        '<invoke name="get_yield_curve">\n'
        '<parameter name="lookback_periods">4</parameter>\n'
        '</invoke>\n'
        '</function_calls> <function_calls>\n'
        '<invoke name="get_fed_calendar">\n'
        '<parameter name="lookback_periods">6</parameter>\n'
        '</invoke>\n'
        '</function_calls>'
    )

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 4, f"Expected 4 tool calls, got {len(tool_calls)}"
    assert tool_calls[0]["name"] == "get_ohlcv_data"
    assert tool_calls[1]["name"] == "get_indicator_data"
    assert tool_calls[2]["name"] == "get_yield_curve"
    assert tool_calls[3]["name"] == "get_fed_calendar"
    assert "<function_calls>" not in clean_content
    assert "Cloudflare" in clean_content
    # Each tool call should have a unique ID
    ids = [tc["id"] for tc in tool_calls]
    assert len(set(ids)) == len(ids), "Tool call IDs should be unique"
    print("  PASS: Real production log pattern parsed correctly (4 tool calls)")
    return True


def test_tool_call_ids_are_unique():
    """Test that generated tool call IDs are unique even for same tool name."""
    from ba2_trade_platform.core.ModelFactory import ChatMoonshotThinking

    content = (
        '<function_calls>\n'
        '<invoke name="get_data">\n'
        '<parameter name="ticker">AAPL</parameter>\n'
        '</invoke>\n'
        '</function_calls> <function_calls>\n'
        '<invoke name="get_data">\n'
        '<parameter name="ticker">GOOGL</parameter>\n'
        '</invoke>\n'
        '</function_calls>'
    )

    clean_content, tool_calls = ChatMoonshotThinking._parse_xml_tool_calls(content)

    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] != tool_calls[1]["id"], "IDs should be unique even for same tool name"
    print("  PASS: Tool call IDs are unique")
    return True


# ============================================================================
# Live API Tests (requires Moonshot API key)
# ============================================================================

def test_live_thinking_mode_tool_calls():
    """Test Kimi K2.5 thinking mode with tools - verifies the full pipeline."""
    from ba2_trade_platform.core.db import init_db
    init_db()

    from langchain_core.tools import tool
    from ba2_trade_platform.core.ModelFactory import ModelFactory

    @tool
    def get_stock_price(symbol: str) -> str:
        """Get the current stock price for a trading symbol."""
        prices = {"AAPL": 195.50, "GOOGL": 178.30, "NVDA": 142.80}
        price = prices.get(symbol, 100.00)
        return f"The current price of {symbol} is ${price}"

    @tool
    def get_market_data(ticker: str, indicators: str = "rsi,macd") -> str:
        """Get technical indicators for a stock ticker."""
        return f"Technical data for {ticker}: RSI=55.3, MACD=1.2, Signal=0.8"

    print("\n  Creating Kimi K2.5 (thinking mode)...")
    llm = ModelFactory.create_llm('moonshot/kimi_k2.5', temperature=1.0, track_usage=False)
    print(f"  LLM type: {type(llm).__name__}")

    print("  Binding tools...")
    llm_with_tools = llm.bind_tools([get_stock_price, get_market_data])

    print("  Invoking with tool-triggering prompt...")
    response = llm_with_tools.invoke(
        "What is the current stock price of AAPL? Use the get_stock_price tool."
    )

    print(f"  Response type: {type(response).__name__}")
    print(f"  Content: {response.content[:200] if response.content else '(empty)'}")

    if response.tool_calls:
        print(f"  Tool calls: {len(response.tool_calls)}")
        for tc in response.tool_calls:
            print(f"    - {tc.get('name')}({tc.get('args')})")
        print("  PASS: Tool calls returned (structured or parsed from XML)")
        return True
    else:
        # Check if content contains XML that wasn't parsed (should not happen with fix)
        if response.content and '<function_calls>' in response.content:
            print("  FAIL: XML tool calls in content were NOT parsed!")
            return False
        else:
            print("  WARN: Model answered directly without tool calls (not necessarily a bug)")
            return True


def test_live_nonthinking_mode_tool_calls():
    """Test Kimi K2.5 non-thinking mode with tools - baseline comparison."""
    from ba2_trade_platform.core.db import init_db
    init_db()

    from langchain_core.tools import tool
    from ba2_trade_platform.core.ModelFactory import ModelFactory

    @tool
    def get_weather(city: str) -> str:
        """Get the current weather in a city."""
        return f"The weather in {city} is sunny and 72F"

    print("\n  Creating Kimi K2.5 (non-thinking mode)...")
    llm = ModelFactory.create_llm('moonshot/kimi_k2.5-nonthinking', temperature=0.6, track_usage=False)
    print(f"  LLM type: {type(llm).__name__}")

    print("  Binding tools...")
    llm_with_tools = llm.bind_tools([get_weather])

    print("  Invoking with tool-triggering prompt...")
    response = llm_with_tools.invoke("What's the weather in Tokyo? Use the get_weather tool.")

    print(f"  Response type: {type(response).__name__}")
    print(f"  Content: {response.content[:200] if response.content else '(empty)'}")

    if response.tool_calls:
        print(f"  Tool calls: {len(response.tool_calls)}")
        for tc in response.tool_calls:
            print(f"    - {tc.get('name')}({tc.get('args')})")
        print("  PASS: Non-thinking mode tool calls work")
        return True
    else:
        if response.content and '<function_calls>' in response.content:
            print("  FAIL: XML tool calls in content (unexpected in non-thinking mode)")
            return False
        else:
            print("  WARN: Model answered directly without tool calls")
            return True


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    args = sys.argv[1:]
    unit_only = "--unit-only" in args
    live_only = "--live-only" in args

    results = []

    if not live_only:
        print("\n" + "=" * 60)
        print("UNIT TESTS - XML Parsing Logic")
        print("=" * 60)

        unit_tests = [
            ("Single XML tool call", test_parse_single_xml_tool_call),
            ("Multiple XML tool calls", test_parse_multiple_xml_tool_calls),
            ("Numeric parameters", test_parse_numeric_parameters),
            ("No XML returns empty", test_parse_no_xml_returns_empty),
            ("Real production log pattern", test_parse_real_log_pattern),
            ("Unique tool call IDs", test_tool_call_ids_are_unique),
        ]

        for name, test_fn in unit_tests:
            try:
                success = test_fn()
                results.append((f"[Unit] {name}", success))
            except Exception as e:
                print(f"  FAIL: {e}")
                import traceback
                traceback.print_exc()
                results.append((f"[Unit] {name}", False))

    if not unit_only:
        print("\n" + "=" * 60)
        print("LIVE API TESTS - Kimi K2.5 Tool Calling")
        print("=" * 60)

        live_tests = [
            ("Thinking mode + tools", test_live_thinking_mode_tool_calls),
            ("Non-thinking mode + tools", test_live_nonthinking_mode_tool_calls),
        ]

        for name, test_fn in live_tests:
            try:
                success = test_fn()
                results.append((f"[Live] {name}", success))
            except Exception as e:
                print(f"  FAIL: {e}")
                import traceback
                traceback.print_exc()
                results.append((f"[Live] {name}", False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, success in results:
        status = "PASS" if success else "FAIL"
        print(f"  {status}: {name}")

    passed = sum(1 for _, s in results if s)
    total = len(results)
    print(f"\n  {passed}/{total} tests passed")

    sys.exit(0 if all(r[1] for r in results) else 1)

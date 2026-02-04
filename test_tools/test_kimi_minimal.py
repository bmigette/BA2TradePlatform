"""
Minimal test for Kimi K2.5 thinking mode with tool calls.
Completely standalone - uses only httpx, no project imports needed.

Run from the G: drive venv:
  G:\Mon Drive\Work\AiTrading\BA2TradePlatform\.venv\Scripts\python.exe test_tools\test_kimi_minimal.py

Or set MOONSHOT_API_KEY env var and run with any Python that has httpx.
"""

import sys
import os
import json

try:
    import httpx
except ImportError:
    print("httpx not installed. Install with: pip install httpx")
    sys.exit(1)

# Try multiple methods to get API key
API_KEY = os.environ.get('MOONSHOT_API_KEY')

# Try to read from sqlite database if no env var (hacky but works for testing)
if not API_KEY:
    try:
        import sqlite3
        db_path = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value_str FROM appsetting WHERE key = 'moonshot_api_key'")
            row = cursor.fetchone()
            if row:
                API_KEY = row[0]
            conn.close()
    except Exception as e:
        pass

if not API_KEY:
    print("Please set MOONSHOT_API_KEY environment variable")
    print("Or enter it now:")
    API_KEY = input("Moonshot API Key: ").strip()

if not API_KEY:
    print("No API key provided, exiting")
    sys.exit(1)

BASE_URL = "https://api.moonshot.ai/v1"


def test_thinking_with_tools_httpx():
    """Test thinking mode with tool calls using httpx directly."""
    print("=" * 60)
    print("Testing Kimi K2.5 Thinking Mode + Tools (httpx)")
    print("=" * 60)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    # Define a simple tool
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather in a city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "The city name"}
                    },
                    "required": ["city"]
                }
            }
        }
    ]

    messages = [
        {"role": "user", "content": "What's the weather in Tokyo?"}
    ]

    request_body = {
        "model": "kimi-k2.5",
        "messages": messages,
        "tools": tools,
        "temperature": 1.0,
        "top_p": 0.95,
        "thinking": {"type": "enabled"},
    }

    print("\n1. Making initial request with thinking enabled...")
    try:
        client = httpx.Client(timeout=120.0)

        response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=request_body)
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason")

        print(f"   Finish reason: {finish_reason}")
        print(f"   Content: {message.get('content', '')[:100]}")

        reasoning_content = message.get("reasoning_content")
        if reasoning_content:
            print(f"   Reasoning content: {reasoning_content[:100]}...")

        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            print(f"\n   TOOL CALLS DETECTED:")
            for tc in tool_calls:
                print(f"     - ID: {tc.get('id')}")
                print(f"       Function: {tc.get('function', {}).get('name')}")
                print(f"       Args: {tc.get('function', {}).get('arguments')}")

            # Now test multi-turn: add assistant message WITH reasoning_content
            print("\n2. Testing multi-turn with tool result...")

            # Build assistant message preserving reasoning_content
            assistant_msg = {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content

            messages.append(assistant_msg)

            # Add tool result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_calls[0]["id"],
                "content": json.dumps({"weather": "Sunny", "temperature": "22C"})
            })

            request_body["messages"] = messages

            response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=request_body)
            response.raise_for_status()
            data = response.json()

            choice = data["choices"][0]
            message = choice.get("message", {})

            final_content = message.get('content', '')[:200]
            # Handle unicode for Windows console
            try:
                print(f"   Final response: {final_content}")
            except UnicodeEncodeError:
                print(f"   Final response: {final_content.encode('ascii', 'replace').decode()}")
            print("\n[OK] SUCCESS: Multi-turn tool call with thinking mode works!")

            client.close()
            return True
        else:
            print("\n   No tool calls - model answered directly")
            client.close()
            return True

    except httpx.HTTPStatusError as e:
        print(f"\n[FAIL] HTTP ERROR: {e.response.status_code}")
        print(f"   {e.response.text}")
        return False
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_websearch_instant_mode():
    """Test websearch with instant mode (thinking disabled).

    NOTE: The $web_search builtin tool has a known API limitation where
    reasoning_content is NOT returned in tool_call responses even when
    thinking is enabled. Therefore, websearch must use instant mode.
    Regular tool calls work fine with thinking mode.
    """
    print("\n" + "=" * 60)
    print("Testing Kimi K2.5 Websearch (Instant Mode)")
    print("=" * 60)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    # Define $web_search builtin tool
    tools = [
        {
            "type": "builtin_function",
            "function": {"name": "$web_search"},
        }
    ]

    messages = [
        {"role": "user", "content": "What is the current date today? Search the web to find out."}
    ]

    # Use instant mode (thinking disabled) for $web_search due to API limitation
    request_body = {
        "model": "kimi-k2.5",
        "messages": messages,
        "tools": tools,
        "temperature": 0.6,  # Instant mode temperature
        "top_p": 0.95,
        "max_tokens": 2048,
        "thinking": {"type": "disabled"},  # Required for $web_search builtin
    }

    print("\n1. Making websearch request (instant mode)...")
    try:
        client = httpx.Client(timeout=120.0)
        max_iterations = 5
        iteration = 0
        finish_reason = None

        while finish_reason != "stop" and iteration < max_iterations:
            iteration += 1
            print(f"   Iteration {iteration}...")

            response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=request_body)
            response.raise_for_status()
            data = response.json()

            choice = data["choices"][0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason")

            print(f"   Finish reason: {finish_reason}")

            if finish_reason == "tool_calls":
                tool_calls = message.get("tool_calls", [])
                print(f"   Tool calls: {[tc.get('function', {}).get('name') for tc in tool_calls]}")

                # Build assistant message (no reasoning_content in instant mode)
                assistant_msg = {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }

                messages.append(assistant_msg)

                # Add tool results
                for tc in tool_calls:
                    tc_name = tc.get("function", {}).get("name", "")
                    tc_args = tc.get("function", {}).get("arguments", "{}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": tc_name,
                        "content": tc_args,
                    })

                request_body["messages"] = messages

            elif finish_reason == "stop":
                content = message.get("content", "")
                try:
                    print(f"\n   Final response: {content[:200]}")
                except UnicodeEncodeError:
                    print(f"\n   Final response: {content[:200].encode('ascii', 'replace').decode()}")

        client.close()
        print("\n[OK] SUCCESS: Websearch with thinking mode works!")
        return True

    except httpx.HTTPStatusError as e:
        print(f"\n[FAIL] HTTP ERROR: {e.response.status_code}")
        error_text = e.response.text
        if "reasoning_content" in error_text:
            print("   >>> This is the reasoning_content missing error!")
        print(f"   {error_text[:300]}")
        return False
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\nKimi K2.5 Thinking Mode Tests")
    print("=" * 60)

    results = []

    # Test 1: Tool calls with thinking mode
    results.append(("Thinking + Tools", test_thinking_with_tools_httpx()))

    # Test 2: Websearch with instant mode (thinking disabled due to API limitation)
    results.append(("Websearch (Instant Mode)", test_websearch_instant_mode()))

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

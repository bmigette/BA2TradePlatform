"""Debug script to check if websearch returns reasoning_content."""

import sys
import os
import json
import httpx
import sqlite3

# Get API key from database
db_path = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT value_str FROM appsetting WHERE key = 'moonshot_api_key'")
row = cursor.fetchone()
API_KEY = row[0] if row else None
conn.close()

if not API_KEY:
    print("No API key found in database")
    sys.exit(1)

BASE_URL = "https://api.moonshot.ai/v1"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# Test websearch with thinking
tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
messages = [{"role": "user", "content": "Search the web and tell me what the latest news headlines are today, January 29 2026."}]
request_body = {
    "model": "kimi-k2.5",
    "messages": messages,
    "tools": tools,
    "temperature": 1.0,
    "top_p": 0.95,
    "thinking": {"type": "enabled"},
}

print("Testing $web_search with thinking mode...")
print("=" * 60)

client = httpx.Client(timeout=120.0)

try:
    response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=request_body)
    response.raise_for_status()
    data = response.json()

    choice = data["choices"][0]
    message = choice.get("message", {})

    print(f"Full message keys: {list(message.keys())}")
    print(f"Finish reason: {choice.get('finish_reason')}")
    print(f"Content: {message.get('content', '')[:100]}")
    print(f"Has reasoning_content: {'reasoning_content' in message}")

    if message.get("reasoning_content"):
        rc = message["reasoning_content"]
        print(f"reasoning_content (len={len(rc)}): {rc[:150]}...")
    else:
        print("NO reasoning_content in response!")

    if message.get("tool_calls"):
        print(f"Tool calls: {[tc.get('function', {}).get('name') for tc in message['tool_calls']]}")

except httpx.HTTPStatusError as e:
    print(f"HTTP Error: {e.response.status_code}")
    print(f"Response: {e.response.text}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
finally:
    client.close()

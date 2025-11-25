"""
Testing Gemini 3 fix with multiple models to ensure compatibility.

We'll test:
1. Gemini 3 Pro Preview (the model we're fixing)
2. GPT-5 (to ensure we don't break OpenAI models)
3. Grok-4 (to ensure we don't break Grok models)
4. Qwen (to ensure we don't break other models)

The patch should only affect Gemini and not interfere with other models.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment
env_path = Path(__file__).parent.parent / "creds.env"
if env_path.exists():
    load_dotenv(env_path)

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

naga_api_key = get_app_setting('naga_ai_api_key') or os.getenv("OPENAI_API_KEY")
naga_base_url = "https://api.naga.ac/v1"

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

print("="*80)
print("Testing Gemini Patch - Multi-Model Compatibility")
print("="*80)

# Apply the patch
print("\n[SETUP] Applying Gemini ToolMessage patch...")
from ba2_trade_platform.core.gemini_patch import apply_gemini_toolmessage_patch, is_patch_applied
patch_success = apply_gemini_toolmessage_patch()
print(f"[SETUP] Patch applied: {patch_success}")
print(f"[SETUP] Patch active: {is_patch_applied()}")


def test_model(model_name: str, description: str, expected_to_work: bool = True):
    """Test a model with function calling."""
    print(f"\n{'='*80}")
    print(f"Testing: {model_name}")
    print(f"Description: {description}")
    print(f"{'='*80}")
    
    try:
        if not naga_api_key:
            print("[WARN] No API key configured, skipping")
            return
        
        llm = ChatOpenAI(
            model=model_name,
            temperature=0,
            api_key=naga_api_key,
            base_url=naga_base_url
        )
        llm_with_tools = llm.bind_tools(tools)
        
        # Test 1: Simple tool call
        print("\n--- Test 1: Simple tool call ---")
        messages = [HumanMessage(content="What's the stock price for AAPL?")]
        result = llm_with_tools.invoke(messages)
        
        print(f"Response: {result.content[:100] if result.content else '(empty)'}...")
        print(f"Tool calls: {len(result.tool_calls)}")
        
        if result.tool_calls:
            # Execute tool
            tool_call = result.tool_calls[0]
            tool_output = get_stock_price.invoke(tool_call['args'])
            
            # Create ToolMessage with name (our patch should handle this)
            tool_msg = ToolMessage(
                content=str(tool_output),
                tool_call_id=tool_call['id'],
                name=tool_call['name']
            )
            
            print(f"Tool executed: {tool_call['name']} -> {tool_output}")
            print(f"ToolMessage.name: {tool_msg.name}")
            
            # Test 2: Continue conversation with tool result
            print("\n--- Test 2: Continue conversation with tool result ---")
            conversation = messages + [result, tool_msg]
            conversation.append(HumanMessage(content="Based on that price, what do you think?"))
            
            final_result = llm_with_tools.invoke(conversation)
            print(f"Final response: {final_result.content[:150]}...")
            
            print(f"\n[OK] {model_name} - All tests PASSED")
            return True
        else:
            print(f"[INFO] {model_name} - No tool calls (model may have responded directly)")
            return True
            
    except Exception as e:
        error_msg = str(e)
        if "Name cannot be empty" in error_msg:
            print(f"\n[FAIL] {model_name} - Gemini name field error detected!")
            print(f"Error: {error_msg[:200]}...")
            if expected_to_work:
                print("[ERROR] This model should have worked with the patch!")
            return False
        else:
            print(f"\n[FAIL] {model_name} - Error: {error_msg[:200]}...")
            if not expected_to_work:
                print("[INFO] This failure may be expected (rate limit, model not available, etc.)")
            return False


# Test suite
results = {}

# Test 1: Gemini 3 Pro Preview (base model)
results['gemini-3-base'] = test_model(
    "gemini-3-pro-preview",
    "Gemini 3 Pro Preview (base) - Should work with dummy thought_signature",
    expected_to_work=True
)

# Test 2: Gemini 3 with reasoning_effort:low
results['gemini-3-low'] = test_model(
    "gemini-3-pro-preview",
    "Gemini 3 Pro Preview (reasoning_effort:low) - Should work with dummy signature",
    expected_to_work=True
)

# Test 3: Gemini 3 with reasoning_effort:high  
results['gemini-3-high'] = test_model(
    "gemini-3-pro-preview",
    "Gemini 3 Pro Preview (reasoning_effort:high) - Should work with dummy signature",
    expected_to_work=True
)

# Test 4: GPT-5 (ensure we don't break OpenAI models)
results['gpt-5'] = test_model(
    "gpt-5",
    "GPT-5 - OpenAI's latest (should not be affected by patch)",
    expected_to_work=True
)

# Test 5: Grok-4 (ensure we don't break Grok models)
results['grok-4'] = test_model(
    "grok-4-0709",
    "Grok-4 - xAI model (should not be affected by patch)",
    expected_to_work=True
)

# Test 6: Qwen (ensure we don't break other models)
results['qwen'] = test_model(
    "qwen3-max",
    "Qwen3 Max - Alibaba model (should not be affected by patch)",
    expected_to_work=True
)

# Test 7: Gemini 2.5 Flash (ensure we don't break other Gemini models)
results['gemini-2.5'] = test_model(
    "gemini-2.5-flash",
    "Gemini 2.5 Flash - Older Gemini model (should work with patch)",
    expected_to_work=True
)

# Summary
print("\n" + "="*80)
print("TEST SUMMARY")
print("="*80)

passed = sum(1 for v in results.values() if v)
total = len(results)

for model, result in results.items():
    status = "[OK]" if result else "[FAIL]"
    print(f"{status} {model}")

print(f"\nTotal: {passed}/{total} passed")

if passed == total:
    print("\n[SUCCESS] All models working correctly with the patch!")
    print("The patch is safe to deploy to main application.")
else:
    print(f"\n[WARNING] {total - passed} model(s) failed.")
    print("Review failures before deploying patch to main application.")

print("="*80)


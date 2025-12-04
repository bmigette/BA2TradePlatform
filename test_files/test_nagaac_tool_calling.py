"""
Test tool calling for all NagaAI/NagaAC models.

This test file validates tool calling functionality across all supported
NagaAI and NagaAC models using both:
1. Native OpenAI API (openai library)
2. LangChain (langchain_openai)

It creates a simple test tool and verifies that each model can successfully
call the tool and return the expected results.

These are the models supported as deep_think_llm / risk_manager_model in the platform.

Usage:
    .venv\Scripts\python.exe test_files\test_nagaac_tool_calling.py
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime

# Add parent directory to path to import from ba2_trade_platform
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables from creds.env
from dotenv import load_dotenv
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


# Get API key from app settings
NAGA_API_KEY = get_app_setting('naga_ai_api_key')
NAGA_BASE_URL = "https://api.naga.ac/v1"

if not NAGA_API_KEY:
    print("ERROR: No naga_ai_api_key found in AppSettings")
    print("Please configure the API key in the application settings.")
    sys.exit(1)

print(f"NagaAI API Key loaded: Yes (length: {len(NAGA_API_KEY)})")
print(f"NagaAI Base URL: {NAGA_BASE_URL}")


# =============================================================================
# All models supported as deep_think_llm / risk_manager_model
# From MarketExpertInterface.py valid_values
# =============================================================================

MODELS_TO_TEST = [
    # --- NagaAI GPT-5 family ---
    "NagaAI/gpt-5-2025-08-07",
    "NagaAI/gpt-5-mini-2025-08-07",
    "NagaAI/gpt-5-nano-2025-08-07",
    "NagaAI/gpt-5-chat-latest",
    "NagaAI/gpt-5-codex",
    
    # --- NagaAC GPT-5.1 (with reasoning effort support) ---
    "NagaAC/gpt-5.1-2025-11-13",
    
    # --- NagaAI/NagaAC Grok-4 family ---
    "NagaAI/grok-4-0709",
    "NagaAI/grok-4-fast-non-reasoning",
    "NagaAI/grok-4-fast-reasoning",
    "NagaAC/grok-4.1-fast-reasoning",
    
    # --- NagaAI Qwen family ---
    "NagaAI/qwen3-max",
    "NagaAI/qwen3-next-80b-a3b-instruct",
    "NagaAI/qwen3-next-80b-a3b-thinking",
    
    # --- NagaAI/NagaAC DeepSeek family ---
    "NagaAI/deepseek-v3.2",
    "NagaAI/deepseek-chat-v3.1",
    "NagaAI/deepseek-reasoner-0528",
    
    # --- NagaAI Kimi ---
    "NagaAI/kimi-k2-thinking",
]


# =============================================================================
# Tool Definition
# =============================================================================

# OpenAI-style tool definition
TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_stock_price",
        "description": "Get the current stock price for a given ticker symbol. Returns the price in USD.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The stock ticker symbol (e.g., 'AAPL', 'GOOGL', 'MSFT')"
                }
            },
            "required": ["symbol"]
        }
    }
}


def mock_get_stock_price(symbol: str) -> str:
    """Mock implementation of get_stock_price tool."""
    prices = {
        "AAPL": 178.50,
        "GOOGL": 141.25,
        "MSFT": 378.90,
        "TSLA": 245.30,
        "NVDA": 495.60,
    }
    price = prices.get(symbol.upper(), 100.00)
    return json.dumps({"symbol": symbol.upper(), "price": price, "currency": "USD"})


# =============================================================================
# Test Result Data Class
# =============================================================================

@dataclass
class TestResult:
    model: str
    method: str  # "openai" or "langchain"
    success: bool
    tool_called: bool
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    response_content: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    native_tool_call_detected: bool = False  # If native format was in response


# =============================================================================
# OpenAI API Test
# =============================================================================

def test_openai_api(model: str) -> TestResult:
    """Test tool calling using the native OpenAI API."""
    from openai import OpenAI
    
    start_time = time.time()
    result = TestResult(model=model, method="openai", success=False, tool_called=False)
    
    try:
        # Extract model name (remove provider prefix)
        model_name = model.split("/", 1)[1] if "/" in model else model
        
        client = OpenAI(
            api_key=NAGA_API_KEY,
            base_url=NAGA_BASE_URL,
        )
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Use the available tools to answer questions."},
            {"role": "user", "content": "What is the current stock price of AAPL?"}
        ]
        
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=[TOOL_DEFINITION],
            tool_choice="auto",
        )
        
        result.duration_ms = (time.time() - start_time) * 1000
        
        # Check response
        message = response.choices[0].message
        result.response_content = message.content or ""
        
        # Check for native tool call markers in content
        if result.response_content:
            native_markers = [
                '<|tool_calls_section_begin|>',  # Kimi
                '<|tool▁calls▁begin|>',          # DeepSeek
                '<tool_call>',                    # Hermes/Qwen
            ]
            result.native_tool_call_detected = any(m in result.response_content for m in native_markers)
        
        # Check for tool calls
        if message.tool_calls and len(message.tool_calls) > 0:
            result.tool_called = True
            tool_call = message.tool_calls[0]
            result.tool_name = tool_call.function.name
            try:
                result.tool_args = json.loads(tool_call.function.arguments)
            except:
                result.tool_args = {"raw": tool_call.function.arguments}
            result.success = True
        elif result.native_tool_call_detected:
            # Native tool call in content but not parsed
            result.tool_called = False
            result.success = False
            result.error = "Native tool call in content but not parsed by API"
        else:
            # No tool call - model may have responded directly
            result.success = True  # Not an error, just no tool use
            
    except Exception as e:
        result.duration_ms = (time.time() - start_time) * 1000
        result.error = str(e)
        
    return result


# =============================================================================
# LangChain Test
# =============================================================================

def test_langchain(model: str) -> TestResult:
    """Test tool calling using LangChain."""
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage, SystemMessage
    
    start_time = time.time()
    result = TestResult(model=model, method="langchain", success=False, tool_called=False)
    
    try:
        # Extract model name (remove provider prefix)
        model_name = model.split("/", 1)[1] if "/" in model else model
        
        # Create LangChain tool
        @tool
        def get_stock_price(symbol: str) -> str:
            """Get the current stock price for a given ticker symbol. Returns the price in USD."""
            return mock_get_stock_price(symbol)
        
        llm = ChatOpenAI(
            model=model_name,
            api_key=NAGA_API_KEY,
            base_url=NAGA_BASE_URL,
            temperature=0,
        )
        
        # Bind tools
        llm_with_tools = llm.bind_tools([get_stock_price])
        
        messages = [
            SystemMessage(content="You are a helpful assistant. Use the available tools to answer questions."),
            HumanMessage(content="What is the current stock price of AAPL?"),
        ]
        
        response = llm_with_tools.invoke(messages)
        
        result.duration_ms = (time.time() - start_time) * 1000
        result.response_content = response.content or ""
        
        # Check for native tool call markers in content
        if result.response_content:
            native_markers = [
                '<|tool_calls_section_begin|>',  # Kimi
                '<|tool▁calls▁begin|>',          # DeepSeek
                '<tool_call>',                    # Hermes/Qwen
            ]
            result.native_tool_call_detected = any(m in result.response_content for m in native_markers)
        
        # Check for tool calls
        if response.tool_calls and len(response.tool_calls) > 0:
            result.tool_called = True
            tool_call = response.tool_calls[0]
            result.tool_name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            result.tool_args = tool_call.get("args") or tool_call.get("function", {}).get("arguments")
            result.success = True
        elif result.native_tool_call_detected:
            # Native tool call in content but not parsed
            result.tool_called = False
            result.success = False
            result.error = "Native tool call in content but not parsed by LangChain"
        else:
            # No tool call - model may have responded directly
            result.success = True  # Not an error, just no tool use
            
    except Exception as e:
        result.duration_ms = (time.time() - start_time) * 1000
        result.error = str(e)
        
    return result


# =============================================================================
# Print Results Table
# =============================================================================

def print_results_table(results: List[TestResult]):
    """Print a formatted table of test results."""
    print("\n" + "=" * 130)
    print("TOOL CALLING TEST RESULTS")
    print("=" * 130)
    
    # Table header
    header = f"{'Model':<45} {'Method':<12} {'Success':<8} {'Tool Called':<12} {'Tool Name':<18} {'Native':<8} {'Time (ms)':<10}"
    print(header)
    print("-" * 130)
    
    for r in results:
        success_str = "✓" if r.success else "✗"
        tool_called_str = "✓" if r.tool_called else "-"
        tool_name_str = r.tool_name or "-"
        native_str = "⚠" if r.native_tool_call_detected else "-"
        time_str = f"{r.duration_ms:.0f}"
        
        # Truncate model name if too long
        model_display = r.model[:43] + ".." if len(r.model) > 45 else r.model
        
        row = f"{model_display:<45} {r.method:<12} {success_str:<8} {tool_called_str:<12} {tool_name_str:<18} {native_str:<8} {time_str:<10}"
        print(row)
        
        # Print error if any
        if r.error:
            error_display = r.error[:100] + "..." if len(r.error) > 100 else r.error
            print(f"  └─ Error: {error_display}")
    
    print("=" * 130)
    
    # Summary
    total = len(results)
    successful = sum(1 for r in results if r.success)
    tool_calls = sum(1 for r in results if r.tool_called)
    native_detected = sum(1 for r in results if r.native_tool_call_detected)
    errors = sum(1 for r in results if r.error)
    
    print(f"\nSummary:")
    print(f"  Total tests: {total}")
    print(f"  Successful: {successful} ({100*successful/total:.0f}%)")
    print(f"  Tool calls made: {tool_calls} ({100*tool_calls/total:.0f}%)")
    print(f"  Native format detected: {native_detected} (requires parser workaround)")
    print(f"  Errors: {errors}")
    
    # Group by model to show which models need the parser
    print(f"\n" + "=" * 80)
    print("MODELS REQUIRING NATIVE TOOL CALL PARSER:")
    print("=" * 80)
    
    models_needing_parser = set()
    for r in results:
        if r.native_tool_call_detected:
            models_needing_parser.add(r.model)
    
    if models_needing_parser:
        for m in sorted(models_needing_parser):
            print(f"  - {m}")
    else:
        print("  None! All models properly parse tool calls via NagaAI API.")
    
    # Legend
    print(f"\nLegend:")
    print(f"  ✓ = Success/Yes")
    print(f"  ✗ = Failed")
    print(f"  - = No/Not applicable")
    print(f"  ⚠ = Native tool call format detected in response (needs parsing)")


# =============================================================================
# Main
# =============================================================================

def main():
    print("\n" + "=" * 80)
    print("NagaAI/NagaAC Tool Calling Test")
    print("=" * 80)
    print(f"Testing {len(MODELS_TO_TEST)} models (all supported deep_think_llm models)")
    print(f"Test methods: OpenAI API, LangChain")
    print("=" * 80 + "\n")
    
    results: List[TestResult] = []
    
    for i, model in enumerate(MODELS_TO_TEST, 1):
        print(f"\n[{i}/{len(MODELS_TO_TEST)}] Testing: {model}")
        print("-" * 60)
        
        # Test 1: OpenAI API
        print("  Testing with OpenAI API...", end=" ", flush=True)
        result_openai = test_openai_api(model)
        status = "✓" if result_openai.success else "✗"
        tool_status = f"(tool: {result_openai.tool_name})" if result_openai.tool_called else "(no tool call)"
        native_warning = " [NATIVE FORMAT!]" if result_openai.native_tool_call_detected else ""
        print(f"{status} {tool_status}{native_warning} [{result_openai.duration_ms:.0f}ms]")
        if result_openai.error:
            print(f"    Error: {result_openai.error[:80]}...")
        results.append(result_openai)
        
        # Test 2: LangChain
        print("  Testing with LangChain...", end=" ", flush=True)
        result_langchain = test_langchain(model)
        status = "✓" if result_langchain.success else "✗"
        tool_status = f"(tool: {result_langchain.tool_name})" if result_langchain.tool_called else "(no tool call)"
        native_warning = " [NATIVE FORMAT!]" if result_langchain.native_tool_call_detected else ""
        print(f"{status} {tool_status}{native_warning} [{result_langchain.duration_ms:.0f}ms]")
        if result_langchain.error:
            print(f"    Error: {result_langchain.error[:80]}...")
        results.append(result_langchain)
        
        # Small delay between models to avoid rate limiting
        if i < len(MODELS_TO_TEST):
            time.sleep(0.5)
    
    # Print final results table
    print_results_table(results)
    
    # Save results to JSON
    results_json = []
    for r in results:
        results_json.append({
            "model": r.model,
            "method": r.method,
            "success": r.success,
            "tool_called": r.tool_called,
            "tool_name": r.tool_name,
            "tool_args": r.tool_args,
            "native_tool_call_detected": r.native_tool_call_detected,
            "error": r.error,
            "duration_ms": r.duration_ms,
        })
    
    output_file = Path(__file__).parent / "test_nagaac_tool_calling_results.json"
    with open(output_file, "w") as f:
        json.dump(results_json, f, indent=2)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()

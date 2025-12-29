"""
Comprehensive LLM Provider Test Script

This script tests all LLM providers for which we have API keys configured:
1. Tests one model per provider via LangChain
2. Tests one model per provider via native/factory class
3. Tests tool calling capabilities
4. Tests websearch for providers that support it

Usage:
    python test_files/test_llm_providers.py
"""

import sys
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.core.ModelFactory import ModelFactory
from ba2_trade_platform.core.models_registry import (
    MODELS, PROVIDER_CONFIG, get_all_providers, get_model_for_provider,
    LABEL_WEBSEARCH, LABEL_TOOL_CALLING,
    PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, PROVIDER_ANTHROPIC,
    PROVIDER_XAI, PROVIDER_DEEPSEEK, PROVIDER_MOONSHOT, PROVIDER_OPENROUTER, PROVIDER_BEDROCK
)


# Rich console output for nice formatting
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None


@dataclass
class TestResult:
    """Result of a single test."""
    provider: str
    model: str
    test_type: str  # "langchain", "native", "tool_call", "websearch"
    success: bool
    response: str = ""
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class ProviderTestSuite:
    """Test results for a single provider."""
    provider: str
    api_key_configured: bool
    api_key_setting: str
    results: List[TestResult] = field(default_factory=list)


def check_api_key(provider: str) -> Tuple[bool, str]:
    """Check if API key is configured for a provider."""
    config = PROVIDER_CONFIG.get(provider)
    if not config:
        return False, ""
    
    api_key_setting = config.get("api_key_setting", "")
    if not api_key_setting:
        return False, ""
    
    api_key = get_app_setting(api_key_setting)
    return bool(api_key), api_key_setting


def get_test_model_for_provider(provider: str) -> Optional[str]:
    """
    Get a good test model for a provider.
    Prefers low-cost, fast models with tool calling support.
    """
    # Priority order for test models per provider
    preferred_models = {
        PROVIDER_OPENAI: ["gpt4o_mini", "gpt4o", "gpt5_mini"],
        PROVIDER_NAGAAI: ["gpt4o_mini", "grok3_mini", "gpt5_mini"],
        PROVIDER_GOOGLE: ["gemini_3_flash", "gemini_2.0_flash", "gemini_2.5_flash"],
        PROVIDER_ANTHROPIC: ["claude_3.5_haiku", "claude_3.5_sonnet"],
        PROVIDER_XAI: ["grok3_mini", "grok4_fast"],
        PROVIDER_DEEPSEEK: ["deepseek_chat", "deepseek_coder"],
        PROVIDER_MOONSHOT: ["kimi_k1.5", "kimi_k2"],
        PROVIDER_OPENROUTER: ["gpt4o_mini", "claude_3.5_haiku"],
        PROVIDER_BEDROCK: ["llama3_1_8b", "llama3_1_70b"],
    }
    
    # Try preferred models first
    for model in preferred_models.get(provider, []):
        if model in MODELS:
            provider_name = get_model_for_provider(model, provider)
            if provider_name:
                return model
    
    # Fall back to any model that works with this provider
    for model_name, model_info in MODELS.items():
        if provider in model_info.get("provider_names", {}):
            return model_name
    
    return None


def get_websearch_model_for_provider(provider: str) -> Optional[str]:
    """Get a model that supports websearch for the provider."""
    # First, check for models explicitly labeled with websearch
    for model_name, model_info in MODELS.items():
        if provider in model_info.get("provider_names", {}):
            if LABEL_WEBSEARCH in model_info.get("labels", []):
                return model_name
    
    # For providers that support websearch via their API (not model-specific),
    # return a suitable model even without the label
    # OpenAI uses Responses API with web_search_preview
    # NagaAI uses web_search_options on any model
    # Google uses Google Search grounding
    # xAI uses Live Search API with search_parameters
    # Moonshot (Kimi) uses $web_search builtin tool
    if provider in [PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, PROVIDER_XAI, PROVIDER_MOONSHOT]:
        # Return the same test model - websearch is API-level, not model-level
        return get_test_model_for_provider(provider)
    
    return None


def test_langchain_call(provider: str, model_name: str) -> TestResult:
    """Test a model via LangChain."""
    import time
    
    model_selection = f"{provider}/{model_name}"
    result = TestResult(
        provider=provider,
        model=model_name,
        test_type="langchain",
        success=False
    )
    
    try:
        start = time.time()
        llm = ModelFactory.create_llm(model_selection, temperature=0.0)
        response = llm.invoke("What is 2+2? Reply with just the number.")
        result.duration_ms = (time.time() - start) * 1000
        
        # Extract text from response
        if hasattr(response, 'content'):
            result.response = str(response.content)[:200]
        else:
            result.response = str(response)[:200]
        
        result.success = True
        
    except Exception as e:
        result.error = str(e)[:300]
    
    return result


def test_native_call(provider: str, model_name: str) -> TestResult:
    """Test a model via native API using OpenAI client."""
    import time
    from openai import OpenAI
    
    result = TestResult(
        provider=provider,
        model=model_name,
        test_type="native",
        success=False
    )
    
    try:
        # Get provider config
        config = PROVIDER_CONFIG.get(provider, {})
        base_url = config.get("base_url")
        api_key_setting = config.get("api_key_setting", "")
        api_key = get_app_setting(api_key_setting) if api_key_setting else None
        
        if not api_key:
            result.error = f"API key not found for {api_key_setting}"
            return result
        
        # Get provider-specific model name
        actual_model = get_model_for_provider(model_name, provider)
        if not actual_model:
            result.error = f"Model {model_name} not available for {provider}"
            return result
        
        # Special handling for different providers
        if provider == PROVIDER_GOOGLE:
            # Google uses the native google.genai SDK
            from google import genai
            
            client = genai.Client(api_key=api_key)
            
            start = time.time()
            response = client.models.generate_content(
                model=actual_model,
                contents="What is 2+2? Reply with just the number."
            )
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.text[:200] if response.text else ""
            result.success = True
            
        elif provider == PROVIDER_ANTHROPIC:
            # Anthropic uses its own SDK
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            
            start = time.time()
            response = client.messages.create(
                model=actual_model,
                max_tokens=100,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}]
            )
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.content[0].text[:200] if response.content else ""
            result.success = True
            
        elif provider == PROVIDER_DEEPSEEK:
            # DeepSeek uses OpenAI-compatible API
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            
            start = time.time()
            response = client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.choices[0].message.content[:200] if response.choices else ""
            result.success = True
            
        elif provider == PROVIDER_XAI:
            # xAI uses its native xai_sdk
            from xai_sdk import Client as XAIClient
            from xai_sdk.chat import user
            
            client = XAIClient(api_key=api_key)
            
            start = time.time()
            chat = client.chat.create(model=actual_model)
            chat.append(user("What is 2+2? Reply with just the number."))
            response = chat.sample()
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.content[:200] if response.content else ""
            result.success = True
                
        elif provider == PROVIDER_MOONSHOT:
            # Moonshot uses OpenAI-compatible API (international endpoint)
            client = OpenAI(api_key=api_key, base_url="https://api.moonshot.ai/v1")
            
            start = time.time()
            response = client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.choices[0].message.content[:200] if response.choices else ""
            result.success = True
            
        else:
            # OpenAI, NagaAI, OpenRouter use OpenAI-compatible API
            client = OpenAI(api_key=api_key, base_url=base_url)
            
            start = time.time()
            response = client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.duration_ms = (time.time() - start) * 1000
            result.response = response.choices[0].message.content[:200] if response.choices else ""
            result.success = True
    
    except Exception as e:
        result.error = str(e)[:300]
    
    return result


def test_tool_call(provider: str, model_name: str) -> TestResult:
    """Test tool calling capability via LangChain."""
    import time
    from langchain_core.tools import tool
    
    result = TestResult(
        provider=provider,
        model=model_name,
        test_type="tool_call",
        success=False
    )
    
    @tool
    def get_weather(city: str) -> str:
        """Get the current weather in a city."""
        return f"The weather in {city} is sunny and 72°F"
    
    model_selection = f"{provider}/{model_name}"
    
    try:
        start = time.time()
        llm = ModelFactory.create_llm(model_selection, temperature=0.0)
        
        # Bind tools to the model
        llm_with_tools = llm.bind_tools([get_weather])
        
        # Invoke with a prompt that should trigger tool use
        response = llm_with_tools.invoke("What's the weather in San Francisco?")
        result.duration_ms = (time.time() - start) * 1000
        
        # Check if tool call was made
        if hasattr(response, 'tool_calls') and response.tool_calls:
            tool_call = response.tool_calls[0]
            result.response = f"Tool called: {tool_call.get('name', 'unknown')} with args: {tool_call.get('args', {})}"
            result.success = True
        elif hasattr(response, 'additional_kwargs') and response.additional_kwargs.get('tool_calls'):
            tool_calls = response.additional_kwargs['tool_calls']
            result.response = f"Tool called: {tool_calls[0]['function']['name']}"
            result.success = True
        else:
            # Model might have responded without using tools
            content = response.content if hasattr(response, 'content') else str(response)
            result.response = f"No tool call detected. Response: {content[:150]}"
            result.success = False
    
    except Exception as e:
        result.error = str(e)[:300]
    
    return result


def test_websearch(provider: str, model_name: str) -> TestResult:
    """Test websearch capability via ModelFactory.do_llm_call_with_websearch."""
    import time
    import signal
    
    result = TestResult(
        provider=provider,
        model=model_name,
        test_type="websearch",
        success=False
    )
    
    model_selection = f"{provider}/{model_name}"
    
    try:
        start = time.time()
        response = ModelFactory.do_llm_call_with_websearch(
            model_selection=model_selection,
            prompt="What is 1+1? Reply with just the number.",  # Simple prompt to avoid long searches
            max_tokens=100,
            temperature=0.0
        )
        result.duration_ms = (time.time() - start) * 1000
        
        if response:
            result.response = response[:300]
            result.success = True
        else:
            result.error = "Empty response from websearch"
    
    except KeyboardInterrupt:
        result.error = "Timeout/Interrupted"
    except Exception as e:
        result.error = str(e)[:300]
    
    return result


def print_result(result: TestResult):
    """Print a single test result."""
    status = "✅" if result.success else "❌"
    
    if RICH_AVAILABLE:
        if result.success:
            console.print(f"  {status} [{result.test_type}] {result.model}: {result.duration_ms:.0f}ms")
            if result.response:
                console.print(f"      [dim]{result.response[:100]}...[/dim]" if len(result.response) > 100 else f"      [dim]{result.response}[/dim]")
        else:
            console.print(f"  {status} [{result.test_type}] {result.model}: [red]{result.error}[/red]")
    else:
        if result.success:
            print(f"  {status} [{result.test_type}] {result.model}: {result.duration_ms:.0f}ms")
            if result.response:
                print(f"      {result.response[:100]}..." if len(result.response) > 100 else f"      {result.response}")
        else:
            print(f"  {status} [{result.test_type}] {result.model}: {result.error}")


def run_provider_tests(provider: str) -> ProviderTestSuite:
    """Run all tests for a single provider."""
    has_key, key_setting = check_api_key(provider)
    suite = ProviderTestSuite(
        provider=provider,
        api_key_configured=has_key,
        api_key_setting=key_setting
    )
    
    if not has_key:
        return suite
    
    # Get test model
    test_model = get_test_model_for_provider(provider)
    if not test_model:
        return suite
    
    if RICH_AVAILABLE:
        console.print(f"\n[bold blue]Testing {provider.upper()}[/bold blue] (model: {test_model})")
    else:
        print(f"\nTesting {provider.upper()} (model: {test_model})")
    
    # Test 1: LangChain call
    result = test_langchain_call(provider, test_model)
    suite.results.append(result)
    print_result(result)
    
    # Test 2: Native API call
    result = test_native_call(provider, test_model)
    suite.results.append(result)
    print_result(result)
    
    # Test 3: Tool calling
    model_info = MODELS.get(test_model, {})
    if LABEL_TOOL_CALLING in model_info.get("labels", []):
        result = test_tool_call(provider, test_model)
        suite.results.append(result)
        print_result(result)
    else:
        if RICH_AVAILABLE:
            console.print(f"  ⏭️ [tool_call] Skipped - model doesn't support tool calling")
        else:
            print(f"  ⏭️ [tool_call] Skipped - model doesn't support tool calling")
    
    # Test 4: Websearch
    websearch_model = get_websearch_model_for_provider(provider)
    if websearch_model:
        try:
            result = test_websearch(provider, websearch_model)
            suite.results.append(result)
            print_result(result)
        except Exception as e:
            if RICH_AVAILABLE:
                console.print(f"  ❌ [websearch] {websearch_model}: [red]Error: {str(e)[:100]}[/red]")
            else:
                print(f"  ❌ [websearch] {websearch_model}: Error: {str(e)[:100]}")
    else:
        if RICH_AVAILABLE:
            console.print(f"  ⏭️ [websearch] Skipped - no websearch model available")
        else:
            print(f"  ⏭️ [websearch] Skipped - no websearch model available")
    
    return suite


def print_summary(suites: List[ProviderTestSuite]):
    """Print a summary of all test results."""
    if RICH_AVAILABLE:
        table = Table(title="\n📊 Test Summary")
        table.add_column("Provider", style="cyan")
        table.add_column("API Key", style="green")
        table.add_column("LangChain", justify="center")
        table.add_column("Native", justify="center")
        table.add_column("Tool Call", justify="center")
        table.add_column("Websearch", justify="center")
        
        for suite in suites:
            api_status = "✅" if suite.api_key_configured else "❌"
            
            results_by_type = {r.test_type: r for r in suite.results}
            
            langchain = "✅" if results_by_type.get("langchain", TestResult("","","", False)).success else "❌" if "langchain" in results_by_type else "⏭️"
            native = "✅" if results_by_type.get("native", TestResult("","","", False)).success else "❌" if "native" in results_by_type else "⏭️"
            tool = "✅" if results_by_type.get("tool_call", TestResult("","","", False)).success else "❌" if "tool_call" in results_by_type else "⏭️"
            websearch = "✅" if results_by_type.get("websearch", TestResult("","","", False)).success else "❌" if "websearch" in results_by_type else "⏭️"
            
            if not suite.api_key_configured:
                langchain = native = tool = websearch = "-"
            
            table.add_row(suite.provider, api_status, langchain, native, tool, websearch)
        
        console.print(table)
    else:
        print("\n" + "="*60)
        print("📊 Test Summary")
        print("="*60)
        print(f"{'Provider':<15} {'API Key':<10} {'LangChain':<12} {'Native':<10} {'Tool':<10} {'Websearch':<10}")
        print("-"*60)
        
        for suite in suites:
            api_status = "✅" if suite.api_key_configured else "❌"
            
            results_by_type = {r.test_type: r for r in suite.results}
            
            langchain = "✅" if results_by_type.get("langchain", TestResult("","","", False)).success else "❌" if "langchain" in results_by_type else "⏭️"
            native = "✅" if results_by_type.get("native", TestResult("","","", False)).success else "❌" if "native" in results_by_type else "⏭️"
            tool = "✅" if results_by_type.get("tool_call", TestResult("","","", False)).success else "❌" if "tool_call" in results_by_type else "⏭️"
            websearch = "✅" if results_by_type.get("websearch", TestResult("","","", False)).success else "❌" if "websearch" in results_by_type else "⏭️"
            
            if not suite.api_key_configured:
                langchain = native = tool = websearch = "-"
            
            print(f"{suite.provider:<15} {api_status:<10} {langchain:<12} {native:<10} {tool:<10} {websearch:<10}")


def main():
    """Run all provider tests."""
    if RICH_AVAILABLE:
        console.print(Panel.fit(
            "[bold]LLM Provider Comprehensive Test Suite[/bold]\n"
            "Testing all configured providers with LangChain, Native API, Tool Calls, and Websearch",
            title="🧪 BA2 Trade Platform"
        ))
    else:
        print("="*60)
        print("🧪 LLM Provider Comprehensive Test Suite")
        print("="*60)
    
    # Check which providers have API keys configured
    providers = get_all_providers()
    
    if RICH_AVAILABLE:
        console.print("\n[bold]Checking API key configuration...[/bold]")
    else:
        print("\nChecking API key configuration...")
    
    configured_providers = []
    for provider in providers:
        has_key, key_setting = check_api_key(provider)
        status = "✅" if has_key else "❌"
        if RICH_AVAILABLE:
            console.print(f"  {status} {provider}: {key_setting or 'no key setting'}")
        else:
            print(f"  {status} {provider}: {key_setting or 'no key setting'}")
        
        if has_key:
            configured_providers.append(provider)
    
    if not configured_providers:
        if RICH_AVAILABLE:
            console.print("\n[red]No providers configured with API keys![/red]")
        else:
            print("\nNo providers configured with API keys!")
        return
    
    if RICH_AVAILABLE:
        console.print(f"\n[green]Found {len(configured_providers)} configured provider(s): {', '.join(configured_providers)}[/green]")
    else:
        print(f"\nFound {len(configured_providers)} configured provider(s): {', '.join(configured_providers)}")
    
    # Run tests for each provider
    all_suites = []
    for provider in providers:
        try:
            suite = run_provider_tests(provider)
            all_suites.append(suite)
        except KeyboardInterrupt:
            if RICH_AVAILABLE:
                console.print(f"\n[yellow]Interrupted during {provider} tests[/yellow]")
            else:
                print(f"\nInterrupted during {provider} tests")
            # Add empty suite for this provider
            has_key, key_setting = check_api_key(provider)
            all_suites.append(ProviderTestSuite(provider=provider, api_key_configured=has_key, api_key_setting=key_setting))
        except Exception as e:
            if RICH_AVAILABLE:
                console.print(f"\n[red]Error testing {provider}: {str(e)[:100]}[/red]")
            else:
                print(f"\nError testing {provider}: {str(e)[:100]}")
            has_key, key_setting = check_api_key(provider)
            all_suites.append(ProviderTestSuite(provider=provider, api_key_configured=has_key, api_key_setting=key_setting))
    
    # Print summary
    print_summary(all_suites)
    
    # Count successes and failures
    total_tests = sum(len(s.results) for s in all_suites)
    passed_tests = sum(sum(1 for r in s.results if r.success) for s in all_suites)
    
    if RICH_AVAILABLE:
        console.print(f"\n[bold]Total: {passed_tests}/{total_tests} tests passed[/bold]")
    else:
        print(f"\nTotal: {passed_tests}/{total_tests} tests passed")


if __name__ == "__main__":
    main()

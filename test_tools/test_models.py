"""
Comprehensive Model/Provider Test Script

This script tests LLM providers and models from the registry:
1. Basic LangChain inference tests
2. Native API calls (direct SDK usage)
3. Tool calling capability
4. Websearch capability

Usage:
    python test_tools/test_models.py                           # Quick test (1 model/provider)
    python test_tools/test_models.py --all                     # Test all models
    python test_tools/test_models.py --provider moonshot       # Test specific provider
    python test_tools/test_models.py --model kimi_k2.5         # Test specific model
    python test_tools/test_models.py --tool-call               # Test tool calling only
    python test_tools/test_models.py --websearch               # Test websearch only
    python test_tools/test_models.py --model kimi_k2.5 --tool-call --websearch  # Combined

Options:
    --all           Test all model/provider combinations
    --provider      Comma-separated list of providers to test
    --model         Comma-separated list of models to test
    --tool-call     Include tool calling tests
    --websearch     Include websearch tests
    --verbose       Show detailed error messages
    --output        Output file path for JSON results
"""

import sys
import os
import json
import time
import argparse
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

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
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None
    print("Note: Install 'rich' for better output: pip install rich")


@dataclass
class TestResult:
    """Result of a single test."""
    model: str
    provider: str
    provider_model_name: str
    test_type: str  # "inference", "native", "tool_call", "websearch"
    success: bool
    response: str = ""
    error: str = ""
    duration_ms: float = 0.0
    labels: List[str] = field(default_factory=list)


@dataclass
class ProviderSummary:
    """Summary of all tests for a provider."""
    provider: str
    display_name: str
    api_key_configured: bool
    api_key_setting: str = ""
    total_models: int = 0
    inference_passed: int = 0
    inference_failed: int = 0
    tool_call_passed: int = 0
    tool_call_failed: int = 0
    websearch_passed: int = 0
    websearch_failed: int = 0
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


def get_models_for_provider(provider: str) -> List[Tuple[str, str, Dict]]:
    """
    Get all models available for a specific provider.

    Returns:
        List of (friendly_name, provider_model_name, model_info) tuples
    """
    # Priority order for test models per provider (most reliable first)
    preferred_order = {
        PROVIDER_OPENAI: ["gpt4o_mini", "gpt4o", "gpt5_mini", "gpt5"],
        PROVIDER_NAGAAI: ["gpt4o_mini", "grok3_mini", "gpt5_mini", "gpt5"],
        PROVIDER_GOOGLE: ["gemini_2.0_flash", "gemini_2.5_flash", "gemini_3_flash"],
        PROVIDER_ANTHROPIC: ["claude_3.5_haiku", "claude_3.5_sonnet", "claude_4_sonnet"],
        PROVIDER_XAI: ["grok3_mini", "grok3", "grok4_fast"],
        PROVIDER_DEEPSEEK: ["deepseek_chat", "deepseek_coder", "deepseek_reasoner"],
        PROVIDER_MOONSHOT: ["kimi_k2.5", "kimi_k2", "kimi_k1.5"],
        PROVIDER_OPENROUTER: ["gpt4o_mini", "claude_3.5_haiku", "grok3_mini"],
        PROVIDER_BEDROCK: ["llama3_1_8b", "llama3_1_70b"],
    }

    models = []
    for model_name, model_info in MODELS.items():
        provider_names = model_info.get("provider_names", {})
        if provider in provider_names:
            models.append((model_name, provider_names[provider], model_info))

    # Sort: preferred models first, then alphabetically
    preferred = preferred_order.get(provider, [])
    def sort_key(item):
        model_name = item[0]
        if model_name in preferred:
            return (0, preferred.index(model_name))
        return (1, model_name)

    return sorted(models, key=sort_key)


def test_inference(provider: str, model_name: str, model_info: Dict, verbose: bool = False) -> TestResult:
    """Test basic LangChain inference."""
    provider_model_name = get_model_for_provider(model_name, provider) or "unknown"

    result = TestResult(
        model=model_name,
        provider=provider,
        provider_model_name=provider_model_name,
        test_type="inference",
        success=False,
        labels=model_info.get("labels", [])
    )

    try:
        model_selection = f"{provider}/{model_name}"
        start = time.time()

        llm = ModelFactory.create_llm(model_selection, temperature=0.0)
        response = llm.invoke("What is 2+2? Reply with just the number.")

        result.duration_ms = (time.time() - start) * 1000

        if hasattr(response, 'content'):
            result.response = str(response.content)[:100]
        else:
            result.response = str(response)[:100]

        result.success = True

    except Exception as e:
        result.error = str(e)[:300]
        if verbose:
            import traceback
            traceback.print_exc()

    return result


def test_native_call(provider: str, model_name: str, model_info: Dict, verbose: bool = False) -> TestResult:
    """Test native API call (direct SDK usage)."""
    from openai import OpenAI

    provider_model_name = get_model_for_provider(model_name, provider) or "unknown"

    result = TestResult(
        model=model_name,
        provider=provider,
        provider_model_name=provider_model_name,
        test_type="native",
        success=False,
        labels=model_info.get("labels", [])
    )

    try:
        config = PROVIDER_CONFIG.get(provider, {})
        base_url = config.get("base_url")
        api_key_setting = config.get("api_key_setting", "")
        api_key = get_app_setting(api_key_setting) if api_key_setting else None

        if not api_key:
            result.error = f"API key not found for {api_key_setting}"
            return result

        start = time.time()

        # Provider-specific handling
        if provider == PROVIDER_GOOGLE:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=provider_model_name,
                contents="What is 2+2? Reply with just the number."
            )
            result.response = response.text[:100] if response.text else ""

        elif provider == PROVIDER_ANTHROPIC:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            response = client.messages.create(
                model=provider_model_name,
                max_tokens=100,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}]
            )
            result.response = response.content[0].text[:100] if response.content else ""

        elif provider == PROVIDER_DEEPSEEK:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model=provider_model_name,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.response = response.choices[0].message.content[:100] if response.choices else ""

        elif provider == PROVIDER_XAI:
            from xai_sdk import Client as XAIClient
            from xai_sdk.chat import user
            client = XAIClient(api_key=api_key)
            chat = client.chat.create(model=provider_model_name)
            chat.append(user("What is 2+2? Reply with just the number."))
            response = chat.sample()
            result.response = response.content[:100] if response.content else ""

        elif provider == PROVIDER_MOONSHOT:
            client = OpenAI(api_key=api_key, base_url="https://api.moonshot.ai/v1")
            response = client.chat.completions.create(
                model=provider_model_name,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.response = response.choices[0].message.content[:100] if response.choices else ""

        else:
            # OpenAI, NagaAI, OpenRouter use OpenAI-compatible API
            client = OpenAI(api_key=api_key, base_url=base_url)
            response = client.chat.completions.create(
                model=provider_model_name,
                messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
                max_tokens=100
            )
            result.response = response.choices[0].message.content[:100] if response.choices else ""

        result.duration_ms = (time.time() - start) * 1000
        result.success = True

    except Exception as e:
        result.error = str(e)[:300]
        if verbose:
            import traceback
            traceback.print_exc()

    return result


def test_tool_call(provider: str, model_name: str, model_info: Dict, verbose: bool = False) -> TestResult:
    """Test tool calling capability via LangChain."""
    from langchain_core.tools import tool

    provider_model_name = get_model_for_provider(model_name, provider) or "unknown"

    result = TestResult(
        model=model_name,
        provider=provider,
        provider_model_name=provider_model_name,
        test_type="tool_call",
        success=False,
        labels=model_info.get("labels", [])
    )

    # Check if model supports tool calling
    if LABEL_TOOL_CALLING not in model_info.get("labels", []):
        result.error = "Model doesn't support tool calling (no tool_calling label)"
        return result

    @tool
    def get_weather(city: str) -> str:
        """Get the current weather in a city."""
        return f"The weather in {city} is sunny and 72F"

    try:
        model_selection = f"{provider}/{model_name}"
        start = time.time()

        llm = ModelFactory.create_llm(model_selection, temperature=0.0)
        llm_with_tools = llm.bind_tools([get_weather])

        response = llm_with_tools.invoke("What's the weather in San Francisco?")
        result.duration_ms = (time.time() - start) * 1000

        # Check if tool call was made
        if hasattr(response, 'tool_calls') and response.tool_calls:
            tool_call = response.tool_calls[0]
            result.response = f"Tool: {tool_call.get('name', 'unknown')}, Args: {tool_call.get('args', {})}"
            result.success = True
        elif hasattr(response, 'additional_kwargs') and response.additional_kwargs.get('tool_calls'):
            tool_calls = response.additional_kwargs['tool_calls']
            result.response = f"Tool: {tool_calls[0]['function']['name']}"
            result.success = True
        else:
            content = response.content if hasattr(response, 'content') else str(response)
            result.response = f"No tool call. Response: {content[:100]}"
            result.success = False

    except Exception as e:
        result.error = str(e)[:300]
        if verbose:
            import traceback
            traceback.print_exc()

    return result


def test_websearch(provider: str, model_name: str, model_info: Dict, verbose: bool = False) -> TestResult:
    """Test websearch capability via ModelFactory.do_llm_call_with_websearch."""
    provider_model_name = get_model_for_provider(model_name, provider) or "unknown"

    result = TestResult(
        model=model_name,
        provider=provider,
        provider_model_name=provider_model_name,
        test_type="websearch",
        success=False,
        labels=model_info.get("labels", [])
    )

    # Check if model supports websearch
    if LABEL_WEBSEARCH not in model_info.get("labels", []):
        result.error = "Model doesn't support websearch (no websearch label)"
        return result

    try:
        model_selection = f"{provider}/{model_name}"
        start = time.time()

        response = ModelFactory.do_llm_call_with_websearch(
            model_selection=model_selection,
            prompt="What is the current date today? Reply briefly.",
            max_tokens=1024,  # Some models need more tokens for websearch metadata
            temperature=0.0
        )

        result.duration_ms = (time.time() - start) * 1000

        if response:
            result.response = response[:200]
            result.success = True
        else:
            result.error = "Empty response from websearch"

    except Exception as e:
        result.error = str(e)[:300]
        if verbose:
            import traceback
            traceback.print_exc()

    return result


def run_tests(
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    test_all: bool = False,
    include_tool_call: bool = False,
    include_websearch: bool = False,
    verbose: bool = False
) -> Dict[str, ProviderSummary]:
    """Run tests for specified model/provider combinations."""
    results: Dict[str, ProviderSummary] = {}

    # Get all providers
    all_providers = get_all_providers()
    if providers:
        all_providers = [p for p in all_providers if p.lower() in [x.lower() for x in providers]]

    # Check API keys and prepare provider summaries
    providers_to_test = []
    for provider in all_providers:
        has_key, key_setting = check_api_key(provider)
        config = PROVIDER_CONFIG.get(provider, {})

        summary = ProviderSummary(
            provider=provider,
            display_name=config.get("display_name", provider),
            api_key_configured=has_key,
            api_key_setting=key_setting
        )
        results[provider] = summary

        if has_key:
            providers_to_test.append(provider)
        else:
            if RICH_AVAILABLE:
                console.print(f"[yellow]Skip {provider}: No API key ({key_setting})[/yellow]")
            else:
                print(f"Skip {provider}: No API key ({key_setting})")

    if not providers_to_test:
        print("\nNo providers have API keys configured!")
        return results

    # Build test queue
    test_queue: List[Tuple[str, str, Dict, str]] = []  # (provider, model, model_info, test_type)

    for provider in providers_to_test:
        provider_models = get_models_for_provider(provider)

        # Filter by specified models if any
        if models:
            provider_models = [(m, pn, info) for m, pn, info in provider_models
                            if m.lower() in [x.lower() for x in models]]

        # If not testing all, only use first model
        if not test_all and not models:
            provider_models = provider_models[:1]

        results[provider].total_models = len(provider_models)

        for model_name, _, model_info in provider_models:
            # Always test inference
            test_queue.append((provider, model_name, model_info, "inference"))

            # Optionally test tool calling
            if include_tool_call:
                test_queue.append((provider, model_name, model_info, "tool_call"))

            # Optionally test websearch
            if include_websearch:
                test_queue.append((provider, model_name, model_info, "websearch"))

    total_tests = len(test_queue)
    if RICH_AVAILABLE:
        console.print(f"\n[bold]Running {total_tests} tests...[/bold]\n")
    else:
        print(f"\nRunning {total_tests} tests...\n")

    # Run tests
    completed = 0
    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Testing...", total=total_tests)

            for provider, model_name, model_info, test_type in test_queue:
                progress.update(task, description=f"{test_type}: {provider}/{model_name}")

                if test_type == "inference":
                    result = test_inference(provider, model_name, model_info, verbose)
                elif test_type == "tool_call":
                    result = test_tool_call(provider, model_name, model_info, verbose)
                elif test_type == "websearch":
                    result = test_websearch(provider, model_name, model_info, verbose)

                results[provider].results.append(result)

                # Update counters
                if test_type == "inference":
                    if result.success:
                        results[provider].inference_passed += 1
                    else:
                        results[provider].inference_failed += 1
                elif test_type == "tool_call":
                    if result.success:
                        results[provider].tool_call_passed += 1
                    else:
                        results[provider].tool_call_failed += 1
                elif test_type == "websearch":
                    if result.success:
                        results[provider].websearch_passed += 1
                    else:
                        results[provider].websearch_failed += 1

                completed += 1
                progress.update(task, completed=completed)
    else:
        for provider, model_name, model_info, test_type in test_queue:
            print(f"  {test_type}: {provider}/{model_name}...", end=" ", flush=True)

            if test_type == "inference":
                result = test_inference(provider, model_name, model_info, verbose)
            elif test_type == "tool_call":
                result = test_tool_call(provider, model_name, model_info, verbose)
            elif test_type == "websearch":
                result = test_websearch(provider, model_name, model_info, verbose)

            results[provider].results.append(result)

            if result.success:
                print(f"OK ({result.duration_ms:.0f}ms)")
            else:
                error_preview = result.error[:50] + "..." if len(result.error) > 50 else result.error
                print(f"FAIL ({error_preview})")

            # Update counters
            if test_type == "inference":
                if result.success:
                    results[provider].inference_passed += 1
                else:
                    results[provider].inference_failed += 1
            elif test_type == "tool_call":
                if result.success:
                    results[provider].tool_call_passed += 1
                else:
                    results[provider].tool_call_failed += 1
            elif test_type == "websearch":
                if result.success:
                    results[provider].websearch_passed += 1
                else:
                    results[provider].websearch_failed += 1

    return results


def print_results(results: Dict[str, ProviderSummary], show_tool_call: bool, show_websearch: bool):
    """Print test results."""

    if RICH_AVAILABLE:
        # Summary table
        summary_table = Table(title="\nTest Summary", show_header=True)
        summary_table.add_column("Provider", style="cyan")
        summary_table.add_column("API Key", justify="center")
        summary_table.add_column("Inference", justify="center")
        if show_tool_call:
            summary_table.add_column("Tool Call", justify="center")
        if show_websearch:
            summary_table.add_column("Websearch", justify="center")

        for provider, summary in sorted(results.items()):
            key_status = "[green]OK[/green]" if summary.api_key_configured else "[red]NO[/red]"

            inf_total = summary.inference_passed + summary.inference_failed
            inference = f"[green]{summary.inference_passed}[/green]/{inf_total}" if inf_total > 0 else "-"

            row = [summary.display_name, key_status, inference]

            if show_tool_call:
                tc_total = summary.tool_call_passed + summary.tool_call_failed
                tool_call = f"[green]{summary.tool_call_passed}[/green]/{tc_total}" if tc_total > 0 else "-"
                row.append(tool_call)

            if show_websearch:
                ws_total = summary.websearch_passed + summary.websearch_failed
                websearch = f"[green]{summary.websearch_passed}[/green]/{ws_total}" if ws_total > 0 else "-"
                row.append(websearch)

            summary_table.add_row(*row)

        console.print(summary_table)

        # Detailed results
        all_results = []
        for summary in results.values():
            all_results.extend(summary.results)

        if all_results:
            detail_table = Table(title="\nDetailed Results", show_header=True)
            detail_table.add_column("Status", justify="center", width=4)
            detail_table.add_column("Type", width=10)
            detail_table.add_column("Provider", style="cyan", width=10)
            detail_table.add_column("Model", style="yellow", width=20)
            detail_table.add_column("Time", justify="right", width=8)
            detail_table.add_column("Response/Error", width=50)

            for r in sorted(all_results, key=lambda x: (not x.success, x.test_type, x.provider)):
                status = "[green]OK[/green]" if r.success else "[red]FAIL[/red]"
                duration = f"{r.duration_ms:.0f}ms" if r.duration_ms > 0 else "-"
                detail = r.response[:50] if r.success else r.error[:50]
                if len(r.response if r.success else r.error) > 50:
                    detail += "..."

                detail_table.add_row(status, r.test_type, r.provider, r.model, duration, detail)

            console.print(detail_table)
    else:
        print("\n" + "=" * 80)
        print("TEST SUMMARY")
        print("=" * 80)

        for provider, summary in sorted(results.items()):
            key_status = "OK" if summary.api_key_configured else "NO"
            inf_total = summary.inference_passed + summary.inference_failed

            line = f"  {summary.display_name:15} Key:{key_status:3}  Inference:{summary.inference_passed}/{inf_total}"

            if show_tool_call:
                tc_total = summary.tool_call_passed + summary.tool_call_failed
                line += f"  ToolCall:{summary.tool_call_passed}/{tc_total}"

            if show_websearch:
                ws_total = summary.websearch_passed + summary.websearch_failed
                line += f"  Websearch:{summary.websearch_passed}/{ws_total}"

            print(line)


def save_results(results: Dict[str, ProviderSummary], output_path: str):
    """Save results to JSON file."""
    output = {
        "timestamp": datetime.now().isoformat(),
        "providers": {}
    }

    for provider, summary in results.items():
        output["providers"][provider] = {
            "display_name": summary.display_name,
            "api_key_configured": summary.api_key_configured,
            "total_models": summary.total_models,
            "inference_passed": summary.inference_passed,
            "inference_failed": summary.inference_failed,
            "tool_call_passed": summary.tool_call_passed,
            "tool_call_failed": summary.tool_call_failed,
            "websearch_passed": summary.websearch_passed,
            "websearch_failed": summary.websearch_failed,
            "results": [asdict(r) for r in summary.results]
        }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Test LLM models and providers")
    parser.add_argument("--all", action="store_true", help="Test all models (default: 1 per provider)")
    parser.add_argument("--provider", type=str, help="Comma-separated providers to test")
    parser.add_argument("--model", type=str, help="Comma-separated models to test")
    parser.add_argument("--tool-call", action="store_true", help="Include tool calling tests")
    parser.add_argument("--websearch", action="store_true", help="Include websearch tests")
    parser.add_argument("--verbose", action="store_true", help="Show detailed errors")
    parser.add_argument("--output", type=str, default="test_tools/test_models_results.json", help="Output JSON file")

    args = parser.parse_args()

    # Parse lists
    providers = args.provider.split(",") if args.provider else None
    models = args.model.split(",") if args.model else None

    # Header
    mode = "all models" if args.all else ("specific" if models else "quick (1/provider)")
    tests = ["inference"]
    if args.tool_call:
        tests.append("tool_call")
    if args.websearch:
        tests.append("websearch")

    if RICH_AVAILABLE:
        console.print(Panel.fit(
            f"[bold]Model/Provider Test Suite[/bold]\n"
            f"Mode: {mode}\n"
            f"Tests: {', '.join(tests)}\n"
            f"Providers: {providers or 'all with keys'}\n"
            f"Models: {models or 'auto'}",
            title="BA2 Trade Platform"
        ))
    else:
        print("\n" + "=" * 60)
        print("Model/Provider Test Suite")
        print("=" * 60)
        print(f"Mode: {mode}")
        print(f"Tests: {', '.join(tests)}")

    # Run tests
    results = run_tests(
        providers=providers,
        models=models,
        test_all=args.all or bool(models),
        include_tool_call=args.tool_call,
        include_websearch=args.websearch,
        verbose=args.verbose
    )

    # Print and save
    print_results(results, args.tool_call, args.websearch)
    save_results(results, args.output)

    # Summary
    total_passed = sum(s.inference_passed + s.tool_call_passed + s.websearch_passed for s in results.values())
    total_failed = sum(s.inference_failed + s.tool_call_failed + s.websearch_failed for s in results.values())
    total = total_passed + total_failed

    if RICH_AVAILABLE:
        if total_failed == 0:
            console.print(f"\n[bold green]All {total} tests passed![/bold green]")
        else:
            console.print(f"\n[bold yellow]{total_passed}/{total} passed, {total_failed} failed[/bold yellow]")
    else:
        print(f"\n{total_passed}/{total} passed, {total_failed} failed")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

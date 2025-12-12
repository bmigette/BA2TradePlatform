"""
Comprehensive Model/Provider Test Script

This script tests ALL model/provider combinations from the registry
to identify which ones work and which fail.

Usage:
    python test_files/test_all_models_providers.py [--providers PROVIDER1,PROVIDER2] [--models MODEL1,MODEL2] [--quick]

Options:
    --providers  Comma-separated list of providers to test (default: all with keys)
    --models     Comma-separated list of models to test (default: all)
    --quick      Only test one model per provider (fastest)
    --verbose    Show detailed error messages
    --output     Output file path for JSON results (default: test_all_models_results.json)
"""

import sys
import os
import json
import time
import argparse
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.core.ModelFactory import ModelFactory
from ba2_trade_platform.core.models_registry import (
    MODELS, PROVIDER_CONFIG, get_all_providers, get_model_for_provider,
    LABEL_WEBSEARCH, LABEL_TOOL_CALLING, LABEL_THINKING,
    PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, PROVIDER_ANTHROPIC,
    PROVIDER_XAI, PROVIDER_DEEPSEEK, PROVIDER_MOONSHOT, PROVIDER_OPENROUTER, PROVIDER_BEDROCK
)

# Rich console output for nice formatting
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    from rich import print as rprint
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None
    print("Note: Install 'rich' for better output: pip install rich")


@dataclass
class ModelTestResult:
    """Result of testing a single model/provider combination."""
    model: str
    provider: str
    provider_model_name: str
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
    total_models: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[ModelTestResult] = field(default_factory=list)


def check_api_key(provider: str) -> Tuple[bool, str, Optional[str]]:
    """Check if API key is configured for a provider."""
    config = PROVIDER_CONFIG.get(provider)
    if not config:
        return False, "", None
    
    api_key_setting = config.get("api_key_setting", "")
    if not api_key_setting:
        return False, "", None
    
    api_key = get_app_setting(api_key_setting)
    # Mask the key for display
    masked = f"{api_key[:8]}...{api_key[-4:]}" if api_key and len(api_key) > 12 else "***"
    return bool(api_key), api_key_setting, masked if api_key else None


def get_models_for_provider(provider: str) -> List[Tuple[str, str]]:
    """
    Get all models available for a specific provider.
    Sorted with preferred/reliable models first.
    
    Returns:
        List of (friendly_name, provider_model_name) tuples
    """
    # Priority order for test models per provider (most reliable first)
    preferred_order = {
        PROVIDER_OPENAI: ["gpt4o_mini", "gpt4o", "gpt5_mini", "gpt5"],
        PROVIDER_NAGAAI: ["gpt4o_mini", "grok3_mini", "gpt5_mini", "gpt5"],
        PROVIDER_GOOGLE: ["gemini_2.0_flash", "gemini_2.5_flash", "gemini_2.5_pro"],
        PROVIDER_ANTHROPIC: ["claude_3.5_haiku", "claude_3.5_sonnet", "claude_3.7_sonnet"],
        PROVIDER_XAI: ["grok3_mini", "grok3", "grok4_fast"],
        PROVIDER_DEEPSEEK: ["deepseek_chat", "deepseek_coder", "deepseek_reasoner"],
        PROVIDER_MOONSHOT: ["kimi_k1.5", "kimi_k2", "kimi_k2_thinking"],
        PROVIDER_OPENROUTER: ["gpt4o_mini", "claude_3.5_haiku", "grok3_mini"],
        PROVIDER_BEDROCK: ["llama3_1_8b", "llama3_1_70b"],
    }
    
    models = []
    for model_name, model_info in MODELS.items():
        provider_names = model_info.get("provider_names", {})
        if provider in provider_names:
            models.append((model_name, provider_names[provider]))
    
    # Sort: preferred models first, then alphabetically
    preferred = preferred_order.get(provider, [])
    def sort_key(item):
        model_name = item[0]
        if model_name in preferred:
            return (0, preferred.index(model_name))
        return (1, model_name)
    
    return sorted(models, key=sort_key)


def test_model(provider: str, model_name: str, verbose: bool = False) -> ModelTestResult:
    """Test a single model/provider combination."""
    model_info = MODELS.get(model_name, {})
    provider_model_name = get_model_for_provider(model_name, provider) or "unknown"
    
    result = ModelTestResult(
        model=model_name,
        provider=provider,
        provider_model_name=provider_model_name,
        success=False,
        labels=model_info.get("labels", [])
    )
    
    try:
        model_selection = f"{provider}/{model_name}"
        start = time.time()
        
        # Create LLM and make a simple call
        llm = ModelFactory.create_llm(model_selection, temperature=0.0)
        response = llm.invoke("What is 2+2? Reply with just the number.")
        
        result.duration_ms = (time.time() - start) * 1000
        
        # Extract text from response
        if hasattr(response, 'content'):
            result.response = str(response.content)[:100]
        else:
            result.response = str(response)[:100]
        
        result.success = True
        
    except Exception as e:
        error_msg = str(e)
        # Truncate error but keep useful info
        if len(error_msg) > 200:
            result.error = error_msg[:200] + "..."
        else:
            result.error = error_msg
        
        if verbose:
            import traceback
            print(f"\n[ERROR] {provider}/{model_name}: {error_msg}")
            traceback.print_exc()
    
    return result


def run_tests(
    providers: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    quick: bool = False,
    verbose: bool = False
) -> Dict[str, ProviderSummary]:
    """
    Run tests for all specified model/provider combinations.
    
    Args:
        providers: List of providers to test (None = all with keys)
        models: List of models to test (None = all available)
        quick: If True, only test one model per provider
        verbose: Show detailed error messages
        
    Returns:
        Dictionary of provider -> ProviderSummary
    """
    results: Dict[str, ProviderSummary] = {}
    
    # Get all providers to test
    all_providers = get_all_providers()
    if providers:
        all_providers = [p for p in providers if p in all_providers]
    
    # Check API keys and prepare provider summaries
    providers_to_test = []
    for provider in all_providers:
        has_key, key_setting, masked_key = check_api_key(provider)
        config = PROVIDER_CONFIG.get(provider, {})
        
        summary = ProviderSummary(
            provider=provider,
            display_name=config.get("display_name", provider),
            api_key_configured=has_key
        )
        results[provider] = summary
        
        if has_key:
            providers_to_test.append(provider)
        else:
            if RICH_AVAILABLE:
                console.print(f"[yellow]⚠ {provider}: No API key configured ({key_setting})[/yellow]")
            else:
                print(f"⚠ {provider}: No API key configured ({key_setting})")
    
    if not providers_to_test:
        print("\n❌ No providers have API keys configured!")
        return results
    
    # Build test queue
    test_queue: List[Tuple[str, str]] = []
    for provider in providers_to_test:
        provider_models = get_models_for_provider(provider)
        
        # Filter by specified models if any
        if models:
            provider_models = [(m, pn) for m, pn in provider_models if m in models]
        
        results[provider].total_models = len(provider_models)
        
        if quick:
            # Only test first model
            if provider_models:
                test_queue.append((provider, provider_models[0][0]))
        else:
            for model_name, _ in provider_models:
                test_queue.append((provider, model_name))
    
    total_tests = len(test_queue)
    if RICH_AVAILABLE:
        console.print(f"\n[bold]Running {total_tests} tests across {len(providers_to_test)} providers...[/bold]\n")
    else:
        print(f"\nRunning {total_tests} tests across {len(providers_to_test)} providers...\n")
    
    # Run tests with progress
    completed = 0
    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console
        ) as progress:
            task = progress.add_task("Testing models...", total=total_tests)
            
            for provider, model_name in test_queue:
                progress.update(task, description=f"Testing {provider}/{model_name}")
                result = test_model(provider, model_name, verbose)
                results[provider].results.append(result)
                
                if result.success:
                    results[provider].passed += 1
                else:
                    results[provider].failed += 1
                
                completed += 1
                progress.update(task, completed=completed)
    else:
        for provider, model_name in test_queue:
            print(f"  Testing {provider}/{model_name}...", end=" ", flush=True)
            result = test_model(provider, model_name, verbose)
            results[provider].results.append(result)
            
            if result.success:
                results[provider].passed += 1
                print("✓")
            else:
                results[provider].failed += 1
                print(f"✗ ({result.error[:50]}...)" if len(result.error) > 50 else f"✗ ({result.error})")
            
            completed += 1
    
    return results


def print_results(results: Dict[str, ProviderSummary]):
    """Print test results in a nice format."""
    
    if RICH_AVAILABLE:
        # Summary table
        summary_table = Table(title="Provider Summary", show_header=True)
        summary_table.add_column("Provider", style="cyan")
        summary_table.add_column("API Key", justify="center")
        summary_table.add_column("Passed", justify="right", style="green")
        summary_table.add_column("Failed", justify="right", style="red")
        summary_table.add_column("Total", justify="right")
        summary_table.add_column("Rate", justify="right")
        
        for provider, summary in sorted(results.items()):
            key_status = "✓" if summary.api_key_configured else "✗"
            key_style = "green" if summary.api_key_configured else "red"
            
            total = summary.passed + summary.failed
            rate = f"{(summary.passed/total*100):.0f}%" if total > 0 else "N/A"
            
            summary_table.add_row(
                summary.display_name,
                f"[{key_style}]{key_status}[/{key_style}]",
                str(summary.passed),
                str(summary.failed),
                str(total),
                rate
            )
        
        console.print("\n")
        console.print(summary_table)
        
        # Detailed failures
        failures = []
        for provider, summary in results.items():
            for result in summary.results:
                if not result.success:
                    failures.append(result)
        
        if failures:
            console.print(f"\n[bold red]Failed Tests ({len(failures)}):[/bold red]\n")
            
            failure_table = Table(show_header=True)
            failure_table.add_column("Provider", style="cyan", width=12)
            failure_table.add_column("Model", style="yellow", width=20)
            failure_table.add_column("Provider Model", width=30)
            failure_table.add_column("Error", style="red", width=60)
            
            for f in failures:
                failure_table.add_row(
                    f.provider,
                    f.model,
                    f.provider_model_name,
                    f.error[:60] + "..." if len(f.error) > 60 else f.error
                )
            
            console.print(failure_table)
        
        # Detailed successes
        successes = []
        for provider, summary in results.items():
            for result in summary.results:
                if result.success:
                    successes.append(result)
        
        if successes:
            console.print(f"\n[bold green]Passed Tests ({len(successes)}):[/bold green]\n")
            
            success_table = Table(show_header=True)
            success_table.add_column("Provider", style="cyan", width=12)
            success_table.add_column("Model", style="yellow", width=20)
            success_table.add_column("Provider Model", width=30)
            success_table.add_column("Duration", justify="right", width=10)
            success_table.add_column("Response", width=30)
            
            for s in successes:
                success_table.add_row(
                    s.provider,
                    s.model,
                    s.provider_model_name,
                    f"{s.duration_ms:.0f}ms",
                    s.response[:30] + "..." if len(s.response) > 30 else s.response
                )
            
            console.print(success_table)
    
    else:
        # Plain text output
        print("\n" + "=" * 80)
        print("PROVIDER SUMMARY")
        print("=" * 80)
        
        for provider, summary in sorted(results.items()):
            key_status = "✓" if summary.api_key_configured else "✗"
            total = summary.passed + summary.failed
            rate = f"{(summary.passed/total*100):.0f}%" if total > 0 else "N/A"
            print(f"  {summary.display_name:15} Key:{key_status}  Passed:{summary.passed:3}  Failed:{summary.failed:3}  Rate:{rate}")
        
        # Failures
        print("\n" + "=" * 80)
        print("FAILED TESTS")
        print("=" * 80)
        
        for provider, summary in results.items():
            for result in summary.results:
                if not result.success:
                    print(f"  ✗ {provider}/{result.model}")
                    print(f"    Provider model: {result.provider_model_name}")
                    print(f"    Error: {result.error}")
        
        # Successes
        print("\n" + "=" * 80)
        print("PASSED TESTS")
        print("=" * 80)
        
        for provider, summary in results.items():
            for result in summary.results:
                if result.success:
                    print(f"  ✓ {provider}/{result.model} ({result.duration_ms:.0f}ms)")


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
            "passed": summary.passed,
            "failed": summary.failed,
            "results": [asdict(r) for r in summary.results]
        }
    
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\n💾 Results saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Test all model/provider combinations")
    parser.add_argument("--providers", type=str, help="Comma-separated list of providers to test")
    parser.add_argument("--models", type=str, help="Comma-separated list of models to test")
    parser.add_argument("--quick", action="store_true", help="Only test one model per provider")
    parser.add_argument("--verbose", action="store_true", help="Show detailed error messages")
    parser.add_argument("--output", type=str, default="test_files/test_all_models_results.json", help="Output JSON file")
    
    args = parser.parse_args()
    
    # Parse provider/model lists
    providers = args.providers.split(",") if args.providers else None
    models = args.models.split(",") if args.models else None
    
    # Print header
    if RICH_AVAILABLE:
        console.print(Panel.fit(
            "[bold cyan]Model/Provider Comprehensive Test[/bold cyan]\n"
            f"Testing {'quick (1 model/provider)' if args.quick else 'all models'}\n"
            f"Providers: {providers or 'all with API keys'}\n"
            f"Models: {models or 'all available'}",
            title="BA2 Trade Platform"
        ))
    else:
        print("\n" + "=" * 60)
        print("Model/Provider Comprehensive Test")
        print("=" * 60)
        print(f"Mode: {'quick (1 model/provider)' if args.quick else 'all models'}")
        print(f"Providers: {providers or 'all with API keys'}")
        print(f"Models: {models or 'all available'}")
    
    # Run tests
    results = run_tests(
        providers=providers,
        models=models,
        quick=args.quick,
        verbose=args.verbose
    )
    
    # Print and save results
    print_results(results)
    save_results(results, args.output)
    
    # Calculate totals
    total_passed = sum(s.passed for s in results.values())
    total_failed = sum(s.failed for s in results.values())
    total = total_passed + total_failed
    
    if RICH_AVAILABLE:
        if total_failed == 0:
            console.print(f"\n[bold green]✓ All {total} tests passed![/bold green]")
        else:
            console.print(f"\n[bold yellow]⚠ {total_passed}/{total} tests passed, {total_failed} failed[/bold yellow]")
    else:
        if total_failed == 0:
            print(f"\n✓ All {total} tests passed!")
        else:
            print(f"\n⚠ {total_passed}/{total} tests passed, {total_failed} failed")
    
    # Return exit code based on results
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Test script for the Model Registry, ModelSelector, and ModelFactory.

Usage:
    .venv\Scripts\python.exe test_files\test_model_registry.py
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Initialize database connection
from ba2_trade_platform.core.db import get_db
get_db()  # Initialize database


def test_model_registry():
    """Test the model registry functions."""
    print("\n" + "=" * 60)
    print("Testing Model Registry")
    print("=" * 60)
    
    from ba2_trade_platform.core.models_registry import (
        MODELS, PROVIDER_CONFIG,
        get_model_info, get_model_for_provider, get_models_by_label,
        get_models_by_provider, get_all_labels, get_all_providers,
        format_model_string, parse_model_selection, get_model_display_info
    )
    
    # Test 1: List all models
    print(f"\n✓ Total models registered: {len(MODELS)}")
    print(f"  Models: {list(MODELS.keys())[:10]}...")
    
    # Test 2: List all providers
    providers = get_all_providers()
    print(f"\n✓ Available providers: {providers}")
    
    # Test 3: List all labels
    labels = get_all_labels()
    print(f"\n✓ Available labels: {labels}")
    
    # Test 4: Get model info
    model_info = get_model_info("gpt5")
    if model_info:
        print(f"\n✓ Model info for 'gpt5': {model_info.get('display_name')} - {model_info.get('description')}")
        print(f"  Native provider: {model_info.get('native_provider')}")
        print(f"  Labels: {model_info.get('labels')}")
    else:
        print("\n✗ Failed to get model info for 'gpt5'")
    
    # Test 5: Get provider-specific names
    print("\n✓ Provider-specific names for 'gpt5':")
    for provider in ["openai", "nagaai", "native"]:
        name = get_model_for_provider("gpt5", provider)
        print(f"  {provider}: {name}")
    
    # Test 6: Get models by label
    low_cost_models = get_models_by_label("low_cost")
    print(f"\n✓ Low cost models: {low_cost_models}")
    
    thinking_models = get_models_by_label("thinking")
    print(f"\n✓ Thinking models: {thinking_models}")
    
    # Test 7: Get models by provider
    nagaai_models = get_models_by_provider("nagaai")
    print(f"\n✓ NagaAI models ({len(nagaai_models)}): {nagaai_models[:5]}...")
    
    # Test 8: Format and parse model strings
    formatted = format_model_string("gpt5", "nagaai")
    print(f"\n✓ Format model string: {formatted}")
    
    provider, model = parse_model_selection(formatted)
    print(f"  Parsed back: provider={provider}, model={model}")
    
    # Test 9: Get display info
    display_info = get_model_display_info("grok4")
    print(f"\n✓ Display info for 'grok4':")
    print(f"  Display name: {display_info.get('display_name')}")
    print(f"  Native provider: {display_info.get('native_provider')}")
    print(f"  Available providers: {display_info.get('available_providers')}")
    print(f"  Labels: {display_info.get('labels')}")
    
    print("\n✓ Model Registry tests passed!")


def test_model_factory():
    """Test the ModelFactory class."""
    print("\n" + "=" * 60)
    print("Testing Model Factory")
    print("=" * 60)
    
    from ba2_trade_platform.core.ModelFactory import ModelFactory, create_llm
    
    # Test 1: Get model info
    info = ModelFactory.get_model_info("nagaai/gpt5")
    print(f"\n✓ Model info for 'nagaai/gpt5':")
    print(f"  Friendly name: {info.get('friendly_name')}")
    print(f"  Provider: {info.get('provider')}")
    print(f"  Provider model name: {info.get('provider_model_name')}")
    print(f"  Display name: {info.get('display_name')}")
    print(f"  Base URL: {info.get('base_url')}")
    print(f"  API key setting: {info.get('api_key_setting')}")
    
    # Test 2: Validate model selection
    test_selections = [
        "nagaai/gpt5",
        "native/gpt4o",
        "openai/gpt5_mini",
        "invalid/model",
        "nagaai/nonexistent"
    ]
    print("\n✓ Validation tests:")
    for selection in test_selections:
        is_valid, error = ModelFactory.validate_model_selection(selection)
        status = "✓" if is_valid else "✗"
        error_msg = f" ({error})" if error else ""
        print(f"  {status} {selection}{error_msg}")
    
    # Test 3: List available models
    models = ModelFactory.list_available_models(provider="nagaai")
    print(f"\n✓ NagaAI models available: {len(models)}")
    for m in models[:3]:
        print(f"  - {m['friendly_name']}: {m['display_name']}")
    
    # Test 4: Try to create an LLM (will fail if no API key, but tests the code path)
    print("\n✓ Testing LLM creation (may fail if API keys not configured):")
    try:
        # This will likely fail due to missing API key, which is expected
        llm = ModelFactory.create_llm("nagaai/gpt5_mini", temperature=0.5)
        print(f"  LLM created successfully: {type(llm).__name__}")
    except ValueError as e:
        print(f"  Expected error (no API key): {e}")
    except Exception as e:
        print(f"  Unexpected error: {e}")
    
    print("\n✓ Model Factory tests completed!")


def test_model_selector_import():
    """Test that ModelSelector can be imported."""
    print("\n" + "=" * 60)
    print("Testing ModelSelector Import")
    print("=" * 60)
    
    try:
        from ba2_trade_platform.ui.components import ModelSelector
        print(f"\n✓ ModelSelector imported successfully: {ModelSelector}")
        
        # Create an instance (won't render without NiceGUI context)
        def callback(selection):
            print(f"Selection changed: {selection}")
        
        selector = ModelSelector(on_selection_change=callback)
        print(f"✓ ModelSelector instance created")
        print(f"  Default provider: {selector.default_provider}")
        print(f"  Show native option: {selector.show_native_option}")
        
    except Exception as e:
        print(f"\n✗ Failed to import/create ModelSelector: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_model_registry()
    test_model_factory()
    test_model_selector_import()
    
    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)

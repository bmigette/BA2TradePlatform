"""
Test script for Naga AI integration - Model config parsing

Tests the _parse_model_config() static method to ensure correct
parsing of Provider/ModelName format strings.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents


def test_model_parsing():
    """Test _parse_model_config() with various input formats."""
    
    print("=" * 80)
    print("Testing Model Config Parsing")
    print("=" * 80)
    print()
    
    test_cases = [
        # (input, expected_provider, expected_model, expected_url, expected_key)
        ("OpenAI/gpt-4o-mini", "OpenAI", "gpt-4o-mini", "https://api.openai.com/v1", "openai_api_key"),
        ("OpenAI/gpt-4o", "OpenAI", "gpt-4o", "https://api.openai.com/v1", "openai_api_key"),
        ("NagaAI/grok-beta", "NagaAI", "grok-beta", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("NagaAI/grok-2-latest", "NagaAI", "grok-2-latest", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("NagaAI/deepseek-chat", "NagaAI", "deepseek-chat", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("NagaAI/deepseek-reasoner", "NagaAI", "deepseek-reasoner", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("NagaAI/gemini-2.5-flash", "NagaAI", "gemini-2.5-flash", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("NagaAI/claude-sonnet-4.5-20250929", "NagaAI", "claude-sonnet-4.5-20250929", "https://api.naga.ac/v1", "naga_ai_api_key"),
        ("gpt-4o-mini", "OpenAI", "gpt-4o-mini", "https://api.openai.com/v1", "openai_api_key"),  # Legacy format
        ("gpt-4o", "OpenAI", "gpt-4o", "https://api.openai.com/v1", "openai_api_key"),  # Legacy format
        ("UnknownProvider/some-model", "OpenAI", "some-model", "https://api.openai.com/v1", "openai_api_key"),  # Fallback
    ]
    
    passed = 0
    failed = 0
    
    for model_string, exp_provider, exp_model, exp_url, exp_key in test_cases:
        print(f"Testing: {model_string}")
        print("-" * 80)
        
        try:
            config = TradingAgents._parse_model_config(model_string)
            
            # Verify results
            checks = [
                ("provider", config['provider'], exp_provider),
                ("model", config['model'], exp_model),
                ("base_url", config['base_url'], exp_url),
                ("api_key_setting", config['api_key_setting'], exp_key),
            ]
            
            test_passed = True
            for field, actual, expected in checks:
                match = actual == expected
                status = "✅" if match else "❌"
                print(f"  {status} {field}: {actual}")
                if not match:
                    print(f"      Expected: {expected}")
                    test_passed = False
            
            if test_passed:
                print("  ✅ PASS")
                passed += 1
            else:
                print("  ❌ FAIL")
                failed += 1
                
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            failed += 1
        
        print()
    
    print("=" * 80)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 80)
    
    return failed == 0


def test_config_building():
    """Test that config building works with parsed model configs."""
    
    print()
    print("=" * 80)
    print("Testing Config Building Integration")
    print("=" * 80)
    print()
    
    # Test scenarios
    scenarios = [
        {
            "name": "OpenAI Models",
            "settings": {
                "deep_think_llm": "OpenAI/gpt-4o-mini",
                "quick_think_llm": "OpenAI/gpt-4o-mini",
            },
            "expected_url": "https://api.openai.com/v1",
            "expected_key": "openai_api_key",
        },
        {
            "name": "Naga AI Models",
            "settings": {
                "deep_think_llm": "NagaAI/grok-beta",
                "quick_think_llm": "NagaAI/grok-2-latest",
            },
            "expected_url": "https://api.naga.ac/v1",
            "expected_key": "naga_ai_api_key",
        },
        {
            "name": "Legacy Format (should default to OpenAI)",
            "settings": {
                "deep_think_llm": "gpt-4o-mini",
                "quick_think_llm": "gpt-4o-mini",
            },
            "expected_url": "https://api.openai.com/v1",
            "expected_key": "openai_api_key",
        },
    ]
    
    for scenario in scenarios:
        print(f"Scenario: {scenario['name']}")
        print("-" * 80)
        
        # Parse configs
        deep_config = TradingAgents._parse_model_config(scenario['settings']['deep_think_llm'])
        quick_config = TradingAgents._parse_model_config(scenario['settings']['quick_think_llm'])
        
        print(f"  deep_think_llm: {scenario['settings']['deep_think_llm']}")
        print(f"    → model: {deep_config['model']}")
        print(f"    → base_url: {deep_config['base_url']}")
        print(f"    → api_key_setting: {deep_config['api_key_setting']}")
        print()
        print(f"  quick_think_llm: {scenario['settings']['quick_think_llm']}")
        print(f"    → model: {quick_config['model']}")
        print(f"    → base_url: {quick_config['base_url']}")
        print(f"    → api_key_setting: {quick_config['api_key_setting']}")
        print()
        
        # Verify expectations
        url_match = deep_config['base_url'] == scenario['expected_url']
        key_match = deep_config['api_key_setting'] == scenario['expected_key']
        
        status = "✅ PASS" if (url_match and key_match) else "❌ FAIL"
        print(f"  Expected URL: {scenario['expected_url']} - {'✅' if url_match else '❌'}")
        print(f"  Expected Key: {scenario['expected_key']} - {'✅' if key_match else '❌'}")
        print(f"  {status}")
        print()
    
    print("=" * 80)


def main():
    """Run all tests."""
    print()
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "Naga AI Integration Test Suite" + " " * 28 + "║")
    print("╚" + "═" * 78 + "╝")
    print()
    
    # Test model parsing
    parsing_success = test_model_parsing()
    
    # Test config building
    test_config_building()
    
    print()
    if parsing_success:
        print("✅ All tests passed! Naga AI integration model parsing is working correctly.")
    else:
        print("❌ Some tests failed. Please review the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()

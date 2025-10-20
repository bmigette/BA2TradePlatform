"""
Fetch available models from Naga AI API

This script queries the Naga AI API to get the actual list of available models
and their identifiers to ensure we're using correct model names.

NOTE: This requires a valid Naga AI API key. If you don't have one yet,
you can visit https://naga.ac/ to get one.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from openai import OpenAI
from ba2_trade_platform.config import get_app_setting


def fetch_naga_models():
    """Fetch and display available models from Naga AI."""
    
    print("=" * 80)
    print("Fetching Naga AI Models")
    print("=" * 80)
    print()
    
    # Try to get API key from database
    naga_api_key = get_app_setting("naga_ai_api_key")
    
    if not naga_api_key:
        print("⚠️  No Naga AI API key found in database.")
        print()
        print("To fetch the model list, you need to:")
        print("1. Get an API key from https://naga.ac/")
        print("2. Add it to the settings page in the BA2 platform")
        print("3. Run this script again")
        print()
        print("For now, showing example model IDs from documentation:")
        print()
        print_example_models()
        return
    
    try:
        print(f"✅ Found Naga AI API key (ends with: ...{naga_api_key[-8:]})")
        print()
        print("Fetching models from https://api.naga.ac/v1/models...")
        print()
        
        # Initialize OpenAI client with Naga AI endpoint
        client = OpenAI(
            base_url="https://api.naga.ac/v1",
            api_key=naga_api_key
        )
        
        # Fetch models
        models_response = client.models.list()
        models = models_response.data
        
        # Count free vs paid models
        free_models = [m for m in models if ':free' in m.id]
        paid_models = [m for m in models if ':free' not in m.id]
        
        print(f"✅ Found {len(models)} models")
        print(f"   📊 Free tier: {len(free_models)} models")
        print(f"   💰 Paid tier: {len(paid_models)} models")
        
        if len(models) == len(free_models):
            print()
            print("⚠️  NOTE: You are currently on the FREE TIER")
            print("   Only models with ':free' suffix are available.")
            print("   To access ALL models (including premium ones):")
            print("   1. Visit https://naga.ac/")
            print("   2. Go to Billing/Credits")
            print("   3. Add credits to your account")
            print("   4. Run this script again to see all premium models")
        
        print()
        print("=" * 80)
        print("Available Models")
        print("=" * 80)
        print()
        
        # Group models by provider
        models_by_provider = {}
        for model in models:
            provider = model.owned_by
            if provider not in models_by_provider:
                models_by_provider[provider] = []
            models_by_provider[provider].append(model)
        
        # Display models grouped by provider
        for provider in sorted(models_by_provider.keys()):
            print(f"\n📦 {provider.upper()}")
            print("-" * 80)
            
            provider_models = models_by_provider[provider]
            for model in sorted(provider_models, key=lambda x: x.id):
                # Show model ID and tiers
                tiers = ", ".join(model.available_tiers) if hasattr(model, 'available_tiers') else "N/A"
                
                # Show input/output modalities
                input_mod = []
                output_mod = []
                if hasattr(model, 'architecture'):
                    input_mod = model.architecture.get('input_modalities', [])
                    output_mod = model.architecture.get('output_modalities', [])
                
                modalities = f"In: {','.join(input_mod) if input_mod else 'N/A'} | Out: {','.join(output_mod) if output_mod else 'N/A'}"
                
                print(f"  • {model.id:<40} [{tiers}] ({modalities})")
        
        print()
        print("=" * 80)
        print()
        
        # Extract models for TradingAgents
        print("Recommended Models for TradingAgents:")
        print("-" * 80)
        
        # Look for text-only or text+reasoning models
        chat_models = [m for m in models if 'chat.completions' in getattr(m, 'supported_endpoints', [])]
        
        # Categorize by use case
        print()
        print("🧠 Deep Thinking Models (for deep_think_llm):")
        deep_think = [m.id for m in chat_models if any(x in m.id.lower() for x in ['o1', 'o3', 'sonnet', 'opus', 'gemini-2', 'deepseek', 'grok'])]
        for model_id in deep_think[:10]:  # Show top 10
            print(f"  - NagaAI/{model_id}")
        
        print()
        print("⚡ Quick Thinking Models (for quick_think_llm):")
        quick_think = [m.id for m in chat_models if any(x in m.id.lower() for x in ['mini', 'haiku', 'flash', 'gpt-4o', 'turbo'])]
        for model_id in quick_think[:10]:  # Show top 10
            print(f"  - NagaAI/{model_id}")
        
        print()
        print("🔍 Web Search Models (for dataprovider_websearch_model):")
        print("  Note: Most models support web search. Check Naga AI docs for specifics.")
        web_search = [m.id for m in chat_models if any(x in m.id.lower() for x in ['gemini', 'grok', 'gpt'])]
        for model_id in web_search[:10]:  # Show top 10
            print(f"  - NagaAI/{model_id}")
        
        print()
        print("=" * 80)
        
    except Exception as e:
        print(f"❌ Error fetching models: {e}")
        print()
        print("This could be due to:")
        print("  - Invalid API key")
        print("  - Network connectivity issues")
        print("  - Naga AI API unavailable")
        print()
        print("Showing example models from documentation instead:")
        print()
        print_example_models()


def print_example_models():
    """Print example models from Naga AI documentation."""
    
    print("=" * 80)
    print("Example Models from Documentation")
    print("=" * 80)
    print()
    
    examples = {
        "OpenAI": [
            "gpt-4o",
            "gpt-4o-mini",
            "o1",
            "o1-mini",
            "o3-mini",
        ],
        "Anthropic": [
            "claude-sonnet-4.5-20250929",
            "claude-opus-4-20250514",
            "claude-haiku-4-20250514",
        ],
        "Google": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ],
        "xAI": [
            "grok-beta",
            "grok-2-latest",
        ],
        "DeepSeek": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    }
    
    for provider, models in examples.items():
        print(f"\n📦 {provider.upper()}")
        print("-" * 80)
        for model in models:
            print(f"  • {model}")
    
    print()
    print("=" * 80)
    print()
    print("💡 TIP: Get your own API key at https://naga.ac/ to see the full list!")
    print()


if __name__ == "__main__":
    fetch_naga_models()

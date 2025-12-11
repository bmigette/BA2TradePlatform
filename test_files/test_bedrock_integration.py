"""
Test AWS Bedrock integration in ModelFactory and models_registry.
"""

def test_bedrock_provider():
    """Test that Bedrock provider is properly configured."""
    from ba2_trade_platform.core.models_registry import (
        PROVIDER_BEDROCK, PROVIDER_CONFIG, MODELS, 
        get_model_for_provider, get_provider_config
    )
    
    print("=" * 60)
    print("AWS Bedrock Integration Test")
    print("=" * 60)
    
    # Test 1: Check PROVIDER_BEDROCK constant
    print(f"\n1. PROVIDER_BEDROCK constant: {PROVIDER_BEDROCK}")
    assert PROVIDER_BEDROCK == "bedrock", "PROVIDER_BEDROCK should be 'bedrock'"
    print("   ✓ PROVIDER_BEDROCK is correctly defined")
    
    # Test 2: Check Bedrock provider configuration
    print(f"\n2. Bedrock provider config:")
    bedrock_config = PROVIDER_CONFIG.get(PROVIDER_BEDROCK)
    assert bedrock_config is not None, "Bedrock config should exist"
    print(f"   - display_name: {bedrock_config.get('display_name')}")
    print(f"   - langchain_class: {bedrock_config.get('langchain_class')}")
    print(f"   - api_key_setting: {bedrock_config.get('api_key_setting')}")
    print(f"   - requires_additional_settings: {bedrock_config.get('requires_additional_settings')}")
    assert bedrock_config.get("langchain_class") == "ChatBedrockConverse"
    print("   ✓ Bedrock provider config is correct")
    
    # Test 3: Check models with Bedrock support
    print(f"\n3. Models with Bedrock support:")
    bedrock_models = []
    for model_name, model_info in MODELS.items():
        if PROVIDER_BEDROCK in model_info.get("provider_names", {}):
            bedrock_model_id = model_info["provider_names"][PROVIDER_BEDROCK]
            bedrock_models.append((model_name, bedrock_model_id))
            print(f"   - {model_name}: {bedrock_model_id}")
    
    assert len(bedrock_models) > 0, "Should have at least one model with Bedrock support"
    print(f"   ✓ Found {len(bedrock_models)} models with Bedrock support")
    
    # Test 4: Test get_model_for_provider with Bedrock
    print(f"\n4. Testing get_model_for_provider():")
    for model_name, expected_id in bedrock_models[:2]:  # Test first 2
        bedrock_id = get_model_for_provider(model_name, PROVIDER_BEDROCK)
        print(f"   - {model_name} -> {bedrock_id}")
        assert bedrock_id == expected_id
    print("   ✓ get_model_for_provider() works correctly")
    
    # Test 5: Test get_provider_config
    print(f"\n5. Testing get_provider_config():")
    config = get_provider_config(PROVIDER_BEDROCK)
    assert config is not None
    assert config.get("display_name") == "AWS Bedrock"
    print(f"   - display_name: {config.get('display_name')}")
    print("   ✓ get_provider_config() works correctly")
    
    print("\n" + "=" * 60)
    print("All Bedrock integration tests passed! ✓")
    print("=" * 60)


def test_model_factory_bedrock():
    """Test that ModelFactory can parse Bedrock model selections."""
    from ba2_trade_platform.core.ModelFactory import ModelFactory
    
    print("\n" + "=" * 60)
    print("ModelFactory Bedrock Test")
    print("=" * 60)
    
    # Test get_model_info for Bedrock
    print("\n1. Testing ModelFactory.get_model_info() for Bedrock:")
    info = ModelFactory.get_model_info("bedrock/claude_4_sonnet")
    print(f"   - friendly_name: {info.get('friendly_name')}")
    print(f"   - provider: {info.get('provider')}")
    print(f"   - provider_model_name: {info.get('provider_model_name')}")
    print(f"   - display_name: {info.get('display_name')}")
    
    assert info.get("provider") == "bedrock"
    assert info.get("provider_model_name") == "anthropic.claude-sonnet-4-20250514-v1:0"
    print("   ✓ ModelFactory.get_model_info() works for Bedrock")
    
    print("\n" + "=" * 60)
    print("ModelFactory Bedrock test passed! ✓")
    print("=" * 60)


if __name__ == "__main__":
    test_bedrock_provider()
    test_model_factory_bedrock()

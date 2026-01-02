"""Check which models are configured for TradingAgents experts."""
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting
from sqlmodel import select
import json

with get_db() as db:
    experts = list(db.exec(select(ExpertInstance).where(ExpertInstance.expert == 'TradingAgents')))
    
    print(f"Found {len(experts)} TradingAgents experts\n")
    
    grok4_users = []
    grok4_fast_users = []
    
    for expert in experts:
        print(f"=" * 80)
        print(f"Expert ID: {expert.id}")
        print(f"Account ID: {expert.account_id}")
        
        # Get all settings for this expert
        settings = list(db.exec(select(ExpertSetting).where(ExpertSetting.instance_id == expert.id)))
        
        # Create a dictionary of settings
        settings_dict = {}
        for setting in settings:
            if setting.value_str:
                settings_dict[setting.key] = setting.value_str
            elif setting.value_json:
                settings_dict[setting.key] = setting.value_json
            elif setting.value_float is not None:
                settings_dict[setting.key] = setting.value_float
        
        # Check the key model settings
        deep_think = settings_dict.get('deep_think_llm', 'NOT SET')
        quick_think = settings_dict.get('quick_think_llm', 'NOT SET')
        websearch = settings_dict.get('dataprovider_websearch_model', 'NOT SET')
        
        print(f"\nModel Configuration:")
        print(f"  Deep Think LLM: {deep_think}")
        print(f"  Quick Think LLM: {quick_think}")
        print(f"  WebSearch Model: {websearch}")
        
        # Check if any of them use grok-4 (non-fast)
        models_to_check = [
            ('Deep Think', deep_think),
            ('Quick Think', quick_think),
            ('WebSearch', websearch)
        ]
        
        for model_name, model_value in models_to_check:
            if isinstance(model_value, str):
                # Normalize to lowercase for comparison
                model_lower = model_value.lower()
                
                # Check if it's Grok-4 (non-fast variant)
                if 'grok' in model_lower and '4' in model_lower:
                    # Check if it's NOT a fast variant
                    if 'fast' not in model_lower and '4.1' not in model_lower:
                        print(f"  ⚠️ WARNING: {model_name} using Grok-4 (not fast variant)!")
                        grok4_users.append((expert.id, model_name, model_value))
                    else:
                        grok4_fast_users.append((expert.id, model_name, model_value))
        
        print()
    
    print("\n" + "=" * 80)
    print("SUMMARY:")
    print("=" * 80)
    
    if grok4_users:
        print(f"\n⚠️ {len(grok4_users)} instances using Grok-4 (non-fast):")
        for expert_id, model_type, model_value in grok4_users:
            print(f"  - Expert {expert_id}: {model_type} = {model_value}")
    else:
        print("\n✅ No experts using Grok-4 (non-fast)")
    
    if grok4_fast_users:
        print(f"\n✅ {len(grok4_fast_users)} instances using Grok-4 Fast variants:")
        for expert_id, model_type, model_value in grok4_fast_users:
            print(f"  - Expert {expert_id}: {model_type} = {model_value}")


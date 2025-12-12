"""
Check websearch model settings for all TradingAgents expert instances.
"""
import sys
sys.path.insert(0, '.')

from sqlmodel import Session, select
from ba2_trade_platform.core.db import engine
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting

def check_websearch_settings():
    """Check all TradingAgents instances for websearch model settings."""
    
    with Session(engine) as session:
        # Get all TradingAgents expert instances
        experts = session.exec(select(ExpertInstance).where(
            ExpertInstance.expert == "TradingAgents"
        )).all()
        
        if not experts:
            print("No TradingAgents expert instances found.")
            return
        
        print(f"Found {len(experts)} TradingAgents expert instance(s):\n")
        
        for expert in experts:
            print(f"Expert ID: {expert.id}")
            print(f"  Account ID: {expert.account_id}")
            print(f"  Alias: {expert.alias or '(none)'}")
            print(f"  Enabled: {expert.enabled}")
            
            # Get all settings for this expert
            settings = session.exec(select(ExpertSetting).where(
                ExpertSetting.instance_id == expert.id
            )).all()
            
            settings_dict = {s.key: s.value_str for s in settings}
            
            # Check websearch-related settings
            websearch_model = settings_dict.get('dataprovider_websearch_model', '(not set)')
            openai_provider_model = settings_dict.get('openai_provider_model', '(not set)')  # Old name
            
            print(f"  dataprovider_websearch_model: {websearch_model}")
            if openai_provider_model != '(not set)':
                print(f"  openai_provider_model (OLD): {openai_provider_model}")
            
            # Check main LLM settings too
            deep_think = settings_dict.get('deep_think_llm', '(not set)')
            quick_think = settings_dict.get('quick_think_llm', '(not set)')
            
            print(f"  deep_think_llm: {deep_think}")
            print(f"  quick_think_llm: {quick_think}")
            print()

if __name__ == "__main__":
    check_websearch_settings()

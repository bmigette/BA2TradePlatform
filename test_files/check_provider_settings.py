"""Check provider settings for expert 11 and 13"""
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertSetting
from sqlmodel import select
import json

with get_db() as session:
    print("Expert 11 provider settings:")
    settings_11 = session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == 11)).all()
    for s in settings_11:
        if 'provider' in s.key.lower():
            print(f"  {s.key}: {type(s.value_json).__name__}")
            if s.value_json:
                print(f"    Value: {json.dumps(s.value_json, indent=6)[:200]}")
    
    print("\nExpert 13 provider settings:")
    settings_13 = session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == 13)).all()
    for s in settings_13:
        if 'provider' in s.key.lower():
            print(f"  {s.key}: {type(s.value_json).__name__}")
            if s.value_json:
                print(f"    Value: {json.dumps(s.value_json, indent=6)[:200]}")

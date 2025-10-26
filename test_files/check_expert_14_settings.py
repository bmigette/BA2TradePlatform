"""Check expert 14 settings for max_virtual_equity_per_instrument_percent"""
from ba2_trade_platform.core.db import get_instance, get_db
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting
from sqlmodel import select

expert = get_instance(ExpertInstance, 14)
print(f"Expert 14 - virtual_equity_pct: {expert.virtual_equity_pct}%")

with get_db() as session:
    settings = session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == 14)).all()
    print(f"\nTotal settings count: {len(settings)}")
    print("\nAll settings:")
    for s in settings:
        # Display value based on what's filled
        if s.value_float is not None:
            print(f"  {s.key} = {s.value_float}")
        elif s.value_str is not None:
            print(f"  {s.key} = {s.value_str}")
        elif s.value_json:
            print(f"  {s.key} = {s.value_json}")
        else:
            print(f"  {s.key} = None")

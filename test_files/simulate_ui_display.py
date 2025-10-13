"""Simulate UI's scheduled jobs display for expert 8"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core.db import get_all_instances
from ba2_trade_platform.core.utils import get_expert_instance_from_id
import json

print("="*60)
print("Simulating UI Scheduled Jobs Display")
print("="*60)

# Get expert 8
expert_instances = get_all_instances(ExpertInstance)
expert_8 = [e for e in expert_instances if e.id == 8][0]

print(f"\n✅ Found Expert 8:")
print(f"   ID: {expert_8.id}")
print(f"   Name: {expert_8.expert}")
print(f"   Alias: {expert_8.alias}")
print(f"   Enabled: {expert_8.enabled}")

# Get expert instance (with methods)
expert = get_expert_instance_from_id(8)

# Get enabled instruments (what UI will see)
enabled_instruments = expert.get_enabled_instruments()
print(f"\n📊 Enabled Instruments (from get_enabled_instruments):")
print(f"   {enabled_instruments}")

# Get schedule settings
enter_market_schedule = expert.settings.get('execution_schedule_enter_market')
open_positions_schedule = expert.settings.get('execution_schedule_open_positions')

print(f"\n📅 Schedule Settings:")
print(f"\n   Enter Market Schedule:")
if enter_market_schedule:
    if isinstance(enter_market_schedule, str):
        schedule = json.loads(enter_market_schedule)
    else:
        schedule = enter_market_schedule
    days = schedule.get('days', {})
    times = schedule.get('times', [])
    enabled_days = [day for day, enabled in days.items() if enabled]
    print(f"      Days: {', '.join(enabled_days)}")
    print(f"      Times: {', '.join(times)}")
else:
    print(f"      Not configured")

print(f"\n   Open Positions Schedule:")
if open_positions_schedule:
    if isinstance(open_positions_schedule, str):
        schedule = json.loads(open_positions_schedule)
    else:
        schedule = open_positions_schedule
    days = schedule.get('days', {})
    times = schedule.get('times', [])
    enabled_days = [day for day, enabled in days.items() if enabled]
    print(f"      Days: {', '.join(enabled_days)}")
    print(f"      Times: {', '.join(times)}")
else:
    print(f"      Not configured")

# Show what will appear in UI table
print(f"\n" + "="*60)
print("UI Table Entries (what you'll see):")
print("="*60)

if enabled_instruments and enter_market_schedule:
    for symbol in enabled_instruments:
        print(f"\n✅ Row 1:")
        print(f"   Symbol: {symbol}")
        print(f"   Expert: {expert_8.alias or expert_8.expert}")
        print(f"   Job Type: Enter Market")
        print(f"   Weekdays: Mon, Tue, Wed, Thu, Fri")
        print(f"   Times: 09:30")

if enabled_instruments and open_positions_schedule:
    for symbol in enabled_instruments:
        print(f"\n✅ Row 2:")
        print(f"   Symbol: {symbol}")
        print(f"   Expert: {expert_8.alias or expert_8.expert}")
        print(f"   Job Type: Open Positions")
        print(f"   Weekdays: Mon, Tue, Thu")
        print(f"   Times: 14:30")

if not enabled_instruments:
    print(f"\n❌ No jobs would be displayed (empty enabled_instruments)")

"""Test expert 8 instrument selection in isolation"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.logger import logger

print("="*60)
print("Testing Expert 8 - FMPSenateTraderCopy")
print("="*60)

# Get expert instance
expert_id = 8
expert = get_expert_instance_from_id(expert_id)

if not expert:
    print(f"❌ Expert instance {expert_id} not found!")
    sys.exit(1)

print(f"\n✅ Expert loaded: {expert.__class__.__name__}")
print(f"   Expert ID: {expert_id}")

# Get settings
print(f"\n📋 Settings:")
print(f"   instrument_selection_method: {expert.settings.get('instrument_selection_method', 'MISSING')}")
print(f"   should_expand_instrument_jobs: {expert.settings.get('should_expand_instrument_jobs', 'MISSING')}")

# Get expert properties
print(f"\n🔧 Expert Properties:")
try:
    expert_properties = expert.__class__.get_expert_properties()
    print(f"   can_recommend_instruments: {expert_properties.get('can_recommend_instruments', False)}")
    print(f"   should_expand_instrument_jobs: {expert_properties.get('should_expand_instrument_jobs', True)}")
except Exception as e:
    print(f"   ❌ Error getting properties: {e}")
    import traceback
    traceback.print_exc()

# Test the logic
print(f"\n🧪 Testing _get_enabled_instruments logic:")
instrument_selection_method = expert.settings.get('instrument_selection_method', 'static')
expert_properties = expert.__class__.get_expert_properties()
can_recommend_instruments = expert_properties.get('can_recommend_instruments', False)

print(f"   instrument_selection_method = '{instrument_selection_method}'")
print(f"   can_recommend_instruments = {can_recommend_instruments}")

if instrument_selection_method == 'expert' and can_recommend_instruments:
    print(f"   ✅ Should return ['EXPERT']")
    result = ["EXPERT"]
else:
    print(f"   ❌ Will NOT return ['EXPERT']")
    print(f"   Condition: instrument_selection_method=='expert' = {instrument_selection_method == 'expert'}")
    print(f"   Condition: can_recommend_instruments = {can_recommend_instruments}")
    result = []

print(f"\n📊 Result: {result}")

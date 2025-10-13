"""Test if expert 8's get_enabled_instruments now returns EXPERT"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.utils import get_expert_instance_from_id

print("="*60)
print("Testing Expert 8 - get_enabled_instruments()")
print("="*60)

# Get expert instance
expert = get_expert_instance_from_id(8)

if not expert:
    print("❌ Expert instance 8 not found!")
    sys.exit(1)

print(f"\n✅ Expert loaded: {expert.__class__.__name__}")

# Test get_enabled_instruments
print(f"\n🧪 Testing get_enabled_instruments()...")
enabled_instruments = expert.get_enabled_instruments()

print(f"   Result: {enabled_instruments}")

if enabled_instruments == ["EXPERT"]:
    print(f"   ✅ Correct! Returns ['EXPERT'] for expert selection method")
else:
    print(f"   ❌ Wrong! Expected ['EXPERT'], got {enabled_instruments}")
    
print(f"\n📋 Expert Settings:")
print(f"   instrument_selection_method: {expert.settings.get('instrument_selection_method')}")
print(f"   should_expand_instrument_jobs: {expert.settings.get('should_expand_instrument_jobs')}")

print(f"\n🔧 Expert Properties:")
expert_properties = expert.__class__.get_expert_properties()
print(f"   can_recommend_instruments: {expert_properties.get('can_recommend_instruments')}")
print(f"   should_expand_instrument_jobs: {expert_properties.get('should_expand_instrument_jobs')}")

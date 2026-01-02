import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

import sqlite3

# Connect directly to the SQLite database
conn = sqlite3.connect(r'C:\Users\basti\Documents\ba2_trade_platform\db.sqlite')
cursor = conn.cursor()

print("CHECKING ALL GROK CONFIGURATIONS (INCLUDING DISABLED EXPERTS)\n")
print("=" * 100)

# Get ALL experts (including disabled) with Grok settings
query = """
SELECT 
    ei.id as expert_id,
    ei.account_id,
    ei.enabled,
    ei.expert,
    es.key,
    es.value_str
FROM expertinstance ei
LEFT JOIN expertsetting es ON ei.id = es.instance_id
WHERE es.key IN ('deep_think_llm', 'quick_think_llm', 'dataprovider_websearch_model', 'risk_manager_model', 'dynamic_instrument_selection_model')
    AND (es.value_str LIKE '%grok%' OR es.value_str LIKE '%Grok%')
ORDER BY ei.expert, ei.id, es.key
"""

cursor.execute(query)
results = cursor.fetchall()

print(f"Found {len(results)} Grok configurations across all experts:\n")

grok4_count = {}
grok4_fast_count = {}
by_expert_type = {}

current_expert = None
for expert_id, account_id, enabled, expert_type, key, value in results:
    if current_expert != expert_id:
        if current_expert is not None:
            print()
        current_expert = expert_id
        status = "✅ ENABLED" if enabled else "❌ DISABLED"
        print(f"{expert_type} Expert {expert_id} (Account {account_id}) - {status}:")
    
    print(f"  {key} = {value}")
    
    # Track by expert type
    if expert_type not in by_expert_type:
        by_expert_type[expert_type] = {'grok4': 0, 'grok4_fast': 0, 'enabled': enabled}
    
    # Count grok-4 vs grok-4-fast
    if 'grok' in value.lower() and '4' in value:
        if 'fast' not in value.lower() and '4.1' not in value:
            grok4_count[f"{expert_type}_Expert{expert_id}_{key}"] = value
            by_expert_type[expert_type]['grok4'] += 1
            print(f"    ⚠️ Non-fast Grok-4!")
        else:
            grok4_fast_count[f"{expert_type}_Expert{expert_id}_{key}"] = value
            by_expert_type[expert_type]['grok4_fast'] += 1

print("\n" + "=" * 100)
print("SUMMARY BY EXPERT TYPE:")
print("=" * 100)

for expert_type, counts in by_expert_type.items():
    print(f"\n{expert_type}:")
    print(f"  Non-fast Grok-4: {counts['grok4']} settings")
    print(f"  Fast Grok-4: {counts['grok4_fast']} settings")

print("\n" + "=" * 100)
print("DETAILED BREAKDOWN:")
print("=" * 100)

print(f"\n⚠️ Non-fast Grok-4 configurations ({len(grok4_count)} total):")
for key, value in grok4_count.items():
    print(f"  - {key}: {value}")

print(f"\n✅ Fast Grok-4 configurations ({len(grok4_fast_count)} total):")
for key, value in grok4_fast_count.items():
    print(f"  - {key}: {value}")

# Now check for Smart Risk Manager and other features
print("\n" + "=" * 100)
print("CHECKING SMART RISK MANAGER SETTINGS:")
print("=" * 100)

query2 = """
SELECT 
    ei.id as expert_id,
    ei.account_id,
    ei.enabled,
    ei.expert,
    es.key,
    es.value_str
FROM expertinstance ei
LEFT JOIN expertsetting es ON ei.id = es.instance_id
WHERE es.key IN ('risk_manager_model', 'dynamic_instrument_selection_model')
ORDER BY ei.id, es.key
"""

cursor.execute(query2)
results2 = cursor.fetchall()

if results2:
    current_expert = None
    for expert_id, account_id, enabled, expert_type, key, value in results2:
        if current_expert != expert_id:
            if current_expert is not None:
                print()
            current_expert = expert_id
            status = "✅ ENABLED" if enabled else "❌ DISABLED"
            print(f"{expert_type} Expert {expert_id} (Account {account_id}) - {status}:")
        
        print(f"  {key} = {value}")
        
        if value and ('grok' in value.lower() or 'Grok' in value):
            if 'fast' not in value.lower() and '4.1' not in value:
                print(f"    ⚠️ Non-fast Grok model!")
else:
    print("No Smart Risk Manager or Dynamic Instrument Selection settings found")

conn.close()

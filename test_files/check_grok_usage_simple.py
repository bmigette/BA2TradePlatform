import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

import sqlite3

# Connect directly to the SQLite database
conn = sqlite3.connect(r'C:\Users\basti\Documents\ba2_trade_platform\db.sqlite')
cursor = conn.cursor()

# Get all TradingAgents experts and their Grok settings
query = """
SELECT 
    ei.id as expert_id,
    ei.account_id,
    es.key,
    es.value_str
FROM expertinstance ei
LEFT JOIN expertsetting es ON ei.id = es.instance_id
WHERE ei.expert = 'TradingAgents'
    AND ei.enabled = 1
    AND es.key IN ('deep_think_llm', 'quick_think_llm', 'dataprovider_websearch_model')
    AND (es.value_str LIKE '%grok%' OR es.value_str LIKE '%Grok%')
ORDER BY ei.id, es.key
"""

cursor.execute(query)
results = cursor.fetchall()

print(f"Found {len(results)} Grok model configurations in enabled experts:\n")
print("=" * 100)

grok4_non_fast = []
grok4_fast = []

for expert_id, account_id, key, value in results:
    print(f"Expert {expert_id} (Account {account_id}): {key} = {value}")
    
    # Check if it's non-fast Grok-4
    if 'grok' in value.lower() and '4' in value:
        if 'fast' not in value.lower() and '4.1' not in value:
            grok4_non_fast.append((expert_id, key, value))
            print(f"  ⚠️ WARNING: This is Grok-4 (non-fast)!")
        else:
            grok4_fast.append((expert_id, key, value))
            print(f"  ✅ This is Grok-4 Fast")
    print()

print("\n" + "=" * 100)
print("SUMMARY:")
print("=" * 100)

if grok4_non_fast:
    print(f"\n⚠️ {len(grok4_non_fast)} configurations using Grok-4 (non-fast, expensive):")
    for expert_id, key, value in grok4_non_fast:
        print(f"  - Expert {expert_id}: {key} = {value}")
else:
    print("\n✅ No experts using Grok-4 (non-fast)")

if grok4_fast:
    print(f"\n✅ {len(grok4_fast)} configurations using Grok-4 Fast (correct):")
    grouped = {}
    for expert_id, key, value in grok4_fast:
        if expert_id not in grouped:
            grouped[expert_id] = []
        grouped[expert_id].append(f"{key}={value}")
    
    for expert_id, configs in grouped.items():
        print(f"  - Expert {expert_id}: {', '.join(configs)}")

conn.close()

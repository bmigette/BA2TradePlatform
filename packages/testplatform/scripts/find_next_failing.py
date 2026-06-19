import json

with open('feature_list.json') as f:
    data = json.load(f)

failing = [f for f in data if not f.get('passes', False)]

print(f"Total failing features: {len(failing)}")
print("\nNext 10 failing features:")
print("=" * 80)
for i, feature in enumerate(failing[:10], 1):
    print(f"{i}. [FAIL] {feature.get('description')}")
    if feature.get('steps'):
        print(f"   First step: {feature['steps'][0]}")
    print()

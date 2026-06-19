import json

with open('feature_list.json') as f:
    data = json.load(f)

failing_ui = [f for f in data if not f.get('passes', False) and f.get('category') == 'ui']

print(f"Total failing UI features: {len(failing_ui)}")
print("\nNext 10 failing UI features:")
print("=" * 80)
for i, feature in enumerate(failing_ui[:10], 1):
    print(f"{i}. [FAIL] {feature.get('description')}")
    if feature.get('steps'):
        print(f"   First step: {feature['steps'][0]}")
    print()

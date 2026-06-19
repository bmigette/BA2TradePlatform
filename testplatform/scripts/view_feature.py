import json
import sys

feature_id = int(sys.argv[1]) if len(sys.argv) > 1 else 126

with open('feature_list.json') as f:
    data = json.load(f)

feature = None
for f in data:
    if f.get('id') == feature_id:
        feature = f
        break

if feature:
    print(f"Feature {feature_id}: {feature.get('description')}")
    print(f"Category: {feature.get('category')}")
    print(f"Passes: {feature.get('passes')}")
    print("\nTest Steps:")
    for i, step in enumerate(feature.get('test_steps', []), 1):
        print(f"  {i}. {step}")
else:
    print(f"Feature {feature_id} not found")

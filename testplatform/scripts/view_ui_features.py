import json

with open('feature_list.json', 'r') as f:
    features = json.load(f)

print("Features 109-130 (UI/Navigation):")
print("=" * 80)
for i, feature in enumerate(features[108:130], start=109):
    status = "PASS" if feature['passes'] else "FAIL"
    print(f"{i:3d}. [{status}] {feature['description']}")

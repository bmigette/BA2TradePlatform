import json

with open('feature_list.json', 'r') as f:
    features = json.load(f)

print("Features 21-40:")
print("=" * 80)
for i, feature in enumerate(features[20:40], start=21):
    status = "PASS" if feature['passes'] else "FAIL"
    print(f"{i:3d}. [{status}] {feature['description']}")

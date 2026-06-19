import json

with open('feature_list.json') as f:
    data = json.load(f)

ui_features = [(i+1, f) for i, f in enumerate(data) if f['category'] == 'ui' and not f['passes']]

print("Next 15 UI features to implement:")
print("=" * 80)
for idx, feature in ui_features[:15]:
    print(f"{idx}. {feature['description']}")

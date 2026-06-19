import json

# Read the feature list
with open('feature_list.json', 'r') as f:
    features = json.load(f)

# Find and update the zoom and pan feature
for feature in features:
    if 'zoom and pan' in feature.get('description', '').lower():
        feature['passes'] = True
        print(f"Updated feature: {feature['description']}")
        print(f"Passes: {feature['passes']}")
        break

# Write back to file
with open('feature_list.json', 'w') as f:
    json.dump(features, f, indent=2)

print("\nfeature_list.json updated successfully!")

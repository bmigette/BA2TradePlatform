import json

with open('feature_list.json') as f:
    features = json.load(f)

failing = [f for f in features if not f['passes']]
print(f"Total failing features: {len(failing)}\n")
print("Next 10 failing features:")
print("=" * 80)
for i, f in enumerate(failing[:10], 1):
    status = "PASS" if f['passes'] else "FAIL"
    print(f"{i}. [{status}] {f['description']}")
    if 'steps' in f and len(f['steps']) > 0:
        print(f"   First step: {f['steps'][0]}")
    print()

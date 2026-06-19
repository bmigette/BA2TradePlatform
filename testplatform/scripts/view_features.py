import json
import sys

with open('feature_list.json', 'r') as f:
    data = json.load(f)

start = int(sys.argv[1]) if len(sys.argv) > 1 else 1
end = int(sys.argv[2]) if len(sys.argv) > 2 else 20

print(f"Features {start}-{end} status:")
print("="*80)
for i in range(start-1, min(end, len(data))):
    item = data[i]
    status = "PASS" if item["passes"] else "FAIL"
    print(f"{i+1:3d}. [{status}] {item['description']}")

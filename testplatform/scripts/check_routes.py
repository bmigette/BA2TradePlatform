import requests
import json

response = requests.get('http://localhost:8002/openapi.json')
data = response.json()
paths = data['paths'].keys()

print("All routes:")
for path in sorted(paths):
    print(f"  {path}")

preview_routes = [p for p in paths if 'preview' in p]
if preview_routes:
    print(f"\nPreview routes found: {preview_routes}")
else:
    print("\nNo preview routes found in API")
